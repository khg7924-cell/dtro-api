import tkinter as tk
from tkinter import ttk, messagebox, font
import requests
import math
from datetime import datetime, timedelta
import os
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

# --- 한글 폰트 셋팅 (이모티콘은 깨지므로 일반 텍스트 사용) ---
if os.name == 'nt': plt.rc('font', family='Malgun Gothic')
elif os.name == 'posix': plt.rc('font', family='AppleGothic')
plt.rcParams['axes.unicode_minus'] = False

# --- 1. API 키 설정 ---
KMA_API_KEY = "4480c93a63159f09aebc2d0aa5ec7cff37503e60d6297b500e6da8d91e20f5cb"

# --- 2. 디자인 및 맵핑 ---
COLOR_BG = "#FDFDFD"
COLOR_CARD = "#FFFFFF"
COLOR_PRIMARY = "#4A90E2"
COLOR_HISTORY = "#9C27B0"
COLOR_TEXT_MAIN = "#333333"
COLOR_TEXT_SUB = "#888888"

def safe_format(val, suffix="", is_int=False):
    if val is None or val == "--" or val == "": return "--" + suffix
    try:
        if is_int: return f"{int(float(val))}{suffix}"
        return f"{float(val):.1f}{suffix}"
    except: return "--" + suffix

def get_weather_icon_and_desc(sky, pty):
    if pty == "0":
        if sky == "1": return "☀️", "맑음"
        elif sky == "3": return "⛅", "구름 많음"
        elif sky == "4": return "☁️", "흐림"
        else: return "☀️", "맑음"
    else:
        pty_map = {"1": ("🌧", "비"), "2": ("🌧❄️", "비/눈"), "3": ("🌨", "눈"), "4": ("🌧", "소나기"), "5": ("🌦", "빗방울"), "6": ("🌦🌨", "빗방울/눈날림"), "7": ("❄️", "눈날림")}
        return pty_map.get(pty, ("❓", "정보 없음"))

WMO_MAP = {
    0: "☀️ 맑음", 1: "🌤 대체로 맑음", 2: "⛅ 구름 조금", 3: "☁️ 흐림",
    45: "🌫 안개", 48: "🌫 안개", 51: "🌦 이슬비", 53: "🌦 이슬비", 55: "🌦 강한 이슬비",
    61: "🌧 비 조금", 63: "🌧 비", 65: "🌧 강한 비",
    71: "🌨 눈 조금", 73: "🌨 눈", 75: "🌨 강한 눈",
    80: "🌦 소나기", 81: "🌧 소나기", 82: "🌧 강한 소나기",
    95: "⛈ 뇌우", 96: "⛈ 뇌우/우박", 99: "⛈ 강한 뇌우"
}

def get_pm_status(val, dust_type="pm25"):
    try:
        v = float(val)
        if dust_type == "pm10":
            if v <= 30: return "좋음", "#4A90E2"
            elif v <= 80: return "보통", "#4CAF50"
            elif v <= 150: return "나쁨", "#F5A623"
            else: return "매우 나쁨", "#FF5252"
        else:
            if v <= 15: return "좋음", "#4A90E2"
            elif v <= 35: return "보통", "#4CAF50"
            elif v <= 75: return "나쁨", "#F5A623"
            else: return "매우 나쁨", "#FF5252"
    except: return "정보 없음", COLOR_TEXT_SUB

# --- 3. 좌표 변환 ---
def map_to_grid(lat, lon):
    RE = 6371.00877; GRID = 5.0; SLAT1 = 30.0; SLAT2 = 60.0; OLON = 126.0; OLAT = 38.0; XO = 43; YO = 136
    DEGRAD = math.pi / 180.0; re = RE / GRID
    slat1 = SLAT1 * DEGRAD; slat2 = SLAT2 * DEGRAD; olon = OLON * DEGRAD; olat = OLAT * DEGRAD
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5); sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5); ro = re * sf / math.pow(ro, sn)
    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5); ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi: theta -= 2.0 * math.pi
    if theta < -math.pi: theta += 2.0 * math.pi
    theta *= sn
    return math.floor(ra * math.sin(theta) + XO + 0.5), math.floor(ro - ra * math.cos(theta) + YO + 0.5)

# --- 4. API 통신 함수 ---
def get_coordinates(location_name):
    url = f"https://nominatim.openstreetmap.org/search?q={location_name}&format=json&limit=1&countrycodes=kr"
    headers = {'User-Agent': 'KoreanMultiWeatherApp/8.0'} 
    try:
        res = requests.get(url, headers=headers, timeout=5).json()
        if len(res) > 0:
            result = res[0]
            display_name = result.get('display_name', location_name).replace(", 대한민국", "").replace("대한민국, ", "")
            parts = [p.strip() for p in display_name.split(',')]
            if len(parts) > 1: display_name = f"{parts[-1]} {parts[0]}"
            return float(result["lat"]), float(result["lon"]), display_name
        return None, None, "검색 결과 없음"
    except: return None, None, "네트워크 오류"

def get_current_hybrid_weather(lat, lon):
    nx, ny = map_to_grid(lat, lon)
    now = datetime.now()
    fcst_now = now - timedelta(hours=1) if now.minute < 45 else now
    fcst_date, fcst_time = fcst_now.strftime("%Y%m%d"), fcst_now.strftime("%H30")

    if now.hour < 2 or (now.hour == 2 and now.minute < 10):
        vil_date, vil_time, target_date = (now - timedelta(days=1)).strftime("%Y%m%d"), "2300", now.strftime("%Y%m%d")
    else:
        vil_date, vil_time, target_date = now.strftime("%Y%m%d"), "0200", now.strftime("%Y%m%d")

    url_fcst = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtFcst"
    url_vil = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
    url_air = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm10,pm2_5&timezone=Asia%2FSeoul"
    
    params_fcst = {"serviceKey": KMA_API_KEY, "pageNo": "1", "numOfRows": "100", "dataType": "JSON", "base_date": fcst_date, "base_time": fcst_time, "nx": nx, "ny": ny}
    params_vil = {"serviceKey": KMA_API_KEY, "pageNo": "1", "numOfRows": "300", "dataType": "JSON", "base_date": vil_date, "base_time": vil_time, "nx": nx, "ny": ny}

    try:
        res_fcst = requests.get(url_fcst, params=params_fcst, timeout=5).json()
        res_vil = requests.get(url_vil, params=params_vil, timeout=5).json()
        res_air = requests.get(url_air, timeout=5).json()
        
        if 'response' not in res_fcst or 'items' not in res_fcst['response'].get('body', {}):
            return {"error": "기상청 서버 지연. 잠시 후 시도하세요."}

        items_f = res_fcst['response']['body']['items']['item']
        items_v = res_vil['response']['body']['items']['item'] if 'body' in res_vil.get('response', {}) else []
        current_air = res_air.get('current', {})

        weather = {"temp": "--", "humidity": "--", "sky": "1", "pty": "0", "max": "--", "min": "--", "pm10": current_air.get("pm10", "--"), "pm25": current_air.get("pm2_5", "--")}

        found = {"T1H": False, "REH": False, "SKY": False, "PTY": False}
        for item in items_f:
            cat, val = item['category'], item['fcstValue']
            if cat in found and not found[cat]:
                if cat == 'T1H': weather["temp"] = val
                elif cat == 'REH': weather["humidity"] = val
                elif cat == 'SKY': weather["sky"] = val
                elif cat == 'PTY': weather["pty"] = val
                found[cat] = True

        for item in items_v:
            if item['fcstDate'] == target_date:
                if item['category'] == 'TMX': weather["max"] = item['fcstValue']
                elif item['category'] == 'TMN': weather["min"] = item['fcstValue']
        return weather
    except: return None

def get_historical_range_weather(lat, lon, start_date, end_date):
    url_weather = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}&daily=weather_code,temperature_2m_max,temperature_2m_min&hourly=relative_humidity_2m&timezone=Asia%2FSeoul"
    url_air = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}&hourly=pm10,pm2_5&timezone=Asia%2FSeoul"

    try:
        w_res = requests.get(url_weather, timeout=8).json()
        a_res = requests.get(url_air, timeout=8).json()

        daily = w_res.get("daily", {})
        hourly_w = w_res.get("hourly", {})
        hourly_a = a_res.get("hourly", {})

        dates = daily.get("time", [])
        max_t = daily.get("temperature_2m_max", [])
        min_t = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])

        h_hum = hourly_w.get("relative_humidity_2m", [])
        a_pm10 = hourly_a.get("pm10", [])
        a_pm25 = hourly_a.get("pm2_5", [])

        results = []
        for i, d in enumerate(dates):
            start_idx = i * 24; end_idx = start_idx + 24
            day_hum = [x for x in h_hum[start_idx:end_idx] if x is not None]
            avg_hum = sum(day_hum) / len(day_hum) if day_hum else None

            avg_pm10, avg_pm25 = None, None
            if len(a_pm10) >= end_idx:
                d_pm10 = [x for x in a_pm10[start_idx:end_idx] if x is not None]
                d_pm25 = [x for x in a_pm25[start_idx:end_idx] if x is not None]
                avg_pm10 = sum(d_pm10) / len(d_pm10) if d_pm10 else None
                avg_pm25 = sum(d_pm25) / len(d_pm25) if d_pm25 else None

            results.append({
                "date": d, "code": codes[i] if len(codes) > i else None,
                "max": max_t[i] if len(max_t) > i else None, "min": min_t[i] if len(min_t) > i else None,
                "hum": avg_hum, "pm10": avg_pm10, "pm25": avg_pm25
            })
        return results
    except Exception: return None

# --- 5. GUI 제어 로직 ---
current_chart_data = []
current_is_monthly = False
chart_canvas = None
fig = Figure(figsize=(6, 2.5), dpi=100)

# 전역 변수: 다수의 하이라이트 포인트를 담을 리스트
annot = None
highlight_points = [] 

def update_current_display(event=None):
    loc_name = entry_loc_current.get().strip()
    if not loc_name: return
    lbl_status_curr.config(text="데이터 수집 중...", fg=COLOR_TEXT_SUB); window.update()

    lat, lon, full_name = get_coordinates(loc_name)
    if lat is None: lbl_status_curr.config(text=f"❌ '{loc_name}' 지역을 찾을 수 없습니다.", fg="#FF5252"); return

    weather = get_current_hybrid_weather(lat, lon)
    if weather is None or "error" in weather:
        lbl_status_curr.config(text="❌ 서버 오류", fg="#FF5252"); return

    lbl_status_curr.config(text=f"✅ {full_name} 실시간 수신 완료", fg="#4CAF50")
    lbl_loc_title_curr.config(text=full_name)
    lbl_temp_curr.config(text=safe_format(weather['temp'], "°"))
    lbl_temp_mixmax.config(text=f"최저 {safe_format(weather['min'], '°')} / 최고 {safe_format(weather['max'], '°')}")
    lbl_humidity.config(text=f"습도 {safe_format(weather['humidity'], '%', is_int=True)}")

    icon, desc = get_weather_icon_and_desc(weather['sky'], weather['pty'])
    lbl_weather_icon.config(text=icon); lbl_weather_desc.config(text=desc)

    p10_txt, p10_col = get_pm_status(weather['pm10'], "pm10")
    lbl_pm10_val.config(text=safe_format(weather['pm10'])); lbl_pm10_status.config(text=p10_txt, fg=p10_col)
    p25_txt, p25_col = get_pm_status(weather['pm25'], "pm25")
    lbl_pm25_val.config(text=safe_format(weather['pm25'])); lbl_pm25_status.config(text=p25_txt, fg=p25_col)

# 🎯 스마트 스냅 & 다중 하이라이트 이벤트 함수
def on_chart_click(event):
    global annot, highlight_points
    if not event.inaxes or annot is None: return

    idx = int(round(event.xdata))
    
    if 0 <= idx < len(current_chart_data):
        d = current_chart_data[idx]
        ctype = chart_type_var.get()
        
        # 1. 기존에 그려진 하이라이트 마커들 지우기
        for p in highlight_points:
            try: p.remove()
            except: pass
        highlight_points.clear()

        # 2. 현재 선택된 차트에 해당하는 모든 y값 수집 (기온이면 max, min 둘 다)
        y_values = []
        if ctype == "temp":
            if d['max'] is not None: y_values.append(float(d['max']))
            if d['min'] is not None: y_values.append(float(d['min']))
        elif ctype == "hum":
            if d['hum'] is not None: y_values.append(float(d['hum']))
        elif ctype == "pm":
            if d['pm10'] is not None: y_values.append(float(d['pm10']))
            if d['pm25'] is not None: y_values.append(float(d['pm25']))

        if not y_values: return

        # 3. 말풍선이 뜰 '가장 높은 y값' 찾기
        highest_y = max(y_values)

        # 4. 수집된 모든 y값 위치에 동그란 강조 마커 그리기
        for y in y_values:
            p = event.inaxes.plot(idx, y, marker='o', markersize=12, 
                                  markeredgecolor='#E91E63', markerfacecolor='#FCE4EC', 
                                  markeredgewidth=2.5, zorder=5)[0]
            highlight_points.append(p)

        # 5. 말풍선 텍스트 작성 (폰트 깨짐을 방지하기 위해 이모티콘을 한글로 변경)
        txt = (f"[날짜] {d['label']}\n"
               f"[기온] 최고 {safe_format(d['max'],'°C')} | 최저 {safe_format(d['min'],'°C')}\n"
               f"[습도] {safe_format(d['hum'],'%',True)}\n"
               f"[먼지] PM10 {safe_format(d['pm10'],'',True)} | PM2.5 {safe_format(d['pm25'],'',True)}")

        # 6. 말풍선을 가장 높은 포인트(highest_y) 기준으로 세팅
        annot.xy = (idx, highest_y)
        annot.set_text(txt)
        
        # 항상 위쪽 약간 우측으로 고정 (10, 15 포인트 만큼 이동)
        annot.set_position((10, 15))
        annot.set_visible(True)

        chart_canvas.draw_idle()


def redraw_chart():
    global chart_canvas, annot, highlight_points
    if not current_chart_data: return

    # 차트를 다시 그릴 땐 하이라이트 리스트 초기화
    highlight_points = []

    fig.clear()
    ax = fig.add_subplot(111)
    
    ctype = chart_type_var.get()
    labels = [d['label'] for d in current_chart_data]

    if ctype == "temp":
        max_t = [d['max'] for d in current_chart_data]
        min_t = [d['min'] for d in current_chart_data]
        ax.plot(labels, max_t, marker='o', color='#FF5252', label='최고기온', linewidth=2, picker=5)
        ax.plot(labels, min_t, marker='o', color='#4A90E2', label='최저기온', linewidth=2, picker=5)
        ax.set_ylabel("온도 (°C)")
        title_str = "기온 변화"

    elif ctype == "hum":
        hum = [d['hum'] for d in current_chart_data]
        ax.bar(labels, hum, color='#26A69A', alpha=0.6, label='일/월 평균 습도', picker=5)
        ax.plot(labels, hum, marker='o', color='#00695C', linewidth=1)
        ax.set_ylabel("습도 (%)")
        title_str = "습도 변화"

    elif ctype == "pm":
        pm10 = [d['pm10'] for d in current_chart_data]
        pm25 = [d['pm25'] for d in current_chart_data]
        ax.plot(labels, pm10, marker='s', color='#FFA726', label='PM10', linewidth=2, picker=5)
        ax.plot(labels, pm25, marker='^', color='#8D6E63', label='PM2.5', linewidth=2, picker=5)
        ax.set_ylabel("농도 (㎍/㎥)")
        title_str = "대기질 변화"

    time_label = "월평균" if current_is_monthly else "일별"
    ax.set_title(f"[{time_label}] {title_str} (그래프를 클릭해 상세 정보 확인)", fontsize=10, fontweight='bold')
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(True, linestyle='--', alpha=0.4)
    ax.tick_params(axis='x', rotation=45, labelsize=8)
    fig.tight_layout()

    # 화살표(arrowprops)를 없애고 텍스트 박스만 생성
    annot = ax.annotate("", xy=(0,0), xytext=(10, 15), textcoords="offset points",
                        bbox=dict(boxstyle="round,pad=0.5", fc="#FFFDE7", ec="#FBC02D", lw=1.5, alpha=0.9),
                        fontsize=9, zorder=10)
    annot.set_visible(False)

    if chart_canvas: chart_canvas.get_tk_widget().destroy()
    chart_canvas = FigureCanvasTkAgg(fig, master=chart_display_frame)
    
    chart_canvas.mpl_connect('button_press_event', on_chart_click)
    
    chart_canvas.draw()
    chart_canvas.get_tk_widget().pack(fill="both", expand=True)

def update_history_display(event=None):
    global current_chart_data, current_is_monthly
    loc_name = entry_loc_hist.get().strip()
    start_str = entry_start_date.get().strip()
    end_str = entry_end_date.get().strip()
    
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d")
        end_date = datetime.strptime(end_str, "%Y-%m-%d")
    except ValueError:
        lbl_status_hist.config(text="❌ 날짜를 YYYY-MM-DD 형식으로 정확히 입력하세요.", fg="#FF5252"); return

    lbl_status_hist.config(text="데이터 수집 및 차트 렌더링 중...", fg=COLOR_TEXT_SUB); window.update()

    lat, lon, full_name = get_coordinates(loc_name)
    if lat is None: lbl_status_hist.config(text="❌ 지역을 찾을 수 없습니다.", fg="#FF5252"); return

    history_data = get_historical_range_weather(lat, lon, start_str, end_str)
    for item in tree.get_children(): tree.delete(item)
    if not history_data:
        lbl_status_hist.config(text="❌ 해당 기간의 데이터를 불러올 수 없습니다.", fg="#FF5252"); return

    lbl_status_hist.config(text=f"✅ {full_name} 검색 완료", fg="#4CAF50")
    
    for day in history_data:
        w_desc = WMO_MAP.get(day['code'], "알 수 없음")
        tree.insert("", "end", values=(
            day['date'], w_desc, safe_format(day['max']), safe_format(day['min']),
            safe_format(day['hum'], is_int=True), safe_format(day['pm10'], is_int=True), safe_format(day['pm25'], is_int=True)
        ))
        
    days_diff = (end_date - start_date).days
    current_is_monthly = days_diff > 60
    current_chart_data = []

    if current_is_monthly:
        m_groups = {}
        for d in history_data:
            m_key = d['date'][:7] # YYYY-MM
            if m_key not in m_groups: m_groups[m_key] = {'max':[], 'min':[], 'hum':[], 'pm10':[], 'pm25':[]}
            if d['max'] is not None: m_groups[m_key]['max'].append(float(d['max']))
            if d['min'] is not None: m_groups[m_key]['min'].append(float(d['min']))
            if d['hum'] is not None: m_groups[m_key]['hum'].append(float(d['hum']))
            if d['pm10'] is not None: m_groups[m_key]['pm10'].append(float(d['pm10']))
            if d['pm25'] is not None: m_groups[m_key]['pm25'].append(float(d['pm25']))

        for m_key, vals in m_groups.items():
            current_chart_data.append({
                'label': m_key[2:], # YY-MM
                'max': sum(vals['max'])/len(vals['max']) if vals['max'] else 0,
                'min': sum(vals['min'])/len(vals['min']) if vals['min'] else 0,
                'hum': sum(vals['hum'])/len(vals['hum']) if vals['hum'] else 0,
                'pm10': sum(vals['pm10'])/len(vals['pm10']) if vals['pm10'] else 0,
                'pm25': sum(vals['pm25'])/len(vals['pm25']) if vals['pm25'] else 0,
            })
    else:
        for d in history_data:
            current_chart_data.append({
                'label': d['date'][5:], # MM-DD
                'max': float(d['max']) if d['max'] is not None else 0,
                'min': float(d['min']) if d['min'] is not None else 0,
                'hum': float(d['hum']) if d['hum'] is not None else 0,
                'pm10': float(d['pm10']) if d['pm10'] is not None else 0,
                'pm25': float(d['pm25']) if d['pm25'] is not None else 0,
            })

    redraw_chart()

# --- 6. UI 화면 ---
window = tk.Tk()
window.title("종합 기상 데이터센터 프로")
window.geometry("750x850") 
window.configure(bg=COLOR_BG)

style = ttk.Style()
style.theme_use('default')
style.configure('TNotebook.Tab', padding=[20, 5], font=('Segoe UI', 10, 'bold'))
style.configure("Treeview", font=('Segoe UI', 9), rowheight=25)
style.configure("Treeview.Heading", font=('Segoe UI', 10, 'bold'))

notebook = ttk.Notebook(window)
notebook.pack(fill="both", expand=True, padx=10, pady=10)

tab_current = tk.Frame(notebook, bg=COLOR_BG)
tab_history = tk.Frame(notebook, bg=COLOR_BG)

notebook.add(tab_current, text="🌤 실시간 기상/대기질")
notebook.add(tab_history, text="📅 분석 차트 및 과거 조회")

# ==========================================
# [탭 1] 실시간 화면
# ==========================================
search_frame_c = tk.Frame(tab_current, bg=COLOR_BG, pady=10)
search_frame_c.pack(fill="x", padx=10)
entry_loc_current = tk.Entry(search_frame_c, font=("Segoe UI", 12), bd=0, highlightthickness=1)
entry_loc_current.insert(0, "대구 수창동")
entry_loc_current.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 10))
entry_loc_current.bind('<Return>', update_current_display)
tk.Button(search_frame_c, text="검색", command=update_current_display, font=("Segoe UI", 10), bg=COLOR_PRIMARY, fg="white", bd=0, padx=15, pady=5).pack(side="right")

lbl_status_curr = tk.Label(tab_current, text="기상청 실시간 정보 대기 중...", font=("Segoe UI", 9), bg=COLOR_BG, fg=COLOR_TEXT_SUB)
lbl_status_curr.pack(pady=(0, 5))

card_c = tk.Frame(tab_current, bg=COLOR_CARD, padx=20, pady=20, highlightthickness=1, highlightbackground="#EEEEEE")
card_c.pack(padx=20, pady=10, fill="both", expand=True)

lbl_loc_title_curr = tk.Label(card_c, text="지역 대기 중", font=("Segoe UI", 18, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN, anchor="w")
lbl_loc_title_curr.pack(fill="x", pady=(0, 15))

w_frame_c = tk.Frame(card_c, bg=COLOR_CARD)
w_frame_c.pack(fill="x")
lbl_weather_icon = tk.Label(w_frame_c, text="❓", font=("Segoe UI", 60), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN)
lbl_weather_icon.pack(side="left", padx=(0, 20))
t_frame_c = tk.Frame(w_frame_c, bg=COLOR_CARD)
t_frame_c.pack(side="left")
lbl_temp_curr = tk.Label(t_frame_c, text="--°", font=("Segoe UI Light", 48), bg=COLOR_CARD, fg=COLOR_PRIMARY)
lbl_temp_curr.pack(anchor="w")
lbl_weather_desc = tk.Label(t_frame_c, text="--", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN)
lbl_weather_desc.pack(anchor="w")

tk.Frame(card_c, height=1, bg="#EEEEEE").pack(fill="x", pady=15)
lbl_temp_mixmax = tk.Label(card_c, text="최저 --° / 최고 --°", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN)
lbl_temp_mixmax.pack(anchor="w")
lbl_humidity = tk.Label(card_c, text="습도 --%", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB)
lbl_humidity.pack(anchor="w", pady=(5, 15))

tk.Frame(card_c, height=1, bg="#EEEEEE").pack(fill="x", pady=10)
tk.Label(card_c, text="대기질 (미세먼지)", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN).pack(anchor="w", pady=(0, 5))

pm10_frame = tk.Frame(card_c, bg=COLOR_CARD)
pm10_frame.pack(fill="x", pady=2)
tk.Label(pm10_frame, text="PM10", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB, width=8, anchor="w").pack(side="left")
lbl_pm10_val = tk.Label(pm10_frame, text="--", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN, width=6, anchor="e"); lbl_pm10_val.pack(side="left")
tk.Label(pm10_frame, text="㎍/㎥", font=("Segoe UI", 10), bg=COLOR_CARD, fg=COLOR_TEXT_SUB).pack(side="left", padx=(2, 10))
lbl_pm10_status = tk.Label(pm10_frame, text="--", font=("Segoe UI", 11, "bold"), bg=COLOR_CARD)
lbl_pm10_status.pack(side="left")

pm25_frame = tk.Frame(card_c, bg=COLOR_CARD)
pm25_frame.pack(fill="x", pady=2)
tk.Label(pm25_frame, text="PM2.5", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB, width=8, anchor="w").pack(side="left")
lbl_pm25_val = tk.Label(pm25_frame, text="--", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN, width=6, anchor="e"); lbl_pm25_val.pack(side="left")
tk.Label(pm25_frame, text="㎍/㎥", font=("Segoe UI", 10), bg=COLOR_CARD, fg=COLOR_TEXT_SUB).pack(side="left", padx=(2, 10))
lbl_pm25_status = tk.Label(pm25_frame, text="--", font=("Segoe UI", 11, "bold"), bg=COLOR_CARD)
lbl_pm25_status.pack(side="left")


# ==========================================
# [탭 2] 분석 차트 및 과거 조회
# ==========================================
search_frame_h = tk.Frame(tab_history, bg=COLOR_BG, pady=10)
search_frame_h.pack(fill="x", padx=10)

loc_date_frame = tk.Frame(search_frame_h, bg=COLOR_BG)
loc_date_frame.pack(fill="x", pady=(0, 10))

tk.Label(loc_date_frame, text="지역:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="left")
entry_loc_hist = tk.Entry(loc_date_frame, font=("Segoe UI", 11), bd=0, highlightthickness=1, width=15)
entry_loc_hist.insert(0, "대구 수창동")
entry_loc_hist.pack(side="left", ipady=5, padx=(5, 10))

now = datetime.now()
one_year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")
yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

tk.Label(loc_date_frame, text="기간:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="left")
entry_start_date = tk.Entry(loc_date_frame, font=("Segoe UI", 11), bd=0, highlightthickness=1, width=12)
entry_start_date.insert(0, one_year_ago)
entry_start_date.pack(side="left", ipady=5, padx=5)

tk.Label(loc_date_frame, text="~", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="left")
entry_end_date = tk.Entry(loc_date_frame, font=("Segoe UI", 11), bd=0, highlightthickness=1, width=12)
entry_end_date.insert(0, yesterday)
entry_end_date.pack(side="left", ipady=5, padx=5)

tk.Button(search_frame_h, text="데이터 분석 시작", command=update_history_display, font=("Segoe UI", 11, "bold"), bg=COLOR_HISTORY, fg="white", bd=0, pady=5).pack(fill="x")

lbl_status_hist = tk.Label(tab_history, text="60일 이상의 기간을 검색하면 자동으로 월평균 데이터로 압축됩니다.", font=("Segoe UI", 9), bg=COLOR_BG, fg=COLOR_TEXT_SUB)
lbl_status_hist.pack(pady=(0, 5))

# 1. 표 구역
tree_frame = tk.Frame(tab_history, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD")
tree_frame.pack(fill="x", padx=10, pady=5)
tree_scroll = tk.Scrollbar(tree_frame)
tree_scroll.pack(side="right", fill="y")
cols = ("date", "weather", "max", "min", "hum", "pm10", "pm25")
tree = ttk.Treeview(tree_frame, columns=cols, show="headings", yscrollcommand=tree_scroll.set, height=6)
tree.heading("date", text="날짜"); tree.heading("weather", text="날씨")
tree.heading("max", text="최고(°C)"); tree.heading("min", text="최저(°C)")
tree.heading("hum", text="습도(%)"); tree.heading("pm10", text="PM10"); tree.heading("pm25", text="PM2.5")
tree.column("date", width=90, anchor="center"); tree.column("weather", width=110, anchor="center")
tree.column("max", width=70, anchor="center"); tree.column("min", width=70, anchor="center")
tree.column("hum", width=70, anchor="center"); tree.column("pm10", width=60, anchor="center"); tree.column("pm25", width=60, anchor="center")
tree.pack(fill="x")
tree_scroll.config(command=tree.yview)

# 2. 그래프 컨트롤 버튼
chart_ctrl_frame = tk.Frame(tab_history, bg=COLOR_BG)
chart_ctrl_frame.pack(fill="x", padx=15, pady=(15, 5))

tk.Label(chart_ctrl_frame, text="📊 시각화 선택:", font=("Segoe UI", 10, "bold"), bg=COLOR_BG).pack(side="left", padx=(0, 10))

chart_type_var = tk.StringVar(value="temp")
tk.Radiobutton(chart_ctrl_frame, text="기온", variable=chart_type_var, value="temp", command=redraw_chart, bg=COLOR_BG, font=("Segoe UI", 10)).pack(side="left", padx=5)
tk.Radiobutton(chart_ctrl_frame, text="습도", variable=chart_type_var, value="hum", command=redraw_chart, bg=COLOR_BG, font=("Segoe UI", 10)).pack(side="left", padx=5)
tk.Radiobutton(chart_ctrl_frame, text="먼지", variable=chart_type_var, value="pm", command=redraw_chart, bg=COLOR_BG, font=("Segoe UI", 10)).pack(side="left", padx=5)

lbl_chart_detail = tk.Label(chart_ctrl_frame, text="👈 그래프를 클릭하면 세부 수치가 표시됩니다.", font=("Segoe UI", 10, "bold"), bg=COLOR_BG, fg=COLOR_PRIMARY)
lbl_chart_detail.pack(side="right", padx=10)

# 3. 그래프 구역
chart_display_frame = tk.Frame(tab_history, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD")
chart_display_frame.pack(fill="both", expand=True, padx=10, pady=(0, 20))

update_current_display()
window.mainloop()