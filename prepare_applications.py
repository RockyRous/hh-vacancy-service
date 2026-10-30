"""Создать локальный предпросмотр решений и писем без отправки откликов."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import applications
from scan import MOSCOW, ROOT


def main() -> int:
    profiles = json.loads((ROOT / "data/profiles.json").read_text(encoding="utf-8-sig"))
    letters = json.loads((ROOT / "data/letters.json").read_text(encoding="utf-8-sig"))
    history = json.loads((ROOT / "data/known-applications.json").read_text(encoding="utf-8-sig"))
    known = {str(entry["vacancy_id"]): entry for entry in history}
    db_path = ROOT / "data/vacancies.sqlite3"
    if not db_path.exists():
        raise ValueError("нет базы вакансий")
    db = sqlite3.connect(db_path)
    try:
        run = db.execute("SELECT id,started FROM runs WHERE status='complete' ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            raise ValueError("нет завершённого обхода")
        run_id = run[0]
        observations = [json.loads(row[0]) for row in db.execute("SELECT data FROM observations WHERE run_id=?", (run_id,))]
        scores = {vid: json.loads(value) for vid, value in db.execute("SELECT vacancy_id,result FROM scores WHERE run_id=?", (run_id,))}
        table_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_attempts'").fetchone()
        if table_exists:
            for vid, status in db.execute("SELECT vacancy_id,status FROM application_attempts"):
                known[str(vid)] = {"status": status}
    finally:
        db.close()
    today = datetime.now(MOSCOW).date().isoformat()
    candidates = [v for v in observations if v.get("date_status") == "today" and v.get("salary_status") == "pass"]
    plans = [applications.prepare(item, scores.get(item["id"]), profiles, letters, known.get(item["id"]), today) for item in candidates]
    lines = [f"# Предпросмотр откликов — обход {run_id} от {run[1]}", "", "Только локальная подготовка. Отклики не отправлены; письма — черновики. История hh проверена не полностью.", ""]
    for key, heading in (("duplicate", "Уже были отклики или неопределённые попытки"), ("manual", "Требуется ручная проверка формы"), ("blocked", "Ожидают других проверок"), ("skip", "Пропущены по оценке или зарплате")):
        group = [p for p in plans if p["decision"] == key]
        lines += [f"## {heading}: {len(group)}", ""]
        for p in group:
            lines.append(f"- {p['title']} — {p['url']} | резюме: {p['profile'] or 'не определено'} | причины: {', '.join(p['reasons'])}")
            if key in {"manual", "blocked"} and p["letter"]:
                lines.append(f"  Черновик письма ({p['template_id']} v{p['template_version']}, {'согласован' if p['template_approved'] else 'не согласован'}): {p['letter']}")
        lines.append("")
    target = ROOT / "reports/application-plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"Обход {run_id}: {len(plans)} вакансий, предпросмотр: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
