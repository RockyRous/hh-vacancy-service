import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import applications
import verify_uncertain


class VerificationTests(unittest.TestCase):
    def test_history_and_exact_letter_confirm_without_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.codex/hh-session').mkdir(parents=True)
            (root / 'data').mkdir()
            (root / '.codex/hh-session/session.json').write_text(json.dumps({'cookies': {'fake': 'fake'}}))
            db_path = root / 'db.sqlite'
            with closing(sqlite3.connect(db_path)) as db:
                applications.setup(db)
                db.execute("INSERT INTO application_attempts(vacancy_id,status,letter,attempted_at,source,detail) VALUES (?,?,?,?,?,?)",
                           ('123', 'uncertain', 'Здравствуйте!', '2026-10-30T12:00:00+03:00', 'service', '{}'))
                db.commit()
            topic = {'id': 1, 'vacancyId': 123, 'creationTime': '2026-10-30T12:00:10+03:00', 'chatId': 42}
            history = {'filterInUse': 'active', 'topicList': [topic], 'pageCount': '1', 'total': 1}
            state = {'chatData': {'chat': {'currentParticipantId': 1, 'messages': {'items': [
                {'participantId': 1, 'text': 'Здравствуйте!'}]}}}}
            with patch.object(verify_uncertain.control, 'DEFAULT_DB', db_path), patch.object(verify_uncertain.scan, 'ROOT', root), patch.object(Path, 'home', return_value=root), \
                 patch('verify_uncertain.control.enabled', return_value=True), patch('verify_uncertain.control.enqueue') as notify, \
                 patch('verify_uncertain.cycle_extensions.history_page', return_value=history), \
                 patch('verify_uncertain.HhApplication') as client:
                client.return_value.chat_state.return_value = state['chatData']['chat']
                verify_uncertain.run()
            with closing(sqlite3.connect(db_path)) as db:
                status, detail = db.execute('SELECT status,detail FROM application_attempts').fetchone()
            self.assertEqual(status, 'confirmed')
            self.assertIs(json.loads(detail)['letter_verified'], True)
            notify.assert_called_once()

    def test_checked_absence_keeps_vacancy_blocked_without_claiming_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.codex/hh-session').mkdir(parents=True)
            (root / 'data').mkdir()
            (root / '.codex/hh-session/session.json').write_text(json.dumps({'cookies': {'fake': 'fake'}}))
            db_path = root / 'db.sqlite'
            with closing(sqlite3.connect(db_path)) as db:
                applications.setup(db)
                db.execute("INSERT INTO application_attempts(vacancy_id,status,letter,attempted_at,source,detail) VALUES (?,?,?,?,?,?)",
                           ('123', 'uncertain', 'text', '2026-10-30T12:00:00+03:00', 'service', '{}'))
                db.commit()
            def history(cookie, kind, number):
                return {'filterInUse': kind, 'topicList': [], 'pageCount': '1', 'total': 0}
            with patch.object(verify_uncertain.control, 'DEFAULT_DB', db_path), patch.object(verify_uncertain.scan, 'ROOT', root), \
                 patch.object(Path, 'home', return_value=root), patch('verify_uncertain.control.enabled', return_value=True), \
                 patch('verify_uncertain.control.enqueue'), patch('verify_uncertain.cycle_extensions.history_page', side_effect=history):
                verify_uncertain.run()
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute('SELECT status FROM application_attempts').fetchone()[0], 'uncertain_reviewed')
                self.assertIsNotNone(db.execute("SELECT 1 FROM application_attempts WHERE vacancy_id='123'").fetchone())


if __name__ == '__main__':
    unittest.main()
