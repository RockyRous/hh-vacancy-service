import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import applications as app


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=app.MOSCOW)


def db_at(path):
    db = sqlite3.connect(path, timeout=5)
    app.setup(db)
    return db


def reserve(db, vacancy, run=1, now=NOW):
    return app.reserve_attempt(db, vacancy_id=str(vacancy), resume_id="resume", run_id=run, letter="Здравствуйте!", template_id="general", template_version="1", now=now)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        app.setup(self.db)

    def tearDown(self):
        self.db.close()

    def test_unknown_or_detected_form_stays_manual(self):
        base = {"id": "123", "url": "https://hh.ru/vacancy/123", "name": "Role", "salary_status": "pass", "published": NOW.isoformat()}
        result = {"status": "scored", "selected": "ai", "scores": {}, "below_threshold": False}
        for status in ("unknown", "manual"):
            plan = app.prepare({**base, "form_status": status}, result, {"ai": "resume"}, [], None, "2026-10-30")
            self.assertEqual(plan["decision"], "manual")
            self.assertIn("нет согласованного письма", plan["reasons"])

    def test_historical_duplicate_blocks_another_resume(self):
        app.import_history(self.db, [{"vacancy_id": "123", "status": "confirmed"}])
        self.assertFalse(reserve(self.db, 123))
        self.assertEqual(app.import_history(self.db, [{"vacancy_id": "123", "status": "uncertain"}]), 0)

    def test_even_clear_form_is_only_a_blocked_draft(self):
        item = {"id": "123", "url": "https://hh.ru/vacancy/123", "name": "Role", "salary_status": "pass", "form_status": "clear", "published": NOW.isoformat()}
        result = {"status": "scored", "selected": "backend", "scores": {}, "below_threshold": False}
        template = {"id": "x", "version": "1", "profiles": ["backend"], "approved": True, "text": "Добрый день!"}
        plan = app.prepare(item, result, {"backend": "resume"}, [template], None, "2026-10-30")
        self.assertEqual(plan["decision"], "blocked")
        self.assertTrue(plan["template_approved"])

    def test_salary_and_low_score_are_skipped(self):
        item = {"id": "123", "url": "https://hh.ru/vacancy/123", "name": "Role", "salary_status": "pass", "form_status": "unknown", "published": NOW.isoformat()}
        result = {"status": "scored", "selected": "backend", "scores": {}, "below_threshold": False, "salary_from_description": {"below_threshold": True}}
        self.assertEqual(app.prepare(item, result, {"backend": "resume"}, [], None, "2026-10-30")["decision"], "skip")

    def test_attempt_is_one_per_vacancy_and_unknown_cannot_retry(self):
        self.assertTrue(reserve(self.db, 123))
        self.assertEqual(app.finish_attempt(self.db, "123", None, None, NOW), ("uncertain", True))
        self.assertFalse(reserve(self.db, 123, run=2))
        with self.assertRaises(ValueError):
            app.finish_attempt(self.db, "123", 200, {"success": True}, NOW)

    def test_interrupted_attempt_is_unknown(self):
        self.assertTrue(reserve(self.db, 123))
        self.assertEqual(app.recover_inflight(self.db), 1)
        self.assertEqual(self.db.execute("SELECT status FROM application_attempts").fetchone()[0], "uncertain")
        self.assertFalse(reserve(self.db, 123))

    def test_response_codes_and_letter_verification(self):
        self.assertEqual(app.classify_response(400, {"error": "test-required"}), ("manual", False))
        self.assertEqual(app.classify_response(403, {"error": "test_required"}), ("manual", True))
        self.assertEqual(app.classify_response(200, {"success": False}), ("uncertain", True))
        self.assertTrue(reserve(self.db, 123))
        self.assertEqual(app.finish_attempt(self.db, "123", 200, {"success": True}, NOW, letter_verified=None), ("confirmed", False))
        self.assertTrue(reserve(self.db, 124))
        self.assertEqual(app.finish_attempt(self.db, "124", 200, {"success": True}, NOW, letter_verified=False), ("uncertain", True))

    def test_per_run_limit_counts_all_attempts(self):
        for vid in range(10, 30):
            self.assertTrue(reserve(self.db, vid))
        self.assertFalse(reserve(self.db, 30))
        self.assertTrue(reserve(self.db, 30, run=2))

    def test_pilot_limits_cannot_be_relaxed(self):
        with self.assertRaises(ValueError):
            app.reserve_attempt(self.db, vacancy_id="123", resume_id="resume", run_id=1, letter="Здравствуйте", template_id="a", template_version="1", now=NOW, max_day=101)

    def test_rolling_day_limit_spans_midnight(self):
        before_midnight = NOW.replace(hour=23, minute=30) - timedelta(days=1)
        for i in range(100):
            self.assertTrue(reserve(self.db, 100 + i, run=100 + i, now=before_midnight))
        self.assertFalse(reserve(self.db, 999, run=999, now=NOW))
        self.assertTrue(reserve(self.db, 999, run=999, now=NOW + timedelta(days=1)))

    def test_known_manual_attempts_count_toward_daily_limit(self):
        app.import_history(self.db, [{"vacancy_id": str(200 + i), "status": "confirmed", "attempted_at": NOW.astimezone(timezone.utc).isoformat()} for i in range(100)])
        self.assertFalse(reserve(self.db, 999, now=NOW))

    def test_two_competing_reservations_allow_only_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.db"
            db = db_at(path)
            db.close()
            barrier = threading.Barrier(2)
            result = []
            def worker():
                conn = sqlite3.connect(path, timeout=5)
                try:
                    barrier.wait()
                    result.append(reserve(conn, 123))
                finally:
                    conn.close()
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sorted(result), [False, True])


if __name__ == "__main__":
    unittest.main()
