import sqlite3
import unittest
from datetime import datetime

import application_history as history
import applications


NOW = datetime(2026, 10, 30, 12, tzinfo=applications.MOSCOW)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        history.setup(self.db)

    def tearDown(self):
        self.db.close()

    def test_import_deduplicates_and_preserves_own_attempt(self):
        applications.import_history(self.db, [{"vacancy_id": "123", "status": "uncertain"}])
        page = {"topicList": [{"vacancyId": 123, "initialTopicType": "RESPONSE_BY_APPLICANT", "creationTime": NOW.isoformat()},
                              {"vacancyId": 124, "initialTopicType": "RESPONSE_BY_APPLICANT", "creationTime": NOW.isoformat()}]}
        for _ in range(2):
            history.import_pages(self.db, [page], checked_at=NOW, current_complete=True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT status FROM application_attempts WHERE vacancy_id='123'").fetchone()[0], "uncertain")
        self.assertEqual(self.db.execute("SELECT archive_complete FROM history_checks LIMIT 1").fetchone()[0], 0)
        self.assertFalse(applications.reserve_attempt(self.db, vacancy_id="124", resume_id="other", run_id=1, letter="letter", template_id="x", template_version="1", now=NOW))

    def test_malformed_page_is_rejected_without_partial_import(self):
        with self.assertRaises(ValueError):
            history.import_pages(self.db, [{"topicList": [{"vacancyId": 1}, {}]}], checked_at=NOW, current_complete=True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0], 0)

    def test_queue_deduplicates_and_does_not_reopen_completed(self):
        args = dict(vacancy_id="123", url="https://hh.ru/vacancy/123", title="Role", reason="questions", seen_at=NOW, profile="ai")
        history.queue_manual(self.db, **args)
        history.queue_manual(self.db, **args)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM manual_applications").fetchone()[0], 1)
        self.assertIn("https://hh.ru/vacancy/123", history.manual_report(self.db))
        self.db.execute("UPDATE manual_applications SET status='done'")
        self.db.commit()
        history.queue_manual(self.db, **args)
        self.assertNotIn("https://hh.ru/vacancy/123", history.manual_report(self.db))

    def test_manual_case_disappears_after_hh_reports_application(self):
        history.queue_manual(self.db, vacancy_id="123", url="https://hh.ru/vacancy/123", title="Role", reason="questions", seen_at=NOW)
        history.import_pages(self.db, [{"topicList": [{"vacancyId": 123}]}], checked_at=NOW, current_complete=True)
        self.assertNotIn("https://hh.ru/vacancy/123", history.manual_report(self.db))

    def test_incoming_invitation_does_not_spend_limit(self):
        page = {"topicList": [{"vacancyId": 123, "initialTopicType": "INVITATION", "creationTime": NOW.isoformat()}]}
        history.import_pages(self.db, [page], checked_at=NOW, current_complete=False)
        self.assertIsNone(self.db.execute("SELECT attempted_at FROM application_attempts").fetchone()[0])

    def test_pagination_and_wrong_filter(self):
        pages = [{"filterInUse": "active", "total": 2, "pageCount": "2", "topicList": [{"id": i}]} for i in (1, 2)]
        self.assertTrue(history.read_pages(lambda n: pages[n])[1])
        pages[1]["topicList"] = pages[0]["topicList"]
        self.assertFalse(history.read_pages(lambda n: pages[n])[1])
        pages[0]["filterInUse"] = "active"
        self.assertFalse(history.read_pages(lambda n: pages[n], expected_filter="archived")[1])


if __name__ == "__main__":
    unittest.main()
