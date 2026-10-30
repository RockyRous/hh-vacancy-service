"""Перенести уже прочитанные данные hh в журнал и сформировать ручной список."""

import argparse
import json
import sqlite3
from datetime import datetime

import application_history
import applications
from scan import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--done", help="отметить ручной случай обработанным по ID вакансии")
    args = parser.parse_args()
    probe = json.loads((ROOT / "data/stage1-probe.json").read_text(encoding="utf-8"))
    profiles = json.loads((ROOT / "data/profiles.json").read_text(encoding="utf-8-sig"))
    known = json.loads((ROOT / "data/known-applications.json").read_text(encoding="utf-8-sig"))
    checked_at = datetime.fromisoformat(probe["at"])
    with sqlite3.connect(ROOT / "data/vacancies.sqlite3", timeout=10) as db:
        application_history.setup(db)
        applications.import_history(db, known)
        count = application_history.import_pages(db, probe["current_history_pages"], checked_at=checked_at,
            current_complete=probe["current_history_pages_complete"], archive_complete=probe["archived_history_complete"])
        for candidate in probe["candidates"]:
            item, score = candidate["item"], candidate["score"]
            if item["form_status"] not in ("manual", "unknown"):
                continue
            if db.execute("SELECT 1 FROM application_attempts WHERE vacancy_id=?", (item["id"],)).fetchone():
                continue
            profile = score.get("selected")
            application_history.queue_manual(db, vacancy_id=item["id"], url=item["url"], title=item["name"],
                profile=profile, resume_id=profiles.get(profile), reason=("обнаружены вопросы" if item["form_status"] == "manual" else "наличие вопросов не удалось исключить"), seen_at=checked_at)
        if args.done:
            with db:
                updated = db.execute("UPDATE manual_applications SET status='done' WHERE vacancy_id=?", (args.done,)).rowcount
            if not updated:
                raise ValueError("вакансия не найдена в ручном списке")
        report = application_history.manual_report(db)
    target = ROOT / "reports/manual-applications.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    print(f"Записей истории обработано: {count}; ручной список: {target}")


if __name__ == "__main__":
    main()
