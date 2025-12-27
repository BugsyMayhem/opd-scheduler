import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection

# --- Page Configuration ---
st.set_page_config(page_title="OPD Hourly Roster", layout="wide")

# --- Google Sheets Connection ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- 1. SESSION STATE INITIALIZATION ---
if 'main_df' not in st.session_state:
    st.session_state.main_df = pd.DataFrame(columns=["Associate", "Role", "Shift", "Lunch Time", "StartDt", "EndDt", "Duration"])
if 'history' not in st.session_state:
    st.session_state.history = []
if 'calculated' not in st.session_state:
    st.session_state.calculated = False

# --- Helper Functions ---
def get_local_time():
    now_utc = datetime.utcnow()
    year = now_utc.year
    dst_start = datetime(year, 3, 8) + timedelta(days=(6 - datetime(year, 3, 8).weekday()) % 7)
    dst_end = datetime(year, 11, 1) + timedelta(days=(6 - datetime(year, 11, 1).weekday()) % 7)
    offset = 5 if dst_start <= now_utc <= dst_end else 6
    return (now_utc - timedelta(hours=offset)).strftime("%I:%M %p")

def load_lists_from_sheets():
    try:
        url = st.secrets["connections"]["gsheets"]["spreadsheet"]
        roster_df = conn.read(spreadsheet=url, worksheet="Roster", ttl=0) 
        exclude_df = conn.read(spreadsheet=url, worksheet="Exclude", ttl=0)
        r_names = "\n".join(roster_df.iloc[:, 0].dropna().astype(str).tolist())
        e_names = "\n".join(exclude_df.iloc[:, 0].dropna().astype(str).tolist())
        st.session_state.last_sync = get_local_time()
        return r_names, e_names
    except Exception as e:
        st.error(f"Sheet Error: {e}")
        return "Add Names Here", "Manager Name"

def save_lists_to_sheets(roster_text, exclude_text):
    try:
        url = st.secrets["connections"]["gsheets"]["spreadsheet"]
        r_df = pd.DataFrame([n.strip() for n in roster_text.split('\n') if n.strip()], columns=["Names"])
        e_df = pd.DataFrame([n.strip() for n in exclude_text.split('\n') if n.strip()], columns=["Names"])
        conn.update(spreadsheet=url, worksheet="Roster", data=r_df)
        conn.update(spreadsheet=url, worksheet="Exclude", data=e_df)
        st.session_state.last_sync = get_local_time()
        st.sidebar.success(f"✅ Saved at {st.session_state.last_sync}")
    except Exception as e:
        st.sidebar.error(f"Failed to save: {e}")

def save_history():
    st.session_state.history.append(st.session_state.main_df.copy())
    if len(st.session_state.history) > 10:
        st.session_state.history.pop(0)

def parse_time(time_str):
    if not time_str: return None
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%I:%M"):
        try: return datetime.strptime(time_str, fmt)
        except ValueError: continue
    return None

def process_pdf(file, assoc_in, excl_in):
    data = []
    t_regex = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    v_list = []
    for n in assoc_in.split('\n'):
        if n.strip():
            raw = n.strip().lower()
            clean = raw.replace("(m)", "").replace("minor", "").strip()
            v_list.append({"search": clean, "raw": n.strip(), "is_minor": ("(m)" in raw or "minor" in raw)})
    e_names = [n.strip().lower() for n in excl_in.split('\n') if n.strip()]
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                m = re.search(t_regex, line.strip(), re.IGNORECASE)
                if m:
                    if any(ex in line.lower() for ex in e_names): continue
                    match_name = None
                    for entry in v_list:
                        if entry["search"] in line.lower():
                            parts = entry["search"].title().split()
                            fmt = f"{parts[0]} {parts[1][0]}.".strip() if len(parts) > 1 else parts[0]
                            match_name = f"👶 {fmt}" if entry["is_minor"] else fmt
                            break 
                    if match_name:
                        st_dt, en_dt = parse_time(m.group(1)), parse_time(m.group(2))
                        if st_dt and en_dt:
                            real_end = en_dt + timedelta(days=1) if en_dt < st_dt else en_dt
                            data.append({"Associate": match_name, "Role": "Pickers", "Shift": f"{m.group(1)} - {m.group(2)}", "Lunch Time": "Pending...", "StartDt": st_dt, "EndDt": real_end, "Duration": (real_end - st_dt).total_seconds() / 3600})
    return pd.DataFrame(data)

def calculate_staggered_lunches(df):
    if df.empty: return df
    final = []
    active = ["Pickers", "Backroom", "Exceptions"]
    for role in active:
        grp = df[df['Role'] == role].sort_values(by='StartDt').copy()
        taken = []
        for _, row in grp.iterrows():
            if row['Duration'] <= 6:
                row['Lunch Time'] = "N/A"; final.append(row.to_dict()); continue
            target, early, late = row['StartDt'] + timedelta(hours=4), row['StartDt'] + timedelta(hours=3), row['StartDt'] + timedelta(hours=5)
            safe = min(late, row['EndDt'] - timedelta(hours=1))
            curr, found = target, False
            while curr <= safe:
                if not any(abs((curr - t).total_seconds()) < 1800 for t in taken):
                    found = True; break
                curr += timedelta(minutes=30)
            if not found:
                curr = target - timedelta(minutes=30)
                while curr >= early:
                    if not any(abs((curr - t).total_seconds()) < 1800 for t in taken):
                        found = True; break
                    curr -= timedelta(minutes=30)
            row['Lunch Time'] = curr.strftime("%I:%M %p") if found else "No Slot Avail"
            if found: taken.append(curr)
            final.append(row.to_dict())
    ex = df[df['Role'] == "Exclude"].to_dict('records')
    for item in ex: item['Lunch Time'] = "N/A"; final.append(item)
    return pd.DataFrame(final)

# --- SIDEBAR (Persistent Storage Controls) ---
st.sidebar.title("☁️ Database Settings")
if 'r_val' not in st.session_state:
    r, e = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r, e

# Sync Row
s_col1, s_col2 = st.sidebar.columns([3, 1])
s_col1.write(f"Sync: {st.session_state.get('last_sync', 'Never')}")
if s_col2.button("🔄"):
    r, e = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r, e
    st.rerun()

# Whitelist/Blacklist
assoc_input = st.sidebar.text_area("Whitelist (Associates)", value=st.session_state.r_val, height=250)
excl_input = st.sidebar.text_area("Blacklist (Excluded)", value=st.session_state.e_val, height=150)

st.sidebar.divider()

# --- SIDEBAR SAVE BUTTON ---
if st.sidebar.button("💾 SAVE PERMANENTLY TO SHEETS", use_container_width=True):
    save_lists_to_sheets(assoc_input, excl_input)
    st.session_state.r_val, st.session_state.e_val = assoc_input, excl_input

# --- MAIN UI ---
st.title("📅 OPD Hourly Roster")

# --- Quick Controls ---
st.markdown("### 🛠️ Quick Controls")
qc1, qc2, qc3 = st.columns([1, 1, 2])
h_len = len(st.session_state.history)
if qc1.button(f"↩️ Undo Action ({h_len})", disabled=(h_len == 0), use_container_width=True):
    st.session_state.main_df = st.session_state.history.pop()
    st.rerun()
if qc2.button("🗑️ Clear Roster", type="secondary", use_container_width=True):
    save_history()
    st.session_state.main_df = pd.DataFrame(columns=["Associate", "Role", "Shift", "Lunch Time", "StartDt", "EndDt", "Duration"])
    st.rerun()

st.divider()

# --- Entry Row ---
col_in1, col_in2 = st.columns(2)
with col_in1:
    with st.expander("➕ Manually Add Associate", expanded=False):
        names = sorted([n.strip() for n in assoc_input.split('\n') if n.strip()])
        m_name = st.selectbox("Name", options=names if names else ["Empty"])
        m_role = st.selectbox("Role", options=["Pickers", "Backroom", "Exceptions", "Exclude"])
        m_shift = st.text_input("Shift (e.g. 5am-2pm)")
        if st.button("Add Now", use_container_width=True):
            save_history()
            try:
                ts = m_shift.lower().replace(" ", "").split('-')
                s_dt, e_dt = parse_time(ts[0]), parse_time(ts[1])
                r_e = e_dt + timedelta(days=1) if e_dt < s_dt else e_dt
                dur = (r_e - s_dt).total_seconds() / 3600
            except: s_dt, r_e, dur = None, None, 0
            d_name = m_name
            if "(m)" in m_name.lower() or "minor" in m_name.lower():
                p = m_name.replace("(m)", "").replace("minor", "").strip().title().split()
                d_name = f"👶 {p[0]} {p[1][0]}." if len(p) > 1 else f"👶 {p[0]}"
            else:
                p = m_name.title().split()
                d_name = f"{p[0]} {p[1][0]}." if len(p) > 1 else p[0]
            nr = pd.DataFrame([{"Associate": d_name, "Role": m_role, "Shift": m_shift, "Lunch Time": "Pending...", "StartDt": s_dt, "EndDt": r_e, "Duration": dur}])
            st.session_state.main_df = pd.concat([st.session_state.main_df, nr], ignore_index=True)
            st.rerun()

with col_in2:
    pdf_file = st.file_uploader("Upload Roster PDF", type="pdf")
    if pdf_file and st.button("📂 Process PDF", use_container_width=True):
        save_history()
        new_df = process_pdf(pdf_file, assoc_input, excl_input)
        st.session_state.main_df = pd.concat([st.session_state.main_df, new_df], ignore_index=True)
        st.rerun()

# --- Role Metrics (ALWAYS VISIBLE ABOVE TABLE) ---
df = st.session_state.main_df
st.divider()
st.subheader("📊 Current Counts")
m1, m2, m3, m4 = st.columns(4)
m1.metric("🛒 Pickers", len(df[df['Role'] == 'Pickers']))
m2.metric("📦 Backroom", len(df[df['Role'] == 'Backroom']))
m3.metric("⚠️ Exceptions", len(df[df['Role'] == 'Exceptions']))
m4.metric("❌ Excluded", len(df[df['Role'] == 'Exclude']))

if not df.empty:
    st.divider()
    # Roster Management Tools
    st.subheader("📋 Roster Management")
    b1, b2, b3, b4 = st.columns([2, 1, 1, 1])
    sel = b1.multiselect("Select People:", options=df['Associate'].tolist())
    target_role = b2.selectbox("Set Role:", ["Pickers", "Backroom", "Exceptions", "Exclude"])
    if b3.button("🚀 Apply"):
        save_history()
        st.session_state.main_df.loc[st.session_state.main_df['Associate'].isin(sel), 'Role'] = target_role
        st.rerun()
    if b4.button("❌ Delete"):
        save_history()
        st.session_state.main_df = st.session_state.main_df[~st.session_state.main_df['Associate'].isin(sel)]
        st.rerun()

    if st.button("🔥 GENERATE ALL LUNCHES", type="primary", use_container_width=True):
        save_history()
        st.session_state.main_df = calculate_staggered_lunches(st.session_state.main_df)
        st.session_state.calculated = True
        st.rerun()

    # Master Table
    st.subheader("Master Daily Roster")
    l_opts = ["Pending...", "N/A", "No Slot Avail"] + [(datetime(2025,1,1,0,0)+timedelta(minutes=30*i)).strftime("%I:%M %p") for i in range(48)]
    edited = st.data_editor(st.session_state.main_df, column_config={
        "Associate": st.column_config.TextColumn(disabled=False),
        "Role": st.column_config.SelectboxColumn(options=["Pickers", "Backroom", "Exceptions", "Exclude"]),
        "Shift": st.column_config.TextColumn(disabled=False),
        "Lunch Time": st.column_config.SelectboxColumn(options=l_opts),
        "StartDt": None, "EndDt": None, "Duration": None
    }, use_container_width=True, hide_index=True)
    if not edited.equals(st.session_state.main_df):
        st.session_state.main_df = edited
        st.rerun()

# --- Results Tabs ---
if st.session_state.calculated:
    st.divider()
    h_tabs = st.tabs(["🛒 Pickers", "📦 Backroom", "⚠️ Exceptions"])
    for i, r_name in enumerate(["Pickers", "Backroom", "Exceptions"]):
        with h_tabs[i]:
            rows = []
            for h in range(4, 23):
                lbl = f"{h if h<=12 else h-12} {'AM' if h<12 else 'PM'}"; lbl = "12 PM" if h==12 else lbl
                count = 0
                for _, r in st.session_state.main_df.iterrows():
                    if r['StartDt'] is None: continue
                    sm, em = r['StartDt'].hour*60+r['StartDt'].minute, r['EndDt'].hour*60+r['EndDt'].minute
                    if sm <= h*60 and em >= (h+1)*60:
                        on_l = False
                        if r['Lunch Time'] not in ["N/A", "Pending...", "No Slot Avail"]:
                            ld = parse_time(r['Lunch Time'])
                            if ld and (ld.hour*60 < (h+1)*60 and (ld.hour*60+60) > h*60): on_l = True
                        if not on_l:
                            act = "Pickers" if (h==4 and r['Role']=="Backroom") else r['Role']
                            if act == r_name: count += 1
                row = {"Hour": lbl, "Count": str(count)}
                if r_name == "Pickers": row["Able to Pick"] = str(count * 75)
                if r_name == "Backroom": row["Able to Dispense"] = str(count * 5)
                rows.append(row)
            st.table(pd.DataFrame(rows))
