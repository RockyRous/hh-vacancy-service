"""Обновление истории и ручного списка во время обхода."""

import html
import json
import sqlite3
import time
from datetime import datetime

import application_history
import control
import scan
from hh_application import HhApplication, Restricted, session


def history_page(cookie, kind, number):
    page = scan.fetch(f"https://hh.ru/applicant/negotiations?filter={kind}&page={number}", cookie)
    match = scan.STATE.search(page)
    if not match:
        raise scan.StopScan("history state missing")
    return json.loads(html.unescape(match.group(1))).get("applicantNegotiations")


def refresh_history(db, cookie, *, fetch_page=history_page, delay=0.4, now=None):
    """Обновить обе вкладки и вернуть новые изменения после первого наблюдения."""
    application_history.setup(db)
    now = now or datetime.now(scan.MOSCOW)
    def read(kind):
        def fetch(number):
            if delay:
                time.sleep(delay)
            return fetch_page(cookie, kind, number)
        return application_history.read_pages(fetch, expected_filter=kind)
    active, active_ok = read("active")
    archived, archive_ok = read("archived")
    if not active_ok or not archive_ok:
        raise scan.StopScan("history pagination incomplete")
    pages = active + archived
    changes = record_history(db, pages, now)
    application_history.import_pages(db, pages, checked_at=now, current_complete=True, archive_complete=True)
    return changes


def record_history(db, pages, now):
    db.execute("""CREATE TABLE IF NOT EXISTS monitored_topics (
        topic_id TEXT PRIMARY KEY, vacancy_id TEXT NOT NULL, state TEXT,
        message_count INTEGER, has_new INTEGER, chat_id TEXT, observed_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS history_events (
        id INTEGER PRIMARY KEY, data TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0
    )""")
    changes = []
    with db:
        baseline = db.execute("SELECT COUNT(*) FROM monitored_topics").fetchone()[0] == 0
        for page in pages:
            for topic in page["topicList"]:
                topic_id, vacancy_id = str(topic.get("id") or ""), str(topic.get("vacancyId") or "")
                if not topic_id.isdigit() or not vacancy_id.isdigit():
                    raise ValueError("invalid history topic")
                state = str(topic.get("lastState") or "")
                count = topic.get("conversationMessagesCount")
                if not isinstance(count, int) or isinstance(count, bool):
                    count = None
                unread = int(topic.get("hasNewMessages") is True)
                chat = str(topic.get("chatId") or "")
                chat = chat if chat.isdigit() else None
                previous = db.execute("SELECT state,message_count,has_new,observed_at FROM monitored_topics WHERE topic_id=?", (topic_id,)).fetchone()
                if previous and (state != previous[0] or (count is not None and previous[1] is not None and count > previous[1]) or (unread and not previous[2])):
                    changes.append({"vacancy_id": vacancy_id, "chat_id": chat, "state": state,
                                    "previous_state": previous[0], "new_messages": count is not None and previous[1] is not None and count > previous[1],
                                    "unread": bool(unread), "since": previous[3]})
                elif not previous and not baseline:
                    changes.append({"vacancy_id": vacancy_id, "chat_id": chat, "state": state,
                                    "previous_state": None, "new_messages": False, "unread": bool(unread), "since": now.isoformat()})
                if changes and changes[-1]["vacancy_id"] == vacancy_id and changes[-1].get("_stored") is None:
                    event = changes[-1]
                    db.execute("INSERT INTO history_events(data) VALUES (?)", (json.dumps(event, ensure_ascii=False),))
                    event["_stored"] = True
                db.execute("""INSERT INTO monitored_topics VALUES (?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET
                    vacancy_id=excluded.vacancy_id,state=excluded.state,message_count=excluded.message_count,
                    has_new=excluded.has_new,chat_id=excluded.chat_id,observed_at=excluded.observed_at""",
                    (topic_id, vacancy_id, state, count, unread, chat, now.isoformat()))
    return changes


def report_history_events(db, cookie):
    """Сообщить об изменениях; текст из доступного адресного чата сохраняется локально."""
    db.execute("""CREATE TABLE IF NOT EXISTS monitored_messages (
        chat_id TEXT NOT NULL, message_id TEXT NOT NULL, text TEXT NOT NULL, created TEXT,
        PRIMARY KEY(chat_id,message_id)
    )""")
    db.commit()
    events = db.execute("SELECT id,data FROM history_events WHERE notified=0 ORDER BY id").fetchall()
    for event_id, data in events:
        event = json.loads(data)
        texts, new_messages = [], []
        if event.get("chat_id") and (event.get("new_messages") or event.get("unread")):
            try:
                chat = HhApplication(session()).chat_state(event["chat_id"])
            except Restricted as exc:
                raise scan.StopScan(str(exc)) from None
            me, messages = chat.get("currentParticipantId"), (chat.get("messages") or {}).get("items")
            if me is not None and isinstance(messages, list):
                for msg in messages:
                    message_id = str(msg.get("id") or "")
                    text = msg.get("text") or ""
                    created = msg.get("creationTime")
                    if not message_id.isdigit() or not text or msg.get("participantId") == me:
                        continue
                    try:
                        stamp, since = datetime.fromisoformat(created), datetime.fromisoformat(event["since"])
                        if stamp.tzinfo is None or since.tzinfo is None or stamp <= since:
                            continue
                    except (TypeError, ValueError):
                        continue
                    if not db.execute("SELECT 1 FROM monitored_messages WHERE chat_id=? AND message_id=?", (event["chat_id"], message_id)).fetchone():
                        new_messages.append((event["chat_id"], message_id, text, created))
                        texts.append(text)
        line = f"Изменение отклика: https://hh.ru/vacancy/{event['vacancy_id']} | статус hh: {event['state'] or 'неизвестен'}"
        if event.get("new_messages") or event.get("unread"):
            line += " | есть изменения в сообщениях"
        if texts:
            line += "\n" + "\n".join(texts)[:1500]
        with db:
            db.executemany("INSERT OR IGNORE INTO monitored_messages VALUES (?,?,?,?)", new_messages)
            db.execute("INSERT INTO notifications(text,next_attempt) VALUES (?,?)", (line, time.time()))
            db.execute("UPDATE history_events SET notified=1 WHERE id=?", (event_id,))


def queue_candidates(db, run_id, profiles, now):
    application_history.setup(db)
    rows = db.execute("""SELECT o.data,s.result FROM observations o JOIN scores s
        ON s.run_id=o.run_id AND s.vacancy_id=o.vacancy_id WHERE o.run_id=?""", (run_id,)).fetchall()
    count = 0
    for raw_item, raw_score in rows:
        item, score = json.loads(raw_item), json.loads(raw_score)
        if item.get("date_status") != "today" or item.get("salary_status") != "pass" or score.get("status") != "scored" or score.get("below_threshold") is not False or (score.get("salary_from_description") or {}).get("below_threshold"):
            continue
        if item.get("form_status") == "clear":
            with db:
                db.execute("UPDATE manual_applications SET status='not_needed' WHERE vacancy_id=? AND status='open'", (item["id"],))
            continue
        if item.get("form_status") not in ("manual", "unknown", "not_checked"):
            continue
        if db.execute("SELECT 1 FROM application_attempts WHERE vacancy_id=?", (item["id"],)).fetchone():
            continue
        profile = score.get("selected")
        reason = "обнаружена анкета или тест" if item["form_status"] == "manual" else "наличие вопросов не удалось исключить"
        application_history.queue_manual(db, vacancy_id=item["id"], url=item["url"], title=item["name"],
            reason=reason, seen_at=now, profile=profile, resume_id=profiles.get(profile))
        count += 1
    return count


def render_manual(db):
    target = scan.ROOT / "reports/manual-applications.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(application_history.manual_report(db), encoding="utf-8")
