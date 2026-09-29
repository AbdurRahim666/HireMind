import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402
from hiremind import matcher, ranker  # noqa: E402

PROFILE = {
    "version": 1,
    "summary": "Backend engineer",
    "headline": "Python Dev",
    "seniority": "mid",
    "yoe": 4.5,
    "skills": [{"name": "Python", "category": "language", "years": 4}],
    "experience": [],
    "education": [],
    "certifications": ["AWS"],
    "languages": [],
    "target_roles": ["Backend Engineer"],
    "remote_preferred": True,
    "internship_mode": False,
    "keywords": ["Django", "Postgres"],
    "preferred": {"locations": [], "remote": True},
}

GOOD_JOB = {
    "title": "Backend Engineer",
    "location": "Remote",
    "tags": ["Python"],
    "yoe_bucket": "4-7",
    "url": "https://example.com/1",
}
WEAK_JOB = {
    "title": "Barista",
    "location": "",
    "url": "https://example.com/2",
}


class HousekeepingDBTestCase(unittest.TestCase):
    """Point db at a throwaway SQLite file created via the real init_db()."""

    @classmethod
    def setUpClass(cls):
        import db as _db
        cls._orig_path = os.path.join(os.path.dirname(os.path.abspath(_db.__file__)), "job_agent.db")
        fd, cls.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _db._DB_PATH = cls.tmp
        _db.init_db()

    @classmethod
    def tearDownClass(cls):
        import db as _db
        try:
            os.remove(cls.tmp)
        except OSError:
            pass
        _db._DB_PATH = cls._orig_path

    def tearDown(self):
        with db._get_conn() as (conn, cur):
            cur.execute("DELETE FROM jobs")
            cur.execute("DELETE FROM sessions")
            cur.execute("DELETE FROM candidate_profiles")
            cur.execute("DELETE FROM users")
            conn.commit()


class TestScoreForUrls(unittest.TestCase):
    def test_keyed_by_url(self):
        out = ranker.score_for_urls(PROFILE, [GOOD_JOB, WEAK_JOB])
        self.assertEqual(out["count"], 2)
        self.assertEqual(set(out["scores"]), {"https://example.com/1", "https://example.com/2"})
        self.assertIn(GOOD_JOB["url"], out["scores"])

    def test_matches_matcher_score_exactly(self):
        out = ranker.score_for_urls(PROFILE, [GOOD_JOB])
        expected = matcher.score_job(PROFILE, GOOD_JOB)
        got = out["scores"][GOOD_JOB["url"]]
        self.assertEqual(got["score"], expected["score"])
        self.assertEqual(got["verdict"], expected["verdict"])
        self.assertEqual(got["matched_role"], expected["matched_role"])
        self.assertEqual(got["matched_skills"], expected["matched_skills"])

    def test_block_is_compact_and_drops_breakdown(self):
        block = ranker.score_for_urls(PROFILE, [GOOD_JOB])["scores"][GOOD_JOB["url"]]
        self.assertEqual(
            set(block), {"score", "verdict", "matched_role", "matched_skills", "reasons"})
        self.assertNotIn("breakdown", block)

    def test_reasons_are_capped(self):
        out = ranker.score_for_urls(PROFILE, [GOOD_JOB])
        self.assertLessEqual(len(out["scores"][GOOD_JOB["url"]]["reasons"]), 3)

    def test_reasons_are_the_matcher_prefix(self):
        # The ranker truncates to the top N reasons. The matcher's "seen before"
        # note is emitted last, so it is expected to be dropped here — Phase 2
        # already surfaces that as its own badge on the card.
        job = dict(GOOD_JOB, _seen_before=True)
        block = ranker.score_for_urls(PROFILE, [job])["scores"][GOOD_JOB["url"]]
        full = matcher.score_job(PROFILE, job)["reasons"]
        self.assertEqual(block["reasons"], full[:ranker._REASONS_LIMIT])
        self.assertEqual(len(block["reasons"]), 3)

    def test_skips_jobs_without_url(self):
        out = ranker.score_for_urls(PROFILE, [{"title": "Backend Engineer"}, {"url": ""}])
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["scores"], {})

    def test_ignores_non_dict_entries(self):
        out = ranker.score_for_urls(PROFILE, [GOOD_JOB, None, "nope", 7])
        self.assertEqual(out["count"], 1)

    def test_limit_is_respected(self):
        jobs = [dict(GOOD_JOB, url=f"https://example.com/{i}") for i in range(10)]
        self.assertEqual(ranker.score_for_urls(PROFILE, jobs, limit=3)["count"], 3)
        self.assertEqual(ranker.score_for_urls(PROFILE, jobs, limit=0)["count"], 0)

    def test_empty_and_non_list(self):
        self.assertEqual(ranker.score_for_urls(PROFILE, [])["count"], 0)
        self.assertEqual(ranker.score_for_urls(PROFILE, None)["count"], 0)
        self.assertEqual(ranker.score_for_urls(PROFILE, "nope")["count"], 0)

    def test_does_not_mutate_inputs(self):
        job = dict(GOOD_JOB)
        jobs = [job]
        ranker.score_for_urls(PROFILE, jobs)
        self.assertEqual(job, GOOD_JOB)
        self.assertNotIn("match", job)
        self.assertEqual(len(jobs), 1)

    def test_stable_for_identical_input(self):
        a = ranker.score_for_urls(PROFILE, [GOOD_JOB, WEAK_JOB])
        b = ranker.score_for_urls(PROFILE, [GOOD_JOB, WEAK_JOB])
        self.assertEqual(a, b)

    def test_no_llm_calls(self):
        class _Boom:
            def __getattr__(self, name):
                def _raise(*a, **k):
                    raise AssertionError(f"ranker must not call the LLM ({name})")
                return _raise

        with patch("llm.llm_client.LLMClient", _Boom()):
            out = ranker.score_for_urls(PROFILE, [GOOD_JOB])
        self.assertEqual(out["count"], 1)


class TestSearchScoresEndpoint(HousekeepingDBTestCase):
    SID = "sid-hiremind-3a"

    def setUp(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from utils.rate_limiter import _limits

        self.client = TestClient(app)
        _limits.clear()

    def _headers(self, email="carl@example.com"):
        db.create_user(email, "Carl")
        from utils.jwt import create_token
        return {"Authorization": "Bearer " + create_token(email)}

    def _session(self, owner="carl@example.com", sid=None):
        sid = sid or self.SID
        db.create_session(sid, user_email=owner, sites=["linkedin"], roles=["Backend Engineer"])
        db.set_raw_jobs(sid, [dict(GOOD_JOB), dict(WEAK_JOB)])
        return sid

    def _profile(self, email="carl@example.com"):
        db.save_candidate_profile(email, "r.pdf", "text", PROFILE, version=1)

    def test_requires_auth(self):
        self._session()
        r = self.client.get(f"/api/hiremind/search/{self.SID}")
        self.assertEqual(r.status_code, 401)

    def test_disabled_returns_404(self):
        headers = self._headers()
        self._session()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=False):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_unknown_search_returns_404(self):
        headers = self._headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get("/api/hiremind/search/does-not-exist", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_other_users_session_returns_403(self):
        headers = self._headers()
        self._session(owner="someone-else@example.com")
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 403)

    def test_session_owner_match_is_case_insensitive(self):
        headers = self._headers(email="carl@example.com")
        self._session(owner="CARL@Example.com")
        self._profile()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 200)

    def test_guest_session_returns_403(self):
        headers = self._headers()
        self._session(owner="")
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 403)

    def test_missing_profile_returns_409(self):
        headers = self._headers()
        self._session()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 409)

    def test_returns_scores_keyed_by_stored_urls(self):
        headers = self._headers()
        self._session()
        self._profile()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["email"], "carl@example.com")
        self.assertEqual(data["search_id"], self.SID)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["count"], 2)
        self.assertEqual(set(data["scores"]), {GOOD_JOB["url"], WEAK_JOB["url"]})
        block = data["scores"][GOOD_JOB["url"]]
        self.assertIn("score", block)
        self.assertIn("verdict", block)
        self.assertNotIn("breakdown", block)
        self.assertGreaterEqual(
            data["scores"][GOOD_JOB["url"]]["score"],
            data["scores"][WEAK_JOB["url"]]["score"],
        )

    def test_rate_limited(self):
        headers = self._headers()
        self._session()
        self._profile()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            codes = [self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers).status_code
                     for _ in range(8)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)

    def test_does_not_modify_stored_jobs_or_scores(self):
        headers = self._headers()
        self._session()
        self._profile()
        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) FROM jobs WHERE session_id = ? AND total_score IS NOT NULL",
                        (self.SID,))
            before = cur.fetchone()[0]

        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)

        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) FROM jobs WHERE session_id = ? AND total_score IS NOT NULL",
                        (self.SID,))
            self.assertEqual(cur.fetchone()[0], before)
            cur.execute("SELECT COUNT(*) FROM known_jobs")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_existing_jobs_route_unchanged_when_flag_off(self):
        """Phase 3a must not touch the unauthenticated /jobs payload."""
        headers = self._headers()
        self._session()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=False):
            baseline = self.client.get(f"/jobs?search_id={self.SID}").json()
            self.client.get(f"/api/hiremind/search/{self.SID}", headers=headers)
            after = self.client.get(f"/jobs?search_id={self.SID}").json()
        self.assertEqual(baseline, after)
        self.assertEqual(baseline["total"], 2)
        for job in baseline["jobs"]:
            self.assertNotIn("match", job)
            self.assertNotIn("_hiremind", job)


if __name__ == "__main__":
    unittest.main()
