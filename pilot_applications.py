"""Отклики из завершённого обхода с согласованными лимитами."""

import html
import json
import time
import urllib.parse
from datetime import datetime

import application_sender
import control
import scan
import scoring
from form_handling import classify_form, is_simple_response_form


def run(db, *, run_id, profiles, cookie, now=None, notify=None, delay_seconds=30):
    now = now or datetime.now(scan.MOSCOW)
    if db.execute("SELECT 1 FROM application_attempts WHERE source='service' AND status IN ('sending','uncertain') LIMIT 1").fetchone():
        return []
    if db.execute("SELECT COUNT(*) FROM application_attempts WHERE source='service' AND run_id=?", (run_id,)).fetchone()[0] >= 20:
        return []
    history = db.execute("SELECT checked_at,current_complete,archive_complete FROM history_checks ORDER BY checked_at DESC LIMIT 1").fetchone()
    if not history or not history[1] or not history[2]:
        return []
    checked = datetime.fromisoformat(history[0])
    if checked.date() != now.astimezone(scan.MOSCOW).date() or checked > now:
        return []
    templates = json.loads((scan.ROOT / "data/letters.json").read_text(encoding="utf-8-sig"))
    rows = db.execute("""SELECT o.data,o.raw,s.result,s.description FROM observations o JOIN scores s
        ON s.run_id=o.run_id AND s.vacancy_id=o.vacancy_id WHERE o.run_id=?""", (run_id,)).fetchall()
    candidates = []
    for raw_item, raw_vacancy, raw_score, description in rows:
        item, score = json.loads(raw_item), json.loads(raw_score)
        if (item.get("form_status") not in {"clear", "unknown"} or item.get("date_status") != "today" or
            item.get("salary_status") != "pass" or score.get("status") != "scored" or
            score.get("version") != scoring.VERSION or score.get("below_threshold") is not False or
            (score.get("salary_from_description") or {}).get("below_threshold")):
            continue
        if db.execute("SELECT 1 FROM application_attempts WHERE vacancy_id=?", (item["id"],)).fetchone():
            continue
        value = score.get("scores", {}).get(score.get("selected"), {}).get("score", 0)
        candidates.append((value, item, json.loads(raw_vacancy), score, description))
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    results = []
    for index, (_, item, raw_vacancy, score, description) in enumerate(candidates):
        if not control.enabled():
            break
        selected = score["selected"]
        if not profiles.get(selected):
            continue
        known_form = classify_form({"response_url": raw_vacancy.get("responseUrl"), "has_test": raw_vacancy.get("hasTest")})
        if known_form.status == "manual":
            continue
        form_url = "https://hh.ru/applicant/vacancy_response?" + urllib.parse.urlencode({"vacancyId": item["id"]})
        form_page = scan.fetch(form_url, cookie)
        if not is_simple_response_form(form_page):
            continue
        item["form_status"] = "clear"
        page = scan.fetch("https://hh.ru/resume/" + profiles[selected], cookie)
        match = scan.STATE.search(page)
        state = json.loads(html.unescape(match.group(1))) if match else {}
        resume = state.get("applicantResume")
        if not resume:
            raise scan.StopScan("resume snapshot missing")
        evidence = {"history_complete": True, "scoring_validated": True, "form_checked": True,
                    "resume_snapshot": resume, "vacancy_snapshot": {"item": item, "raw": raw_vacancy, "description": description},
                    "history_scope": "active and archived tabs; hidden data may be unavailable"}
        result = application_sender.send_one_hh(db, item=item, score=score, profiles=profiles, templates=templates,
                                                  run_id=run_id, now=datetime.now(scan.MOSCOW), evidence=evidence, max_run=20)
        entry = {"vacancy_id": item["id"], "name": item["name"], "url": item["url"], "profile": selected, "status": result}
        results.append(entry)
        if notify:
            suffix = ""
            if result == "uncertain":
                row = db.execute("SELECT detail FROM application_attempts WHERE vacancy_id=?", (item["id"],)).fetchone()
                detail = json.loads(row[0]) if row and row[0] else {}
                if detail.get("captcha") or "hhcaptcha" in detail.get("response_keys", []):
                    suffix = " | hh запросил капчу; работа остановлена"
            notify(f"Отклик: {entry['name']} — {entry['url']} | резюме: {entry['profile']} | результат: {entry['status']}{suffix}")
        if result in {"uncertain", "off", "duplicate_or_limit", "blocked"} or len(results) >= 20:
            break
        if index + 1 == len(candidates):
            break
        # Не посылать отклики плотной очередью; /off проверяется во время ожидания.
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if not control.enabled():
                return results
            time.sleep(max(0, min(1, deadline - time.monotonic())))
    return results
