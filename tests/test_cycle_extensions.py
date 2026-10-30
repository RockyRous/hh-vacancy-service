import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import application_history
import cycle_extensions as ext
import scan


NOW = datetime(2026, 10, 30, 12, tzinfo=scan.MOSCOW)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        application_history.setup(self.db)
        self.db.execute("CREATE TABLE notifications(id INTEGER PRIMARY KEY, text TEXT, next_attempt REAL)")
        self.db.execute("CREATE TABLE observations(run_id INTEGER, vacancy_id TEXT, data TEXT)")
        self.db.execute("CREATE TABLE scores(run_id INTEGER, vacancy_id TEXT, result TEXT)")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_history_baseline_then_change_is_once_and_persistent(self):
        def page(state, count):
            return {"topicList": [{"id": 77, "vacancyId": 123, "lastState": state, "conversationMessagesCount": count, "chatId": 42}]}
        self.assertEqual(ext.record_history(self.db, [page("RESPONSE", 1)], NOW), [])
        changes = ext.record_history(self.db, [page("INTERVIEW", 2)], NOW + timedelta(hours=1))
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["new_messages"])
        self.assertEqual(ext.record_history(self.db, [page("INTERVIEW", 2)], NOW + timedelta(hours=2)), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM history_events WHERE notified=0").fetchone()[0], 1)

    def test_manual_queue_only_good_current_nonduplicates(self):
        for vid, score, form in (("1", False, "unknown"), ("2", True, "manual"), ("3", False, "manual")):
            item = {"id": vid, "name": "Role", "url": "https://hh.ru/vacancy/" + vid,
                    "date_status": "today", "salary_status": "pass", "form_status": form}
            result = {"status": "scored", "below_threshold": score, "selected": "backend"}
            self.db.execute("INSERT INTO observations VALUES (?,?,?)", (1, vid, json.dumps(item)))
            self.db.execute("INSERT INTO scores VALUES (?,?,?)", (1, vid, json.dumps(result)))
        self.db.execute("INSERT INTO application_attempts(vacancy_id,status,source) VALUES ('3','confirmed','hh_history')")
        self.db.commit()
        self.assertEqual(ext.queue_candidates(self.db, 1, {"backend": "resume"}, NOW), 1)
        self.assertEqual(self.db.execute("SELECT vacancy_id FROM manual_applications").fetchall(), [("1",)])

    def test_incomplete_history_never_imports(self):
        page = {"filterInUse": "active", "total": 2, "pageCount": "1", "topicList": [{"id": 1, "vacancyId": 1}]}
        with self.assertRaises(scan.StopScan):
            ext.refresh_history(self.db, "", fetch_page=lambda *args: page, delay=0, now=NOW)
        self.assertEqual(self.db.execute("SELECT count(*) FROM application_attempts").fetchone()[0], 0)

    def test_new_employer_message_is_stored_and_notified_once(self):
        def page(count):
            return {"topicList": [{"id": 77, "vacancyId": 123, "lastState": "RESPONSE", "conversationMessagesCount": count, "chatId": 42}]}
        ext.record_history(self.db, [page(1)], NOW)
        ext.record_history(self.db, [page(2)], NOW + timedelta(hours=1))
        state = {"chatData": {"chat": {"currentParticipantId": 1, "messages": {"items": [
            {"id": 5, "participantId": 2, "text": "Приглашаем на встречу", "creationTime": (NOW + timedelta(minutes=5)).isoformat()}]}}}}
        with patch("cycle_extensions.session", return_value={}), patch("cycle_extensions.HhApplication") as client:
            client.return_value.chat_state.return_value = state["chatData"]["chat"]
            ext.report_history_events(self.db, "fake")
            ext.report_history_events(self.db, "fake")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)
        self.assertIn("Приглашаем", self.db.execute("SELECT text FROM notifications").fetchone()[0])
        self.assertEqual(self.db.execute("SELECT count(*) FROM monitored_messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
