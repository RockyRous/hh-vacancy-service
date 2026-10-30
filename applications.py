"""Локальная подготовка откликов и журнал попыток. Сетевой отправки нет."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))


def setup(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS application_attempts (
        vacancy_id TEXT PRIMARY KEY, resume_id TEXT, run_id INTEGER,
        status TEXT NOT NULL, letter TEXT, template_id TEXT, template_version TEXT,
        attempted_at TEXT, confirmed_at TEXT, source TEXT NOT NULL, detail TEXT
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(application_attempts)")}
    if "decision_snapshot" not in columns:
        db.execute("ALTER TABLE application_attempts ADD COLUMN decision_snapshot TEXT")
    db.commit()


def select_letter(profile: str, templates: list[dict]) -> dict | None:
    """Первый разрешённый шаблон; черновики можно показывать, но не отправлять."""
    candidates = [t for t in templates if profile in t.get("profiles", []) or "all" in t.get("profiles", [])]
    return next((t for t in candidates if t.get("approved") is True and str(t.get("text", "")).strip()), None)


def draft_letter(profile: str, templates: list[dict]) -> dict | None:
    return next((t for t in templates if (profile in t.get("profiles", []) or "all" in t.get("profiles", [])) and str(t.get("text", "")).strip()), None)


def prepare(item: dict, result: dict | None, profiles: dict, templates: list[dict], existing: dict | None, today: str) -> dict:
    """Показать решение без сетевых обращений и записи отклика."""
    reasons = []
    score = result or {}
    selected = score.get("selected")
    chosen = select_letter(selected, templates)
    draft = chosen or draft_letter(selected, templates)
    salary = score.get("salary_from_description") or {}
    if item.get("salary_status") != "pass" or salary.get("below_threshold"):
        reasons.append("зарплатный фильтр")
    if not result or score.get("status") != "scored":
        reasons.append("нет оценки")
    elif score.get("below_threshold"):
        reasons.append("все оценки ниже 50")
    if existing:
        reasons.append("уже есть отклик или неразрешённая попытка")
    if item.get("form_status") == "manual":
        reasons.append("обнаружена форма или тест")
    elif item.get("form_status") != "clear":
        reasons.append("отсутствие формы не подтверждено")
    published = item.get("published") or ""
    try:
        ts = datetime.fromisoformat(published)
        current = ts.tzinfo is not None and ts.astimezone(MOSCOW).date().isoformat() == today
    except (ValueError, TypeError):
        current = False
    if not current:
        reasons.append("вакансия не за текущий день или дата неизвестна")
    if not profiles.get(selected):
        reasons.append("нет ID выбранного резюме")
    if not chosen:
        reasons.append("нет согласованного письма")
    # Пока нет полной сверки истории, неизменяемого снимка резюме и проверенного
    # порога: любое предложение остаётся локальным черновиком.
    reasons.append("история, версия резюме и порог ещё не подтверждены")
    if existing:
        decision = "duplicate"
    elif item.get("salary_status") != "pass" or salary.get("below_threshold") or score.get("below_threshold"):
        decision = "skip"
    elif item.get("form_status") != "clear":
        decision = "manual"
    else:
        decision = "blocked"
    return {"vacancy_id": item["id"], "url": item["url"], "title": item["name"], "profile": selected, "resume_id": profiles.get(selected), "decision": decision, "reasons": reasons, "scores": score.get("scores", {}), "score_version": score.get("version"), "form_status": item.get("form_status"), "letter": draft.get("text") if draft else None, "template_id": draft.get("id") if draft else None, "template_version": draft.get("version") if draft else None, "template_approved": bool(draft and draft.get("approved") is True), "existing_status": existing.get("status") if existing else None}


def import_history(db: sqlite3.Connection, entries: list[dict]) -> int:
    """Импортировать известные отправки и неопределённые попытки без перезаписи."""
    inserted = 0
    with db:
        for entry in entries:
            vid, status = str(entry.get("vacancy_id", "")), entry.get("status")
            if not vid.isdigit() or status not in {"confirmed", "uncertain"}:
                raise ValueError("invalid history entry")
            raw_time = entry.get("attempted_at")
            if raw_time:
                parsed = datetime.fromisoformat(raw_time)
                if parsed.tzinfo is None:
                    raise ValueError("import timestamp must include timezone")
                raw_time = parsed.astimezone(MOSCOW).isoformat()
            row = db.execute("INSERT OR IGNORE INTO application_attempts(vacancy_id,status,source,detail,attempted_at) VALUES (?,?,?,?,?)", (vid, status, "manual_import", entry.get("detail", ""), raw_time))
            inserted += row.rowcount
    return inserted


def reserve_attempt(db: sqlite3.Connection, *, vacancy_id: str, resume_id: str, run_id: int, letter: str, template_id: str, template_version: str, now: datetime, max_run: int = 5, max_day: int = 15, max_rolling: int = 15, snapshot: dict | None = None) -> bool:
    """Атомарный резерв для будущего отправщика; не отправляет запрос.

    Вызывающий код обязан заранее проверить все бизнес-условия и режим. Эта
    низкоуровневая функция не включает автоматическую отправку.
    """
    limits = (max_run, max_day, max_rolling)
    if not vacancy_id.isdigit() or not resume_id or not letter.strip() or not template_id or not template_version or now.tzinfo is None or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in limits) or max_run > 5 or max_day > 15 or max_rolling > 15:
        raise ValueError("invalid attempt")
    now = now.astimezone(MOSCOW)
    beginning = now.replace(hour=0, minute=0, second=0, microsecond=0)
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM application_attempts WHERE vacancy_id=?", (vacancy_id,)).fetchone():
            db.rollback()
            return False
        per_run = db.execute("SELECT COUNT(*) FROM application_attempts WHERE source='service' AND run_id=?", (run_id,)).fetchone()[0]
        day = db.execute("SELECT COUNT(*) FROM application_attempts WHERE attempted_at>=? AND attempted_at<=?", (beginning.isoformat(), now.isoformat())).fetchone()[0]
        rolling = db.execute("SELECT COUNT(*) FROM application_attempts WHERE attempted_at>=? AND attempted_at<=?", ((now - timedelta(hours=24)).isoformat(), now.isoformat())).fetchone()[0]
        if per_run >= max_run or day >= max_day or rolling >= max_rolling:
            db.rollback()
            return False
        db.execute("INSERT INTO application_attempts (vacancy_id,resume_id,run_id,status,letter,template_id,template_version,attempted_at,confirmed_at,source,detail,decision_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (vacancy_id, resume_id, run_id, "sending", letter, template_id, template_version, now.isoformat(), None, "service", None, json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def classify_response(http_status: int | None, body: dict | None) -> tuple[str, bool]:
    """Вернуть (статус, нужна ли остановка hh); не повторять неясные попытки."""
    data = body if isinstance(body, dict) else {}
    code = str(data.get("error") or "").replace("_", "-")
    if code == "test-required":
        return "manual", http_status in (403, 429)
    if code == "already-applied":
        return "duplicate", http_status in (403, 429)
    if http_status in (403, 429) or "limit" in code:
        return "uncertain", True
    if http_status in (200, 201) and data.get("success") is True:
        return "confirmed", False
    return "uncertain", True


def finish_attempt(db: sqlite3.Connection, vacancy_id: str, http_status: int | None, body: dict | None, now: datetime, *, letter_verified: bool | None = None) -> tuple[str, bool]:
    """Записать исход ровно один раз; None для письма означает проверка недоступна."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    status, pause = classify_response(http_status, body)
    if status == "confirmed" and letter_verified is False:
        status, pause = "uncertain", True
    evidence = {"http_status": http_status, "error": (body or {}).get("error") if isinstance(body, dict) else None, "letter_verified": letter_verified}
    if isinstance(body, dict):
        evidence.update({"success": body.get("success") if isinstance(body.get("success"), (bool, int, str)) else None,
                         "chat_id_present": bool(body.get("chat_id")), "topic_id_present": bool(body.get("topic_id")),
                         "response_keys": sorted(str(key) for key in body)})
    with db:
        changed = db.execute("UPDATE application_attempts SET status=?,confirmed_at=?,detail=? WHERE vacancy_id=? AND status='sending'", (status, now.astimezone(MOSCOW).isoformat() if status == "confirmed" else None, json.dumps(evidence, ensure_ascii=False), vacancy_id)).rowcount
        if changed != 1:
            raise ValueError("attempt is missing or already finalized")
    return status, pause


def recover_inflight(db: sqlite3.Connection) -> int:
    """После перезапуска не угадывать результат прерванной отправки."""
    with db:
        return db.execute("UPDATE application_attempts SET status='uncertain',detail='interrupted before finalization' WHERE status='sending'").rowcount
