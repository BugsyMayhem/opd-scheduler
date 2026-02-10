import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection

# --- Page Configuration ---
st.set_page_config(page_title="OPD Hourly Pickers/Dispensers", layout="wide")

# --- Google Sheets Connection ---
conn = st.connection("gsheets", type=GSheetsConnection)

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
        st.error(f"Error connecting to Google Sheets: {e}")
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

def parse_time(time_str):
    if not time_str: return None
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%I:%M"):
        try: return datetime.strptime(time_str, fmt)
        except ValueError: continue
    return None

# --- CLEANED STYLING LOGIC ---
def style_roster(df):
    def apply_styles(row):
        styles = [''] * len(row)
        if 'Lunch Time' in row.index and row['Lunch Time'] == "No Slot Avail":
            lunch_idx = row.index.get_loc('Lunch Time')
            styles[lunch_idx] = 'color: red; font-weight: bold'
        return styles
    return df.style.apply(apply_styles, axis=1)

def clean_name_icons(name):
    return name.replace("💖 ", "").replace("💙 ", "").replace("💛 ", "")

def add_role_icons(df):
    for idx, row in df.iterrows():
        clean_name = clean_name_icons(row['Associate'])
        if row['Role'] == "Pickers":
            df.at[idx, 'Associate'] = f"💖 {clean_name}"
        elif row['Role'] == "Backroom":
            df.at[idx, 'Associate'] = f"💙 {clean_name}"
        elif row['Role'] == "Exceptions":
            df.at[idx, 'Associate'] = f"💛 {clean_name}"
        else:
            df.at[idx, 'Associate'] = clean_name
    return df

def calculate_staggered_lunches(df):
    if df.empty: return df
    final_records = []
    active_roles = ["Pickers", "Backroom", "Exceptions"]
    for role in active_roles:
        role_group = df[df['Role'] == role].sort_values(by='StartDt').copy()
        taken_slots = []
        for _, row in role_group.iterrows():
            # UPDATED RULE: Shifts less than 6 hours get N/A. Exactly 6 hours now get assigned.
            if row['Duration'] < 6:
                row['Lunch Time'] = "N/A"; final_records.append(row.to_dict()); continue
            target, early, late = row['StartDt'] + timedelta(hours=4), row['StartDt'] + timedelta(hours=3), row['StartDt'] + timedelta(hours=5)
            safe_limit = min(late, row['EndDt'] - timedelta(hours=1))
            curr, found = target, False
            while curr <= safe_limit:
                if not any(abs((curr - t).total_seconds()) < 1800 for t in taken_slots):
                    found = True; break
                curr += timedelta(minutes=30)
            if not found:
                curr = target - timedelta(minutes=30)
                while curr >= early:
                    if not any(abs((curr - t).total_seconds()) < 1800 for t in taken_slots):
                        found = True; break
                    curr -= timedelta(minutes=30)
            row['Lunch Time'] = curr.strftime("%I:%M %p") if found else "No Slot Avail"
            if found: taken_slots.append(curr)
            final_records.append(row.to_dict())
    ex_group = df[df['Role'] == "Exclude"].to_dict('records')
    for item in ex_group:
        item['Lunch Time'] = "N/A"; final_records.append(item)
    return pd.DataFrame(final_records)

def process_pdf(file, associate_input, exclude_input):
    data, mismatches = [], []
    t_regex = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    v_list = []
    for n in associate_input.split('\n'):
        if n.strip():
            raw = n.strip().lower()
            clean = raw.replace("(m)", "").replace("minor", "").strip()
            v_list.append({"search": clean, "raw": n.strip(), "is_minor": ("(m)" in raw or "minor" in raw)})
    e_names = [n.strip().lower() for n in exclude_input.split('\n') if n.strip()]
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
                    else:
                        pot = line.split('-')[0].split('am')[0].split('pm')[0].strip()
                        if len(pot) > 3: mismatches.append(pot)
    return pd.DataFrame(data), list(set(mismatches))

# --- Sidebar ---
st.sidebar.header("☁️ Database Management")
if 'r_val' not in st.session_state:
    r, e = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r, e

sync_time = st.session_state.get('last_sync', 'Never')
col_s1, col_s2 = st.sidebar.columns([3, 1])
col_s1.write(f"**Last Sync:** {sync_time}")
if col_s2.button("🔄"):
    r, e = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r, e
    st.rerun()

assoc_input = st.sidebar.text_area("Whitelist", value=st.session_state.r_val, height=200)
excl_input = st.sidebar.text_area("Blacklist", value=st.session_state.e_val, height=150)
if st.sidebar.button("💾 SAVE PERMANENTLY", use_container_width=True):
    save_lists_to_sheets(assoc_input, excl_input)
    st.session_state.r_val, st.session_state.e_val = assoc_input, excl_input

# --- Main UI ---
st.title("📅 OPD Hourly Pickers/Dispensers")

if 'main_df' not in st.session_state:
    st.session_state.main_df = pd.DataFrame(columns=["Associate", "Role", "Shift", "Lunch Time", "StartDt", "EndDt", "Duration"])

with st.expander("➕ Manually Add Associate"):
    ma1, ma2, ma3, ma4 = st.columns([2, 1, 1, 1])
    whitelist = sorted([n.strip() for n in assoc_input.split('\n') if n.strip()])
    new_a = ma1.selectbox("Name", options=whitelist if whitelist else ["No Names in Database"])
    new_r = ma2.selectbox("Role", options=["Pickers", "Backroom", "Exceptions", "Exclude"])
    new_s = ma3.text_input("Shift (5am-2pm)")
    l_opts = ["Pending...", "N/A", "No Slot Avail"] + [(datetime(2025,1,1,0,0)+timedelta(minutes=30*i)).strftime("%I:%M %p") for i in range(48)]
    new_l = ma4.selectbox("Lunch", options=l_opts)
    
    if st.button("➕ Add to Roster", use_container_width=True):
        try:
            ts = new_s.lower().replace(" ", "").split('-')
            s_dt, e_dt = parse_time(ts[0]), parse_time(ts[1])
            r_e = e_dt + timedelta(days=1) if e_dt < s_dt else e_dt
            dur = (r_e - s_dt).total_seconds() / 3600
        except: s_dt, r_e, dur = None, None, 0
        
        disp_name = new_a
        if "(m)" in new_a.lower() or "minor" in new_a.lower():
            p = new_a.replace("(m)", "").replace("minor", "").strip().title().split()
            disp_name = f"👶 {p[0]} {p[1][0]}." if len(p) > 1 else f"👶 {p[0]}"
        else:
            p = new_a.title().split()
            disp_name = f"{p[0]} {p[1][0]}." if len(p) > 1 else p[0]
            
        nr = pd.DataFrame([{"Associate": disp_name, "Role": new_r, "Shift": new_s, "Lunch Time": new_l, "StartDt": s_dt, "EndDt": r_e, "Duration": dur}])
        st.session_state.main_df = pd.concat([st.session_state.main_df, nr], ignore_index=True)
        st.rerun()

st.divider()

uploaded_file = st.file_uploader("Upload Roster PDF", type="pdf")
if uploaded_file and st.button("📂 Load PDF into Roster"):
    new_df, mismatches = process_pdf(uploaded_file, assoc_input, excl_input)
    st.session_state.main_df = pd.concat([st.session_state.main_df, new_df], ignore_index=True)
    st.session_state.mismatches = mismatches
    st.rerun()

df = st.session_state.main_df
if not df.empty:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🛒 Pickers", len(df[df['Role'] == 'Pickers']))
    m2.metric("📦 Backroom", len(df[df['Role'] == 'Backroom']))
    m3.metric("⚠️ Exceptions", len(df[df['Role'] == 'Exceptions']))
    m4.metric("❌ Excluded", len(df[df['Role'] == 'Exclude']))

    st.divider()

    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader("Bulk Actions")
        s1, s2, s3, s4 = st.columns([2, 1, 1, 1])
        selected = s1.multiselect("Select People:", options=df['Associate'].tolist())
        target = s2.selectbox("Assign Role:", ["Pickers", "Backroom", "Exceptions", "Exclude"])
        if s3.button("🚀 Apply"):
            st.session_state.main_df.loc[st.session_state.main_df['Associate'].isin(selected), 'Role'] = target
            st.session_state.main_df = add_role_icons(st.session_state.main_df)
            st.rerun()
        if s4.button("🗑️ Delete"):
            st.session_state.main_df = st.session_state.main_df[~st.session_state.main_df['Associate'].isin(selected)]
            st.rerun()

    with c2:
        st.subheader("Finalize")
        if st.button("🔥 GENERATE LUNCHES", type="primary", use_container_width=True):
            st.session_state.main_df = add_role_icons(st.session_state.main_df)
            st.session_state.main_df = calculate_staggered_lunches(st.session_state.main_df)
            st.session_state.calculated = True
            st.rerun()

    st.subheader("Master Daily Roster")
    st.session_state.main_df = add_role_icons(st.session_state.main_df)
    styled_master = style_roster(st.session_state.main_df)

    edited_df = st.data_editor(styled_master, column_config={
        "Associate": st.column_config.TextColumn(disabled=False),
        "Role": st.column_config.SelectboxColumn(options=["Pickers", "Backroom", "Exceptions", "Exclude"]),
        "Shift": st.column_config.TextColumn(disabled=False),
        "Lunch Time": st.column_config.SelectboxColumn(options=l_opts),
        "StartDt": None, "EndDt": None, "Duration": None
    }, use_container_width=True, hide_index=True)
    
    if not edited_df.equals(st.session_state.main_df):
        st.session_state.main_df = edited_df
        st.rerun()

if st.session_state.get('calculated'):
    st.divider()
    h_tabs = st.tabs(["🛒 Pickers Count", "📦 Backroom Count", "⚠️ Exceptions Count"])
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

    st.divider()
    st.header("📋 Associates Lunches")
    l_tabs = st.tabs(["🛒 Pickers", "📦 Backroom", "⚠️ Exceptions"])
    for i, r_name in enumerate(["Pickers", "Backroom", "Exceptions"]):
        with l_tabs[i]:
            ldf = st.session_state.main_df[st.session_state.main_df['Role']==r_name][["Associate", "Shift", "Lunch Time", "Role", "StartDt"]].sort_values("StartDt")
            st.dataframe(
                style_roster(ldf), 
                column_config={"Role": None, "StartDt": None},
                use_container_width=True, 
                hide_index=True
            )

    csv = st.session_state.main_df[["Associate", "Role", "Shift", "Lunch Time"]].to_csv(index=False).encode('utf-8')
    st.sidebar.download_button("📥 DOWNLOAD CSV", csv, f"OPD_{datetime.now().strftime('%Y-%m-%d')}.csv", "text/csv", use_container_width=True)
