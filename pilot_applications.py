"""Ограниченный пилот: не более одной попытки из завершённого обхода."""

import html
import json
import urllib.parse
from datetime import datetime

import application_sender
import control
import scan
import scoring
from form_handling import classify_form, is_simple_response_form


def run(db, *, run_id, profiles, cookie, now=None):
    now = now or datetime.now(scan.MOSCOW)
    if db.execute("SELECT 1 FROM application_attempts WHERE source='service' AND status IN ('sending','uncertain') LIMIT 1").fetchone():
        return []
    if db.execute("SELECT 1 FROM application_attempts WHERE source='service' AND run_id=? LIMIT 1", (run_id,)).fetchone():
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
    for _, item, raw_vacancy, score, description in candidates:
        if not control.enabled():
            return []
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
                                                  run_id=run_id, now=datetime.now(scan.MOSCOW), evidence=evidence, max_run=1)
        return [{"vacancy_id": item["id"], "name": item["name"], "url": item["url"], "profile": selected, "status": result}]
    return []
