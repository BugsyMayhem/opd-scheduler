import streamlit as st
import pdfplumber
import pandas as pd
import re
import os
import io
from datetime import datetime, timedelta

# --- Page Configuration ---
st.set_page_config(page_title="OPD Hourly Pickers/Dispensers", layout="wide")

# --- Cloud-Friendly Logic (Secrets) ---
def load_data(key, default_text):
    if key in st.secrets:
        return st.secrets[key]
    return default_text

def parse_time(time_str):
    if not time_str: return None
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try: return datetime.strptime(time_str, fmt)
        except ValueError: continue
    return None

def highlight_no_slots(val):
    return 'color: red; font-weight: bold' if val == "No Slot Avail" else ''

# --- Lunch Calculation ---
def calculate_staggered_lunches(df):
    if df.empty: return df
    final_records = []
    active_roles = ["Pickers", "Backroom", "Exceptions"]
    for role in active_roles:
        role_group = df[df['Role'] == role].sort_values(by='StartDt').copy()
        taken_slots = []
        for _, row in role_group.iterrows():
            if row['Duration'] <= 6:
                row['Lunch Time'] = "N/A"
                final_records.append(row.to_dict())
                continue
            
            target = row['StartDt'] + timedelta(hours=4)
            earliest = row['StartDt'] + timedelta(hours=3)
            latest = row['StartDt'] + timedelta(hours=5)
            safe_limit = row['EndDt'] - timedelta(hours=1)
            final_latest = min(latest, safe_limit)
            
            curr = target
            found = False
            while curr <= final_latest:
                if not any(abs((curr - t).total_seconds()) < 1800 for t in taken_slots):
                    found = True; break
                curr += timedelta(minutes=30)
            if not found:
                curr = target - timedelta(minutes=30)
                while curr >= earliest:
                    if not any(abs((curr - t).total_seconds()) < 1800 for t in taken_slots):
                        found = True; break
                    curr -= timedelta(minutes=30)
            
            row['Lunch Time'] = curr.strftime("%I:%M %p") if found else "No Slot Avail"
            if found: taken_slots.append(curr)
            final_records.append(row.to_dict())
            
    ex_group = df[df['Role'] == "Exclude"].to_dict('records')
    for item in ex_group:
        item['Lunch Time'] = "N/A"
        final_records.append(item)
    return pd.DataFrame(final_records)

# --- PDF Processing ---
def process_pdf(file, roster_text, exclude_text):
    data, mismatches = [], []
    t_regex = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    v_names = [n.strip().lower() for n in roster_text.split('\n') if n.strip()]
    e_names = [n.strip().lower() for n in exclude_text.split('\n') if n.strip()]
    
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                m = re.search(t_regex, line, re.IGNORECASE)
                if m:
                    if any(ex in line.lower() for ex in e_names): continue
                    match_name = None
                    for name_key in v_names:
                        if name_key in line.lower():
                            parts = name_key.title().split()
                            match_name = f"{parts[0]} {parts[1][0]}." if len(parts) > 1 else parts[0]
                            break 
                    if match_name:
                        st_dt, en_dt = parse_time(m.group(1)), parse_time(m.group(2))
                        if st_dt and en_dt:
                            real_end = en_dt + timedelta(days=1) if en_dt < st_dt else en_dt
                            data.append({"Associate": match_name, "Role": "Pickers", "Shift": f"{m.group(1)} - {m.group(2)}", "Lunch Time": "Pending...", "StartDt": st_dt, "EndDt": real_end, "Duration": (real_end - st_dt).total_seconds()/3600})
                    else:
                        pot = line.split('-')[0].split('am')[0].split('pm')[0].strip()
                        if len(pot) > 3: mismatches.append(pot)
    return pd.DataFrame(data), list(set(mismatches))

# --- Main UI ---
st.title("📅 OPD Hourly Pickers/Dispensers")

with st.sidebar:
    st.header("⚙️ Settings")
    r_input = st.text_area("Roster (Whitelist)", value=load_data("roster_data", "Name 1"), height=200)
    e_input = st.text_area("Auto-Exclude (Blacklist)", value=load_data("exclude_data", "Manager"), height=150)

uploaded_file = st.file_uploader("Upload Roster PDF", type="pdf")

if uploaded_file:
    if 'main_df' not in st.session_state or st.sidebar.button("🔄 Reload PDF"):
        df, miss = process_pdf(uploaded_file, r_input, e_input)
        st.session_state.main_df, st.session_state.mismatches, st.session_state.calc = df, miss, False

    df = st.session_state.main_df

    # 1. Metrics (Restored)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🛒 Pickers", len(df[df['Role'] == 'Pickers']))
    m2.metric("📦 Backroom", len(df[df['Role'] == 'Backroom']))
    m3.metric("⚠️ Exceptions", len(df[df['Role'] == 'Exceptions']))
    m4.metric("❌ Excluded", len(df[df['Role'] == 'Exclude']))

    st.divider()

    # 2. Controls & Bulk Assignment (Restored)
    c_a, c_b = st.columns([2, 1])
    with c_a:
        st.subheader("Bulk Role Assignment")
        s1, s2, s3 = st.columns([2, 1, 1])
        selected = s1.multiselect("Select Associates:", options=sorted(df['Associate'].tolist()))
        target = s2.selectbox("Set Role:", ["Pickers", "Backroom", "Exceptions", "Exclude"])
        if s3.button("🚀 Apply"):
            st.session_state.main_df.loc[st.session_state.main_df['Associate'].isin(selected), 'Role'] = target
            st.rerun()
    with c_b:
        st.subheader("Finalize")
        if st.button("🔥 GENERATE TABLES", type="primary", use_container_width=True):
            st.session_state.main_df = calculate_staggered_lunches(st.session_state.main_df)
            st.session_state.calc = True
            st.rerun()

    # 3. Master Editor
    st.subheader("Master Daily Roster")
    full_times = ["N/A", "No Slot Avail", "Pending..."] + [(datetime(2025,1,1,0,0)+timedelta(minutes=30*i)).strftime("%I:%M %p") for i in range(48)]
    edited_df = st.data_editor(st.session_state.main_df.style.applymap(highlight_no_slots, subset=['Lunch Time']), column_config={
        "Associate": st.column_config.TextColumn(disabled=True),
        "Role": st.column_config.SelectboxColumn(options=["Pickers", "Backroom", "Exceptions", "Exclude"]),
        "Shift": st.column_config.TextColumn(disabled=True),
        "Lunch Time": st.column_config.SelectboxColumn(options=full_times),
        "StartDt": None, "EndDt": None, "Duration": None
    }, use_container_width=True, hide_index=True)
    
    if not edited_df.equals(st.session_state.main_df):
        st.session_state.main_df = edited_df
        st.rerun()

    # 4. Hourly Counts & Lunch Tabs
    if st.session_state.calc:
        st.divider()
        h_tabs = st.tabs(["🛒 Pickers Count", "📦 Backroom Count", "⚠️ Exceptions Count"])
        for i, role in enumerate(["Pickers", "Backroom", "Exceptions"]):
            with h_tabs[i]:
                rows = []
                for h in range(4, 23):
                    lbl = f"{h if h<=12 else h-12} {'AM' if h<12 else 'PM'}"; lbl = "12 PM" if h==12 else lbl
                    count = 0
                    for _, r in st.session_state.main_df.iterrows():
                        s_m, e_m = r['StartDt'].hour*60+r['StartDt'].minute, r['EndDt'].hour*60+r['EndDt'].minute
                        if s_m <= h*60 and e_m >= (h+1)*60:
                            on_l = False
                            if r['Lunch Time'] not in ["N/A", "Pending...", "No Slot Avail"]:
                                l_dt = parse_time(r['Lunch Time'])
                                if l_dt and (l_dt.hour*60 < (h+1)*60 and (l_dt.hour*60+60) > h*60): on_l = True
                            if not on_l:
                                act_role = "Pickers" if (h == 4 and r['Role'] == "Backroom") else r['Role']
                                if act_role == role: count += 1
                    row = {"Hour": lbl, "Count": count}
                    if role == "Pickers": row["Able to Pick"] = count * 75
                    if role == "Backroom": row["Able to Dispense"] = count * 5
                    rows.append(row)
                st.table(pd.DataFrame(rows)) # st.table is more stable for mobile

        st.divider()
        st.header("📋 Associates Lunches")
        l_tabs = st.tabs(["🛒 Pickers", "📦 Backroom", "⚠️ Exceptions"])
        for i, r_name in enumerate(["Pickers", "Backroom", "Exceptions"]):
            with l_tabs[i]:
                l_df = st.session_state.main_df[st.session_state.main_df['Role']==r_name][["Associate", "Shift", "Lunch Time", "StartDt"]].sort_values("StartDt")
                st.dataframe(l_df[["Associate", "Shift", "Lunch Time"]].style.applymap(highlight_no_slots, subset=['Lunch Time']), use_container_width=True, hide_index=True)

        # 5. CSV Download (Restored)
        csv = st.session_state.main_df[["Associate", "Role", "Shift", "Lunch Time"]].to_csv(index=False).encode('utf-8')
        st.sidebar.download_button("📥 DOWNLOAD CSV", csv, f"OPD_{datetime.now().strftime('%Y-%m-%d')}.csv", "text/csv", use_container_width=True)
