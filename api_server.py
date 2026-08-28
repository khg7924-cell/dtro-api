from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import requests
import random
import hashlib
import pandas as pd
import numpy as np

try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_percentage_error
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🚨 기상청 API Hub 인증키
KMA_API_KEY = "vDWZwqskT6W1mcKrJL-l4w"

# 🌟 WMO 날씨 코드 매핑표 (Open-Meteo 기준)
WMO_MAP = {
    0: "☀️ 맑음", 1: "🌤 대체로 맑음", 2: "⛅ 구름 조금", 3: "☁️ 흐림",
    45: "🌫 안개", 48: "🌫 안개", 51: "🌦 이슬비", 53: "🌦 이슬비", 55: "🌦 강한 이슬비",
    61: "🌧 비 조금", 63: "🌧 비", 65: "🌧 강한 비", 71: "🌨 눈 조금", 73: "🌨 눈", 75: "🌨 강한 눈",
    80: "🌦 소나기", 81: "🌧 소나기", 82: "🌧 강한 소나기", 95: "⛈ 뇌우", 96: "⛈ 뇌우/우박", 99: "⛈ 강한 뇌우"
}

# 📍 대상 개소별 위치 및 KMA 측정소 매핑
STATION_COORDS = {
    '전체': {'loc': '대구 전체', 'kma_stn': '143', 'lat': 35.8714, 'lon': 128.6014, 'mult': 100},
    '1호선': {'loc': '대구 전체', 'kma_stn': '143', 'lat': 35.8714, 'lon': 128.6014, 'mult': 30},
    '2호선': {'loc': '대구 전체', 'kma_stn': '143', 'lat': 35.8714, 'lon': 128.6014, 'mult': 40},
    '3호선': {'loc': '대구 전체', 'kma_stn': '143', 'lat': 35.8714, 'lon': 128.6014, 'mult': 30},
    
    # 1호선
    '설화명곡': {'loc': '대구 화원읍', 'kma_stn': '143', 'lat': 35.8016, 'lon': 128.4984, 'mult': 1},
    '월배기지': {'loc': '대구 유천동', 'kma_stn': '143', 'lat': 35.8152, 'lon': 128.5230, 'mult': 1},
    '서부정류장': {'loc': '대구 대명동', 'kma_stn': '143', 'lat': 35.8368, 'lon': 128.5670, 'mult': 1},
    '반월당': {'loc': '대구 덕산동', 'kma_stn': '143', 'lat': 35.8655, 'lon': 128.5934, 'mult': 1},
    '신천': {'loc': '대구 신천동', 'kma_stn': '143', 'lat': 35.8702, 'lon': 128.6186, 'mult': 1},
    '방촌': {'loc': '대구 방촌동', 'kma_stn': '143', 'lat': 35.8778, 'lon': 128.6667, 'mult': 1},
    '안심': {'loc': '대구 괴전동', 'kma_stn': '143', 'lat': 35.8741, 'lon': 128.7180, 'mult': 1},
    '숙천': {'loc': '대구 숙천동', 'kma_stn': '143', 'lat': 35.8850, 'lon': 128.7350, 'mult': 1},
    '금락': {'loc': '경북 하양읍', 'kma_stn': '281', 'lat': 35.9125, 'lon': 128.8180, 'mult': 1},
    
    # 2호선
    '문양기지': {'loc': '대구 신매동', 'kma_stn': '143', 'lat': 35.8550, 'lon': 128.4550, 'mult': 1},
    '대실': {'loc': '대구 다사읍', 'kma_stn': '143', 'lat': 35.8566, 'lon': 128.4638, 'mult': 1},
    '성서산단': {'loc': '대구 이곡동', 'kma_stn': '143', 'lat': 35.8528, 'lon': 128.5080, 'mult': 1},
    '죽전': {'loc': '대구 죽전동', 'kma_stn': '143', 'lat': 35.8510, 'lon': 128.5370, 'mult': 1},
    '반고개': {'loc': '대구 두류동', 'kma_stn': '143', 'lat': 35.8615, 'lon': 128.5670, 'mult': 1},
    '대구은행': {'loc': '대구 수성동4가', 'kma_stn': '143', 'lat': 35.8600, 'lon': 128.6150, 'mult': 1},
    '만촌': {'loc': '대구 만촌동', 'kma_stn': '143', 'lat': 35.8580, 'lon': 128.6500, 'mult': 1},
    '수성알파시티': {'loc': '대구 연호동', 'kma_stn': '143', 'lat': 35.8480, 'lon': 128.6750, 'mult': 1},
    '사월': {'loc': '대구 신매동', 'kma_stn': '143', 'lat': 35.8385, 'lon': 128.7050, 'mult': 1},
    '영남대': {'loc': '경산시 대동', 'kma_stn': '281', 'lat': 35.8250, 'lon': 128.7530, 'mult': 1},
    
    # 3호선
    '칠곡기지': {'loc': '대구 동호동', 'kma_stn': '143', 'lat': 35.9600, 'lon': 128.5500, 'mult': 1},
    '팔달시장': {'loc': '대구 노원동3가', 'kma_stn': '143', 'lat': 35.8900, 'lon': 128.5750, 'mult': 1},
    '남산': {'loc': '대구 남산동', 'kma_stn': '143', 'lat': 35.8570, 'lon': 128.5850, 'mult': 1},
    '범물기지': {'loc': '대구 범물동', 'kma_stn': '143', 'lat': 35.8150, 'lon': 128.6380, 'mult': 1},
    
    # 종합청사
    '종합청사': {'loc': '대구 상인동', 'kma_stn': '143', 'lat': 35.8180, 'lon': 128.5380, 'mult': 15}
}

def _parse_kma_response(text: str):
    """기상청 API Hub의 텍스트 응답 파싱 (온도 및 습도만 처리)"""
    parsed = {}
    lines = text.split('\n')
    headers = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('#') and any(k in line for k in ['TM', 'YYMMDD', 'TA_MAX', 'STN']):
            headers = [h.strip() for h in line.replace('#', '').split()]
            continue
        if line.startswith('#'):
            continue
            
        cols = line.split()
        if len(cols) >= 5:
            tm_idx = 0
            max_idx = 8 if len(cols) > 8 else 3
            min_idx = 10 if len(cols) > 10 else 4
            hum_idx = 12 if len(cols) > 12 else 5
            
            if headers:
                for idx, h in enumerate(headers):
                    h_upper = h.upper()
                    if h_upper in ['TM', 'YYMMDD', 'DATE']: tm_idx = idx
                    elif h_upper in ['TA_MAX', 'MAX_TA']: max_idx = idx
                    elif h_upper in ['TA_MIN', 'MIN_TA']: min_idx = idx
                    elif h_upper in ['HM_AVG', 'AVG_HM', 'HM']: hum_idx = idx

            try:
                date_raw = cols[tm_idx]
                if len(date_raw) == 8 and date_raw.isdigit():
                    date_val = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
                    
                    def to_float(val, is_hum=False):
                        try:
                            v = float(val)
                            if is_hum:
                                return v if 0.0 <= v <= 100.0 else None
                            return v if v > -90.0 else None
                        except:
                            return None
                            
                    t_max = to_float(cols[max_idx]) if max_idx < len(cols) else None
                    t_min = to_float(cols[min_idx]) if min_idx < len(cols) else None
                    hum = to_float(cols[hum_idx], is_hum=True) if hum_idx < len(cols) else None
                    
                    if t_max is not None or t_min is not None or hum is not None:
                        parsed[date_val] = {
                            'temp_max': t_max,
                            'temp_min': t_min,
                            'humidity': hum
                        }
            except Exception:
                continue
    return parsed

# 🌟 1. 기상청 API Hub (다중 관측소 분기)
def fetch_kma_daily_weather(kma_stn, start_date_str, end_date_str):
    kma_dict = {}
    url = "https://apihub.kma.go.kr/api/typ01/url/kma_sfcdd3.php"
    start_dt = start_date_str.replace("-", "")
    end_dt = end_date_str.replace("-", "")
    
    try:
        params = {"tm1": start_dt, "tm2": end_dt, "stn": kma_stn, "help": "1", "authKey": KMA_API_KEY}
        res = requests.get(url, params=params, timeout=8)
        res.encoding = 'euc-kr'
        kma_dict = _parse_kma_response(res.text)
    except Exception as e:
        print(f"KMA API 1차 호출({kma_stn}) 오류: {e}")

    if not kma_dict and kma_stn != '143':
        try:
            params = {"tm1": start_dt, "tm2": end_dt, "stn": "143", "help": "1", "authKey": KMA_API_KEY}
            res = requests.get(url, params=params, timeout=8)
            res.encoding = 'euc-kr'
            kma_dict = _parse_kma_response(res.text)
        except Exception as e:
            print(f"KMA API 2차 Fallback(143) 오류: {e}")
            
    return kma_dict

# 🌟 2. Open-Meteo (대기질 + 날씨 상태 아이콘 + 보조 기온)
def fetch_openmeteo_data(lat, lon, start_date_str, end_date_str):
    air_dict = {}
    weather_dict = {}
    try:
        # 대기질 (PM10, PM2.5)
        air_url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&start_date={start_date_str}&end_date={end_date_str}&hourly=pm10,pm2_5&timezone=Asia%2FSeoul"
        air_res = requests.get(air_url, timeout=8).json()
        
        if "hourly" in air_res:
            h_time = air_res["hourly"].get("time", [])
            h_pm10 = air_res["hourly"].get("pm10", [])
            h_pm25 = air_res["hourly"].get("pm2_5", [])
            
            daily_map = {}
            for idx, t_str in enumerate(h_time):
                d_str = t_str[:10]
                if d_str not in daily_map:
                    daily_map[d_str] = {'pm10': [], 'pm25': []}
                if idx < len(h_pm10) and h_pm10[idx] is not None: daily_map[d_str]['pm10'].append(h_pm10[idx])
                if idx < len(h_pm25) and h_pm25[idx] is not None: daily_map[d_str]['pm25'].append(h_pm25[idx])
                
            for d_str, vals in daily_map.items():
                air_dict[d_str] = {
                    'pm10': round(sum(vals['pm10']) / len(vals['pm10']), 1) if vals['pm10'] else None,
                    'pm25': round(sum(vals['pm25']) / len(vals['pm25']), 1) if vals['pm25'] else None
                }

        # 보조 기상 데이터 (WMO 날씨 코드 및 기상청 백업용 기온/습도)
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&start_date={start_date_str}&end_date={end_date_str}&daily=weather_code,temperature_2m_max,temperature_2m_min&hourly=relative_humidity_2m&timezone=Asia%2FSeoul"
        w_res = requests.get(w_url, timeout=8).json()
        if "daily" in w_res:
            d_time = w_res["daily"].get("time", [])
            d_max = w_res["daily"].get("temperature_2m_max", [])
            d_min = w_res["daily"].get("temperature_2m_min", [])
            d_code = w_res["daily"].get("weather_code", []) # 🌟 날씨 상태 WMO 코드
            h_hum = w_res.get("hourly", {}).get("relative_humidity_2m", [])
            
            for idx, d_str in enumerate(d_time):
                s_idx, e_idx = idx * 24, (idx + 1) * 24
                h_slice = [x for x in h_hum[s_idx:e_idx] if x is not None] if h_hum else []
                avg_h = round(sum(h_slice) / len(h_slice), 1) if h_slice else None
                
                code = d_code[idx] if idx < len(d_code) and d_code[idx] is not None else 0
                
                weather_dict[d_str] = {
                    'temp_max': d_max[idx] if idx < len(d_max) else None,
                    'temp_min': d_min[idx] if idx < len(d_min) else None,
                    'humidity': avg_h,
                    'weather': WMO_MAP.get(code, "☀️ 맑음") # 정밀한 날씨 아이콘 맵핑
                }
    except Exception as e:
        print(f"Open-Meteo 통신 오류: {e}")
        
    return air_dict, weather_dict

# ⚡ [고정 전력량 시뮬레이터]
def get_deterministic_power_usage(station_name, date_str, mult):
    seed_hash = int(hashlib.md5(f"{station_name}_{date_str}".encode('utf-8')).hexdigest(), 16)
    random.seed(seed_hash)
    usage_kwh = round(random.uniform(20000, 24000) * mult, 1)
    peak_kw = round(usage_kwh * random.uniform(0.06, 0.08), 1)
    details = []
    h, m = 0, 0
    for _ in range(96):
        usage = round((usage_kwh / 96) * random.uniform(0.85, 1.15), 1)
        details.append({
            "time": f"└ {h:02d}:{m:02d}", 
            "usage": usage, 
            "peak": round(peak_kw * random.uniform(0.8, 1.0), 1), 
            "varLag": round(usage * 0.13, 1), 
            "varLead": 0.1, 
            "co2": round(usage * 0.4662 / 1000, 2), 
            "pfLag": 99.2, 
            "pfLead": 100.0
        })
        m += 15
        if m >= 60: m = 0; h += 1
    random.seed()
    return usage_kwh, peak_kw, details

# ⚡ [API 1] 대시보드
@app.get("/api/dashboard/{station}")
def get_dashboard_data(station: str, start: str = Query(None), end: str = Query(None)):
    if not start or not end:
        end_date = datetime.now() - timedelta(days=1)
        start_date = end_date - timedelta(days=7)
    else:
        start_date = datetime.strptime(start, "%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y-%m-%d")

    target = STATION_COORDS.get(station, STATION_COORDS['전체'])
    start_str, end_str = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
    
    # 기상청 실측 및 Open-Meteo 보조/대기질 병렬 연동
    kma_map = fetch_kma_daily_weather(target['kma_stn'], start_str, end_str)
    air_map, om_weather_map = fetch_openmeteo_data(target['lat'], target['lon'], start_str, end_str)

    records = []
    total_usage = 0
    max_peak = 0
    curr_date = start_date

    while curr_date <= end_date:
        d_str = curr_date.strftime("%Y-%m-%d")
        usage_kwh, peak_kw, details = get_deterministic_power_usage(station, d_str, target['mult'])
        total_usage += usage_kwh
        max_peak = max(max_peak, peak_kw)
        
        # 1순위 KMA 기온/습도 데이터, 1순위 Open-Meteo 날씨/미세먼지 데이터
        k_info = kma_map.get(d_str, {})
        om_w = om_weather_map.get(d_str, {})
        a_info = air_map.get(d_str, {})
        
        t_max = k_info.get('temp_max') if k_info.get('temp_max') is not None else om_w.get('temp_max', '--')
        t_min = k_info.get('temp_min') if k_info.get('temp_min') is not None else om_w.get('temp_min', '--')
        hum = k_info.get('humidity') if k_info.get('humidity') is not None else om_w.get('humidity', '--')
        weather_val = om_w.get('weather', '☀️ 맑음') # 🌟 날씨는 이제 무조건 정확한 Open-Meteo 사용

        records.append({
            "date": d_str, 
            "usage_kwh": usage_kwh, 
            "peak_kw": peak_kw,
            "varLag": round(usage_kwh * 0.13, 1), 
            "varLead": 10.0, 
            "co2": round(usage_kwh * 0.4662 / 1000, 2), 
            "pfLag": 99.2, 
            "pfLead": 100.0,
            
            "pm10": a_info.get('pm10', '--'), 
            "pm25": a_info.get('pm25', '--'),
            "weather": weather_val,
            "temp_max": t_max if t_max is not None else '--', 
            "temp_min": t_min if t_min is not None else '--', 
            "humidity": hum if hum is not None else '--',
            
            "details": details
        })
        curr_date += timedelta(days=1)

    return { 
        "station_name": station, 
        "mapped_location": target['loc'], 
        "summary": { 
            "total_usage": round(total_usage, 1), 
            "max_peak": round(max_peak, 1), 
            "total_co2": round(total_usage * 0.4662 / 1000, 2) 
        }, 
        "daily_records": records 
    }

# 📊 [API 2] 연도별 비교
@app.get("/api/compare/{station}")
def get_compare_data(station: str, base_year: int = 2024, comp_year: int = 2025, price: float = 150.0):
    target = STATION_COORDS.get(station, STATION_COORDS['전체'])
    mult = target['mult']
    
    def get_monthly_usage(year, month):
        seed_hash = int(hashlib.md5(f"{station}_{year}_{month}".encode('utf-8')).hexdigest(), 16)
        random.seed(seed_hash)
        base = random.uniform(500000, 650000) * mult
        if month in [7, 8, 1, 12]: base *= 1.3 
        random.seed()
        return round(base)

    records = []
    total_base = 0
    total_comp = 0
    for m in range(1, 13):
        b_val = get_monthly_usage(base_year, m)
        c_val = get_monthly_usage(comp_year, m)
        diff = c_val - b_val
        diff_pct = round((diff / b_val) * 100, 2) if b_val else 0
        cost = round(diff * price)
        total_base += b_val
        total_comp += c_val
        records.append({ "month": f"{m}월", "base_val": b_val, "comp_val": c_val, "diff": diff, "diff_pct": diff_pct, "cost": cost })
        
    diff_total = total_comp - total_base
    return {
        "summary": { 
            "total_base": total_base, 
            "total_comp": total_comp, 
            "diff": diff_total, 
            "diff_pct": round((diff_total / total_base) * 100, 2) if total_base else 0, 
            "cost": round(diff_total * price) 
        },
        "records": records
    }

# 🤖 [API 3] AI 전력 수요 예측
@app.get("/api/predict/{station}")
def predict_power_demand(station: str, target_year: int = 2026, pass_rate: float = 5.0, temp_adj: float = 1.5):
    if not HAS_SKLEARN: return {"error": "Scikit-learn 라이브러리가 필요합니다."}
        
    try: df = pd.read_csv("database_analysis_6_테스트.csv", encoding='utf-8')
    except:
        try: df = pd.read_csv("database_analysis_6_테스트.csv", encoding='cp949')
        except: return {"error": "데이터셋 파일을 찾을 수 없습니다. (database_analysis_6_테스트.csv를 준비해주세요.)"}
            
    df['date'] = pd.to_datetime(df['date'])
    df['year'] = df['date'].dt.year
    df['month'] = df['date'].dt.month
    df['dayofweek'] = df['date'].dt.dayofweek

    if station == '전체':
        kwh_cols = [c for c in df.columns if 'total_kwh' in c]
        peak_cols = [c for c in df.columns if 'peak_kw' in c]
    elif '호선' in station:
        kwh_cols = [c for c in df.columns if station[:3] in c and 'total_kwh' in c]
        peak_cols = [c for c in df.columns if station[:3] in c and 'peak_kw' in c]
    else:
        kwh_cols = [c for c in df.columns if station in c and 'total_kwh' in c]
        peak_cols = [c for c in df.columns if station in c and 'peak_kw' in c]

    if not kwh_cols: return {"error": f"'{station}'에 해당하는 데이터를 찾을 수 없습니다."}

    df['target_kwh'] = df[kwh_cols].sum(axis=1)
    df['target_peak'] = df[peak_cols].sum(axis=1)

    train_df = df[df['year'] < target_year].copy()
    if train_df.empty: train_df = df.copy()
        
    pass_col = '승객수\n(passengers)'
    hol_col = '휴일\n(is_holiday)'
    if pass_col in train_df.columns: train_df[pass_col] = train_df[pass_col].fillna(train_df[pass_col].median())
    else: train_df[pass_col] = 100000
    if hol_col not in train_df.columns: train_df[hol_col] = train_df['dayofweek'].apply(lambda x: 1 if x >= 5 else 0)

    np.random.seed(42)
    train_df['temp_max'] = 15 + 15 * np.sin(np.pi * (train_df['month'] - 4) / 6) + np.random.normal(0, 2, len(train_df))
    train_df['humidity'] = np.clip(60 + np.random.normal(0, 10, len(train_df)), 0.0, 100.0)

    features = ['month', 'dayofweek', hol_col, pass_col, 'temp_max', 'humidity']
    X_train = train_df[features]
    y_kwh = train_df['target_kwh']
    y_peak = train_df['target_peak']

    model_kwh = RandomForestRegressor(n_estimators=100, random_state=42)
    model_kwh.fit(X_train, y_kwh)
    model_peak = RandomForestRegressor(n_estimators=100, random_state=42)
    model_peak.fit(X_train, y_peak)

    dates_future = pd.date_range(f'{target_year}-01-01', f'{target_year}-12-31')
    X_future = pd.DataFrame({
        'month': dates_future.month, 'dayofweek': dates_future.dayofweek,
        hol_col: [1 if d.dayofweek >= 5 else 0 for d in dates_future],
        pass_col: train_df[pass_col].median() * (1 + pass_rate / 100.0),
        'temp_max': 15 + 15 * np.sin(np.pi * (dates_future.month - 4) / 6) + temp_adj,
        'humidity': 60
    })

    pred_kwh = model_kwh.predict(X_future)
    pred_peak = model_peak.predict(X_future)
    
    pred_df = pd.DataFrame({'date': dates_future, 'kwh': pred_kwh, 'peak': pred_peak})
    pred_df['month'] = pred_df['date'].dt.month
    monthly_pred = pred_df.groupby('month').agg({'kwh':'sum', 'peak':'max'}).reset_index()

    last_year = target_year - 1
    last_df = train_df[train_df['year'] == last_year]
    if last_df.empty: last_df = train_df[train_df['year'] == train_df['year'].max()]
    monthly_last = last_df.groupby('month').agg({'target_kwh':'sum', 'target_peak':'max'}).reset_index()

    chart_data = []
    for m in range(1, 13):
        past_val = monthly_last[monthly_last['month']==m]['target_kwh'].sum() if m in monthly_last['month'].values else 0
        pred_val = monthly_pred[monthly_pred['month']==m]['kwh'].sum()
        chart_data.append({ "month": f"{m}월", "past_kwh": round(past_val), "pred_kwh": round(pred_val) })

    importances = model_kwh.feature_importances_ * 100
    raw_feat = [
        {"name": "휴일여부", "value": importances[2]}, {"name": "승객수", "value": importances[3]},
        {"name": "기온(Temp)", "value": importances[4]}, {"name": "습도(Hum)", "value": importances[5]}
    ]
    tot_rem = sum([item["value"] for item in raw_feat])
    feat_data = []
    if tot_rem > 0:
        for item in raw_feat: feat_data.append({"name": item["name"], "value": round((item["value"] / tot_rem) * 100, 1)})
    feat_data = sorted(feat_data, key=lambda x: x["value"])

    mape = mean_absolute_percentage_error(y_kwh, model_kwh.predict(X_train))
    acc = round(max(0.0, (1 - mape) * 100), 1)

    return {
        "summary": { 
            "tot_future": round(pred_df['kwh'].sum()), 
            "peak_future": round(pred_df['peak'].max()), 
            "last_tot": round(last_df['target_kwh'].sum()), 
            "last_peak": round(last_df['target_peak'].max()), 
            "acc": acc 
        },
        "chart_data": chart_data,
        "feat_data": feat_data
    }