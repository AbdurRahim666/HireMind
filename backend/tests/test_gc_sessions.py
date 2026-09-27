import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402


def _make_session(sid="sess-1", email="user@example.com", created=None, ip="", geo=()):
    now = db._now() if created is None else created
    cols = """(id, created_at, updated_at, sites, keywords, roles, keywords_count,
            roles_count, resume_length, internship_mode, location, user_email, resume_filename"""
    vals = (sid, now, now, json.dumps(["indeed"]), json.dumps(["react"]),
            json.dumps(["software engineer"]), 1, 1, 100, 0, "Toronto",
            email, f"{sid}.txt")
    if ip or geo:
        cols += ", ip_address, user_country, user_city, user_region"
        vals += (ip, *(geo or ("", "", "")))
    cols += ")"
    with db._write_lock:
        with db._get_conn() as (conn, cur):
            cur.execute(f"INSERT INTO sessions {cols} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" if (ip or geo) else f"INSERT INTO sessions {cols} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            conn.commit()


def _make_jobs(sid, n_raw=3, n_scored=2):
    now = db._now()
    rows = []
    for i in range(n_raw):
        rows.append((sid, f"Raw Job {i}", "Acme", "Toronto", f"https://x/{i}",
                     "desc", "[]", 0, 0, "", "", "", "", 1, "", "", "", "", now))
    for i in range(n_scored):
        rows.append((sid, f"Scored Job {i}", "Acme", "Toronto", f"https://x/score{i}",
                     "desc", "[]", 90, 50, 80, "great", "", "", "", 0, "", "mid", "", now))
    with db._write_lock:
        with db._get_conn() as (conn, cur):
            cur.executemany(
                """INSERT INTO jobs
                   (session_id, title, company, location, url, description, tags,
                    ai_score, keyword_score, total_score, reason, salary, experience_level,
                    is_raw, date_posted, company_url, job_level, matched_role, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            conn.commit()


class GcSessionsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db._DB_PATH = cls.tmp
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls.tmp)
        except OSError:
            pass
        db._DB_PATH = os.path.join(os.path.dirname(os.path.abspath(db.__file__)), "job_agent.db")

    def tearDown(self):
        with db._write_lock:
            with db._get_conn() as (conn, cur):
                for t in ("sessions", "events", "visits"):
                    cur.execute(f"DELETE FROM {t}")
                cur.execute("DELETE FROM jobs")
                conn.commit()

    def test_gc_purges_stale_jobs_and_events_keeps_sessions(self):
        old = (datetime.utcnow() - timedelta(days=200)).isoformat()
        _make_session("gc-old", ip="3.3.3.3", geo=("Canada", "Toronto", "ON"))
        _make_jobs("gc-old", n_raw=4, n_scored=2)
        with db._write_lock:
            with db._get_conn() as (conn, cur):
                cur.execute("INSERT INTO events (session_id, event, created_at) VALUES ('gc-old', 'done', ?)", (old,))
                cur.execute("UPDATE sessions SET updated_at = ?, scraped = 4 WHERE id = 'gc-old'", (old,))
                conn.commit()

        db.gc_sessions(max_age_minutes=0)

        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE id = 'gc-old'")
            self.assertEqual(cur.fetchone()["n"], 1)
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE session_id = 'gc-old'")
            self.assertEqual(cur.fetchone()["n"], 0)
            cur.execute("SELECT COUNT(*) AS n FROM events WHERE session_id = 'gc-old'")
            self.assertEqual(cur.fetchone()["n"], 0)
            cur.execute("SELECT ip_address, keywords_count, scraped FROM sessions WHERE id = 'gc-old'")
            row = cur.fetchone()
            self.assertEqual(row["ip_address"], "3.3.3.3")
            self.assertEqual(row["keywords_count"], 1)
            self.assertEqual(row["scraped"], 4)  # raw job count survives in sessions

    def test_gc_keeps_fresh_session_data(self):
        _make_session("gc-fresh", email="f@example.com")
        _make_jobs("gc-fresh", n_raw=2)
        with db._write_lock:
            with db._get_conn() as (conn, cur):
                cur.execute("INSERT INTO events (session_id, event, created_at) VALUES ('gc-fresh', 'done', ?)", (db._now(),))
                conn.commit()

        db.gc_sessions(max_age_minutes=10080)  # 7 days

        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE id = 'gc-fresh'")
            self.assertEqual(cur.fetchone()["n"], 1)
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE session_id = 'gc-fresh'")
            self.assertEqual(cur.fetchone()["n"], 4)  # 2 raw + 2 scored
            cur.execute("SELECT COUNT(*) AS n FROM events WHERE session_id = 'gc-fresh'")
            self.assertEqual(cur.fetchone()["n"], 1)

    def test_gc_never_deletes_sessions(self):
        _make_session("gc-keep", email="")
        _make_jobs("gc-keep", n_raw=1)
        old = (datetime.utcnow() - timedelta(days=400)).isoformat()
        with db._write_lock:
            with db._get_conn() as (conn, cur):
                cur.execute("UPDATE sessions SET updated_at = ? WHERE id = 'gc-keep'", (old,))
                conn.commit()
        db.gc_sessions(max_age_minutes=0)
        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE id = 'gc-keep'")
            self.assertEqual(cur.fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main()