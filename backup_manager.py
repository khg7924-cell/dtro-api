import os
import time
from datetime import datetime, timedelta
import pandas as pd
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor

# HTTPS 인증서 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================================================================
# ⚙️ 1. 설정 및 매핑 정보 (API 키 및 대구도시철도 개소 정보)
# =========================================================================
KEPCO_API_KEY = "6lrb2gu8t5dzg3a3505s"

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

# 백업 대상 개소 리스트 추출 (총 24개소)
ALL_STATIONS = LINE_STATIONS['1호선'] + LINE_STATIONS['2호선'] + LINE_STATIONS['3호선'] + ['종합청사']

# =========================================================================
# 📡 2. 한전 OpenAPI 수집 엔진
# =========================================================================
def fetch_kepco_day_lp(cust_no: str, date_str: str):
    """한전 서버에서 특정 고객번호의 특정 날짜 15분 단위(LP) 전력량을 가져옵니다."""
    url = "https://opm.kepco.co.kr:11080/OpenAPI/getDayLpData.do"
    params = {"custNo": cust_no, "date": date_str.replace("-", ""), "serviceKey": KEPCO_API_KEY, "returnType": "02"}
    
    for _ in range(3):
        try:
            res = requests.get(url, params=params, verify=False, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if "dayLpDataInfoList" in data:
                    return data["dayLpDataInfoList"]
                elif "header" in data: 
                    return []
        except:
            time.sleep(1)
    return None

def process_kepco_day_data(day_list, target_meter_no):
    """수신된 데이터를 96개(15분 단위) 배열로 파싱 및 필터링합니다."""
    interval_usage = [0.0] * 96
    if not day_list: 
        return interval_usage
    
    for item in day_list:
        meter_no = item.get("meterNo", "")
        for k, v in item.items():
            if k.startswith("pwr_qty") and k != "pwr_qty":
                try:
                    val = float(v)
                    # 1호선처럼 모수용 계량기가 여러 개인 경우 타겟 계량기만 필터링
                    if target_meter_no in ["", "전체"] or meter_no == target_meter_no:
                        time_str = k[-4:]
                        hh = int(time_str[:2])
                        mm = int(time_str[2:])
                        # 00시 15분(idx=0) ~ 24시 00분(idx=95)
                        idx = 95 if (hh == 24 and mm == 0) else hh * 4 + (mm // 15) - 1
                        if 0 <= idx < 96:
                            interval_usage[idx] += val
                except:
                    pass
    return interval_usage

def get_kepco_details_for_station(station: str, date_str: str):
    """단일 개소의 96개 시간대별 상세 전력량과 최대수요전력을 반환합니다."""
    c_no = STATION_CUST_MAP.get(station)
    if not c_no: return []
    
    target_meter_no = STATION_METER_MAP.get(station, "") if station in LINE_STATIONS['1호선'] else "전체"
    m_target = target_meter_no if c_no == '0526314773' else "전체"
    
    day_list = fetch_kepco_day_lp(c_no, date_str)
    if day_list is None: day_list = []
    
    int_u = process_kepco_day_data(day_list, m_target)
    
    details = []
    for m in range(96):
        hh = m // 4
        mm = (m % 4) * 15 + 15
        if mm == 60:
            hh += 1
            mm = 0
        details.append({
            "station": station,
            "date": date_str,
            "time": f"{hh:02d}:{mm:02d}",
            "usage_kwh": round(int_u[m], 1),
            "peak_kw": round(int_u[m] * 4, 1) # 15분 사용량 * 4 = 최대수요전력(kW)
        })
        
    return details

# =========================================================================
# 🚀 3. 메인 백업 실행 로직
# =========================================================================
def run_backup(start_date: str, end_date: str):
    print(f"\n[ DTRO 에너지 관제 백업 시스템 가동 ]")
    print(f"▶ 수집 기간: {start_date} ~ {end_date}")
    print(f"▶ 대상 개소: 총 {len(ALL_STATIONS)}개 역사 및 종합청사\n")
    
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    diff_days = (e_dt - s_dt).days + 1
    
    # 병렬 처리를 위한 (개소, 날짜) 작업 큐 생성
    tasks = []
    for station in ALL_STATIONS:
        for i in range(diff_days):
            d_str = (s_dt + timedelta(days=i)).strftime("%Y-%m-%d")
            tasks.append((station, d_str))
            
    print(f"총 {len(tasks)}개의 일일 데이터를 한전 서버에 요청합니다. (소요 시간 1~2분 대기 요망...)\n")
    
    all_backup_records = []
    
    def process_task(task):
        station, date_str = task
        try:
            return get_kepco_details_for_station(station, date_str)
        except Exception as e:
            print(f"[{station}] {date_str} 수집 실패: {e}")
            return []

    # 병목 방지를 위해 ThreadPool 스레드 15개로 제한하여 스크래핑
    with ThreadPoolExecutor(max_workers=15) as executor:
        results = list(executor.map(process_task, tasks))
        
    for res in results:
        all_backup_records.extend(res)
        
    # 4. CSV 파일로 추출 (Pandas 활용)
    if all_backup_records:
        df = pd.DataFrame(all_backup_records)
        # 데이터가 없는 날짜(모두 0인 경우)도 뼈대 확보를 위해 남겨두되, 
        # 필요 시 df = df[df['usage_kwh'] > 0] 로 필터링 가능합니다.
        
        file_name = f"DTRO_KEPCO_BACKUP_{start_date.replace('-','')}_{end_date.replace('-','')}.csv"
        
        # utf-8-sig로 저장하여 엑셀에서 한글 깨짐 방지
        df.to_csv(file_name, index=False, encoding='utf-8-sig')
        print(f"✅ 백업 완료! 총 {len(df):,}행의 15분 단위 전력량 데이터가 추출되었습니다.")
        print(f"📁 저장 파일명: {file_name}\n")
    else:
        print("❌ 수집된 데이터가 없습니다. 날짜 및 한전 API 상태를 확인하세요.")

if __name__ == "__main__":
    # 실행 시 기본 수집 범위: 2026-09-04 부터 오늘 날짜 전날까지 (당일은 실시간이므로 전날 마감 데이터까지 백업)
    start_date_str = "2026-09-04"
    
    yesterday = datetime.now() - timedelta(days=1)
    end_date_str = yesterday.strftime("%Y-%m-%d")
    
    # 만약 오늘 날짜로 테스트하시려면 아래 주석을 풀고 사용하세요.
    end_date_str = datetime.now().strftime("%Y-%m-%d")
    
    # 백업 실행
    run_backup(start_date_str, end_date_str)