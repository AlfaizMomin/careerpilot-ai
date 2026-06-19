"""Shared backend logic for the job agent: scraping, filtering, dedup, LLM
analysis, and recruiter-outreach helpers. No Streamlit imports here — this
module is used both by app.py (interactive UI) and scheduled_scan.py
(headless cron job), so anything UI-specific (st.progress, st.session_state)
stays out and is handled via plain callbacks/return values instead.

Profile data (resume text, known skills, title-filter keywords) used to be
hardcoded module constants. They're now passed in as parameters everywhere,
sourced from job_store's `profiles` table — DEFAULT_* below only exist to
seed the first ('default') profile on first run, so out-of-the-box behavior
is unchanged.
"""
import json
import re
import urllib.parse

import pandas as pd
from jobspy import scrape_jobs

# ==========================================
# DEFAULT PROFILE SEED (used once, to create the 'default' profile in SQLite)
# ==========================================
DEFAULT_RESUME_TEXT = """
ALFEJ ASLAM MOMIN
Full Stack Engineer | Technical Lead | AI-First Engineering
Pune, India | alfaiz.momin1998@gmail.com

SUMMARY:
Full Stack Engineer with 4.7+ years of experience building enterprise SaaS platforms at BrowserStack.
Sole technical owner of a production code quality platform responsible for all architectural decisions,
database design, end-to-end feature delivery, and sprint facilitation. Background in Angular, Node.js,
TypeScript, and PostgreSQL. Treat AI tooling as a core part of daily engineering workflow.

TECHNICAL SKILLS:
- AI & Tooling: GitHub Copilot, Gemini, Ollama (Local LLMs), Prompt Engineering
- Frontend: Angular, RxJS, NgRx, Ionic, TypeScript, JavaScript (ES6+), Micro Frontends, Responsive Design
- Backend: Node.js, Express.js, REST APIs, Microservices, WebSockets, JWT Authentication, RBAC, Multi-tenant Architecture, Event-driven Systems
- Database: PostgreSQL, MongoDB schema design, query optimisation, multi-tenant isolation
- DevOps: Docker, Jenkins, GitHub Actions, CI/CD Pipelines, Linux, Shell Scripting
"""

DEFAULT_KNOWN_SKILLS = [
    "Angular", "Node.js", "TypeScript", "JavaScript", "PostgreSQL", "Express.js",
    "RxJS", "NgRx", "Ionic", "Docker", "Jenkins", "Micro Frontends",
    "Microservices", "REST APIs", "GitHub Actions", "MongoDB", "MEAN Stack"
]

DEFAULT_CANDIDATE_NAME = "Alfej Aslam Momin"
DEFAULT_CANDIDATE_EMAIL = "alfaiz.momin1998@gmail.com"

DEFAULT_INCLUDE_KEYWORDS = [
    "Angular", "Node", "JavaScript", "TypeScript", "Full Stack", "MEAN Stack", "MEAN",
    "UI Engineer", "UI Developer", "Front End", "Back End", "Web Developer", "Software Developer",
]
DEFAULT_EXCLUDE_KEYWORDS = ["Intern", "Internship", "Interns", "Fresher", "Freshers", "Java", "Python"]
DEFAULT_EXCLUDE_SUBSTRINGS = ["c#", ".net"]

DEFAULT_SEARCH_KEYWORDS = "Angular, Node.js, MEAN Stack"
DEFAULT_SEARCH_LOCATIONS = "Pune, Mumbai, Remote"


def default_profile_seed():
    return {
        "profile_name": "Default",
        "candidate_name": DEFAULT_CANDIDATE_NAME,
        "candidate_email": DEFAULT_CANDIDATE_EMAIL,
        "resume_text": DEFAULT_RESUME_TEXT,
        "known_skills": DEFAULT_KNOWN_SKILLS,
        "include_keywords": DEFAULT_INCLUDE_KEYWORDS,
        "exclude_keywords": DEFAULT_EXCLUDE_KEYWORDS,
        "exclude_substrings": DEFAULT_EXCLUDE_SUBSTRINGS,
        "default_keywords": DEFAULT_SEARCH_KEYWORDS,
        "default_locations": DEFAULT_SEARCH_LOCATIONS,
    }


# ==========================================
# CONFIGURABLE TITLE FILTER BUILDERS
# ==========================================
def _term_to_pattern(term):
    """re.escape a term but keep internal whitespace flexible (matches space,
    hyphen, or nothing) so user-entered phrases like 'Front End' also match
    'Frontend' / 'Front-End' without the user having to know regex."""
    escaped = re.escape(term.strip())
    return escaped.replace(r'\ ', r'[\s-]?').replace(' ', r'[\s-]?')


def build_include_regex(keywords):
    terms = [k for k in keywords if k and k.strip()]
    if not terms:
        return re.compile(r'(?!x)x')  # matches nothing
    pattern = '|'.join(_term_to_pattern(k) for k in terms)
    return re.compile(f'({pattern})', re.IGNORECASE)


def build_exclude_words_regex(keywords):
    terms = [k for k in keywords if k and k.strip()]
    if not terms:
        return re.compile(r'(?!x)x')  # matches nothing
    pattern = '|'.join(r'\b' + _term_to_pattern(k) + r'\b' for k in terms)
    return re.compile(f'({pattern})', re.IGNORECASE)


EMAIL_REGEX = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
PHONE_REGEX = re.compile(r'(?:\+?91[\s-]?)?[6-9]\d{9}\b')

# Safety net for the "Remote" pass: LinkedIn's location matching is loose enough that
# is_remote=True still leaks remote jobs based in other countries. Blank locations are
# kept (legitimate India-remote LinkedIn posts often omit a location), but an explicit
# non-India country/city is rejected.
NON_INDIA_LOCATION_REGEX = re.compile(
    r'\b(australia|sydney|melbourne|italy|milan|spain|madrid|usa|united states|uk|'
    r'united kingdom|london|germany|berlin|canada|toronto|france|paris|netherlands|'
    r'poland|brazil|mexico|philippines|pakistan|bangladesh|nigeria|egypt|turkey|'
    r'ukraine|russia|singapore|malaysia|indonesia|vietnam|china|japan|south korea|'
    r'ireland|portugal|sweden|norway|denmark|switzerland|austria|belgium|dubai|uae)\b',
    re.IGNORECASE,
)


def is_outside_india(location):
    if pd.isna(location) or not str(location).strip():
        return False
    loc = str(location).lower()
    if 'india' in loc:
        return False
    return bool(NON_INDIA_LOCATION_REGEX.search(loc))


def is_excluded_title(title, exclude_words_regex, exclude_substrings):
    if pd.isna(title):
        return False
    t = str(title).lower()
    if any(sub.lower() in t for sub in exclude_substrings):
        return True
    return bool(exclude_words_regex.search(t))


def is_valid_title(title, include_regex):
    if pd.isna(title):
        return False
    return bool(include_regex.search(str(title).lower()))


# ==========================================
# DATA PROCESSING PIPELINE BACKEND
# ==========================================
def fetch_aggregated_listings(keywords, locations, platforms, results_wanted, hours_old,
                               include_regex, exclude_words_regex, exclude_substrings,
                               progress_callback=None):
    """Scrapes jobs per individual (keyword, location) pair — a true search matrix —
    to avoid boolean 'OR' strings being misread as literal location text by portals.
    progress_callback(pass_num, total_passes, message) is called before each pass if provided."""
    combined_df = pd.DataFrame()
    run_log = []
    total_passes = max(len(keywords) * len(locations), 1)
    pass_num = 0

    for loc in locations:
        # "Remote" is not a real geography — passing it as a literal location lets
        # LinkedIn/Indeed match remote jobs from anywhere in the world. Searching
        # location="India" with is_remote=True instead keeps results India-tied.
        is_remote_pass = loc.strip().lower() == "remote"
        effective_location = "India" if is_remote_pass else loc

        for kw in keywords:
            pass_num += 1
            if progress_callback:
                progress_callback(pass_num, total_passes, f"Scanning '{kw}' in '{loc}'...")
            try:
                jobs_df = scrape_jobs(
                    site_name=platforms,
                    search_term=kw,
                    location=effective_location,
                    is_remote=is_remote_pass,
                    results_wanted=results_wanted,
                    hours_old=hours_old,
                    country_indeed="india",
                )
                count = 0 if jobs_df is None else len(jobs_df)
                run_log.append({"keyword": kw, "location": loc, "status": "ok", "count": count})
                if jobs_df is not None and not jobs_df.empty:
                    combined_df = pd.concat([combined_df, jobs_df], ignore_index=True)
            except Exception as e:
                run_log.append({"keyword": kw, "location": loc, "status": "error", "count": 0, "error": str(e)})

    if combined_df.empty:
        return combined_df, combined_df, run_log

    # Drop exact duplicates across separate keyword/location passes
    combined_df.drop_duplicates(subset=['job_url'], inplace=True)

    # Strip remote jobs that leaked through tied to a non-India country
    combined_df = combined_df[~combined_df['location'].apply(is_outside_india)]

    # Hard exclusion gate: kill mismatched stacks/levels immediately — these are
    # never worth showing, so they're the only rows actually discarded.
    combined_df = combined_df[~combined_df['title'].apply(lambda t: is_excluded_title(t, exclude_words_regex, exclude_substrings))]

    combined_df = consolidate_cross_platform_duplicates(combined_df)
    combined_df.reset_index(drop=True, inplace=True)

    # Title regex is a recall aid, not a hard filter — a title regex can never cover
    # every real-world phrasing, so anything that doesn't clearly match your stack
    # is kept as "borderline" instead of being silently thrown away.
    is_strict_match = combined_df['title'].apply(lambda t: is_valid_title(t, include_regex))
    strict_df = combined_df[is_strict_match].reset_index(drop=True)
    borderline_df = combined_df[~is_strict_match].reset_index(drop=True)
    return strict_df, borderline_df, run_log


# Same opening often gets scraped separately from linkedin/indeed/google with different
# job_url values, so the exact-URL dedup above misses them. This groups by normalized
# (title, company) and prefers the LinkedIn row as the canonical listing/apply link,
# since you're already authenticated there for one-click apply.
SITE_PRIORITY = {"linkedin": 0, "indeed": 1, "google": 2, "naukri": 3, "glassdoor": 4, "zip_recruiter": 5}


def consolidate_cross_platform_duplicates(df):
    if df.empty:
        return df

    def normalize(s):
        return re.sub(r'[^a-z0-9]+', ' ', str(s).lower()).strip()

    df = df.copy()
    df['_dedup_key'] = df['title'].apply(normalize) + '|' + df['company'].apply(normalize)
    df['_site_rank'] = df['site'].map(SITE_PRIORITY).fillna(99)

    consolidated_rows = []
    for _, group in df.groupby('_dedup_key', sort=False):
        group_sorted = group.sort_values('_site_rank')
        best_row = group_sorted.iloc[0].copy()

        # Keep the richest description across the duplicate group
        descs = group['description'].dropna().astype(str)
        if not descs.empty:
            best_row['description'] = descs.loc[descs.str.len().idxmax()]

        # Merge recruiter emails found across all duplicate listings
        all_emails = []
        for e in group['emails']:
            if isinstance(e, list):
                all_emails.extend(e)
        if all_emails:
            best_row['emails'] = list(dict.fromkeys(all_emails))

        other_sites = sorted(set(group['site'].dropna()) - {best_row['site']})
        best_row['_also_on'] = ', '.join(other_sites)

        consolidated_rows.append(best_row)

    result = pd.DataFrame(consolidated_rows).drop(columns=['_dedup_key', '_site_rank'])
    return result


def extract_hr_emails(row):
    """Pulls recruiter emails from jobspy's parsed 'emails' field plus a regex
    sweep of the raw description, deduped and stripped of obvious noreply addresses."""
    emails = []

    raw_field = row.get('emails')
    if isinstance(raw_field, list):
        emails.extend(raw_field)

    description = row.get('description')
    if not pd.isna(description) and isinstance(description, str) and description.strip():
        emails.extend(EMAIL_REGEX.findall(description))

    cleaned, seen = [], set()
    for e in emails:
        if not e or not isinstance(e, str):
            continue
        e_norm = e.strip().lower()
        if 'noreply' in e_norm or 'no-reply' in e_norm:
            continue
        if e_norm not in seen:
            seen.add(e_norm)
            cleaned.append(e.strip())
    return cleaned


def extract_hr_phones(row):
    """Indian recruiters/agencies frequently share a WhatsApp/mobile number in the
    JD instead of (or alongside) an email — this catches those for a wa.me link."""
    description = row.get('description')
    if pd.isna(description) or not isinstance(description, str) or not description.strip():
        return []
    raw_matches = PHONE_REGEX.findall(description)
    cleaned, seen = [], set()
    for m in raw_matches:
        digits = re.sub(r'\D', '', m)
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        if len(digits) != 10:
            continue
        if digits not in seen:
            seen.add(digits)
            cleaned.append(digits)
    return cleaned


def build_mailto_link(email, subject, body):
    params = urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    return f"mailto:{email}?{params}"


def build_whatsapp_link(phone_10digit, message):
    return f"https://wa.me/91{phone_10digit}?{urllib.parse.urlencode({'text': message}, quote_via=urllib.parse.quote)}"


def _parse_llm_json(raw_text):
    cleaned_json_text = re.sub(r'```json|```', '', (raw_text or "")).strip()
    match = re.search(r'\{.*\}', cleaned_json_text, re.DOTALL)
    if match:
        cleaned_json_text = match.group(0)
    return json.loads(cleaned_json_text)


def process_skill_matching_engine(model, title, company, description, hr_emails,
                                   resume_text, known_skills, candidate_name):
    """Uses LLM to validate the 3-6 year experience filter, true tech skills,
    a tailored resume summary, and (if a recruiter email exists) a cold-email draft.
    On any failure (including hitting an API quota limit), returns a result with
    "_analysis_failed": True so the caller knows NOT to cache this as a real result
    — otherwise a quota-exhausted run would permanently poison the cache with
    placeholder data that's never retried."""
    if description is None or pd.isna(description) or not str(description).strip():
        description = "No description details provided by the portal."
    description = str(description)[:6000]

    has_email = bool(hr_emails)
    if has_email:
        email_instruction = (
            f"An HR/recruiter contact email was found for this listing: {hr_emails[0]}. "
            "Draft a concise, hyper-targeted cold email (subject + body) pitching the candidate for this exact role. "
            "Reference the specific company and job title. Mention the candidate's strongest matching experience "
            "and 2-3 of the candidate's True Known Skills that match this JD. Keep the body under "
            f"150 words, professional, plain text (no markdown), and sign off as {candidate_name}."
        )
    else:
        email_instruction = "No direct HR email was found for this listing, so set email_subject and email_body to empty strings."

    prompt = f"""
    You are an advanced technical recruitment tracking filter evaluating this candidate.

    Job Title: {title} at {company}
    Job Description: {description}

    Candidate Master Resume Data: {resume_text}
    Candidate True Known Skills List: {known_skills}

    Instructions:
    1. EXPERIENCE FILTER: Analyze the JD for required years of experience against the candidate's actual experience
       (infer their total years from the resume data above). If the job demands meaningfully more seniority than the
       candidate has, OR if it is strictly a fresher/0-1 year job and the candidate is clearly more senior, set "should_apply" to false.
    2. SKILLS CHECK: Check if the candidate matches at least 75% of the core skills using ONLY their True Known Skills.
    3. SUMMARY GENERATION: If "should_apply" is true, generate an optimized Resume Summary paragraph highlighting their true matching background matching this specific JD. Never fabricate tools or companies.
    4. EMAIL DRAFTING: {email_instruction}

    Format your response strictly as a raw JSON string matching this exact structure, with no markdown codeblocks or trailing comments:
    {{
        "should_apply": true,
        "match_score": 85,
        "estimated_experience_required": "3-5 years",
        "matching_skills": ["Angular", "Node.js"],
        "missing_skills": ["AWS"],
        "reason_for_changes": "Matches the target experience range. Emphasized relevant ownership and matching architecture experience.",
        "modified_summary": "Tailored paragraph output here...",
        "email_subject": "Subject line here or empty string",
        "email_body": "Plain text email body here or empty string"
    }}
    """
    try:
        response = model.generate_content(prompt)
        result = _parse_llm_json(response.text)
        result.setdefault("email_subject", "")
        result.setdefault("email_body", "")
        result["_analysis_failed"] = False
        return result
    except Exception as e:
        return {
            "should_apply": True,
            "match_score": 50,
            "estimated_experience_required": "Unknown",
            "matching_skills": [],
            "missing_skills": [],
            "reason_for_changes": f"AI call failed (will retry next scan): {e}",
            "modified_summary": "",
            "email_subject": "",
            "email_body": "",
            "_analysis_failed": True,
        }


# ==========================================
# RECRUITER AGENCY DIRECTORY & OUTREACH ENGINE
# ==========================================
# Curated by category/specialization only — no guessed company URLs. Each entry gets
# a dynamically-built Google / LinkedIn search link so you always land on the agency's
# *current* official page instead of risking a stale or wrong hardcoded domain.
RECRUITER_DIRECTORY = [
    {"name": "TeamLease Digital", "category": "IT Staffing Agency", "focus": "Contract & permanent IT/software roles across India, including MEAN stack, Angular, Node.js."},
    {"name": "Quess Corp IT Staffing", "category": "IT Staffing Agency", "focus": "Large-scale enterprise IT staffing and contract-to-hire placements."},
    {"name": "Randstad India", "category": "IT Staffing Agency", "focus": "General + IT/tech staffing, mid-level software engineering roles."},
    {"name": "Experis India ManpowerGroup", "category": "IT Staffing Agency", "focus": "Specialist IT/tech talent arm of ManpowerGroup, mid-senior developer roles."},
    {"name": "Adecco India", "category": "IT Staffing Agency", "focus": "IT & engineering staffing, contract and permanent."},
    {"name": "CIEL HR Services", "category": "IT Staffing Agency", "focus": "Mid-level IT/tech recruitment across Indian tech hubs."},
    {"name": "Collabera India", "category": "IT Staffing Agency", "focus": "IT staffing/contract roles, strong presence in Pune/Mumbai/Bangalore."},
    {"name": "TEKsystems India", "category": "IT Staffing Agency", "focus": "IT contract & permanent staffing (Allegis Group)."},
    {"name": "Instahyre", "category": "Tech Recruiter Platform", "focus": "Recruiters & hiring managers message you directly based on your profile — no cold applying."},
    {"name": "Hirist", "category": "Tech Recruiter Platform", "focus": "India-focused tech jobs platform; recruiters reach out to matching candidates."},
    {"name": "Cutshort", "category": "Tech Recruiter Platform", "focus": "AI-matched tech talent platform; startups & companies message you directly."},
    {"name": "Wellfound (AngelList Talent)", "category": "Tech Recruiter Platform", "focus": "Startup jobs; founders/recruiters message you directly, strong for remote roles."},
    {"name": "Turing", "category": "Remote Tech Recruiter Platform", "focus": "Vets you once, then matches you to US/global remote roles — recruiters reach out."},
    {"name": "Toptal", "category": "Remote Tech Recruiter Platform", "focus": "Vetted network for remote full-time/freelance roles with global clients."},
]


def build_search_url(query):
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"


def build_linkedin_company_search_url(name):
    return f"https://www.linkedin.com/search/results/companies/?keywords={urllib.parse.quote(name)}"


def build_linkedin_post_search_url(query):
    """LinkedIn's content/post search vertical — surfaces 'we are hiring' posts
    that never make it into structured job boards. No documented URL parameter
    for an exact 14-day window, so this intentionally omits a guessed datePosted
    param; apply LinkedIn's own 'Past 24 hours'/'Past week'/'Past month' filter
    chip (next to 'Sort by' on the results page) after opening."""
    return f"https://www.linkedin.com/search/results/content/?keywords={urllib.parse.quote(query)}&origin=SWITCH_SEARCH_VERTICAL"


def build_linkedin_people_search_url(query):
    return f"https://www.linkedin.com/search/results/people/?keywords={urllib.parse.quote(query)}"


def generate_generic_outreach(model, resume_text, known_skills, candidate_name, default_locations):
    """Uses the LLM to draft a reusable LinkedIn connection note + a general
    pipeline-pitch cold email, not tied to any single job posting."""
    prompt = f"""
    You are a career coach helping a candidate maximize inbound recruiter interest.

    Candidate Master Resume Data: {resume_text}
    Candidate True Known Skills List: {known_skills}
    Candidate target locations: {default_locations}

    Generate:
    1. A LinkedIn connection request note (STRICT MAX 300 characters) to send to IT staffing recruiters/agency consultants, introducing the candidate and asking to be considered for openings matching their skills.
    2. A general cold email (subject + body, under 180 words, plain text, no markdown) the candidate can send to any IT recruitment agency consultant, pitching their background broadly (not tied to one specific job), asking to be added to their active candidate pipeline for roles in the candidate's target locations.

    Format your response strictly as raw JSON, no markdown codeblocks or trailing commentary:
    {{
        "linkedin_note": "...",
        "general_email_subject": "...",
        "general_email_body": "..."
    }}
    """
    try:
        response = model.generate_content(prompt)
        result = _parse_llm_json(response.text)
        result.setdefault("linkedin_note", "")
        result.setdefault("general_email_subject", "")
        result.setdefault("general_email_body", "")
        return result
    except Exception as e:
        return {
            "linkedin_note": "",
            "general_email_subject": "",
            "general_email_body": "",
            "error": str(e),
        }
