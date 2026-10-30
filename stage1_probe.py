"""Небольшая живая проверка этапа 1, запускается потоком внутри сервиса."""

import html
import json
import urllib.parse
from datetime import datetime

import control
import scan
import scoring
import score_run
from form_handling import detect_questions_in_html


def run():
    try:
        profiles = json.loads((scan.ROOT / "data/profiles.json").read_text(encoding="utf-8-sig"))
        session = json.loads((__import__("pathlib").Path.home() / ".codex/hh-session/session.json").read_text(encoding="utf-8-sig"))
        cookies = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
        snapshots = {}
        for name, resume_id in profiles.items():
            page = scan.fetch("https://hh.ru/resume/" + resume_id, cookies)
            match = scan.STATE.search(page)
            state = json.loads(html.unescape(match.group(1))) if match else {}
            resume = state.get("applicantResume")
            if not resume:
                raise ValueError("resume snapshot unavailable")
            snapshots[name] = resume
        history_pages = []
        history_complete = False
        for page_number in range(50):
            page = scan.fetch(f"https://hh.ru/applicant/negotiations?page={page_number}", cookies)
            match = scan.STATE.search(page)
            state = json.loads(html.unescape(match.group(1))) if match else {}
            history = state.get("applicantNegotiations")
            if not isinstance(history, dict) or not isinstance(history.get("topicList"), list):
                break
            history_pages.append(history)
            try:
                pages = int(history.get("pageCount"))
            except (ValueError, TypeError):
                break
            if pages < 1:
                break
            if page_number + 1 >= pages:
                history_complete = True
                break
        today = datetime.now(scan.MOSCOW).date()
        checked, seen = [], set()
        for name, resume_id in profiles.items():
            params = urllib.parse.urlencode({"resume": resume_id, "from": "resumelist", "page": 0,
                "items_on_page": 20, "search_period": 1, "order_by": "publication_time"})
            rows, _, _ = scan.parse_search(scan.fetch("https://hh.ru/search/vacancy?" + params, cookies), resume_id)
            for row in rows:
                item = scan.normalize(row, today)
                if item["id"] in seen or item["date_status"] != "today" or item["salary_status"] != "pass":
                    continue
                seen.add(item["id"])
                description = score_run.vacancy_text(scan.fetch(item["url"], cookies))
                result = scoring.score_vacancy(item["name"], description)
                result["salary_from_description"] = scoring.salary_upper_from_text(description, item.get("salary") or {})
                if result.get("below_threshold") or (result["salary_from_description"] or {}).get("below_threshold"):
                    continue
                form_page = scan.fetch("https://hh.ru/applicant/vacancy_response?" + urllib.parse.urlencode({"vacancyId": item["id"]}), cookies)
                form = detect_questions_in_html(form_page)
                item["form_status"] = form.status
                checked.append({"item": item, "score": result, "description": description, "form_reason": form.reason, "form_page": form_page})
                if len(checked) >= 3:
                    break
            if len(checked) >= 3:
                break
        output = {"at": datetime.now(scan.MOSCOW).isoformat(), "resume_snapshots": snapshots, "candidates": checked,
                  "current_history_pages": history_pages, "current_history_pages_complete": history_complete,
                  "archived_history_complete": False}
        (scan.ROOT / "data/stage1-probe.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        control.enqueue(f"Проверка этапа 1: резюме {len(snapshots)}, подходящих свежих вакансий {len(checked)}. Анкеты требуют отдельной проверки; отклики не отправлены.")
    except Exception as exc:
        if control.enabled():
            control.set_enabled(False, reason="ошибка проверки hh")
        control.enqueue(f"Проверка этапа 1 остановлена: {type(exc).__name__}. Подробности проверяются локально.")
        (scan.ROOT / "data/stage1-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
