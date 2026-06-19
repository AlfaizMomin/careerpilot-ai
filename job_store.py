"""SQLite persistence for candidate profiles, scraped jobs, cached LLM
analysis, and your application pipeline status. Lets results survive across
app restarts and across headless scheduled_scan.py runs, lets analysis be
cached so re-scanning never re-spends Gemini quota on an already-analyzed
job, and lets multiple candidate profiles share the same job pool while
keeping separate analysis/status per profile."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

DB_PATH = "job_agent.db"


@contextmanager
def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_to_profile_scoped_tables(conn):
    """analysis/pipeline_status used to be keyed by job_url alone. Multiple
    profiles need independent analysis+status per job, so both need a
    composite (job_url, profile_id) primary key. SQLite can't ALTER a
    primary key in place, so existing tables (if not already migrated) are
    recreated and their rows carried over under profile_id='default'."""
    existing_tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    if "analysis" in existing_tables and "profile_id" not in _table_columns(conn, "analysis"):
        conn.execute("ALTER TABLE analysis RENAME TO analysis_old")
        conn.execute("""
            CREATE TABLE analysis (
                job_url TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                should_apply INTEGER,
                match_score INTEGER,
                estimated_experience_required TEXT,
                matching_skills TEXT,
                missing_skills TEXT,
                reason_for_changes TEXT,
                modified_summary TEXT,
                email_subject TEXT,
                email_body TEXT,
                analyzed_at TEXT,
                PRIMARY KEY (job_url, profile_id)
            )
        """)
        conn.execute("""
            INSERT INTO analysis (job_url, profile_id, should_apply, match_score, estimated_experience_required,
                matching_skills, missing_skills, reason_for_changes, modified_summary, email_subject, email_body, analyzed_at)
            SELECT job_url, 'default', should_apply, match_score, estimated_experience_required,
                matching_skills, missing_skills, reason_for_changes, modified_summary, email_subject, email_body, analyzed_at
            FROM analysis_old
        """)
        conn.execute("DROP TABLE analysis_old")

    if "pipeline_status" in existing_tables and "profile_id" not in _table_columns(conn, "pipeline_status"):
        conn.execute("ALTER TABLE pipeline_status RENAME TO pipeline_status_old")
        conn.execute("""
            CREATE TABLE pipeline_status (
                job_url TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT DEFAULT 'New',
                follow_up_date TEXT,
                notes TEXT,
                notified INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (job_url, profile_id)
            )
        """)
        conn.execute("""
            INSERT INTO pipeline_status (job_url, profile_id, status, follow_up_date, notes, notified, updated_at)
            SELECT job_url, 'default', status, follow_up_date, notes, notified, updated_at
            FROM pipeline_status_old
        """)
        conn.execute("DROP TABLE pipeline_status_old")


def init_db(db_path=DB_PATH, seed_defaults=None):
    """seed_defaults, if provided, is a dict of the original hardcoded profile
    (candidate_name, candidate_email, resume_text, known_skills, include_keywords,
    exclude_keywords, exclude_substrings) used to create the 'default' profile
    on first run so existing behavior is unchanged unless you add more profiles."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                profile_id TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL,
                candidate_name TEXT,
                candidate_email TEXT,
                resume_text TEXT,
                known_skills TEXT,
                include_keywords TEXT,
                exclude_keywords TEXT,
                exclude_substrings TEXT,
                default_keywords TEXT,
                default_locations TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_url TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                site TEXT,
                apply_url TEXT,
                description TEXT,
                emails TEXT,
                also_on TEXT,
                match_tier TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analysis (
                job_url TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                should_apply INTEGER,
                match_score INTEGER,
                estimated_experience_required TEXT,
                matching_skills TEXT,
                missing_skills TEXT,
                reason_for_changes TEXT,
                modified_summary TEXT,
                email_subject TEXT,
                email_body TEXT,
                analyzed_at TEXT,
                PRIMARY KEY (job_url, profile_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_status (
                job_url TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT DEFAULT 'New',
                follow_up_date TEXT,
                notes TEXT,
                notified INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (job_url, profile_id)
            )
        """)

        _migrate_to_profile_scoped_tables(conn)

        if seed_defaults is not None:
            existing = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
            if existing == 0:
                now = _now()
                conn.execute("""
                    INSERT INTO profiles (profile_id, profile_name, candidate_name, candidate_email, resume_text,
                        known_skills, include_keywords, exclude_keywords, exclude_substrings,
                        default_keywords, default_locations, created_at, updated_at)
                    VALUES ('default', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    seed_defaults.get("profile_name", "Default"),
                    seed_defaults.get("candidate_name", ""),
                    seed_defaults.get("candidate_email", ""),
                    seed_defaults.get("resume_text", ""),
                    json.dumps(seed_defaults.get("known_skills", [])),
                    json.dumps(seed_defaults.get("include_keywords", [])),
                    json.dumps(seed_defaults.get("exclude_keywords", [])),
                    json.dumps(seed_defaults.get("exclude_substrings", [])),
                    seed_defaults.get("default_keywords", ""),
                    seed_defaults.get("default_locations", ""),
                    now, now,
                ))


# ==========================================
# PROFILES
# ==========================================
def _row_to_profile_dict(row):
    return {
        "profile_id": row["profile_id"],
        "profile_name": row["profile_name"],
        "candidate_name": row["candidate_name"],
        "candidate_email": row["candidate_email"],
        "resume_text": row["resume_text"],
        "known_skills": json.loads(row["known_skills"] or "[]"),
        "include_keywords": json.loads(row["include_keywords"] or "[]"),
        "exclude_keywords": json.loads(row["exclude_keywords"] or "[]"),
        "exclude_substrings": json.loads(row["exclude_substrings"] or "[]"),
        "default_keywords": row["default_keywords"] or "",
        "default_locations": row["default_locations"] or "",
    }


def get_all_profiles(db_path=DB_PATH):
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at ASC").fetchall()
    return [_row_to_profile_dict(r) for r in rows]


def get_profile(profile_id, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    return _row_to_profile_dict(row) if row else None


def _slugify(name):
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    return slug or "profile"


def save_profile(profile_id, profile_name, candidate_name, candidate_email, resume_text,
                  known_skills, include_keywords, exclude_keywords, exclude_substrings,
                  default_keywords, default_locations, db_path=DB_PATH):
    """Creates a new profile if profile_id is None/empty (slugified from the
    name, with a numeric suffix on collision), otherwise updates in place."""
    now = _now()
    with get_connection(db_path) as conn:
        if not profile_id:
            base_slug = _slugify(profile_name)
            slug = base_slug
            n = 2
            while conn.execute("SELECT 1 FROM profiles WHERE profile_id = ?", (slug,)).fetchone():
                slug = f"{base_slug}-{n}"
                n += 1
            profile_id = slug
            conn.execute("""
                INSERT INTO profiles (profile_id, profile_name, candidate_name, candidate_email, resume_text,
                    known_skills, include_keywords, exclude_keywords, exclude_substrings,
                    default_keywords, default_locations, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                profile_id, profile_name, candidate_name, candidate_email, resume_text,
                json.dumps(known_skills), json.dumps(include_keywords), json.dumps(exclude_keywords),
                json.dumps(exclude_substrings), default_keywords, default_locations, now, now,
            ))
        else:
            conn.execute("""
                UPDATE profiles SET profile_name=?, candidate_name=?, candidate_email=?, resume_text=?,
                    known_skills=?, include_keywords=?, exclude_keywords=?, exclude_substrings=?,
                    default_keywords=?, default_locations=?, updated_at=?
                WHERE profile_id=?
            """, (
                profile_name, candidate_name, candidate_email, resume_text,
                json.dumps(known_skills), json.dumps(include_keywords), json.dumps(exclude_keywords),
                json.dumps(exclude_substrings), default_keywords, default_locations, now, profile_id,
            ))
    return profile_id


def delete_profile(profile_id, db_path=DB_PATH):
    """Refuses to delete the last remaining profile so the app never ends up
    with zero profiles to select. Also drops that profile's analysis/status
    rows (the underlying job listings in `jobs` are left alone — other
    profiles may still reference them)."""
    with get_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
        if count <= 1:
            return False
        conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM analysis WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM pipeline_status WHERE profile_id = ?", (profile_id,))
    return True


# ==========================================
# JOBS (profile-independent listing data)
# ==========================================
def upsert_jobs(df, match_tier, db_path=DB_PATH):
    """Insert new jobs / refresh last_seen_at for jobs already known, tagged
    with whether this scan classified them as 'strict' or 'borderline'."""
    if df is None or df.empty:
        return
    now = _now()
    with get_connection(db_path) as conn:
        for _, row in df.iterrows():
            job_url = row.get('job_url')
            if not job_url or pd.isna(job_url):
                continue
            emails = row.get('emails')
            emails_json = json.dumps(emails) if isinstance(emails, list) else json.dumps([])
            description = row.get('description')
            description = "" if pd.isna(description) else str(description)
            conn.execute("""
                INSERT INTO jobs (job_url, title, company, location, site, apply_url, description, emails, also_on, match_tier, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    title=excluded.title, company=excluded.company, location=excluded.location,
                    site=excluded.site, apply_url=excluded.apply_url, description=excluded.description,
                    emails=excluded.emails, also_on=excluded.also_on, match_tier=excluded.match_tier,
                    last_seen_at=excluded.last_seen_at
            """, (
                job_url,
                None if pd.isna(row.get('title')) else str(row.get('title')),
                None if pd.isna(row.get('company')) else str(row.get('company')),
                None if pd.isna(row.get('location')) else str(row.get('location')),
                None if pd.isna(row.get('site')) else str(row.get('site')),
                job_url,
                description,
                emails_json,
                row.get('_also_on') if isinstance(row.get('_also_on'), str) else '',
                match_tier,
                now,
                now,
            ))


# ==========================================
# ANALYSIS (per job_url + profile_id)
# ==========================================
def get_unanalyzed_job_urls(job_urls, profile_id, db_path=DB_PATH):
    if not job_urls:
        return []
    with get_connection(db_path) as conn:
        placeholders = ",".join("?" * len(job_urls))
        rows = conn.execute(
            f"SELECT job_url FROM analysis WHERE profile_id = ? AND job_url IN ({placeholders})",
            [profile_id] + job_urls,
        ).fetchall()
        analyzed = {r["job_url"] for r in rows}
    return [u for u in job_urls if u not in analyzed]


def save_analysis(job_url, profile_id, result, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO analysis (job_url, profile_id, should_apply, match_score, estimated_experience_required,
                matching_skills, missing_skills, reason_for_changes, modified_summary, email_subject, email_body, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_url, profile_id) DO UPDATE SET
                should_apply=excluded.should_apply, match_score=excluded.match_score,
                estimated_experience_required=excluded.estimated_experience_required,
                matching_skills=excluded.matching_skills, missing_skills=excluded.missing_skills,
                reason_for_changes=excluded.reason_for_changes, modified_summary=excluded.modified_summary,
                email_subject=excluded.email_subject, email_body=excluded.email_body, analyzed_at=excluded.analyzed_at
        """, (
            job_url, profile_id,
            int(bool(result.get('should_apply', True))),
            int(result.get('match_score', 0) or 0),
            result.get('estimated_experience_required', ''),
            json.dumps(result.get('matching_skills', [])),
            json.dumps(result.get('missing_skills', [])),
            result.get('reason_for_changes', ''),
            result.get('modified_summary', ''),
            result.get('email_subject', ''),
            result.get('email_body', ''),
            _now(),
        ))


def get_analysis(job_url, profile_id, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM analysis WHERE job_url = ? AND profile_id = ?", (job_url, profile_id)).fetchone()
    if not row:
        return None
    return {
        "should_apply": bool(row["should_apply"]),
        "match_score": row["match_score"],
        "estimated_experience_required": row["estimated_experience_required"],
        "matching_skills": json.loads(row["matching_skills"] or "[]"),
        "missing_skills": json.loads(row["missing_skills"] or "[]"),
        "reason_for_changes": row["reason_for_changes"],
        "modified_summary": row["modified_summary"],
        "email_subject": row["email_subject"],
        "email_body": row["email_body"],
    }


# ==========================================
# PIPELINE STATUS (per job_url + profile_id)
# ==========================================
def upsert_status(job_url, profile_id, status=None, follow_up_date=None, notes=None, db_path=DB_PATH):
    with get_connection(db_path) as conn:
        existing = conn.execute("SELECT * FROM pipeline_status WHERE job_url = ? AND profile_id = ?", (job_url, profile_id)).fetchone()
        new_status = status if status is not None else (existing["status"] if existing else "New")
        new_follow_up = follow_up_date if follow_up_date is not None else (existing["follow_up_date"] if existing else None)
        new_notes = notes if notes is not None else (existing["notes"] if existing else None)
        notified = existing["notified"] if existing else 0
        conn.execute("""
            INSERT INTO pipeline_status (job_url, profile_id, status, follow_up_date, notes, notified, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_url, profile_id) DO UPDATE SET
                status=excluded.status, follow_up_date=excluded.follow_up_date,
                notes=excluded.notes, updated_at=excluded.updated_at
        """, (job_url, profile_id, new_status, new_follow_up, new_notes, notified, _now()))


def mark_notified(job_urls, profile_id, db_path=DB_PATH):
    if not job_urls:
        return
    now = _now()
    with get_connection(db_path) as conn:
        for job_url in job_urls:
            conn.execute("""
                INSERT INTO pipeline_status (job_url, profile_id, status, notified, updated_at)
                VALUES (?, 'New', 1, ?)
                ON CONFLICT(job_url, profile_id) DO UPDATE SET notified=1, updated_at=excluded.updated_at
            """, (job_url, profile_id, now))


def get_unnotified_high_score_jobs(profile_id, min_score=75, db_path=DB_PATH):
    """For the scheduled script: jobs worth applying to (for this profile)
    that haven't been surfaced in a notification yet."""
    with get_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT j.job_url, j.title, j.company, j.location, j.apply_url, a.match_score, a.estimated_experience_required
            FROM jobs j
            JOIN analysis a ON a.job_url = j.job_url AND a.profile_id = ?
            LEFT JOIN pipeline_status p ON p.job_url = j.job_url AND p.profile_id = ?
            WHERE a.should_apply = 1 AND a.match_score >= ? AND COALESCE(p.notified, 0) = 0
            ORDER BY a.match_score DESC
        """, (profile_id, profile_id, min_score)).fetchall()
    return [dict(r) for r in rows]


def get_all_jobs_df(profile_id, db_path=DB_PATH, status_filter=None, min_score=None):
    """Merged jobs + analysis + pipeline_status view for the Pipeline tracker
    tab, scoped to one profile."""
    with get_connection(db_path) as conn:
        query = """
            SELECT j.job_url, j.title, j.company, j.location, j.site, j.apply_url, j.match_tier,
                   j.first_seen_at, j.last_seen_at,
                   a.should_apply, a.match_score, a.estimated_experience_required, a.modified_summary,
                   COALESCE(p.status, 'New') AS status, p.follow_up_date, p.notes
            FROM jobs j
            LEFT JOIN analysis a ON a.job_url = j.job_url AND a.profile_id = :profile_id
            LEFT JOIN pipeline_status p ON p.job_url = j.job_url AND p.profile_id = :profile_id
        """
        conditions = []
        params = {"profile_id": profile_id}
        if status_filter:
            conditions.append("COALESCE(p.status, 'New') = :status_filter")
            params["status_filter"] = status_filter
        if min_score is not None:
            conditions.append("COALESCE(a.match_score, 0) >= :min_score")
            params["min_score"] = min_score
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY j.last_seen_at DESC"
        df = pd.read_sql_query(query, conn, params=params)
    return df
