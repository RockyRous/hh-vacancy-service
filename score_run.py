"""Прочитать полные вакансии последнего обхода и оценить их без откликов."""

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from scan import MOSCOW, ROOT, STATE, StopScan, fetch
from scoring import salary_upper_from_text, score_vacancy


def vacancy_text(page: str) -> str:
    match = STATE.search(page)
    if not match:
        raise StopScan("vacancy state missing")
    try:
        state = json.loads(html.unescape(match.group(1)))
        view = state["vacancyView"]
        value = ((view.get("vacancyFull") or {}).get("vacancy") or view).get("description")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("vacancy description missing")
        return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value))).strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise StopScan("unrecognized vacancy description") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Оценка вакансий последнего обхода, только чтение")
    parser.add_argument("--db", type=Path, default=ROOT / "data/vacancies.sqlite3")
    parser.add_argument("--session", type=Path, default=Path.home() / ".codex/hh-session/session.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/scored.md")
    parser.add_argument("--limit", type=int, default=0, help="ограничить число вакансий, 0 — все")
    parser.add_argument("--offline", action="store_true", help="переоценить сохранённые описания без запросов к hh")
    args = parser.parse_args()
    if not args.db.exists():
        print("Нет базы обхода: сначала запусти python scan.py", file=sys.stderr)
        return 2
    cookie = ""
    if not args.offline:
        try:
            session = json.loads(args.session.read_text(encoding="utf-8-sig"))
            cookies = session["cookies"]
            if not cookies.get("hhtoken"):
                raise ValueError("session missing login")
            cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        except (OSError, ValueError, KeyError, TypeError):
            print("Не удалось прочитать сессию hh", file=sys.stderr)
            return 2
    db = sqlite3.connect(args.db)
    db.execute("CREATE TABLE IF NOT EXISTS scores (run_id INTEGER NOT NULL, vacancy_id TEXT NOT NULL, fetched_at TEXT NOT NULL, description TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id, vacancy_id))")
    db.execute("CREATE TABLE IF NOT EXISTS score_history (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, vacancy_id TEXT NOT NULL, evaluated_at TEXT NOT NULL, description TEXT NOT NULL, result TEXT NOT NULL)")
    run = db.execute("SELECT id,status,started FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not run or run[1] != "complete":
        print("Последний обход не завершён; сначала выполни новый обход", file=sys.stderr)
        db.close()
        return 2
    if not args.offline and datetime.fromisoformat(run[2]).astimezone(MOSCOW).date() != datetime.now(MOSCOW).date():
        print("Последний обход не за сегодня; сначала выполни новый обход", file=sys.stderr)
        db.close()
        return 2
    run_id = run[0]
    observations = [json.loads(row[0]) for row in db.execute("SELECT data FROM observations WHERE run_id=?", (run_id,))]
    candidates = [v for v in observations if v["date_status"] == "today" and v["salary_status"] == "pass"]
    if args.limit > 0:
        candidates = candidates[:args.limit]
    error = ""
    for i, item in enumerate(candidates, 1):
        try:
            previous = db.execute("SELECT fetched_at,description,result FROM scores WHERE run_id=? AND vacancy_id=?", (run_id, item["id"])).fetchone()
            if args.offline:
                if previous is None:
                    continue
                description = previous[1]
            else:
                description = vacancy_text(fetch(item["url"], cookie))
            result = score_vacancy(item["name"], description)
            result["salary_from_description"] = salary_upper_from_text(description, item.get("salary") or {})
            now = datetime.now(MOSCOW).isoformat()
            if previous:
                db.execute("INSERT INTO score_history(run_id,vacancy_id,evaluated_at,description,result) VALUES (?,?,?,?,?)", (run_id, item["id"], *previous))
            db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?)", (run_id, item["id"], now, description, json.dumps(result, ensure_ascii=False)))
            db.commit()
            print(f"Карточки {i}/{len(candidates)}: {item['id']}", flush=True)
            if not args.offline:
                time.sleep(0.5)
        except (StopScan, OSError) as exc:
            error = str(exc)
            break
    wanted = {v["id"] for v in candidates}
    scored = {vid: json.loads(data) for vid, data in db.execute("SELECT vacancy_id,result FROM scores WHERE run_id=?", (run_id,)) if vid in wanted}
    db.close()
    lines = [f"# Предварительные оценки — обход {run_id}", "", f"Обработано карточек: {len(scored)} из {len(candidates)}." + (f" Прервано: {error}." if error else ""), "Оценка 0–100 и порог 50 предварительные. Наличие слова в вакансии не доказывает опыта, оценка не является вероятностью собеседования.", ""]
    for item in candidates:
        result = scored.get(item["id"])
        if not result:
            lines.append(f"- {item['name']} — {item['url']}: не оценена")
            continue
        scores = result.get("scores", {})
        def explain(profile, value):
            role = ", ".join(value["role_matches"]) or "название не совпало"
            skills = ", ".join(value["skills"]) or "не найдены"
            constraints = []
            if value.get("title_mismatch"):
                constraints.append("другое направление")
            if value.get("no_python_signal"):
                constraints.append("Python не указан")
            if value.get("ml_without_llm_signal"):
                constraints.append("в ML-роли не найдены признаки LLM или агентов")
            if value.get("other_stack_without_python"):
                constraints.append("указан другой стек без Python")
            suffix = "; " + ", ".join(constraints) if constraints else ""
            return f"{profile}: {value['score']} (роль: {role}; навыки: {skills}{suffix})"
        values = " | ".join(explain(p, x) for p, x in scores.items())
        best = result.get("selected")
        status = "ниже порога" if result.get("below_threshold") else f"рекомендуется {best}"
        text_salary = result.get("salary_from_description")
        if text_salary and text_salary["below_threshold"]:
            status = "исключена по зарплате из описания"
        salary_note = f" | зарплата в тексте: {text_salary['excerpt']}" + (" — ниже 200 000 RUB" if text_salary['below_threshold'] else "") if text_salary else ""
        lines.append(f"- {item['name']} — {item['company']} | {item['url']} | {values} | {status}{salary_note} | форма: {item['form_status']}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Отчёт: {args.report}; оценено: {len(scored)}", flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
