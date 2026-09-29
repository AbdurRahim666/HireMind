import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402

CANNED_PROFILE = (
    '{"summary":"Backend engineer","headline":"Python Dev","seniority":"mid","yoe":4.5,'
    '"skills":[{"name":"Python","category":"language","years":4}],'
    '"experience":[{"role":"Backend Engineer","company":"Acme","years":2.0,'
    '"domain":"fintech","highlights":["shipped payment APIs"]}],'
    '"education":[{"degree":"B.Tech","school":"Some University","year":2022}],'
    '"certifications":["AWS SAA"],"languages":["English"],'
    '"target_roles":["Backend Engineer"],"remote_preferred":true,"internship_mode":false,'
    '"keywords":["Django","Postgres"]}'
)

RESUME_TEXT = (
    "## Jane Doe\n"
    "jane@example.com 12345 67890\n"
    "Software Engineer at Acme\n"
    "Built APIs in Django and Postgres."
)


class HiremindDBTestCase(unittest.TestCase):
    """Point db at a throwaway SQLite file created via the real init_db()"""

    @classmethod
    def setUpClass(cls):
        cls._orig_path = os.path.join(os.path.dirname(os.path.abspath(db.__file__)), "job_agent.db")
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
        db._DB_PATH = cls._orig_path

    def tearDown(self):
        with db._get_conn() as (conn, cur):
            cur.execute("DELETE FROM candidate_profiles")
            conn.commit()


class TestCandidateProfileDB(HiremindDBTestCase):
    def test_save_and_get(self):
        db.create_user("a@b.com", "A B")
        profile = {"version": 1, "summary": "s", "skills": []}
        db.save_candidate_profile("a@b.com", "resume.pdf", "parsed text", profile)
        row = db.get_candidate_profile("a@b.com")
        self.assertIsNotNone(row)
        self.assertEqual(row["profile"]["summary"], "s")
        self.assertEqual(row["parsed_text"], "parsed text")
        self.assertEqual(row["resume_filename"], "resume.pdf")

    def test_upsert_refreshes_in_place(self):
        db.create_user("a@b.com", "A B")
        db.save_candidate_profile("a@b.com", "", "old", {"summary": "old"})
        db.save_candidate_profile("a@b.com", "", "new", {"summary": "new"}, version=2)
        row = db.get_candidate_profile("a@b.com")
        self.assertEqual(row["profile"]["summary"], "new")
        self.assertEqual(row["parsed_text"], "new")
        self.assertEqual(row["version"], 2)

    def test_get_missing_email(self):
        self.assertIsNone(db.get_candidate_profile("nobody@example.com"))
        self.assertIsNone(db.get_candidate_profile(""))


class TestProfileBuilder(unittest.TestCase):
    def test_build_profile_cleans_and_versions(self):
        from hiremind import profile_builder as pb

        with patch.object(pb.LLMClient, "keyword_chat", return_value=CANNED_PROFILE) as mock:
            profile = pb.build_profile(RESUME_TEXT, email="jane@example.com")
        self.assertEqual(profile["version"], 1)
        self.assertEqual(profile["summary"], "Backend engineer")
        self.assertEqual(profile["yoe"], 4.5)
        self.assertEqual(profile["skills"][0]["name"], "Python")
        self.assertEqual(profile["target_roles"], ["Backend Engineer"])
        self.assertEqual(profile["internship_mode"], False)
        mock.assert_called_once()

    def test_build_profile_scrubs_pii_before_llm(self):
        from hiremind import profile_builder as pb

        with patch.object(pb.LLMClient, "keyword_chat", return_value=CANNED_PROFILE) as mock:
            pb.build_profile(RESUME_TEXT, email="jane@example.com")
        prompt = mock.call_args.args[0]
        self.assertIn("[email]", prompt)
        self.assertNotIn("jane@example.com", prompt)
        self.assertIn("[phone]", prompt)
        self.assertNotIn("12345 67890", prompt)

    def test_build_profile_garbage_llm_returns_safe_profile(self):
        from hiremind import profile_builder as pb

        with patch.object(pb.LLMClient, "keyword_chat", return_value="not json at all"):
            profile = pb.build_profile("some resume text")
        self.assertEqual(profile["version"], 1)
        self.assertEqual(profile["summary"], "")
        self.assertEqual(profile["skills"], [])
        self.assertEqual(profile["target_roles"], [])
        self.assertEqual(profile["preferred"]["locations"], [])

    def test_build_profile_merges_user_location_and_resume(self):
        from hiremind import profile_builder as pb

        user = {"city": "Mumbai", "state": "Maharashtra", "country": "in", "resume_filename": "r.pdf"}
        with patch.object(pb.LLMClient, "keyword_chat", return_value=CANNED_PROFILE):
            profile = pb.build_profile(RESUME_TEXT, email="jane@example.com", user=user)
        self.assertEqual(profile["preferred"]["locations"],
                         [{"city": "Mumbai", "state": "Maharashtra", "country": "in"}])
        self.assertEqual(profile["preferred"]["remote"], True)
        self.assertEqual(profile["source_resume"], "r.pdf")


class TestHiremindAPI(HiremindDBTestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from utils.rate_limiter import _limits

        self.client = TestClient(app)
        _limits.clear()

    def _user_headers(self):
        db.create_user("beta@example.com", "Beta Tester")
        from utils.jwt import create_token
        return {"Authorization": "Bearer " + create_token("beta@example.com")}

    def test_profile_requires_auth(self):
        r = self.client.get("/api/hiremind/profile")
        self.assertEqual(r.status_code, 401)

    def test_profile_disabled_returns_404_even_when_authed(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=False):
            r = self.client.get("/api/hiremind/profile", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_profile_missing_returns_404(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.get("/api/hiremind/profile", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_refresh_builds_then_get_returns_profile(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True), \
             patch("hiremind.profile_builder.LLMClient.keyword_chat", return_value=CANNED_PROFILE):
            r = self.client.post("/api/hiremind/profile/refresh",
                                 json={"resume_text": RESUME_TEXT}, headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["profile"]["summary"], "Backend engineer")

        r2 = None
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True), \
             patch("hiremind.profile_builder.LLMClient.keyword_chat") as mock_chat:
            r2 = self.client.get("/api/hiremind/profile", headers=headers)
        mock_chat.assert_not_called()
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["profile"]["target_roles"], ["Backend Engineer"])

    def test_refresh_requires_resume_when_none_passed(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=True):
            r = self.client.post("/api/hiremind/profile/refresh", json={}, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_refresh_disabled_returns_404(self):
        headers = self._user_headers()
        with patch("api.routes.hiremind.hiremind_enabled", return_value=False):
            r = self.client.post("/api/hiremind/profile/refresh",
                                 json={"resume_text": RESUME_TEXT}, headers=headers)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()