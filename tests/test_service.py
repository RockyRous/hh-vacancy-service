import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import control
import scan
import service


def update(n, user, chat, text, chat_type="private"):
    return {"update_id": n, "message": {"from": {"id": user}, "chat": {"id": chat, "type": chat_type}, "text": text}}


class ControlTests(unittest.TestCase):
    def test_commands_are_authorized_persisted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            self.assertFalse(control.enabled(db))
            control.process_update(update(1, 77, 77, "/on"), 1, db)
            self.assertFalse(control.enabled(db))
            control.process_update(update(2, 1, 1, "/on"), 1, db)
            self.assertTrue(control.enabled(db))
            control.process_update(update(2, 1, 1, "/off"), 1, db)
            self.assertTrue(control.enabled(db))
            control.process_update(update(3, 1, 1, "/off"), 1, db)
            self.assertFalse(control.enabled(db))
            with control.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT value FROM control WHERE key='offset'").fetchone()[0], "4")

    def test_off_prevents_hh_request(self):
        with patch("scan.control.enabled", return_value=False), patch("scan.urllib.request.build_opener") as opener:
            with self.assertRaises(scan.StopScan):
                scan.fetch("https://hh.ru/search/vacancy", "")
            opener.assert_not_called()

    def test_off_after_request_blocks_next_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            control.set_enabled(True, db)
            original_enabled = control.enabled

            class Response:
                status = 200
                headers = __import__("email.message", fromlist=["Message"]).Message()
                def geturl(self): return "https://hh.ru/vacancy/1"
                def read(self): return b"ok"
                def __enter__(self): return self
                def __exit__(self, *args): pass

            class Opener:
                def open(self, *args, **kwargs):
                    control.set_enabled(False, db)
                    return Response()

            with patch("scan.control.enabled", side_effect=lambda: original_enabled(db)), patch("scan.urllib.request.build_opener", return_value=Opener()) as build:
                self.assertEqual(scan.fetch("https://hh.ru/vacancy/1", ""), "ok")
                with self.assertRaises(scan.StopScan):
                    scan.fetch("https://hh.ru/vacancy/2", "")
                self.assertEqual(build.call_count, 1)

    def test_lock_rejects_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            with control.single_instance(lock):
                with self.assertRaises(RuntimeError):
                    with control.single_instance(lock):
                        pass

    def test_report_splits_without_breaking_urls_or_unicode_limit(self):
        lines = ["Название " + "😀" * 2500 + " https://hh.ru/vacancy/12345", "Вакансия https://hh.ru/vacancy/777"]
        parts = service.split_report(lines)
        self.assertTrue(all(service.utf16_len(part) <= 4096 for part in parts))
        self.assertIn("https://hh.ru/vacancy/12345", "\n".join(parts))
        self.assertIn("https://hh.ru/vacancy/777", "\n".join(parts))

    def test_schedule_uses_future_slots_without_catchup(self):
        now = datetime(2026, 10, 30, 12, 0, tzinfo=scan.MOSCOW)
        self.assertEqual(service.next_slot(now).hour, 15)
        now = now.replace(hour=21, minute=5)
        next_time = service.next_slot(now)
        self.assertEqual((next_time.day, next_time.hour), (31, 9))

    def test_telegram_429_does_not_repeat_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            control.enqueue("report", db)
            original_connect = control.connect
            with patch("service.control.connect", side_effect=lambda: original_connect(db)), patch("service.telegram_call", side_effect=service.TelegramError("Telegram HTTP 429", 120)):
                service.deliver_one("fake", 1)
            with control.connect(db) as conn:
                row = conn.execute("SELECT attempts,next_attempt FROM notifications").fetchone()
                self.assertEqual(row[0], 1)
                self.assertGreater(row[1], __import__("time").time() + 110)

    def test_below_threshold_notification_is_queued_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            with control.connect(db) as conn:
                conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, status TEXT, error TEXT)")
                conn.execute("CREATE TABLE observations(run_id INTEGER, vacancy_id TEXT, data TEXT, raw TEXT)")
                conn.execute("CREATE TABLE scores(run_id INTEGER, vacancy_id TEXT, fetched_at TEXT, description TEXT, result TEXT)")
                item = {"id": "123", "name": "Role", "url": "https://hh.ru/vacancy/123", "date_status": "today", "salary_status": "pass", "form_status": "unknown"}
                result = {"below_threshold": True, "selected": "backend", "scores": {"backend": {"score": 20, "role_matches": [], "skills": []}}}
                for run_id in (1, 2):
                    conn.execute("INSERT INTO runs VALUES (?, 'complete', NULL)", (run_id,))
                    conn.execute("INSERT INTO observations VALUES (?, '123', ?, '{}')", (run_id, json.dumps(item)))
                    conn.execute("INSERT INTO scores VALUES (?, '123', '', '', ?)", (run_id, json.dumps(result)))
            original_connect = control.connect
            with patch("service.control.connect", side_effect=lambda: original_connect(db)):
                service.queue_cycle_report(1)
                service.queue_cycle_report(2)
            with control.connect(db) as conn:
                messages = [row[0] for row in conn.execute("SELECT text FROM notifications")]
                self.assertEqual(sum("Ниже порога по всем резюме" in msg for msg in messages), 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM reported_below").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
