"""Сверка прочитанной истории и постоянный список ручных откликов."""

import json
from datetime import datetime

import applications


def read_pages(fetch_page, *, expected_filter="active", max_pages=50):
    """Получить все страницы конкретного фильтра, не выдавая обрыв за полноту."""
    pages, fingerprints, total_expected, pages_expected = [], set(), None, None
    for index in range(max_pages):
        page = fetch_page(index)
        if not isinstance(page, dict) or page.get("filterInUse") != expected_filter or not isinstance(page.get("topicList"), list):
            return pages, False
        try:
            count, total = int(page["pageCount"]), int(page["total"])
        except (KeyError, TypeError, ValueError):
            return pages, False
        if count < 1 or total < 0 or (pages_expected is not None and (pages_expected != count or total_expected != total)):
            return pages, False
        pages_expected, total_expected = count, total
        fingerprint = tuple(str(item.get("id") or "") for item in page["topicList"])
        if (fingerprint and fingerprint in fingerprints) or (not fingerprint and total > 0):
            return pages, False
        fingerprints.add(fingerprint)
        pages.append(page)
        if index + 1 >= count:
            return pages, sum(len(p["topicList"]) for p in pages) == total
    return pages, False


def setup(db):
    applications.setup(db)
    db.execute("""CREATE TABLE IF NOT EXISTS manual_applications (
        vacancy_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
        resume_id TEXT, profile TEXT, reason TEXT NOT NULL,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS history_checks (
        checked_at TEXT NOT NULL, current_complete INTEGER NOT NULL,
        archive_complete INTEGER NOT NULL, pages INTEGER NOT NULL, entries INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS observed_hh_applications (
        vacancy_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, state TEXT, topic_type TEXT
    )""")
    # Подхватить историю, импортированную прежней версией до появления этой таблицы.
    db.execute("""INSERT OR IGNORE INTO observed_hh_applications(vacancy_id,seen_at)
        SELECT vacancy_id, COALESCE((SELECT MAX(checked_at) FROM history_checks), '')
        FROM application_attempts WHERE source='hh_history'""")
    db.commit()


def import_pages(db, pages, *, checked_at, current_complete, archive_complete=False):
    """Импортировать наблюдённые ID без изменения результатов собственных попыток."""
    if checked_at.tzinfo is None:
        raise ValueError("timezone required")
    entries = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("topicList"), list):
            raise ValueError("unrecognized history page")
        for topic in page["topicList"]:
            vid = str(topic.get("vacancyId") or "")
            if not vid.isdigit():
                raise ValueError("history vacancy ID missing")
            stamp = topic.get("creationTime")
            try:
                parsed = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
                if parsed is not None and parsed.tzinfo is None:
                    parsed = None
            except ValueError:
                parsed = None
            entries.append((vid, parsed.astimezone(applications.MOSCOW).isoformat() if parsed else None,
                            topic.get("lastState"), topic.get("initialTopicType")))
    with db:
        for vid, stamp, state, kind in entries:
            # Отклик со стороны работодателя тоже запрещает повтор, но не расходует лимит отправок.
            attempted = stamp if kind == "RESPONSE_BY_APPLICANT" else None
            db.execute("INSERT OR IGNORE INTO application_attempts(vacancy_id,status,source,detail,attempted_at) VALUES (?,?,?,?,?)",
                       (vid, "confirmed", "hh_history", json.dumps({"state": state, "type": kind}, ensure_ascii=False), attempted))
            if attempted:
                db.execute("UPDATE application_attempts SET attempted_at=? WHERE vacancy_id=? AND source='manual_import' AND attempted_at IS NULL",
                           (attempted, vid))
            db.execute("""INSERT INTO observed_hh_applications VALUES (?,?,?,?)
                ON CONFLICT(vacancy_id) DO UPDATE SET seen_at=excluded.seen_at,state=excluded.state,topic_type=excluded.topic_type""",
                (vid, checked_at.astimezone(applications.MOSCOW).isoformat(), state, kind))
        db.execute("INSERT INTO history_checks VALUES (?,?,?,?,?)", (checked_at.astimezone(applications.MOSCOW).isoformat(),
                   int(current_complete), int(archive_complete), len(pages), len(entries)))
    return len(entries)


def queue_manual(db, *, vacancy_id, url, title, reason, seen_at, profile=None, resume_id=None):
    if not str(vacancy_id).isdigit() or not reason or seen_at.tzinfo is None:
        raise ValueError("invalid manual case")
    stamp = seen_at.astimezone(applications.MOSCOW).isoformat()
    with db:
        db.execute("""INSERT INTO manual_applications(vacancy_id,url,title,resume_id,profile,reason,first_seen,last_seen)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(vacancy_id) DO UPDATE SET
        url=excluded.url,title=excluded.title,resume_id=excluded.resume_id,profile=excluded.profile,
        reason=excluded.reason,last_seen=excluded.last_seen,
        status=CASE WHEN manual_applications.status='not_needed' THEN 'open' ELSE manual_applications.status END""",
        (str(vacancy_id), url, title, resume_id, profile, reason, stamp, stamp))


def manual_report(db):
    rows = db.execute("""SELECT m.vacancy_id,m.url,m.title,m.profile,m.reason FROM manual_applications m
        WHERE m.status='open' AND NOT EXISTS (SELECT 1 FROM application_attempts a
        WHERE a.vacancy_id=m.vacancy_id AND a.status IN ('confirmed','duplicate','uncertain','uncertain_reviewed','sending'))
        AND NOT EXISTS (SELECT 1 FROM observed_hh_applications h WHERE h.vacancy_id=m.vacancy_id)
        ORDER BY m.first_seen,m.vacancy_id""").fetchall()
    lines = ["# Ручные отклики", "", "Анкеты не заполняются автоматически. Неизвестная форма тоже требует проверки.", ""]
    for vid, url, title, profile, reason in rows:
        lines.append(f"- {title} — {url} | резюме: {profile or 'не определено'} | {reason}")
    return "\n".join(lines) + "\n"
