# AI Job Agent Workspace

A local, semi-automated job-hunting dashboard for Angular/Node.js/MEAN-stack roles in India. It scrapes job boards across a keyword × location matrix, filters out irrelevant noise, uses Gemini to score fit and draft outreach, and tracks your application pipeline — all stored locally in SQLite. Nothing auto-applies or auto-messages anyone; every send is a one-click action you trigger yourself, to avoid CAPTCHA bans and stay within each platform's ToS.

## What it actually does

1. **Scrapes job boards** (LinkedIn, Indeed, Google, and optionally Glassdoor/ZipRecruiter/Naukri) via [jobspy](https://github.com/speedyapply/JobSpy), one keyword × one location at a time — not a single boolean "OR" query, because job portals treat the `location` field as literal text, not a search expression.
2. **Filters listings** through two gates:
   - A **hard exclusion gate** (Intern/Fresher/Java/Python/C#/.NET by default, or whatever your profile defines) — these are always discarded.
   - A **soft inclusion gate** (Angular/Node/MEAN/etc. by default) — titles that match go in the main "strict" list; titles that don't (but weren't hard-excluded) go in a separate **"Borderline"** section instead of being silently dropped, since plenty of real openings use generic titles like "Software Engineer."
3. **Deduplicates across platforms** — the same opening scraped from both LinkedIn and Indeed gets merged into one card, preferring the LinkedIn link (you're already authenticated there) and merging the longer description and any contact info found.
4. **Scores fit with Gemini** — reads the full job description against your resume/skills and returns a match score, an experience-fit verdict, a tailored resume summary, and (if an email was found) a drafted cold email.
5. **Extracts recruiter contact info** — pulls emails (jobspy's parsed field + regex over the description) and Indian phone/WhatsApp numbers, and gives you one-click `mailto:` and `wa.me` links pre-filled with an AI-drafted message.
6. **Persists everything to SQLite** (`job_agent.db`) — scan results, AI analysis, and your application status survive app restarts and are cached per job so re-scanning never re-spends AI quota on a job already analyzed.
7. **Supports multiple candidate profiles** — resume text, known skills, and title-filter keywords are stored per profile, not hardcoded, so the same app can run a search for a different resume/stack/person with independent history.
8. **Recruiter/agency discovery** — a curated directory of IT staffing agencies and tech-recruiter platforms (Instahyre, Hirist, Cutshort, etc.), with dynamically-generated Google/LinkedIn search links (never hardcoded company URLs, which can go stale), plus a generic outreach-pitch generator for cold-messaging recruiters who aren't tied to a specific job posting.

## Project structure

```
app.py               Streamlit UI — sidebar config, profile editor, the 3 tabs, all st.* calls
job_agent_core.py     Pure-Python backend: scraping, filtering, dedup, Gemini prompts, link builders.
                       No Streamlit imports — reusable from a headless script too.
job_store.py          SQLite persistence: profiles, jobs, cached AI analysis, pipeline status.
job_agent.db           The actual database (created on first run). Don't share this file — it
                       contains your resume text and scraped job descriptions.
requirements.txt       Python dependencies.
```

### Why split into three files

- `job_agent_core.py` has zero UI dependencies on purpose, so the same scraping/analysis logic could be driven by a cron-style headless script later without duplicating code.
- `job_store.py` isolates all SQL — nothing else in the app writes raw SQL.
- `app.py` is just wiring: sidebar inputs → core functions → store reads/writes → render.

## Setup

```bash
cd d:\Alfaiz\job-hunting
pip install -r requirements.txt
```

You'll also need a free **Gemini API key** from [aistudio.google.com](https://aistudio.google.com) — pasted into the sidebar at runtime, never stored in a file.

## Running

```bash
streamlit run app.py
```

Open **http://localhost:8501**. Paste your Gemini API key into the sidebar to unlock the app (everything is gated behind this — no key, no scanning).

### Accessing from your phone

- **Same WiFi**: open the "Network URL" Streamlit prints on startup (e.g. `http://192.168.1.34:8501`) on your phone's browser — works immediately, no setup.
- **From anywhere**: run a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/) (`cloudflared tunnel --url http://localhost:8501`) for a temporary public URL. There's **no login screen on this app** — anyone with the tunnel URL can open your dashboard and burn your API quota, so don't share it. The URL changes every time the tunnel restarts.

## Using the app

### Sidebar
- **Candidate Profile**: pick or create a profile. Each profile has its own resume text, known skills list, and title include/exclude keywords. "Edit / Create Profile" expander lets you change all of this without touching code — useful for tuning the filters or running the app for a different resume/stack entirely.
- **Gemini API Key / Model Name**: model defaults to `gemini-2.0-flash`. If you hit a `429` with `limit: 0`, that specific model has zero free-tier quota on your key — try `gemini-1.5-flash` instead (quota is allocated per-model, not per-account).
- **Matrix Search Scope**: comma-separated keywords and locations, scanned as every keyword × every location pair. `Remote` as a location is handled specially — it searches `India` with `is_remote=True` rather than passing the literal word "Remote," which portals would otherwise treat as a place name (and occasionally match against unrelated places that happen to share the abbreviation).
- **Target Platforms**: LinkedIn/Indeed/Google are reliable; Glassdoor/ZipRecruiter/Naukri commonly return 403/406 (anti-bot blocks) without rotating proxies.
- **Bulk AI Analysis**: auto-analyzes every new strict-match job right after a scan, throttled by a configurable delay between calls to avoid free-tier rate limits.

### Tab 1 — 🔍 Job Matrix Scan
Run the scan, watch the diagnostics expander for exactly which keyword/location passes succeeded or failed (errors are never silently swallowed), then review each job card: one-click Apply link, detected HR email/phone, AI fit analysis, editable resume summary, and pre-filled email/WhatsApp outreach. Borderline matches (didn't clearly match your include keywords, but weren't hard-excluded either) are in a separate collapsed section underneath.

### Tab 2 — 🤝 Recruiter Network & Outreach
A directory of IT staffing agencies and recruiter platforms with one-click "find their current site/LinkedIn page" buttons (search links, not hardcoded URLs — agency sites move and we don't want to send you somewhere stale), a LinkedIn recruiter people-search matrix built from your keywords × locations, a LinkedIn "hiring posts" search (structured job boards miss a lot of openings that only ever get posted as a feed update), and a generic outreach-pitch generator for cold-messaging recruiters without a specific job tied to it.

### Tab 3 — 📋 My Pipeline
Every job ever scanned for the active profile, with editable status (New/Applied/Interviewing/Rejected/Offer/Skipped) and a follow-up date, filterable by status and minimum match score. This is the persistent record — re-scanning never loses it.

## Data & persistence notes

- `job_agent.db` is a single SQLite file. Back it up before any manual schema surgery (`cp job_agent.db job_agent.db.backup`).
- Job listings (`jobs` table) are shared across all profiles — the posting itself doesn't change based on who's looking at it. AI analysis and pipeline status are scoped per-profile, so two profiles can independently score and track the same listing differently.
- A failed AI call (quota exhausted, network error, bad JSON) is **never cached** — it's retried automatically on the next scan/bulk-analysis pass instead of permanently sticking as a placeholder result.

## Known limitations

- **Naukri**, **Glassdoor**, and **ZipRecruiter** are actively anti-bot-blocked (406/403) in most residential setups without proxies — LinkedIn/Indeed/Google are the dependable sources.
- `google-generativeai` is an end-of-life package upstream (Google's successor is `google-genai`) — it still works today but may need a migration down the line.
- No authentication on the app itself — fine for `localhost`/same-WiFi use; be deliberate before tunneling it to a public URL.
- LinkedIn's post/content search has no public API, so the "Browse LinkedIn Hiring Posts" feature opens LinkedIn's own search UI in your browser rather than scraping it — this is intentional, automating that surface risks your LinkedIn account.
