import html
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from scan import MOSCOW, StopScan, normalize, parse_search, scan


def search_page(resume, ids, next_page=False):
    result = {"vacancySearchResult": {"proxiedSearchFormParams": {"resume": resume}, "vacancies": [{"vacancyId": i, "name": "Role", "publicationTime": {"$": "2026-10-29T09:00:00+03:00"}, "compensation": {"to": 200000, "currencyCode": "RUR"}} for i in ids], "paging": {"next": {"disabled": not next_page}}, "totalResults": len(ids)}}
    return '<template id="HH-Lux-InitialState">' + html.escape(json.dumps(result)) + '</template>'


class ScanTests(unittest.TestCase):
    def test_date_and_salary_boundaries(self):
        item = {"vacancyId": 1, "publicationTime": {"$": "2026-10-28T21:30:00+00:00"}, "compensation": {"currencyCode": "RUR", "to": 199999}}
        result = normalize(item, date(2026, 10, 29))
        self.assertEqual((result["date_status"], result["salary_status"]), ("today", "below_200000"))
        item["compensation"]["to"] = 200000
        self.assertEqual(normalize(item, date(2026, 10, 29))["salary_status"], "pass")
        item["compensation"]["currencyCode"] = "USD"
        item["compensation"]["to"] = 1
        self.assertEqual(normalize(item, date(2026, 10, 29))["salary_status"], "pass")
        item["publicationTime"] = {}
        self.assertEqual(normalize(item, date(2026, 10, 29))["date_status"], "unknown")

    def test_search_requires_resume_and_pagination(self):
        page = search_page("a" * 38, [123], True)
        rows, has_next, _ = parse_search(page, "a" * 38)
        self.assertEqual(rows[0]["vacancyId"], 123)
        self.assertTrue(has_next)
        with self.assertRaises(StopScan):
            parse_search(page, "b" * 38)

    def test_partial_run_preserves_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Path(tmp)
            profiles = {"first": "a" * 38, "second": "b" * 38, "third": "c" * 38}
            with patch("scan.fetch", side_effect=[search_page("a" * 38, [123]), StopScan("hh HTTP 403")]):
                result = scan(profiles, "", paths / "test.db", paths / "report.md", pause=0)
            self.assertEqual(result, 1)
            self.assertIn("hh HTTP 403", (paths / "report.md").read_text(encoding="utf-8"))
            import sqlite3
            db = sqlite3.connect(paths / "test.db")
            try:
                self.assertEqual(db.execute("SELECT status FROM runs").fetchone()[0], "partial")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
            finally:
                db.close()

    def test_repeated_vacancy_is_checked_once_and_keeps_all_profiles(self):
        profiles = {"first": "a" * 38, "second": "b" * 38, "third": "c" * 38}
        today = datetime.now(MOSCOW).date().isoformat()

        def fake_fetch(url, cookie):
            if "/search/vacancy?" in url:
                from urllib.parse import parse_qs, urlsplit
                resume = parse_qs(urlsplit(url).query)["resume"][0]
                return search_page(resume, [123]).replace("2026-10-29", today)
            return '<input type="radio" name="task_1">'

        with tempfile.TemporaryDirectory() as tmp:
            paths = Path(tmp)
            with patch("scan.fetch", side_effect=fake_fetch) as fetch:
                result = scan(profiles, "", paths / "test.db", paths / "report.md", pause=0)
            self.assertEqual(result, 0)
            self.assertEqual(fetch.call_count, 4)
            import sqlite3
            db = sqlite3.connect(paths / "test.db")
            try:
                rows = db.execute("SELECT data FROM observations").fetchall()
                self.assertEqual(len(rows), 1)
                item = json.loads(rows[0][0])
                self.assertEqual(item["profiles"], ["first", "second", "third"])
                self.assertEqual(item["form_status"], "manual")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
