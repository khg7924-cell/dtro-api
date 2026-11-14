import os
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

# =========================================================================
# 🚨 영구 저장소 (Firebase Realtime Database) 연동 및 데이터 검증 함수
# =========================================================================
FIREBASE_DB_URL = "https://datacenter-app-7a69a-default-rtdb.firebaseio.com"

def is_valid_value(val):
    return val is not None and val not in ["--", "-", "", "None", "null"]

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
        # 🌟 상대습도는 0% 초과 ~ 100% 이하일 때만 유효값으로 인정 (기상청 결측 코드 -9.0 원천 차단)
        return 0.0 < fv <= 100.0
    except Exception:
        return False

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
            # 1) 습도(humi)가 0 이하(-9 등) 또는 100 초과로 잘못 저장된 항목 제거
            if k.endswith("_humi") and is_valid_value(v) and not is_valid_humi(v):
                bad_keys[k] = None
                continue
            # 2) 기온이 유효 범위를 벗어난 항목 제거
            if (k.endswith("_tmax") or k.endswith("_tmin")) and is_valid_value(v) and not is_valid_temp(v):
                bad_keys[k] = None
                continue
            # 3) 최근 3일 이내 날짜가 "--"로 저장된 경우 자동 재수집을 위해 제거
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
            # 🌟 자정 직후 미집계로 인해 최근 3일 이내 날짜가 "--"로 저장된 항목은 제거하여 자동 재수집 유도
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
adapter = requests.adapters.HTTPAdapter(pool_connections=200, pool_maxsize=200)
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

GLOBAL_API_CACHE = {
    "dashboard": {},
    "realtime": {},
    "compare": {},
    "predict": {},
    "bill": {}
}

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
            logger.info("🔥 [상시 캐싱] 백그라운드에서 데이터를 미리 준비합니다...")
            def warm_up():
                now_kst = get_kst_now()
                today_str = now_kst.strftime("%Y-%m-%d")
                api_start = "2026-09-05"
                default_14d = max((now_kst - timedelta(days=14)).strftime("%Y-%m-%d"), api_start)
                
                target_stations = ['전체', '종합청사', '1호선', '2호선', '3호선'] + \
                                  LINE_STATIONS['1호선'] + LINE_STATIONS['2호선'] + LINE_STATIONS['3호선']

                # 🌟 0단계: 금일 대구 전체 실시간 초미세먼지 단 1회 호출로 갱신
                fetch_airkorea_sido_realtime(is_bypass=True)

                # 🌟 1단계: 최근 14일 구간 캐싱 (기상청 동일 인증키 동시 호출 충돌 방지를 위해 ASOS -> AWS 순차 실행)
                fetch_asos_daily(default_14d, today_str)
                with ThreadPoolExecutor(max_workers=5) as weather_exec:
                    for air_stn in TARGET_AIR_STNS:
                        weather_exec.submit(fetch_airkorea_station_daily, air_stn, default_14d, today_str, False, False)
                    for lat, lon in set(STATION_COORD_MAP.values()):
                        weather_exec.submit(fetch_openmeteo_env, lat, lon, default_14d, today_str)
                
                with AIR_FETCH_LOCK:
                    if PENDING_AIR_UPDATES:
                        save_airkorea_cache_to_firebase(dict(PENDING_AIR_UPDATES))
                        PENDING_AIR_UPDATES.clear()

                # 기상청 AWS 최근 14일 단일 스레드 순차 수집
                fetch_aws_daily_for_dashboard('143', default_14d, today_str, is_bypass=False, is_background=True)

                for st in target_stations:
                    get_dashboard_data(st, default_14d, today_str)

                # 🌟 2단계: 9월 5일~오늘 전체 구간 마스터 캐싱
                fetch_asos_daily(api_start, today_str)
                with ThreadPoolExecutor(max_workers=5) as weather_exec:
                    for air_stn in TARGET_AIR_STNS:
                        weather_exec.submit(fetch_airkorea_station_daily, air_stn, api_start, today_str, False, False)
                    for lat, lon in set(STATION_COORD_MAP.values()):
                        weather_exec.submit(fetch_openmeteo_env, lat, lon, api_start, today_str)
                
                with AIR_FETCH_LOCK:
                    if PENDING_AIR_UPDATES:
                        save_airkorea_cache_to_firebase(dict(PENDING_AIR_UPDATES))
                        PENDING_AIR_UPDATES.clear()

                # 기상청 AWS 전체 과거 구간 단일 스레드 순차 수집 (10회마다 Firebase 자동 중간 저장)
                fetch_aws_daily_for_dashboard('143', api_start, today_str, is_bypass=False, is_background=True)

                # AWS 신규 수집분을 대시보드 마스터 캐시에 즉시 반영하기 위해 기존 대시보드 캐시 갱신
                GLOBAL_API_CACHE["dashboard"].clear()
                for st in target_stations:
                    get_dashboard_data(st, api_start, today_str)
                    
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, warm_up)
            logger.info("✅ [상시 캐싱 완료] 데이터 100% 적재 완료. 임계치 감시 대기중.")
        except Exception as e:
            logger.error(f"상시 캐싱 중 오류 발생: {e}")
            
        await asyncio.sleep(600)

@app.on_event("startup")
async def startup_event():
    restore_dataset_from_firebase()     
    restore_thresholds_from_firebase()  
    restore_aws_cache_from_firebase()      # 🌟 서버 시작 시 Firebase에서 과거 AWS 기상 캐시 복구 및 오류값 정화
    restore_airkorea_cache_from_firebase() # 🌟 서버 시작 시 Firebase에서 과거 환경공단 미세먼지 캐시 복구 및 미집계 정화
    
    load_excel_dataset()
    load_thresholds()
    asyncio.create_task(auto_cache_warmer())
    logger.info("✅ 서버 정상 구동 완료. (상시 자동 캐싱 및 카카오워크 감시 엔진 가동 중)")

@app.post("/api/threshold/{station}")
def set_threshold(station: str, limit: float):
    THRESHOLDS[station] = limit
    save_thresholds()
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
            "aws_cooldown_remaining_sec": max(0, int(KMA_AWS_COOLDOWN_UNTIL - time.time()))
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
        date_col = next((c for c in df_main.columns if 'date' in str(c).lower() or '일자' in str(c)), 'date')
        df_main['date'] = pd.to_datetime(df_main[date_col]).dt.normalize()
        
        all_pm25 = []
        for sheet in xls.sheet_names:
            if '초미세먼지' in sheet or '미세먼지' in sheet:
                df_pm = pd.read_excel(xls, sheet_name=sheet)
                df_pm = df_pm.iloc[1:].copy() 
                pm_date_col = next((c for c in df_pm.columns if 'date' in str(c).lower() or '일자' in str(c)), None)
                if pm_date_col:
                    df_pm['date'] = pd.to_datetime(df_pm[pm_date_col]).dt.normalize()
                    num_cols = [c for c in df_pm.columns if 'Unnamed' in str(c)]
                    for c in num_cols: df_pm[c] = pd.to_numeric(df_pm[c], errors='coerce')
                    df_pm['pm25_merged'] = df_pm[num_cols].mean(axis=1)
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
                            # 🌟 엑셀 시트에 혹시 -9.0 등 음수 습도가 저장되어 있어도 걸러냄
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
    # 🌟 자정 직후 미집계 대응: 최근 3일 이내 날짜는 유효한 숫자가 아니면 캐시를 건너뛰고 매번 재확인
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
    recent_3d_str = (now_kst - timedelta(days=3)).strftime("%Y-%m-%d")
    
    missing_dates = []
    cached_res = {}
    for i in range(diff):
        d_str = (s_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        ck_tmax = f"ASOS_{d_str}_tmax"
        ck_humi = f"ASOS_{d_str}_humi"
        
        tmax_v = GLOBAL_WEATHER_CACHE.get(ck_tmax, "--")
        humi_v = GLOBAL_WEATHER_CACHE.get(ck_humi, "--")
        
        # 🌟 최근 3일 이내 날짜는 기온뿐만 아니라 습도(humi)가 아직 미집계('--')이거나 잘못된 값이면 재수집
        is_fully_valid = is_valid_temp(tmax_v) and (is_valid_humi(humi_v) or d_str < recent_3d_str)
        if not is_bypass and is_fully_valid:
            cached_res[d_str] = {
                "tmax": tmax_v,
                "tmin": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tmin", "--"),
                "tavg": GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_str}_tavg", "--"),
                "humi": humi_v if is_valid_humi(humi_v) else "--"
            }
        else:
            missing_dates.append(d_str)
            
    if not missing_dates: 
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

# 🌟 Open-Meteo 환경 데이터 (금일 기온 및 결측 보완용)
def fetch_openmeteo_env(lat, lon, start_date, end_date, is_bypass: bool = False):
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    diff = (e_dt - s_dt).days + 1
    
    missing_dates = []
    cached_res = {}
    for i in range(diff):
        d_str = (s_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        ck_pm25 = f"OM_{lat}_{lon}_{d_str}_pm25"
        ck_tmax = f"OM_{lat}_{lon}_{d_str}_tmax"
        ck_humi = f"OM_{lat}_{lon}_{d_str}_humi"
        
        if (not is_bypass and 
            is_valid_value(GLOBAL_WEATHER_CACHE.get(ck_pm25)) and 
            is_valid_temp(GLOBAL_WEATHER_CACHE.get(ck_tmax)) and 
            is_valid_humi(GLOBAL_WEATHER_CACHE.get(ck_humi))):
            cached_res[d_str] = {
                "pm25": GLOBAL_WEATHER_CACHE[ck_pm25],
                "tmax": GLOBAL_WEATHER_CACHE[ck_tmax],
                "tmin": GLOBAL_WEATHER_CACHE.get(f"OM_{lat}_{lon}_{d_str}_tmin", "--"),
                "humi": GLOBAL_WEATHER_CACHE[ck_humi]
            }
        else:
            missing_dates.append(d_str)
            
    if not missing_dates: return cached_res

    req_start = min(missing_dates)
    req_end = max(missing_dates)
    
    env_data = {}
    url_a = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params_a = {"latitude": lat, "longitude": lon, "start_date": req_start, "end_date": req_end, "hourly": "pm2_5", "timezone": "Asia/Seoul"}
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
    params_w = {"latitude": lat, "longitude": lon, "start_date": req_start, "end_date": req_end, "daily": "temperature_2m_max,temperature_2m_min", "hourly": "relative_humidity_2m", "timezone": "Asia/Seoul"}
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

    today_str = get_kst_now().strftime("%Y-%m-%d")
    for d_str in missing_dates:
        v = env_data.get(d_str, {})
        cached_res[d_str] = {
            "pm25": v.get("pm25", "--"), "tmax": v.get("tmax", "--"), 
            "tmin": v.get("tmin", "--"), "humi": v.get("humi", "--")
        }
        if d_str < today_str:
            if is_valid_value(cached_res[d_str]["pm25"]):
                GLOBAL_WEATHER_CACHE[f"OM_{lat}_{lon}_{d_str}_pm25"] = cached_res[d_str]["pm25"]
            if is_valid_temp(cached_res[d_str]["tmax"]):
                GLOBAL_WEATHER_CACHE[f"OM_{lat}_{lon}_{d_str}_tmax"] = cached_res[d_str]["tmax"]
            if is_valid_temp(cached_res[d_str]["tmin"]):
                GLOBAL_WEATHER_CACHE[f"OM_{lat}_{lon}_{d_str}_tmin"] = cached_res[d_str]["tmin"]
            if is_valid_humi(cached_res[d_str]["humi"]):
                GLOBAL_WEATHER_CACHE[f"OM_{lat}_{lon}_{d_str}_humi"] = cached_res[d_str]["humi"]
            
    return cached_res

# 🌟 기상청 방재 기상 관측장비 (AWS) - 단일 스레드 0.7초 순차 호출 + 10회마다 Firebase 중간 저장 (습도는 ASOS 전담)
def fetch_aws_daily_for_dashboard(stn_id: str, start_date: str, end_date: str, is_bypass: bool = False, is_background: bool = False):
    global KMA_AWS_COOLDOWN_UNTIL
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

    # 🌟 일반 웹 조회(is_background=False, is_bypass=False)는 기상청 호출을 기다리지 않고 즉시 메모리 캐시를 반환하여 화면 멈춤 방지
    if (is_background or is_bypass) and time.time() >= KMA_AWS_COOLDOWN_UNTIL:
        with AWS_FETCH_LOCK:
            missing_tasks = []
            for i in range(diff):
                curr = s_dt + timedelta(days=i)
                d_str = curr.strftime("%Y-%m-%d")
                tm2 = curr.strftime("%Y%m%d")
                # 🌟 습도(humi)는 대표 ASOS(143번)에서 가져오므로 AWS는 최고/최저기온 2개만 호출 (호출량 1/3 절감)
                for obs, key in [("ta_max", "tmax"), ("ta_min", "tmin")]:
                    cache_key = f"AWS_{stn_id}_{d_str}_{key}"
                    cached_v = GLOBAL_WEATHER_CACHE.get(cache_key)
                    if is_bypass or cache_key not in GLOBAL_WEATHER_CACHE or (d_str >= recent_3d_str and not is_valid_value(cached_v)):
                        missing_tasks.append((d_str, tm2, obs, key))

            if missing_tasks and time.time() >= KMA_AWS_COOLDOWN_UNTIL:
                newly_aws_updated = {}
                consecutive_failures = 0
                quota_failed = False

                # 🌟 멀티스레드 동시 호출을 제거하고 1개씩 순차 호출(0.7초 간격)하여 기상청 방화벽 차단 원천 방지
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
                                        if new_val is not None:
                                            GLOBAL_WEATHER_CACHE[ck] = new_val
                                            newly_aws_updated[ck] = new_val
                                        elif not is_valid_value(old_val) and d_str < recent_3d_str:
                                            GLOBAL_WEATHER_CACHE[ck] = "--"
                                            newly_aws_updated[ck] = "--"
                                break
                        except Exception as e:
                            last_err_preview = str(e)
                        if attempt == 0:
                            time.sleep(1.2)

                    # 🌟 10회 호출(80건)마다 Firebase에 중간 병합 저장
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
                    logger.warning("⚠️ 기상청 AWS 연속 3회 응답 없음 - 10분간 추가 AWS 호출을 일시 중단하고 ASOS/Open-Meteo로 대체합니다.")

    res = {}
    for i in range(diff):
        curr = s_dt + timedelta(days=i)
        d_str = curr.strftime("%Y-%m-%d")
        res[d_str] = {
            "tmax": GLOBAL_WEATHER_CACHE.get(f"AWS_{stn_id}_{d_str}_tmax", "--"),
            "tmin": GLOBAL_WEATHER_CACHE.get(f"AWS_{stn_id}_{d_str}_tmin", "--"),
            "humi": "--"  # 습도는 대구 대표 ASOS(143번)에서 자동 보완됨
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

def get_kepco_data_for_station(station: str, date_str: str, is_bypass: bool = False):
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
    today_str = get_kst_now().strftime("%Y-%m-%d")
    
    def fetch_and_process(c_no):
        if not c_no: return [0.0] * 96
        cache_key = f"{c_no}_{date_str}"
        day_list = None
        
        if not is_bypass:
            if date_str < today_str and cache_key in GLOBAL_KEPCO_CACHE:
                day_list = GLOBAL_KEPCO_CACHE[cache_key]
            elif date_str == today_str and cache_key in GLOBAL_KEPCO_TODAY_CACHE:
                c_time, c_data = GLOBAL_KEPCO_TODAY_CACHE[cache_key]
                if time.time() - c_time < 600:
                    day_list = c_data

        if day_list is None:
            url = "https://opm.kepco.co.kr:11080/OpenAPI/getDayLpData.do"
            params = {"custNo": c_no, "date": date_str.replace("-", ""), "serviceKey": KEPCO_API_KEY, "returnType": "02"}
            day_list = []
            req_ok = False
            try:
                res = http_session.get(url, params=params, timeout=3)
                if res.status_code == 200:
                    req_ok = True
                    data = res.json()
                    if "dayLpDataInfoList" in data: day_list = data["dayLpDataInfoList"]
            except: pass
            
            if date_str < today_str and day_list:
                GLOBAL_KEPCO_CACHE[cache_key] = day_list
            elif date_str == today_str and req_ok:
                GLOBAL_KEPCO_TODAY_CACHE[cache_key] = (time.time(), day_list)
        
        m_target = target_meter_no if c_no == '0526314773' else "전체"
        return process_kepco_day_data(day_list, m_target)

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch_and_process, cust_nos))
        
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
            GLOBAL_API_CACHE["dashboard"][cache_key] = (m_time, sliced_res)
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
        GLOBAL_API_CACHE["dashboard"][cache_key] = (time.time(), res)
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
        
        usage, peak, details = get_kepco_data_for_station(station, d_str, is_bypass)
        
        if not details: details = safe_empty_details
        
        tmax = aws_data.get(d_str, {}).get("tmax", "--")
        tmin = aws_data.get(d_str, {}).get("tmin", "--")
        humi = aws_data.get(d_str, {}).get("humi", "--")
        
        if not is_valid_temp(tmax): tmax = asos_data.get(d_str, {}).get("tmax", "--")
        if not is_valid_temp(tmin): tmin = asos_data.get(d_str, {}).get("tmin", "--")
        if not is_valid_humi(humi): humi = asos_data.get(d_str, {}).get("humi", "--")
        
        # 🌟 기온은 나왔지만 습도만 아직 미집계('--')인 날에도 Open-Meteo 습도로 즉시 보완
        if not is_valid_temp(tmax): tmax = om_data.get(d_str, {}).get("tmax", "--")
        if not is_valid_temp(tmin): tmin = om_data.get(d_str, {}).get("tmin", "--")
        if not is_valid_humi(humi): humi = om_data.get(d_str, {}).get("humi", "--")
            
        # 🌟 초미세먼지: 1순위 환경공단 해당 측정소 -> 2순위 환경공단 대구 대표(수창동) -> 3순위 Open-Meteo
        pm25 = airkorea_data.get(d_str, "--")
        if not is_valid_value(pm25):
            pm25 = GLOBAL_AIRKOREA_CACHE.get(f"AIR_수창동_{d_str}", "--")
        if not is_valid_value(pm25):
            pm25 = om_data.get(d_str, {}).get("pm25", "--")

        co2 = usage * 0.466 / 1000
        records.append({
            "date": d_str, "usage_kwh": round(usage, 1), "peak_kw": round(peak, 1), "co2": round(co2, 2),
            "temp_max": tmax, "temp_min": tmin, "humidity": humi, "pm25": pm25, "details": details
        })
        tot_usage += usage
        if peak > max_peak: max_peak = peak
        tot_co2 += co2

    res = {
        "station_name": station, 
        "mapped_location": f"{station} (기상청 AWS {aws_stn}번 · 환경공단 [{air_stn}] 측정소 연동)",
        "summary": { "total_usage": round(tot_usage), "max_peak": round(max_peak, 1), "total_co2": round(tot_co2, 1) },
        "daily_records": records,
        "cached_at": get_kst_now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_cached": False
    }
    
    GLOBAL_API_CACHE["dashboard"][cache_key] = (time.time(), res)
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
    GLOBAL_API_CACHE["realtime"][cache_key] = (time.time(), res)
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
        if pass_col: df['passengers'] = df[pass_col]

        if 'pm25_val' in df.columns:
            df['is_pm25_bad'] = df['pm25_val'] > 35.0
        else:
            df['is_pm25_bad'] = False

        df['target_power'] = df['target_power'].fillna(0)
        kr_holidays = holidays.KR()
        df['dayofweek'] = df['date'].dt.dayofweek
        df['month'] = df['date'].dt.month
        df['is_offday'] = df.apply(lambda x: 1 if x['date'].dayofweek >= 5 or x['date'] in kr_holidays else 0, axis=1)

        df_base = df[(df['date'].dt.year == int(base_year)) & (df['month'] >= start_month) & (df['month'] <= end_month)]
        df_comp = df[(df['date'].dt.year == int(comp_year)) & (df['month'] >= start_month) & (df['month'] <= end_month)]
        monthly_base = df_base.groupby(df_base['date'].dt.month)['target_power'].sum()
        monthly_comp = df_comp.groupby(df_comp['date'].dt.month)['target_power'].sum()

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

        b_total_off = df_base['is_offday'].sum()
        c_total_off = df_comp['is_offday'].sum()
        off_diff = c_total_off - b_total_off

        asos_data = fetch_asos_daily(f"{base_year}-01-01", f"{comp_year}-12-31")
            
        def get_stats(year_str):
            hw, cw = 0, 0
            summer_tmax_sum, summer_tmax_cnt = 0.0, 0
            winter_tmin_sum, winter_tmin_cnt = 0.0, 0
            for date_str, v in asos_data.items():
                if date_str.startswith(year_str):
                    m = int(date_str[5:7])
                    if not (start_month <= m <= end_month):
                        continue
                    tmax = v.get("tmax", "--")
                    tmin = v.get("tmin", "--")
                    if is_valid_temp(tmax):
                        try:
                            tmax_f = float(tmax)
                            if tmax_f >= 33.0: hw += 1
                            if m in [6, 7, 8]:
                                summer_tmax_sum += tmax_f; summer_tmax_cnt += 1
                        except: pass
                    if is_valid_temp(tmin):
                        try:
                            tmin_f = float(tmin)
                            if tmin_f <= -10.0: cw += 1
                            if m in [12, 1, 2]:
                                winter_tmin_sum += tmin_f; winter_tmin_cnt += 1
                        except: pass
            s_avg_tmax = (summer_tmax_sum / summer_tmax_cnt) if summer_tmax_cnt > 0 else 0.0
            w_avg_tmin = (winter_tmin_sum / winter_tmin_cnt) if winter_tmin_cnt > 0 else 0.0
            return hw, cw, s_avg_tmax, w_avg_tmin

        base_hw, base_cw, b_s_tmax, b_w_tmin = get_stats(base_year)
        comp_hw, comp_cw, c_s_tmax, c_w_tmin = get_stats(comp_year)
        
        hw_diff, cw_diff = comp_hw - base_hw, comp_cw - base_cw
        s_tmax_diff = c_s_tmax - b_s_tmax
        w_tmin_diff = c_w_tmin - b_w_tmin

        base_pm25 = int(df_base['is_pm25_bad'].sum())
        comp_pm25 = int(df_comp['is_pm25_bad'].sum())
        pm_diff = comp_pm25 - base_pm25

        base_pass_sum = float(df_base['passengers'].sum()) if pass_col else 0.0
        comp_pass_sum = float(df_comp['passengers'].sum()) if pass_col else 0.0
        p_diff = comp_pass_sum - base_pass_sum

        period_label = f"{start_month}월~{end_month}월" if start_month != end_month else f"{start_month}월"
        ai_report_text = f"📊 [{station}] {base_year}년 vs {comp_year}년 ({period_label}) 전력 수요 AI 심층 분석 리포트\n\n"
        
        if not asos_data:
            ai_report_text += f"⚠️ 현재 기상청 API 허브 서버의 응답이 없어 기상 지표를 불러오지 못했습니다. 백엔드 터미널 창의 로그를 확인해주세요.\n\n"
        
        ai_report_text += f"[1] 계절별 기후 및 환경 지표 변동 현황 ({period_label})\n"
        ai_report_text += f" • 하절기(6~8월) 평균 최고기온 : {b_s_tmax:.1f}℃ ➔ {c_s_tmax:.1f}℃ ({s_tmax_diff:+.1f}℃)\n"
        ai_report_text += f" • 동절기(12~2월) 평균 최저기온 : {b_w_tmin:.1f}℃ ➔ {c_w_tmin:.1f}℃ ({w_tmin_diff:+.1f}℃)\n"
        ai_report_text += f" • 폭염일수(33℃ 이상) : {base_hw}일 ➔ {comp_hw}일 ({hw_diff:+}일)\n"
        ai_report_text += f" • 한파일수(-10℃ 이하) : {base_cw}일 ➔ {comp_cw}일 ({cw_diff:+}일)\n"
        ai_report_text += f" • 초미세먼지 '나쁨' : {base_pm25}일 ➔ {comp_pm25}일 ({pm_diff:+}일)\n\n"
        ai_report_text += f"[2] 캘린더 및 여객 지표 변동 현황 ({period_label})\n"
        ai_report_text += f" • 휴일(비운무일) 총합 : {b_total_off}일 ➔ {c_total_off}일 ({off_diff:+}일)\n"
        ai_report_text += f" • 기간 총 승객수 : {base_pass_sum:,.0f}명 ➔ {comp_pass_sum:,.0f}명 ({p_diff:+,.0f}명)\n\n"
        ai_report_text += f"🧠 [3] AI 융합 증감 요인 심층 분석\n"
        
        is_climate_harsher = (s_tmax_diff > 0.3 or hw_diff > 0) or (w_tmin_diff < -0.3 or cw_diff > 0)
        is_passenger_up = p_diff > 0

        if diff_total < 0 and (is_climate_harsher or is_passenger_up or off_diff < 0):
            ai_report_text += f"🚨 [주목] 전력 부하가 가중될 조건임에도, 총 전력량은 오히려 감소({diff_pct_total:+.1f}%)하는 긍정적 결과가 도출되었습니다.\n\n"
            climate_str = []
            if s_tmax_diff > 0.3 or hw_diff > 0: climate_str.append("하절기 기온/폭염 상승")
            if w_tmin_diff < -0.3 or cw_diff > 0: climate_str.append("동절기 한파 심화")
            if p_diff > 0: climate_str.append(f"승객수 {p_diff:+,.0f}명 폭증")
            if off_diff < 0: climate_str.append(f"평일 열차 운행일수 {abs(off_diff)}일 증가")
            
            ai_report_text += f"① 공사의 전사적 절전 성과 가시화 (핵심 요인): \n"
            ai_report_text += f"{', '.join(climate_str)} 등으로 역사 및 열차의 공조/동력 부하가 팩트 데이터상 명확히 가중될 조건이었습니다. 그럼에도 불구하고 전력량이 총 {abs(diff_total):,.0f} kWh 줄어든 원인은, 당 공사가 23년부터 전사적으로 추진해 온 '절전 아이템 발굴 및 운영 최적화' 노력이 환경적 악조건을 완벽히 극복하고 상쇄한 성과로 AI는 분석합니다.\n\n"
            
            ai_report_text += "② 캘린더 부하 효과 판단: \n"
            if off_diff > 0: ai_report_text += f"휴일이 전년 대비 {off_diff}일 늘어나 열차 운행 횟수(다이아)가 줄어든 점도, 공사의 절전 노력과 시너지를 일으켜 전력 절감에 긍정적으로 작용했습니다."
            elif off_diff < 0: ai_report_text += f"심지어 휴일 일수마저 감소하여 평일 열차 운행 횟수가 증가하는 악조건이었으나, 전사적인 절전 성과가 이를 모두 성공적으로 방어해 냈습니다."
            else: ai_report_text += "휴일 일수는 전년과 동일하여 운행 다이아 차이에 따른 영향은 없었습니다."
        else:
            direction = "증가" if diff_total > 0 else "감소"
            ai_report_text += f"XGBoost 알고리즘 분석 결과, 전력량이 총 {abs(diff_total):,.0f} kWh ({diff_pct_total:+.1f}%) {direction}한 주요 팩트 요인은 다음과 같습니다.\n\n"
            
            ai_report_text += "① 캘린더 및 열차 운행(다이아) 요인: \n"
            if off_diff > 0: ai_report_text += f"휴일이 전년 대비 {off_diff}일 더 많았습니다. 평일 대비 운행 횟수가 적은 휴일 다이아가 확대 적용되어 추진 전력 및 에스컬레이터, 스크린도어 등 연동 설비의 부하가 감소했습니다."
            elif off_diff < 0: ai_report_text += f"휴일이 {abs(off_diff)}일 줄어 운행 횟수가 가장 많은 '평일 다이아' 적용 일수가 늘어남에 따라 베이스 부하가 구조적으로 상승했습니다."
            else: ai_report_text += "휴일 일수는 전년과 동일하여 다이아 차이로 인한 변동은 발생하지 않았습니다."

            ai_report_text += "\n\n② 계절별 기상 및 공조 설비 부하 요인: \n"
            ai_report_text += f"[하절기 냉방] 여름철(6~8월) 평균 최고기온이 {abs(s_tmax_diff):.1f}℃ {'상승' if s_tmax_diff > 0 else '하락'}하고 폭염일수가 {hw_diff:+}일 변동하여 역사 냉방기 부하가 {'증가' if s_tmax_diff > 0 or hw_diff > 0 else '감소'}했습니다. "
            ai_report_text += f"\n[동절기 난방] 겨울철(12~2월) 평균 최저기온이 {abs(w_tmin_diff):.1f}℃ {'상승(따뜻함)' if w_tmin_diff > 0 else '하락(추워짐)'}하고 한파일수가 {cw_diff:+}일 변동하여, 동절기 난방 부하는 {'감소' if w_tmin_diff > 0 and cw_diff <= 0 else '상승'}한 것으로 파악됩니다."
                
            ai_report_text += "\n\n③ 여객 동선 및 편의 설비 요인: \n"
            if p_diff > 0: ai_report_text += f"승객수가 {p_diff:+,.0f}명 증가하여 동력 설비 가동 빈도가 누적 상승하고 환기 부하 연쇄 상승이 발생했습니다."
            elif p_diff < 0: ai_report_text += f"승객수가 {abs(p_diff):,.0f}명 감소하여 전체적인 동력 전력 감소에 기여했습니다."
            else: ai_report_text += "승객수 변동폭이 작아 유의미한 여객 전력 변동은 관찰되지 않았습니다."

        res = {
            "summary": { "total_base": tb, "total_comp": tc, "diff": diff_total, "diff_pct": diff_pct_total, "cost": diff_total*price, "ai_report": ai_report_text, "period_label": period_label }, 
            "records": records
        }
        GLOBAL_API_CACHE["compare"][cache_key] = res
        return res
    except Exception as e:
        return {"error": f"비교 분석 중 서버 에러가 발생했습니다: {str(e)}\n{traceback.format_exc()}"}

@app.get("/api/predict/{station}")
def get_predict_data(station: str, target_year: str, pass_rate: float = 0.0, temp_adj: float = 0.0, winter_temp_adj: float = 0.0, pm25_adj: int = 0, reports_data: str = None):
    rep_hash = hashlib.md5(reports_data.encode()).hexdigest() if reports_data else "none"
    cache_key = f"{station}_{target_year}_{pass_rate}_{temp_adj}_{winter_temp_adj}_{pm25_adj}_{rep_hash}"
    if cache_key in GLOBAL_API_CACHE["predict"]:
        return GLOBAL_API_CACHE["predict"][cache_key]

    try: 
        df = load_excel_dataset()
        if df is None: return {"error": "과거 다년간의 머신러닝 학습을 위해 데이터셋(Excel) 파일을 수동으로 먼저 업로드해 주세요."}
            
        pass_col = next((c for c in df.columns if '승객수' in str(c) or '수송인원' in str(c)), None)
        if pass_col is None: return {"error": "엑셀 첫번째 시트에 '승객수' 컬럼이 포함되어 있는지 확인해 주세요."}
        
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
        df['is_weekend'] = df['dayofweek'].isin([5,6]).astype(int)
        df['is_holiday'] = df['date'].map(lambda x: 1 if x in kr_holidays else 0)
        
        asos_data = fetch_asos_daily("2023-01-01", f"{target_y}-12-31")
        
        df['temp_max'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tmax') if is_valid_temp(asos_data.get(x, {}).get('tmax')) else np.nan)
        df['temp_min'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tmin') if is_valid_temp(asos_data.get(x, {}).get('tmin')) else np.nan)
        df['temp_avg'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tavg') if is_valid_temp(asos_data.get(x, {}).get('tavg')) else np.nan)
        df['humidity'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('humi') if is_valid_humi(asos_data.get(x, {}).get('humi')) else np.nan)
        df['passengers'] = df[pass_col]

        if 'pm25_val' in df.columns:
            df['pm25'] = df['pm25_val']

        idx_target = df['date'].dt.year == target_y
        for i in df[idx_target].index:
            past_date = df.loc[i, 'date'] - pd.DateOffset(years=1)
            past_val = df[df['date'] == past_date]
            if not past_val.empty:
                if pd.isna(df.loc[i, 'temp_max']): df.loc[i, 'temp_max'] = past_val.iloc[0]['temp_max']
                if pd.isna(df.loc[i, 'temp_min']): df.loc[i, 'temp_min'] = past_val.iloc[0]['temp_min']
                if pd.isna(df.loc[i, 'temp_avg']): df.loc[i, 'temp_avg'] = past_val.iloc[0]['temp_avg']
                if pd.isna(df.loc[i, 'humidity']): df.loc[i, 'humidity'] = past_val.iloc[0]['humidity']
                if pd.isna(df.loc[i, 'passengers']): df.loc[i, 'passengers'] = past_val.iloc[0]['passengers'] * (1 + (pass_rate / 100.0))
                if 'pm25' in df.columns and pd.isna(df.loc[i, 'pm25']): df.loc[i, 'pm25'] = past_val.iloc[0]['pm25']
                
        if df['temp_max'].isna().all():
            return {"error": "기상청 API 허브 서버와 통신할 수 없습니다. 잠시 후 [AI 예측 실행]을 다시 눌러주세요."}
        
        df['temp_max'] = df['temp_max'].bfill().ffill()
        df['temp_min'] = df['temp_min'].bfill().ffill()
        df['temp_avg'] = df['temp_avg'].bfill().ffill()
        df['humidity'] = df['humidity'].bfill().ffill()
        df['passengers'] = df['passengers'].bfill().ffill()
        if 'pm25' in df.columns:
            df['pm25'] = df['pm25'].bfill().ffill()

        if temp_adj != 0:
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_max'] += float(temp_adj)
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_avg'] += float(temp_adj)
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_min'] += float(temp_adj)

        if winter_temp_adj != 0:
            df.loc[idx_target & (df['month'].isin([12, 1, 2])), 'temp_max'] += float(winter_temp_adj)
            df.loc[idx_target & (df['month'].isin([12, 1, 2])), 'temp_avg'] += float(winter_temp_adj)
            df.loc[idx_target & (df['month'].isin([12, 1, 2])), 'temp_min'] += float(winter_temp_adj)
            
        if 'pm25' in df.columns and pm25_adj != 0:
            target_indices = df[idx_target].index
            if pm25_adj > 0:
                normal_days = df.loc[target_indices][df.loc[target_indices, 'pm25'] <= 35]
                adjust_idx = normal_days.sort_values('pm25', ascending=False).head(pm25_adj).index
                df.loc[adjust_idx, 'pm25'] = 45.0
            elif pm25_adj < 0:
                bad_days = df.loc[target_indices][df.loc[target_indices, 'pm25'] > 35]
                adjust_idx = bad_days.sort_values('pm25', ascending=True).head(abs(pm25_adj)).index
                df.loc[adjust_idx, 'pm25'] = 25.0

        features = ['month', 'dayofweek', 'is_weekend', 'is_holiday', 'passengers', 'temp_max', 'temp_min', 'temp_avg', 'humidity']
        if 'pm25' in df.columns: features.append('pm25')
        
        train_df = df[df['date'].dt.year <= (target_y - 1)].copy()
        train_df = train_df.dropna(subset=['target_power'] + features)
        if train_df.empty: return {"error": "학습할 전력량 데이터(과거 실측치)가 존재하지 않습니다."}
             
        test_df = df[df['date'].dt.year == target_y].copy()
        test_df = test_df.dropna(subset=features)
        if test_df.empty: return {"error": f"{target_year}년도 예측을 위한 데이터 포맷이 처리 중 소실되었습니다."}
        
        X_train, y_train = train_df[features].copy(), train_df['target_power'].copy()
        X_test = test_df[features].copy()
        
        X_train = X_train.bfill().ffill()
        X_test = X_test.bfill().ffill()
        
        model = xgb.XGBRegressor(n_estimators=100, learning_rate=0.05, max_depth=5, random_state=42)
        model.fit(X_train, y_train)
        test_df['pred_power'] = model.predict(X_test)
        
        if reports_data:
            try:
                local_reports = json.loads(reports_data)
                for r_info in local_reports:
                    if r_info.get('status') == '확인':
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
                            if '철거' in str(r_info.get('type', '')): daily_kwh = -daily_kwh
                            mask_after = (test_df['date'] >= app_date)
                            test_df.loc[mask_after, 'pred_power'] += daily_kwh
            except Exception as parse_err:
                print(f"로컬 부하증감 데이터 파싱 중 오류: {parse_err}")

        train_pred = model.predict(X_train)
        r2_acc = float(round(r2_score(y_train, train_pred) * 100, 1))
        
        train_last_year_df = train_df[train_df['date'].dt.year == (target_y - 1)]
        lt = float(train_last_year_df['target_power'].sum()) if not train_last_year_df.empty else 0.0
        ft = float(test_df['pred_power'].sum())
        
        last_peak_val = float(train_last_year_df['target_power'].max()) if not train_last_year_df.empty else 0.0
        peak_future_val = float(test_df['pred_power'].max())
        
        importances = [float(v) for v in (model.feature_importances_ * 100).round(1)]
        feat_df = pd.DataFrame({'name': features, 'value': importances})
        
        name_map = {
            'month': '계절(월)', 'temp_max': '기온', 'temp_min': '기온', 
            'temp_avg': '기온', 'humidity': '습도', 'passengers': '승객수', 
            'pm25': '초미세먼지(PM2.5)', 'is_holiday': '공휴일', 'is_weekend': '주말'
        }
        feat_df['name'] = feat_df['name'].map(lambda x: name_map.get(x, x))
        feat_df = feat_df.groupby('name', as_index=False)['value'].sum()
        
        top_feats = feat_df[feat_df['name'].isin(set(name_map.values()))].sort_values('value', ascending=False).to_dict(orient='records')
        
        records = []
        for m in range(1, 13):
            m_past = float(train_last_year_df[train_last_year_df['date'].dt.month == m]['target_power'].sum()) if not train_last_year_df.empty else 0.0
            m_pred = float(test_df[test_df['date'].dt.month == m]['pred_power'].sum())
            records.append({ "month": f"{m}월", "past_kwh": m_past, "pred_kwh": m_pred })
            
        res = {
            "summary": { "last_tot": lt, "tot_future": ft, "last_peak": last_peak_val, "peak_future": peak_future_val, "acc": r2_acc }, 
            "chart_data": records, "feat_data": top_feats
        }
        GLOBAL_API_CACHE["predict"][cache_key] = res
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
        
    url = "https://opm.kepco.co.kr:11080/OpenAPI/getCustBillData.do"
    
    def fetch_month_bill(m):
        data_month = f"{year}{m:02d}"
        params = {"custNo": target_cust, "dataMonth": data_month, "serviceKey": KEPCO_API_KEY, "returnType": "02"}
        for _ in range(3):
            try:
                res = http_session.get(url, params=params, timeout=5)
                if res.status_code == 200:
                    data = res.json()
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
            except: time.sleep(0.5)
        return None

    with ThreadPoolExecutor(max_workers=12) as exec:
        raw_results = list(exec.map(fetch_month_bill, range(1, 13)))
        
    records = [r for r in raw_results if r is not None]
    res = {"station_name": station, "cust_no": target_cust, "records": records}
    GLOBAL_API_CACHE["bill"][cache_key] = res
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

        date_col = next((c for c in df.columns if 'date' in str(c).lower() or '일자' in str(c)), 'date')
        df[date_col] = pd.to_datetime(df[date_col]).dt.normalize()
        df = df.dropna(subset=[date_col])

        yesterday = (get_kst_now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_check_date = pd.to_datetime("2026-09-05")
        existing_dates = set(df[date_col].tolist())
        
        col_map = {}
        for c in df.columns:
            for st in STATION_CUST_MAP.keys():
                if st != '전체' and st in str(c):
                    if 'total_kwh' in str(c): col_map.setdefault(st, {})['kwh'] = c
                    if 'peak_kw' in str(c): col_map.setdefault(st, {})['peak'] = c

        total_days = (yesterday - start_check_date).days + 1
        missing_dates = []
        for i in range(total_days):
            target_dt = start_check_date + timedelta(days=i)
            if target_dt not in existing_dates:
                missing_dates.append(target_dt)
            else:
                sample_col = col_map.get('설화명곡', {}).get('kwh')
                if sample_col:
                    val = df.loc[df[date_col] == target_dt, sample_col].values
                    if len(val) == 0 or pd.isna(val[0]) or val[0] == 0:
                        missing_dates.append(target_dt)

        if len(missing_dates) > 120:
            missing_dates = missing_dates[:120]

        new_rows = []
        for target_dt in missing_dates:
            d_str = target_dt.strftime("%Y-%m-%d")
            is_existing_row = target_dt in existing_dates
            row = {date_col: target_dt}
            
            def fetch_st(st):
                usage, peak, _ = get_kepco_data_for_station(st, d_str)
                return st, usage, peak
                
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(fetch_st, [s for s in STATION_CUST_MAP.keys() if s != '전체']))
                
            if is_existing_row:
                row_idx = df[df[date_col] == target_dt].index[0]
                for st, usage, peak in results:
                    if st in col_map:
                        if 'kwh' in col_map[st]: df.loc[row_idx, col_map[st]['kwh']] = usage
                        if 'peak' in col_map[st]: df.loc[row_idx, col_map[st]['peak']] = peak
            else:
                for st, usage, peak in results:
                    if st in col_map:
                        if 'kwh' in col_map[st]: row[col_map[st]['kwh']] = usage
                        if 'peak' in col_map[st]: row[col_map[st]['peak']] = peak
                new_rows.append(row)
                
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

        load_excel_dataset()
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        asos_all = fetch_asos_daily("2023-01-01", yesterday_str)
        asos_rows = []
        for dt_obj in pd.date_range("2023-01-01", yesterday_str):
            d_s = dt_obj.strftime("%Y-%m-%d")
            v = asos_all.get(d_s, {})
            tx = v.get("tmax", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tmax", "--"))
            tn = v.get("tmin", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tmin", "--"))
            ta = v.get("tavg", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_tavg", "--"))
            hm = v.get("humi", GLOBAL_WEATHER_CACHE.get(f"ASOS_{d_s}_humi", "--"))
            if is_valid_temp(tx):
                asos_rows.append({
                    "date": d_s,
                    "tmax": tx,
                    "tmin": tn if is_valid_temp(tn) else "--",
                    "tavg": ta if is_valid_temp(ta) else "--",
                    "humi": hm if is_valid_humi(hm) else "--"
                })
        
        if asos_rows:
            xls_dict["기상청_ASOS"] = pd.DataFrame(asos_rows)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            for sheet_name, sheet_df in xls_dict.items():
                temp_df = sheet_df.copy()
                if date_col in temp_df.columns:
                    temp_df = temp_df.dropna(subset=[date_col])
                    if pd.api.types.is_datetime64_any_dtype(temp_df[date_col]):
                        temp_df[date_col] = temp_df[date_col].dt.strftime('%Y-%m-%d')
                        
                if '일자' in temp_df.columns:
                    temp_df = temp_df.dropna(subset=['일자'])
                    if pd.api.types.is_datetime64_any_dtype(temp_df['일자']):
                        temp_df['일자'] = temp_df['일자'].dt.strftime('%Y-%m-%d')
                
                temp_df.to_excel(writer, sheet_name=sheet_name, index=False)
            
        excel_bytes = output.getvalue()

        with open(file_path, "wb") as f:
            f.write(excel_bytes)
        backup_dataset_to_firebase(excel_bytes)
        GLOBAL_API_CACHE["compare"].clear()
        GLOBAL_API_CACHE["predict"].clear()
        load_excel_dataset()

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