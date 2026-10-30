import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import application_history
import pilot_applications as pilot
import scan
import scoring


NOW = datetime(2026, 10, 30, 12, tzinfo=scan.MOSCOW)


class PilotTests(unittest.TestCase):
    def test_candidates_are_sent_in_score_order_and_snapshot_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data/letters.json").write_text("[]", encoding="utf8")
            db = sqlite3.connect(":memory:")
            application_history.setup(db)
            db.execute("INSERT INTO history_checks VALUES (?,1,1,2,0)", (NOW.isoformat(),))
            db.execute("CREATE TABLE observations(run_id INTEGER,vacancy_id TEXT,data TEXT,raw TEXT)")
            db.execute("CREATE TABLE scores(run_id INTEGER,vacancy_id TEXT,result TEXT,description TEXT)")
            for vid, mark in ((str(i), 90 if i == 2 else 60) for i in range(1, 23)):
                item = {"id": vid, "name": "Role", "url": "https://hh.ru/vacancy/" + vid,
                        "published": NOW.isoformat(), "form_status": "clear", "date_status": "today", "salary_status": "pass"}
                score = {"selected": "backend", "status": "scored", "below_threshold": False,
                         "version": scoring.VERSION, "scores": {"backend": {"score": mark}}}
                db.execute("INSERT INTO observations VALUES (?,?,?,?)", (1, vid, json.dumps(item), '{}'))
                db.execute("INSERT INTO scores VALUES (?,?,?,?)", (1, vid, json.dumps(score), 'Description'))
            db.commit()
            page = '<template id="HH-Lux-InitialState">' + json.dumps({"applicantResume": {"title": "resume"}}) + '</template>'
            form = '<form name="vacancy_response" method="POST"><input type="hidden" name="_xsrf"></form>'
            with patch.object(scan, "ROOT", root), patch("pilot_applications.control.enabled", return_value=True), \
                 patch("pilot_applications.scan.fetch", side_effect=[form, page] * 20), \
                 patch("pilot_applications.application_sender.send_one_hh", return_value="confirmed") as send:
                result = pilot.run(db, run_id=1, profiles={"backend": "resume"}, cookie="fake", now=NOW, delay_seconds=0)
            self.assertEqual(result[0]["vacancy_id"], "2")
            self.assertEqual(send.call_count, 20)
            self.assertEqual(send.call_args.kwargs["evidence"]["vacancy_snapshot"]["description"], "Description")
            db.close()


if __name__ == "__main__":
    unittest.main()
