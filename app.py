import time

import streamlit as st
import pandas as pd
import google.generativeai as genai

import job_agent_core as core
import job_store as store

# Page settings layout config
st.set_page_config(page_title="AI Job Agent Workspace", layout="wide", page_icon="🎯")

store.init_db(seed_defaults=core.default_profile_seed())

# ==========================================
# SIDEBAR: PROFILE SELECTION
# ==========================================
st.sidebar.title("🛠️ Agent Settings")
st.sidebar.markdown("---")
st.sidebar.subheader("👤 Candidate Profile")

profiles = store.get_all_profiles()
profile_ids = [p['profile_id'] for p in profiles]
profile_name_by_id = {p['profile_id']: p['profile_name'] for p in profiles}

# Defensive: if the previously-selected profile was deleted (or this is the
# first run), fall back to the first available profile BEFORE the selectbox
# widget is created — mutating a widget's session_state key after it's been
# instantiated raises a StreamlitAPIException, so this check must come first.
if 'selected_profile_id' not in st.session_state or st.session_state['selected_profile_id'] not in profile_ids:
    st.session_state['selected_profile_id'] = profile_ids[0]

selected_profile_id = st.sidebar.selectbox(
    "Active Profile", options=profile_ids, format_func=lambda pid: profile_name_by_id.get(pid, pid), key="selected_profile_id"
)
active_profile = store.get_profile(selected_profile_id)

with st.sidebar.expander("✏️ Edit / Create Profile"):
    is_new = st.checkbox("Create as a new profile instead of editing this one", key="profile_new_toggle")
    defaults = {
        "profile_id": None, "profile_name": "", "candidate_name": "", "candidate_email": "",
        "resume_text": "", "known_skills": [], "include_keywords": core.DEFAULT_INCLUDE_KEYWORDS,
        "exclude_keywords": core.DEFAULT_EXCLUDE_KEYWORDS, "exclude_substrings": core.DEFAULT_EXCLUDE_SUBSTRINGS,
        "default_keywords": core.DEFAULT_SEARCH_KEYWORDS, "default_locations": core.DEFAULT_SEARCH_LOCATIONS,
    } if is_new else active_profile
    form_profile_id = None if is_new else selected_profile_id
    form_key = form_profile_id or "new"

    edit_profile_name = st.text_input("Profile Name", value=defaults['profile_name'], key=f"edit_name_{form_key}")
    edit_candidate_name = st.text_input("Candidate Name", value=defaults['candidate_name'], key=f"edit_cname_{form_key}")
    edit_candidate_email = st.text_input("Candidate Email", value=defaults['candidate_email'], key=f"edit_cemail_{form_key}")
    edit_resume_text = st.text_area("Resume Text (fed into every AI prompt)", value=defaults['resume_text'], height=180, key=f"edit_resume_{form_key}")
    edit_known_skills = st.text_input("Known Skills (comma separated)", value=", ".join(defaults['known_skills']), key=f"edit_skills_{form_key}")
    edit_include_kw = st.text_input("Title INCLUDE keywords (comma separated)", value=", ".join(defaults['include_keywords']), key=f"edit_inc_{form_key}", help="A listing's title must contain at least one of these (flexibly matched — spaces/hyphens are interchangeable) to land in the strict-match list instead of borderline.")
    edit_exclude_kw = st.text_input("Title EXCLUDE keywords (comma separated)", value=", ".join(defaults['exclude_keywords']), key=f"edit_exc_{form_key}", help="Whole-word matched, e.g. 'Java' won't false-positive on 'JavaScript'.")
    edit_exclude_sub = st.text_input("Title EXCLUDE substrings (comma separated)", value=", ".join(defaults['exclude_substrings']), key=f"edit_excsub_{form_key}", help="Raw substring matching for terms regex word-boundaries don't handle well, e.g. C#, .NET")
    edit_default_kw = st.text_input("Default Search Keywords", value=defaults['default_keywords'], key=f"edit_defkw_{form_key}")
    edit_default_loc = st.text_input("Default Search Locations", value=defaults['default_locations'], key=f"edit_defloc_{form_key}")

    if st.button("💾 Save Profile", key=f"save_profile_btn_{form_key}"):
        store.save_profile(
            profile_id=form_profile_id,
            profile_name=edit_profile_name.strip() or "Unnamed",
            candidate_name=edit_candidate_name.strip(),
            candidate_email=edit_candidate_email.strip(),
            resume_text=edit_resume_text,
            known_skills=[s.strip() for s in edit_known_skills.split(',') if s.strip()],
            include_keywords=[s.strip() for s in edit_include_kw.split(',') if s.strip()],
            exclude_keywords=[s.strip() for s in edit_exclude_kw.split(',') if s.strip()],
            exclude_substrings=[s.strip() for s in edit_exclude_sub.split(',') if s.strip()],
            default_keywords=edit_default_kw.strip(),
            default_locations=edit_default_loc.strip(),
        )
        st.success(f"Profile '{edit_profile_name}' saved. Select it from the Active Profile dropdown above.")
        st.rerun()

    if not is_new and len(profiles) > 1:
        st.markdown("---")
        confirm_delete = st.checkbox("Confirm permanent delete of this profile (its analysis + pipeline status history is deleted too)", key=f"confirm_del_{form_key}")
        if st.button("🗑️ Delete This Profile", disabled=not confirm_delete, key=f"del_btn_{form_key}"):
            store.delete_profile(form_profile_id)
            st.success("Profile deleted.")
            st.rerun()

include_regex = core.build_include_regex(active_profile['include_keywords'])
exclude_words_regex = core.build_exclude_words_regex(active_profile['exclude_keywords'])
exclude_substrings = active_profile['exclude_substrings']

# ==========================================
# SIDEBAR: AI + SEARCH CONFIGURATION
# ==========================================
st.sidebar.markdown("---")
api_key = st.sidebar.text_input("1. Provide Gemini API Key", type="password", help="Get a free key from aistudio.google.com")
gemini_model_name = st.sidebar.text_input("2. Gemini Model Name", value="gemini-2.0-flash", help="If you hit a 404 'model not found' or 429 quota=0 error, try gemini-1.5-flash or gemini-2.5-flash instead — quota allocation differs per model on the free tier.")

st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Matrix Search Scope")
keywords_raw = st.sidebar.text_input("Target Keywords (comma separated)", value=active_profile['default_keywords'], key=f"keywords_raw_{selected_profile_id}")
locations_raw = st.sidebar.text_input("Target Locations (comma separated)", value=active_profile['default_locations'], key=f"locations_raw_{selected_profile_id}")
st.sidebar.caption("Each keyword is scanned against each location individually (true matrix search) — boolean 'OR' strings inside a single location field are NOT supported by job portals and will silently return garbage results.")

platform_options = ["linkedin", "indeed", "google", "glassdoor", "zip_recruiter", "naukri"]
selected_platforms = st.sidebar.multiselect("Target Platforms", options=platform_options, default=["linkedin", "indeed", "google"])
st.sidebar.caption("⚠️ glassdoor, zip_recruiter, and naukri frequently return 0 results for India searches due to CAPTCHA/anti-bot blocks (406/403) unless you run with rotating proxies. linkedin, indeed, and google are the most reliable.")

results_wanted = st.sidebar.slider("Results per Pass", min_value=2, max_value=20, value=8)
hours_old = st.sidebar.slider("Max Posting Age (hours)", min_value=24, max_value=2160, value=720, step=24)

st.sidebar.markdown("---")
st.sidebar.subheader("🤖 Bulk AI Analysis")
auto_analyze = st.sidebar.checkbox("Auto-analyze new strict matches after scan", value=True, help="Runs the AI fit/experience check on every new strict-match job automatically and caches it, instead of clicking 'Run AI Fit Analysis' per job.")
analyze_delay = st.sidebar.slider("Seconds between AI calls", min_value=1, max_value=10, value=4, help="Throttles Gemini calls to stay under free-tier rate limits.")

# Initialize Gemini AI Configuration
model = None
if api_key:
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(gemini_model_name)
    except Exception as e:
        st.sidebar.error(f"Initialization error: {e}")

# ==========================================
# SHARED JOB CARD RENDERER
# ==========================================
def render_job_card(row, key_prefix):
    """Renders one job's expander card (apply link, AI analysis, email/WhatsApp
    drafting). Shared by the strict-match list and the borderline-match list."""
    job_title = row.get('title') if not pd.isna(row.get('title')) else 'Software Engineer'
    company_name = row.get('company') if not pd.isna(row.get('company')) else 'Enterprise Org'
    portal_origin = row.get('site') if not pd.isna(row.get('site')) else 'direct'
    apply_url = row.get('job_url') if not pd.isna(row.get('job_url')) else '#'
    job_url = row.get('job_url')

    job_description = row.get('description')
    if job_description is None or pd.isna(job_description):
        job_description = ""

    hr_emails = core.extract_hr_emails(row)
    hr_phones = core.extract_hr_phones(row)

    with st.expander(f"💼 {job_title} — **{company_name}** `[{str(portal_origin).upper()}]`"):
        left_col, right_col = st.columns([1, 2])

        with left_col:
            st.link_button("🚀 Open One-Click Quick Apply Link", apply_url, type="primary", use_container_width=True)
            st.caption("Opens in your default browser, already logged into LinkedIn/Naukri — click Apply there manually to avoid CAPTCHA bans.")

            also_on = row.get('_also_on')
            if also_on and not pd.isna(also_on) and str(also_on).strip():
                st.caption(f"📌 Also cross-listed on: {also_on}")

            if hr_emails:
                st.error(f"📧 Found Direct HR Email Contact: `{hr_emails[0]}`")
            if hr_phones:
                st.info(f"📱 Found Phone/WhatsApp Contact: `{hr_phones[0]}`")
            if not hr_emails and not hr_phones:
                st.write("ℹ️ Submission Pathway: Standard web application pipeline.")

            cached_result = store.get_analysis(job_url, selected_profile_id) if job_url else None
            run_clicked = st.button("📊 Run AI Fit & Experience Analysis" if not cached_result else "🔄 Re-run AI Analysis", key=f"analyze_btn_{key_prefix}")
            if run_clicked:
                with st.spinner("Processing keyword density, experience validation & email drafting..."):
                    analysis_output = core.process_skill_matching_engine(
                        model, job_title, company_name, job_description, hr_emails,
                        active_profile['resume_text'], active_profile['known_skills'], active_profile['candidate_name'],
                    )
                    if job_url and not analysis_output.get('_analysis_failed'):
                        store.save_analysis(job_url, selected_profile_id, analysis_output)
                    st.session_state[f"match_result_{key_prefix}"] = analysis_output
            elif cached_result and f"match_result_{key_prefix}" not in st.session_state:
                st.session_state[f"match_result_{key_prefix}"] = cached_result
                st.caption("✅ Using cached analysis from a previous scan.")

        with right_col:
            if f"match_result_{key_prefix}" in st.session_state:
                res = st.session_state[f"match_result_{key_prefix}"]

                if res.get('_analysis_failed'):
                    st.error(f"⚠️ AI analysis failed: {res.get('reason_for_changes', 'unknown error')}")
                elif not res.get('should_apply', True):
                    st.warning(f"⚠️ AI Recommendation: SKIP THIS JOB. Required experience ({res.get('estimated_experience_required', 'N/A')}) doesn't match your profile.")
                else:
                    st.metric(label="ATS Skill Score Match Rank", value=f"{res.get('match_score', 0)}%")
                    st.write(f"⏱️ **Detected Target Experience:** {res.get('estimated_experience_required', '')}")
                    st.write("**✅ Your skills found in description:** " + ", ".join(res.get('matching_skills', [])))
                    if res.get('missing_skills'):
                        st.write("**⚠️ Unmatched tech mentioned:** " + ", ".join(res['missing_skills']))

                    st.markdown("---")
                    st.subheader("📝 Live Modification Preview Monitor")
                    st.markdown(f"**AI Optimization Reason:** *{res.get('reason_for_changes', '')}*")

                    st.text_area("Proposed Dynamic Section Output (Edit here if needed)", res.get('modified_summary', ''), height=95, key=f"mod_txt_{key_prefix}")

                    if st.button("🚀 Confirm and Copy Optimized Text Block", key=f"confirm_btn_{key_prefix}"):
                        st.success("Text saved! Copy this summary block to tailor your application.")

                    # Automated HR email & mailto launcher
                    if hr_emails and (res.get('email_subject') or res.get('email_body')):
                        st.markdown("---")
                        st.subheader("✉️ Auto-Drafted HR Outreach Email")
                        edited_subject = st.text_input("Subject", res.get('email_subject', ''), key=f"email_subj_{key_prefix}")
                        edited_body = st.text_area("Body", res.get('email_body', ''), height=150, key=f"email_body_{key_prefix}")
                        mailto_href = core.build_mailto_link(hr_emails[0], edited_subject, edited_body)
                        st.link_button(f"✉️ Launch Pre-Filled Email to {hr_emails[0]}", mailto_href, use_container_width=True)
                        st.caption("Opens your default mail client (Outlook/Gmail desktop) with recipient, subject and body pre-filled — attach your resume and send.")

                    if hr_phones:
                        st.markdown("---")
                        st.subheader("💬 WhatsApp the Recruiter")
                        wa_message = st.text_area(
                            "WhatsApp message",
                            f"Hi, I'm {active_profile['candidate_name']}. Saw your opening for {job_title} at {company_name} — sharing my resume, happy to connect!",
                            height=80, key=f"wa_msg_{key_prefix}",
                        )
                        wa_href = core.build_whatsapp_link(hr_phones[0], wa_message)
                        st.link_button(f"💬 Open WhatsApp Chat with {hr_phones[0]}", wa_href, use_container_width=True)


def run_bulk_analysis(df, delay_seconds):
    """Auto-analyzes every job in df that isn't already cached in SQLite for
    the active profile, persisting results as it goes so progress survives
    an interruption. Failed calls (e.g. quota exhausted) are never cached —
    they stay 'unanalyzed' so the next scan retries them automatically."""
    if df is None or df.empty or model is None:
        return
    job_urls = df['job_url'].dropna().tolist()
    unanalyzed = store.get_unanalyzed_job_urls(job_urls, selected_profile_id)
    if not unanalyzed:
        return
    bulk_progress = st.progress(0, text=f"Running AI analysis on {len(unanalyzed)} new matches...")
    for i, job_url in enumerate(unanalyzed):
        row = df[df['job_url'] == job_url].iloc[0]
        hr_emails = core.extract_hr_emails(row)
        result = core.process_skill_matching_engine(
            model, row.get('title'), row.get('company'), row.get('description'), hr_emails,
            active_profile['resume_text'], active_profile['known_skills'], active_profile['candidate_name'],
        )
        if not result.get('_analysis_failed'):
            store.save_analysis(job_url, selected_profile_id, result)
        bulk_progress.progress((i + 1) / len(unanalyzed), text=f"Analyzed {i + 1}/{len(unanalyzed)}: {row.get('title')}")
        if i < len(unanalyzed) - 1:
            time.sleep(delay_seconds)
    bulk_progress.empty()


# ==========================================
# INTERACTIVE GRAPHICAL FRONTEND
# ==========================================
st.title("🎯 Local AI Job Agent Dashboard")
st.caption(f"Profile: **{active_profile['profile_name']}** · Matrix-scrapes portals per keyword x location, enforces your experience filters, drafts HR emails, and gives you safe one-click apply links.")

if not api_key:
    st.info("💡 Getting Started: Paste your free Gemini API Key into the left sidebar input field to start running queries.")
    st.stop()

tab1, tab2, tab3 = st.tabs(["🔍 Job Matrix Scan", "🤝 Recruiter Network & Outreach", "📋 My Pipeline"])

with tab1:
    if st.button("🔍 Run Portal Search (Matrix Scan)"):
        keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()]
        locations = [loc.strip() for loc in locations_raw.split(',') if loc.strip()]

        if not keywords or not locations:
            st.warning("Please provide at least one keyword and one location.")
        elif not selected_platforms:
            st.warning("Please select at least one target platform.")
        else:
            progress_bar = st.progress(0, text="Starting matrix scan...")

            def progress_cb(pass_num, total_passes, message):
                progress_bar.progress(pass_num / total_passes, text=f"📡 {message}")

            strict_data, borderline_data, run_log = core.fetch_aggregated_listings(
                keywords, locations, selected_platforms, results_wanted, hours_old,
                include_regex, exclude_words_regex, exclude_substrings, progress_callback=progress_cb,
            )
            progress_bar.empty()

            store.upsert_jobs(strict_data, match_tier="strict")
            store.upsert_jobs(borderline_data, match_tier="borderline")

            st.session_state['run_log'] = run_log
            st.session_state['active_listings_df'] = strict_data
            st.session_state['borderline_listings_df'] = borderline_data

            if auto_analyze and model is not None and strict_data is not None and not strict_data.empty:
                run_bulk_analysis(strict_data, analyze_delay)

            if strict_data is not None and not strict_data.empty:
                st.success(f"Successfully loaded {len(strict_data)} strictly verified matching records" + (f", plus {len(borderline_data)} borderline ones below." if borderline_data is not None and not borderline_data.empty else "."))
            elif borderline_data is not None and not borderline_data.empty:
                st.warning(f"No strict title matches, but {len(borderline_data)} borderline listings survived the stack/level exclusion filter — check the section below before giving up on this search.")
            else:
                st.warning("No active matching listings found at all. Check the scan diagnostics below for per-pass details.")

    if 'run_log' in st.session_state:
        with st.expander(f"🔬 Scan Diagnostics ({len(st.session_state['run_log'])} passes)"):
            for entry in st.session_state['run_log']:
                if entry['status'] == 'ok':
                    st.write(f"✅ `{entry['keyword']}` in `{entry['location']}` — {entry['count']} raw hits")
                else:
                    st.write(f"❌ `{entry['keyword']}` in `{entry['location']}` — {entry['error']}")

    if 'active_listings_df' in st.session_state:
        df = st.session_state['active_listings_df']
        for idx, row in df.iterrows():
            render_job_card(row, key_prefix=f"strict_{idx}")

    if 'borderline_listings_df' in st.session_state and not st.session_state['borderline_listings_df'].empty:
        bdf = st.session_state['borderline_listings_df']
        st.markdown("---")
        with st.expander(f"📂 Borderline Matches — title didn't clearly match your stack, worth a quick look ({len(bdf)})", expanded=False):
            st.caption("These passed your exclusion filter but their title doesn't contain one of your INCLUDE keywords. Could be real opportunities with a generic title (e.g. \"Software Engineer\"), or noise — run the AI analysis to check.")
            for idx, row in bdf.iterrows():
                render_job_card(row, key_prefix=f"borderline_{idx}")

with tab2:
    st.subheader("🤝 Free IT Staffing Agencies & Recruiter Platforms")
    st.warning("🚩 Legitimate recruiters and staffing agencies are paid by the **hiring company**, never by you. If anyone asks you to pay a 'registration fee', 'training fee', or 'placement fee', it's a scam — don't pay, and walk away.")
    st.caption("Click 'Find Official Site' to land on a fresh Google search for each agency's current site rather than relying on a possibly outdated hardcoded link.")

    for agency in core.RECRUITER_DIRECTORY:
        with st.container(border=True):
            cols = st.columns([3, 1, 1])
            with cols[0]:
                st.markdown(f"**{agency['name']}** · `{agency['category']}`")
                st.caption(agency['focus'])
            with cols[1]:
                st.link_button("🔎 Find Official Site", core.build_search_url(f"{agency['name']} official site India IT staffing register resume"), use_container_width=True)
            with cols[2]:
                st.link_button("🔗 Find on LinkedIn", core.build_linkedin_company_search_url(agency['name']), use_container_width=True)

    st.markdown("---")
    st.subheader("📋 How to Actually Connect With Them")
    st.markdown("""
1. Use the buttons above to find each agency's official careers/registration page or LinkedIn company page.
2. Upload your resume to their portal, or apply to any open requisition that overlaps your stack — this gets you into their internal candidate database even if that exact req isn't a fit.
3. Search their tech recruiters on LinkedIn (use the people-search below) and send a short, personalized connection note.
4. Reply fast when they reach out. Agencies already have warm relationships with hiring managers, so they often move faster than a cold direct application.
5. Keep your LinkedIn "Open to Work" set to recruiters-only and your profile keyword-matched to your target stack so you surface in their searches passively.
""")

    st.markdown("---")
    st.subheader("🔎 Find Recruiters on LinkedIn (Your Search Matrix)")
    search_keywords = [k.strip() for k in keywords_raw.split(',') if k.strip()] or ["Angular", "Node.js"]
    search_locations = [loc.strip() for loc in locations_raw.split(',') if loc.strip()] or ["Pune"]
    for loc in search_locations:
        loc_cols = st.columns(len(search_keywords))
        for col, kw in zip(loc_cols, search_keywords):
            query = f"{kw} recruiter {loc}"
            col.link_button(f"🔗 {query}", core.build_linkedin_people_search_url(query), use_container_width=True)

    st.markdown("---")
    st.subheader("📰 Browse LinkedIn 'Hiring' Posts")
    st.caption("LinkedIn's post search has no API — these open LinkedIn's own content search in your browser so you stay logged-in and ToS-compliant. Once it opens, use the **'Date posted'** filter near the top of the results (next to 'Sort by') and pick **Past 24 hours**, **Past week**, or **Past month** — there's no exact 14-day option, so 'Past week' is the closest fit for a 14-day-ish window.")
    for loc in search_locations:
        loc_cols = st.columns(len(search_keywords))
        for col, kw in zip(loc_cols, search_keywords):
            post_query = f"{kw} hiring {loc}"
            col.link_button(f"📰 {post_query}", core.build_linkedin_post_search_url(post_query), use_container_width=True)

    st.markdown("---")
    st.subheader("✨ Generic Recruiter Outreach Pitch")
    st.caption("Not tied to one job — use this to message any staffing agency consultant or recruiter cold, to get added to their active candidate pipeline.")
    if st.button("✨ Generate My Outreach Pitch"):
        with st.spinner("Drafting LinkedIn note and pipeline email..."):
            st.session_state['generic_outreach'] = core.generate_generic_outreach(
                model, active_profile['resume_text'], active_profile['known_skills'],
                active_profile['candidate_name'], active_profile['default_locations'],
            )

    if 'generic_outreach' in st.session_state:
        outreach = st.session_state['generic_outreach']
        if outreach.get('error'):
            st.error(f"Generation failed: {outreach['error']}")
        else:
            st.text_area("LinkedIn Connection Note (max 300 chars)", outreach.get('linkedin_note', ''), height=80, key="outreach_linkedin_note")
            edited_gen_subject = st.text_input("General Pipeline Email — Subject", outreach.get('general_email_subject', ''), key="outreach_email_subject")
            edited_gen_body = st.text_area("General Pipeline Email — Body", outreach.get('general_email_body', ''), height=180, key="outreach_email_body")
            generic_mailto = core.build_mailto_link("", edited_gen_subject, edited_gen_body)
            st.link_button("✉️ Open Blank Email With This Pitch (fill in recipient)", generic_mailto, use_container_width=True)
            st.caption("Opens your mail client with subject/body pre-filled — paste in any recruiter's email address you found above and send.")

with tab3:
    st.subheader("📋 My Application Pipeline")
    st.caption(f"Profile: **{active_profile['profile_name']}** — every job ever scanned (strict + borderline, across all past runs) lives here permanently, scoped to this profile.")

    status_options = ["New", "Applied", "Interviewing", "Rejected", "Offer", "Skipped"]
    filter_col1, filter_col2 = st.columns(2)
    status_filter = filter_col1.selectbox("Filter by status", ["All"] + status_options)
    min_score_filter = filter_col2.slider("Minimum match score (0 = include unanalyzed)", 0, 100, 0)

    pipeline_df = store.get_all_jobs_df(
        selected_profile_id,
        status_filter=None if status_filter == "All" else status_filter,
        min_score=min_score_filter if min_score_filter > 0 else None,
    )
    st.caption(f"{len(pipeline_df)} job(s) match this filter.")

    for _, row in pipeline_df.iterrows():
        with st.container(border=True):
            info_col, status_col, followup_col, save_col = st.columns([3, 1, 1, 1])
            with info_col:
                st.markdown(f"**{row['title']}** — {row['company']} · `{row['location']}`")
                score_caption = f"Match score: {int(row['match_score'])}%" if pd.notna(row['match_score']) else "Not yet analyzed"
                st.caption(f"{score_caption} · {row.get('estimated_experience_required') or ''} · first seen {str(row['first_seen_at'])[:10]}")
                st.link_button("Open Listing", row['apply_url'], use_container_width=False)
            with status_col:
                current_status = row['status'] if row['status'] in status_options else "New"
                new_status = st.selectbox("Status", status_options, index=status_options.index(current_status), key=f"pl_status_{row['job_url']}")
            with followup_col:
                existing_date = pd.to_datetime(row['follow_up_date']).date() if row['follow_up_date'] else None
                new_follow_up = st.date_input("Follow-up", value=existing_date, key=f"pl_followup_{row['job_url']}")
            with save_col:
                st.write("")
                if st.button("💾 Save", key=f"pl_save_{row['job_url']}"):
                    store.upsert_status(
                        row['job_url'], selected_profile_id,
                        status=new_status,
                        follow_up_date=str(new_follow_up) if new_follow_up else None,
                    )
                    st.success("Saved")
