import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402
from hiremind import normalizer  # noqa: E402
from hiremind.dedup import dedup_tag_jobs, known_jobs_stats, record_seen  # noqa: E402

JOB_A = {
    "title": "Senior Backend Engineer",
    "company": "Acme Tech",
    "location": "Dubai, UAE",
    "url": "https://indeed.example/jobs/1",
    "salary": "AED 5,000 - AED 8,000/month",
    "job_board": "indeed",
}


class TestNormalizeSalary(unittest.TestCase):
    def test_monthly_range_aed(self):
        s = normalizer.normalize_salary("AED 5,000 - AED 8,000/month")
        self.assertTrue(s["eligible"])
        self.assertEqual((s["min"], s["max"]), (5000, 8000))
        self.assertEqual(s["currency"], "AED")
        self.assertEqual(s["per"], "month")
        self.assertEqual(s["semantics"], "range")

    def test_range_second_amount_currencyless(self):
        s = normalizer.normalize_salary("AED 5,000 - 8,000 per month")
        self.assertEqual((s["min"], s["max"]), (5000, 8000))
        self.assertEqual(s["currency"], "AED")
        self.assertEqual(s["per"], "month")

    def test_k_multiplier_year(self):
        s = normalizer.normalize_salary("$60k - $80k per year")
        self.assertEqual((s["min"], s["max"]), (60000, 80000))
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["per"], "year")

    def test_eur_single(self):
        s = normalizer.normalize_salary("€45k/yr")
        self.assertEqual((s["min"], s["max"]), (45000, 45000))
        self.assertEqual(s["currency"], "EUR")
        self.assertEqual(s["semantics"], "exact")

    def test_inr_lakh(self):
        s = normalizer.normalize_salary("₹12,00,000 - ₹18,00,000 PA")
        self.assertEqual((s["min"], s["max"]), (1200000, 1800000))
        self.assertEqual(s["currency"], "INR")
        self.assertEqual(s["per"], "year")

    def test_currency_after_numbers(self):
        s = normalizer.normalize_salary("3000-5000 EGP/month")
        self.assertEqual((s["min"], s["max"]), (3000, 5000))
        self.assertEqual(s["currency"], "EGP")
        self.assertEqual(s["per"], "month")

    def test_hourly(self):
        s = normalizer.normalize_salary("$25 - $35/hour")
        self.assertEqual((s["min"], s["max"]), (25, 35))
        self.assertEqual(s["per"], "hour")

    def test_from_min(self):
        s = normalizer.normalize_salary("From AED 4,000/month")
        self.assertEqual(s["min"], 4000)
        self.assertIsNone(s["max"])
        self.assertEqual(s["semantics"], "min")

    def test_up_to_max(self):
        s = normalizer.normalize_salary("Up to AED 120,000/yr")
        self.assertEqual(s["max"], 120000)
        self.assertIsNone(s["min"])
        self.assertEqual(s["semantics"], "max")

    def test_numeric_input(self):
        s = normalizer.normalize_salary(55000)
        self.assertTrue(s["eligible"])
        self.assertEqual((s["min"], s["max"]), (55000, 55000))

    def test_unparseable_is_none(self):
        for raw in (None, "", "Competitive", "DOE", "Negotiable"):
            self.assertIsNone(normalizer.normalize_salary(raw))

    def test_yearly_annualized(self):
        s = normalizer.normalize_salary("AED 5,000/month")
        self.assertEqual(s["yearly"]["min"], 60000)

    def test_usd_annual_converted(self):
        s = normalizer.normalize_salary("AED 5,000/month")
        self.assertIsNotNone(s["usd_annual"])
        self.assertAlmostEqual(s["usd_annual"]["min"], 60000 * normalizer._PPP_USD["AED"], places=2)

    def test_usd_annual_unknown_currency_is_none(self):
        s = normalizer.normalize_salary("5000 - 8000 per month")
        self.assertIsNone(s["usd_annual"])

    def test_usd_annual_preserves_open_bound(self):
        s = normalizer.normalize_salary("From AED 4,000/month")
        self.assertEqual(s["usd_annual"]["min"], 4000 * 12 * normalizer._PPP_USD["AED"])
        self.assertIsNone(s["usd_annual"]["max"])

    def test_numeric_input_yearly_shape(self):
        s = normalizer.normalize_salary(55000)
        self.assertEqual(s["yearly"], {"min": 55000, "max": 55000})
        self.assertIsNone(s["usd_annual"])


class TestBand(unittest.TestCase):
    def test_two_significant_figures(self):
        self.assertEqual(normalizer._band(27230), 27000)
        self.assertEqual(normalizer._band(27499), 27000)
        self.assertEqual(normalizer._band(27500), 27000)

    def test_small_values_unbanded(self):
        self.assertEqual(normalizer._band(9), 9)
        self.assertEqual(normalizer._band(99), 99)

    def test_non_positive_and_junk(self):
        self.assertEqual(normalizer._band(0), 0)
        self.assertEqual(normalizer._band(-5), 0)
        self.assertEqual(normalizer._band(None), 0)
        self.assertEqual(normalizer._band("abc"), 0)


class TestNormalizeExperience(unittest.TestCase):
    def test_plus_min(self):
        self.assertEqual(normalizer.normalize_experience("3+ years"),
                         {"min_years": 3, "max_years": None, "semantics": "min"})

    def test_range(self):
        self.assertEqual(normalizer.normalize_experience("5-8 years"),
                         {"min_years": 5, "max_years": 8, "semantics": "range"})

    def test_to_range(self):
        self.assertEqual(normalizer.normalize_experience("2 to 4 years of experience"),
                         {"min_years": 2, "max_years": 4, "semantics": "range"})

    def test_minimum_word(self):
        self.assertEqual(normalizer.normalize_experience("Minimum 5 years"),
                         {"min_years": 5, "max_years": None, "semantics": "min"})

    def test_at_least(self):
        self.assertEqual(normalizer.normalize_experience("At least 7 years"),
                         {"min_years": 7, "max_years": None, "semantics": "min"})

    def test_exact(self):
        self.assertEqual(normalizer.normalize_experience("exactly 4 years of experience"),
                         {"min_years": 4, "max_years": 4, "semantics": "exact"})

    def test_empty(self):
        for raw in (None, "", "fresher"):
            self.assertIsNone(normalizer.normalize_experience(raw))


class TestNormalizeLocation(unittest.TestCase):
    def test_dubai_uae(self):
        loc = normalizer.normalize_location("Dubai, UAE")
        self.assertEqual(loc["city"], "Dubai")
        self.assertEqual(loc["country"], "UAE")
        self.assertFalse(loc["is_remote"])

    def test_hybrid_decorated(self):
        loc = normalizer.normalize_location("Hybrid • Dubai, United Arab Emirates • Full-time")
        self.assertEqual(loc["city"], "Dubai")
        self.assertEqual(loc["country"], "UAE")
        self.assertFalse(loc["is_remote"])

    def test_pure_remote(self):
        loc = normalizer.normalize_location("Remote")
        self.assertTrue(loc["is_remote"])
        self.assertEqual(loc["city"], "")

    def test_remote_country(self):
        loc = normalizer.normalize_location("Remote - UAE")
        self.assertTrue(loc["is_remote"])
        self.assertEqual(loc["country"], "UAE")

    def test_city_state_country(self):
        loc = normalizer.normalize_location("Bengaluru, Karnataka, India")
        self.assertEqual(loc["city"], "Bengaluru")
        self.assertEqual(loc["region"], "Karnataka")
        self.assertEqual(loc["country"], "India")

    def test_blank(self):
        loc = normalizer.normalize_location(None)
        self.assertEqual(loc, {"city": "", "region": "", "country": "", "is_remote": False})


class TestJobFingerprint(unittest.TestCase):
    def test_deterministic(self):
        fp1 = normalizer.job_fingerprint(JOB_A)
        fp2 = normalizer.job_fingerprint(dict(JOB_A))
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

    def test_different_salary_differs(self):
        a = dict(JOB_A)
        b = dict(JOB_A)
        b["salary"] = "AED 6,000 - AED 9,000/month"
        self.assertNotEqual(normalizer.job_fingerprint(a), normalizer.job_fingerprint(b))

    def test_different_title_differs(self):
        b = dict(JOB_A, title="Junior Backend Engineer")
        self.assertNotEqual(normalizer.job_fingerprint(JOB_A), normalizer.job_fingerprint(b))

    def test_company_suffix_insensitive(self):
        base = normalizer.job_fingerprint(dict(JOB_A, company="Acme Tech"))
        suffixed = normalizer.job_fingerprint(dict(JOB_A, company="Acme Tech Ltd."))
        self.assertEqual(base, suffixed)

    def test_location_variants_same(self):
        a = normalizer.job_fingerprint(dict(JOB_A, location="Dubai, UAE"))
        b = normalizer.job_fingerprint(dict(JOB_A, location="Dubai • United Arab Emirates • Full-time"))
        self.assertEqual(a, b)

    def test_missing_salary_uses_none_key(self):
        a = normalizer.job_fingerprint(dict(JOB_A, salary=""))
        b = normalizer.job_fingerprint(dict(JOB_A, salary=None))
        self.assertEqual(a, b)

    def test_cross_currency_same_ad_collapses(self):
        """AED 6,000/mo and $2,700/mo are the same real budget, so they must
        land on one fingerprint key."""
        aed = normalizer.job_fingerprint(dict(JOB_A, salary="AED 6,000/month"))
        usd = normalizer.job_fingerprint(dict(JOB_A, salary="$2,700/month"))
        self.assertEqual(aed, usd)

    def test_cross_currency_materially_different_splits(self):
        aed = normalizer.job_fingerprint(dict(JOB_A, salary="AED 6,000/month"))
        usd = normalizer.job_fingerprint(dict(JOB_A, salary="$3,000/month"))
        self.assertNotEqual(aed, usd)

    def test_unknown_currency_falls_back_to_raw_key(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A, salary="5000 - 8000 per month"))
        self.assertTrue(parts["salary"].startswith("raw:"))

    def test_usd_key_is_namespaced(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A, salary="AED 6,000/month"))
        self.assertTrue(parts["salary"].startswith("usd:"))

    def test_gbp_matches_equivalent_usd(self):
        gbp = normalizer.job_fingerprint(dict(JOB_A, salary="GBP 2,800/month"))
        usd = normalizer.job_fingerprint(dict(JOB_A, salary="$4,100/month"))
        self.assertEqual(gbp, usd)


class TestExperienceInFingerprint(unittest.TestCase):
    def test_board_level_tag_used(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A, experience_level="senior"))
        self.assertEqual(parts["experience"], "lvl:senior")

    def test_parsed_years_used_when_no_tag(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A, description="We need 5-8 years of experience"))
        self.assertEqual(parts["experience"], "yrs:5")

    def test_explicit_experience_field_used(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A, experience="3+ years"))
        self.assertEqual(parts["experience"], "yrs:3")

    def test_unknown_is_empty_segment(self):
        parts = normalizer.fingerprint_parts(dict(JOB_A))
        self.assertEqual(parts["experience"], "")

    def test_different_experience_splits_fingerprint(self):
        base = dict(JOB_A, salary="")
        a = normalizer.job_fingerprint(dict(base, experience="3+ years"))
        b = normalizer.job_fingerprint(dict(base, experience="8+ years"))
        self.assertNotEqual(a, b)

    def test_unknown_does_not_collide_with_stated(self):
        base = dict(JOB_A, salary="")
        a = normalizer.job_fingerprint(dict(base))
        b = normalizer.job_fingerprint(dict(base, experience="3+ years"))
        self.assertNotEqual(a, b)

    def test_enrich_job_adds_keys(self):
        job = dict(JOB_A, salary="AED 5,000/month")
        out = normalizer.enrich_job(job)
        self.assertIs(out, job)
        self.assertEqual(job["fingerprint"], normalizer.job_fingerprint(job))
        self.assertIn("AED", job["salary_norm"])


class TestCrossCurrencyEquivalence(unittest.TestCase):
    """The banded USD key exists so the same ad on two boards collapses even
    when the boards quote different currencies."""

    def test_equivalent_monthly_offers_match(self):
        # ~$3,600/mo PPP on both sides.
        a = normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,000/month"))
        b = normalizer.job_fingerprint(dict(JOB_A, salary="$3,600/month"))
        self.assertEqual(a, b)

    def test_test_names_salaries_are_distinct(self):
        """Guard the fixture: JOB_A's own salary must stay a fixed baseline."""
        self.assertNotEqual(
            normalizer.job_fingerprint(dict(JOB_A, salary="AED 6,000/month")),
            normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,000/month")),
        )

    def test_equivalent_annual_vs_monthly(self):
        annual = normalizer.job_fingerprint(dict(JOB_A, salary="$86,400/year"))
        monthly = normalizer.job_fingerprint(dict(JOB_A, salary="$7,200/month"))
        self.assertEqual(annual, monthly)

    def test_hourly_vs_annual(self):
        hourly = normalizer.job_fingerprint(dict(JOB_A, salary="$50/hour"))
        annual = normalizer.job_fingerprint(dict(JOB_A, salary="$104,000/year"))
        self.assertEqual(hourly, annual)

    def test_band_tolerates_rounding_drift_between_boards(self):
        a = normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,000/month"))
        b = normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,050/month"))
        self.assertEqual(a, b)

    def test_band_still_splits_far_apart_offers(self):
        a = normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,000/month"))
        b = normalizer.job_fingerprint(dict(JOB_A, salary="AED 30,000/month"))
        self.assertNotEqual(a, b)

    def test_exact_value_is_not_bucketed_into_a_neighbour(self):
        a = normalizer.job_fingerprint(dict(JOB_A, salary="AED 8,000/month"))
        b = normalizer.job_fingerprint(dict(JOB_A, salary="AED 9,000/month"))
        self.assertNotEqual(a, b)


class HiremindKnownJobsTestCase(unittest.TestCase):
    """Point db at a throwaway SQLite file created via the real init_db()."""

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
        db.clear_known_jobs()


@patch("hiremind.config.hiremind_enabled", return_value=False)
class TestDedupGate(HiremindKnownJobsTestCase):
    def test_disabled_records_nothing(self, _patched):
        flagged = dedup_tag_jobs([dict(JOB_A)])
        self.assertEqual(flagged, 0)
        self.assertNotIn("fingerprint", JOB_A)
        self.assertFalse(record_seen(dict(JOB_A)))
        with db._get_conn() as (conn, cur):
            cur.execute("SELECT COUNT(*) FROM known_jobs")
            self.assertEqual(cur.fetchone()[0], 0)
        self.assertEqual(known_jobs_stats(), {"enabled": False, "count": 0, "sources": 0})


@patch("hiremind.config.hiremind_enabled", return_value=True)
class TestDedupWorkflow(HiremindKnownJobsTestCase):
    def test_first_seen_new_second_flagged(self, _patched):
        j1 = dict(JOB_A, url="https://indeed.example/jobs/1")
        j2 = dict(JOB_A, url="https://linkedin.example/jobs/1")
        self.assertEqual(dedup_tag_jobs([j1], source="indeed"), 0)
        self.assertIn("fingerprint", j1)
        self.assertFalse(j1["_seen_before"])
        self.assertEqual(dedup_tag_jobs([j2], source="linkedin"), 1)
        self.assertTrue(j2["_seen_before"])
        stats = known_jobs_stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["sources"], 1)

    def test_same_batch_second_duplicate_flagged(self, _patched):
        """The same ad listed twice on one board must still be flagged."""
        j1 = dict(JOB_A, url="https://indeed.example/jobs/1")
        j2 = dict(JOB_A, url="https://indeed.example/jobs/1-mirror")
        self.assertEqual(dedup_tag_jobs([j1, j2], source="indeed"), 1)
        self.assertFalse(j1["_seen_before"])
        self.assertTrue(j2["_seen_before"])

    def test_batch_of_distinct_jobs_flags_none(self, _patched):
        jobs = [dict(JOB_A, title=f"Engineer {i}", salary="") for i in range(4)]
        self.assertEqual(dedup_tag_jobs(jobs, source="indeed"), 0)
        self.assertTrue(all(not j["_seen_before"] for j in jobs))

    def test_same_fingerprint_recorded_once(self, _patched):
        self.assertTrue(db.record_known_job("abc123", title="T", company="C", source="x"))
        self.assertFalse(db.record_known_job("abc123", title="T2", company="C2", source="y"))
        self.assertTrue(db.known_job_seen("abc123"))
        self.assertFalse(db.known_job_seen("nope"))
        self.assertEqual(db.known_jobs_seen(["abc123", "zzz"]), {"abc123"})

    def test_bulk_record(self, _patched):
        rows = [{"fingerprint": f"fp{i}", "title": f"T{i}", "company": "", "url": "", "source": "x"}
                for i in range(3)]
        self.assertEqual(db.record_known_jobs_bulk(rows), 3)
        self.assertEqual(db.known_jobs_seen(["fp0", "fp2"]), {"fp0", "fp2"})

    def test_clear(self, _patched):
        db.record_known_job("fp-clear", source="x")
        self.assertEqual(db.clear_known_jobs(), 1)
        self.assertEqual(known_jobs_stats()["count"], 0)


class TestKnownJobsAdminAPI(HiremindKnownJobsTestCase):
    """The registry is only useful if an operator can inspect and reset it."""

    ADMIN = "owner@example.com"
    REGULAR = "joe@example.com"

    def setUp(self):
        from fastapi.testclient import TestClient
        from api.main import app
        self.client = TestClient(app)
        db.create_user(self.ADMIN, "Owner")
        db.create_user(self.REGULAR, "Joe")
        db.grant_admin(self.ADMIN, "seed")
        db.clear_known_jobs()

    def tearDown(self):
        db.clear_known_jobs()
        with db._get_conn() as (conn, cur):
            cur.execute("DELETE FROM admin_users")
            cur.execute("DELETE FROM users")
            conn.commit()

    def _headers(self, email):
        from utils.jwt import create_token
        return {"Authorization": "Bearer " + create_token(email)}

    def _seed(self):
        db.record_known_job("fp-1", title="T1", source="indeed")
        db.record_known_job("fp-2", title="T2", source="linkedin")

    @patch("hiremind.config.hiremind_enabled", return_value=True)
    def test_admin_reads_stats_with_source_breakdown(self, _flag):
        self._seed()
        r = self.client.get("/api/admin/hiremind/known-jobs", headers=self._headers(self.ADMIN))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["sources"], 2)
        self.assertEqual({s["source"] for s in data["by_source"]}, {"indeed", "linkedin"})
        self.assertEqual(sum(s["count"] for s in data["by_source"]), 2)

    @patch("hiremind.config.hiremind_enabled", return_value=False)
    def test_404_when_flag_off(self, _flag):
        r = self.client.get("/api/admin/hiremind/known-jobs", headers=self._headers(self.ADMIN))
        self.assertEqual(r.status_code, 404)

    @patch("hiremind.config.hiremind_enabled", return_value=True)
    def test_requires_auth(self, _flag):
        r = self.client.get("/api/admin/hiremind/known-jobs")
        self.assertEqual(r.status_code, 401)

    @patch("hiremind.config.hiremind_enabled", return_value=True)
    def test_non_admin_forbidden(self, _flag):
        r = self.client.get("/api/admin/hiremind/known-jobs", headers=self._headers(self.REGULAR))
        self.assertEqual(r.status_code, 403)

    @patch("hiremind.config.hiremind_enabled", return_value=True)
    def test_non_admin_cannot_clear(self, _flag):
        self._seed()
        r = self.client.delete("/api/admin/hiremind/known-jobs",
                               headers=self._headers(self.REGULAR))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.known_jobs_stats()["count"], 2)

    @patch("hiremind.config.hiremind_enabled", return_value=True)
    def test_admin_clears_registry(self, _flag):
        self._seed()
        r = self.client.delete("/api/admin/hiremind/known-jobs",
                               headers=self._headers(self.ADMIN))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cleared"], 2)
        self.assertEqual(db.known_jobs_stats()["count"], 0)

    @patch("hiremind.config.hiremind_enabled", return_value=False)
    def test_clear_404_when_flag_off(self, _flag):
        r = self.client.delete("/api/admin/hiremind/known-jobs",
                               headers=self._headers(self.ADMIN))
        self.assertEqual(r.status_code, 404)


@patch("hiremind.config.hiremind_enabled", return_value=True)
class TestSeenBeforePersistence(HiremindKnownJobsTestCase):
    """`_seen_before` must survive the DB round-trip so the client can show it."""

    def test_flagged_row_round_trips_as_bool(self, _patched):
        first = dict(JOB_A, url="https://indeed.example/jobs/1")
        second = dict(JOB_A, url="https://linkedin.example/jobs/1")
        dedup_tag_jobs([first], source="indeed")
        dedup_tag_jobs([second], source="linkedin")
        db.set_raw_jobs("s-seen", [first, second])
        rows = {r["url"]: r for r in db.get_raw_jobs("s-seen")}
        self.assertFalse(rows["https://indeed.example/jobs/1"]["_seen_before"])
        self.assertTrue(rows["https://linkedin.example/jobs/1"]["_seen_before"])
        self.assertNotIn("seen_before", rows["https://linkedin.example/jobs/1"])

    def test_filtered_jobs_round_trip(self, _patched):
        first = dict(JOB_A, url="https://indeed.example/jobs/1")
        second = dict(JOB_A, url="https://linkedin.example/jobs/1")
        dedup_tag_jobs([first], source="indeed")
        dedup_tag_jobs([second], source="linkedin")
        db.set_filtered_jobs("s-f", [first, second])
        rows = {r["url"]: r for r in db.get_filtered_jobs("s-f")}
        self.assertTrue(rows["https://linkedin.example/jobs/1"]["_seen_before"])

    def test_unflagged_when_flag_off(self, _patched):
        db.set_raw_jobs("s-off", [dict(JOB_A)])
        self.assertFalse(db.get_raw_jobs("s-off")[0]["_seen_before"])


@patch("hiremind.config.hiremind_enabled", return_value=True)
class TestJobsTableEnrichment(HiremindKnownJobsTestCase):
    def test_raw_rows_carry_fingerprint(self, _patched):
        job = dict(JOB_A, salary="AED 5,000 - AED 8,000/month")
        dedup_tag_jobs([job], source="indeed")
        db.set_raw_jobs("s1", [job])
        rows = db.get_raw_jobs("s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint"], job["fingerprint"])
        salary_norm = json.loads(rows[0]["salary_norm"])
        self.assertEqual(salary_norm["min"], 5000)
        self.assertEqual(salary_norm["currency"], "AED")

    def test_rows_blank_when_flag_off(self, _patched):
        db.set_raw_jobs("s2", [dict(JOB_A)])
        rows = db.get_raw_jobs("s2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint"], "")
        self.assertEqual(rows[0]["salary_norm"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)