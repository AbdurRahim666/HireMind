import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402
from hiremind import matcher  # noqa: E402

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
            cur.execute("DELETE FROM candidate_profiles")
            cur.execute("DELETE FROM users")
            conn.commit()


class TestRoleFit(unittest.TestCase):
    def test_exact_target_as_substring(self):
        score, role = matcher.role_fit("Senior Backend Engineer", ["Backend Engineer"])
        self.assertEqual((score, role), (1.0, "Backend Engineer"))

    def test_partial_token_overlap(self):
        score, role = matcher.role_fit("Backend Developer", ["Backend Engineer"])
        self.assertAlmostEqual(score, 0.5)
        self.assertEqual(role, "Backend Engineer")

    def test_no_target_roles_is_neutral(self):
        score, role = matcher.role_fit("Anything", [])
        self.assertEqual(score, 0.6)
        self.assertIsNone(role)

    def test_empty_title(self):
        score, role = matcher.role_fit("", ["Backend Engineer"])
        self.assertEqual(score, 0.0)
        self.assertIsNone(role)


class TestSkillFit(unittest.TestCase):
    def test_core_skill_fills_expected(self):
        text = "Senior Backend Engineer Python Django Postgres AWS"
        score, matched = matcher.skill_fit(PROFILE, text)
        self.assertEqual(score, 1.0)
        self.assertIn("Python", matched)

    def test_partial_overlap(self):
        many = {"skills": [{"name": s} for s in
                           ["Python", "Go", "Rust", "Java", "Ruby", "PHP", "C++"]],
                "keywords": [], "certifications": []}
        score, matched = matcher.skill_fit(many, "Senior Backend Engineer Java")
        self.assertEqual(score, 1 / 6)
        self.assertEqual(matched, ["Java"])

    def test_no_signal_is_neutral(self):
        empty = {"skills": [], "keywords": [], "certifications": []}
        score, matched = matcher.skill_fit(empty, "Anything here")
        self.assertEqual(score, 0.6)
        self.assertEqual(matched, [])


class TestSeniorityFit(unittest.TestCase):
    def test_matching_bucket(self):
        score, note = matcher.seniority_fit(PROFILE, {"yoe_bucket": "4-7"})
        self.assertEqual(score, 1.0)
        self.assertIn("4-7", note)

    def test_two_bucket_gap(self):
        score, _ = matcher.seniority_fit(PROFILE, {"yoe_bucket": "0-2"})
        self.assertAlmostEqual(score, 0.5)

    def test_not_specified_is_neutral(self):
        score, note = matcher.seniority_fit(PROFILE, {"yoe_bucket": "not_specified"})
        self.assertEqual(score, 0.6)

    def test_internship_respects_mode(self):
        off = matcher.seniority_fit(PROFILE, {"experience_level": "internship"})
        self.assertEqual(off[0], 0.1)
        on_profile = dict(PROFILE, internship_mode=True)
        on = matcher.seniority_fit(on_profile, {"experience_level": "internship"})
        self.assertEqual(on[0], 1.0)


class TestLocationFit(unittest.TestCase):
    def test_remote_pref_on(self):
        score, note = matcher.location_fit(PROFILE, {"location": "Remote"})
        self.assertEqual(score, 1.0)
        self.assertIn("Remote", note)

    def test_remote_pref_off_penalises_remote(self):
        off = dict(PROFILE, remote_preferred=False)
        score, _ = matcher.location_fit(off, {"location": "Remote"})
        self.assertEqual(score, 0.4)

    def test_country_match(self):
        profile = dict(PROFILE, preferred={"locations": [{"city": "Bengaluru", "country": "India"}], "remote": False})
        score, note = matcher.location_fit(profile, {"location": "Bengaluru, Karnataka, India"})
        self.assertEqual(score, 1.0)
        self.assertIn("region", note.lower())

    def test_city_match(self):
        profile = dict(PROFILE, preferred={"locations": [{"city": "Dubai", "country": ""}], "remote": False})
        score, note = matcher.location_fit(profile, {"location": "Dubai, UAE"})
        self.assertEqual(score, 1.0)
        self.assertIn("Dubai", note)

    def test_no_location_is_neutral(self):
        neutral = dict(PROFILE, remote_preferred=None, preferred={"locations": []})
        score, note = matcher.location_fit(neutral, {"location": ""})
        self.assertEqual(score, 0.6)
        self.assertIn("not specified", note)


class TestScoreJob(unittest.TestCase):
    def test_strong_match_structure(self):
        job = {
            "title": "Backend Engineer",
            "tags": ["Python", "Django"],
            "description": "AWS Postgres. 3+ years experience.",
            "location": "Remote",
            "salary": "AED 20,000/month",
            "yoe_bucket": "4-7",
        }
        m = matcher.score_job(PROFILE, job)
        self.assertIsInstance(m["score"], int)
        self.assertTrue(0 <= m["score"] <= 100)
        self.assertIn(m["verdict"], ("strong", "solid", "possible", "weak"))
        self.assertEqual(set(m["breakdown"]), {"role", "skills", "seniority", "location", "salary"})
        self.assertTrue(m["reasons"])
        self.assertEqual(m["matched_role"], "Backend Engineer")
        self.assertGreaterEqual(m["score"], 80)

    def test_weak_match(self):
        job = {"title": "Barista", "location": ""}
        m = matcher.score_job(PROFILE, job)
        self.assertLess(m["score"], 40)
        self.assertEqual(m["verdict"], "weak")

    def test_duplicate_reason(self):
        m = matcher.score_job(PROFILE, {"title": "Backend Engineer", "location": "Remote",
                                        "_seen_before": True, "yoe_bucket": "4-7",
                                        "tags": ["Python"]})
        self.assertTrue(any("another board" in r for r in m["reasons"]))


class TestScoreJobs(unittest.TestCase):
    def test_sorted_descending(self):
        jobs = [
            {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"], "yoe_bucket": "4-7"},
            {"title": "Barista", "location": ""},
            {"title": "Waiter", "location": ""},
        ]
        out = matcher.score_jobs(PROFILE, jobs)
        scores = [j["match"]["score"] for j in out]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(out), 3)

    def test_min_score_filter(self):
        jobs = [
            {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"], "yoe_bucket": "4-7"},
            {"title": "Barista", "location": ""},
        ]
        out = matcher.score_jobs(PROFILE, jobs, min_score=80)
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(out[0]["match"]["score"], 80)

    def test_does_not_mutate_inputs(self):
        job = {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"]}
        jobs = [job]
        matcher.score_jobs(PROFILE, jobs)
        self.assertEqual(job, {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"]})
        self.assertNotIn("match", job)

    def test_limit(self):
        jobs = [
            {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"], "yoe_bucket": "4-7"},
            {"title": "Barista", "location": ""},
            {"title": "Waiter", "location": ""},
        ]
        out = matcher.score_jobs(PROFILE, jobs, limit=2)
        self.assertEqual(len(out), 2)

    def test_empty_and_not_a_list(self):
        self.assertEqual(matcher.score_jobs(PROFILE, []), [])
        self.assertEqual(matcher.score_jobs(PROFILE, "nope"), [])

    def test_each_has_match_and_no_llm(self):
        out = matcher.score_jobs(PROFILE, [{"title": "X"}, {"title": "Y"}])
        self.assertTrue(all("match" in j and "reasons" in j["match"] for j in out))


class TestHiremindMatchAPI(HousekeepingDBTestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from utils.rate_limiter import _limits

        self.client = TestClient(app)
        _limits.clear()

    def _user_headers(self):
        db.create_user("mary@example.com", "Mary")
        from utils.jwt import create_token
        return {"Authorization": "Bearer " + create_token("mary@example.com")}

    def _save_profile(self):
        db.save_candidate_profile("mary@example.com", "r.pdf", "text", PROFILE, version=1)

    def test_match_requires_auth(self):
        r = self.client.post("/api/hiremind/match", json={"jobs": []})
        self.assertEqual(r.status_code, 401)

    def test_match_disabled_returns_404(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=False):
            r = self.client.post("/api/hiremind/match", json={"jobs": []}, headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_match_without_profile_returns_409(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.post("/api/hiremind/match", json={"jobs": []}, headers=headers)
        self.assertEqual(r.status_code, 409)

    def test_match_returns_scored_sorted_jobs(self):
        headers = self._user_headers()
        self._save_profile()
        jobs = [
            {"title": "Barista", "location": ""},
            {"title": "Backend Engineer", "location": "Remote", "tags": ["Python"], "yoe_bucket": "4-7"},
        ]
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.post("/api/hiremind/match", json={"jobs": jobs}, headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["email"], "mary@example.com")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["jobs"][0]["match"]["score"] >= data["jobs"][1]["match"]["score"], True)
        self.assertIn("reasons", data["jobs"][0]["match"])
        self.assertEqual(data["jobs"][0]["title"], "Backend Engineer")

    def test_match_min_score_filters_and_validates_body(self):
        headers = self._user_headers()
        self._save_profile()
        jobs = [{"title": "Barista", "location": ""}]
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.post("/api/hiremind/match",
                                 json={"jobs": jobs, "min_score": 80}, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            bad = self.client.post("/api/hiremind/match",
                                   json={"jobs": jobs, "min_score": 120}, headers=headers)
        self.assertIn(bad.status_code, (400, 422))


if __name__ == "__main__":
    unittest.main()