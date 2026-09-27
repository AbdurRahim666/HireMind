import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402
from api.routes import landing  # noqa: E402


def _add_visit(visit_id, country, code="", ip="1.2.3.4"):
    with db._write_lock:
        with db._get_conn() as (conn, cur):
            cur.execute(
                """INSERT INTO visits (visit_id, ip_address, device_type, path, country, country_code, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (visit_id, ip, "desktop", "/", country, code, db._now()),
            )
            conn.commit()


class CountriesUsedTestCase(unittest.TestCase):
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
                cur.execute("DELETE FROM visits")
                conn.commit()
        landing._countries_cache = {}

    def test_migration_has_country_code_column(self):
        with db._get_conn() as (conn, cur):
            cur.execute("PRAGMA table_info(visits)")
            cols = {r[1] for r in cur.fetchall()}
        self.assertIn("country_code", cols)

    def test_resolve_ip_returns_country_code(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {"status": "success", "country": "India", "countryCode": "IN",
                        "city": "Mumbai", "regionName": "Maharashtra"}

        class _F:
            @staticmethod
            def get(*a, **k):
                return FakeResp()

        orig = db._requests
        db._requests = _F
        db._ip_geo_cache.clear()
        try:
            loc = db._resolve_ip_sync("1.2.3.4")
        finally:
            db._requests = orig
        self.assertEqual(loc["country"], "India")
        self.assertEqual(loc["country_code"], "in")

    def test_countries_used_groups_filters_orders(self):
        _add_visit("v1", "Canada", code="ca")
        _add_visit("v2", "Canada", code="ca")
        _add_visit("v3", "India", code="")          # no code -> name map fallback
        _add_visit("v4", "Local")                    # loopback junk -> excluded
        _add_visit("v5", "")                         # empty -> excluded

        data = landing.countries_used()
        countries = data["countries"]

        self.assertEqual(len(countries), 2)
        self.assertEqual(countries[0]["country"], "Canada")
        self.assertEqual(countries[0]["visits"], 2)
        self.assertEqual(countries[0]["code"], "ca")
        self.assertEqual(countries[1]["country"], "India")
        self.assertEqual(countries[1]["code"], "in")
        self.assertEqual(countries[1]["visits"], 1)

    def test_countries_used_serves_cache(self):
        _add_visit("v1", "Canada", code="ca")
        first = landing.countries_used()
        _add_visit("v2", "Germany", code="de")
        second = landing.countries_used()  # cached -> same as first
        self.assertEqual(first["countries"], second["countries"])
        self.assertEqual(len(second["countries"]), 1)

    def test_endpoint_is_public_without_auth(self):
        from fastapi.testclient import TestClient
        from api.main import app

        _add_visit("p1", "Canada", code="ca")
        with TestClient(app) as c:
            r = c.get("/api/countries-used")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["countries"][0]["country"], "Canada")

    def test_backfill_visit_country_codes(self):
        _add_visit("b1", "Canada")
        _add_visit("b2", "Unknown Place")
        n = db.backfill_visit_country_codes()
        self.assertEqual(n, 1)
        with db._get_conn() as (conn, cur):
            cur.execute("SELECT country_code FROM visits WHERE visit_id = 'b1'")
            self.assertEqual(cur.fetchone()[0], "ca")
            cur.execute("SELECT country_code FROM visits WHERE visit_id = 'b2'")
            self.assertEqual(cur.fetchone()[0], "")


if __name__ == "__main__":
    unittest.main()