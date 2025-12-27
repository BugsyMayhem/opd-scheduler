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

def get_local_time():
    """Automatically adjusts for Central Standard (UTC-6) or Daylight (UTC-5) Time."""
    now_utc = datetime.utcnow()
    
    # Simple Daylight Savings Check for Central Time (Standard: 2nd Sun March - 1st Sun Nov)
    # This logic covers most years accurately for US Central Time
    year = now_utc.year
    dst_start = datetime(year, 3, 8) + timedelta(days=(6 - datetime(year, 3, 8).weekday()) % 7)
    dst_end = datetime(year, 11, 1) + timedelta(days=(6 - datetime(year, 11, 1).weekday()) % 7)
    
    if dst_start <= now_utc <= dst_end:
        offset = 5  # CDT
    else:
        offset = 6  # CST
        
    return (now_utc - timedelta(hours=offset)).strftime("%I:%M %p")

def load_lists_from_sheets():
    try:
        url = st.secrets["connections"]["gsheets"]["spreadsheet"]
        roster_df = conn.read(spreadsheet=url, worksheet="Roster", ttl=0) 
        exclude_df = conn.read(spreadsheet=url, worksheet="Exclude", ttl=0)
        
        roster_names = "\n".join(roster_df.iloc[:, 0].dropna().astype(str).tolist())
        exclude_names = "\n".join(exclude_df.iloc[:, 0].dropna().astype(str).tolist())
        
        st.session_state.last_sync = get_local_time()
        return roster_names, exclude_names
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

# --- Logic Functions ---
def parse_time(time_str):
    if not time_str: return None
    time_str = time_str.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try: return datetime.strptime(time_str, fmt)
        except ValueError: continue
    return None

def highlight_no_slots(val):
    return 'color: red; font-weight: bold' if val == "No Slot Avail" else ''

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
            
            # Standard rules for everyone (including Minors)
            target = row['StartDt'] + timedelta(hours=4)
            earliest = row['StartDt'] + timedelta(hours=3)
            latest = row['StartDt'] + timedelta(hours=5)
            safe_limit = min(latest, row['EndDt'] - timedelta(hours=1))
            
            curr, found = target, False
            while curr <= safe_limit:
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
            
    # Add Excluded back in
    ex_group = df[df['Role'] == "Exclude"].to_dict('records')
    for item in ex_group:
        item['Lunch Time'] = "N/A"
        final_records.append(item)
    return pd.DataFrame(final_records)

def process_pdf(file, associate_input, exclude_input):
    data, mismatches = [], []
    t_regex = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
    
    # Create a list of dictionaries to track name vs. minor status
    v_names_list = []
    for n in associate_input.split('\n'):
        if n.strip():
            raw_name = n.strip().lower()
            # Clean name for searching (remove (m) or minor)
            search_name = raw_name.replace("(m)", "").replace("minor", "").strip()
            v_names_list.append({
                "search": search_name,
                "display_raw": n.strip(),
                "is_minor": ("(m)" in raw_name or "minor" in raw_name)
            })
            
    e_names = [n.strip().lower() for n in exclude_input.split('\n') if n.strip()]
    
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text: continue
            for line in text.split('\n'):
                m = re.search(t_regex, line.strip(), re.IGNORECASE)
                if m:
                    # 1. Check Blacklist first
                    if any(ex in line.lower() for ex in e_names): 
                        continue
                    
                    match_name = None
                    is_minor = False
                    
                    # 2. Search for the CLEAN name in the PDF line
                    for entry in v_names_list:
                        if entry["search"] in line.lower():
                            is_minor = entry["is_minor"]
                            # Format for display: "Khloe W."
                            parts = entry["search"].title().split()
                            formatted = f"{parts[0]} {parts[1][0]}.".strip() if len(parts) > 1 else parts[0]
                            
                            # Add icon if minor
                            match_name = f"👶 {formatted}" if is_minor else formatted
                            break 
                    
                    if match_name:
                        st_dt, en_dt = parse_time(m.group(1)), parse_time(m.group(2))
                        if st_dt and en_dt:
                            real_end = en_dt + timedelta(days=1) if en_dt < st_dt else en_dt
                            data.append({
                                "Associate": match_name, 
                                "Role": "Pickers", 
                                "Shift": f"{m.group(1)} - {m.group(2)}", 
                                "Lunch Time": "Pending...", 
                                "StartDt": st_dt, 
                                "EndDt": real_end, 
                                "Duration": (real_end - st_dt).total_seconds() / 3600
                            })
                    else:
                        # If no match found in whitelist, add to mismatch list
                        pot = line.split('-')[0].split('am')[0].split('pm')[0].strip()
                        if len(pot) > 3: 
                            mismatches.append(pot)
                            
    return pd.DataFrame(data), list(set(mismatches))

# --- Sidebar ---
st.sidebar.header("☁️ Database Management")

if 'last_sync' not in st.session_state:
    r_val, e_val = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r_val, e_val

# Status Header
sync_time = st.session_state.get('last_sync', 'Never')
col1, col2 = st.sidebar.columns([3, 1])
col1.write(f"**Last Sync:** {sync_time}")
if col2.button("🔄"):
    r, e = load_lists_from_sheets()
    st.session_state.r_val, st.session_state.e_val = r, e
    st.rerun()

assoc_input = st.sidebar.text_area("Whitelist", value=st.session_state.r_val, height=200)
excl_input = st.sidebar.text_area("Blacklist", value=st.session_state.e_val, height=150)

if st.sidebar.button("💾 SAVE PERMANENTLY", use_container_width=True):
    save_lists_to_sheets(assoc_input, excl_input)
    st.session_state.r_val, st.session_state.e_val = assoc_input, excl_input

# ... [Keep your previous imports and helper functions] ...

# --- Main UI ---
st.title("📅 OPD Hourly Pickers/Dispensers")
uploaded_file = st.file_uploader("Upload Roster PDF", type="pdf")

if uploaded_file or 'main_df' in st.session_state:
    # 1. Initialize data if not already present
    if uploaded_file and ('main_df' not in st.session_state or st.sidebar.button("🔄 Reload PDF")):
        new_df, mismatches = process_pdf(uploaded_file, assoc_input, excl_input)
        st.session_state.main_df, st.session_state.mismatches, st.session_state.calculated = new_df, mismatches, False

    # Fallback if someone hits refresh and session state is empty
    if 'main_df' not in st.session_state:
        st.session_state.main_df = pd.DataFrame(columns=["Associate", "Role", "Shift", "Lunch Time", "StartDt", "EndDt", "Duration"])

    df = st.session_state.main_df

    # 2. Metrics (Restored)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🛒 Pickers", len(df[df['Role'] == 'Pickers']))
    m2.metric("📦 Backroom", len(df[df['Role'] == 'Backroom']))
    m3.metric("⚠️ Exceptions", len(df[df['Role'] == 'Exceptions']))
    m4.metric("❌ Excluded", len(df[df['Role'] == 'Exclude']))

    st.divider()

    # 3. Manual Entry Section (NEW)
    with st.expander("➕ Manually Add Associate to Roster"):
        ma_col1, ma_col2, ma_col3, ma_col4 = st.columns([2, 1, 1, 1])
        
        # Clean the whitelist for the dropdown
        whitelist_options = sorted([n.strip() for n in assoc_input.split('\n') if n.strip()])
        
        new_assoc = ma_col1.selectbox("Associate", options=whitelist_options)
        new_role = ma_col2.selectbox("Role", options=["Pickers", "Backroom", "Exceptions", "Exclude"])
        new_shift = ma_col3.text_input("Shift (e.g. 5am-2pm)")
        
        # Create lunch list for the dropdown
        lunch_opts = ["Pending...", "N/A", "No Slot Avail"] + [(datetime(2025,1,1,0,0)+timedelta(minutes=30*i)).strftime("%I:%M %p") for i in range(48)]
        new_lunch = ma_col4.selectbox("Lunch Time", options=lunch_opts)
        
        if st.button("➕ Add to Roster", use_container_width=True):
            # Try to parse times for the manual shift for math compatibility
            try:
                # Basic parsing: assumes format like '5am-2pm'
                times = new_shift.lower().replace(" ", "").split('-')
                st_dt = parse_time(times[0])
                en_dt = parse_time(times[1])
                real_end = en_dt + timedelta(days=1) if en_dt < st_dt else en_dt
                duration = (real_end - st_dt).total_seconds() / 3600
            except:
                st_dt, real_end, duration = None, None, 0
                st.warning("Could not calculate math for this shift. Use format '5am-2pm'.")

            # Format name with 👶 if they are a minor in the whitelist
            display_name = new_assoc
            if "(m)" in new_assoc.lower() or "minor" in new_assoc.lower():
                clean_n = new_assoc.replace("(m)", "").replace("minor", "").strip().title().split()
                display_name = f"👶 {clean_n[0]} {clean_n[1][0]}." if len(clean_n) > 1 else f"👶 {clean_n[0]}"
            else:
                clean_n = new_assoc.title().split()
                display_name = f"{clean_n[0]} {clean_n[1][0]}." if len(clean_n) > 1 else clean_n[0]

            new_row = pd.DataFrame([{
                "Associate": display_name, "Role": new_role, "Shift": new_shift,
                "Lunch Time": new_lunch, "StartDt": st_dt, "EndDt": real_end, "Duration": duration
            }])
            st.session_state.main_df = pd.concat([st.session_state.main_df, new_row], ignore_index=True)
            st.rerun()

    # 4. Controls & Bulk Assignment
    c_col1, c_col2 = st.columns([2, 1])
    with c_col1:
        st.subheader("Bulk Role Assignment")
        s1, s2, s3 = st.columns([2, 1, 1])
        selected = s1.multiselect("Select Associates:", options=sorted(st.session_state.main_df['Associate'].tolist()))
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

    # 5. Master Editor
    st.subheader("Master Daily Roster")
    full_time_list = ["N/A", "No Slot Avail", "Pending..."] + [(datetime(2025, 1, 1, 0, 0) + timedelta(minutes=30*i)).strftime("%I:%M %p") for i in range(48)]

    edited_df = st.data_editor(st.session_state.main_df.style.applymap(highlight_no_slots, subset=['Lunch Time']), column_config={
        "Associate": st.column_config.TextColumn("Associate", disabled=False), # Enabled so you can tweak names
        "Role": st.column_config.SelectboxColumn("Role", options=["Pickers", "Backroom", "Exceptions", "Exclude"], required=True),
        "Shift": st.column_config.TextColumn("Shift", disabled=False),
        "Lunch Time": st.column_config.SelectboxColumn("Lunch Time", options=full_time_list, required=False),
        "StartDt": None, "EndDt": None, "Duration": None
    }, use_container_width=True, hide_index=False) # Hide index False helps you select rows to delete if needed
    
    if not edited_df.equals(st.session_state.main_df):
        st.session_state.main_df = edited_df
        st.rerun()

# ... [Keep the rest of your Final Output/Table logic] ...

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
                        s_m, e_m = r['StartDt'].hour*60+r['StartDt'].minute, r['EndDt'].hour*60+r['EndDt'].minute
                        if s_m <= h*60 and e_m >= (h+1)*60:
                            on_l = False
                            if r['Lunch Time'] not in ["N/A", "Pending...", "No Slot Avail"]:
                                l_dt = parse_time(r['Lunch Time'])
                                if l_dt and (l_dt.hour*60 < (h+1)*60 and (l_dt.hour*60+60) > h*60): on_l = True
                            if not on_l:
                                act = "Pickers" if (h==4 and r['Role']=="Backroom") else r['Role']
                                if act == r_name: count += 1
                    row = {"Hour": lbl, "Count": str(count)}
                    if r_name == "Pickers": row["Able to Pick"] = str(count * 75)
                    if r_name == "Backroom": row["Able to Dispense"] = str(count * 5)
                    rows.append(row)
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.divider()
        st.header("📋 Associates Lunches")
        l_tabs = st.tabs(["🛒 Pickers", "📦 Backroom", "⚠️ Exceptions"])
        for i, r_name in enumerate(["Pickers", "Backroom", "Exceptions"]):
            with l_tabs[i]:
                l_df = st.session_state.main_df[st.session_state.main_df['Role']==r_name][["Associate", "Shift", "Lunch Time", "StartDt"]].sort_values("StartDt")
                st.dataframe(l_df[["Associate", "Shift", "Lunch Time"]].style.applymap(highlight_no_slots, subset=['Lunch Time']), use_container_width=True, hide_index=True)

        csv = st.session_state.main_df[["Associate", "Role", "Shift", "Lunch Time"]].to_csv(index=False).encode('utf-8')
        st.sidebar.download_button("📥 DOWNLOAD CSV", csv, f"OPD_{datetime.now().strftime('%Y-%m-%d')}.csv", "text/csv", use_container_width=True)





