"""Проверить доступные фильтры истории hh внутри процесса бота."""

import html
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import control
import application_history
import scan


def run():
    try:
        session = json.loads((Path.home() / ".codex/hh-session/session.json").read_text(encoding="utf-8-sig"))
        cookie = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
        page = scan.fetch("https://hh.ru/applicant/negotiations", cookie)
        match = scan.STATE.search(page)
        state = json.loads(html.unescape(match.group(1))) if match else {}
        if not isinstance(state.get("applicantNegotiations"), dict):
            raise ValueError("history state missing")
        path = scan.ROOT / "data/history-page.html"
        path.write_text(page, encoding="utf-8")
        checks = []
        for query in ("filter=archived", "filter=archive", "status=archived", "filter=trash"):
            filtered = scan.fetch("https://hh.ru/applicant/negotiations?" + query, cookie)
            match = scan.STATE.search(filtered)
            blob = json.loads(html.unescape(match.group(1))) if match else {}
            history = blob.get("applicantNegotiations") or {}
            checks.append({"query": query, "filter": history.get("filterInUse"), "total": history.get("total"),
                           "page_count": history.get("pageCount"), "topicList": history.get("topicList")})
        (scan.ROOT / "data/stage2-filter-checks.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
        def get_page(kind, number):
            time.sleep(0.4)
            raw = scan.fetch(f"https://hh.ru/applicant/negotiations?filter={kind}&page={number}", cookie)
            match = scan.STATE.search(raw)
            if not match:
                return None
            return json.loads(html.unescape(match.group(1))).get("applicantNegotiations")
        active, active_complete = application_history.read_pages(lambda n: get_page("active", n), expected_filter="active")
        archived, archive_complete = application_history.read_pages(lambda n: get_page("archived", n), expected_filter="archived")
        (scan.ROOT / "data/stage2-history-pages.json").write_text(json.dumps({"active": active, "archived": archived}, ensure_ascii=False), encoding="utf-8")
        with sqlite3.connect(control.DEFAULT_DB, timeout=10) as db:
            application_history.setup(db)
            count = application_history.import_pages(db, active + archived, checked_at=datetime.now(scan.MOSCOW),
                    current_complete=active_complete, archive_complete=archive_complete)
        result = {"active_pages": len(active), "archive_pages": len(archived), "active_complete": active_complete,
                  "archive_complete": archive_complete, "entries": count}
        (scan.ROOT / "data/stage2-history.json").write_text(json.dumps(result), encoding="utf-8")
        control.enqueue(f"Проверка истории hh: обработано {count}, текущая: {active_complete}, архив: {archive_complete}.")
    except Exception as exc:
        if control.enabled():
            control.set_enabled(False, reason="ошибка проверки истории hh")
        control.enqueue(f"Проверка истории hh остановлена: {type(exc).__name__}.")
        (scan.ROOT / "data/stage2-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
