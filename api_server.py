import os
import gc
import time
import io
import json
import logging
import asyncio
import hashlib
import base64
import threading
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
import xgboost as xgb
from sklearn.metrics import r2_score
import traceback
import holidays
import urllib3
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_GO_KR_API_KEY = "4480c93a63159f09aebc2d0aa5ec7cff37503e60d6297b500e6da8d91e20f5cb"
KEPCO_API_KEY = "6lrb2gu8t5dzg3a3505s"
KMA_API_HUB_KEY = "vDWZwqskT6W1mcKrJL-l4w"

# 🌟 Vercel 서울(icn1) 리전 기상청 및 환경공단(에어코리아) 중계 서버 주소
KMA_PROXY_URL = "https://dtro-web-frontend.vercel.app/api/kma"
# 🌟 Vercel 서울(icn1) 리전 한전(KEPCO) 전용 중계 서버 주소
KEPCO_PROXY_URL = "https://dtro-web-frontend.vercel.app/api/kepco"
KEPCO_ZERO_GRACE_DAYS = 7  # 🌟 과거 0값 확정 유예기간 (7일 이내 0은 재확인, 7일 초과 정상응답 0은 'ZERO' 확정)

# =========================================================================
# 🚨 영구 저장소 (Firebase Realtime Database) 연동 및 데이터 검증 함수
# =========================================================================
FIREBASE_DB_URL = "https://datacenter-app-7a69a-default-rtdb.firebaseio.com"

def is_valid_value(val):
    if val is None or val in ["--", "-", "", "None", "null"]:
        return False
    try:
        if pd.isna(val):
            return False
    except Exception:
        pass
    return True

def is_valid_temp(val):
    if not is_valid_value(val):
        return False
    try:
        fv = float(val)
        return -45.0 < fv < 60.0
    except Exception:
        return False

def is_valid_humi(val):
    if not is_valid_value(val):
        return False
    try:
        fv = float(val)
        return 0.0 < fv <= 100.0
    except Exception:
        return False

# 🌟 [신규 추가] 초미세먼지 시트에서 'Unnamed:'를 제거하고 측정소명을 1행 헤더로 정규화
def normalize_pm25_df(df_raw):
    if df_raw is None or df_raw.empty:
        return pd.DataFrame(columns=['일자']), '일자', []
    df = df_raw.copy()

    pm_date_col = next((c for c in df.columns if 'date' in str(c).lower() or '일자' in str(c)), df.columns[0])

    date_start_idx = 0
    header_row_idx = None
    for r_idx in range(min(3, len(df))):
        val = df.iloc[r_idx][pm_date_col]
        parsed_dt = pd.to_datetime(val, errors='coerce')
        if pd.notna(parsed_dt) and 2000 <= parsed_dt.year <= 2100:
            date_start_idx = r_idx
            break
        else:
            header_row_idx = r_idx

    if header_row_idx is not None:
        # 1행이 Unnamed이고 아래 행에 측정소명이 있는 경우 -> 측정소명을 컬럼명으로 올리고 Unnamed 삭제
        new_cols = []
        keep_indices = []
        for idx, col in enumerate(df.columns):
            if col == pm_date_col:
                new_cols.append('일자')
                keep_indices.append(idx)
            else:
                cell_val = df.iloc[header_row_idx, idx]
                if pd.notna(cell_val) and str(cell_val).strip() != '' and not str(cell_val).startswith('Unnamed'):
                    new_cols.append(str(cell_val).strip())
                    keep_indices.append(idx)
                elif not str(col).startswith('Unnamed'):
                    new_cols.append(str(col).strip())
                    keep_indices.append(idx)
        df = df.iloc[date_start_idx:, keep_indices].copy()
        df.columns = new_cols
    else:
        # 이미 Unnamed 행을 지워서 1행이 측정소명인 경우 -> 빈 Unnamed 열만 제거
        keep_cols = [col for col in df.columns if col == pm_date_col or not str(col).startswith('Unnamed')]
        df = df[keep_cols].copy()
        if pm_date_col != '일자':
            df.rename(columns={pm_date_col: '일자'}, inplace=True)

    df = df.loc[:, ~df.columns.duplicated()].copy()
    df['일자'] = pd.to_datetime(df['일자'], errors='coerce').dt.normalize()
    df = df.dropna(subset=['일자']).copy()
    stn_cols = [c for c in df.columns if c not in ['일자', 'date'] and not str(c).startswith('Unnamed')]
    for c in stn_cols:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df, '일자', stn_cols

def backup_dataset_to_firebase(file_bytes):
    if "여기에_파이어베이스" in FIREBASE_DB_URL:
        logger.warning("⚠️ Firebase DB URL이 설정되지 않아 엑셀 데이터가 영구 저장되지 않습니다.")
        return
    try:
        b64_data = base64.b64encode(file_bytes).decode('utf-8')
        requests.put(f"{FIREBASE_DB_URL}/master_dataset.json", json={"file_data": b64_data}, timeout=15)
        logger.info("✅ 엑셀 데이터셋 Firebase 영구 백업 완료")
    except Exception as e:
        logger.error(f"❌ Firebase 백업 실패: {e}")

def restore_dataset_from_firebase():
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        res = requests.get(f"{FIREBASE_DB_URL}/master_dataset.json", timeout=15)
        if res.status_code == 200 and res.json():
            data = res.json().get("file_data")
            if data:
                with open("uploaded_dataset.xlsx", "wb") as f:
                    f.write(base64.b64decode(data))
                logger.info("✅ 서버 시작됨 - Firebase에서 엑셀 데이터셋 복구 완료")
    except Exception as e:
        logger.error(f"❌ Firebase 엑셀 복구 실패: {e}")

def restore_thresholds_from_firebase():
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        res = requests.get(f"{FIREBASE_DB_URL}/peak_thresholds.json", timeout=5)
        if res.status_code == 200 and res.json():
            global THRESHOLDS
            THRESHOLDS = res.json()
            save_thresholds()
            logger.info("✅ 서버 시작됨 - Firebase에서 경고 임계치 복구 완료")
    except Exception:
        pass

# 🌟 1. 기상청 동네 AWS 과거 데이터 Firebase 병합 저장(PATCH) 및 오류 캐시 자동 정화 복구
def save_aws_cache_to_firebase(new_items: dict = None):
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        aws_all = {k: v for k, v in GLOBAL_WEATHER_CACHE.items() if k.startswith("AWS_")}
        if not aws_all and not new_items: return
        with open("aws_cache.json", "w", encoding="utf-8") as f:
            json.dump(aws_all, f, ensure_ascii=False)
        payload = new_items if new_items else aws_all
        requests.patch(f"{FIREBASE_DB_URL}/aws_weather_cache.json", json=payload, timeout=10)
        logger.info(f"✅ 기상청 AWS 캐시 Firebase 누적 병합(PATCH) 완료 (신규/갱신 {len(payload)}건, 총 {len(aws_all)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase AWS 캐시 저장 실패: {e}")

def restore_aws_cache_from_firebase():
    recent_cutoff = (get_kst_now().replace(tzinfo=None) - timedelta(days=3)).strftime("%Y-%m-%d")

    def split_clean_and_bad_aws(raw_dict):
        cleaned = {}
        bad_keys = {}
        for k, v in raw_dict.items():
            parts = k.split("_")
            if k.endswith("_humi") and is_valid_value(v) and not is_valid_humi(v):
                bad_keys[k] = None
                continue
            if (k.endswith("_tmax") or k.endswith("_tmin")) and is_valid_value(v) and not is_valid_temp(v):
                bad_keys[k] = None
                continue
            if len(parts) >= 4 and parts[2] >= recent_cutoff and not is_valid_value(v):
                bad_keys[k] = None
                continue
            cleaned[k] = v
        return cleaned, bad_keys

    if os.path.exists("aws_cache.json"):
        try:
            with open("aws_cache.json", "r", encoding="utf-8") as f:
                local_aws = json.load(f)
                if isinstance(local_aws, dict):
                    cleaned_local, _ = split_clean_and_bad_aws(local_aws)
                    GLOBAL_WEATHER_CACHE.update(cleaned_local)
        except Exception:
            pass
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        res = requests.get(f"{FIREBASE_DB_URL}/aws_weather_cache.json", timeout=10)
        if res.status_code == 200 and res.json():
            fb_aws = res.json()
            if isinstance(fb_aws, dict):
                cleaned_fb, bad_fb_keys = split_clean_and_bad_aws(fb_aws)
                GLOBAL_WEATHER_CACHE.update(cleaned_fb)
                with open("aws_cache.json", "w", encoding="utf-8") as f:
                    json.dump(cleaned_fb, f, ensure_ascii=False)
                if bad_fb_keys:
                    requests.patch(f"{FIREBASE_DB_URL}/aws_weather_cache.json", json=bad_fb_keys, timeout=10)
                    logger.info(f"🧹 Firebase AWS 오류 캐시({len(bad_fb_keys)}건: -9% 습도 및 최근 결측) 자동 삭제 완료")
                logger.info(f"✅ 서버 시작됨 - Firebase에서 기상청 AWS 캐시 복구 완료 ({len(cleaned_fb)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase AWS 캐시 복구 실패: {e}")

# 🌟 2. 환경공단(에어코리아) 과거 일평균 초미세먼지 Firebase 병합 저장(PATCH) 및 오류 캐시 자동 정화 복구
def save_airkorea_cache_to_firebase(new_items: dict = None):
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        if not GLOBAL_AIRKOREA_CACHE and not new_items: return
        with open("airkorea_cache.json", "w", encoding="utf-8") as f:
            json.dump(GLOBAL_AIRKOREA_CACHE, f, ensure_ascii=False)
        payload = new_items if new_items else GLOBAL_AIRKOREA_CACHE
        requests.patch(f"{FIREBASE_DB_URL}/airkorea_cache.json", json=payload, timeout=10)
        logger.info(f"✅ 환경공단 초미세먼지 캐시 Firebase 누적 병합(PATCH) 완료 (신규/갱신 {len(payload)}건, 총 {len(GLOBAL_AIRKOREA_CACHE)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase 환경공단 캐시 저장 실패: {e}")

def restore_airkorea_cache_from_firebase():
    recent_cutoff = (get_kst_now().replace(tzinfo=None) - timedelta(days=3)).strftime("%Y-%m-%d")

    def split_clean_and_bad_air(raw_dict):
        cleaned = {}
        bad_keys = {}
        for k, v in raw_dict.items():
            parts = k.split("_")
            if len(parts) >= 3 and parts[2] >= recent_cutoff and not is_valid_value(v):
                bad_keys[k] = None
                continue
            cleaned[k] = v
        return cleaned, bad_keys

    if os.path.exists("airkorea_cache.json"):
        try:
            with open("airkorea_cache.json", "r", encoding="utf-8") as f:
                local_air = json.load(f)
                if isinstance(local_air, dict):
                    cleaned_local, _ = split_clean_and_bad_air(local_air)
                    GLOBAL_AIRKOREA_CACHE.update(cleaned_local)
        except Exception:
            pass
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        res = requests.get(f"{FIREBASE_DB_URL}/airkorea_cache.json", timeout=10)
        if res.status_code == 200 and res.json():
            fb_air = res.json()
            if isinstance(fb_air, dict):
                cleaned_air, bad_air_keys = split_clean_and_bad_air(fb_air)
                GLOBAL_AIRKOREA_CACHE.update(cleaned_air)
                with open("airkorea_cache.json", "w", encoding="utf-8") as f:
                    json.dump(cleaned_air, f, ensure_ascii=False)
                if bad_air_keys:
                    requests.patch(f"{FIREBASE_DB_URL}/airkorea_cache.json", json=bad_air_keys, timeout=10)
                    logger.info(f"🧹 Firebase 환경공단 최근 3일 미집계('--') 캐시({len(bad_air_keys)}건) 자동 삭제 완료 (재수집 대기)")
                logger.info(f"✅ 서버 시작됨 - Firebase에서 환경공단 초미세먼지 캐시 복구 완료 ({len(cleaned_air)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase 환경공단 캐시 복구 실패: {e}")
# 🌟 3. 한전(KEPCO) 과거 15분 LP 데이터 압축 및 Firebase 영구 캐싱 (7일 유예기간 적용)
def compress_kepco_day_list(day_list):
    compressed = {}
    if not day_list or not isinstance(day_list, list):
        return compressed
    for item in day_list:
        if not isinstance(item, dict):
            continue
        raw_m = str(item.get("meterNo", "")).strip()
        m_key = f"m_{raw_m}" if raw_m else "m_ALL"
        if m_key not in compressed:
            compressed[m_key] = [0.0] * 96
        arr = compressed[m_key]
        for k, v in item.items():
            if k.startswith("pwr_qty") and k != "pwr_qty":
                try:
                    val = float(v)
                    time_str = k[-4:]
                    hh = int(time_str[:2])
                    mm = int(time_str[2:])
                    idx = 95 if (hh == 24 and mm == 0) else hh * 4 + (mm // 15) - 1
                    if 0 <= idx < 96:
                        arr[idx] = round(arr[idx] + val, 2)
                except Exception:
                    pass
    return compressed

def is_valid_kepco_cache_entry(val):
    if val == "ZERO":
        return True
    if isinstance(val, dict) and len(val) > 0:
        for arr in val.values():
            if isinstance(arr, list) and len(arr) == 96:
                return True
    return False

def get_kepco_entry_total(entry):
    if not entry or entry == "ZERO" or not isinstance(entry, dict):
        return 0.0
    total = 0.0
    for arr in entry.values():
        if isinstance(arr, list):
            for x in arr:
                try:
                    total += float(x)
                except Exception:
                    pass
    return total

def extract_intervals_from_kepco_entry(entry, target_meter_no="전체"):
    interval_usage = [0.0] * 96
    if not entry or entry == "ZERO":
        return interval_usage
    if isinstance(entry, list):
        entry = compress_kepco_day_list(entry)
    if not isinstance(entry, dict):
        return interval_usage
    target_key = f"m_{target_meter_no}" if target_meter_no not in ["", "전체"] else ""
    for m_key, arr in entry.items():
        if not isinstance(arr, list):
            continue
        if target_meter_no in ["", "전체"] or m_key == target_key or m_key == target_meter_no:
            for i in range(min(96, len(arr))):
                try:
                    interval_usage[i] += float(arr[i])
                except Exception:
                    pass
    return interval_usage

def save_kepco_cache_to_firebase(new_items: dict = None):
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        if not GLOBAL_KEPCO_CACHE and not new_items: return
        with open("kepco_cache.json", "w", encoding="utf-8") as f:
            json.dump(GLOBAL_KEPCO_CACHE, f, ensure_ascii=False)
        payload = new_items if new_items else GLOBAL_KEPCO_CACHE
        if not payload: return
        requests.patch(f"{FIREBASE_DB_URL}/kepco_lp_cache.json", json=payload, timeout=15)
        logger.info(f"✅ 한전 15분 LP 캐시 Firebase 누적 병합(PATCH) 완료 (신규/갱신 {len(payload)}건, 총 {len(GLOBAL_KEPCO_CACHE)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase 한전 캐시 저장 실패: {e}")

def flush_pending_kepco_updates(min_batch: int = 1):
    with KEPCO_FETCH_LOCK:
        if len(PENDING_KEPCO_UPDATES) >= min_batch:
            batch = dict(PENDING_KEPCO_UPDATES)
            PENDING_KEPCO_UPDATES.clear()
        else:
            batch = None
    if batch:
        save_kepco_cache_to_firebase(batch)

def restore_kepco_cache_from_firebase():
    grace_cutoff = (get_kst_now().replace(tzinfo=None) - timedelta(days=KEPCO_ZERO_GRACE_DAYS)).strftime("%Y-%m-%d")

    def split_clean_and_bad_kepco(raw_dict):
        cleaned = {}
        bad_keys = {}
        for k, v in raw_dict.items():
            if not is_valid_kepco_cache_entry(v):
                bad_keys[k] = None
                continue
            parts = k.split("_")
            d_part = parts[-1] if len(parts) >= 2 else ""
            # 최근 7일 유예기간 내 'ZERO'가 남아있으면 정화하여 재수집 허용
            if v == "ZERO" and len(d_part) == 10 and d_part >= grace_cutoff:
                bad_keys[k] = None
                continue
            cleaned[k] = v
        return cleaned, bad_keys

    if os.path.exists("kepco_cache.json"):
        try:
            with open("kepco_cache.json", "r", encoding="utf-8") as f:
                local_kepco = json.load(f)
                if isinstance(local_kepco, dict):
                    cleaned_local, _ = split_clean_and_bad_kepco(local_kepco)
                    GLOBAL_KEPCO_CACHE.update(cleaned_local)
        except Exception:
            pass
    if "여기에_파이어베이스" in FIREBASE_DB_URL: return
    try:
        res = requests.get(f"{FIREBASE_DB_URL}/kepco_lp_cache.json", timeout=15)
        if res.status_code == 200 and res.json():
            fb_kepco = res.json()
            if isinstance(fb_kepco, dict):
                cleaned_fb, bad_fb_keys = split_clean_and_bad_kepco(fb_kepco)
                GLOBAL_KEPCO_CACHE.update(cleaned_fb)
                with open("kepco_cache.json", "w", encoding="utf-8") as f:
                    json.dump(cleaned_fb, f, ensure_ascii=False)
                if bad_fb_keys:
                    requests.patch(f"{FIREBASE_DB_URL}/kepco_lp_cache.json", json=bad_fb_keys, timeout=10)
                    logger.info(f"🧹 Firebase 한전 캐시 오류/유예기간 ZERO({len(bad_fb_keys)}건) 자동 정화 완료")
                logger.info(f"✅ 서버 시작됨 - Firebase에서 한전 15분 LP 캐시 복구 완료 ({len(cleaned_fb)}건)")
    except Exception as e:
        logger.error(f"❌ Firebase 한전 캐시 복구 실패: {e}")
# =========================================================================
# 🚨 카카오워크 알림 시스템
# =========================================================================
KAKAO_WEBHOOK_URL = "https://kakaowork.com/bots/hook/1b7bce124e5b4ce28d9b1374a81073ad"

ALERT_FLAGS = {}   
THRESHOLDS = {}    

def load_thresholds():
    global THRESHOLDS
    if os.path.exists("thresholds.json"):
        try:
            with open("thresholds.json", "r", encoding="utf-8") as f:
                THRESHOLDS = json.load(f)
        except Exception:
            THRESHOLDS = {}

def save_thresholds():
    with open("thresholds.json", "w", encoding="utf-8") as f:
        json.dump(THRESHOLDS, f, ensure_ascii=False)

def check_and_send_alert(station: str, current_peak: float):
    if not KAKAO_WEBHOOK_URL or KAKAO_WEBHOOK_URL == "여기에_발급받은_URL을_붙여넣으세요": return
    if station not in THRESHOLDS: return
    
    limit = float(THRESHOLDS[station])
    if current_peak >= limit and limit > 0:
        today_str = get_kst_now().strftime("%Y-%m-%d")
        flag_key = f"{station}_{today_str}"
        
        if flag_key not in ALERT_FLAGS:
            msg = f"🚨 [{station}] 최대수요전력 경고!\n- 현재 피크: {current_peak} kW\n- 설정 기준: {limit} kW"
            try:
                requests.post(KAKAO_WEBHOOK_URL, json={"text": msg}, timeout=3)
                ALERT_FLAGS[flag_key] = True  
                logger.info(f"✅ 카카오워크 알림 발송 완료: {station}")
            except Exception as e:
                logger.error(f"❌ 카카오워크 발송 실패: {e}")

# =========================================================================

http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
http_session.mount('https://', adapter)
http_session.mount('http://', adapter)
http_session.verify = False

GLOBAL_KEPCO_CACHE = {}
GLOBAL_KEPCO_TODAY_CACHE = {}  # 🌟 금일 한전 데이터 10분(600초) 캐싱용
GLOBAL_WEATHER_CACHE = {}
GLOBAL_AIRKOREA_CACHE = {}     # 🌟 환경공단 과거 일평균 초미세먼지 영구 캐시
GLOBAL_AIRKOREA_REALTIME_CACHE = {"time": 0, "data": {}} # 🌟 금일 대구 실시간 초미세먼지 10분 캐시
GLOBAL_EXCEL_CACHE = {"df": None, "mtime": 0}

AWS_FETCH_LOCK = threading.Lock()
AIR_FETCH_LOCK = threading.Lock()
KMA_AWS_COOLDOWN_UNTIL = 0.0
PENDING_AIR_UPDATES = {}
KEPCO_FETCH_LOCK = threading.Lock()
PENDING_KEPCO_UPDATES = {}
GLOBAL_API_CACHE = {
    "dashboard": {},
    "realtime": {},
    "compare": {},
    "predict": {},
    "bill": {}
}
KEPCO_COOLDOWN_UNTIL = 0.0
KEPCO_CONSECUTIVE_FAILS = 0
KEPCO_COOLDOWN_SECONDS = 300  # 🌟 한전 연속 3회 실패 시 5분(300초)간 추가 요청 자동 차단

API_CACHE_LIMITS = {
    "dashboard": 12,
    "realtime": 30,
    "compare": 12,
    "predict": 10,
    "bill": 15,
}

def reset_kepco_cooldown():
    global KEPCO_COOLDOWN_UNTIL, KEPCO_CONSECUTIVE_FAILS
    with KEPCO_FETCH_LOCK:
        KEPCO_COOLDOWN_UNTIL = 0.0
        KEPCO_CONSECUTIVE_FAILS = 0

def record_kepco_result(success: bool):
    global KEPCO_COOLDOWN_UNTIL, KEPCO_CONSECUTIVE_FAILS
    with KEPCO_FETCH_LOCK:
        if success:
            KEPCO_CONSECUTIVE_FAILS = 0
            KEPCO_COOLDOWN_UNTIL = 0.0
        else:
            KEPCO_CONSECUTIVE_FAILS += 1
            if KEPCO_CONSECUTIVE_FAILS >= 3 and time.time() >= KEPCO_COOLDOWN_UNTIL:
                KEPCO_COOLDOWN_UNTIL = time.time() + KEPCO_COOLDOWN_SECONDS
                logger.warning(
                    f"⚠️ 한전 API 연속 {KEPCO_CONSECUTIVE_FAILS}회 응답 없음 - "
                    f"{KEPCO_COOLDOWN_SECONDS // 60}분간 추가 호출을 일시 중단하고 캐시로 대체합니다."
                )

def set_bounded_api_cache(category: str, key: str, value):
    cache_dict = GLOBAL_API_CACHE.setdefault(category, {})
    if key in cache_dict:
        del cache_dict[key]
    cache_dict[key] = value
    max_size = API_CACHE_LIMITS.get(category, 20)
    while len(cache_dict) > max_size:
        oldest_key = next(iter(cache_dict))
        del cache_dict[oldest_key]
def get_kst_now():
    return datetime.utcnow() + timedelta(hours=9)

STATION_AWS_MAP = {
    '설화명곡': '991', '월배기지': '991',
    '서부정류장': '846', '성서산단': '846', '죽전': '846', '반고개': '846',
    '반월당': '143', '종합청사': '143', '남산': '143', '대구은행': '143', 
    '만촌': '143', '수성알파시티': '143', '범물기지': '143', 
    '전체': '143', '1호선': '143', '2호선': '143', '3호선': '143',
    '신천': '860', '방촌': '860',
    '안심': '827', '숙천': '827', '사월': '827', '영남대': '827',
    '금락': '840',
    '문양기지': '992', '대실': '992',
    '칠곡기지': '845', '팔달시장': '845'
}
TARGET_AWS_STNS = set(STATION_AWS_MAP.values())

STATION_AIR_MAP = {
    '전체': '수창동', '1호선': '수창동', '2호선': '수창동', '3호선': '수창동',
    '종합청사': '본동',
    '설화명곡': '화원읍', '월배기지': '진천동', '서부정류장': '본동',
    '반월당': '수창동', '신천': '신암동', '방촌': '용계동',
    '안심': '서호동', '숙천': '서호동', '금락': '서호동',
    '문양기지': '다사읍', '대실': '다사읍', '성서산단': '호림동',
    '죽전': '이곡동', '반고개': '내당동', '대구은행': '대명동',
    '만촌': '만촌동', '수성알파시티': '연호동', '사월': '시지동', '영남대': '시지동',
    '칠곡기지': '태전동', '팔달시장': '침산동', '남산': '남산1동', '범물기지': '지산동'
}
TARGET_AIR_STNS = sorted(set(STATION_AIR_MAP.values()))

STATION_CUST_MAP = {
    '설화명곡': '0526314773', '월배기지': '0526314773', '서부정류장': '0526314773', 
    '반월당': '0526314773', '신천': '0526314773', '방촌': '0526314773', 
    '안심': '0526314773', '숙천': '0526314773', '금락': '0526314773',
    '문양기지': '0530087761', '대실': '0530142327', '성서산단': '0530094940', 
    '죽전': '0530094888', '반고개': '0530094851', '대구은행': '0530166621', 
    '만촌': '0530160011', '수성알파시티': '0530160020', '사월': '0530160039', 
    '영남대': '0537184143',
    '칠곡기지': '0535121367', '팔달시장': '0535121376', '남산': '0535121385', 
    '범물기지': '0535102262',
    '종합청사': '0526066096'
}

STATION_METER_MAP = {
    '설화명곡': '06242061952', '월배기지': '98212012145', '서부정류장': '06242062191',
    '반월당': '06242063013', '신천': '24232028621', '방촌': '06242063100',
    '안심': '24206006285', '숙천': '98232037153', '금락': '24232027850',
}

LINE_STATIONS = {
    '1호선': ['설화명곡', '월배기지', '서부정류장', '반월당', '신천', '방촌', '안심', '숙천', '금락'],
    '2호선': ['문양기지', '대실', '성서산단', '죽전', '반고개', '대구은행', '만촌', '수성알파시티', '사월', '영남대'],
    '3호선': ['칠곡기지', '팔달시장', '남산', '범물기지']
}

STATION_COORD_MAP = {
    '전체': (35.8714, 128.6014), '1호선': (35.8714, 128.6014), '2호선': (35.8714, 128.6014), '3호선': (35.8714, 128.6014),
    '종합청사': (35.8200, 128.5300),
    '설화명곡': (35.7988, 128.4898), '월배기지': (35.8153, 128.5233), '서부정류장': (35.8360, 128.5560),
    '반월당': (35.8648, 128.5933), '신천': (35.8744, 128.6186), '방촌': (35.8770, 128.6657),
    '안심': (35.8718, 128.7183), '숙천': (35.8730, 128.7250), '금락': (35.8750, 128.7300),
    '문양기지': (35.8600, 128.4650), '대실': (35.8580, 128.4810), '성서산단': (35.8520, 128.5080),
    '죽전': (35.8490, 128.5350), '반고개': (35.8590, 128.5720), '대구은행': (35.8590, 128.6120),
    '만촌': (35.8580, 128.6430), '수성알파시티': (35.8400, 128.6810), '사월': (35.8350, 128.7050),
    '영남대': (35.8290, 128.7530), '칠곡기지': (35.9520, 128.5580), '팔달시장': (35.8900, 128.5630),
    '남산': (35.8600, 128.5830), '범물기지': (35.8150, 128.6450)
}

async def auto_cache_warmer():
    await asyncio.sleep(3)
    while True:
        try:
            logger.info("🔥 [상시 캐싱] 경량 백그라운드 갱신을 시작합니다...")
            def warm_up():
                now_kst = get_kst_now()
                today_str = now_kst.strftime("%Y-%m-%d")
                api_start = "2026-09-05"
                default_14d = max((now_kst - timedelta(days=14)).strftime("%Y-%m-%d"), api_start)

                # 🌟 지난 날짜의 금일 임시 한전 캐시 자동 정리 (메모리 누수 방지)
                stale_today_keys = [k for k in list(GLOBAL_KEPCO_TODAY_CACHE.keys()) if not k.endswith(f"_{today_str}")]
                for k in stale_today_keys:
                    GLOBAL_KEPCO_TODAY_CACHE.pop(k, None)

                # 🌟 0단계: 금일 대구 전체 실시간 초미세먼지 단 1회 호출로 갱신
                fetch_airkorea_sido_realtime(is_bypass=True)

                # 🌟 1단계: 기상(ASOS·AWS·금일 Open-Meteo) 및 환경공단(에어코리아) 경량 수집 (Firebase 영구 누적)
                fetch_asos_daily(api_start, today_str)
                with ThreadPoolExecutor(max_workers=4) as weather_exec:
                    for air_stn in TARGET_AIR_STNS:
                        weather_exec.submit(fetch_airkorea_station_daily, air_stn, api_start, today_str, False, False)
                    for lat, lon in set(STATION_COORD_MAP.values()):
                        weather_exec.submit(fetch_openmeteo_env, lat, lon, today_str, today_str)

                with AIR_FETCH_LOCK:
                    if PENDING_AIR_UPDATES:
                        save_airkorea_cache_to_firebase(dict(PENDING_AIR_UPDATES))
                        PENDING_AIR_UPDATES.clear()

                fetch_aws_daily_for_dashboard('143', api_start, today_str, is_bypass=False, is_background=True)

                # 🌟 2단계: 한전 쿨다운이 꺼져 있을 때만 종합 관제(금일·전일) 및 최근 7일 누락 LP만 압축 적재
                if time.time() >= KEPCO_COOLDOWN_UNTIL:
                    get_overview_data("true")

                    # 대시보드 전체 딕셔너리 대신 가벼운 96개 숫자 배열(GLOBAL_KEPCO_CACHE)만 최근 7일 보충
                    for offset in range(1, 8):
                        if time.time() < KEPCO_COOLDOWN_UNTIL:
                            break
                        past_d = (now_kst - timedelta(days=offset)).strftime("%Y-%m-%d")
                        if past_d < api_start:
                            break
                        get_kepco_data_for_station("전체", past_d, is_bypass=False, save_immediately=False)
                        get_kepco_data_for_station("종합청사", past_d, is_bypass=False, save_immediately=False)

                    flush_pending_kepco_updates(min_batch=1)
                else:
                    rem = int(KEPCO_COOLDOWN_UNTIL - time.time())
                    logger.info(f"⏸️ 한전 쿨다운 중({rem}초 남음) - 한전 백그라운드 호출을 건너뜁니다.")

                gc.collect()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, warm_up)
            logger.info("✅ [상시 캐싱 완료] 경량 적재 및 메모리 정리 완료.")
        except Exception as e:
            logger.error(f"상시 캐싱 중 오류 발생: {e}")

        await asyncio.sleep(600)

@app.on_event("startup")
async def startup_event():
    restore_dataset_from_firebase()     
    restore_thresholds_from_firebase()  
    restore_aws_cache_from_firebase()      # 🌟 서버 시작 시 Firebase에서 과거 AWS 기상 캐시 복구 및 오류값 정화
    restore_airkorea_cache_from_firebase() # 🌟 서버 시작 시 Firebase에서 과거 환경공단 미세먼지 캐시 복구 및 미집계 정화
    restore_kepco_cache_from_firebase()    # 🌟 서버 시작 시 Firebase에서 과거 한전 15분 LP 캐시 복구 (7일 유예 정화)
    
    load_excel_dataset()
    load_thresholds()
    gc.collect()
    asyncio.create_task(auto_cache_warmer())
    logger.info("✅ 서버 정상 구동 완료. (경량 상시 캐싱 · 한전 쿨다운 · 카카오워크 감시 가동 중)")

@app.post("/api/threshold/{station}")
def set_threshold(station: str, limit: float):
    THRESHOLDS[station] = limit
    save_thresholds()
    GLOBAL_API_CACHE.get("overview", {}).clear()  # 🌟 [1줄 추가] 임계치 변경 시 종합 관제 신호등 즉시 갱신
    return {"status": "success", "station": station, "limit": limit}

@app.get("/api/threshold/{station}")
def get_threshold(station: str):
    return {"station": station, "limit": THRESHOLDS.get(station, 0.0)}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with open("uploaded_dataset.xlsx", "wb") as f:
            f.write(contents)
            
        backup_dataset_to_firebase(contents)

        for key in GLOBAL_API_CACHE:
            GLOBAL_API_CACHE[key].clear()
        load_excel_dataset()
        return {"status": "success", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/check_dataset")
def check_dataset_status():
    file_path = "uploaded_dataset.xlsx"
    if os.path.exists(file_path):
        modified_time = os.path.getmtime(file_path)
        dt_m = datetime.fromtimestamp(modified_time).strftime('%m월 %d일 %H:%M')
        return {"exists": True, "updated_at": dt_m}
    return {"exists": False}

# 🌟 기상청 및 환경공단 API 수신 상태 & Firebase 영구 캐시 적재 현황 진단 주소
@app.get("/api/test_kma")
def test_kma_connection(date: str = None, reset_cooldown: str = "false"):
    global KMA_AWS_COOLDOWN_UNTIL
    if reset_cooldown.lower() == "true":
        KMA_AWS_COOLDOWN_UNTIL = 0.0

    yesterday_dt = get_kst_now() - timedelta(days=1)
    target_date = date if date else yesterday_dt.strftime("%Y-%m-%d")
    tm2 = target_date.replace("-", "")

    aws_url = f"{KMA_PROXY_URL}?type=aws&tm2={tm2}&obs=ta_max&stn=0&authKey={KMA_API_HUB_KEY}"
    air_url = f"{KMA_PROXY_URL}?type=air_daily&stationName=수창동&inqBginDt={tm2}&inqEndDt={tm2}&serviceKey={DATA_GO_KR_API_KEY}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}

    aws_parsed_stations = {}
    aws_valid_lines = 0
    aws_status = "확인 전"

    try:
        r_aws = http_session.get(aws_url, headers=headers, timeout=8.0)
        r_aws.encoding = 'utf-8'
        for line in r_aws.text.split('\n'):
            s_line = line.strip()
            if s_line and not s_line.startswith('#'):
                parts = s_line.split()
                if len(parts) >= 6 and parts[0].isdigit() and len(parts[0]) == 8:
                    aws_valid_lines += 1
                    s_id = parts[1]
                    if s_id in TARGET_AWS_STNS:
                        aws_parsed_stations[s_id] = parts[5]
        aws_status = "정상 수신" if aws_valid_lines >= 10 else "일일 한도 초과 또는 응답 오류"
    except Exception as e:
        aws_status = f"호출 에러: {str(e)}"

    air_status = "확인 전"
    air_sample = {}
    try:
        r_air = http_session.get(air_url, timeout=8.0)
        if r_air.status_code == 200:
            items = r_air.json().get("response", {}).get("body", {}).get("items", [])
            air_status = f"정상 수신 ({len(items)}건)"
            if items:
                air_sample = items[0]
        else:
            air_status = f"응답 코드: {r_air.status_code}"
    except Exception as e:
        air_status = f"호출 에러: {str(e)}"

    aws_cache_cnt = sum(1 for k in GLOBAL_WEATHER_CACHE if k.startswith("AWS_"))
    asos_cache_cnt = sum(1 for k in GLOBAL_WEATHER_CACHE if k.startswith("ASOS_"))
    air_cache_cnt = len(GLOBAL_AIRKOREA_CACHE)

    return {
        "check_date": target_date,
        "kma_aws_status": aws_status,
        "daegu_8_stations_tmax": aws_parsed_stations,
        "airkorea_daily_status": air_status,
        "airkorea_sample": air_sample,
        "airkorea_realtime_cached": GLOBAL_AIRKOREA_REALTIME_CACHE.get("data", {}),
        "cache_summary": {
            "aws_cached_items": aws_cache_cnt,
            "asos_cached_items": asos_cache_cnt,
            "airkorea_cached_items": air_cache_cnt,
            "kepco_cached_items": len(GLOBAL_KEPCO_CACHE),
            "kepco_zero_items": sum(1 for v in GLOBAL_KEPCO_CACHE.values() if v == "ZERO"),
            "aws_cooldown_remaining_sec": max(0, int(KMA_AWS_COOLDOWN_UNTIL - time.time()))
        }
    }

# 🌟 Vercel 서울(icn1) 한전 중계 수신 상태 및 Firebase 한전 캐시 적재 현황 진단 주소
@app.get("/api/test_kepco")
def test_kepco_connection(date: str = None, cust_no: str = "0526066096", reset_cooldown: str = "false"):
    if reset_cooldown.lower() == "true":
        reset_kepco_cooldown()

    yesterday_dt = get_kst_now() - timedelta(days=1)
    target_date = date if date else yesterday_dt.strftime("%Y-%m-%d")
    date_compact = target_date.replace("-", "")
    lp_status = "확인 전"
    sample_usage = 0.0
    sample_peak = 0.0
    preview = ""

    try:
        r = http_session.get(
            KEPCO_PROXY_URL,
            params={"type": "lp", "custNo": cust_no, "date": date_compact, "serviceKey": KEPCO_API_KEY},
            timeout=10
        )
        preview = r.text[:200]
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and not data.get("errCd") and not data.get("error"):
                d_list = data.get("dayLpDataInfoList") or []
                comp = compress_kepco_day_list(d_list)
                arr = extract_intervals_from_kepco_entry(comp, "전체")
                sample_usage = round(sum(arr), 1)
                sample_peak = round(max(arr) * 4, 1) if arr else 0.0
                lp_status = f"정상 수신 (계량기 {len(d_list)}건, 합계 {sample_usage} kWh)"
                record_kepco_result(True)
            else:
                lp_status = f"한전 응답 오류: {data}"
        else:
            lp_status = f"중계 응답 코드 {r.status_code}"
    except Exception as e:
        lp_status = f"호출 에러: {str(e)}"

    valid_cnt = sum(1 for v in GLOBAL_KEPCO_CACHE.values() if v != "ZERO")
    zero_cnt = sum(1 for v in GLOBAL_KEPCO_CACHE.values() if v == "ZERO")
    return {
        "check_date": target_date,
        "cust_no": cust_no,
        "proxy_url": KEPCO_PROXY_URL,
        "kepco_lp_status": lp_status,
        "sample_usage_kwh": sample_usage,
        "sample_peak_kw": sample_peak,
        "response_preview": preview,
        "cache_summary": {
            "total_cached_keys": len(GLOBAL_KEPCO_CACHE),
            "valid_data_keys": valid_cnt,
            "confirmed_zero_keys": zero_cnt,
            "grace_days": KEPCO_ZERO_GRACE_DAYS,
            "kepco_cooldown_remaining_sec": max(0, int(KEPCO_COOLDOWN_UNTIL - time.time()))
        }
    }

def load_excel_dataset():
    file_path = "uploaded_dataset.xlsx"
    if not os.path.exists(file_path): return None
    
    mtime = os.path.getmtime(file_path)
    if GLOBAL_EXCEL_CACHE["mtime"] == mtime and GLOBAL_EXCEL_CACHE["df"] is not None:
        return GLOBAL_EXCEL_CACHE["df"].copy()
        
    try:
        xls = pd.ExcelFile(file_path)
        df_main = pd.read_excel(xls, sheet_name=0)
        df_main = df_main.loc[:, ~df_main.columns.astype(str).str.startswith('Unnamed')].copy()
        date_col = next((c for c in df_main.columns if 'date' in str(c).lower() or '일자' in str(c)), 'date')
        df_main['date'] = pd.to_datetime(df_main[date_col]).dt.normalize()
        
        all_pm25 = []
        for sheet in xls.sheet_names:
            # 🌟 통합된 '초미세먼지' 시트든 기존 연도별 시트든 자동 정규화하여 비교·예측용 평균치 산출
            if '초미세먼지' in sheet or '미세먼지' in sheet:
                raw_pm = pd.read_excel(xls, sheet_name=sheet)
                df_pm, _, stn_cols = normalize_pm25_df(raw_pm)
                if not df_pm.empty and stn_cols:
                    df_pm['date'] = df_pm['일자']
                    df_pm['pm25_merged'] = df_pm[stn_cols].mean(axis=1)
                    all_pm25.append(df_pm[['date', 'pm25_merged']])
            
            elif '기상청_ASOS' in sheet or 'ASOS' in sheet:
                try:
                    df_asos = pd.read_excel(xls, sheet_name=sheet)
                    for _, r_row in df_asos.iterrows():
                        d_val = r_row.get('date') or r_row.get('일자')
                        if pd.notna(d_val):
                            d_str = pd.to_datetime(d_val).strftime('%Y-%m-%d')
                            if pd.notna(r_row.get('tmax')) and is_valid_temp(r_row.get('tmax')):
                                GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmax"] = float(r_row['tmax'])
                            if pd.notna(r_row.get('tmin')) and is_valid_temp(r_row.get('tmin')):
                                GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmin"] = float(r_row['tmin'])
                            if pd.notna(r_row.get('tavg')) and is_valid_temp(r_row.get('tavg')):
                                GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tavg"] = float(r_row['tavg'])
                            if pd.notna(r_row.get('humi')) and is_valid_humi(r_row.get('humi')):
                                GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_humi"] = float(r_row['humi'])
                            else:
                                GLOBAL_WEATHER_CACHE.pop(f"ASOS_{d_str}_humi", None)
                    logger.info("✅ 엑셀 '기상청_ASOS' 시트에서 과거 기상 데이터 캐시 로드 완료")
                except Exception as e:
                    logger.warning(f"기상청_ASOS 시트 로드 중 오류: {e}")
        
        if all_pm25:
            pm25_df = pd.concat(all_pm25).dropna(subset=['date']).groupby('date')['pm25_merged'].mean().reset_index()
            df_main = df_main.merge(pm25_df, on='date', how='left')
            df_main['pm25_val'] = df_main['pm25_merged']
            
        GLOBAL_EXCEL_CACHE["df"] = df_main
        GLOBAL_EXCEL_CACHE["mtime"] = mtime
        return df_main.copy()
    except Exception: 
        return None

# 🌟 [환경공단 1] 금일 대구 26개 측정소 실시간 초미세먼지 단 1회 호출로 일괄 조회 (10분 캐싱)
def fetch_airkorea_sido_realtime(is_bypass: bool = False):
    now_ts = time.time()
    if not is_bypass and GLOBAL_AIRKOREA_REALTIME_CACHE["data"] and (now_ts - GLOBAL_AIRKOREA_REALTIME_CACHE["time"] < 600):
        return GLOBAL_AIRKOREA_REALTIME_CACHE["data"]

    url = f"{KMA_PROXY_URL}?type=air_sido&sidoName=대구&numOfRows=100&pageNo=1&serviceKey={DATA_GO_KR_API_KEY}"
    rt_map = {}
    try:
        r = http_session.get(url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            items = data.get("response", {}).get("body", {}).get("items", [])
            valid_vals = []
            for item in items:
                st_name = str(item.get("stationName", "")).strip()
                val_raw = str(item.get("pm25Value", "")).strip()
                if st_name and val_raw and val_raw not in ["-", "--", "None", "null"]:
                    try:
                        iv = int(round(float(val_raw)))
                        if iv >= 0:
                            rt_map[st_name] = iv
                            valid_vals.append(iv)
                    except:
                        pass
            if valid_vals:
                rt_map["AVG"] = int(round(sum(valid_vals) / len(valid_vals)))
            if rt_map:
                GLOBAL_AIRKOREA_REALTIME_CACHE["time"] = now_ts
                GLOBAL_AIRKOREA_REALTIME_CACHE["data"] = rt_map
    except Exception as e:
        logger.warning(f"에어코리아 대구 실시간 조회 오류: {e}")

    return GLOBAL_AIRKOREA_REALTIME_CACHE["data"]

# 🌟 [환경공단 2] 측정소별 공식 일평균 초미세먼지 조회 (3개월 지난 데이터 보호 + 최근 3일 '--' 자동 재수집)
def fetch_airkorea_station_daily(air_stn: str, start_date: str, end_date: str, is_bypass: bool = False, save_immediately: bool = True):
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    today_dt = get_kst_now().replace(tzinfo=None)
    today_str = today_dt.strftime("%Y-%m-%d")
    retention_cutoff_str = (today_dt - timedelta(days=85)).strftime("%Y-%m-%d")
    recent_3d_str = (today_dt - timedelta(days=3)).strftime("%Y-%m-%d")
    diff = (e_dt - s_dt).days + 1

    res = {}
    missing_past_dates = []

    for i in range(diff):
        curr_dt = s_dt + timedelta(days=i)
        d_str = curr_dt.strftime("%Y-%m-%d")
        if d_str >= today_str:
            rt_data = fetch_airkorea_sido_realtime(is_bypass=is_bypass)
            v = rt_data.get(air_stn) or rt_data.get("수창동") or rt_data.get("AVG", "--")
            res[d_str] = v if v is not None else "--"
        else:
            ck = f"AIR_{air_stn}_{d_str}"
            cached_v = GLOBAL_AIRKOREA_CACHE.get(ck)
            if ck in GLOBAL_AIRKOREA_CACHE and (is_valid_value(cached_v) or d_str < recent_3d_str):
                res[d_str] = cached_v
                if is_bypass and d_str >= retention_cutoff_str:
                    missing_past_dates.append(d_str)
            else:
                if d_str >= retention_cutoff_str:
                    missing_past_dates.append(d_str)
                else:
                    res[d_str] = "--"

    if not missing_past_dates:
        return res

    req_start = min(missing_past_dates).replace("-", "")
    req_end = max(missing_past_dates).replace("-", "")
    num_rows = max((datetime.strptime(max(missing_past_dates), "%Y-%m-%d") - datetime.strptime(min(missing_past_dates), "%Y-%m-%d")).days + 10, 50)

    url = f"{KMA_PROXY_URL}?type=air_daily&stationName={air_stn}&inqBginDt={req_start}&inqEndDt={req_end}&numOfRows={num_rows}&serviceKey={DATA_GO_KR_API_KEY}"
    fetched_dates = {}
    api_success = False

    try:
        r = http_session.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            header_code = data.get("response", {}).get("header", {}).get("resultCode", "")
            items = data.get("response", {}).get("body", {}).get("items", [])
            if header_code == "00" or isinstance(items, list):
                api_success = True
                for item in (items or []):
                    d_key = str(item.get("msurDt", "")).strip()[:10]
                    val_raw = str(item.get("pm25Value", "")).strip()
                    if len(d_key) == 10 and val_raw and val_raw not in ["-", "--", "None", "null"]:
                        try:
                            iv = int(round(float(val_raw)))
                            if iv >= 0:
                                fetched_dates[d_key] = iv
                        except:
                            pass
    except Exception as e:
        logger.warning(f"에어코리아 일평균 [{air_stn}] 조회 오류: {e}")

    if api_success:
        newly_updated = {}
        with AIR_FETCH_LOCK:
            for d_str in missing_past_dates:
                ck = f"AIR_{air_stn}_{d_str}"
                old_val = GLOBAL_AIRKOREA_CACHE.get(ck)
                new_val = fetched_dates.get(d_str)

                if new_val is not None:
                    res[d_str] = new_val
                    if d_str < today_str and old_val != new_val:
                        GLOBAL_AIRKOREA_CACHE[ck] = new_val
                        newly_updated[ck] = new_val
                        PENDING_AIR_UPDATES[ck] = new_val
                else:
                    if is_valid_value(old_val):
                        res[d_str] = old_val
                    else:
                        res[d_str] = "--"
                        # 🌟 최근 3일 이내 날짜는 자정 직후 미집계일 수 있으므로 "--"를 영구 캐시에 저장하지 않음
                        if d_str < recent_3d_str and ck not in GLOBAL_AIRKOREA_CACHE:
                            GLOBAL_AIRKOREA_CACHE[ck] = "--"
                            newly_updated[ck] = "--"
                            PENDING_AIR_UPDATES[ck] = "--"

            if save_immediately and newly_updated:
                save_airkorea_cache_to_firebase(newly_updated)
                for k in newly_updated:
                    PENDING_AIR_UPDATES.pop(k, None)
    else:
        for d_str in missing_past_dates:
            res[d_str] = GLOBAL_AIRKOREA_CACHE.get(f"AIR_{air_stn}_{d_str}", "--")

    return res

# 🌟 기상청 대표 관측소 (ASOS) - Vercel 서울(icn1) 중계 서버 경유 (습도 -9.0 결측 코드 필터링 적용)
def fetch_asos_daily(start_date: str, end_date: str, is_bypass: bool = False):
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    now_kst = get_kst_now().replace(tzinfo=None)
    yesterday = now_kst - timedelta(days=1)
    if e_dt > yesterday: e_dt = yesterday
    if s_dt > e_dt: return {}

    diff = (e_dt - s_dt).days + 1
    today_str = now_kst.strftime("%Y-%m-%d")
    
    missing_dates = []
    cached_res = {}
    recent_14d_str = (now_kst - timedelta(days=14)).strftime("%Y-%m-%d")

    for i in range(diff):
        d_str = (s_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        tmax_v = GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmax", "--")
        tmin_v = GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmin", "--")
        tavg_v = GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tavg", "--")
        humi_v = GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_humi", "--")
        
        # 🌟 최근 14일 이내는 기온·습도 4개 중 하나라도 '--'면 재조회하고,
        # 14일 이전 과거(예: 2023-03-26 습도 영구결측)는 기온이 정상 존재하면 반복 재호출하지 않고 '--' 유지
        if d_str >= recent_14d_str:
            is_fully_valid = is_valid_temp(tmax_v) and is_valid_temp(tmin_v) and is_valid_temp(tavg_v) and is_valid_humi(humi_v)
        else:
            is_fully_valid = is_valid_temp(tmax_v) and is_valid_temp(tmin_v)

        if not is_bypass and is_fully_valid:
            cached_res[d_str] = {
                "tmax": tmax_v,
                "tmin": tmin_v,
                "tavg": tavg_v,
                "humi": humi_v if is_valid_humi(humi_v) else "--"
            }
        else:
            missing_dates.append(d_str)
            
    if not missing_dates: 
        return cached_res

    # 🌟 누락일이 여러 연도에 떨어져 있을 때(예: 2023-03-26, 2024-02-21, 2026-11-16) 30일 단위 구간으로 나누어 호출
    sorted_dts = sorted(datetime.strptime(d, "%Y-%m-%d") for d in missing_dates)
    chunks = []
    c_start = sorted_dts[0]
    c_end = sorted_dts[0]
    for dt in sorted_dts[1:]:
        if (dt - c_start).days <= 30:
            c_end = dt
        else:
            chunks.append((c_start.strftime("%Y%m%d"), c_end.strftime("%Y%m%d")))
            c_start = dt
            c_end = dt
    chunks.append((c_start.strftime("%Y%m%d"), c_end.strftime("%Y%m%d")))

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    res = {}

    for req_start, req_end in chunks:
        url = f"{KMA_PROXY_URL}?type=asos&tm1={req_start}&tm2={req_end}&stn=143&authKey={KMA_API_HUB_KEY}"
        for _ in range(2):
            try:
                r = http_session.get(url, headers=headers, timeout=15)
                r.encoding = 'utf-8'
                lines = r.text.split('\n')
                headers_list = []
                chunk_parsed = False
                for line in lines:
                    if line.startswith('#') and ':' in line and '.' in line:
                        left_side = line.split(':')[0].strip()
                        cleaned = left_side.replace('#', '').strip()
                        if cleaned and cleaned[0].isdigit():
                            col_name = left_side.split('.')[1].strip().split()[0]
                            if col_name not in headers_list: headers_list.append(col_name)
                    elif line.strip() and not line.startswith('#'):
                        parts = line.split()
                        if len(parts) >= 15:
                            def parse_temp(v):
                                return float(v) if is_valid_temp(v) else "--"
                            def parse_humi(v):
                                return float(v) if is_valid_humi(v) else "--"

                            if headers_list and len(parts) >= len(headers_list) - 5:
                                row_dict = dict(zip(headers_list, parts))
                                date_key = row_dict.get('TM')
                                if date_key and len(date_key) >= 8:
                                    d_str = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                                    res[d_str] = {
                                        "tmax": parse_temp(row_dict.get('TA_MAX') or row_dict.get('TX')),
                                        "tmin": parse_temp(row_dict.get('TA_MIN') or row_dict.get('TN')),
                                        "tavg": parse_temp(row_dict.get('TA_AVG') or row_dict.get('TA')),
                                        "humi": parse_humi(row_dict.get('HM_AVG') or row_dict.get('HM'))
                                    }
                                    chunk_parsed = True
                            elif len(parts[0]) == 8 and parts[0].isdigit():
                                date_key = parts[0]
                                d_str = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                                res[d_str] = {
                                    "tavg": parse_temp(parts[10]),
                                    "tmax": parse_temp(parts[11]),
                                    "tmin": parse_temp(parts[13]),
                                    "humi": parse_humi(parts[18]) if len(parts) > 18 else "--"
                                }
                                chunk_parsed = True
                if chunk_parsed:
                    break
            except Exception:
                time.sleep(0.5)

    for d_str in missing_dates:
        v = res.get(d_str, {"tmax": "--", "tmin": "--", "tavg": "--", "humi": "--"})
        if d_str < today_str:
            if is_valid_temp(v["tmax"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmax"] = v["tmax"]
            if is_valid_temp(v["tmin"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmin"] = v["tmin"]
            if is_valid_temp(v["tavg"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tavg"] = v["tavg"]
            if is_valid_humi(v["humi"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_humi"] = v["humi"]
        cached_res[d_str] = {
            "tmax": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmax", v["tmax"]),
            "tmin": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmin", v["tmin"]),
            "tavg": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tavg", v["tavg"]),
            "humi": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_humi", v["humi"] if is_valid_humi(v["humi"]) else "--")
        }
            
    return cached_res

    req_start = min(missing_dates).replace("-", "")
    req_end = max(missing_dates).replace("-", "")
    url = f"{KMA_PROXY_URL}?type=asos&tm1={req_start}&tm2={req_end}&stn=143&authKey={KMA_API_HUB_KEY}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    
    res = {}
    for _ in range(2):
        try:
            r = http_session.get(url, headers=headers, timeout=15)
            r.encoding = 'utf-8'
            lines = r.text.split('\n')
            headers_list = []
            for line in lines:
                if line.startswith('#') and ':' in line and '.' in line:
                    left_side = line.split(':')[0].strip()
                    cleaned = left_side.replace('#', '').strip()
                    if cleaned and cleaned[0].isdigit():
                        col_name = left_side.split('.')[1].strip().split()[0]
                        if col_name not in headers_list: headers_list.append(col_name)
                elif line.strip() and not line.startswith('#'):
                    parts = line.split()
                    if len(parts) >= 15:
                        def parse_temp(v):
                            return float(v) if is_valid_temp(v) else "--"
                        def parse_humi(v):
                            return float(v) if is_valid_humi(v) else "--"

                        if headers_list and len(parts) >= len(headers_list) - 5:
                            row_dict = dict(zip(headers_list, parts))
                            date_key = row_dict.get('TM')
                            if date_key and len(date_key) >= 8:
                                d_str = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                                res[d_str] = {
                                    "tmax": parse_temp(row_dict.get('TA_MAX') or row_dict.get('TX')),
                                    "tmin": parse_temp(row_dict.get('TA_MIN') or row_dict.get('TN')),
                                    "tavg": parse_temp(row_dict.get('TA_AVG') or row_dict.get('TA')),
                                    "humi": parse_humi(row_dict.get('HM_AVG') or row_dict.get('HM'))
                                }
                        elif len(parts[0]) == 8 and parts[0].isdigit():
                            date_key = parts[0]
                            d_str = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                            res[d_str] = {
                                "tavg": parse_temp(parts[10]),
                                "tmax": parse_temp(parts[11]),
                                "tmin": parse_temp(parts[13]),
                                "humi": parse_humi(parts[18]) if len(parts) > 18 else "--"
                            }
            if res: break
        except: time.sleep(0.5)

    for d_str in missing_dates:
        v = res.get(d_str, {"tmax": "--", "tmin": "--", "tavg": "--", "humi": "--"})
        if d_str < today_str:
            if is_valid_temp(v["tmax"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmax"] = v["tmax"]
            if is_valid_temp(v["tmin"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tmin"] = v["tmin"]
            if is_valid_temp(v["tavg"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_tavg"] = v["tavg"]
            if is_valid_humi(v["humi"]): GLOBAL_WEATHER_CACHE[f"ASOS_{d_str}_humi"] = v["humi"]
        cached_res[d_str] = {
            "tmax": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmax", v["tmax"]),
            "tmin": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmin", v["tmin"]),
            "tavg": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tavg", v["tavg"]),
            "humi": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_humi", v["humi"] if is_valid_humi(v["humi"]) else "--")
        }
            
    return cached_res

# 🌟 Open-Meteo 환경 데이터 (오직 금일 기온·습도 표시 전용으로만 작동, 과거 날짜는 조회하지 않음)
def fetch_openmeteo_env(lat, lon, start_date, end_date, is_bypass: bool = False):
    today_str = get_kst_now().strftime("%Y-%m-%d")
    # 🌟 조회 종료일이 오늘보다 이전(과거 기간 조회)이면 Open-Meteo를 아예 호출하지 않음
    if end_date < today_str:
        return {}

    env_data = {}
    url_a = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params_a = {"latitude": lat, "longitude": lon, "start_date": today_str, "end_date": today_str, "hourly": "pm2_5", "timezone": "Asia/Seoul"}
    try:
        r_a = http_session.get(url_a, params=params_a, timeout=4)
        if r_a.status_code == 200:
            data = r_a.json()
            h_times = data.get("hourly", {}).get("time", [])
            pm25s = data.get("hourly", {}).get("pm2_5", [])
            pm_dict = {}
            for i, ht in enumerate(h_times):
                d_str = ht[:10]
                if pm25s[i] is not None: pm_dict.setdefault(d_str, []).append(pm25s[i])
            for d_str, p_list in pm_dict.items():
                env_data.setdefault(d_str, {})["pm25"] = round(sum(p_list)/len(p_list), 1)
    except: pass

    url_w = "https://api.open-meteo.com/v1/forecast"
    params_w = {"latitude": lat, "longitude": lon, "start_date": today_str, "end_date": today_str, "daily": "temperature_2m_max,temperature_2m_min", "hourly": "relative_humidity_2m", "timezone": "Asia/Seoul"}
    try:
        r_w = http_session.get(url_w, params=params_w, timeout=4)
        if r_w.status_code == 200:
            data = r_w.json()
            d_times = data.get("daily", {}).get("time", [])
            tmaxs = data.get("daily", {}).get("temperature_2m_max", [])
            tmins = data.get("daily", {}).get("temperature_2m_min", [])
            h_times = data.get("hourly", {}).get("time", [])
            humis = data.get("hourly", {}).get("relative_humidity_2m", [])
            
            humi_dict = {}
            for i, ht in enumerate(h_times):
                d_str = ht[:10]
                if humis[i] is not None: humi_dict.setdefault(d_str, []).append(humis[i])
                    
            for i, t in enumerate(d_times):
                env_data.setdefault(t, {})
                if tmaxs and i < len(tmaxs) and tmaxs[i] is not None: env_data[t]["tmax"] = round(tmaxs[i], 1)
                if tmins and i < len(tmins) and tmins[i] is not None: env_data[t]["tmin"] = round(tmins[i], 1)
                if t in humi_dict: env_data[t]["humi"] = round(sum(humi_dict[t])/len(humi_dict[t]), 1)
    except: pass

    v = env_data.get(today_str, {})
    return {
        today_str: {
            "pm25": v.get("pm25", "--"),
            "tmax": v.get("tmax", "--"),
            "tmin": v.get("tmin", "--"),
            "humi": v.get("humi", "--")
        }
    }

# 🌟 기상청 방재 기상 관측장비 (AWS) - 단일 스레드 0.7초 순차 호출 + 10회마다 Firebase 중간 저장 (습도는 ASOS 전담)
def fetch_aws_daily_for_dashboard(stn_id: str, start_date: str, end_date: str, is_bypass: bool = False, is_background: bool = False):
    global KMA_AWS_COOLDOWN_UNTIL
    if is_bypass:
        KMA_AWS_COOLDOWN_UNTIL = 0.0

    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    now_kst = get_kst_now().replace(tzinfo=None)
    yesterday = now_kst - timedelta(days=1)
    if e_dt > yesterday: e_dt = yesterday
    if s_dt > e_dt: return {} 

    diff = (e_dt - s_dt).days + 1
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    today_str = now_kst.strftime("%Y-%m-%d")
    recent_3d_str = (now_kst - timedelta(days=3)).strftime("%Y-%m-%d")

    if (is_background or is_bypass) and time.time() >= KMA_AWS_COOLDOWN_UNTIL:
        with AWS_FETCH_LOCK:
            missing_tasks = []
            # 🌟 백그라운드 점검 시에는 143번 하나만 보지 않고 대구 8개 동네 관측소 전체의 누락 여부를 검사
            check_stns = list(TARGET_AWS_STNS) if is_background else [stn_id]

            for i in range(diff):
                curr = s_dt + timedelta(days=i)
                d_str = curr.strftime("%Y-%m-%d")
                tm2 = curr.strftime("%Y%m%d")

                # 최고기온 < 최저기온 역전 오류가 있는 관측소가 있는지 확인
                has_inversion = False
                for s_chk in check_stns:
                    tx_v = GLOBAL_WEATHER_CACHE.get(f"AWS_{s_chk}_{d_str}_tmax")
                    tn_v = GLOBAL_WEATHER_CACHE.get(f"AWS_{s_chk}_{d_str}_tmin")
                    if is_valid_temp(tx_v) and is_valid_temp(tn_v) and float(tx_v) < float(tn_v):
                        has_inversion = True
                        break

                for obs, key in [("ta_max", "tmax"), ("ta_min", "tmin")]:
                    need_call = is_bypass or has_inversion
                    if not need_call:
                        for s_chk in check_stns:
                            ck_chk = f"AWS_{s_chk}_{d_str}_{key}"
                            cv = GLOBAL_WEATHER_CACHE.get(ck_chk)
                            if ck_chk not in GLOBAL_WEATHER_CACHE or (d_str >= recent_3d_str and not is_valid_temp(cv)):
                                need_call = True
                                break
                    if need_call:
                        missing_tasks.append((d_str, tm2, obs, key))

            if missing_tasks and time.time() >= KMA_AWS_COOLDOWN_UNTIL:
                newly_aws_updated = {}
                consecutive_failures = 0
                quota_failed = False

                for idx, (d_str, tm2, obs, key) in enumerate(missing_tasks):
                    if quota_failed:
                        break
                    if idx > 0:
                        time.sleep(0.7)

                    url = f"{KMA_PROXY_URL}?type=aws&tm2={tm2}&obs={obs}&stn=0&authKey={KMA_API_HUB_KEY}"
                    success_on_try = False
                    last_err_preview = ""

                    for attempt in range(2):
                        try:
                            r = http_session.get(url, headers=headers, timeout=8.0)
                            r.encoding = 'utf-8'
                            last_err_preview = r.text[:120].replace('\n', ' ')
                            lines = r.text.split('\n')
                            valid_line_count = 0
                            found_vals = {}
                            for line in lines:
                                s_line = line.strip()
                                if s_line and not s_line.startswith('#'):
                                    parts = s_line.split()
                                    if len(parts) >= 6 and parts[0].isdigit() and len(parts[0]) == 8:
                                        valid_line_count += 1
                                        s_id = parts[1]
                                        if s_id in TARGET_AWS_STNS:
                                            try:
                                                fv = float(parts[5])
                                                if is_valid_temp(fv):
                                                    found_vals[s_id] = fv
                                            except:
                                                pass
                            if valid_line_count >= 10:
                                success_on_try = True
                                consecutive_failures = 0
                                if d_str < today_str:
                                    for s_id in TARGET_AWS_STNS:
                                        ck = f"AWS_{s_id}_{d_str}_{key}"
                                        old_val = GLOBAL_WEATHER_CACHE.get(ck)
                                        new_val = found_vals.get(s_id)
                                        # 🌟 새로 받은 기온이 기존 캐시 값과 다르면 최신 확정값으로 즉시 덮어쓰기
                                        if new_val is not None:
                                            if old_val != new_val:
                                                GLOBAL_WEATHER_CACHE[ck] = new_val
                                                newly_aws_updated[ck] = new_val
                                        elif not is_valid_temp(old_val) and d_str < recent_3d_str and ck not in GLOBAL_WEATHER_CACHE:
                                            GLOBAL_WEATHER_CACHE[ck] = "--"
                                            newly_aws_updated[ck] = "--"
                                break
                        except Exception as e:
                            last_err_preview = str(e)
                        if attempt == 0:
                            time.sleep(1.2)

                    if len(newly_aws_updated) >= 80:
                        save_aws_cache_to_firebase(dict(newly_aws_updated))
                        newly_aws_updated.clear()

                    if not success_on_try:
                        consecutive_failures += 1
                        logger.warning(f"⚠️ 기상청 AWS 응답 지연 ({d_str} {obs}, 연속 {consecutive_failures}회): {last_err_preview}")
                        if consecutive_failures >= 3:
                            quota_failed = True

                if newly_aws_updated:
                    save_aws_cache_to_firebase(dict(newly_aws_updated))

                if quota_failed:
                    KMA_AWS_COOLDOWN_UNTIL = time.time() + 600
                    logger.warning("⚠️ 기상청 AWS 연속 3회 응답 없음 - 10분간 추가 AWS 호출을 일시 중단하고 ASOS로 대체합니다.")

    res = {}
    for i in range(diff):
        curr = s_dt + timedelta(days=i)
        d_str = curr.strftime("%Y-%m-%d")
        res[d_str] = {
            "tmax": GLOBAL_WEATHER_CACHE.get(f"AWS_{stn_id}_{d_str}_tmax", "--"),
            "tmin": GLOBAL_WEATHER_CACHE.get(f"AWS_{stn_id}_{d_str}_tmin", "--"),
            "humi": "--"
        }
    return res

def process_kepco_day_data(day_list, target_meter_no):
    interval_usage = [0.0] * 96
    if not day_list: return interval_usage
    for item in day_list:
        meter_no = item.get("meterNo", "")
        for k, v in item.items():
            if k.startswith("pwr_qty") and k != "pwr_qty":
                try:
                    val = float(v)
                    if target_meter_no in ["", "전체"] or meter_no == target_meter_no:
                        time_str = k[-4:]
                        hh = int(time_str[:2])
                        mm = int(time_str[2:])
                        idx = 95 if (hh == 24 and mm == 0) else hh * 4 + (mm // 15) - 1
                        if 0 <= idx < 96: interval_usage[idx] += val
                except: pass
    return interval_usage

def get_kepco_data_for_station(station: str, date_str: str, is_bypass: bool = False, save_immediately: bool = True):
    cust_nos = []
    target_meter_no = "전체"
    
    if station == '전체': 
        cust_nos = list(set([v for k, v in STATION_CUST_MAP.items() if k != '종합청사']))
    elif station in LINE_STATIONS: 
        cust_nos = list(set([STATION_CUST_MAP[s] for s in LINE_STATIONS[station]]))
    elif station == '종합청사': 
        cust_nos = [STATION_CUST_MAP['종합청사']]
    else:
        cust_nos = [STATION_CUST_MAP.get(station)]
        if station in LINE_STATIONS['1호선']: target_meter_no = STATION_METER_MAP.get(station, "")
            
    total_interval_usage = [0.0] * 96
    now_kst = get_kst_now().replace(tzinfo=None)
    today_str = now_kst.strftime("%Y-%m-%d")
    grace_cutoff_str = (now_kst - timedelta(days=KEPCO_ZERO_GRACE_DAYS)).strftime("%Y-%m-%d")
    
    def fetch_and_process(c_no):
        if not c_no: return [0.0] * 96
        cache_key = f"{c_no}_{date_str}"
        m_target = target_meter_no if c_no == '0526314773' else "전체"
        cached_entry = None
        
        if not is_bypass:
            if date_str < today_str and cache_key in GLOBAL_KEPCO_CACHE:
                entry = GLOBAL_KEPCO_CACHE[cache_key]
                if entry == "ZERO":
                    if date_str < grace_cutoff_str:
                        return [0.0] * 96
                elif is_valid_kepco_cache_entry(entry):
                    intervals = extract_intervals_from_kepco_entry(entry, m_target)
                    if sum(intervals) > 0 or date_str < grace_cutoff_str:
                        return intervals
                    cached_entry = entry
            elif date_str == today_str and cache_key in GLOBAL_KEPCO_TODAY_CACHE:
                c_time, c_data = GLOBAL_KEPCO_TODAY_CACHE[cache_key]
                if time.time() - c_time < 600:
                    return extract_intervals_from_kepco_entry(c_data, m_target)

        # 🌟 한전 5분 쿨다운(서킷 브레이커) 발동 중이면 네트워크 호출 즉시 생략하고 기존 캐시 반환
        if time.time() < KEPCO_COOLDOWN_UNTIL:
            fallback_entry = cached_entry if cached_entry is not None else GLOBAL_KEPCO_CACHE.get(cache_key)
            if fallback_entry and fallback_entry != "ZERO":
                return extract_intervals_from_kepco_entry(fallback_entry, m_target)
            return [0.0] * 96

        # 🌟 Vercel 서울(icn1) 한전 전용 중계 서버 경유 호출
        params = {
            "type": "lp",
            "custNo": c_no,
            "date": date_str.replace("-", ""),
            "serviceKey": KEPCO_API_KEY
        }
        day_list = []
        req_ok = False
        for attempt in range(2):
            if time.time() < KEPCO_COOLDOWN_UNTIL:
                break
            try:
                res = http_session.get(KEPCO_PROXY_URL, params=params, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict) and not data.get("errCd") and not data.get("error"):
                        req_ok = True
                        if isinstance(data.get("dayLpDataInfoList"), list):
                            day_list = data["dayLpDataInfoList"]
                            if day_list:
                                break
                        if attempt >= 1:
                            break
            except Exception:
                pass
            if attempt == 0 and time.time() >= KEPCO_COOLDOWN_UNTIL:
                time.sleep(0.4)

        record_kepco_result(req_ok)

        if not req_ok:
            fallback_entry = cached_entry if cached_entry is not None else GLOBAL_KEPCO_CACHE.get(cache_key)
            if fallback_entry and fallback_entry != "ZERO":
                return extract_intervals_from_kepco_entry(fallback_entry, m_target)
            return [0.0] * 96

        compressed = compress_kepco_day_list(day_list)
        total_cust_sum = get_kepco_entry_total(compressed)
        parsed_intervals = extract_intervals_from_kepco_entry(compressed, m_target)

        if date_str < today_str:
            with KEPCO_FETCH_LOCK:
                old_entry = GLOBAL_KEPCO_CACHE.get(cache_key)
                old_sum = get_kepco_entry_total(old_entry)

                if total_cust_sum > 0:
                    if old_entry is None or old_entry == "ZERO" or total_cust_sum > old_sum or (is_bypass and compressed != old_entry):
                        GLOBAL_KEPCO_CACHE[cache_key] = compressed
                        PENDING_KEPCO_UPDATES[cache_key] = compressed
                else:
                    if date_str < grace_cutoff_str and old_sum == 0:
                        if old_entry != "ZERO":
                            GLOBAL_KEPCO_CACHE[cache_key] = "ZERO"
                            PENDING_KEPCO_UPDATES[cache_key] = "ZERO"
                    elif old_sum > 0:
                        return extract_intervals_from_kepco_entry(old_entry, m_target)
        elif date_str == today_str:
            GLOBAL_KEPCO_TODAY_CACHE[cache_key] = (time.time(), compressed if total_cust_sum > 0 else {})

        return parsed_intervals

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(fetch_and_process, cust_nos))

    if save_immediately:
        flush_pending_kepco_updates(min_batch=1)
    else:
        flush_pending_kepco_updates(min_batch=25)
        
    for int_u in results:
        for i, val in enumerate(int_u): 
            total_interval_usage[i] += val
            
    total_usage = sum(total_interval_usage)
    max_peak = max(total_interval_usage) * 4 if total_interval_usage else 0.0
    
    if date_str == today_str:
        check_and_send_alert(station, max_peak)
    
    details = []
    for m in range(96):
        hh = m // 4
        mm = (m % 4) * 15 + 15
        if mm == 60: hh += 1; mm = 0
        details.append({ "time": f"{hh:02d}:{mm:02d}", "usage_kwh": round(total_interval_usage[m], 1), "peak_kw": round(total_interval_usage[m] * 4, 1) })
        
    return total_usage, max_peak, details

@app.get("/api/dashboard/{station}")
def get_dashboard_data(station: str, start: str, end: str, bypass: str = "false"):
    is_bypass = bypass.lower() == "true"
    cache_key = f"{station}_{start}_{end}"
    today_str = get_kst_now().strftime("%Y-%m-%d")
    master_key = f"{station}_2026-09-05_{today_str}"
    aws_stn = STATION_AWS_MAP.get(station, '143')
    air_stn = STATION_AIR_MAP.get(station, '수창동')
    
    if is_bypass:
        GLOBAL_API_CACHE["dashboard"].pop(master_key, None)
        reset_kepco_cooldown()

    if not is_bypass and cache_key in GLOBAL_API_CACHE["dashboard"]:
        c_time, c_res = GLOBAL_API_CACHE["dashboard"][cache_key]
        if end < today_str or (time.time() - c_time < 600):
            res_copy = dict(c_res)
            res_copy["is_cached"] = True
            return res_copy

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    api_start_date = datetime.strptime("2026-09-05", "%Y-%m-%d")

    if not is_bypass and master_key in GLOBAL_API_CACHE["dashboard"] and end <= today_str:
        m_time, m_res = GLOBAL_API_CACHE["dashboard"][master_key]
        if time.time() - m_time < 600:
            eff_start = max(start_dt, api_start_date).strftime("%Y-%m-%d")
            eff_end = end_dt.strftime("%Y-%m-%d")
            sliced_records = [r for r in m_res.get("daily_records", []) if eff_start <= r["date"] <= eff_end]
            tot_u = sum(r["usage_kwh"] for r in sliced_records)
            max_p = max((r["peak_kw"] for r in sliced_records), default=0.0)
            tot_c = sum(r["co2"] for r in sliced_records)
            sliced_res = {
                "station_name": station,
                "mapped_location": m_res.get("mapped_location", f"{station} (기상청 AWS {aws_stn}번 · 환경공단 [{air_stn}] 측정소 연동)"),
                "summary": { "total_usage": round(tot_u), "max_peak": round(max_p, 1), "total_co2": round(tot_c, 1) },
                "daily_records": sliced_records,
                "cached_at": m_res.get("cached_at", get_kst_now().strftime("%Y-%m-%d %H:%M:%S")),
                "is_cached": True
            }
            set_bounded_api_cache("dashboard", cache_key, (m_time, sliced_res))
            return sliced_res
    
    if end_dt < api_start_date:
        res = {
            "station_name": station, 
            "mapped_location": f"{station} (기상청 AWS {aws_stn}번 · 환경공단 [{air_stn}] 측정소 연동)",
            "summary": { "total_usage": 0, "max_peak": 0, "total_co2": 0 },
            "daily_records": [],
            "cached_at": get_kst_now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_cached": False
        }
        set_bounded_api_cache("dashboard", cache_key, (time.time(), res))
        return res
        
    if start_dt < api_start_date:
        start_dt = api_start_date
        
    diff = (end_dt - start_dt).days + 1
    lat, lon = STATION_COORD_MAP.get(station, (35.8714, 128.6014))
    s_str = start_dt.strftime("%Y-%m-%d")
    e_str = end_dt.strftime("%Y-%m-%d")

    with ThreadPoolExecutor(max_workers=4) as weather_exec:
        f_as = weather_exec.submit(fetch_asos_daily, s_str, e_str, is_bypass)
        f_aw = weather_exec.submit(fetch_aws_daily_for_dashboard, aws_stn, s_str, e_str, is_bypass, False)
        f_ak = weather_exec.submit(fetch_airkorea_station_daily, air_stn, s_str, e_str, is_bypass, True)
        f_om = weather_exec.submit(fetch_openmeteo_env, lat, lon, s_str, e_str, is_bypass)
        
    asos_data = f_as.result()
    aws_data = f_aw.result()
    airkorea_data = f_ak.result()
    om_data = f_om.result()
    
    records = []
    tot_usage, max_peak, tot_co2 = 0.0, 0.0, 0.0
    
    safe_empty_details = []
    for m in range(96):
        hh = m // 4; mm = (m % 4) * 15 + 15
        if mm == 60: hh += 1; mm = 0
        safe_empty_details.append({"time": f"{hh:02d}:{mm:02d}", "usage_kwh": 0.0, "peak_kw": 0.0})

    for i in range(diff):
        d_str = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        
        usage, peak, details = get_kepco_data_for_station(station, d_str, is_bypass, save_immediately=False)
        
        if not details: details = safe_empty_details
        
        tmax = aws_data.get(d_str, {}).get("tmax", "--")
        tmin = aws_data.get(d_str, {}).get("tmin", "--")
        humi = aws_data.get(d_str, {}).get("humi", "--")
        
        if not is_valid_temp(tmax): tmax = asos_data.get(d_str, {}).get("tmax", "--")
        if not is_valid_temp(tmin): tmin = asos_data.get(d_str, {}).get("tmin", "--")
        if not is_valid_humi(humi): humi = asos_data.get(d_str, {}).get("humi", "--")
        
        # 🌟 오늘 날짜(d_str >= today_str)만 Open-Meteo를 사용하고, 어제 이전 과거 날짜는 미집계 시 무조건 '--' 표시
        if d_str >= today_str:
            if not is_valid_temp(tmax): tmax = om_data.get(d_str, {}).get("tmax", "--")
            if not is_valid_temp(tmin): tmin = om_data.get(d_str, {}).get("tmin", "--")
            if not is_valid_humi(humi): humi = om_data.get(d_str, {}).get("humi", "--")
        else:
            if not is_valid_temp(tmax): tmax = "--"
            if not is_valid_temp(tmin): tmin = "--"
            if not is_valid_humi(humi): humi = "--"
            
        # 🌟 초미세먼지: 과거 날짜는 환경공단(해당 측정소 -> 수창동) 공식 통계만 사용하고 미집계 시 '--' 표시
        pm25 = airkorea_data.get(d_str, "--")
        if not is_valid_value(pm25):
            pm25 = GLOBAL_AIRKOREA_CACHE.get(f"AIR_수창동_{d_str}", "--")
        if not is_valid_value(pm25):
            pm25 = om_data.get(d_str, {}).get("pm25", "--") if d_str >= today_str else "--"

        co2 = usage * 0.466 / 1000
        records.append({
            "date": d_str, "usage_kwh": round(usage, 1), "peak_kw": round(peak, 1), "co2": round(co2, 2),
            "temp_max": tmax, "temp_min": tmin, "humidity": humi, "pm25": pm25, "details": details
        })
        tot_usage += usage
        if peak > max_peak: max_peak = peak
        tot_co2 += co2

    flush_pending_kepco_updates(min_batch=1)

    res = {
        "station_name": station, 
        "mapped_location": f"{station} (기상청 AWS {aws_stn}번 · 환경공단 [{air_stn}] 측정소 연동)",
        "summary": { "total_usage": round(tot_usage), "max_peak": round(max_peak, 1), "total_co2": round(tot_co2, 1) },
        "daily_records": records,
        "cached_at": get_kst_now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_cached": False
    }
    
    if tot_usage > 0 or time.time() >= KEPCO_COOLDOWN_UNTIL:
        set_bounded_api_cache("dashboard", cache_key, (time.time(), res))
    return res

@app.get("/api/realtime/{station}")
def get_realtime_data(station: str):
    cache_key = station
    if cache_key in GLOBAL_API_CACHE["realtime"]:
        c_time, c_res = GLOBAL_API_CACHE["realtime"][cache_key]
        if time.time() - c_time < 600:
            return c_res

    today_str = get_kst_now().strftime("%Y-%m-%d")
    usage, peak, details = get_kepco_data_for_station(station, today_str)
    
    if not details or len(details) < 96:
        details = []
        for m in range(96):
            hh = m // 4; mm = (m % 4) * 15 + 15
            if mm == 60: hh += 1; mm = 0
            details.append({"time": f"{hh:02d}:{mm:02d}", "usage_kwh": 0.0, "peak_kw": 0.0})
            
    now_minutes = get_kst_now().hour * 60 + get_kst_now().minute
    res_details = []
    
    for d in details:
        hh, mm = map(int, d["time"].split(":"))
        time_m = hh * 60 + mm if hh != 24 else 24 * 60
        if time_m > now_minutes:
            res_details.append({"time": d["time"], "usage_kwh": None, "peak_kw": None})
        else:
            res_details.append(d.copy())
            
    res = {"station_name": station, "date": today_str, "records": res_details}
    if usage > 0 or time.time() >= KEPCO_COOLDOWN_UNTIL:
        set_bounded_api_cache("realtime", cache_key, (time.time(), res))
    return res

@app.get("/api/compare/{station}")
def get_compare_data(station: str, base_year: str, comp_year: str, price: int = 150, start_month: int = 1, end_month: int = 12):
    if start_month < 1: start_month = 1
    if end_month > 12: end_month = 12
    if start_month > end_month: start_month, end_month = end_month, start_month

    cache_key = f"{station}_{base_year}_{comp_year}_{price}_{start_month}_{end_month}"
    if cache_key in GLOBAL_API_CACHE["compare"]:
        return GLOBAL_API_CACHE["compare"][cache_key]

    try:
        df = load_excel_dataset()
        if df is None: return {"error": "과거 데이터셋(Excel) 파일을 수동으로 먼저 업로드해 주세요."}

        # 🌟 종합청사 여부 판별 (열차 운행·승객수·열차 다이아와 무관한 업무시설 단독 부하 분기용)
        is_hq = (station == '종합청사')

        if station == '전체':
            kwh_cols = [c for c in df.columns if 'total_kwh' in c and '종합청사' not in c]
            df['target_power'] = df[kwh_cols].sum(axis=1) if kwh_cols else pd.Series(0, index=df.index)
        elif station in LINE_STATIONS:
            line_stations = LINE_STATIONS[station]
            kwh_cols = [c for c in df.columns if any(s in c for s in line_stations) and 'total_kwh' in c]
            df['target_power'] = df[kwh_cols].sum(axis=1) if kwh_cols else pd.Series(0, index=df.index)
        else:
            kwh_cols = [c for c in df.columns if station in c and 'total_kwh' in c]
            df['target_power'] = df[kwh_cols[0]] if kwh_cols else pd.Series(0, index=df.index)
            
        pass_col = next((c for c in df.columns if '승객수' in str(c) or '수송인원' in str(c)), None)
        df['passengers'] = pd.to_numeric(df[pass_col], errors='coerce') if (pass_col and not is_hq) else np.nan

        if 'pm25_val' in df.columns:
            df['pm25_val'] = pd.to_numeric(df['pm25_val'], errors='coerce')
        else:
            df['pm25_val'] = np.nan
        # 🌟 초미세먼지 '나쁨(35㎍/㎥ 이상)' 시 공조설비 추가 가동 기준 적용
        df['is_pm25_bad'] = (df['pm25_val'] >= 35.0).astype(int)

        df['target_power'] = pd.to_numeric(df['target_power'], errors='coerce').fillna(0)
        kr_holidays = holidays.KR()
        df['dayofweek'] = df['date'].dt.dayofweek
        df['month'] = df['date'].dt.month
        df['is_offday'] = df.apply(lambda x: 1 if x['date'].dayofweek >= 5 or x['date'] in kr_holidays else 0, axis=1)

        # 기상청 ASOS 일별 데이터 매핑 및 파생 기상지표(냉방도일 CDD, 난방도일 HDD, 불쾌지수 THI, 열대야 축열지수) 생성
        min_y, max_y = min(int(base_year), int(comp_year)), max(int(base_year), int(comp_year))
        asos_data = fetch_asos_daily(f"{min_y}-01-01", f"{max_y}-12-31")

        def extract_asos_float(d_s, key):
            val = asos_data.get(d_s, {}).get(key, "--")
            if key == 'humi':
                return float(val) if is_valid_humi(val) else np.nan
            return float(val) if is_valid_temp(val) else np.nan

        d_strs = df['date'].dt.strftime("%Y-%m-%d")
        df['tmax'] = d_strs.map(lambda x: extract_asos_float(x, 'tmax'))
        df['tmin'] = d_strs.map(lambda x: extract_asos_float(x, 'tmin'))
        df['tavg'] = d_strs.map(lambda x: extract_asos_float(x, 'tavg'))
        df['humi'] = d_strs.map(lambda x: extract_asos_float(x, 'humi'))
        df['tavg'] = df['tavg'].fillna((df['tmax'] + df['tmin']) / 2.0)

        df['cdd'] = (df['tavg'] - 24.0).clip(lower=0).fillna(0)
        df['hdd'] = (12.0 - df['tavg']).clip(lower=0).fillna(0)
        df['thi'] = (0.81 * df['tavg'] + 0.01 * df['humi'].fillna(60.0) * (0.99 * df['tavg'] - 14.3) + 46.3).fillna(65.0)
        df['trop_night_deg'] = (df['tmin'] - 25.0).clip(lower=0).fillna(0)

        df_base = df[(df['date'].dt.year == int(base_year)) & (df['month'] >= start_month) & (df['month'] <= end_month) & (df['target_power'] > 0)].copy()
        df_comp = df[(df['date'].dt.year == int(comp_year)) & (df['month'] >= start_month) & (df['month'] <= end_month) & (df['target_power'] > 0)].copy()
        monthly_base = df_base.groupby('month')['target_power'].sum()
        monthly_comp = df_comp.groupby('month')['target_power'].sum()

        records, tb, tc = [], 0.0, 0.0
        for m in range(start_month, end_month + 1):
            bv = float(monthly_base.get(m, 0.0)); cv = float(monthly_comp.get(m, 0.0))
            if pd.isna(bv): bv = 0.0
            if pd.isna(cv): cv = 0.0
            df_diff = cv - bv
            diff_pct = round((df_diff / bv) * 100, 1) if bv > 0 else 0
            records.append({"month": f"{m}월", "base_val": bv, "comp_val": cv, "diff": df_diff, "diff_pct": diff_pct, "cost": df_diff * price})
            tb += bv; tc += cv
        
        diff_total = tc - tb
        diff_pct_total = round((diff_total / tb) * 100, 1) if tb > 0 else 0

        # 🌟 신규 개통/연장 역사(예: 1호선 숙천·금락 등 기준연도 미운영 개소) 부하 자동 분리 (종합청사 제외)
        new_ext_kwh = 0.0
        new_ext_info = []
        if not is_hq and len(kwh_cols) > 1:
            for col in kwh_cols:
                b_s = float(df_base[col].fillna(0).sum())
                c_s = float(df_comp[col].fillna(0).sum())
                if b_s == 0 and c_s > 0:
                    new_ext_kwh += c_s
                    st_nm = col.split('\n')[1] if '\n' in col else col
                    new_ext_info.append(f"{st_nm}(+{c_s:,.0f} kWh)")
                elif c_s == 0 and b_s > 0:
                    new_ext_kwh -= b_s
                    st_nm = col.split('\n')[1] if '\n' in col else col
                    new_ext_info.append(f"{st_nm}(-{b_s:,.0f} kWh)")

        common_cols = [col for col in kwh_cols if not ((df_base[col].fillna(0).sum() == 0) ^ (df_comp[col].fillna(0).sum() == 0))]
        if not common_cols:
            common_cols = kwh_cols
        df['common_power'] = df[common_cols].sum(axis=1) if len(common_cols) > 1 else (df[common_cols[0]] if common_cols else df['target_power'])
        df_base['common_power'] = df_base[common_cols].sum(axis=1) if len(common_cols) > 1 else (df_base[common_cols[0]] if common_cols else df_base['target_power'])
        df_comp['common_power'] = df_comp[common_cols].sum(axis=1) if len(common_cols) > 1 else (df_comp[common_cols[0]] if common_cols else df_comp['target_power'])

        common_tb = float(df_base['common_power'].sum())
        common_tc = float(df_comp['common_power'].sum())
        common_diff = common_tc - common_tb
        common_diff_pct = round((common_diff / common_tb) * 100, 1) if common_tb > 0 else 0.0

        # 계절별·기간별 정밀 기상 및 대기질 통계 산출
        def calc_env_metrics(sub_df):
            if sub_df.empty:
                return {k: 0 for k in ['tavg','tmax','tmin','humi','s_tmax','s_humi','s_thi','w_tmin','cdd','hdd','cool_days','hw_days','trop_nights','freeze_days','cw_days','pm_avg','pm_bad','days','wk_days','off_days']}
            s_months = [m for m in [6, 7, 8] if start_month <= m <= end_month]
            if not s_months:
                s_months = [m for m in [5, 6, 7, 8, 9] if start_month <= m <= end_month]
            w_months = [m for m in [12, 1, 2] if start_month <= m <= end_month]
            if not w_months:
                w_months = [m for m in [11, 12, 1, 2, 3] if start_month <= m <= end_month]

            s_df = sub_df[sub_df['month'].isin(s_months)] if s_months else sub_df
            w_df = sub_df[sub_df['month'].isin(w_months)] if w_months else sub_df

            return {
                'tavg': float(sub_df['tavg'].mean()) if sub_df['tavg'].notna().any() else 0.0,
                'tmax': float(sub_df['tmax'].mean()) if sub_df['tmax'].notna().any() else 0.0,
                'tmin': float(sub_df['tmin'].mean()) if sub_df['tmin'].notna().any() else 0.0,
                'humi': float(sub_df['humi'].mean()) if sub_df['humi'].notna().any() else 0.0,
                's_tmax': float(s_df['tmax'].mean()) if s_df['tmax'].notna().any() else 0.0,
                's_humi': float(s_df['humi'].mean()) if s_df['humi'].notna().any() else 0.0,
                's_thi': float(s_df['thi'].mean()) if s_df['thi'].notna().any() else 0.0,
                'w_tmin': float(w_df['tmin'].mean()) if w_df['tmin'].notna().any() else 0.0,
                'cdd': float(sub_df['cdd'].sum()),
                'hdd': float(sub_df['hdd'].sum()),
                'cool_days': int((sub_df['tmax'] >= 28.0).sum()),
                'hw_days': int((sub_df['tmax'] >= 33.0).sum()),
                'trop_nights': int((sub_df['tmin'] >= 25.0).sum()),
                'freeze_days': int((sub_df['tmin'] <= 0.0).sum()),
                'cw_days': int((sub_df['tmin'] <= -10.0).sum()),
                'pm_avg': float(sub_df['pm25_val'].mean()) if sub_df['pm25_val'].notna().any() else 0.0,
                'pm_bad': int((sub_df['pm25_val'] >= 35.0).sum()),
                'days': len(sub_df),
                'wk_days': int((sub_df['is_offday'] == 0).sum()),
                'off_days': int((sub_df['is_offday'] == 1).sum()),
                's_label': "하절기(6~8월)" if [m for m in [6, 7, 8] if start_month <= m <= end_month] else "조회기간",
                'w_label': "동절기(12~2월)" if [m for m in [12, 1, 2] if start_month <= m <= end_month] else "조회기간"
            }

        st_b = calc_env_metrics(df_base)
        st_c = calc_env_metrics(df_comp)

        # 승객수 비교 (종합청사는 산출 제외)
        base_pass_sum, comp_pass_sum, b_pass_daily, c_pass_daily = 0.0, 0.0, 0.0, 0.0
        pass_note, p_diff, pass_daily_diff, pass_daily_pct = "", 0.0, 0.0, 0.0
        if not is_hq:
            b_pass_valid = df_base['passengers'].dropna()
            c_pass_valid = df_comp['passengers'].dropna()
            common_pass_months = sorted(set(df_base.dropna(subset=['passengers'])['month']) & set(df_comp.dropna(subset=['passengers'])['month']))
            if common_pass_months and (len(b_pass_valid) < len(df_base) * 0.9 or len(c_pass_valid) < len(df_comp) * 0.9):
                b_p_sub = df_base[df_base['month'].isin(common_pass_months)]['passengers'].dropna()
                c_p_sub = df_comp[df_comp['month'].isin(common_pass_months)]['passengers'].dropna()
                base_pass_sum, comp_pass_sum = float(b_p_sub.sum()), float(c_p_sub.sum())
                b_pass_daily = float(b_p_sub.mean()) if not b_p_sub.empty else 0.0
                c_pass_daily = float(c_p_sub.mean()) if not c_p_sub.empty else 0.0
                pass_note = f" (공통 집계 {min(common_pass_months)}~{max(common_pass_months)}월 기준)"
            else:
                base_pass_sum, comp_pass_sum = float(b_pass_valid.sum()), float(c_pass_valid.sum())
                b_pass_daily = float(b_pass_valid.mean()) if not b_pass_valid.empty else 0.0
                c_pass_daily = float(c_pass_valid.mean()) if not c_pass_valid.empty else 0.0

            p_diff = comp_pass_sum - base_pass_sum
            pass_daily_diff = c_pass_daily - b_pass_daily
            pass_daily_pct = round((pass_daily_diff / b_pass_daily) * 100, 1) if b_pass_daily > 0 else 0.0

        # 평일 vs 휴일(종합청사: 근무일 vs 휴무일 / 노선·역사: 평일 다이아 vs 휴일 다이아) 실측 부하 격차
        wk_avg_b = float(df_base[df_base['is_offday'] == 0]['common_power'].mean()) if not df_base[df_base['is_offday'] == 0].empty else 0.0
        off_avg_b = float(df_base[df_base['is_offday'] == 1]['common_power'].mean()) if not df_base[df_base['is_offday'] == 1].empty else 0.0
        dia_gap_kwh = wk_avg_b - off_avg_b
        wk_diff = st_c['wk_days'] - st_b['wk_days']
        off_diff = st_c['off_days'] - st_b['off_days']
        days_diff = st_c['days'] - st_b['days']
        dia_shift_kwh = (-off_diff) * dia_gap_kwh if days_diff == 0 else (wk_diff * wk_avg_b + off_diff * off_avg_b)

        # 개소별 기온 민감도(kWh/℃) 회귀 산출
        pool_df = df[(df['common_power'] > 0) & (df['date'].dt.year.isin([int(base_year), int(comp_year)]))].copy()
        warm_pool = pool_df[pool_df['tavg'] >= 22.0].dropna(subset=['tavg', 'common_power'])
        cold_pool = pool_df[pool_df['tavg'] <= 12.0].dropna(subset=['tavg', 'common_power'])
        cool_sens = float(np.polyfit(warm_pool['tavg'], warm_pool['common_power'], 1)[0]) if len(warm_pool) >= 6 else 0.0
        heat_sens = float(abs(np.polyfit(cold_pool['tavg'], cold_pool['common_power'], 1)[0])) if len(cold_pool) >= 6 else 0.0

        # 🌟 물리 제약(monotone_constraints)을 적용한 XGBoost 요인별 기여도(kWh) 정량 분해 (종합청사는 승객수 피처 완전 배제)
        if is_hq:
            features = ['month', 'dayofweek', 'is_offday', 'tmax', 'tmin', 'tavg', 'humi', 'cdd', 'hdd', 'thi', 'trop_night_deg', 'pm25_val', 'is_pm25_bad']
            mono_constraints = (0, 0, -1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1)
        else:
            features = ['month', 'dayofweek', 'is_offday', 'passengers', 'tmax', 'tmin', 'tavg', 'humi', 'cdd', 'hdd', 'thi', 'trop_night_deg', 'pm25_val', 'is_pm25_bad']
            mono_constraints = (0, 0, -1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1)

        for f in features:
            med_v = pool_df[f].median() if pd.notna(pool_df[f].median()) else 0.0
            pool_df[f] = pool_df[f].bfill().ffill().fillna(med_v)
            df_base[f] = df_base[f].bfill().ffill().fillna(med_v)
            df_comp[f] = df_comp[f].bfill().ffill().fillna(med_v)

        weather_effect_kwh, pm25_effect_kwh, pass_effect_kwh, pm_unit_bad_kwh = 0.0, 0.0, 0.0, 0.0
        if not pool_df.empty and not df_base.empty and not df_comp.empty:
            model = xgb.XGBRegressor(
                n_estimators=100, learning_rate=0.05, max_depth=4,
                monotone_constraints=mono_constraints, random_state=42
            )
            model.fit(pool_df[features], pool_df['common_power'])
            pred_c_full = float(model.predict(df_comp[features]).sum())

            # 1) 기상 변화(기온·습도·냉난방도일·열대야 축열) 순수 기여량
            df_c_w = df_comp.copy()
            w_cols = ['tmax', 'tmin', 'tavg', 'humi', 'cdd', 'hdd', 'thi', 'trop_night_deg']
            for m in range(start_month, end_month + 1):
                for wc in w_cols:
                    bm = df_base[df_base['month'] == m][wc].mean()
                    cm = df_comp[df_comp['month'] == m][wc].mean()
                    if pd.notna(bm) and pd.notna(cm):
                        df_c_w.loc[df_c_w['month'] == m, wc] = df_comp.loc[df_comp['month'] == m, wc] - (cm - bm)
            weather_effect_kwh = float(pred_c_full - model.predict(df_c_w[features]).sum())

            # 2) 초미세먼지 나쁨(>=35) 공조·환기 설비 추가 가동 순수 기여량
            df_c_pm = df_comp.copy()
            for m in range(start_month, end_month + 1):
                bm = df_base[df_base['month'] == m]['pm25_val'].mean()
                cm = df_comp[df_comp['month'] == m]['pm25_val'].mean()
                if pd.notna(bm) and pd.notna(cm):
                    df_c_pm.loc[df_c_pm['month'] == m, 'pm25_val'] = (df_comp.loc[df_comp['month'] == m, 'pm25_val'] - (cm - bm)).clip(lower=0)
            df_c_pm['is_pm25_bad'] = (df_c_pm['pm25_val'] >= 35.0).astype(int)
            pm25_effect_kwh = float(pred_c_full - model.predict(df_c_pm[features]).sum())

            bad_sample = pool_df[pool_df['is_pm25_bad'] == 1].copy()
            if not bad_sample.empty:
                clean_sample = bad_sample.copy()
                clean_sample['is_pm25_bad'] = 0
                clean_sample['pm25_val'] = 15.0
                pm_unit_bad_kwh = max(0.0, float((model.predict(bad_sample[features]) - model.predict(clean_sample[features])).mean()))

            # 3) 승객수 증감 기여량 (종합청사 제외)
            if not is_hq:
                df_c_p = df_comp.copy()
                if pass_daily_pct != 0:
                    df_c_p['passengers'] = df_comp['passengers'] / (1.0 + (pass_daily_pct / 100.0))
                pass_effect_kwh = float(pred_c_full - model.predict(df_c_p[features]).sum())

        expected_env_diff = weather_effect_kwh + pm25_effect_kwh + dia_shift_kwh + (0.0 if is_hq else pass_effect_kwh)
        net_operation_diff = common_diff - expected_env_diff

        # 월별 최대 증가월 / 최대 감소월 추출
        active_records = [r for r in records if r["base_val"] > 0 and r["comp_val"] > 0]
        max_inc_m = max(active_records, key=lambda x: x["diff"]) if active_records else None
        max_dec_m = min(active_records, key=lambda x: x["diff"]) if active_records else None

        s_tmax_diff = st_c['s_tmax'] - st_b['s_tmax']
        s_humi_diff = st_c['s_humi'] - st_b['s_humi']
        s_thi_diff = st_c['s_thi'] - st_b['s_thi']
        w_tmin_diff = st_c['w_tmin'] - st_b['w_tmin']
        cdd_diff = st_c['cdd'] - st_b['cdd']
        hdd_diff = st_c['hdd'] - st_b['hdd']
        cool_d_diff = st_c['cool_days'] - st_b['cool_days']
        hw_diff = st_c['hw_days'] - st_b['hw_days']
        tn_diff = st_c['trop_nights'] - st_b['trop_nights']
        frz_diff = st_c['freeze_days'] - st_b['freeze_days']
        cw_diff = st_c['cw_days'] - st_b['cw_days']
        pm_avg_diff = st_c['pm_avg'] - st_b['pm_avg']
        pm_bad_diff = st_c['pm_bad'] - st_b['pm_bad']

        period_label = f"{start_month}월~{end_month}월" if start_month != end_month else f"{start_month}월"

        # =====================================================================
        # 📝 [분기] 종합청사 업무시설 전용 리포트 vs 도시철도 노선·역사 리포트
        # =====================================================================
        if is_hq:
            ai_report_text = f"🏢 [{station}] {base_year}년 vs {comp_year}년 ({period_label}) 업무시설(청사) 전력 수요 AI 심층 분석 리포트\n"
            ai_report_text += " ※ 종합청사는 열차 운행(다이아·승객수)과 무관한 업무용 건물 단독 수전 개소로, 기상·대기질·근무일수 전용 모델이 적용되었습니다.\n\n"

            if not asos_data:
                ai_report_text += "⚠️ 기상청 API 응답 지연으로 일부 기상 지표가 제한되었습니다.\n\n"

            ai_report_text += f"[1] 기상(냉·난방·축열) 및 대기질(건물 공조·환기) 핵심 지표 변동 ({period_label})\n"
            ai_report_text += f" • {st_b['s_label']} 평균 최고기온 / 습도 : {st_b['s_tmax']:.1f}℃ ({st_b['s_humi']:.1f}%) ➔ {st_c['s_tmax']:.1f}℃ ({st_c['s_humi']:.1f}%) [기온 {s_tmax_diff:+.1f}℃, 습도 {s_humi_diff:+.1f}%p, 불쾌지수 {s_thi_diff:+.1f}]\n"
            ai_report_text += f" • 냉방 부하 일수 : 실질 냉방일(28℃↑) {st_b['cool_days']}일 ➔ {st_c['cool_days']}일 ({cool_d_diff:+}일) | 폭염(33℃↑) {st_b['hw_days']}일 ➔ {st_c['hw_days']}일 ({hw_diff:+}일) | 냉방도일(CDD) {st_b['cdd']:.1f} ➔ {st_c['cdd']:.1f} ({cdd_diff:+.1f})\n"
            ai_report_text += f" • 열대야(최저 25℃↑, 야간 건물 축열) : {st_b['trop_nights']}일 ➔ {st_c['trop_nights']}일 ({tn_diff:+}일)\n"
            ai_report_text += f" • {st_b['w_label']} 평균 최저기온 : {st_b['w_tmin']:.1f}℃ ➔ {st_c['w_tmin']:.1f}℃ ({w_tmin_diff:+.1f}℃)\n"
            ai_report_text += f" • 난방 부하 일수 : 영하권(0℃↓) {st_b['freeze_days']}일 ➔ {st_c['freeze_days']}일 ({frz_diff:+}일) | 한파(-10℃↓) {st_b['cw_days']}일 ➔ {st_c['cw_days']}일 ({cw_diff:+}일) | 난방도일(HDD) {st_b['hdd']:.1f} ➔ {st_c['hdd']:.1f} ({hdd_diff:+.1f})\n"
            ai_report_text += f" • 초미세먼지(PM2.5) 및 건물 환기·공조('나쁨' 35㎍/㎥↑) : 평균 {st_b['pm_avg']:.1f} ➔ {st_c['pm_avg']:.1f}㎍/㎥ ({pm_avg_diff:+.1f}) | '나쁨' {st_b['pm_bad']}일 ➔ {st_c['pm_bad']}일 ({pm_bad_diff:+}일)\n\n"

            ai_report_text += f"[2] 청사 근무일수(평일/휴무일) 및 업무 부하 변동 ({period_label})\n"
            ai_report_text += f" • 집계 일수 및 근무일 구성 : 총 {st_b['days']}일(평일 근무 {st_b['wk_days']}일 / 주말·공휴일 휴무 {st_b['off_days']}일) ➔ 총 {st_c['days']}일(평일 근무 {st_c['wk_days']}일 / 주말·공휴일 휴무 {st_c['off_days']}일) [평일 {wk_diff:+}일, 휴무 {off_diff:+}일]\n"
            ai_report_text += f" • [종합청사] 근무일별 실측 부하 : 평일 근무일 일평균 {wk_avg_b:,.0f} kWh vs 휴무일 {off_avg_b:,.0f} kWh (평일 정상 근무 시 휴무일 대비 일평균 +{dia_gap_kwh:,.0f} kWh 추가 소요)\n\n"

            ai_report_text += f"🧠 [3] XGBoost 기반 요인별 전력 증감 기여도 정량 분해 (총 증감: {diff_total:+,.0f} kWh / {diff_pct_total:+.1f}%)\n"
            ai_report_text += f"  ├─ ① 기상(냉·난방도일·습도·열대야 축열) 요인 : {weather_effect_kwh:+,.0f} kWh (기상 민감도: 냉방 +1℃당 +{cool_sens:,.0f} kWh/일, 난방 -1℃당 +{heat_sens:,.0f} kWh/일)\n"
            ai_report_text += f"  ├─ ② 대기질(초미세먼지 35↑ 공조·환기 추가가동) 요인 : {pm25_effect_kwh:+,.0f} kWh ('나쁨' 발생 시 건물 환기·공조 가동으로 일평균 +{pm_unit_bad_kwh:,.0f} kWh 상승)\n"
            ai_report_text += f"  ├─ ③ 근무일수(평일 근무일·휴무일 일수 변동) 요인 : {dia_shift_kwh:+,.0f} kWh (평일 근무일 {wk_diff:+}일 / 휴무일 {off_diff:+}일 변동 반영)\n"
            ai_report_text += f"  └─ ④ 외부 여건 보정 후 순수 청사 운영·절전 성과 : {net_operation_diff:+,.0f} kWh ({'청사 에너지 절감 및 운영 최적화 순절감!' if net_operation_diff < 0 else '사무기기·전산실 및 기저 설비 부하 상승'})\n\n"

            ai_report_text += f"🔍 [4] 종합청사 업무시설 설비별 심층 진단\n"
            ai_report_text += f"① 기상(온·습도) 및 청사 건물 축열 영향 ({weather_effect_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • [하절기 냉방 및 제습 부하] 냉방도일(CDD)이 {cdd_diff:+.1f}℃·일, 폭염일수가 {hw_diff:+}일 변동했으며 평균 상대습도는 {s_humi_diff:+.1f}%p(불쾌지수 {s_thi_diff:+.1f})를 기록했습니다. 업무 공간의 쾌적 온도 유지와 제습을 위한 중앙 냉동기 및 시스템에어컨(EHP) 가동 부하에 직접 반영되었습니다.\n"
            ai_report_text += f" • [열대야(최저 25℃↑) 야간 축열 및 출근 시간대 예냉 효과] 업무시설 특성상 야간·새벽 비근무 시간대에는 사무 부하가 줄어들지만, 열대야 일수가 {st_b['trop_nights']}일 ➔ {st_c['trop_nights']}일({tn_diff:+}일)로 변동함에 따라 건물 외피와 실내 구조물의 잔열이 식지 않아 익일 출근 및 업무 개시 시점의 초기 냉방(예냉) 부하가 {'조기 가중되었습니다.' if tn_diff > 0 else ('완화되었습니다.' if tn_diff < 0 else '전년과 유사하게 유지되었습니다.')}\n"
            ai_report_text += f" • [동절기 난방 부하] 영하권(0℃ 이하) 일수가 {st_b['freeze_days']}일 ➔ {st_c['freeze_days']}일({frz_diff:+}일), 난방도일(HDD)이 {hdd_diff:+.1f}℃·일 변동하여 사무실·강당 난방기 및 옥외 배관 동파방지 열선 가동 부하가 {'상승' if hdd_diff > 0 or frz_diff > 0 else '감소'}했습니다.\n\n"

            ai_report_text += f"② 초미세먼지 '나쁨(35㎍/㎥ 이상)' 청사 공조·환기 설비 영향 ({pm25_effect_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • 분석 기간 중 초미세먼지 '나쁨(35㎍/㎥ 이상)' 일수가 {st_b['pm_bad']}일 ➔ {st_c['pm_bad']}일({pm_bad_diff:+}일)로 변동하고 평균 농도가 {pm_avg_diff:+.1f}㎍/㎥ 달라지면서, 청사 내 공조기(AHU)·전열교환기 및 공기청정 설비 가동 변화로 약 {pm25_effect_kwh:+,.0f} kWh의 전력 변동이 산출되었습니다.\n\n"

            ai_report_text += f"③ 평일 근무일수 및 휴무일 변동 영향 ({dia_shift_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • [종합청사]는 임직원이 출근하는 평일 근무일에 주말·공휴일(휴무일) 대비 일평균 {dia_gap_kwh:,.0f} kWh의 전력을 더 소비합니다. 비교 기간 중 평일 근무일수가 {wk_diff:+}일, 휴무일수가 {off_diff:+}일(총 집계일수 {days_diff:+}일) 변동하여 사무실 조명·OA기기·냉난방·승강기 가동에서 {dia_shift_kwh:+,.0f} kWh의 구조적 증감이 발생했습니다.\n\n"

            ai_report_text += f"④ 외부 환경 보정 후 실질 절전·운영 성과 종합 평가 ({net_operation_diff:+,.0f} kWh): \n"
            if net_operation_diff < 0:
                ai_report_text += f" • 🌟 [핵심 성과] 기상·미세먼지·근무일수 변동을 모두 보정한 기대 사용량 대비, 실제 청사 전력 사용량은 {abs(net_operation_diff):,.0f} kWh (약 {abs(net_operation_diff)*price:,.0f}원 상당) 더 적게 소비되었습니다. 이는 실내 적정 냉·난방 온도 준수, 점심·퇴근 시간대 조명 소등 및 대기전력 차단 등 청사 자체 에너지 절감 운영이 실질적인 성과로 이어진 결과입니다.\n"
            else:
                ai_report_text += f" • 기상·미세먼지·근무일수 요인 외에 청사 기저 설비 부하에서 {net_operation_diff:+,.0f} kWh의 순증가가 확인됩니다. 전산실·서버 부하 증가, 신규 사무기기·전기차 충전기 증설, 또는 야간·주말 비근무 시간대 대기전력 누수 여부를 점검할 필요가 있습니다.\n"

        else:
            ai_report_text = f"📊 [{station}] {base_year}년 vs {comp_year}년 ({period_label}) 도시철도 전력 수요 AI 심층 분석 리포트\n\n"

            if not asos_data:
                ai_report_text += "⚠️ 기상청 API 응답 지연으로 일부 기상 지표가 제한되었습니다.\n\n"

            ai_report_text += f"[1] 기상(냉·난방·축열) 및 대기질(역사 공조) 핵심 지표 변동 ({period_label})\n"
            ai_report_text += f" • {st_b['s_label']} 평균 최고기온 / 습도 : {st_b['s_tmax']:.1f}℃ ({st_b['s_humi']:.1f}%) ➔ {st_c['s_tmax']:.1f}℃ ({st_c['s_humi']:.1f}%) [기온 {s_tmax_diff:+.1f}℃, 습도 {s_humi_diff:+.1f}%p, 불쾌지수 {s_thi_diff:+.1f}]\n"
            ai_report_text += f" • 냉방 부하 일수 : 실질 냉방일(28℃↑) {st_b['cool_days']}일 ➔ {st_c['cool_days']}일 ({cool_d_diff:+}일) | 폭염(33℃↑) {st_b['hw_days']}일 ➔ {st_c['hw_days']}일 ({hw_diff:+}일) | 냉방도일(CDD) {st_b['cdd']:.1f} ➔ {st_c['cdd']:.1f} ({cdd_diff:+.1f})\n"
            ai_report_text += f" • 열대야(최저 25℃↑, 야간 터널·역사 축열) : {st_b['trop_nights']}일 ➔ {st_c['trop_nights']}일 ({tn_diff:+}일)\n"
            ai_report_text += f" • {st_b['w_label']} 평균 최저기온 : {st_b['w_tmin']:.1f}℃ ➔ {st_c['w_tmin']:.1f}℃ ({w_tmin_diff:+.1f}℃)\n"
            ai_report_text += f" • 난방 부하 일수 : 영하권(0℃↓) {st_b['freeze_days']}일 ➔ {st_c['freeze_days']}일 ({frz_diff:+}일) | 한파(-10℃↓) {st_b['cw_days']}일 ➔ {st_c['cw_days']}일 ({cw_diff:+}일) | 난방도일(HDD) {st_b['hdd']:.1f} ➔ {st_c['hdd']:.1f} ({hdd_diff:+.1f})\n"
            ai_report_text += f" • 초미세먼지(PM2.5) 및 공조 강제가동('나쁨' 35㎍/㎥↑) : 평균 {st_b['pm_avg']:.1f} ➔ {st_c['pm_avg']:.1f}㎍/㎥ ({pm_avg_diff:+.1f}) | '나쁨' {st_b['pm_bad']}일 ➔ {st_c['pm_bad']}일 ({pm_bad_diff:+}일)\n\n"

            ai_report_text += f"[2] 도시철도 열차 운행 다이아(평일/휴일) 및 여객 수송 지표 변동 ({period_label})\n"
            ai_report_text += f" • 집계 일수 및 열차 다이아 : 총 {st_b['days']}일(평일 {st_b['wk_days']}일 / 휴일 {st_b['off_days']}일) ➔ 총 {st_c['days']}일(평일 {st_c['wk_days']}일 / 휴일 {st_c['off_days']}일) [평일 {wk_diff:+}일, 휴일 {off_diff:+}일]\n"
            ai_report_text += f" • [{station}] 다이아별 실측 부하 : 평일 다이아 일평균 {wk_avg_b:,.0f} kWh vs 휴일 다이아 {off_avg_b:,.0f} kWh (평일 운행 시 일평균 +{dia_gap_kwh:,.0f} kWh 추가 소요)\n"
            ai_report_text += f" • 여객 수송량{pass_note} : 일평균 {b_pass_daily:,.0f}명 ➔ {c_pass_daily:,.0f}명 ({pass_daily_diff:+,.0f}명/일, {pass_daily_pct:+.1f}%) | 누적 {base_pass_sum:,.0f}명 ➔ {comp_pass_sum:,.0f}명 ({p_diff:+,.0f}명)\n\n"

            ai_report_text += f"🧠 [3] XGBoost 기반 요인별 전력 증감 기여도 정량 분해 (총 증감: {diff_total:+,.0f} kWh / {diff_pct_total:+.1f}%)\n"
            if new_ext_kwh != 0:
                ai_report_text += f" 📌 [신규 개통·연장 구간 분리] : {', '.join(new_ext_info)} 신규 편입으로 +{new_ext_kwh:,.0f} kWh 발생\n"
                ai_report_text += f" 📌 [기존 동일 운영 구간 실질 증감] : {common_tb:,.0f} kWh ➔ {common_tc:,.0f} kWh ({common_diff:+,.0f} kWh, {common_diff_pct:+.1f}%)\n"
            ai_report_text += f"  ├─ ① 기상(냉·난방도일·습도·열대야 축열) 요인 : {weather_effect_kwh:+,.0f} kWh (기상 민감도: 냉방 +1℃당 +{cool_sens:,.0f} kWh/일, 난방 -1℃당 +{heat_sens:,.0f} kWh/일)\n"
            ai_report_text += f"  ├─ ② 대기질(초미세먼지 35↑ 공조 추가가동) 요인 : {pm25_effect_kwh:+,.0f} kWh ('나쁨' 발생 시 역사 환기·공조 가동으로 일평균 +{pm_unit_bad_kwh:,.0f} kWh 상승)\n"
            ai_report_text += f"  ├─ ③ 열차 다이아(평일·휴일 운행일수 변동) 요인 : {dia_shift_kwh:+,.0f} kWh (평일 {wk_diff:+}일 / 휴일 {off_diff:+}일 변동 반영)\n"
            ai_report_text += f"  ├─ ④ 여객 수요(승객수 변동 동력·환기) 요인 : {pass_effect_kwh:+,.0f} kWh (일평균 승객 {pass_daily_pct:+.1f}% 변동 반영)\n"
            ai_report_text += f"  └─ ⑤ 외부 여건 보정 후 순수 설비 운영·절전 성과 : {net_operation_diff:+,.0f} kWh ({'전사적 절전 및 운영 최적화 순절감!' if net_operation_diff < 0 else '설비 가동률 및 기저부하 상승'})\n\n"

            ai_report_text += f"🔍 [4] 도시철도 계통·설비별 심층 진단\n"
            ai_report_text += f"① 기상(온·습도) 및 터널·역사 축열 영향 ({weather_effect_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • [하절기 냉방 및 제습(잠열) 부하] 냉방도일(CDD)이 {cdd_diff:+.1f}℃·일, 폭염일수가 {hw_diff:+}일 변동했으며 평균 상대습도는 {s_humi_diff:+.1f}%p(불쾌지수 {s_thi_diff:+.1f}) 기록되었습니다. 지하 역사는 외부 습기 유입 시 냉동기 제습(잠열) 부하가 급증하는데, 해당 온·습도 변화가 역사 냉방 전력에 직접 반영되었습니다.\n"
            ai_report_text += f" • [도시철도 열대야(최저 25℃↑) 초기 예냉 효과] 도시철도 특성상 열차 운행이 종료된 심야·새벽 시간대(01~05시)에는 영업 전력 부하가 없습니다. 다만 열대야 일수가 {st_b['trop_nights']}일 ➔ {st_c['trop_nights']}일({tn_diff:+}일)로 변동함에 따라, 야간 비운행 시간 동안 지하 구조물과 본선 터널 내부의 축열(잔열)이 식지 않아 익일 첫차 운행 및 역사 개방 시점(05:00~오전)부터 초기 냉방(예냉) 및 본선 환기 부하가 {'조기 가중되었습니다.' if tn_diff > 0 else ('완화되었습니다.' if tn_diff < 0 else '전년과 유사하게 유지되었습니다.')}\n"
            ai_report_text += f" • [동절기 난방 부하] 실질 난방 가동 지표인 영하권(0℃ 이하) 일수가 {st_b['freeze_days']}일 ➔ {st_c['freeze_days']}일({frz_diff:+}일), 난방도일(HDD)이 {hdd_diff:+.1f}℃·일 변동하여 대합실·현업실 난방 및 동파방지 열선 부하가 {'상승' if hdd_diff > 0 or frz_diff > 0 else '감소'}했습니다.\n\n"

            ai_report_text += f"② 초미세먼지 '나쁨(35㎍/㎥ 이상)' 역사 공조설비 추가 가동 영향 ({pm25_effect_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • 도시철도 지하 역사는 초미세먼지 농도가 '나쁨(35㎍/㎥ 이상)' 도달 시 실내 공기질 유지 기준에 따라 외기 도입·배기 팬(공조기 AHU), 전기집진기 및 본선 환기설비를 추가 연장·강제 가동합니다.\n"
            ai_report_text += f" • 분석 기간 중 35㎍/㎥ 이상 '나쁨' 일수가 {st_b['pm_bad']}일 ➔ {st_c['pm_bad']}일({pm_bad_diff:+}일)로 변동하고 평균 농도가 {pm_avg_diff:+.1f}㎍/㎥ 달라지면서, 공조·환기 동력 설비에서 약 {pm25_effect_kwh:+,.0f} kWh의 전력 변동이 발생한 것으로 AI 모델은 산출했습니다.\n\n"

            ai_report_text += f"③ 열차 운행 다이아(평일/휴일) 및 여객 이동편의 설비 영향 ({dia_shift_kwh + pass_effect_kwh:+,.0f} kWh): \n"
            ai_report_text += f" • [열차 다이아 효과 ({dia_shift_kwh:+,.0f} kWh)] [{station}] 기준 열차 운행 횟수(첨두시 배차)가 많은 '평일 다이아'는 '휴일 다이아' 대비 일평균 {dia_gap_kwh:,.0f} kWh의 전력을 더 소비합니다. 비교 기간 중 평일 운행일수가 {wk_diff:+}일, 휴일 운행일수가 {off_diff:+}일(총 집계일수 {days_diff:+}일) 변동하여 변전소 추진 전력 및 연동 설비에서 {dia_shift_kwh:+,.0f} kWh의 구조적 증감이 발생했습니다.\n"
            ai_report_text += f" • [승객수 변동 효과 ({pass_effect_kwh:+,.0f} kWh)] 일평균 승객수가 {pass_daily_diff:+,.0f}명({pass_daily_pct:+.1f}%) 변동함에 따라 에스컬레이터(E/S)·엘리베이터(E/V)·스크린도어(PSD) 가동 부하와 역사 내 재실 인체 발열·CO₂ 농도 상승에 따른 공조 환기 연동 부하가 {pass_effect_kwh:+,.0f} kWh 변동했습니다.\n\n"

            ai_report_text += f"④ 외부 환경 보정 후 실질 절전·운영 성과 종합 평가 ({net_operation_diff:+,.0f} kWh): \n"
            if net_operation_diff < 0:
                ai_report_text += f" • 🌟 [핵심 성과] 기상·미세먼지·열차 다이아·승객수 등 외부 환경 변화를 모두 보정한 기대 사용량 대비, 실제 기존 구간 전력 사용량은 오히려 {abs(net_operation_diff):,.0f} kWh (약 {abs(net_operation_diff)*price:,.0f}원 상당) 더 적게 소비되었습니다. 이는 공사가 추진 중인 냉·난방 탄력 운전, 공조기 인버터 제어 및 조명·동력 설비 절전 운영 최적화가 외부 부하 상승 요인을 성공적으로 상쇄한 실질적 절전 성과입니다.\n"
            else:
                ai_report_text += f" • 외부 환경(기상·미세먼지·다이아·승객) 요인 외에 기저 설비 부하에서 {net_operation_diff:+,.0f} kWh의 순증가가 확인됩니다. 신규 편의시설 증설, 노후 설비 효율 저하 또는 비영업 시간대 대기전력 증가 여부를 점검할 필요가 있습니다.\n"

        if max_inc_m or max_dec_m:
            peak_notes = []
            if max_inc_m and max_inc_m["diff"] > 0:
                peak_notes.append(f"최대 증가월은 [{max_inc_m['month']}]({max_inc_m['diff']:+,.0f} kWh, {max_inc_m['diff_pct']:+.1f}%)")
            if max_dec_m and max_dec_m["diff"] < 0:
                peak_notes.append(f"최대 절감월은 [{max_dec_m['month']}]({max_dec_m['diff']:+,.0f} kWh, {max_dec_m['diff_pct']:+.1f}%)")
            if peak_notes:
                ai_report_text += f" • [월별 핵심 변동 구간] 비교 기간 중 {' / '.join(peak_notes)}로 나타났습니다."

        res = {
            "summary": { "total_base": tb, "total_comp": tc, "diff": diff_total, "diff_pct": diff_pct_total, "cost": diff_total*price, "ai_report": ai_report_text, "period_label": period_label }, 
            "records": records
        }
        set_bounded_api_cache("compare", cache_key, res)
        gc.collect()
        return res
    except Exception as e:
        return {"error": f"비교 분석 중 서버 에러가 발생했습니다: {str(e)}\n{traceback.format_exc()}"}

@app.get("/api/predict/{station}")
def get_predict_data(station: str, target_year: str, pass_rate: float = 0.0, temp_adj: float = 0.0, winter_temp_adj: float = 0.0, pm25_adj: int = 0, reports_data: str = None):
    is_hq = (station == '종합청사')
    eff_pass_rate = 0.0 if is_hq else float(pass_rate)

    # 🌟 프론트엔드에서 reports_data를 넘기지 않아도 백엔드가 직접 Firebase 부하증감 신고 내역을 자동 조회
    local_reports = []
    if reports_data:
        try:
            local_reports = json.loads(reports_data)
        except Exception:
            local_reports = []
    elif "여기에_파이어베이스" not in FIREBASE_DB_URL:
        try:
            r_fb = requests.get(f"{FIREBASE_DB_URL}/load_reports.json", timeout=3)
            if r_fb.status_code == 200 and r_fb.json():
                fb_data = r_fb.json()
                if isinstance(fb_data, dict):
                    local_reports = list(fb_data.values())
        except Exception:
            local_reports = []

    rep_hash = hashlib.md5(json.dumps(local_reports, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if local_reports else "none"
    cache_key = f"{station}_{target_year}_{eff_pass_rate}_{temp_adj}_{winter_temp_adj}_{pm25_adj}_{rep_hash}"
    if cache_key in GLOBAL_API_CACHE["predict"]:
        return GLOBAL_API_CACHE["predict"][cache_key]

    try: 
        df = load_excel_dataset()
        if df is None: return {"error": "과거 다년간의 머신러닝 학습을 위해 데이터셋(Excel) 파일을 수동으로 먼저 업로드해 주세요."}
            
        pass_col = next((c for c in df.columns if '승객수' in str(c) or '수송인원' in str(c)), None)
        if not is_hq and pass_col is None:
            return {"error": "엑셀 첫번째 시트에 '승객수' 컬럼이 포함되어 있는지 확인해 주세요."}
        
        if station == '전체':
            kwh_cols = [c for c in df.columns if 'total_kwh' in c and '종합청사' not in c]
            peak_cols = [c for c in df.columns if 'peak_kw' in c and '종합청사' not in c]
        elif station in LINE_STATIONS:
            line_stations = LINE_STATIONS[station]
            kwh_cols = [c for c in df.columns if any(s in c for s in line_stations) and 'total_kwh' in c]
            peak_cols = [c for c in df.columns if any(s in c for s in line_stations) and 'peak_kw' in c]
        else:
            kwh_cols = [c for c in df.columns if station in c and 'total_kwh' in c]
            peak_cols = [c for c in df.columns if station in c and 'peak_kw' in c]

        if not kwh_cols:
            return {"error": f"[{station}]의 전력량 컬럼을 데이터셋에서 찾을 수 없습니다."}
        
        target_y = int(target_year)
        
        start_dt = pd.to_datetime(f"{target_y}-01-01")
        end_dt = pd.to_datetime(f"{target_y}-12-31")
        full_year_dates = pd.date_range(start=start_dt, end=end_dt)
        
        existing_dates = pd.to_datetime(df['date']).dt.normalize().tolist()
        missing_dates = pd.Index(full_year_dates).difference(pd.Index(existing_dates))
        
        if len(missing_dates) > 0:
            missing_df = pd.DataFrame({'date': missing_dates})
            df = pd.concat([df, missing_df], ignore_index=True)
            df.sort_values(by='date', inplace=True)
            df.reset_index(drop=True, inplace=True)

        kr_holidays = holidays.KR()
        df['month'] = df['date'].dt.month
        df['dayofweek'] = df['date'].dt.dayofweek
        df['is_weekend'] = df['dayofweek'].isin([5, 6]).astype(int)
        df['is_holiday'] = df['date'].map(lambda x: 1 if (x in kr_holidays and x.dayofweek < 5) else 0)

        # 직전 연도 실제 실적(전력량 kWh 및 최대수요 Peak kW) 산출용 원시 합계
        df['raw_power'] = df[kwh_cols].sum(axis=1, min_count=1) if len(kwh_cols) > 1 else pd.to_numeric(df[kwh_cols[0]], errors='coerce')
        df['raw_peak'] = df[peak_cols].sum(axis=1, min_count=1) if len(peak_cols) > 1 else (pd.to_numeric(df[peak_cols[0]], errors='coerce') if peak_cols else df['raw_power'] / 14.0)

        # 🌟 신규 연장 개통 역사(예: 1호선 숙천·금락) 포함 시 미개통 연도(23~24년) 스케일을 현재 운영 기준으로 보정하여 과소 예측 방지
        df_train_adj = df.copy()
        if len(kwh_cols) > 1:
            for col in kwh_cols:
                s_num = pd.to_numeric(df_train_adj[col], errors='coerce')
                active_mask = s_num > 0
                if active_mask.any():
                    first_open_dt = df_train_adj.loc[active_mask, 'date'].min()
                    if first_open_dt > pd.to_datetime("2023-06-01"):
                        active_sub = df_train_adj.loc[active_mask]
                        grp_mean = active_sub.groupby(['month', 'is_holiday'])[col].mean().to_dict()
                        overall_mean = float(active_sub[col].mean())
                        pre_mask = df_train_adj['date'] < first_open_dt
                        df_train_adj.loc[pre_mask, col] = df_train_adj.loc[pre_mask].apply(
                            lambda r: grp_mean.get((r['month'], r['is_holiday']), overall_mean), axis=1
                        )
            for col in peak_cols:
                s_num = pd.to_numeric(df_train_adj[col], errors='coerce')
                active_mask = s_num > 0
                if active_mask.any():
                    first_open_dt = df_train_adj.loc[active_mask, 'date'].min()
                    if first_open_dt > pd.to_datetime("2023-06-01"):
                        active_sub = df_train_adj.loc[active_mask]
                        grp_mean = active_sub.groupby(['month', 'is_holiday'])[col].mean().to_dict()
                        overall_mean = float(active_sub[col].mean())
                        pre_mask = df_train_adj['date'] < first_open_dt
                        df_train_adj.loc[pre_mask, col] = df_train_adj.loc[pre_mask].apply(
                            lambda r: grp_mean.get((r['month'], r['is_holiday']), overall_mean), axis=1
                        )

        df['target_power'] = df_train_adj[kwh_cols].sum(axis=1, min_count=1) if len(kwh_cols) > 1 else pd.to_numeric(df_train_adj[kwh_cols[0]], errors='coerce')
        df['target_peak'] = df_train_adj[peak_cols].sum(axis=1, min_count=1) if len(peak_cols) > 1 else (pd.to_numeric(df_train_adj[peak_cols[0]], errors='coerce') if peak_cols else df['target_power'] / 14.0)
        
        asos_data = fetch_asos_daily("2023-01-01", f"{target_y}-12-31")
        
        df['temp_max'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: float(asos_data.get(x, {}).get('tmax')) if is_valid_temp(asos_data.get(x, {}).get('tmax')) else np.nan)
        df['temp_min'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: float(asos_data.get(x, {}).get('tmin')) if is_valid_temp(asos_data.get(x, {}).get('tmin')) else np.nan)
        df['temp_avg'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: float(asos_data.get(x, {}).get('tavg')) if is_valid_temp(asos_data.get(x, {}).get('tavg')) else np.nan)
        df['temp_avg'] = df['temp_avg'].fillna((df['temp_max'] + df['temp_min']) / 2.0)
        df['humidity'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: float(asos_data.get(x, {}).get('humi')) if is_valid_humi(asos_data.get(x, {}).get('humi')) else np.nan)
        df['passengers'] = pd.to_numeric(df[pass_col], errors='coerce') if (pass_col and not is_hq) else 0.0

        if 'pm25_val' in df.columns:
            df['pm25'] = pd.to_numeric(df['pm25_val'], errors='coerce')
        else:
            df['pm25'] = 18.0

        if df['temp_max'].isna().all():
            return {"error": "기상청 API 허브 서버와 통신할 수 없습니다. 잠시 후 [AI 예측 실행]을 다시 눌러주세요."}

        # 🌟 미래 연도 미집계 구간의 기상·미세먼지·승객수 기준선(Baseline) 정밀 매칭
        df['mm_dd'] = df['date'].dt.strftime('%m-%d')
        hist_valid_w = df[df['date'].dt.year < target_y].dropna(subset=['temp_max', 'temp_min'])
        hist_w_by_mmdd = hist_valid_w.sort_values('date', ascending=False).drop_duplicates('mm_dd').set_index('mm_dd')
        hist_w_by_month = hist_valid_w.groupby('month')[['temp_max', 'temp_min', 'temp_avg', 'humidity']].mean()

        hist_valid_pm = df[df['date'].dt.year < target_y].dropna(subset=['pm25'])
        hist_pm_by_mmdd = hist_valid_pm.sort_values('date', ascending=False).drop_duplicates('mm_dd').set_index('mm_dd')
        hist_pm_by_month = hist_valid_pm.groupby('month')['pm25'].mean()

        if not is_hq:
            hist_valid_pass = df.dropna(subset=['passengers']).copy()
            recent_pass_df = hist_valid_pass.sort_values('date').tail(365)
            pass_by_m_hol_dow = recent_pass_df.groupby(['month', 'is_holiday', 'dayofweek'])['passengers'].mean().to_dict()
            pass_by_m_hol = recent_pass_df.groupby(['month', 'is_holiday'])['passengers'].mean().to_dict()
            pass_overall_mean = float(recent_pass_df['passengers'].mean()) if not recent_pass_df.empty else 350000.0
        else:
            pass_by_m_hol_dow, pass_by_m_hol, pass_overall_mean = {}, {}, 0.0

        idx_target = df['date'].dt.year == target_y
        for i in df[idx_target].index:
            mm_dd = df.loc[i, 'mm_dd']
            m_val = int(df.loc[i, 'month'])
            hol_val = int(df.loc[i, 'is_holiday'])
            dow_val = int(df.loc[i, 'dayofweek'])

            if pd.isna(df.loc[i, 'temp_max']):
                df.loc[i, 'temp_max'] = hist_w_by_mmdd.loc[mm_dd, 'temp_max'] if mm_dd in hist_w_by_mmdd.index else hist_w_by_month.loc[m_val, 'temp_max']
            if pd.isna(df.loc[i, 'temp_min']):
                df.loc[i, 'temp_min'] = hist_w_by_mmdd.loc[mm_dd, 'temp_min'] if mm_dd in hist_w_by_mmdd.index else hist_w_by_month.loc[m_val, 'temp_min']
            if pd.isna(df.loc[i, 'temp_avg']):
                df.loc[i, 'temp_avg'] = hist_w_by_mmdd.loc[mm_dd, 'temp_avg'] if mm_dd in hist_w_by_mmdd.index else (df.loc[i, 'temp_max'] + df.loc[i, 'temp_min']) / 2.0
            if pd.isna(df.loc[i, 'humidity']):
                df.loc[i, 'humidity'] = hist_w_by_mmdd.loc[mm_dd, 'humidity'] if mm_dd in hist_w_by_mmdd.index else hist_w_by_month.loc[m_val, 'humidity']
            if pd.isna(df.loc[i, 'pm25']):
                df.loc[i, 'pm25'] = hist_pm_by_mmdd.loc[mm_dd, 'pm25'] if mm_dd in hist_pm_by_mmdd.index else hist_pm_by_month.loc[m_val, 'pm25']
            if not is_hq and pd.isna(df.loc[i, 'passengers']):
                df.loc[i, 'passengers'] = pass_by_m_hol_dow.get((m_val, hol_val, dow_val), pass_by_m_hol.get((m_val, hol_val), pass_overall_mean))
        
        if not is_hq:
            df['passengers'] = df.groupby(['month', 'is_holiday'])['passengers'].transform(lambda s: s.fillna(s.mean())).bfill().ffill()
        df['temp_max'] = df['temp_max'].bfill().ffill()
        df['temp_min'] = df['temp_min'].bfill().ffill()
        df['temp_avg'] = df['temp_avg'].fillna((df['temp_max'] + df['temp_min']) / 2.0).bfill().ffill()
        df['humidity'] = df['humidity'].bfill().ffill()
        df['pm25'] = df['pm25'].bfill().ffill()

        def add_transit_features(d_frame):
            d_frame['cdd'] = (d_frame['temp_avg'] - 24.0).clip(lower=0)
            d_frame['hdd'] = (12.0 - d_frame['temp_avg']).clip(lower=0)
            d_frame['trop_night_deg'] = (d_frame['temp_min'] - 25.0).clip(lower=0)
            d_frame['is_pm25_bad'] = (d_frame['pm25'] >= 35.0).astype(int)
            return d_frame

        df = add_transit_features(df)

        # 🌟 종합청사 선택 시 승객수 피처를 학습 및 예측에서 완전 제외
        if is_hq:
            features = [
                'month', 'dayofweek', 'is_weekend', 'is_holiday',
                'temp_max', 'temp_min', 'temp_avg', 'cdd', 'hdd', 'trop_night_deg',
                'humidity', 'pm25', 'is_pm25_bad'
            ]
            mono_constraints = (0, 0, -1, -1, 0, 0, 0, 1, 1, 1, 0, 1, 1)
        else:
            features = [
                'month', 'dayofweek', 'is_weekend', 'is_holiday', 'passengers',
                'temp_max', 'temp_min', 'temp_avg', 'cdd', 'hdd', 'trop_night_deg',
                'humidity', 'pm25', 'is_pm25_bad'
            ]
            mono_constraints = (0, 0, -1, -1, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1)

        train_df = df[(df['date'].dt.year <= (target_y - 1)) & (df['raw_power'] > 0)].copy()
        train_df = train_df.dropna(subset=['target_power', 'target_peak'] + features)
        if train_df.empty: return {"error": "학습할 전력량 데이터(과거 실측치)가 존재하지 않습니다."}
             
        test_base_df = df[df['date'].dt.year == target_y].copy()
        test_base_df = test_base_df.dropna(subset=features)
        if test_base_df.empty: return {"error": f"{target_year}년도 예측을 위한 데이터 포맷이 처리 중 소실되었습니다."}

        # 🌟 시뮬레이션 변수 반영 (종합청사는 승객수 변동 미적용)
        test_df = test_base_df.copy()
        if not is_hq and eff_pass_rate != 0:
            test_df['passengers'] = test_df['passengers'] * (1.0 + (eff_pass_rate / 100.0))

        if temp_adj != 0:
            s_mask = test_df['month'].isin([6, 7, 8])
            test_df.loc[s_mask, 'temp_max'] += float(temp_adj)
            test_df.loc[s_mask, 'temp_avg'] += float(temp_adj)
            test_df.loc[s_mask, 'temp_min'] += float(temp_adj)

        if winter_temp_adj != 0:
            w_mask = test_df['month'].isin([12, 1, 2])
            test_df.loc[w_mask, 'temp_max'] += float(winter_temp_adj)
            test_df.loc[w_mask, 'temp_avg'] += float(winter_temp_adj)
            test_df.loc[w_mask, 'temp_min'] += float(winter_temp_adj)
            
        if pm25_adj != 0:
            if pm25_adj > 0:
                normal_days = test_df[test_df['pm25'] < 35.0]
                adjust_idx = normal_days.sort_values('pm25', ascending=False).head(int(pm25_adj)).index
                test_df.loc[adjust_idx, 'pm25'] = np.maximum(test_df.loc[adjust_idx, 'pm25'] + 15.0, 42.0)
            elif pm25_adj < 0:
                bad_days = test_df[test_df['pm25'] >= 35.0]
                adjust_idx = bad_days.sort_values('pm25', ascending=True).head(abs(int(pm25_adj))).index
                test_df.loc[adjust_idx, 'pm25'] = 20.0

        test_df = add_transit_features(test_df)
        
        X_train = train_df[features].bfill().ffill()
        y_train_kwh = train_df['target_power'].copy()
        y_train_peak = train_df['target_peak'].copy()
        X_test_base = test_base_df[features].bfill().ffill()
        X_test = test_df[features].bfill().ffill()
        
        model_kwh = xgb.XGBRegressor(
            n_estimators=120, learning_rate=0.05, max_depth=5,
            monotone_constraints=mono_constraints, random_state=42
        )
        model_kwh.fit(X_train, y_train_kwh)

        model_peak = xgb.XGBRegressor(
            n_estimators=120, learning_rate=0.05, max_depth=5,
            monotone_constraints=mono_constraints, random_state=42
        )
        model_peak.fit(X_train, y_train_peak)

        base_pred_kwh = model_kwh.predict(X_test_base)
        raw_pred_kwh = model_kwh.predict(X_test)
        base_pred_peak = model_peak.predict(X_test_base)
        raw_pred_peak = model_peak.predict(X_test)

        train_last_year_df = train_df[train_df['date'].dt.year == (target_y - 1)].copy()
        ref_df = train_last_year_df if not train_last_year_df.empty else train_df
        ref_pred_peak = model_peak.predict(ref_df[features].bfill().ffill())
        actual_ref_peak = float(ref_df['target_peak'].max())
        pred_ref_peak_max = float(np.max(ref_pred_peak)) if len(ref_pred_peak) > 0 else 1.0
        crest_factor = float(np.clip(actual_ref_peak / pred_ref_peak_max, 1.0, 1.35)) if pred_ref_peak_max > 0 else 1.0

        warm_tr = train_df[train_df['temp_avg'] >= 22.0]
        cold_tr = train_df[train_df['temp_avg'] <= 12.0]
        cool_slope_kwh = max(0.0, float(np.polyfit(warm_tr['temp_avg'], warm_tr['target_power'], 1)[0])) if len(warm_tr) >= 10 else 0.0
        heat_slope_kwh = max(0.0, float(-np.polyfit(cold_tr['temp_avg'], cold_tr['target_power'], 1)[0])) if len(cold_tr) >= 10 else 0.0
        cool_slope_kw = max(0.0, float(np.polyfit(warm_tr['temp_avg'], warm_tr['target_peak'], 1)[0])) if len(warm_tr) >= 10 else 0.0
        heat_slope_kw = max(0.0, float(-np.polyfit(cold_tr['temp_avg'], cold_tr['target_peak'], 1)[0])) if len(cold_tr) >= 10 else 0.0

        wk_mean_kwh = float(train_df[train_df['is_holiday'] == 0]['target_power'].mean())
        off_mean_kwh = float(train_df[train_df['is_holiday'] == 1]['target_power'].mean())
        var_load_kwh = max(wk_mean_kwh - off_mean_kwh, wk_mean_kwh * 0.05)
        wk_mean_kw = float(train_df[train_df['is_holiday'] == 0]['target_peak'].mean())
        off_mean_kw = float(train_df[train_df['is_holiday'] == 1]['target_peak'].mean())
        var_load_kw = max(wk_mean_kw - off_mean_kw, wk_mean_kw * 0.05)

        final_pred_kwh = raw_pred_kwh.copy()
        final_pred_peak = (raw_pred_peak * crest_factor).copy()
        base_scaled_peak = (base_pred_peak * crest_factor).copy()

        for idx_pos, (_, row_t) in enumerate(test_df.iterrows()):
            m_v = int(row_t['month'])
            if m_v in [6, 7, 8] and temp_adj != 0:
                tree_dk = raw_pred_kwh[idx_pos] - base_pred_kwh[idx_pos]
                phys_dk = float(temp_adj) * cool_slope_kwh
                if (temp_adj > 0 and tree_dk < phys_dk * 0.5) or (temp_adj < 0 and tree_dk > phys_dk * 0.5):
                    final_pred_kwh[idx_pos] = base_pred_kwh[idx_pos] + 0.5 * tree_dk + 0.5 * phys_dk

                tree_dp = final_pred_peak[idx_pos] - base_scaled_peak[idx_pos]
                phys_dp = float(temp_adj) * cool_slope_kw * crest_factor
                if (temp_adj > 0 and tree_dp < phys_dp * 0.5) or (temp_adj < 0 and tree_dp > phys_dp * 0.5):
                    final_pred_peak[idx_pos] = base_scaled_peak[idx_pos] + 0.5 * tree_dp + 0.5 * phys_dp

            if m_v in [12, 1, 2] and winter_temp_adj != 0:
                tree_dk = raw_pred_kwh[idx_pos] - base_pred_kwh[idx_pos]
                phys_dk = -float(winter_temp_adj) * heat_slope_kwh
                if (winter_temp_adj < 0 and tree_dk < phys_dk * 0.5) or (winter_temp_adj > 0 and tree_dk > phys_dk * 0.5):
                    final_pred_kwh[idx_pos] = base_pred_kwh[idx_pos] + 0.5 * tree_dk + 0.5 * phys_dk

                tree_dp = final_pred_peak[idx_pos] - base_scaled_peak[idx_pos]
                phys_dp = -float(winter_temp_adj) * heat_slope_kw * crest_factor
                if (winter_temp_adj < 0 and tree_dp < phys_dp * 0.5) or (winter_temp_adj > 0 and tree_dp > phys_dp * 0.5):
                    final_pred_peak[idx_pos] = base_scaled_peak[idx_pos] + 0.5 * tree_dp + 0.5 * phys_dp

            if not is_hq and eff_pass_rate != 0:
                phys_pk = (eff_pass_rate / 100.0) * var_load_kwh * 0.35
                phys_pp = (eff_pass_rate / 100.0) * var_load_kw * 0.35
                final_pred_kwh[idx_pos] += phys_pk * 0.5
                final_pred_peak[idx_pos] += phys_pp * 0.5

        test_df['pred_power'] = np.maximum(final_pred_kwh, 0.0)
        test_df['pred_peak'] = np.maximum(final_pred_peak, 0.0)
        
        if local_reports:
            try:
                for r_info in local_reports:
                    if isinstance(r_info, dict) and r_info.get('status') == '확인':
                        r_sub = r_info.get('substation', '')
                        is_match = False
                        if station == '전체' and r_sub != '종합청사': is_match = True
                        elif station in LINE_STATIONS and r_sub in LINE_STATIONS[station]: is_match = True
                        elif station == r_sub: is_match = True

                        if is_match:
                            app_date = pd.to_datetime(r_info.get('applyDate', f"{target_y}-01-01"))
                            kw_val = float(r_info.get('kw', 0))
                            hours_val = float(r_info.get('hours', 0))
                            daily_kwh = kw_val * hours_val
                            peak_delta = kw_val
                            if '철거' in str(r_info.get('type', '')) or '-' in str(r_info.get('type', '')):
                                daily_kwh = -daily_kwh
                                peak_delta = -peak_delta
                            mask_after = (test_df['date'] >= app_date)
                            test_df.loc[mask_after, 'pred_power'] = np.maximum(test_df.loc[mask_after, 'pred_power'] + daily_kwh, 0.0)
                            test_df.loc[mask_after, 'pred_peak'] = np.maximum(test_df.loc[mask_after, 'pred_peak'] + peak_delta, 0.0)
            except Exception as parse_err:
                logger.warning(f"부하증감 신고 데이터 반영 중 오류: {parse_err}")

        train_pred = model_kwh.predict(X_train)
        r2_acc = float(round(r2_score(y_train_kwh, train_pred) * 100, 1))
        
        lt = float(train_last_year_df['raw_power'].sum()) if not train_last_year_df.empty else 0.0
        ft = float(test_df['pred_power'].sum())
        
        last_peak_val = float(train_last_year_df['raw_peak'].max()) if not train_last_year_df.empty else 0.0
        peak_future_val = float(test_df['pred_peak'].max())
        
        # 🌟 예측 변수 중요도 산출 (종합청사는 '승객수' 그룹 제외)
        groups_map = {
            '기온': ['temp_max', 'temp_min', 'temp_avg', 'cdd', 'hdd', 'trop_night_deg'],
            '계절(월)': ['month'],
            '주말': ['is_weekend', 'dayofweek'],
            '공휴일': ['is_holiday'],
            '습도': ['humidity'],
            '초미세먼지(PM2.5)': ['pm25', 'is_pm25_bad']
        }
        if not is_hq:
            groups_map['승객수'] = ['passengers']

        rng = np.random.RandomState(42)
        group_impacts = {}
        for g_name, g_cols in groups_map.items():
            X_perm = X_train.copy()
            perm_idx = rng.permutation(len(X_perm))
            for col_c in g_cols:
                X_perm[col_c] = X_train[col_c].values[perm_idx]
            perm_pred = model_kwh.predict(X_perm)
            group_impacts[g_name] = float(np.mean(np.abs(train_pred - perm_pred)))

        total_impact = sum(group_impacts.values()) or 1.0
        top_feats = [
            {"name": g_name, "value": round((val / total_impact) * 100.0, 1)}
            for g_name, val in sorted(group_impacts.items(), key=lambda x: x[1], reverse=True)
        ]
        
        records = []
        for m in range(1, 13):
            m_past = float(train_last_year_df[train_last_year_df['date'].dt.month == m]['raw_power'].sum()) if not train_last_year_df.empty else 0.0
            m_pred = float(test_df[test_df['date'].dt.month == m]['pred_power'].sum())
            records.append({ "month": f"{m}월", "past_kwh": m_past, "pred_kwh": m_pred })
            
        res = {
            "summary": { "last_tot": lt, "tot_future": ft, "last_peak": last_peak_val, "peak_future": peak_future_val, "acc": r2_acc }, 
            "chart_data": records, "feat_data": top_feats
        }
        set_bounded_api_cache("predict", cache_key, res)
        gc.collect()
        return res
    except Exception as e:
        return {"error": f"서버 내부 오류로 예측에 실패했습니다: {str(e)}\n\n{traceback.format_exc()}"}
    
@app.get("/api/bill/{station}")
def get_bill_data(station: str, year: str):
    cache_key = f"{station}_{year}"
    if cache_key in GLOBAL_API_CACHE["bill"]:
        return GLOBAL_API_CACHE["bill"][cache_key]

    if station in ['전체', '2호선', '3호선']:
        return {"error": "전기요금 조회는 개별 역사/기지 또는 1호선(통합)을 선택해야 정확한 내역을 확인할 수 있습니다. 좌측 메뉴에서 선택해 주세요."}
        
    if station == '1호선': target_cust = '0526314773'
    else: target_cust = STATION_CUST_MAP.get(station)
        
    if not target_cust:
        return {"error": f"[{station}]의 한전 고객번호 매핑 정보를 찾을 수 없습니다."}
        
    reset_kepco_cooldown()
    url = KEPCO_PROXY_URL
    
    def fetch_month_bill(m):
        if time.time() < KEPCO_COOLDOWN_UNTIL:
            return None
        data_month = f"{year}{m:02d}"
        params = {"type": "bill", "custNo": target_cust, "dataMonth": data_month, "serviceKey": KEPCO_API_KEY}
        req_ok = False
        for attempt in range(2):
            if time.time() < KEPCO_COOLDOWN_UNTIL:
                return None
            try:
                res = http_session.get(url, params=params, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict) and not data.get("errCd") and not data.get("error"):
                        req_ok = True
                        record_kepco_result(True)
                        if "custBillDataInfoList" in data and data["custBillDataInfoList"]:
                            item = data["custBillDataInfoList"][0]
                            def parse_float(val):
                                try:
                                    if isinstance(val, str): val = val.replace(',', '').strip()
                                    return float(val)
                                except: return 0.0
                            
                            lower_item = {k.lower(): v for k, v in item.items()}
                            return {
                                "bill_ym": str(lower_item.get("billym", lower_item.get("bill_ym", ""))),
                                "mr_ymd": str(lower_item.get("mrymd", lower_item.get("mr_ymd", ""))),
                                "bill_aply_pwr": parse_float(lower_item.get("billaplypwr", lower_item.get("bill_aply_pwr"))),
                                "base_bill": parse_float(lower_item.get("basebill", lower_item.get("base_bill"))),
                                "kwh_bill": parse_float(lower_item.get("kwhbill", lower_item.get("kwh_bill"))),
                                "dc_bill": parse_float(lower_item.get("dcbill", lower_item.get("dc_bill"))),
                                "req_bill": parse_float(lower_item.get("reqbill", lower_item.get("req_bill"))),
                                "req_amt": parse_float(lower_item.get("reqamt", lower_item.get("req_amt"))),
                                "lload_usekwh": parse_float(lower_item.get("lloadusekwh", lower_item.get("lload_usekwh"))),
                                "mload_usekwh": parse_float(lower_item.get("mloadusekwh", lower_item.get("mload_usekwh"))),
                                "maxload_usekwh": parse_float(lower_item.get("maxloadusekwh", lower_item.get("maxload_usekwh"))),
                                "lload_needle": parse_float(lower_item.get("lloadneedle", lower_item.get("lload_needle"))),
                                "mload_needle": parse_float(lower_item.get("mloadneedle", lower_item.get("mload_needle"))),
                                "maxload_needle": parse_float(lower_item.get("maxloadneedle", lower_item.get("maxload_needle"))),
                                "ji_pwrfact": parse_float(lower_item.get("jipwrfact", lower_item.get("ji_pwrfact"))),
                                "jn_pwrfact": parse_float(lower_item.get("jnpwrfact", lower_item.get("jn_pwrfact")))
                            }
                        return None
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.4)
        if not req_ok:
            record_kepco_result(False)
        return None

    with ThreadPoolExecutor(max_workers=3) as exec:
        raw_results = list(exec.map(fetch_month_bill, range(1, 13)))
        
    records = [r for r in raw_results if r is not None]
    res = {"station_name": station, "cust_no": target_cust, "records": records}
    if records:
        set_bounded_api_cache("bill", cache_key, res)
    return res

@app.get("/api/overview")
def get_overview_data(bypass: str = "false"):
    is_bypass = bypass.lower() == "true"
    overview_cache = GLOBAL_API_CACHE.setdefault("overview", {})
    if not is_bypass and "all" in overview_cache:
        c_time, c_res = overview_cache["all"]
        if time.time() - c_time < 300:
            return c_res
    if is_bypass:
        reset_kepco_cooldown()    

    now_kst = get_kst_now()
    today_str = now_kst.strftime("%Y-%m-%d")
    yesterday_str = (now_kst - timedelta(days=1)).strftime("%Y-%m-%d")

    # 🌟 1) 16개 고유 한전 고객번호 캐시를 먼저 일괄 적재하여 24개 개소 개별 조회 시 중복 호출 방지
    get_kepco_data_for_station("전체", today_str, is_bypass)
    get_kepco_data_for_station("종합청사", today_str, is_bypass)
    get_kepco_data_for_station("전체", yesterday_str, False)
    get_kepco_data_for_station("종합청사", yesterday_str, False)

    all_stations = (
        [("1호선", s) for s in LINE_STATIONS["1호선"]] +
        [("2호선", s) for s in LINE_STATIONS["2호선"]] +
        [("3호선", s) for s in LINE_STATIONS["3호선"]] +
        [("종합청사", "종합청사")]
    )

    items = []
    counts = {"danger": 0, "warning": 0, "normal": 0}

    for line_name, st in all_stations:
        t_usage, t_peak, t_details = get_kepco_data_for_station(st, today_str, False)
        y_usage, y_peak, y_details = get_kepco_data_for_station(st, yesterday_str, False)

        # 금일 실제 집계가 들어온 마지막 15분 구간 인덱스 확인 (전일 동시간대 비교용)
        valid_indices = [idx for idx, d in enumerate(t_details) if d.get("usage_kwh", 0) > 0]
        if valid_indices:
            last_idx = max(valid_indices)
            last_time = t_details[last_idx]["time"]
            peak_item = max(t_details[:last_idx + 1], key=lambda x: x.get("peak_kw", 0))
            peak_time = peak_item["time"] if peak_item.get("peak_kw", 0) > 0 else "--:--"
            y_same_kwh = sum(d.get("usage_kwh", 0.0) for d in y_details[:last_idx + 1]) if y_details else 0.0
        else:
            last_idx = -1
            last_time = "집계대기"
            peak_time = "--:--"
            y_same_kwh = 0.0

        dod_pct = round(((t_usage - y_same_kwh) / y_same_kwh) * 100, 1) if y_same_kwh > 0 else 0.0

        # 임계치 설정 시 설정값 사용, 미설정 시 전일 최대피크의 110%를 임시 기준선으로 적용
        custom_limit = float(THRESHOLDS.get(st, 0.0))
        ref_limit = custom_limit if custom_limit > 0 else round(y_peak * 1.1, 1)
        is_custom_limit = custom_limit > 0

        peak_ratio = round((t_peak / ref_limit) * 100, 1) if ref_limit > 0 else 0.0

        # 신호등 판정: 🔴위험(피크 95% 이상) / 🟡주의(피크 80% 이상 또는 전일 동시간 대비 +10% 이상 급증) / 🟢정상
        if ref_limit > 0 and peak_ratio >= 95.0:
            status = "danger"
            counts["danger"] += 1
        elif (ref_limit > 0 and peak_ratio >= 80.0) or dod_pct >= 10.0:
            status = "warning"
            counts["warning"] += 1
        else:
            status = "normal"
            counts["normal"] += 1

        items.append({
            "line": line_name,
            "station": st,
            "status": status,
            "today_kwh": round(t_usage, 1),
            "yest_same_kwh": round(y_same_kwh, 1),
            "dod_pct": dod_pct,
            "today_peak": round(t_peak, 1),
            "yest_peak": round(y_peak, 1),
            "peak_time": peak_time,
            "last_time": last_time,
            "limit_kw": round(ref_limit, 1),
            "is_custom_limit": is_custom_limit,
            "peak_ratio": peak_ratio
        })

    res = {
        "updated_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "items": items
    }
    overview_cache["all"] = (time.time(), res)
    return res

@app.get("/api/backup")
def export_master_backup():
    file_path = "uploaded_dataset.xlsx"
    if not os.path.exists(file_path):
        return {"error": "기본이 될 과거 데이터베이스(Excel)가 업로드되어 있지 않습니다."}

    try:
        xls_dict = pd.read_excel(file_path, sheet_name=None)
        sheet_names = list(xls_dict.keys())
        main_sheet_name = sheet_names[0] 
        df = xls_dict[main_sheet_name]
        df = df.loc[:, ~df.columns.astype(str).str.startswith('Unnamed')].copy()

        date_col = next((c for c in df.columns if 'date' in str(c).lower() or '일자' in str(c)), 'date')
        df[date_col] = pd.to_datetime(df[date_col]).dt.normalize()
        df = df.dropna(subset=[date_col])

        yesterday = (get_kst_now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        
        start_check_date = pd.to_datetime("2026-09-05")
        existing_dates = set(df[date_col].tolist())
        
        col_map = {}
        for c in df.columns:
            for st in STATION_CUST_MAP.keys():
                if st != '전체' and st in str(c):
                    if 'total_kwh' in str(c): col_map.setdefault(st, {})['kwh'] = c
                    if 'peak_kw' in str(c): col_map.setdefault(st, {})['peak'] = c

        total_days = (yesterday - start_check_date).days + 1
        all_stations = [s for s in STATION_CUST_MAP.keys() if s != '전체']
        recent_recheck_dt = yesterday - timedelta(days=2)  # 최근 3일(어제·그제·사흘전)은 지연 집계 반영을 위해 재확인

        # 🌟 1) 0 이하/빈칸인 경우, 2) 최근 3일 이내인 경우, 3) 부분 집계 의심(사용량 < 피크*6)인 경우를 모두 찾아냄
        missing_targets = {}
        for i in range(total_days):
            target_dt = start_check_date + timedelta(days=i)
            if target_dt not in existing_dates:
                missing_targets[target_dt] = list(all_stations)
            else:
                row_df = df.loc[df[date_col] == target_dt]
                stns_to_fetch = []
                for st in all_stations:
                    kwh_col = col_map.get(st, {}).get('kwh')
                    peak_col = col_map.get(st, {}).get('peak')
                    need_fetch = False
                    cur_k, cur_p = 0.0, 0.0

                    if kwh_col:
                        v_k = row_df[kwh_col].values
                        if len(v_k) == 0 or pd.isna(v_k[0]) or float(v_k[0]) <= 0:
                            need_fetch = True
                        else:
                            cur_k = float(v_k[0])
                    if peak_col:
                        v_p = row_df[peak_col].values
                        if len(v_p) == 0 or pd.isna(v_p[0]) or float(v_p[0]) <= 0:
                            need_fetch = True
                        else:
                            cur_p = float(v_p[0])

                    # 🌟 0은 아니지만 하루 96개 구간 중 일부만 들어온 의심 값(사용량이 피크의 6배 미만)이거나 최근 3일 데이터면 재확인
                    if not need_fetch:
                        if target_dt >= recent_recheck_dt:
                            need_fetch = True
                        elif cur_p > 0 and (cur_k / cur_p) < 6.0:
                            need_fetch = True

                    if need_fetch:
                        stns_to_fetch.append(st)
                if stns_to_fetch:
                    missing_targets[target_dt] = stns_to_fetch

        target_dt_list = sorted(missing_targets.keys())[:120]

        new_rows = []
        for target_dt in target_dt_list:
            d_str = target_dt.strftime("%Y-%m-%d")
            is_existing_row = target_dt in existing_dates
            stns_needed = missing_targets[target_dt]
            row = {date_col: target_dt}
            
            def fetch_st(st):
                usage, peak, _ = get_kepco_data_for_station(st, d_str, is_bypass=True, save_immediately=False)
                return st, usage, peak
                
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(fetch_st, stns_needed))
                
            if is_existing_row:
                row_idx = df[df[date_col] == target_dt].index[0]
                for st, usage, peak in results:
                    if st in col_map:
                        if 'kwh' in col_map[st] and usage > 0:
                            old_k = df.loc[row_idx, col_map[st]['kwh']]
                            # 🌟 기존 값이 비어있거나 0이거나, 새로 받아온 완전 집계량이 기존 부분 집계량보다 클 때만 덮어씀
                            if pd.isna(old_k) or float(old_k) <= 0 or usage > float(old_k):
                                df.loc[row_idx, col_map[st]['kwh']] = usage
                        if 'peak' in col_map[st] and peak > 0:
                            old_p = df.loc[row_idx, col_map[st]['peak']]
                            if pd.isna(old_p) or float(old_p) <= 0 or peak > float(old_p):
                                df.loc[row_idx, col_map[st]['peak']] = peak
            else:
                for st, usage, peak in results:
                    if st in col_map:
                        if 'kwh' in col_map[st]:
                            row[col_map[st]['kwh']] = usage if usage > 0 else np.nan
                        if 'peak' in col_map[st]:
                            row[col_map[st]['peak']] = peak if peak > 0 else np.nan
                new_rows.append(row)
                
        flush_pending_kepco_updates(min_batch=1)

        if new_rows:
            df_new = pd.DataFrame(new_rows)
            df = pd.concat([df, df_new], ignore_index=True)

        df.sort_values(by=date_col, inplace=True)
        df.drop_duplicates(subset=[date_col], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)

        holi_col = next((c for c in df.columns if '휴일' in str(c) or 'is_holiday' in str(c)), '휴일\n(is_holiday)')
        kr_holidays = holidays.KR()
        
        df[holi_col] = df[date_col].apply(
            lambda x: 1 if pd.notna(x) and (x.dayofweek >= 5 or x.strftime('%Y-%m-%d') in kr_holidays) else 0
        )

        if df[date_col].dt.tz is not None: df[date_col] = df[date_col].dt.tz_localize(None)
        xls_dict[main_sheet_name] = df

        # 🌟 [초미세먼지 통합 및 누적] 연도별 탭들을 '초미세먼지' 단일 시트 1개로 합치고 어제 날짜까지 자동 누적
        pm_dfs = []
        pm_stn_order = []
        for s_name in list(xls_dict.keys()):
            if '초미세먼지' in s_name or '미세먼지' in s_name:
                df_c, _, s_cols = normalize_pm25_df(xls_dict[s_name])
                if not df_c.empty:
                    pm_dfs.append(df_c)
                    for sc in s_cols:
                        if sc not in pm_stn_order:
                            pm_stn_order.append(sc)
                del xls_dict[s_name]  # 기존 연도별 미세먼지 탭 삭제

        for stn in TARGET_AIR_STNS:
            if stn not in pm_stn_order:
                pm_stn_order.append(stn)

        if pm_dfs:
            df_pm_old = pd.concat(pm_dfs, ignore_index=True)
            df_pm_old.sort_values(by='일자', inplace=True)
            df_pm_old.drop_duplicates(subset=['일자'], keep='last', inplace=True)
        else:
            df_pm_old = pd.DataFrame(columns=['일자'] + pm_stn_order)
# 🌟 1) 마스터 백업 실행 시 최근 7일 구간은 환경공단 API를 강제 재조회(is_bypass=True)하여 가집계/오류값을 최신 확정값으로 갱신
        recent_7d_str = max("2026-09-05", (yesterday - timedelta(days=7)).strftime("%Y-%m-%d"))
        with ThreadPoolExecutor(max_workers=5) as air_exec:
            for stn in TARGET_AIR_STNS:
                air_exec.submit(fetch_airkorea_station_daily, stn, recent_7d_str, yesterday_str, True, False)

        with AIR_FETCH_LOCK:
            if PENDING_AIR_UPDATES:
                save_airkorea_cache_to_firebase(dict(PENDING_AIR_UPDATES))
                PENDING_AIR_UPDATES.clear()

        pm_cache_rows = []
        for i in range(total_days):
            t_dt = start_check_date + timedelta(days=i)
            d_str = t_dt.strftime("%Y-%m-%d")
            r_dict = {'일자': t_dt}
            for stn in pm_stn_order:
                c_val = GLOBAL_AIRKOREA_CACHE.get(f"AIR_{stn}_{d_str}")
                # 0 이하의 비정상 값은 제외하고 유효한 양수만 반영
                r_dict[stn] = int(round(float(c_val))) if (is_valid_value(c_val) and float(c_val) > 0) else np.nan
            pm_cache_rows.append(r_dict)

        df_pm_cache = pd.DataFrame(pm_cache_rows)
        if not df_pm_old.empty:
            # 기존 엑셀에 혹시 0 이하로 잘못 들어간 값이 있으면 NaN으로 비워 복구 유도
            for c in pm_stn_order:
                if c in df_pm_old.columns:
                    df_pm_old.loc[df_pm_old[c] <= 0, c] = np.nan

            # 🌟 2) 14일 이전 과거 확정 데이터는 기존 엑셀(df_pm_old)을 보호하고, 최근 14일 이내는 새로 갱신된 캐시(df_pm_cache)로 덮어써서 오류값 자동 복구
            recent_14d_dt = yesterday - timedelta(days=14)
            old_idx = df_pm_old.set_index('일자')
            cache_idx = df_pm_cache.set_index('일자')

            merged_past = old_idx[old_idx.index < recent_14d_dt].combine_first(cache_idx[cache_idx.index < recent_14d_dt])
            merged_recent = cache_idx[cache_idx.index >= recent_14d_dt].combine_first(old_idx[old_idx.index >= recent_14d_dt])
            df_pm_merged = pd.concat([merged_past, merged_recent]).reset_index()
        else:
            df_pm_merged = df_pm_cache

        final_pm_cols = ['일자'] + [c for c in pm_stn_order if c in df_pm_merged.columns]
        df_pm_merged = df_pm_merged[final_pm_cols].sort_values(by='일자').reset_index(drop=True)
        xls_dict["초미세먼지"] = df_pm_merged

        # 🌟 [기상청 ASOS & 동네 AWS 백업] 선형 보간 없이 실측값만 저장하고 결측치는 '--'로 그대로 유지
        load_excel_dataset()
        recent_7d_str = max("2026-09-05", (yesterday - timedelta(days=7)).strftime("%Y-%m-%d"))
        recent_3d_aws_str = max("2026-09-05", (yesterday - timedelta(days=2)).strftime("%Y-%m-%d"))
        fetch_asos_daily(recent_7d_str, yesterday_str, is_bypass=True)
        fetch_aws_daily_for_dashboard('143', recent_3d_aws_str, yesterday_str, is_bypass=True, is_background=True)

        asos_all = fetch_asos_daily("2023-01-01", yesterday_str)
        asos_rows = []
        for dt_obj in pd.date_range("2023-01-01", yesterday_str):
            d_s = dt_obj.strftime("%Y-%m-%d")
            v = asos_all.get(d_s, {})
            tx = v.get("tmax", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tmax", "--"))
            tn = v.get("tmin", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tmin", "--"))
            ta = v.get("tavg", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tavg", "--"))
            hm = v.get("humi", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_humi", "--"))
            asos_rows.append({
                "date": d_s,
                "tmax": float(tx) if is_valid_temp(tx) else "--",
                "tmin": float(tn) if is_valid_temp(tn) else "--",
                "tavg": float(ta) if is_valid_temp(ta) else "--",
                "humi": float(hm) if is_valid_humi(hm) else "--"
            })
        
        if asos_rows:
            xls_dict["기상청_ASOS"] = pd.DataFrame(asos_rows)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            for sheet_name, sheet_df in xls_dict.items():
                temp_df = sheet_df.copy()
                # 모든 시트에서 Unnamed 컬럼 원천 제거
                temp_df = temp_df.loc[:, ~temp_df.columns.astype(str).str.startswith('Unnamed')].copy()

                for d_c in [date_col, 'date', '일자']:
                    if d_c in temp_df.columns:
                        temp_df = temp_df.dropna(subset=[d_c])
                        if pd.api.types.is_datetime64_any_dtype(temp_df[d_c]):
                            temp_df[d_c] = temp_df[d_c].dt.strftime('%Y-%m-%d')
                
                temp_df.to_excel(writer, sheet_name=sheet_name, index=False)
            
        excel_bytes = output.getvalue()

        with open(file_path, "wb") as f:
            f.write(excel_bytes)
        backup_dataset_to_firebase(excel_bytes)
        GLOBAL_API_CACHE["compare"].clear()
        GLOBAL_API_CACHE["predict"].clear()
        load_excel_dataset()
        gc.collect()
        output.seek(0)
        return StreamingResponse(
            output, 
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=DTRO_Master_Backup_{yesterday.strftime('%Y%m%d')}.xlsx"}
        )
    except Exception as e:
        return {"error": f"백업 파일 생성 중 서버 에러 발생: {str(e)}\n{traceback.format_exc()}"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)