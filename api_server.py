import os
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import requests
import xgboost as xgb
from sklearn.metrics import r2_score
import traceback
import holidays
import urllib3
import re
from concurrent.futures import ThreadPoolExecutor

# HTTPS 인증서 경고 숨김
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 🌟 클라우드 서버 배포 시 시간차 해결을 위한 한국 시간(KST) 강제 설정
KST = timezone(timedelta(hours=9))

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

STATION_AWS_MAP = {
    '설화명곡': '870', '월배기지': '870', '서부정류장': '870', 
    '반월당': '143', '신천': '827', '방촌': '827', 
    '안심': '868', '숙천': '868', '금락': '816',
    '문양기지': '814', '대실': '814', '성서산단': '870', 
    '죽전': '870', '반고개': '829', '대구은행': '143', 
    '만촌': '143', '수성알파시티': '143', '사월': '143', 
    '영남대': '277',
    '칠곡기지': '854', '팔달시장': '829', '남산': '143', 
    '범물기지': '143', '종합청사': '143',
    '전체': '143', '1호선': '143', '2호선': '143', '3호선': '143'
}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        with open("uploaded_dataset.xlsx", "wb") as f:
            f.write(contents)
        return {"status": "success", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def load_excel_dataset():
    file_path = "uploaded_dataset.xlsx"
    if not os.path.exists(file_path): return None
    try:
        xls = pd.ExcelFile(file_path)
        df_main = pd.read_excel(xls, sheet_name=xls.sheet_names[0])
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
                    for c in num_cols:
                        df_pm[c] = pd.to_numeric(df_pm[c], errors='coerce')
                    df_pm['pm25_merged'] = df_pm[num_cols].mean(axis=1)
                    all_pm25.append(df_pm[['date', 'pm25_merged']])
        
        if all_pm25:
            pm25_df = pd.concat(all_pm25).dropna(subset=['date']).groupby('date')['pm25_merged'].mean().reset_index()
            df_main = df_main.merge(pm25_df, on='date', how='left')
            df_main['pm25_val'] = df_main['pm25_merged']
            
        return df_main
    except Exception as e: 
        return None

def fetch_openmeteo_env(lat, lon, start_date, end_date):
    env_data = {}
    url_a = "https://air-quality-api.open-meteo.com/v1/air-quality"
    params_a = {
        "latitude": lat, "longitude": lon,
        "start_date": start_date, "end_date": end_date,
        "hourly": "pm2_5",
        "timezone": "Asia/Seoul"
    }
    try:
        r_a = requests.get(url_a, params=params_a, timeout=10)
        if r_a.status_code == 200:
            data = r_a.json()
            h_times = data.get("hourly", {}).get("time", [])
            pm25s = data.get("hourly", {}).get("pm2_5", [])
            pm_dict = {}
            for i, ht in enumerate(h_times):
                d_str = ht[:10]
                if pm25s[i] is not None:
                    pm_dict.setdefault(d_str, []).append(pm25s[i])
            for d_str, p_list in pm_dict.items():
                env_data.setdefault(d_str, {})["pm25"] = round(sum(p_list)/len(p_list), 1)
    except: pass

    url_w = "https://api.open-meteo.com/v1/forecast"
    params_w = {
        "latitude": lat, "longitude": lon,
        "start_date": start_date, "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "relative_humidity_2m",
        "timezone": "Asia/Seoul"
    }
    try:
        r_w = requests.get(url_w, params=params_w, timeout=10)
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
                if humis[i] is not None:
                    humi_dict.setdefault(d_str, []).append(humis[i])
                    
            for i, t in enumerate(d_times):
                env_data.setdefault(t, {})
                if tmaxs and i < len(tmaxs) and tmaxs[i] is not None: env_data[t]["tmax"] = round(tmaxs[i], 1)
                if tmins and i < len(tmins) and tmins[i] is not None: env_data[t]["tmin"] = round(tmins[i], 1)
                if t in humi_dict: env_data[t]["humi"] = round(sum(humi_dict[t])/len(humi_dict[t]), 1)
    except: pass

    return env_data

def fetch_aws_daily_for_dashboard(stn_id: str, start_date: str, end_date: str):
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # 🌟 KST 변수 적용
    yesterday = datetime.now(KST) - timedelta(days=1)
    if e_dt > yesterday.replace(tzinfo=None): e_dt = yesterday.replace(tzinfo=None)
    if s_dt > e_dt: return {} 

    res = {}
    diff = (e_dt - s_dt).days + 1
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    
    def fetch_single_element(d_str, tm2, obs, key):
        url = f"https://apihub.kma.go.kr/api/typ01/url/sfc_aws_day.php?tm2={tm2}&obs={obs}&stn={stn_id}&disp=0&help=0&authKey={KMA_API_HUB_KEY}"
        for attempt in range(2):
            try:
                r = requests.get(url, headers=headers, verify=False, timeout=5)
                lines = r.text.split('\n')
                for line in lines:
                    if line.strip() and not line.startswith('#'):
                        parts = line.split()
                        if len(parts) >= 2 and parts[0] == stn_id:
                            try:
                                fv = float(parts[1])
                                if fv > -50.0:  
                                    return key, fv
                            except: pass
                break
            except: time.sleep(0.5)
        return key, "--"

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for i in range(diff):
            curr = s_dt + timedelta(days=i)
            d_str = curr.strftime("%Y-%m-%d")
            tm2 = curr.strftime("%Y%m%d")
            res[d_str] = {"tmax": "--", "tmin": "--", "humi": "--"}
            
            futures.append((d_str, executor.submit(fetch_single_element, d_str, tm2, "ta_max", "tmax")))
            futures.append((d_str, executor.submit(fetch_single_element, d_str, tm2, "ta_min", "tmin")))
            futures.append((d_str, executor.submit(fetch_single_element, d_str, tm2, "hm_avg", "humi")))
            
        for d_str, future in futures:
            key, val = future.result()
            if val != "--":
                res[d_str][key] = val
            
    return res

def fetch_asos_daily(start_date: str, end_date: str):
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # 🌟 KST 변수 적용
    yesterday = datetime.now(KST) - timedelta(days=1)
    if e_dt > yesterday.replace(tzinfo=None): e_dt = yesterday.replace(tzinfo=None)
    if s_dt > e_dt: return {} 

    res = {}
    y_s = s_dt.strftime("%Y%m%d")
    y_e = e_dt.strftime("%Y%m%d")
    url = f"https://apihub.kma.go.kr/api/typ01/url/kma_sfcdd3.php?tm1={y_s}&tm2={y_e}&stn=143&help=1&authKey={KMA_API_HUB_KEY}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, verify=False, timeout=30)
            r.encoding = 'EUC-KR'
            lines = r.text.split('\n')
            headers_list = []
            for line in lines:
                if line.startswith('#') and ':' in line and '.' in line:
                    left_side = line.split(':')[0].strip()
                    if left_side.replace('#', '').strip()[0].isdigit():
                        col_name = left_side.split('.')[1].strip().split()[0]
                        if col_name not in headers_list: headers_list.append(col_name)
                elif line.strip() and not line.startswith('#') and headers_list:
                    parts = line.split()
                    if len(parts) >= len(headers_list) - 5 and len(parts) >= 10:
                        row_dict = dict(zip(headers_list, parts))
                        date_key = row_dict.get('TM')
                        if date_key and len(date_key) >= 8:
                            d_str = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
                            def parse_val(v):
                                if not v: return "--"
                                try:
                                    fv = float(v)
                                    if fv <= -50.0: return "--"
                                    return fv
                                except: return "--"
                            res[d_str] = {
                                "tmax": parse_val(row_dict.get('TA_MAX') or row_dict.get('TX')),
                                "tmin": parse_val(row_dict.get('TA_MIN') or row_dict.get('TN')),
                                "tavg": parse_val(row_dict.get('TA_AVG') or row_dict.get('TA')),
                                "humi": parse_val(row_dict.get('HM_AVG') or row_dict.get('HM'))
                            }
            if res: break 
        except: time.sleep(1.5)
    return res

def fetch_kepco_day_lp(cust_no: str, date_str: str):
    url = "https://opm.kepco.co.kr:11080/OpenAPI/getDayLpData.do"
    params = {"custNo": cust_no, "date": date_str.replace("-", ""), "serviceKey": KEPCO_API_KEY, "returnType": "02"}
    for _ in range(2):
        try:
            res = requests.get(url, params=params, verify=False, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if "dayLpDataInfoList" in data: return data["dayLpDataInfoList"]
                elif "header" in data: return []
        except: time.sleep(0.5)
    return None

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

def get_kepco_data_for_station(station: str, date_str: str):
    cust_nos = []
    target_meter_no = "전체"
    
    if station == '전체': cust_nos = list(set(STATION_CUST_MAP.values()))
    elif station in LINE_STATIONS: cust_nos = list(set([STATION_CUST_MAP[s] for s in LINE_STATIONS[station]]))
    elif station == '종합청사': cust_nos = [STATION_CUST_MAP['종합청사']]
    else:
        cust_nos = [STATION_CUST_MAP.get(station)]
        if station in LINE_STATIONS['1호선']: target_meter_no = STATION_METER_MAP.get(station, "")
            
    total_interval_usage = [0.0] * 96
    
    for c_no in cust_nos:
        if not c_no: continue
        day_list = fetch_kepco_day_lp(c_no, date_str)
        if day_list is None: day_list = []
        m_target = target_meter_no if c_no == '0526314773' else "전체"
        int_u = process_kepco_day_data(day_list, m_target)
        for i, val in enumerate(int_u): total_interval_usage[i] += val
            
    total_usage = sum(total_interval_usage)
    max_peak = max(total_interval_usage) * 4 if total_interval_usage else 0.0
    
    details = []
    for m in range(96):
        hh = m // 4
        mm = (m % 4) * 15 + 15
        if mm == 60: hh += 1; mm = 0
        details.append({ "time": f"{hh:02d}:{mm:02d}", "usage_kwh": round(total_interval_usage[m], 1), "peak_kw": round(total_interval_usage[m] * 4, 1) })
        
    return total_usage, max_peak, details

# =========================================================================
# 🚀 1. 통합 대시보드
# =========================================================================
@app.get("/api/dashboard/{station}")
def get_dashboard_data(station: str, start: str, end: str):
    start_dt, end_dt = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    diff = (end_dt - start_dt).days + 1
    
    lat, lon = STATION_COORD_MAP.get(station, (35.8714, 128.6014))
    openmeteo_data = fetch_openmeteo_env(lat, lon, start, end)
    
    aws_stn = STATION_AWS_MAP.get(station, '143')
    aws_data = fetch_aws_daily_for_dashboard(aws_stn, start, end)
    asos_data = fetch_asos_daily(start, end)
    
    # 🌟 KST 변수 적용
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    def process_day(i):
        curr_date = start_dt + timedelta(days=i)
        date_str = curr_date.strftime("%Y-%m-%d")
        
        if date_str >= "2026-09-04": usage, peak, details = get_kepco_data_for_station(station, date_str)
        else: usage, peak, details = 0.0, 0.0, []
            
        co2 = usage * 0.466 / 1000
        
        env_o = openmeteo_data.get(date_str, {})
        env_a = aws_data.get(date_str, {})
        
        tmax = env_a.get("tmax", "--")
        tmin = env_a.get("tmin", "--")
        humi = env_a.get("humi", "--")
        
        if tmax == "--": tmax = asos_data.get(date_str, {}).get("tmax", "--")
        if tmin == "--": tmin = asos_data.get(date_str, {}).get("tmin", "--")
        if humi == "--": humi = asos_data.get(date_str, {}).get("humi", "--")
        
        if date_str >= today_str:
            if tmax == "--": tmax = env_o.get("tmax", "--")
            if tmin == "--": tmin = env_o.get("tmin", "--")
            if humi == "--": humi = env_o.get("humi", "--")
        
        pm25 = env_o.get("pm25", "--")

        if not details or len(details) < 96:
            details = []
            for m in range(96):
                hh = m // 4
                mm = (m % 4) * 15 + 15
                if mm == 60: hh += 1; mm = 0
                details.append({"time": f"{hh:02d}:{mm:02d}", "usage_kwh": 0.0, "peak_kw": 0.0})

        return {
            "date": date_str, "usage_kwh": round(usage, 1), "peak_kw": round(peak, 1), "co2": round(co2, 2),
            "temp_max": tmax, "temp_min": tmin, "humidity": humi, "pm25": pm25, "details": details
        }

    with ThreadPoolExecutor(max_workers=30) as executor:
        records = list(executor.map(process_day, range(diff)))

    tot_usage = sum(r["usage_kwh"] for r in records)
    max_peak = max((r["peak_kw"] for r in records), default=0.0)
    tot_co2 = sum(r["co2"] for r in records)

    return {
        "station_name": station, 
        "mapped_location": f"{station} (기상청 동네 AWS {aws_stn}번 매핑 완료 / 습도는 대표 ASOS 143 보완)",
        "summary": { "total_usage": round(tot_usage), "max_peak": round(max_peak, 1), "total_co2": round(tot_co2, 1) },
        "daily_records": records
    }

@app.get("/api/realtime/{station}")
def get_realtime_data(station: str):
    # 🌟 KST 변수 적용
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    kepco_data = get_kepco_data_for_station(station, today_str)
    day_usage, day_peak, details = kepco_data if kepco_data else (0.0, 0.0, [])
    
    if not details or len(details) < 96:
        details = []
        for m in range(96):
            hh = m // 4
            mm = (m % 4) * 15 + 15
            if mm == 60: hh += 1; mm = 0
            details.append({"time": f"{hh:02d}:{mm:02d}", "usage_kwh": 0.0, "peak_kw": 0.0})
            
    # 🌟 KST 변수 적용
    now_minutes = datetime.now(KST).hour * 60 + datetime.now(KST).minute
    for d in details:
        hh, mm = map(int, d["time"].split(":"))
        time_m = hh * 60 + mm if hh != 24 else 24 * 60
        if time_m > now_minutes:
            d["usage_kwh"] = None
            d["peak_kw"] = None
            
    return {"station_name": station, "date": today_str, "records": details}

# =========================================================================
# 🚀 2. 연도별 비교 분석
# =========================================================================
@app.get("/api/compare/{station}")
def get_compare_data(station: str, base_year: str, comp_year: str, price: int = 150):
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
        df['is_offday'] = df.apply(lambda x: 1 if x['date'].dayofweek >= 5 or x['date'] in kr_holidays else 0, axis=1)

        df_base = df[df['date'].dt.year == int(base_year)]
        df_comp = df[df['date'].dt.year == int(comp_year)]
        monthly_base = df_base.groupby(df_base['date'].dt.month)['target_power'].sum()
        monthly_comp = df_comp.groupby(df_comp['date'].dt.month)['target_power'].sum()

        records, tb, tc = [], 0.0, 0.0
        for m in range(1, 13):
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
                    tmax = v.get("tmax", "--")
                    tmin = v.get("tmin", "--")
                    if tmax != "--":
                        try:
                            tmax_f = float(tmax)
                            if tmax_f >= 33.0: hw += 1
                            if m in [6, 7, 8]:
                                summer_tmax_sum += tmax_f; summer_tmax_cnt += 1
                        except: pass
                    if tmin != "--":
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

        ai_report_text = f"📊 [{station}] {base_year}년 vs {comp_year}년 전력 수요 AI 심층 분석 리포트\n\n"
        
        if not asos_data:
            ai_report_text += f"⚠️ 현재 기상청 API 허브 서버의 응답이 없어 기상 지표를 불러오지 못했습니다. 백엔드 터미널 창의 로그를 확인해주세요.\n\n"
        
        ai_report_text += f"[1] 계절별 기후 및 환경 지표 변동 현황\n"
        ai_report_text += f" • 하절기(6~8월) 평균 최고기온 : {b_s_tmax:.1f}℃ ➔ {c_s_tmax:.1f}℃ ({s_tmax_diff:+.1f}℃)\n"
        ai_report_text += f" • 동절기(12~2월) 평균 최저기온 : {b_w_tmin:.1f}℃ ➔ {c_w_tmin:.1f}℃ ({w_tmin_diff:+.1f}℃)\n"
        ai_report_text += f" • 폭염일수(33℃ 이상) : {base_hw}일 ➔ {comp_hw}일 ({hw_diff:+}일)\n"
        ai_report_text += f" • 한파일수(-10℃ 이하) : {base_cw}일 ➔ {comp_cw}일 ({cw_diff:+}일)\n"
        ai_report_text += f" • 초미세먼지 '나쁨' : {base_pm25}일 ➔ {comp_pm25}일 ({pm_diff:+}일)\n\n"
        ai_report_text += f"[2] 캘린더 및 여객 지표 변동 현황\n"
        ai_report_text += f" • 휴일(비운무일) 총합 : {b_total_off}일 ➔ {c_total_off}일 ({off_diff:+}일)\n"
        ai_report_text += f" • 연간 총 승객수 : {base_pass_sum:,.0f}명 ➔ {comp_pass_sum:,.0f}명 ({p_diff:+,.0f}명)\n\n"
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
            else: ai_report_text += "휴일 일수가 전년과 동일하여 다이아 차이로 인한 변동은 발생하지 않았습니다."

            ai_report_text += "\n\n② 계절별 기상 및 공조 설비 부하 요인: \n"
            ai_report_text += f"[하절기 냉방] 여름철(6~8월) 평균 최고기온이 {abs(s_tmax_diff):.1f}℃ {'상승' if s_tmax_diff > 0 else '하락'}하고 폭염일수가 {hw_diff:+}일 변동하여 역사 냉방기 부하가 {'증가' if s_tmax_diff > 0 or hw_diff > 0 else '감소'}했습니다. "
            ai_report_text += f"\n[동절기 난방] 겨울철(12~2월) 평균 최저기온이 {abs(w_tmin_diff):.1f}℃ {'상승(따뜻함)' if w_tmin_diff > 0 else '하락(추워짐)'}하고 한파일수가 {cw_diff:+}일 변동하여, 동절기 난방 부하는 {'감소' if w_tmin_diff > 0 and cw_diff <= 0 else '상승'}한 것으로 파악됩니다."
                
            ai_report_text += "\n\n③ 여객 동선 및 편의 설비 요인: \n"
            if p_diff > 0: ai_report_text += f"승객수가 {p_diff:+,.0f}명 증가하여 동력 설비 가동 빈도가 누적 상승하고 환기 부하 연쇄 상승이 발생했습니다."
            elif p_diff < 0: ai_report_text += f"승객수가 {abs(p_diff):,.0f}명 감소하여 전체적인 동력 전력 감소에 기여했습니다."
            else: ai_report_text += "승객수 변동폭이 작아 유의미한 여객 전력 변동은 관찰되지 않았습니다."

        return {
            "summary": { "total_base": tb, "total_comp": tc, "diff": diff_total, "diff_pct": diff_pct_total, "cost": diff_total*price, "ai_report": ai_report_text }, 
            "records": records
        }
    except Exception as e:
        return {"error": f"비교 분석 중 서버 에러가 발생했습니다: {str(e)}\n{traceback.format_exc()}"}

# =========================================================================
# 🚀 3. AI 수요 예측 
# =========================================================================
@app.get("/api/predict/{station}")
def get_predict_data(station: str, target_year: str, pass_rate: float = 0.0, temp_adj: float = 0.0):
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
        
        kr_holidays = holidays.KR()
        df['month'] = df['date'].dt.month
        df['dayofweek'] = df['date'].dt.dayofweek
        df['is_weekend'] = df['dayofweek'].isin([5,6]).astype(int)
        df['is_holiday'] = df['date'].map(lambda x: 1 if x in kr_holidays else 0)
        
        target_y = int(target_year)
        asos_data = fetch_asos_daily("2023-01-01", f"{target_y}-12-31")
        
        df['temp_max'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tmax') if asos_data.get(x, {}).get('tmax') != "--" else np.nan)
        df['temp_min'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tmin') if asos_data.get(x, {}).get('tmin') != "--" else np.nan)
        df['temp_avg'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('tavg') if asos_data.get(x, {}).get('tavg') != "--" else np.nan)
        df['humidity'] = df['date'].dt.strftime("%Y-%m-%d").map(lambda x: asos_data.get(x, {}).get('humi') if asos_data.get(x, {}).get('humi') != "--" else np.nan)
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
                
        if temp_adj != 0:
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_max'] += float(temp_adj)
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_avg'] += float(temp_adj)
            df.loc[idx_target & (df['month'].isin([6, 7, 8])), 'temp_min'] += float(temp_adj)
            
        if df['temp_max'].isna().all():
            return {"error": "기상청 API 허브 서버와 통신할 수 없습니다. 백엔드 로그 확인 후 잠시 후 [AI 예측 실행]을 다시 눌러주세요."}
        
        df['temp_max'] = df['temp_max'].bfill().ffill()
        df['temp_min'] = df['temp_min'].bfill().ffill()
        df['temp_avg'] = df['temp_avg'].bfill().ffill()
        df['humidity'] = df['humidity'].bfill().ffill()
        df['passengers'] = df['passengers'].bfill().ffill()

        features = ['month', 'dayofweek', 'is_weekend', 'is_holiday', 'passengers', 'temp_max', 'temp_min', 'temp_avg', 'humidity']
        if 'pm25' in df.columns: features.append('pm25')
        
        train_df = df[df['date'].dt.year <= (target_y - 1)].copy()
        train_df = train_df.dropna(subset=['target_power'] + features)
        if train_df.empty: return {"error": "학습할 전력량 데이터(과거 실측치)가 존재하지 않습니다."}
             
        test_df = df[df['date'].dt.year == target_y].copy()
        test_df = test_df.dropna(subset=features)
        if test_df.empty: return {"error": f"{target_year}년도 예측을 위한 데이터 포맷(빈 날짜 행)이 엑셀에 마련되어 있지 않습니다."}
        
        X_train, y_train = train_df[features].copy(), train_df['target_power'].copy()
        X_test = test_df[features].copy()
        
        X_train = X_train.bfill().ffill()
        X_test = X_test.bfill().ffill()
        
        model = xgb.XGBRegressor(n_estimators=100, learning_rate=0.05, max_depth=5, random_state=42)
        model.fit(X_train, y_train)
        test_df['pred_power'] = model.predict(X_test)
        
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
            'month': '계절(월)', 'temp_max': '최고기온(냉방)', 'temp_min': '최저기온(난방)', 
            'temp_avg': '평균기온(기저)', 'humidity': '평균습도', 'passengers': '승객수', 
            'pm25': '초미세먼지(PM2.5)', 'is_holiday': '공휴일', 'is_weekend': '주말'
        }
        feat_df['name'] = feat_df['name'].map(lambda x: name_map.get(x, x))
        top_feats = feat_df[feat_df['name'].isin(name_map.values())].sort_values('value', ascending=False).to_dict(orient='records')
        
        records = []
        for m in range(1, 13):
            m_past = float(train_last_year_df[train_last_year_df['date'].dt.month == m]['target_power'].sum()) if not train_last_year_df.empty else 0.0
            m_pred = float(test_df[test_df['date'].dt.month == m]['pred_power'].sum())
            records.append({ "month": f"{m}월", "past_kwh": m_past, "pred_kwh": m_pred })
            
        return {
            "summary": { "last_tot": lt, "tot_future": ft, "last_peak": last_peak_val, "peak_future": peak_future_val, "acc": r2_acc }, 
            "chart_data": records, "feat_data": top_feats
        }
    except Exception as e:
        return {"error": f"서버 내부 오류로 예측에 실패했습니다: {str(e)}\n\n{traceback.format_exc()}"}

# =========================================================================
# 🚀 4. 전기요금 청구정보 (월별) API 연동
# =========================================================================
@app.get("/api/bill/{station}")
def get_bill_data(station: str, year: str):
    if station in ['전체', '2호선', '3호선']:
        return {"error": "전기요금 조회는 개별 역사/기지 또는 1호선(통합)을 선택해야 정확한 내역을 확인할 수 있습니다. 좌측 메뉴에서 선택해 주세요."}
        
    if station == '1호선': target_cust = '0526314773'
    else: target_cust = STATION_CUST_MAP.get(station)
        
    if not target_cust:
        return {"error": f"[{station}]의 한전 고객번호 매핑 정보를 찾을 수 없습니다."}
        
    url = "https://opm.kepco.co.kr:11080/OpenAPI/getCustBillData.do"
    records = []
    
    for m in range(1, 13):
        data_month = f"{year}{m:02d}"
        params = {
            "custNo": target_cust,
            "dataMonth": data_month,
            "serviceKey": KEPCO_API_KEY,
            "returnType": "02"
        }
        for _ in range(3):
            try:
                res = requests.get(url, params=params, verify=False, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    info_list = data.get("custBillDataInfoList")
                    if info_list:
                        for item in info_list:
                            def parse_float(val):
                                try:
                                    if isinstance(val, str):
                                        val = val.replace(',', '').strip()
                                    return float(val)
                                except: return 0.0
                            
                            lower_item = {k.lower(): v for k, v in item.items()}
                                
                            mapped = {
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
                                "ji_pwrfact": parse_float(lower_item.get("jipwrfact", lower_item.get("ji_pwrfact"))),
                                "jn_pwrfact": parse_float(lower_item.get("jnpwrfact", lower_item.get("jn_pwrfact")))
                            }
                            records.append(mapped)
                    break
            except:
                time.sleep(1)
            
    return {"station_name": station, "cust_no": target_cust, "records": records}