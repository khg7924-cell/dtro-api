import hashlib
import random
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
import urllib.parse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATION_COORDS = {
    '전체': {'mult': 1.0}, '1호선': {'mult': 0.4}, '2호선': {'mult': 0.4}, 
    '3호선': {'mult': 0.15}, '종합청사': {'mult': 0.05}
}

def parse_float(val):
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0

def fetch_kma_asos_daily(start_date: str, end_date: str, stn_id: str = "143"):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if end_date >= datetime.now().strftime("%Y-%m-%d"):
        end_date = yesterday

    start_dt = start_date.replace("-", "")
    end_dt = end_date.replace("-", "")
    
    url = "http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"
    
    # 🚨 발급받으신 [일반 인증키(Decoding)] 유지해주세요!
    service_key = "4480c93a63159f09aebc2d0aa5ec7cff37503e60d6297b500e6da8d91e20f5cb"
    
    weather_data = {}
    page_no = 1
    num_of_rows = 999 
    
    while True:
        params = {
            "serviceKey": urllib.parse.unquote(service_key),
            "pageNo": page_no, "numOfRows": num_of_rows,
            "dataType": "JSON", "dataCd": "ASOS", "dateCd": "DAY",
            "startDt": start_dt, "endDt": end_dt, "stnIds": stn_id
        }
        
        try:
            response = requests.get(url, params=params)
            res_json = response.json()
            
            header = res_json.get("response", {}).get("header", {})
            if header.get("resultCode") != "00":
                print(f"⚠️ 기상청 API 에러: {header.get('resultMsg')}")
                break
                
            body = res_json.get("response", {}).get("body", {})
            items = body.get("items", {})
            item_list = items.get("item", []) if isinstance(items, dict) else []
            
            if not item_list: break
                
            for item in item_list:
                date_str = item.get("tm") 
                rn = parse_float(item.get("sumRn"))
                sn = parse_float(item.get("ddMefs"))
                cloud = parse_float(item.get("avgTca"))
                
                if sn > 0: wx_state = "눈 ❄️"
                elif rn > 0: wx_state = "비 🌧️"
                elif cloud >= 8.0: wx_state = "흐림 ☁️"
                elif cloud >= 5.0: wx_state = "구름많음 ⛅"
                else: wx_state = "맑음 ☀️"

                weather_data[date_str] = {
                    "temp_max": item.get("maxTa"),
                    "temp_min": item.get("minTa"),
                    "humidity": item.get("avgRhm"),
                    "weather": wx_state
                }
            
            total_count = body.get("totalCount", 0)
            if page_no * num_of_rows >= total_count: break
            page_no += 1
            
        except Exception as e:
            print(f"⚠️ 기상청 통신 오류: {e}")
            break
            
    return weather_data


@app.get("/api/dashboard/{station}")
def get_dashboard_data(station: str, start: str, end: str):
    target = STATION_COORDS.get(station, STATION_COORDS.get('전체'))
    mult = target['mult'] if target else 1.0
    
    kma_weather_dict = fetch_kma_asos_daily(start, end, "143")
    
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    diff = (end_dt - start_dt).days + 1
    
    # 🌟 최대 조회 가능 기간을 2년에서 5년(약 1825일)으로 대폭 늘렸습니다.
    if diff > 365 * 5: diff = 365 * 5 
    
    records = []
    total_usage, max_peak, total_co2 = 0, 0, 0
    
    for i in range(diff):
        current_date = (start_dt.timestamp() + i*86400)
        date_str = datetime.fromtimestamp(current_date).strftime("%Y-%m-%d")
        
        daily_weather = kma_weather_dict.get(date_str, {})
        t_max = daily_weather.get("temp_max", "--")
        t_min = daily_weather.get("temp_min", "--")
        humi = daily_weather.get("humidity", "--")
        wx = daily_weather.get("weather", "조회불가") if date_str >= datetime.now().strftime("%Y-%m-%d") else daily_weather.get("weather", "데이터없음")
        
        seed_hash = int(hashlib.md5(f"{station}_{date_str}".encode('utf-8')).hexdigest(), 16)
        random.seed(seed_hash)
        
        daily_usage = random.uniform(20000, 24000) * mult
        daily_peak = daily_usage * random.uniform(0.06, 0.08)
        total_usage += daily_usage
        if daily_peak > max_peak: max_peak = daily_peak
        co2_val = daily_usage * 0.466 / 1000
        total_co2 += co2_val
        
        records.append({
            "date": date_str,
            "usage_kwh": round(daily_usage, 1),
            "peak_kw": round(daily_peak, 1),
            "varLag": round(daily_usage * 0.1, 1),
            "varLead": round(daily_usage * 0.02, 1),
            "co2": round(co2_val, 2),
            "pfLag": round(random.uniform(97, 99), 1),
            "pfLead": round(random.uniform(98, 99.9), 1),
            "weather": wx, 
            "temp_max": t_max,
            "temp_min": t_min,
            "humidity": humi,
            "pm10": int(random.uniform(20, 80)),
            "pm25": int(random.uniform(10, 40)),
            "details": [] 
        })
    random.seed()
    
    return {
        "station_name": station, "mapped_location": "대구(KMA 143)",
        "summary": { "total_usage": round(total_usage), "max_peak": round(max_peak, 1), "total_co2": round(total_co2, 1) },
        "daily_records": records
    }

@app.get("/api/realtime/{station}")
def get_realtime_data(station: str):
    target = STATION_COORDS.get(station, STATION_COORDS.get('전체'))
    mult = target['mult'] if target else 1.0
    today_str = datetime.now().strftime("%Y-%m-%d")
    seed_hash = int(hashlib.md5(f"realtime_{station}_{today_str}".encode('utf-8')).hexdigest(), 16)
    random.seed(seed_hash)
    usage_kwh_base = random.uniform(20000, 24000) * mult
    peak_kw_base = usage_kwh_base * random.uniform(0.06, 0.08)
    records = []
    h, m = 0, 0
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    for _ in range(96):
        time_str = f"{h:02d}:{m:02d}"
        time_minutes = h * 60 + m
        if time_minutes > current_minutes:
            records.append({ "time": time_str, "usage_kwh": None, "peak_kw": None })
        else:
            tf = 1.2 if 8<=h<=18 else 0.6 if 0<=h<=5 else 1.0
            usage = round((usage_kwh_base / 96) * tf * random.uniform(0.9, 1.1), 1)
            peak = round((peak_kw_base) * tf * random.uniform(0.9, 1.1), 1)
            records.append({ "time": time_str, "usage_kwh": usage, "peak_kw": peak })
        m += 15
        if m >= 60: m = 0; h += 1
    random.seed()
    return {"station_name": station, "date": today_str, "records": records}

@app.get("/api/compare/{station}")
def get_compare_data(station: str, base_year: str, comp_year: str, price: int = 150):
    target = STATION_COORDS.get(station, STATION_COORDS.get('전체'))
    mult = target['mult'] if target else 1.0
    records = []
    tb, tc = 0, 0
    for m in range(1, 13):
        random.seed(int(hashlib.md5(f"base_{station}_{base_year}_{m}".encode('utf-8')).hexdigest(), 16))
        bv = int(random.uniform(500000, 800000) * mult)
        random.seed(int(hashlib.md5(f"comp_{station}_{comp_year}_{m}".encode('utf-8')).hexdigest(), 16))
        cv = int(bv * random.uniform(1.05, 1.15)) if m in [7,8] else int(bv * random.uniform(0.95, 1.05))
        df = cv - bv
        tb += bv; tc += cv
        records.append({"month": f"{m}월", "base_val": bv, "comp_val": cv, "diff": df, "diff_pct": round((df/bv)*100,1), "cost": df*price})
    return {"summary": {"total_base": tb, "total_comp": tc, "diff": tc-tb, "diff_pct": round(((tc-tb)/tb)*100,1), "cost": (tc-tb)*price}, "records": records}

@app.get("/api/compare/{station}")
def get_compare_data(station: str, base_year: str, comp_year: str, price: int = 150):
    target = STATION_COORDS.get(station, STATION_COORDS.get('전체'))
    mult = target['mult'] if target else 1.0
    
    # 🌟 1. 기상청 API를 호출하여 두 연도의 전체 기상 데이터 수집 (페이징 자동 처리)
    base_wx = fetch_kma_asos_daily(f"{base_year}-01-01", f"{base_year}-12-31", "143")
    comp_wx = fetch_kma_asos_daily(f"{comp_year}-01-01", f"{comp_year}-12-31", "143")
    
    # 🌟 2. 대구 관측소 맞춤형 기후 지표 산출 (폭염: 33도 이상, 한파: -10도 이하)
    def get_weather_stats(wx_data):
        heatwave = sum(1 for v in wx_data.values() if v.get('temp_max') is not None and v['temp_max'] >= 33.0)
        coldwave = sum(1 for v in wx_data.values() if v.get('temp_min') is not None and v['temp_min'] <= -10.0)
        valid_temps = [v['temp_max'] for v in wx_data.values() if v.get('temp_max') is not None]
        avg_temp = sum(valid_temps) / len(valid_temps) if valid_temps else 0.0
        return heatwave, coldwave, avg_temp

    base_hw, base_cw, base_avg = get_weather_stats(base_wx)
    comp_hw, comp_cw, comp_avg = get_weather_stats(comp_wx)
    
    records = []
    tb, tc = 0, 0
    for m in range(1, 13):
        random.seed(int(hashlib.md5(f"base_{station}_{base_year}_{m}".encode('utf-8')).hexdigest(), 16))
        bv = int(random.uniform(500000, 800000) * mult)
        
        # 기상 지표에 따라 전력 사용량 시뮬레이션 가중치 부여
        random.seed(int(hashlib.md5(f"comp_{station}_{comp_year}_{m}".encode('utf-8')).hexdigest(), 16))
        factor = 1.0
        if m in [7, 8] and comp_hw > base_hw: factor += 0.05
        if m in [1, 2, 12] and comp_cw > base_cw: factor += 0.05
        
        cv = int(bv * factor * random.uniform(0.95, 1.08))
        df = cv - bv
        tb += bv; tc += cv
        records.append({"month": f"{m}월", "base_val": bv, "comp_val": cv, "diff": df, "diff_pct": round((df/bv)*100,1) if bv>0 else 0, "cost": df*price})
    
    diff_total = tc - tb
    diff_pct = round((diff_total / tb) * 100, 1) if tb > 0 else 0
    
    # 🌟 3. 실제 기상 데이터를 반영한 동적 리포트 생성
    report_text = f"[{station}] {base_year}년 대비 {comp_year}년 전력 수요 분석 리포트\n\n"
    report_text += f"▶ 대구 관측소 기상 실측 데이터:\n"
    report_text += f" - {base_year}년: 폭염일수 {base_hw}일 / 한파일수 {base_cw}일 (연평균 최고기온 {base_avg:.1f}℃)\n"
    report_text += f" - {comp_year}년: 폭염일수 {comp_hw}일 / 한파일수 {comp_cw}일 (연평균 최고기온 {comp_avg:.1f}℃)\n\n"
    
    report_text += f"▶ 전력 증감 요인 AI 종합 분석:\n"
    if diff_total > 0:
        report_text += f" 총 전력량이 전년 대비 {abs(diff_pct)}% 증가하였습니다.\n"
        if comp_hw > base_hw:
            report_text += f" 기상 데이터 분석 결과, {comp_year}년의 폭염일수({comp_hw}일)가 전년보다 {comp_hw - base_hw}일 증가하여 여름철 냉방 공조 설비 부하가 급증한 것이 주원인으로 분석됩니다.\n"
        elif comp_cw > base_cw:
            report_text += f" 기상 데이터 분석 결과, {comp_year}년의 한파일수({comp_cw}일)가 전년보다 {comp_cw - base_cw}일 증가하여 겨울철 난방 설비 가동이 증가한 것이 영향을 미쳤습니다.\n"
        else:
            report_text += f" 이상 기후(폭염/한파)의 큰 증가 없이 전력량이 증가하였으므로, 승객 수 증가 및 열차 운행 스케줄 변동 등 내부 운영 데이터와의 교차 분석이 필요합니다.\n"
    else:
        report_text += f" 총 전력량이 전년 대비 {abs(diff_pct)}% 감소하였습니다.\n"
        if comp_hw < base_hw or comp_cw < base_cw:
            report_text += f" 전년 대비 폭염 또는 한파 일수가 감소하여 온화한 기후가 유지된 것이 냉난방 부하 감소 및 전력 절감에 기여한 것으로 분석됩니다.\n"
        else:
            report_text += f" 기후 조건이 비슷하거나 악화되었음에도 전력이 감소한 것은 에너지 효율화 사업(LED 교체 등)이나 부하 최적화 운영의 긍정적 효과로 추정됩니다.\n"

    return {
        "summary": {
            "total_base": tb, "total_comp": tc, "diff": diff_total, "diff_pct": diff_pct, "cost": diff_total*price,
            "ai_report": report_text # 🌟 완성된 텍스트를 프론트엔드로 전달
        }, 
        "records": records
    }