import streamlit as st
import pdfplumber
import pandas as pd
import re
import os
from datetime import datetime, timedelta

# --- Page Configuration ---
st.set_page_config(page_title="OPD Hourly Pickers/Dispensers", layout="wide")

ROSTER_FILE = "roster.txt"
EXCLUDE_FILE = "exclude_list.txt"

# --- Helper Functions ---
def load_text_file(filepath, default_text):
    try:
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                content = f.read()
                return content if content.strip() else default_text
    except Exception:
        pass
    return default_text

def save_text_file(filepath, text, success_msg):
    try:
        with open(filepath, "w") as f:
            f.write(text)
        st.sidebar.success(success_msg)
    except Exception as e:
        st.sidebar.error("Note: Changes saved for this session, but permanent cloud saving requires a database.")

def parse_time(time_str):
    if not time_str: return None
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try: return datetime.strptime(time_str, fmt)
        except ValueError: continue
    return None

def highlight_no_slots(val):
    """Styles cells containing 'No Slot Avail' with bold red text."""
    color = 'red' if val == "No Slot Avail" else None
    return f'color: {color}; font-weight: bold' if color else ''

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
            earliest_allowed = row['StartDt'] + timedelta(hours=3)
            latest_allowed = row['StartDt'] + timedelta(hours=5)
            latest_safe = row['EndDt'] - timedelta(hours=1)
            final_latest = min(latest_allowed, latest_safe)
            
            current_guess = target
            found = False
            while current_guess <= final_latest:
                if not any(abs((current_guess - taken).total_seconds()) < 1800 for taken in taken_slots):
                    found = True
                    break
                current_guess += timedelta(minutes=30)
            
            if not found:
                current_guess = target - timedelta(minutes=30)
                while current_guess >= earliest_allowed:
                    if not any(abs((current_guess - taken).total_seconds()) < 1800 for taken in taken_slots):
                        found = True
                        break
                    current_guess -= timedelta(minutes=30)
            
            row['Lunch Time'] = current_guess.strftime("%I:%M %p") if found else "No Slot Avail"
            if found: taken_slots.append(current_guess)
            final_records.append(row.to_dict())
            
    exclude_group = df[df['Role'] == "Exclude"].to_dict('records')
    for item in exclude_group:
        item['Lunch Time'] = "N/A"
        final_records.append(item)
    return pd.DataFrame(final_records)

def process_pdf(file, associate_input, exclude_input):
    data, mismatched_names = [], []
    time_regex = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    valid_names = [name.strip().lower() for name in associate_input.split('\n') if name.strip()]
    auto_exclude_names = [name.strip().lower() for name in exclude_input.split('\n') if name.strip()]
    
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                clean = line.strip()
                match_time = re.search(time_regex, clean, re.IGNORECASE)
                if match_time:
                    if any(ex in clean.lower() for ex in auto_exclude_names): continue
                    match_name = None
                    for name_key in valid_names:
                        if name_key in clean.lower():
                            parts = name_key.title().split()
                            match_name = f"{parts[0]} {parts[1][0]}.".strip() if len(parts) > 1 else parts[0]
                            break 
                    if match_name:
                        st_dt, en_dt = parse_time(match_time.group(1)), parse_time(match_time.group(2))
                        if st_dt and en_dt:
                            end_dt_real = en_dt + timedelta(days=1) if en_dt < st_dt else en_dt
                            data.append({
                                "Associate": match_name, "Role": "Pickers", 
                                "Shift": f"{match_time.group(1)} - {match_time.group(2)}",
                                "Lunch Time": "Pending...", "StartDt": st_dt, "EndDt": end_dt_real, 
                                "Duration": (end_dt_real - st_dt).total_seconds() / 3600
                            })
                    else:
                        potential = clean.split('-')[0].split('am')[0].split('pm')[0].strip()
                        if len(potential) > 3: mismatched_names.append(potential)
    return pd.DataFrame(data), list(set(mismatched_names))

# --- Sidebar ---
st.sidebar.header("⚙️ Roster Settings")
assoc_val = load_text_file(ROSTER_FILE, "Associate Name 1")
assoc_input = st.sidebar.text_area("Associate Names (Whitelist):", value=assoc_val, height=250)
if st.sidebar.button("💾 Save Roster"): save_text_file(ROSTER_FILE, assoc_input, "Roster saved!")

st.sidebar.divider()

excl_val = load_text_file(EXCLUDE_FILE, "Manager Name")
excl_input = st.sidebar.text_area("Auto-Exclude List (Blacklist):", value=excl_val, height=200)
if st.sidebar.button("💾 Save Exclusions"): save_text_file(EXCLUDE_FILE, excl_input, "Exclusion list saved!")

# --- Main UI ---
st.title("📅 OPD Hourly Pickers/Dispensers")
uploaded_file = st.file_uploader("Upload Roster PDF", type="pdf")

if uploaded_file:
    if 'main_df' not in st.session_state or st.sidebar.button("🔄 Reload PDF"):
        new_df, mismatches = process_pdf(uploaded_file, assoc_input, excl_input)
        st.session_state.main_df, st.session_state.mismatches, st.session_state.calculated = new_df, mismatches, False

    if st.session_state.get('mismatches'):
        with st.expander("⚠️ Found shifts for names NOT on either list"):
            st.code("\n".join(st.session_state.mismatches))

    df = st.session_state.main_df

    # 1. Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🛒 Pickers", len(df[df['Role'] == 'Pickers']))
    m2.metric("📦 Backroom", len(df[df['Role'] == 'Backroom']))
    m3.metric("⚠️ Exceptions", len(df[df['Role'] == 'Exceptions']))
    m4.metric("❌ Excluded", len(df[df['Role'] == 'Exclude']))

    st.divider()

    # 2. Controls
    c_col1, c_col2 = st.columns([2, 1])
    with c_col1:
        st.subheader("Bulk Role Assignment")
        s1, s2, s3 = st.columns([2, 1, 1])
        selected = s1.multiselect("Select Associates:", options=sorted(df['Associate'].tolist()))
        target = s2.selectbox("Set Role:", ["Pickers", "Backroom", "Exceptions", "Exclude"])
        if s3.button("🚀 Apply"):
            st.session_state.main_df.loc[st.session_state.main_df['Associate'].isin(selected), 'Role'] = target
            st.rerun()
    with c_col2:
        st.subheader("Finalize")
        if st.button("🔥 GENERATE TABLES", type="primary", use_container_width=True):
            st.session_state.main_df = calculate_staggered_lunches(st.session_state.main_df)
            st.session_state.calculated = True
            st.rerun()

    # 3. Master Editor
    st.subheader("Master Daily Roster")
    full_time_list = []
    base_t = datetime(2025, 1, 1, 0, 0)
    for i in range(48):
        full_time_list.append((base_t + timedelta(minutes=30*i)).strftime("%I:%M %p"))
    all_possible_options = ["N/A", "No Slot Avail", "Pending..."] + full_time_list

    styled_master = st.session_state.main_df.style.applymap(highlight_no_slots, subset=['Lunch Time'])

    edited_df = st.data_editor(styled_master, column_config={
        "Associate": st.column_config.TextColumn("Associate", disabled=True),
        "Role": st.column_config.SelectboxColumn("Role", options=["Pickers", "Backroom", "Exceptions", "Exclude"], required=True),
        "Shift": st.column_config.TextColumn("Shift", disabled=True),
        "Lunch Time": st.column_config.SelectboxColumn("Lunch Time", options=all_possible_options, required=False),
        "StartDt": None, "EndDt": None, "Duration": None
    }, use_container_width=True, hide_index=True)
    
    if not edited_df.equals(st.session_state.main_df):
        st.session_state.main_df = edited_df
        st.rerun()

    # 4. Final Output
    if st.session_state.get('calculated'):
        st.divider()
        st.header("📊 Hourly Count (4 AM - 10 PM)")
        h_tabs = st.tabs(["🛒 Pickers Count", "📦 Backroom Count", "⚠️ Exceptions Count"])
        
        def get_h_df(tab_role, label=None, mult=None):
            rows = []
            for h in range(4, 23):
                lbl = f"{h if h<=12 else h-12} {'AM' if h<12 else 'PM'}"; lbl = "12 PM" if h==12 else lbl
                count = 0
                for _, r in st.session_state.main_df.iterrows():
                    s_min, e_min = r['StartDt'].hour*60+r['StartDt'].minute, r['EndDt'].hour*60+r['EndDt'].minute
                    if s_min <= h*60 and e_min >= (h+1)*60:
                        on_l = False
                        if r['Lunch Time'] not in ["N/A", "Pending...", "No Slot Avail"]:
                            l_dt = parse_time(r['Lunch Time'])
                            if l_dt:
                                l_s = l_dt.hour*60+l_dt.minute; l_e = l_s+60
                                if l_s < (h+1)*60 and l_e > h*60: on_l = True
                        if not on_l:
                            current_role = r['Role']
                            active_role = "Pickers" if (h == 4 and current_role == "Backroom") else current_role
                            if active_role == tab_role: count += 1
                row = {"Hour": lbl, "Count": str(count)}
                if label: row[label] = str(count * mult)
                rows.append(row)
            return pd.DataFrame(rows)

        base_cfg = {"Hour": st.column_config.TextColumn("Hour"), "Count": st.column_config.TextColumn("Count")}
        with h_tabs[0]: st.dataframe(get_h_df("Pickers", "Able to Pick", 75), use_container_width=True, hide_index=True, column_config={**base_cfg, "Able to Pick": st.column_config.TextColumn("Able to Pick")})
        with h_tabs[1]: st.dataframe(get_h_df("Backroom", "Able to Dispense", 5), use_container_width=True, hide_index=True, column_config={**base_cfg, "Able to Dispense": st.column_config.TextColumn("Able to Dispense")})
        with h_tabs[2]: st.dataframe(get_h_df("Exceptions"), use_container_width=True, hide_index=True, column_config=base_cfg)

        st.divider()
        st.header("📋 Associates Lunches")
        l_tabs = st.tabs(["🛒 Pickers", "📦 Backroom", "⚠️ Exceptions"])
        for i, r_name in enumerate(["Pickers", "Backroom", "Exceptions"]):
            with l_tabs[i]:
                display_df = st.session_state.main_df[st.session_state.main_df['Role']==r_name][["Associate", "Shift", "Lunch Time", "StartDt"]].sort_values("StartDt")
                st.dataframe(display_df[["Associate", "Shift", "Lunch Time"]].style.applymap(highlight_no_slots, subset=['Lunch Time']), use_container_width=True, hide_index=True)

        # Simple CSV Download
        csv_data = st.session_state.main_df[["Associate", "Role", "Shift", "Lunch Time"]].to_csv(index=False).encode('utf-8')
        st.sidebar.divider()
        st.sidebar.download_button(
            label="📥 DOWNLOAD DAILY ROSTER (CSV)", 
            data=csv_data, 
            file_name=f"OPD_Roster_{datetime.now().strftime('%Y-%m-%d')}.csv", 
            mime="text/csv", 
            use_container_width=True

        )
