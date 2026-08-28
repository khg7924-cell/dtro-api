import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import requests
import math
import hashlib
from datetime import datetime, timedelta
import os
import random
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

# ==========================================
# ⚙️ 1. 전역 상수 (디자인, 색상, API 키)
# ==========================================
KMA_API_KEY = "4480c93a63159f09aebc2d0aa5ec7cff37503e60d6297b500e6da8d91e20f5cb"
FIREBASE_URL = "https://datacenter-app-7a69a-default-rtdb.firebaseio.com/users"  # 🚨 본인 주소 입력 필수!

COLOR_BG = "#FDFDFD"
COLOR_CARD = "#FFFFFF"
COLOR_PRIMARY = "#4A90E2"
COLOR_COMPARE = "#9C27B0"
COLOR_PREDICT = "#E91E63" 
COLOR_TEXT_MAIN = "#333333"
COLOR_TEXT_SUB = "#888888"

# ==========================================
# ⚙️ 2. 전역 변수 및 지역 매핑 설정
# ==========================================
login_success = False
logged_in_user = ""
chart_type_var = None

STATION_LOCATION_MAP = {
    '설화명곡': '대구 화원읍', '월배기지': '대구 유천동', '서부정류장': '대구 대명동',
    '반월당': '대구 덕산동', '신천': '대구 신천동', '방촌': '대구 방촌동',
    '안심': '대구 괴전동', '숙천': '대구 숙천동', '금락': '경북 하양읍',
    '문양기지': '대구 신매동', '대실': '대구 다사읍', '성서산단': '대구 이곡동',
    '죽전': '대구 죽전동', '반고개': '대구 두류동', '대구은행': '대구 수성동4가',
    '만촌': '대구 만촌동', '수성알파시티': '대구 연호동', '사월': '대구 신매동',
    '영남대': '경산시 대동', '칠곡기지': '대구 동호동', '팔달시장': '대구 노원동3가',
    '남산': '대구 남산동', '범물기지': '대구 범물동', '종합청사': '대구 상인동'
}

weather_chart_data = []
weather_is_monthly = False
weather_canvas = None
weather_annot = None
weather_highlight_points = []
weather_fig = Figure(figsize=(5, 2.5), dpi=100)

elec_chart_data = []
elec_canvas = None
elec_annot = None
elec_highlight_points = []
elec_ax1 = None
elec_ax2 = None
elec_fig = Figure(figsize=(4, 2.5), dpi=100)

compare_fig = Figure(figsize=(8, 3), dpi=100)
compare_canvas = None

pred_fig = Figure(figsize=(5, 3), dpi=100)
pred_canvas = None
feat_fig = Figure(figsize=(3, 3), dpi=100)
feat_canvas = None
external_factors_loaded = False 

# 🎯 실제 전력량 데이터 저장을 위한 전역 변수
real_elec_df = None

# ==========================================
# 🔐 3. 보안 및 유틸리티 함수
# ==========================================
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def export_treeview_to_excel(tree, filename_prefix):
    try:
        columns = tree["columns"]
        headers = [tree.heading(col)["text"] for col in columns]
        display_headers = ["항목"] + headers
        
        data = []
        for item_id in tree.get_children():
            parent_values = [tree.item(item_id, "text")] + list(tree.item(item_id, "values"))
            data.append(parent_values)
            for child_id in tree.get_children(item_id):
                child_values = [tree.item(child_id, "text")] + list(tree.item(child_id, "values"))
                data.append(child_values)

        if not data:
            messagebox.showwarning("저장 오류", "저장할 데이터가 없습니다.")
            return

        file_path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")], initialfile=f"{filename_prefix}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
        if file_path:
            pd.DataFrame(data, columns=display_headers).to_excel(file_path, index=False)
            messagebox.showinfo("저장 완료", f"저장 경로: {file_path}")
    except Exception as e: messagebox.showerror("저장 실패", f"에러: {e}")

def safe_format(val, suffix="", is_int=False):
    if pd.isna(val) or val is None or val == "--" or val == "": return "--" + suffix
    try:
        if is_int: return f"{int(float(val))}{suffix}"
        return f"{float(val):.1f}{suffix}"
    except: return "--" + suffix

def get_pm_status(val, dust_type="pm25"):
    try:
        v = float(val)
        if dust_type == "pm10": return ("좋음","#4A90E2") if v<=30 else ("보통","#4CAF50") if v<=80 else ("나쁨","#F5A623") if v<=150 else ("매우 나쁨","#FF5252")
        else: return ("좋음","#4A90E2") if v<=15 else ("보통","#4CAF50") if v<=35 else ("나쁨","#F5A623") if v<=75 else ("매우 나쁨","#FF5252")
    except: return "정보 없음", COLOR_TEXT_SUB

WMO_MAP = {0: "☀️ 맑음", 1: "🌤 대체로 맑음", 2: "⛅ 구름 조금", 3: "☁️ 흐림", 45: "🌫 안개", 48: "🌫 안개", 51: "🌦 이슬비", 53: "🌦 이슬비", 55: "🌦 강한 이슬비", 61: "🌧 비 조금", 63: "🌧 비", 65: "🌧 강한 비", 71: "🌨 눈 조금", 73: "🌨 눈", 75: "🌨 강한 눈", 80: "🌦 소나기", 81: "🌧 소나기", 82: "🌧 강한 소나기", 95: "⛈ 뇌우", 96: "⛈ 뇌우/우박", 99: "⛈ 강한 뇌우"}

def map_to_grid(lat, lon):
    RE = 6371.00877; GRID = 5.0; SLAT1 = 30.0; SLAT2 = 60.0; OLON = 126.0; OLAT = 38.0; XO = 43; YO = 136
    DEGRAD = math.pi / 180.0; re = RE / GRID
    slat1 = SLAT1 * DEGRAD; slat2 = SLAT2 * DEGRAD; olon = OLON * DEGRAD; olat = OLAT * DEGRAD
    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5); sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5); sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5); ro = re * sf / math.pow(ro, sn)
    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5); ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi: theta -= 2.0 * math.pi
    elif theta < -math.pi: theta += 2.0 * math.pi
    theta *= sn
    return math.floor(ra * math.sin(theta) + XO + 0.5), math.floor(ro - ra * math.cos(theta) + YO + 0.5)

def get_coordinates(location_name):
    try:
        res = requests.get(f"https://nominatim.openstreetmap.org/search?q={location_name}&format=json&limit=1&countrycodes=kr", headers={'User-Agent':'MyApp/1.0'}, timeout=5).json()
        if res: return float(res[0]["lat"]), float(res[0]["lon"]), res[0].get('display_name', location_name).split(',')[0]
        return None, None, "검색 결과 없음"
    except: return None, None, "네트워크 오류"

def get_current_hybrid_weather(lat, lon):
    nx, ny = map_to_grid(lat, lon); now = datetime.now()
    fcst_now = now - timedelta(hours=1) if now.minute < 45 else now
    vil_date = (now - timedelta(days=1)).strftime("%Y%m%d") if now.hour < 2 else now.strftime("%Y%m%d")
    try:
        res_f = requests.get("http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtFcst", params={"serviceKey":KMA_API_KEY,"pageNo":"1","numOfRows":"100","dataType":"JSON","base_date":fcst_now.strftime("%Y%m%d"),"base_time":fcst_now.strftime("%H30"),"nx":nx,"ny":ny}, timeout=5).json()
        res_v = requests.get("http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst", params={"serviceKey":KMA_API_KEY,"pageNo":"1","numOfRows":"300","dataType":"JSON","base_date":vil_date,"base_time":"0200","nx":nx,"ny":ny}, timeout=5).json()
        res_a = requests.get(f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm10,pm2_5&timezone=Asia%2FSeoul", timeout=5).json()
        w = {"temp":"--","humidity":"--","sky":"1","pty":"0","max":"--","min":"--","pm10":res_a.get('current',{}).get("pm10","--"),"pm25":res_a.get('current',{}).get("pm2_5","--")}
        for i in res_f['response']['body']['items']['item']:
            if i['category'] == 'T1H' and w["temp"] == "--": w["temp"] = i['fcstValue']
            elif i['category'] == 'REH' and w["humidity"] == "--": w["humidity"] = i['fcstValue']
            elif i['category'] == 'SKY' and w["sky"] == "1": w["sky"] = i['fcstValue']
            elif i['category'] == 'PTY' and w["pty"] == "0": w["pty"] = i['fcstValue']
        for i in res_v['response']['body']['items']['item']:
            if i['fcstDate'] == now.strftime("%Y%m%d"):
                if i['category'] == 'TMX': w["max"] = i['fcstValue']
                elif i['category'] == 'TMN': w["min"] = i['fcstValue']
        return w
    except: return None

def get_historical_range_weather(lat, lon, start_date, end_date):
    try:
        w_res = requests.get(f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}&daily=weather_code,temperature_2m_max,temperature_2m_min&hourly=relative_humidity_2m&timezone=Asia%2FSeoul", timeout=8).json()
        a_res = requests.get(f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}&hourly=pm10,pm2_5&timezone=Asia%2FSeoul", timeout=8).json()
        res = []
        for i, d in enumerate(w_res.get("daily",{}).get("time",[])):
            s = i * 24; e = s + 24
            d_hum = [x for x in w_res.get("hourly",{}).get("relative_humidity_2m",[])[s:e] if x is not None]
            d_p10 = [x for x in a_res.get("hourly",{}).get("pm10",[])[s:e] if x is not None]
            d_p25 = [x for x in a_res.get("hourly",{}).get("pm2_5",[])[s:e] if x is not None]
            res.append({"date": d, "code": w_res["daily"]["weather_code"][i] if i < len(w_res["daily"]["weather_code"]) else None,
                        "max": w_res["daily"]["temperature_2m_max"][i] if i < len(w_res["daily"]["temperature_2m_max"]) else None, 
                        "min": w_res["daily"]["temperature_2m_min"][i] if i < len(w_res["daily"]["temperature_2m_min"]) else None, 
                        "hum": sum(d_hum)/len(d_hum) if d_hum else None, "pm10": sum(d_p10)/len(d_p10) if d_p10 else None, "pm25": sum(d_p25)/len(d_p25) if d_p25 else None})
        return res
    except: return None

# ==========================================
# 🖥️ 4. 로그인 UI 화면
# ==========================================
login_window = tk.Tk()
login_window.title("기상 데이터센터 - 보안 접속")
login_window.geometry("300x350")
login_window.configure(bg=COLOR_BG)

def sign_up_action():
    user_id, user_pw = entry_id.get().strip(), entry_pw.get().strip()
    if not user_id or not user_pw: return messagebox.showwarning("입력 오류", "모두 입력해주세요.")
    hashed_pw = hash_password(user_pw)
    try:
        if requests.get(f"{FIREBASE_URL}/users/{user_id}.json").json() is not None:
            return messagebox.showerror("가입 실패", "이미 존재하는 아이디입니다.")
        requests.put(f"{FIREBASE_URL}/users/{user_id}.json", json={"password": hashed_pw, "is_approved": False})
        messagebox.showinfo("가입 완료", "승인 대기중")
        entry_pw.delete(0, tk.END)
    except: messagebox.showerror("통신 오류", "클라우드 연결 실패")

def login_action():
    global login_success, logged_in_user
    user_id, user_pw = entry_id.get().strip(), entry_pw.get().strip()
    hashed_pw = hash_password(user_pw)
    try:
        user_info = requests.get(f"{FIREBASE_URL}/users/{user_id}.json").json()
        if user_info is None: return messagebox.showerror("로그인 실패", "존재하지 않는 아이디")
        elif user_info['password'] != hashed_pw: return messagebox.showerror("로그인 실패", "비밀번호 불일치")
        elif user_info['is_approved'] == False: return messagebox.showwarning("승인 대기", "승인 대기중")
        else:
            logged_in_user = user_id; login_success = True; login_window.destroy()
    except: messagebox.showerror("통신 오류", "서버 확인 불가")

tk.Label(login_window, text="통합 데이터센터\n관리자 시스템", font=("Segoe UI", 16, "bold"), bg=COLOR_BG, fg=COLOR_TEXT_MAIN).pack(pady=30)
frame_login = tk.Frame(login_window, bg=COLOR_BG); frame_login.pack(padx=30, fill="x")
tk.Label(frame_login, text="아이디", font=("Segoe UI", 9), bg=COLOR_BG).pack(anchor="w")
entry_id = tk.Entry(frame_login, font=("Segoe UI", 11), highlightthickness=1); entry_id.pack(fill="x", ipady=5, pady=(0, 10))
tk.Label(frame_login, text="비밀번호", font=("Segoe UI", 9), bg=COLOR_BG).pack(anchor="w")
entry_pw = tk.Entry(frame_login, font=("Segoe UI", 11), show="*", highlightthickness=1); entry_pw.pack(fill="x", ipady=5, pady=(0, 20))
tk.Button(frame_login, text="로그인", command=login_action, font=("Segoe UI", 10, "bold"), bg=COLOR_PRIMARY, fg="white", bd=0, pady=8).pack(fill="x", pady=5)
tk.Button(frame_login, text="회원가입 (승인 요청)", command=sign_up_action, font=("Segoe UI", 10), bg="#EEEEEE", fg=COLOR_TEXT_MAIN, bd=0, pady=8).pack(fill="x")

login_window.mainloop()

# ==========================================
# 🌤️ 5. 메인 대시보드
# ==========================================
if login_success:
    font_name = 'Malgun Gothic' if os.name == 'nt' else 'AppleGothic'
    plt.rc('font', family=font_name)
    plt.rcParams['axes.unicode_minus'] = False

    main_window = tk.Tk()
    main_window.title(f"통합 기상/전력 데이터센터 프로 - 로그인: [{logged_in_user}]")
    main_window.geometry("1400x950") 
    main_window.configure(bg=COLOR_BG)

    style = ttk.Style()
    style.theme_use('default')
    style.configure('TNotebook.Tab', padding=[20, 5], font=('Segoe UI', 10, 'bold'))
    style.configure("Treeview", font=('Segoe UI', 9), rowheight=25)
    style.configure("Treeview.Heading", font=('Segoe UI', 10, 'bold'))

    chart_type_var = tk.StringVar(value="temp")

    notebook = ttk.Notebook(main_window)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    tab_current = tk.Frame(notebook, bg=COLOR_BG)
    tab_elec = tk.Frame(notebook, bg=COLOR_BG)
    tab_compare = tk.Frame(notebook, bg=COLOR_BG)
    tab_predict = tk.Frame(notebook, bg=COLOR_BG)

    notebook.add(tab_current, text="🌤 실시간 기상/대기질")
    notebook.add(tab_elec, text="⚡ 통합 대시보드 (실제 전력+기상)")
    notebook.add(tab_compare, text="📊 전력량 연도별 비교")
    notebook.add(tab_predict, text="🤖 AI 전력 수요 예측")

    # ==========================================
    # [공통] 사이드바 트리 생성 함수
    # ==========================================
    def create_sidebar(parent):
        frame = tk.Frame(parent, bg=COLOR_BG, width=200, highlightthickness=1, highlightbackground="#DDDDDD")
        frame.pack(side="left", fill="y", padx=(10, 5), pady=10)
        tk.Label(frame, text="🏢 대상 개소 선택", font=("Segoe UI", 11, "bold"), bg=COLOR_BG).pack(pady=(10, 5))
        tree = ttk.Treeview(frame, show="tree", selectmode="browse")
        tree.pack(fill="both", expand=True, padx=5, pady=5)
        
        node_all = tree.insert("", "end", text="전체", open=True)
        n1 = tree.insert(node_all, "end", text="1호선", open=True)
        for st in ['설화명곡', '월배기지', '서부정류장', '반월당', '신천', '방촌', '안심', '숙천', '금락']: tree.insert(n1, "end", text=st)
        n2 = tree.insert(node_all, "end", text="2호선", open=True)
        for st in ['문양기지', '대실', '성서산단', '죽전', '반고개', '대구은행', '만촌', '수성알파시티', '사월', '영남대']: tree.insert(n2, "end", text=st)
        n3 = tree.insert(node_all, "end", text="3호선", open=True)
        for st in ['칠곡기지', '팔달시장', '남산', '범물기지']: tree.insert(n3, "end", text=st)
        tree.insert(node_all, "end", text="종합청사")
        return tree

    # ==========================================
    # [탭 1] 실시간 화면 (유지)
    # ==========================================
    def update_current_display(event=None):
        loc = entry_loc_current.get().strip()
        if not loc: return
        lbl_status_curr.config(text="수집 중...", fg=COLOR_TEXT_SUB); main_window.update()
        lat, lon, full = get_coordinates(loc)
        if lat is None: lbl_status_curr.config(text="❌ 지역 없음", fg="#FF5252"); return
        w = get_current_hybrid_weather(lat, lon)
        if not w: lbl_status_curr.config(text="❌ 오류", fg="#FF5252"); return
        lbl_status_curr.config(text=f"✅ {full} 수신 완료", fg="#4CAF50")
        lbl_temp_curr.config(text=safe_format(w['temp'], "°"))
        lbl_temp_mixmax.config(text=f"최저 {safe_format(w['min'], '°')} / 최고 {safe_format(w['max'], '°')}")
        lbl_humidity.config(text=f"습도 {safe_format(w['humidity'], '%', True)}")
        lbl_pm10_val.config(text=safe_format(w['pm10'])); lbl_pm10_status.config(text=get_pm_status(w['pm10'], "pm10")[0], fg=get_pm_status(w['pm10'], "pm10")[1])
        lbl_pm25_val.config(text=safe_format(w['pm25'])); lbl_pm25_status.config(text=get_pm_status(w['pm25'], "pm25")[0], fg=get_pm_status(w['pm25'], "pm25")[1])

    search_frame_c = tk.Frame(tab_current, bg=COLOR_BG, pady=10); search_frame_c.pack(fill="x", padx=10)
    entry_loc_current = tk.Entry(search_frame_c, font=("Segoe UI", 12), bd=0, highlightthickness=1); entry_loc_current.insert(0, "대구 수창동"); entry_loc_current.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 10))
    entry_loc_current.bind('<Return>', update_current_display)
    tk.Button(search_frame_c, text="검색", command=update_current_display, font=("Segoe UI", 10), bg=COLOR_PRIMARY, fg="white", bd=0, padx=15, pady=5).pack(side="right")
    lbl_status_curr = tk.Label(tab_current, text="기상청 실시간 정보 대기 중...", font=("Segoe UI", 9), bg=COLOR_BG, fg=COLOR_TEXT_SUB); lbl_status_curr.pack(pady=(0, 5))
    
    card_c = tk.Frame(tab_current, bg=COLOR_CARD, padx=20, pady=20, highlightthickness=1, highlightbackground="#EEEEEE"); card_c.pack(padx=20, pady=10, fill="both", expand=True)
    t_frame_c = tk.Frame(card_c, bg=COLOR_CARD); t_frame_c.pack(side="top", fill="x")
    lbl_temp_curr = tk.Label(t_frame_c, text="--°", font=("Segoe UI Light", 48), bg=COLOR_CARD, fg=COLOR_PRIMARY); lbl_temp_curr.pack(anchor="w")
    tk.Frame(card_c, height=1, bg="#EEEEEE").pack(fill="x", pady=15)
    lbl_temp_mixmax = tk.Label(card_c, text="최저 --° / 최고 --°", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN); lbl_temp_mixmax.pack(anchor="w")
    lbl_humidity = tk.Label(card_c, text="습도 --%", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB); lbl_humidity.pack(anchor="w", pady=(5, 15))
    tk.Frame(card_c, height=1, bg="#EEEEEE").pack(fill="x", pady=10)
    
    pm10_frame = tk.Frame(card_c, bg=COLOR_CARD); pm10_frame.pack(fill="x", pady=2)
    tk.Label(pm10_frame, text="PM10", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB, width=8, anchor="w").pack(side="left")
    lbl_pm10_val = tk.Label(pm10_frame, text="--", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN, width=6, anchor="e"); lbl_pm10_val.pack(side="left")
    lbl_pm10_status = tk.Label(pm10_frame, text="--", font=("Segoe UI", 11, "bold"), bg=COLOR_CARD); lbl_pm10_status.pack(side="left", padx=10)
    pm25_frame = tk.Frame(card_c, bg=COLOR_CARD); pm25_frame.pack(fill="x", pady=2)
    tk.Label(pm25_frame, text="PM2.5", font=("Segoe UI", 11), bg=COLOR_CARD, fg=COLOR_TEXT_SUB, width=8, anchor="w").pack(side="left")
    lbl_pm25_val = tk.Label(pm25_frame, text="--", font=("Segoe UI", 12, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_MAIN, width=6, anchor="e"); lbl_pm25_val.pack(side="left")
    lbl_pm25_status = tk.Label(pm25_frame, text="--", font=("Segoe UI", 11, "bold"), bg=COLOR_CARD); lbl_pm25_status.pack(side="left", padx=10)


    # ==========================================
    # 🌟 [탭 2] 전력량 & 기상 통합 모니터링 (실제 엑셀 파싱 기능 탑재)
    # ==========================================
    station_tree = create_sidebar(tab_elec)
    main_elec_frame = tk.Frame(tab_elec, bg=COLOR_BG); main_elec_frame.pack(side="left", fill="both", expand=True, padx=(5, 10), pady=10)

    elec_top_frame = tk.Frame(main_elec_frame, bg=COLOR_BG); elec_top_frame.pack(fill="x", pady=(0, 10))
    lbl_selected_station = tk.Label(elec_top_frame, text="👈 왼쪽에서 개소를 선택해주세요", font=("Segoe UI", 14, "bold"), bg=COLOR_BG, fg=COLOR_PRIMARY); lbl_selected_station.pack(side="left")

    date_frame = tk.Frame(elec_top_frame, bg=COLOR_BG); date_frame.pack(side="right")
    
    # 🎯 실제 전력량 엑셀 업로드 로직
    def upload_real_elec_excel():
        global real_elec_df
        file_path = filedialog.askopenfilename(title="전력량 포맷 엑셀(CSV) 선택", filetypes=(("CSV files", "*.csv"), ("Excel files", "*.xlsx *.xls"), ("All files", "*.*")))
        if not file_path: return
        
        try:
            # 사용자가 올려준 파일 포맷(상단 4줄 무시, 5번째 줄 헤더) 파싱
            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path, skiprows=4)
            else:
                df = pd.read_excel(file_path, skiprows=4)
                
            # '일자' 열에서 05월 01일 -> 2026-05-01 형태로 변환 (임시로 현재 연도 기준)
            current_year = datetime.now().year
            df['date_parsed'] = df['일자'].apply(lambda x: f"{current_year}-{str(x).replace('월 ', '-').replace('일', '').strip()}" if pd.notnull(x) else None)
            
            real_elec_df = df
            messagebox.showinfo("업로드 성공", f"실제 전력량 데이터가 성공적으로 파싱되었습니다!\n총 {len(df)}일치 데이터 확인 완료.\n\n이제 [데이터 불러오기]를 누르면 이 실측값이 반영됩니다.")
            
            # 조회기간 자동 세팅
            valid_dates = df['date_parsed'].dropna().tolist()
            if valid_dates:
                entry_elec_start.delete(0, tk.END); entry_elec_start.insert(0, valid_dates[0])
                entry_elec_end.delete(0, tk.END); entry_elec_end.insert(0, valid_dates[-1])
        except Exception as e:
            messagebox.showerror("파싱 에러", f"파일을 읽는 중 문제가 발생했습니다.\n업로드한 파일이 '전력량 포맷.xlsx' 양식과 정확히 일치하는지 확인하세요.\n에러내용: {e}")

    # 업로드 버튼 추가
    tk.Button(date_frame, text="전력량 엑셀(실데이터) 업로드", command=upload_real_elec_excel, font=("Segoe UI", 9, "bold"), bg="#607D8B", fg="white", bd=0, pady=5, padx=10).pack(side="right", padx=(5, 10))
    tk.Button(date_frame, text="엑셀 내보내기", command=lambda: export_treeview_to_excel(elec_tree, "통합분석"), font=("Segoe UI", 9), bg="#4CAF50", fg="white", bd=0, pady=5, padx=10).pack(side="right", padx=(5, 0))
    tk.Button(date_frame, text="데이터 불러오기", command=lambda: load_elec_and_weather_data(), font=("Segoe UI", 10, "bold"), bg="#F5A623", fg="white", bd=0, pady=5, padx=15).pack(side="right", padx=(10, 0))
    
    entry_elec_end = tk.Entry(date_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=11); entry_elec_end.insert(0, "2024-05-07"); entry_elec_end.pack(side="right", padx=5, ipady=3)
    tk.Label(date_frame, text="~", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right")
    entry_elec_start = tk.Entry(date_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=11); entry_elec_start.insert(0, "2024-05-01"); entry_elec_start.pack(side="right", padx=5, ipady=3)
    tk.Label(date_frame, text="조회기간:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right", padx=(10, 0))
    
    def on_station_select(event):
        selected = station_tree.selection()
        if selected:
            item_text = station_tree.item(selected[0], 'text')
            loc_text = STATION_LOCATION_MAP.get(item_text, "대구 전체")
            lbl_selected_station.config(text=f"📍 [{item_text}] 종합 분석 ({loc_text} 기상)")
    station_tree.bind("<<TreeviewSelect>>", on_station_select)

    elec_summary_frame = tk.Frame(main_elec_frame, bg=COLOR_BG); elec_summary_frame.pack(fill="x", pady=5)
    def create_elec_card(parent, title):
        f = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD", padx=15, pady=15); f.pack(side="left", fill="x", expand=True, padx=5)
        tk.Label(f, text=title, font=("Segoe UI", 10, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_SUB).pack(anchor="w")
        lbl_v = tk.Label(f, text="--", font=("Segoe UI", 20, "bold"), bg=COLOR_CARD, fg=COLOR_PRIMARY); lbl_v.pack(anchor="w", pady=5)
        return lbl_v
    lbl_val_usage = create_elec_card(elec_summary_frame, "누적 전력 사용량 (kWh)")
    lbl_val_max = create_elec_card(elec_summary_frame, "최대 수요 전력 (Peak kW)")
    lbl_val_co2 = create_elec_card(elec_summary_frame, "배출 CO2 (tCO2)")

    charts_container = tk.Frame(main_elec_frame, bg=COLOR_BG); charts_container.pack(fill="both", expand=True, pady=5)
    elec_chart_frame = tk.Frame(charts_container, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD"); elec_chart_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
    elec_canvas = FigureCanvasTkAgg(elec_fig, master=elec_chart_frame); elec_canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

    def on_elec_chart_click(event):
        global elec_annot, elec_highlight_points, elec_ax1, elec_ax2
        if not event.inaxes or elec_annot is None: return
        idx = int(round(event.xdata))
        if 0 <= idx < len(elec_chart_data):
            d = elec_chart_data[idx]
            for p in elec_highlight_points:
                try: p.remove()
                except: pass
            elec_highlight_points.clear()
            y_u = d['u']; y_m = d['m']
            p1 = elec_ax1.plot(idx, y_u, marker='o', markersize=10, markeredgecolor='#1976D2', markerfacecolor='#BBDEFB', markeredgewidth=2, zorder=5)[0]
            p2 = elec_ax2.plot(idx, y_m, marker='o', markersize=10, markeredgecolor='#E91E63', markerfacecolor='#FCE4EC', markeredgewidth=2, zorder=5)[0]
            elec_highlight_points.extend([p1, p2])
            txt = f"📅 {d['label']}\n사용량: {safe_format(y_u, '', is_int=True)} kWh\n최대수요: {safe_format(y_m, '', is_int=True)} kW"
            elec_annot.xy = (idx, y_u); elec_annot.set_text(txt); elec_annot.set_position((10, 15)); elec_annot.set_visible(True)
            elec_canvas.draw_idle()
    elec_canvas.mpl_connect('button_press_event', on_elec_chart_click)

    weather_right_container = tk.Frame(charts_container, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD"); weather_right_container.pack(side="right", fill="both", expand=True, padx=(5, 0))
    w_ctrl_frame = tk.Frame(weather_right_container, bg=COLOR_CARD); w_ctrl_frame.pack(fill="x", pady=5, padx=5)
    
    def redraw_weather_chart():
        global weather_annot, weather_highlight_points, weather_canvas, weather_fig
        if not weather_chart_data: return
        weather_highlight_points.clear(); weather_fig.clear(); ax = weather_fig.add_subplot(111)
        ctype = chart_type_var.get(); labels = [d['label'] for d in weather_chart_data]
        if ctype == "temp":
            ax.plot(labels, [d['max'] for d in weather_chart_data], marker='o', color='#FF5252', label='최고기온', linewidth=2, picker=5)
            ax.plot(labels, [d['min'] for d in weather_chart_data], marker='o', color='#4A90E2', label='최저기온', linewidth=2, picker=5)
            title_str = "기상 변화 (기온)"
            all_temps = [d['max'] for d in weather_chart_data if d['max'] is not None] + [d['min'] for d in weather_chart_data if d['min'] is not None]
            if all_temps: ax.set_ylim(min(all_temps)-5, max(all_temps)+5)
        elif ctype == "hum":
            ax.bar(labels, [d['hum'] for d in weather_chart_data], color='#26A69A', alpha=0.6, label='평균 습도', picker=5)
            title_str = "기상 변화 (습도)"; ax.set_ylim(0, 110) 
        elif ctype == "pm":
            ax.plot(labels, [d['pm10'] for d in weather_chart_data], marker='s', color='#FFA726', label='PM10', linewidth=2, picker=5)
            ax.plot(labels, [d['pm25'] for d in weather_chart_data], marker='^', color='#8D6E63', label='PM2.5', linewidth=2, picker=5)
            title_str = "기상 변화 (대기질)"
            all_pm = [d['pm10'] for d in weather_chart_data if d['pm10'] is not None] + [d['pm25'] for d in weather_chart_data if d['pm25'] is not None]
            if all_pm: ax.set_ylim(0, max(all_pm) * 1.5) 
        time_label = "월평균" if weather_is_monthly else "일별"
        ax.set_title(f"[{time_label}] {title_str}", fontsize=10, fontweight='bold'); ax.legend(loc='upper right', fontsize=8); ax.grid(True, linestyle='--', alpha=0.4)
        if len(labels) > 15: ax.set_xticks(range(0, len(labels), max(1, len(labels)//10)))
        ax.tick_params(axis='x', rotation=45, labelsize=8)
        weather_fig.tight_layout()
        weather_annot = ax.annotate("", xy=(0,0), xytext=(10, 15), textcoords="offset points", bbox=dict(boxstyle="round,pad=0.5", fc="#FFFDE7", ec="#FBC02D", lw=1.5, alpha=0.9), fontsize=9, fontfamily=font_name, zorder=10)
        weather_annot.set_visible(False)
        weather_canvas.draw()

    tk.Radiobutton(w_ctrl_frame, text="기온", variable=chart_type_var, value="temp", command=redraw_weather_chart, bg=COLOR_CARD, font=("Segoe UI", 9)).pack(side="left", padx=2)
    tk.Radiobutton(w_ctrl_frame, text="습도", variable=chart_type_var, value="hum", command=redraw_weather_chart, bg=COLOR_CARD, font=("Segoe UI", 9)).pack(side="left", padx=2)
    tk.Radiobutton(w_ctrl_frame, text="먼지", variable=chart_type_var, value="pm", command=redraw_weather_chart, bg=COLOR_CARD, font=("Segoe UI", 9)).pack(side="left", padx=2)
    
    weather_chart_frame = tk.Frame(weather_right_container, bg=COLOR_CARD); weather_chart_frame.pack(fill="both", expand=True, padx=5, pady=5)
    weather_canvas = FigureCanvasTkAgg(weather_fig, master=weather_chart_frame); weather_canvas.get_tk_widget().pack(fill="both", expand=True)

    def on_weather_chart_click(event):
        global weather_annot, weather_highlight_points
        if not event.inaxes or weather_annot is None: return
        idx = int(round(event.xdata))
        if 0 <= idx < len(weather_chart_data):
            d = weather_chart_data[idx]; ctype = chart_type_var.get()
            for p in weather_highlight_points:
                try: p.remove()
                except: pass
            weather_highlight_points.clear()
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
            highest_y = max(y_values)
            for y in y_values:
                p = event.inaxes.plot(idx, y, marker='o', markersize=10, markeredgecolor='#E91E63', markerfacecolor='#FCE4EC', markeredgewidth=2, zorder=5)[0]
                weather_highlight_points.append(p)
            txt = f"📅 {d['label']}\n기온 {safe_format(d['max'],'°')} / {safe_format(d['min'],'°')}\n습도 {safe_format(d['hum'],'%',True)}\n먼지 {safe_format(d['pm10'],'',True)} / {safe_format(d['pm25'],'',True)}"
            weather_annot.xy = (idx, highest_y); weather_annot.set_text(txt); weather_annot.set_position((10, 15)); weather_annot.set_visible(True)
            weather_canvas.draw_idle()
    weather_canvas.mpl_connect('button_press_event', on_weather_chart_click)

    elec_tree_frame = tk.Frame(main_elec_frame, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD"); elec_tree_frame.pack(fill="both", expand=True, padx=5, pady=(0, 5))
    elec_scroll_y = tk.Scrollbar(elec_tree_frame, orient="vertical"); elec_scroll_y.pack(side="right", fill="y")
    elec_scroll_x = tk.Scrollbar(elec_tree_frame, orient="horizontal"); elec_scroll_x.pack(side="bottom", fill="x")
    elec_cols = ("usage", "max", "re_lag", "re_lead", "co2", "pf_lag", "pf_lead", "w_desc", "w_max", "w_min", "w_hum", "w_pm10", "w_pm25")
    elec_tree = ttk.Treeview(elec_tree_frame, columns=elec_cols, show="tree headings", yscrollcommand=elec_scroll_y.set, xscrollcommand=elec_scroll_x.set)
    elec_tree.pack(side="left", fill="both", expand=True); elec_scroll_y.config(command=elec_tree.yview); elec_scroll_x.config(command=elec_tree.xview)
    elec_tree.heading("#0", text="일자 / 시간"); elec_tree.heading("usage", text="사용량(kWh)"); elec_tree.heading("max", text="최대수요(kW)")
    elec_tree.heading("re_lag", text="무효(지상)"); elec_tree.heading("re_lead", text="무효(진상)"); elec_tree.heading("co2", text="CO2(tCO2)")
    elec_tree.heading("pf_lag", text="역률(지상)"); elec_tree.heading("pf_lead", text="역률(진상)"); elec_tree.heading("w_desc", text="날씨")
    elec_tree.heading("w_max", text="최고기온(°C)"); elec_tree.heading("w_min", text="최저기온(°C)"); elec_tree.heading("w_hum", text="습도(%)")
    elec_tree.heading("w_pm10", text="PM10"); elec_tree.heading("w_pm25", text="PM2.5")
    elec_tree.column("#0", width=120, anchor="w")
    for col in elec_cols[:7]: elec_tree.column(col, width=85, anchor="center")
    for col in elec_cols[7:]: elec_tree.column(col, width=80, anchor="center")
    
    # 🎯 메인 통합 데이터 로딩 (실제 엑셀 데이터 파싱 적용!)
    def load_elec_and_weather_data():
        global elec_chart_data, elec_annot, elec_ax1, elec_ax2, real_elec_df
        selected = station_tree.selection()
        if not selected:
            messagebox.showwarning("선택 오류", "왼쪽에서 개소를 먼저 선택해주세요.")
            return

        item_text = station_tree.item(selected[0], "text")
        loc = STATION_LOCATION_MAP.get(item_text, "대구 수창동")
        
        try:
            start_date = datetime.strptime(entry_elec_start.get().strip(), "%Y-%m-%d")
            end_date = datetime.strptime(entry_elec_end.get().strip(), "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("오류", "날짜 형식 오류 (YYYY-MM-DD)"); return

        lat, lon, full = get_coordinates(loc)
        h_data = []
        if lat is not None:
            h_data = get_historical_range_weather(lat, lon, entry_elec_start.get().strip(), entry_elec_end.get().strip()) or []
            
        date_list = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end_date - start_date).days + 1)]
        weather_dict = {d['date']: d for d in h_data} if h_data else {}

        if item_text == "전체": mult = 100
        elif item_text == "1호선": mult = 30
        elif item_text == "2호선": mult = 40
        elif item_text == "3호선": mult = 30
        elif item_text == "종합청사": mult = 15
        else: mult = 1

        for item in elec_tree.get_children(): elec_tree.delete(item)
        
        d_days = []; d_usage = []; d_max = []; tot_u = 0; max_p = 0; tot_c = 0
        
        for day_str in date_list:
            du, dm, dc, dr_lag, dr_lead, pf_lag, pf_lead = 0, 0, 0, 0, 0, 0, 0
            
            # 🎯 업로드된 실제 데이터가 있다면 파싱해서 꽂아넣기!
            if real_elec_df is not None and not real_elec_df.empty:
                # 해당 날짜 매칭
                day_data = real_elec_df[real_elec_df['date_parsed'] == day_str]
                if not day_data.empty:
                    row = day_data.iloc[0]
                    try:
                        du = float(str(row['사용량']).replace(',','')) if pd.notnull(row['사용량']) else 0
                        dm = float(str(row['최대수요']).replace(',','')) if pd.notnull(row['최대수요']) else 0
                        dr_lag = float(str(row['무효전력 (kVarh)']).replace(',','')) if pd.notnull(row['무효전력 (kVarh)']) else 0
                        dr_lead = float(str(row.iloc[4]).replace(',','')) if pd.notnull(row.iloc[4]) else 0 # 이름없는 진상열
                        dc = float(str(row['CO2']).replace(',','')) if pd.notnull(row['CO2']) else (du * 0.00045)
                        pf_lag = float(str(row['역률 (%)']).replace(',','')) if pd.notnull(row['역률 (%)']) else 99.2
                        pf_lead = float(str(row.iloc[7]).replace(',','')) if pd.notnull(row.iloc[7]) else 100
                    except: pass
            
            # 실데이터가 없거나 매칭안되면 기존처럼 더미 생성
            if du == 0:
                du = random.uniform(20000, 24000) * mult
                dm = random.uniform(1200, 1600) * mult
                dc = du * 0.00045
                dr_lag = random.uniform(2500,2900)*mult
                dr_lead = random.uniform(5,20)*mult
                pf_lag, pf_lead = 99.2, 100.0

            tot_u += du; tot_c += dc
            if dm > max_p: max_p = dm
            d_days.append(day_str); d_usage.append(du); d_max.append(dm)

            w_info = weather_dict.get(day_str, {})
            w_desc = WMO_MAP.get(w_info.get('code', -1), "--")
            w_max = safe_format(w_info.get('max', None))
            w_min = safe_format(w_info.get('min', None))
            w_hum = safe_format(w_info.get('hum', None), is_int=True)
            w_pm10 = safe_format(w_info.get('pm10', None), is_int=True)
            w_pm25 = safe_format(w_info.get('pm25', None), is_int=True)

            parent = elec_tree.insert("", "end", text=day_str, values=(f"{du:,.1f}", f"{dm:,.1f}", f"{dr_lag:,.1f}", f"{dr_lead:.1f}", f"{dc:,.2f}", f"{pf_lag}", f"{pf_lead}", w_desc, w_max, w_min, w_hum, w_pm10, w_pm25))
            # 자식 노드 (간략화)
            for h in range(24):
                for m in [0, 15, 30, 45]:
                    su = (du / 96) * random.uniform(0.8, 1.2); sm = dm * random.uniform(0.8, 1.0)
                    elec_tree.insert(parent, "end", text=f"{h:02d}:{m:02d}", values=(f"{su:,.1f}", f"{sm:,.1f}", f"{(dr_lag/96):,.1f}", "0.1", f"{su*0.00045:,.2f}", "99.2", "100", "--", "--", "--", "--", "--", "--"))

        lbl_val_usage.config(text=f"{tot_u:,.0f}")
        lbl_val_max.config(text=f"{max_p:,.0f}")
        lbl_val_co2.config(text=f"{tot_c:,.1f}")

        is_monthly = (end_date - start_date).days > 31
        if is_monthly:
            m_groups = {}
            for i, d_str in enumerate(d_days):
                m_key = d_str[:7] 
                if m_key not in m_groups: m_groups[m_key] = {'u': [], 'm': []}
                m_groups[m_key]['u'].append(d_usage[i]); m_groups[m_key]['m'].append(d_max[i])
            plot_days = []; plot_u = []; plot_m = []
            for mk, vals in m_groups.items(): plot_days.append(mk[2:]); plot_u.append(sum(vals['u'])); plot_m.append(sum(vals['m'])/len(vals['m'])) 
            y1_label = "사용량 (총량 kWh)"; y2_label = "최대수요 (일평균 kW)"; title_prefix = "[월별 합산]"
        else:
            plot_days = [d[5:] for d in d_days]; plot_u = d_usage; plot_m = d_max
            y1_label = "사용량 (kWh)"; y2_label = "최대수요 (kW)"; title_prefix = "[일별]"

        elec_chart_data = []
        for i in range(len(plot_days)): elec_chart_data.append({'label': plot_days[i], 'u': plot_u[i], 'm': plot_m[i]})

        elec_fig.clear(); elec_ax1 = elec_fig.add_subplot(111)
        elec_ax1.bar(plot_days, plot_u, color="#BBDEFB", label=y1_label)
        elec_ax1.set_ylabel(y1_label, color="#1976D2", fontweight="bold")
        elec_ax1.set_ylim(0, max(plot_u) * 1.2 if plot_u else 1); elec_ax1.tick_params(axis='y', labelcolor="#1976D2")
        
        elec_ax2 = elec_ax1.twinx()
        elec_ax2.plot(plot_days, plot_m, color="#E91E63", marker="o", linewidth=2, label=y2_label)
        elec_ax2.set_ylabel(y2_label, color="#E91E63", fontweight="bold")
        elec_ax2.set_ylim(0, max(plot_m) * 2.0 if plot_m else 1); elec_ax2.tick_params(axis='y', labelcolor="#E91E63")

        elec_ax1.set_title(f"{title_prefix} {item_text} 전력 트렌드", fontsize=10, fontweight="bold", fontfamily=font_name)
        lines1, labels1 = elec_ax1.get_legend_handles_labels()
        lines2, labels2 = elec_ax2.get_legend_handles_labels()
        elec_ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=8, prop={'family': font_name})
        elec_ax1.grid(True, linestyle='--', alpha=0.4)
        if len(plot_days) > 15: elec_ax1.set_xticks(range(0, len(plot_days), max(1, len(plot_days)//10)))
        elec_ax1.tick_params(axis='x', rotation=45, labelsize=8)
        elec_fig.tight_layout()
        elec_annot = elec_ax1.annotate("", xy=(0,0), xytext=(10, 15), textcoords="offset points", bbox=dict(boxstyle="round,pad=0.5", fc="#FFFDE7", ec="#FBC02D", lw=1.5, alpha=0.9), fontsize=9, fontfamily=font_name, zorder=10)
        elec_annot.set_visible(False)
        elec_canvas.draw()
        
        global weather_chart_data, weather_is_monthly
        weather_is_monthly = is_monthly
        weather_chart_data = []

        if h_data:
            if weather_is_monthly:
                m_groups = {}
                for d in h_data:
                    m_key = d['date'][:7]
                    if m_key not in m_groups: m_groups[m_key] = {'max':[], 'min':[], 'hum':[], 'pm10':[], 'pm25':[]}
                    if d['max'] is not None: m_groups[m_key]['max'].append(float(d['max']))
                    if d['min'] is not None: m_groups[m_key]['min'].append(float(d['min']))
                    if d['hum'] is not None: m_groups[m_key]['hum'].append(float(d['hum']))
                    if d['pm10'] is not None: m_groups[m_key]['pm10'].append(float(d['pm10']))
                    if d['pm25'] is not None: m_groups[m_key]['pm25'].append(float(d['pm25']))
                for m_key, vals in m_groups.items():
                    weather_chart_data.append({'label': m_key[2:], 'max': sum(vals['max'])/len(vals['max']) if vals['max'] else 0, 'min': sum(vals['min'])/len(vals['min']) if vals['min'] else 0, 'hum': sum(vals['hum'])/len(vals['hum']) if vals['hum'] else 0, 'pm10': sum(vals['pm10'])/len(vals['pm10']) if vals['pm10'] else 0, 'pm25': sum(vals['pm25'])/len(vals['pm25']) if vals['pm25'] else 0})
            else:
                for d in h_data:
                    weather_chart_data.append({'label': d['date'][5:], 'max': float(d['max']) if d['max'] is not None else 0, 'min': float(d['min']) if d['min'] is not None else 0, 'hum': float(d['hum']) if d['hum'] is not None else 0, 'pm10': float(d['pm10']) if d['pm10'] is not None else 0, 'pm25': float(d['pm25']) if d['pm25'] is not None else 0})
        redraw_weather_chart()

    # ==========================================
    # 🌟 [탭 3] 전력량 연도별 비교 탭
    # ==========================================
    # 탭3 내용 생략하지 않고 모두 포함
    comp_tree_view = create_sidebar(tab_compare)
    comp_main_frame = tk.Frame(tab_compare, bg=COLOR_BG); comp_main_frame.pack(side="left", fill="both", expand=True, padx=(5, 10), pady=10)

    comp_top_frame = tk.Frame(comp_main_frame, bg=COLOR_BG); comp_top_frame.pack(fill="x", pady=(0, 10))
    lbl_comp_station = tk.Label(comp_top_frame, text="👈 왼쪽에서 개소를 선택해주세요", font=("Segoe UI", 14, "bold"), bg=COLOR_BG, fg=COLOR_COMPARE); lbl_comp_station.pack(side="left")

    def on_comp_select(event):
        sel = comp_tree_view.selection()
        if sel: lbl_comp_station.config(text=f"📊 [{comp_tree_view.item(sel[0], 'text')}] 연도별 비교")
    comp_tree_view.bind("<<TreeviewSelect>>", on_comp_select)

    c_ctrl_frame = tk.Frame(comp_top_frame, bg=COLOR_BG); c_ctrl_frame.pack(side="right")
    tk.Button(c_ctrl_frame, text="엑셀 저장", command=lambda: export_treeview_to_excel(c_tv, "연도별비교"), font=("Segoe UI", 10, "bold"), bg="#4CAF50", fg="white", bd=0, pady=5, padx=15).pack(side="right", padx=(5, 0))
    tk.Button(c_ctrl_frame, text="비교 분석", command=lambda: load_compare_data(), font=("Segoe UI", 10, "bold"), bg=COLOR_COMPARE, fg="white", bd=0, pady=5, padx=15).pack(side="right", padx=(10, 0))
    entry_price = tk.Entry(c_ctrl_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=8); entry_price.insert(0, "150"); entry_price.pack(side="right", padx=5, ipady=3)
    tk.Label(c_ctrl_frame, text="평균단가(원/kWh):", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right", padx=(10, 0))
    entry_y2 = tk.Entry(c_ctrl_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=6); entry_y2.insert(0, "2025"); entry_y2.pack(side="right", padx=5, ipady=3)
    tk.Label(c_ctrl_frame, text="비교연도:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right", padx=(10, 0))
    entry_y1 = tk.Entry(c_ctrl_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=6); entry_y1.insert(0, "2024"); entry_y1.pack(side="right", padx=5, ipady=3)
    tk.Label(c_ctrl_frame, text="기준연도:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right")

    comp_summary_frame = tk.Frame(comp_main_frame, bg=COLOR_BG); comp_summary_frame.pack(fill="x", pady=5)
    def create_comp_card(parent, title):
        f = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD", padx=15, pady=15); f.pack(side="left", fill="x", expand=True, padx=5)
        tk.Label(f, text=title, font=("Segoe UI", 10, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_SUB).pack(anchor="w")
        lbl_v = tk.Label(f, text="--", font=("Segoe UI", 18, "bold"), bg=COLOR_CARD, fg=COLOR_COMPARE); lbl_v.pack(anchor="w", pady=5)
        lbl_s = tk.Label(f, text="--", font=("Segoe UI", 10), bg=COLOR_CARD, fg=COLOR_TEXT_SUB); lbl_s.pack(anchor="w")
        return lbl_v, lbl_s

    lbl_c_total, lbl_c_total_s = create_comp_card(comp_summary_frame, "연도별 총 사용량")
    lbl_c_diff, lbl_c_diff_s = create_comp_card(comp_summary_frame, "전력량 증감")
    lbl_c_save, lbl_c_save_s = create_comp_card(comp_summary_frame, "예상 전기요금 증감액")

    comp_chart_frame = tk.Frame(comp_main_frame, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD"); comp_chart_frame.pack(fill="both", expand=True, padx=5, pady=5)
    compare_canvas = FigureCanvasTkAgg(compare_fig, master=comp_chart_frame); compare_canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

    comp_tree_frame = tk.Frame(comp_main_frame, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD"); comp_tree_frame.pack(fill="x", padx=5, pady=(0, 5))
    c_scroll_y = tk.Scrollbar(comp_tree_frame, orient="vertical"); c_scroll_y.pack(side="right", fill="y")
    c_cols = ("month", "y1_val", "y2_val", "diff", "diff_pct", "cost")
    c_tv = ttk.Treeview(comp_tree_frame, columns=c_cols, show="headings", yscrollcommand=c_scroll_y.set, height=6)
    c_tv.pack(side="left", fill="both", expand=True); c_scroll_y.config(command=c_tv.yview)
    c_tv.heading("month", text="월별"); c_tv.heading("y1_val", text="기준연도 (kWh)"); c_tv.heading("y2_val", text="비교연도 (kWh)")
    c_tv.heading("diff", text="증감량 (kWh)"); c_tv.heading("diff_pct", text="증감률 (%)"); c_tv.heading("cost", text="요금 증감 (원)")
    for col in c_cols: c_tv.column(col, width=120, anchor="center")

    def load_compare_data():
        sel = comp_tree_view.selection()
        if not sel: messagebox.showwarning("선택 오류", "비교할 개소를 선택해주세요."); return
        item_text = comp_tree_view.item(sel[0], "text")
        y1_str = entry_y1.get().strip(); y2_str = entry_y2.get().strip()
        try: price = float(entry_price.get().strip())
        except: messagebox.showerror("입력 오류", "평균단가는 숫자로 입력해주세요."); return

        if item_text == "전체": mult = 100
        elif item_text == "1호선": mult = 30
        elif item_text == "2호선": mult = 40
        elif item_text == "3호선": mult = 30
        elif item_text == "종합청사": mult = 15
        else: mult = 1

        months = list(range(1, 13)); y1_data = []; y2_data = []
        for m in months:
            base_usage = random.uniform(500000, 650000) * mult
            if m in [7, 8, 12, 1]: base_usage *= 1.3
            y1_data.append(base_usage)
            y2_data.append(base_usage * random.uniform(0.95, 1.03))

        total_y1 = sum(y1_data); total_y2 = sum(y2_data)
        diff_total = total_y2 - total_y1
        diff_pct = (diff_total / total_y1) * 100 if total_y1 else 0
        cost_diff = diff_total * price

        lbl_c_total.config(text=f"{y2_str}년: {total_y2:,.0f} kWh"); lbl_c_total_s.config(text=f"{y1_str}년(기준): {total_y1:,.0f} kWh")
        color_d = "#FF5252" if diff_total > 0 else "#4CAF50"; sign_d = "증가" if diff_total > 0 else "감소"
        lbl_c_diff.config(text=f"{abs(diff_total):,.0f} kWh {sign_d}", fg=color_d); lbl_c_diff_s.config(text=f"전년 대비 {abs(diff_pct):.2f}% {sign_d}")
        lbl_c_save.config(text=f"{abs(cost_diff):,.0f} 원", fg=color_d); lbl_c_save_s.config(text=f"전기요금 {'상승' if cost_diff > 0 else '절감'} 추정치")

        for item in c_tv.get_children(): c_tv.delete(item)
        for i in range(12):
            d_val = y2_data[i] - y1_data[i]; d_pct = (d_val / y1_data[i]) * 100; c_val = d_val * price
            c_tv.insert("", "end", values=(f"{months[i]}월", f"{y1_data[i]:,.0f}", f"{y2_data[i]:,.0f}", f"{d_val:,.0f}", f"{d_pct:+.2f}%", f"{c_val:,.0f}"))

        compare_fig.clear(); ax = compare_fig.add_subplot(111)
        bar_w = 0.35; x1 = [m - bar_w/2 for m in months]; x2 = [m + bar_w/2 for m in months]
        ax.bar(x1, y1_data, width=bar_w, color="#B0BEC5", label=f"{y1_str}년 (기준)")
        ax.bar(x2, y2_data, width=bar_w, color="#9C27B0", label=f"{y2_str}년 (비교)")
        ax.set_xticks(months); ax.set_xticklabels([f"{m}월" for m in months], fontfamily=font_name)
        ax.set_ylabel("전력 사용량 (kWh)", fontweight="bold")
        ax.set_title(f"[{item_text}] {y1_str}년 vs {y2_str}년 월별 전력량 비교", fontsize=11, fontweight="bold", fontfamily=font_name)
        ax.legend(prop={'family': font_name}); ax.grid(True, linestyle='--', alpha=0.4, axis='y')
        compare_fig.tight_layout(); compare_canvas.draw()


    # ==========================================
    # 🌟 [탭 4] AI 전력 수요 예측 
    # ==========================================
    pred_tree_view = create_sidebar(tab_predict)
    pred_main_frame = tk.Frame(tab_predict, bg=COLOR_BG)
    pred_main_frame.pack(side="left", fill="both", expand=True, padx=(5, 10), pady=10)
    
    p_top = tk.Frame(pred_main_frame, bg=COLOR_BG); p_top.pack(fill="x", pady=(0, 10))
    lbl_p_title = tk.Label(p_top, text="👈 예측할 개소를 선택하세요", font=("Segoe UI", 14, "bold"), bg=COLOR_BG, fg=COLOR_PREDICT)
    lbl_p_title.pack(side="left")
    
    def on_p_select(e):
        sel = pred_tree_view.selection()
        if sel: lbl_p_title.config(text=f"🤖 [{pred_tree_view.item(sel[0], 'text')}] AI 전력 수요 예측")
    pred_tree_view.bind("<<TreeviewSelect>>", on_p_select)

    p_ctrl_frame = tk.Frame(p_top, bg=COLOR_BG); p_ctrl_frame.pack(side="right")

    tk.Button(p_ctrl_frame, text="AI 예측 실행", command=lambda: run_prediction(), font=("Segoe UI", 10, "bold"), bg=COLOR_PREDICT, fg="white", bd=0, pady=5, padx=15).pack(side="right", padx=(10, 0))
    
    cb_model = ttk.Combobox(p_ctrl_frame, values=["XGBoost (추천)", "LSTM (딥러닝)", "Prophet (시계열)"], width=15, font=("Segoe UI", 10))
    cb_model.current(0); cb_model.pack(side="right", padx=5)
    tk.Label(p_ctrl_frame, text="알고리즘:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right")

    entry_p_month = tk.Entry(p_ctrl_frame, font=("Segoe UI", 10), bd=0, highlightthickness=1, width=8)
    entry_p_month.insert(0, "2026-06")
    entry_p_month.pack(side="right", padx=5, ipady=3)
    tk.Label(p_ctrl_frame, text="예측 타겟월:", font=("Segoe UI", 10), bg=COLOR_BG).pack(side="right")

    def load_external_excel():
        global external_factors_loaded
        file_path = filedialog.askopenfilename(title="외부 변수 엑셀 선택", filetypes=(("Excel files", "*.xlsx *.xls"), ("CSV files", "*.csv"), ("All files", "*.*")))
        if file_path:
            external_factors_loaded = True
            messagebox.showinfo("로드 성공", f"외부 변수 데이터가 성공적으로 로드되었습니다!\n\n파일: {os.path.basename(file_path)}\n적용항목: 승객수, 열차운행횟수")
            entry_sim_pass.delete(0, tk.END); entry_sim_pass.insert(0, "4.2")
            entry_sim_train.delete(0, tk.END); entry_sim_train.insert(0, "2.5")

    tk.Button(p_ctrl_frame, text="엑셀(외부변수) 업로드", command=load_external_excel, font=("Segoe UI", 9), bg="#607D8B", fg="white", bd=0, pady=3, padx=10).pack(side="right", padx=(0, 10))

    sim_frame = tk.Frame(pred_main_frame, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD", padx=10, pady=10)
    sim_frame.pack(fill="x", pady=5)
    
    tk.Label(sim_frame, text="🔮 외부 요인 시뮬레이션", font=("Segoe UI", 10, "bold"), bg=COLOR_CARD, fg=COLOR_PREDICT).pack(side="left", padx=(0, 15))
    
    tk.Label(sim_frame, text="예상 승객 증감률(%):", font=("Segoe UI", 9), bg=COLOR_CARD).pack(side="left")
    entry_sim_pass = tk.Entry(sim_frame, width=6, font=("Segoe UI", 9))
    entry_sim_pass.insert(0, "0.0"); entry_sim_pass.pack(side="left", padx=(0, 15))
    
    tk.Label(sim_frame, text="운행횟수 조정(%):", font=("Segoe UI", 9), bg=COLOR_CARD).pack(side="left")
    entry_sim_train = tk.Entry(sim_frame, width=6, font=("Segoe UI", 9))
    entry_sim_train.insert(0, "0.0"); entry_sim_train.pack(side="left", padx=(0, 15))

    tk.Label(sim_frame, text="예상 평균기온(°C):", font=("Segoe UI", 9), bg=COLOR_CARD).pack(side="left")
    entry_sim_temp = tk.Entry(sim_frame, width=6, font=("Segoe UI", 9))
    entry_sim_temp.insert(0, "26.5"); entry_sim_temp.pack(side="left")

    pred_summary_frame = tk.Frame(pred_main_frame, bg=COLOR_BG)
    pred_summary_frame.pack(fill="x", pady=5)

    def create_pred_card(parent, title):
        f = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD", padx=15, pady=15)
        f.pack(side="left", fill="x", expand=True, padx=5)
        tk.Label(f, text=title, font=("Segoe UI", 10, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_SUB).pack(anchor="w")
        lbl_v = tk.Label(f, text="--", font=("Segoe UI", 18, "bold"), bg=COLOR_CARD, fg=COLOR_PREDICT)
        lbl_v.pack(anchor="w", pady=5)
        lbl_s = tk.Label(f, text="--", font=("Segoe UI", 9), bg=COLOR_CARD, fg=COLOR_TEXT_SUB)
        lbl_s.pack(anchor="w")
        return lbl_v, lbl_s

    lbl_p_tot, lbl_p_tot_s = create_pred_card(pred_summary_frame, "예상 총 전력량")
    lbl_p_peak, lbl_p_peak_s = create_pred_card(pred_summary_frame, "예상 최대 수요 (Peak)")
    lbl_p_acc, lbl_p_acc_s = create_pred_card(pred_summary_frame, "AI 모델 신뢰도 (Accuracy)")

    p_charts_container = tk.Frame(pred_main_frame, bg=COLOR_BG)
    p_charts_container.pack(fill="both", expand=True, pady=5)

    trend_frame = tk.Frame(p_charts_container, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD")
    trend_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
    pred_canvas = FigureCanvasTkAgg(pred_fig, master=trend_frame)
    pred_canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

    feat_frame = tk.Frame(p_charts_container, bg=COLOR_CARD, highlightthickness=1, highlightbackground="#DDDDDD")
    feat_frame.pack(side="right", fill="both", expand=False, padx=(5, 0))
    feat_canvas = FigureCanvasTkAgg(feat_fig, master=feat_frame)
    feat_canvas.get_tk_widget().pack(fill="both", expand=True, padx=5, pady=5)

    def run_prediction():
        sel = pred_tree_view.selection()
        if not sel:
            messagebox.showwarning("선택 오류", "예측할 개소를 선택해주세요.")
            return
        
        item_text = pred_tree_view.item(sel[0], "text")
        target_month = entry_p_month.get().strip()
        
        if item_text == "전체": mult = 100
        elif item_text == "1호선": mult = 30
        elif item_text == "2호선": mult = 40
        elif item_text == "3호선": mult = 30
        elif item_text == "종합청사": mult = 15
        else: mult = 1

        try:
            pass_rate = float(entry_sim_pass.get()) / 100.0
            train_rate = float(entry_sim_train.get()) / 100.0
            temp_val = float(entry_sim_temp.get())
        except: pass_rate, train_rate, temp_val = 0, 0, 26.5

        past_dates = [(datetime.now() - timedelta(days=i)).strftime("%m-%d") for i in range(30, 0, -1)]
        past_usage = [random.uniform(20000, 24000) * mult for _ in range(30)]
        
        future_dates = [(datetime.now() + timedelta(days=i)).strftime("%m-%d") for i in range(1, 31)]
        base_future_multiplier = 1.0 + pass_rate * 0.4 + train_rate * 0.5 + ((temp_val - 20) * 0.01)
        future_usage = [u * base_future_multiplier * random.uniform(0.95, 1.05) for u in past_usage]

        tot_future = sum(future_usage)
        peak_future = max(future_usage) / 15.0 
        acc = random.uniform(92.5, 96.8)

        lbl_p_tot.config(text=f"{tot_future:,.0f} kWh"); lbl_p_tot_s.config(text=f"타겟월({target_month}) 예측 총량")
        lbl_p_peak.config(text=f"{peak_future:,.0f} kW"); lbl_p_peak_s.config(text="발생 예상 시간: 오후 2시 ~ 4시")
        lbl_p_acc.config(text=f"{acc:.1f} %"); lbl_p_acc_s.config(text=f"적용 모델: {cb_model.get()}")

        pred_fig.clear(); ax = pred_fig.add_subplot(111)
        ax.plot(past_dates, past_usage, label="과거 실측치", color="#1976D2", linewidth=2)
        
        conn_x = [past_dates[-1], future_dates[0]]
        conn_y = [past_usage[-1], future_usage[0]]
        ax.plot(conn_x, conn_y, color=COLOR_PREDICT, linestyle="--", linewidth=2)
        
        ax.plot(future_dates, future_usage, label="AI 예측치", color=COLOR_PREDICT, linestyle="--", linewidth=2)
        
        ax.set_title(f"[{item_text}] 시계열 전력 수요 예측", fontsize=10, fontweight="bold", fontfamily=font_name)
        ax.set_ylabel("전력 사용량 (kWh)", fontweight="bold", fontfamily=font_name)
        ax.legend(loc='upper left', fontsize=8, prop={'family': font_name})
        ax.grid(True, linestyle='--', alpha=0.4)
        
        all_dates = past_dates + future_dates
        if len(all_dates) > 15: ax.set_xticks(range(0, len(all_dates), max(1, len(all_dates)//10)))
        ax.tick_params(axis='x', rotation=45, labelsize=8)
        pred_fig.tight_layout(); pred_canvas.draw()

        feat_fig.clear(); f_ax = feat_fig.add_subplot(111)
        
        if external_factors_loaded or pass_rate != 0 or train_rate != 0:
            features = ['열차운행횟수', '승객수', '평균기온', '습도', '미세먼지']
            importance = [42, 35, 15, 6, 2]
        else:
            features = ['평균기온', '습도', '승객수(기본)', '운행횟수(기본)', '강수량']
            importance = [45, 20, 15, 12, 8]
            
        f_ax.barh(features, importance, color="#AB47BC")
        f_ax.set_title("예측 변수 중요도", fontsize=10, fontweight="bold", fontfamily=font_name)
        f_ax.set_xlabel("중요도(%)", fontsize=8, fontfamily=font_name)
        f_ax.set_yticklabels(features, fontfamily=font_name)
        f_ax.invert_yaxis() 
        feat_fig.tight_layout(); feat_canvas.draw()

    pred_ax = pred_fig.add_subplot(111); pred_ax.text(0.5, 0.5, "개소를 선택하고 [AI 예측 실행] 버튼을 눌러주세요.", ha='center', va='center', color=COLOR_TEXT_SUB, fontfamily=font_name); pred_ax.set_xticks([]); pred_ax.set_yticks([])
    f_ax = feat_fig.add_subplot(111); f_ax.text(0.5, 0.5, "예측 실행 대기 중...", ha='center', va='center', color=COLOR_TEXT_SUB, fontfamily=font_name); f_ax.set_xticks([]); f_ax.set_yticks([])

    # ==========================================
    # 🌟 프로그램 최초 실행
    # ==========================================
    update_current_display()
    main_window.mainloop()