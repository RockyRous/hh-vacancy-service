import json
import sqlite3
import unittest
from datetime import datetime

import applications
from application_sender import send_one


NOW = datetime(2026, 10, 30, 12, tzinfo=applications.MOSCOW)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        applications.setup(self.db)
        self.calls = []
        self.paused = []
        self.args = dict(
            item={"id": "123", "published": NOW.isoformat(), "form_status": "clear", "salary_status": "pass"},
            score={"selected": "backend", "status": "scored", "below_threshold": False, "version": "1"},
            profiles={"backend": "resume"},
            templates=[{"id": "one", "version": "1", "profiles": ["backend"], "approved": True, "text": "Здравствуйте!"}],
            run_id=1, now=NOW,
            evidence={"history_complete": True, "scoring_validated": True, "resume_snapshot": {"id": "resume"}, "vacancy_snapshot": {"id": "123"}, "form_checked": True},
            enabled=lambda: True, pause=lambda: self.paused.append(True), post=self.post,
        )

    def tearDown(self):
        self.db.close()

    def post(self, *args):
        self.calls.append(args)
        return 200, {"success": True}

    def send(self):
        return send_one(self.db, **self.args)

    def test_success_records_exact_letter_and_snapshot_and_never_repeats(self):
        self.assertEqual(self.send(), "confirmed")
        row = self.db.execute("SELECT letter,decision_snapshot FROM application_attempts").fetchone()
        self.assertEqual(row[0], "Здравствуйте!")
        self.assertEqual(json.loads(row[1])["resume"], {"id": "resume"})
        self.assertEqual(self.send(), "duplicate_or_limit")
        self.assertEqual(len(self.calls), 1)

    def test_missing_evidence_form_or_approval_blocks_without_reservation(self):
        for key in ("history_complete", "scoring_validated", "resume_snapshot", "vacancy_snapshot", "form_checked"):
            value = self.args["evidence"].pop(key)
            self.assertEqual(self.send(), "blocked", key)
            self.args["evidence"][key] = value
        self.args["item"]["form_status"] = "unknown"
        self.assertEqual(self.send(), "blocked")
        self.args["item"]["form_status"] = "clear"
        self.args["templates"][0]["approved"] = False
        self.assertEqual(self.send(), "blocked")
        self.assertFalse(self.calls)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)

    def test_off_between_reservation_and_request_prevents_post(self):
        states = iter([True, False])
        self.args["enabled"] = lambda: next(states)
        self.assertEqual(self.send(), "off")
        self.assertEqual(self.db.execute("SELECT status FROM application_attempts").fetchone()[0], "not_sent")
        self.assertFalse(self.calls)

    def test_timeout_is_uncertain_and_pauses_without_retry(self):
        def timeout(*args):
            self.calls.append(args)
            raise TimeoutError()
        self.args["post"] = timeout
        self.assertEqual(self.send(), "uncertain")
        self.assertEqual(self.send(), "duplicate_or_limit")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.paused), 1)

    def test_form_response_and_mismatched_letter(self):
        self.args["post"] = lambda *args: (400, {"error": "test-required"})
        self.assertEqual(self.send(), "manual")
        self.args["item"]["id"] = "124"
        self.args["post"] = self.post
        self.args["verify"] = lambda *args: False
        self.assertEqual(self.send(), "uncertain")
        self.assertEqual(len(self.paused), 1)

    def test_off_after_post_skips_verification(self):
        states = iter([True, True, False])
        self.args["enabled"] = lambda: next(states)
        self.args["verify"] = lambda *args: self.fail("verification should not run")
        self.assertEqual(self.send(), "confirmed")
        detail = self.db.execute("SELECT detail FROM application_attempts").fetchone()[0]
        self.assertIsNone(json.loads(detail)["letter_verified"])


if __name__ == "__main__":
    unittest.main()
