"""Повторно прочитать исход одной неопределённой попытки; POST не выполняется."""

import html
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import control
import cycle_extensions
import scan
import application_history
from hh_application import HhApplication, Restricted


def run(include_confirmed=False):
    try:
        with closing(sqlite3.connect(control.DEFAULT_DB)) as db:
            row = db.execute("SELECT vacancy_id,letter,attempted_at,detail FROM application_attempts WHERE source='service' AND (status='uncertain' OR (? AND status='confirmed')) ORDER BY attempted_at DESC LIMIT 1", (int(include_confirmed),)).fetchone()
        if not row:
            return
        vid, letter, attempted_at, raw_detail = row
        session = json.loads((Path.home() / ".codex/hh-session/session.json").read_text(encoding="utf-8-sig"))
        cookie = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
        topic = None
        completed = []
        for kind in ("active", "archived"):
            pages, complete = application_history.read_pages(lambda number: cycle_extensions.history_page(cookie, kind, number), expected_filter=kind)
            completed.append(complete)
            if not complete:
                raise scan.StopScan("incomplete history during verification")
            topic = next((t for page in pages for t in page["topicList"] if str(t.get("vacancyId")) == vid), None)
            if topic:
                break
        verified = None
        created = None
        if topic:
            try:
                created = datetime.fromisoformat(topic.get("creationTime") or "")
                attempted = datetime.fromisoformat(attempted_at)
                recent = created.tzinfo is not None and attempted.tzinfo is not None and created >= attempted - timedelta(minutes=5)
            except ValueError:
                recent = False
            chat_id = str(topic.get("chatId") or "")
            if chat_id.isdigit() and control.enabled():
                try:
                    chat = HhApplication(session["cookies"]).chat_state(chat_id)
                    own_id = chat.get("currentParticipantId")
                    messages = (chat.get("messages") or {}).get("items")
                    (scan.ROOT / "data/verification-shape.json").write_text(json.dumps({
                        "chat_fields": list(chat), "has_participant": own_id is not None,
                        "message_count": len(messages) if isinstance(messages, list) else None,
                        "message_fields": sorted({key for msg in messages for key in msg}) if isinstance(messages, list) else []}), encoding="utf-8")
                    if own_id is not None and isinstance(messages, list):
                        verified = any(msg.get("participantId") == own_id and (msg.get("text") or "").strip() == letter.strip() for msg in messages)
                        if not verified:
                            verified = None
                except Restricted as exc:
                    if control.enabled():
                        control.set_enabled(False, reason="ошибка чтения чата hh")
                    (scan.ROOT / "data/verification-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
                except (ValueError, OSError) as exc:
                    (scan.ROOT / "data/verification-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
            if recent:
                detail = json.loads(raw_detail or "{}")
                detail.update({"history_rechecked": True, "letter_verified": verified, "topic_created": created.isoformat(), "history_chat_id_present": chat_id.isdigit()})
                with closing(sqlite3.connect(control.DEFAULT_DB)) as db:
                    with db:
                        db.execute("UPDATE application_attempts SET status='confirmed',confirmed_at=COALESCE(confirmed_at,?),detail=? WHERE vacancy_id=? AND status IN ('uncertain','confirmed')",
                            (datetime.now(scan.MOSCOW).isoformat(), json.dumps(detail, ensure_ascii=False), vid))
                control.enqueue(f"Пилот: отклик https://hh.ru/vacancy/{vid} подтверждён повторным чтением hh. Письмо: {'совпало' if verified else 'не удалось проверить'}.")
                return
        if not topic and completed == [True, True]:
            detail = json.loads(raw_detail or "{}")
            detail.update({"history_rechecked": True, "history_scope": "active and archived; hidden data may be unavailable"})
            with closing(sqlite3.connect(control.DEFAULT_DB)) as db:
                with db:
                    db.execute("UPDATE application_attempts SET status='uncertain_reviewed',detail=? WHERE vacancy_id=? AND status='uncertain'",
                        (json.dumps(detail, ensure_ascii=False), vid))
            control.enqueue(f"Отклик https://hh.ru/vacancy/{vid} в активной и архивной истории не найден. Исход остаётся неясным, повтор этой вакансии запрещён.")
        else:
            if control.enabled():
                control.set_enabled(False, reason="исход отклика не подтверждён")
            control.enqueue(f"Отклик https://hh.ru/vacancy/{vid} пока не подтверждён; повторная отправка запрещена.")
    except Exception as exc:
        (scan.ROOT / "data/verification-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        if control.enabled():
            control.set_enabled(False, reason="ошибка проверки отклика")
            control.enqueue(f"Проверка пилотного отклика остановлена: {type(exc).__name__}. Повторная отправка запрещена.")
