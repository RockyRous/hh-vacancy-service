"""Личный Telegram-бот и расписание read-only обходов hh."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import control
import scan
import score_run

SLOTS = (9, 12, 15, 18, 21)
MAX_UNITS = 3800  # Telegram counts text after parsing; plain text with a margin.


class TelegramError(Exception):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


def read_config(path: Path = scan.ROOT / ".env") -> tuple[str, int]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    owner = values.get("TELEGRAM_OWNER_ID", "")
    if not token or not owner.isdigit():
        raise ValueError("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID в .env")
    return token, int(owner)


def telegram_call(token: str, method: str, payload: dict, timeout: int = 20) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        retry = 0
        try:
            body = json.loads(exc.read().decode("utf-8"))
            retry = int((body.get("parameters") or {}).get("retry_after") or 0)
        except (ValueError, TypeError, UnicodeError):
            pass
        raise TelegramError(f"Telegram HTTP {exc.code}", retry) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise TelegramError("Telegram недоступен или вернул некорректный ответ") from None
    if not data.get("ok"):
        raise TelegramError("Telegram вернул ошибку", int((data.get("parameters") or {}).get("retry_after") or 0))
    return data


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_report(lines: list[str]) -> list[str]:
    """Делить по строкам; при длинном названии целиком сохранить ссылку."""
    fitted = []
    for line in lines:
        if utf16_len(line) > MAX_UNITS - 100:
            match = re.search(r"https?://\S+", line)
            url = match.group(0) if match else ""
            if utf16_len(url) > MAX_UNITS - 150:
                raise ValueError("Ссылка слишком длинная для Telegram")
            prefix = line[:match.start()] if match else line
            while utf16_len(prefix + "… " + url) > MAX_UNITS - 100:
                prefix = prefix[:max(0, len(prefix) - 100)]
            line = prefix.rstrip() + "… " + url
        fitted.append(line)
    parts, part = [], ""
    for line in fitted:
        candidate = part + ("\n" if part else "") + line
        if part and utf16_len(candidate) > MAX_UNITS - 30:
            parts.append(part)
            part = line
        else:
            part = candidate
    if part:
        parts.append(part)
    return [f"({i}/{len(parts)})\n{text}" for i, text in enumerate(parts, 1)]


def next_slot(now: datetime) -> datetime:
    now = now.astimezone(scan.MOSCOW)
    for hour in SLOTS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=SLOTS[0], minute=0, second=0, microsecond=0)


def queue_cycle_report(run_id: int) -> None:
    with control.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS reported_below (vacancy_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS scores (run_id INTEGER NOT NULL, vacancy_id TEXT NOT NULL, fetched_at TEXT NOT NULL, description TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id, vacancy_id))")
        run = db.execute("SELECT status,error FROM runs WHERE id=?", (run_id,)).fetchone()
        observations = [json.loads(row[0]) for row in db.execute("SELECT data FROM observations WHERE run_id=?", (run_id,))]
        scores = {vid: json.loads(result) for vid, result in db.execute("SELECT vacancy_id,result FROM scores WHERE run_id=?", (run_id,))}
        already = {row[0] for row in db.execute("SELECT vacancy_id FROM reported_below")}
        today = [item for item in observations if item.get("date_status") == "today"]
        salary_text_rejected = {vid for vid, result in scores.items() if (result.get("salary_from_description") or {}).get("below_threshold")}
        qualified = [item for item in today if item.get("salary_status") == "pass" and item["id"] not in salary_text_rejected]
        lines = [f"Обход #{run_id}: {run[0] if run else 'неизвестно'}", f"Найдено уникальных: {len(observations)}; сегодня: {len(today)}; зарплатный фильтр: {len(qualified)}; оценено: {len(scores)}; откликов: 0."]
        if run and run[1]:
            lines.append(f"Причина неполного обхода: {run[1]}")
        manual = [v for v in qualified if v.get("form_status") == "manual"]
        unknown = [v for v in qualified if v.get("form_status") in {"unknown", "not_checked"}]
        lines.append(f"Анкеты: {len(manual)}; форма не проверена или неизвестна: {len(unknown)}.")
        if manual:
            lines.append("Требуют ручного отклика:")
            for item in manual:
                result = scores.get(item["id"], {})
                lines.append(f"• {item['name']} — {item['url']} (резюме: {result.get('selected') or 'не определено'})")
        if unknown:
            lines.append("Форму не удалось исключить; проверить вручную:")
            for item in unknown:
                result = scores.get(item["id"], {})
                lines.append(f"• {item['name']} — {item['url']} (резюме: {result.get('selected') or 'не определено'})")
        new_below = []
        for item in qualified:
            result = scores.get(item["id"], {})
            if result.get("below_threshold") and item["id"] not in already:
                if not new_below:
                    lines.append("Ниже порога по всем резюме:")
                entries = result.get("scores", {})
                marks = ", ".join(f"{key}: {val['score']}" for key, val in entries.items())
                reasons = "; ".join(f"{key}: роль {', '.join(val.get('role_matches') or []) or 'не совпала'}, навыки {', '.join(val.get('skills') or []) or 'не найдены'}" for key, val in entries.items())
                lines.append(f"• {item['name']} — {item['url']} | {marks} | {reasons}")
                new_below.append(item["id"])
        parts = split_report(lines)
        for part in parts:
            db.execute("INSERT INTO notifications(text,next_attempt) VALUES (?,?)", (part, time.time()))
        db.executemany("INSERT OR IGNORE INTO reported_below(vacancy_id) VALUES (?)", ((vid,) for vid in new_below))


def deliver_one(token: str, owner: int) -> None:
    with control.connect() as db:
        row = db.execute("SELECT id,text,attempts FROM notifications WHERE next_attempt<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
    if not row:
        return
    try:
        telegram_call(token, "sendMessage", {"chat_id": owner, "text": row[1]}, timeout=15)
    except TelegramError as exc:
        delay = max(exc.retry_after, min(3600, 30 * 2 ** min(row[2], 7)))
        with control.connect() as db:
            db.execute("UPDATE notifications SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?", (time.time() + delay, str(exc), row[0]))
        return
    with control.connect() as db:
        db.execute("DELETE FROM notifications WHERE id=?", (row[0],))


def run_cycle() -> None:
    try:
        profiles = json.loads((scan.ROOT / "data/profiles.json").read_text(encoding="utf-8-sig"))
        session = json.loads((Path.home() / ".codex/hh-session/session.json").read_text(encoding="utf-8-sig"))
        if len(profiles) != 3 or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{38}", v) for v in profiles.values()):
            raise ValueError("профили hh не настроены")
        cookies = session["cookies"]
        if not cookies.get("hhtoken"):
            raise ValueError("нет сессии hh")
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        result = scan.scan(profiles, cookie, control.DEFAULT_DB, scan.ROOT / "reports/latest.md")
        with control.connect() as db:
            run_id = db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if result == 0 and control.enabled():
            result = score_run.main([])
        queue_cycle_report(run_id)
        if result and control.enabled():
            control.set_enabled(False, reason="ошибка обхода или оценки")
            control.enqueue("Работа hh остановлена из-за ошибки. Проверь локальные отчёты и после устранения проблемы отправь /on.")
    except Exception:
        control.set_enabled(False, reason="ошибка настроек или базы")
        control.enqueue("Работа hh остановлена: ошибка настроек или базы. Проверь локальные файлы, затем отправь /on.")


async def run_bot(token: str, owner: int) -> None:
    was_enabled = False
    due = None
    worker = None
    while True:
        active = control.enabled()
        now = datetime.now(scan.MOSCOW)
        if active and not was_enabled:
            due = next_slot(now)
        if not active:
            due = None
        if active and due is not None and now >= due:
            if now - due <= timedelta(minutes=1) and (worker is None or worker.done()):
                worker = asyncio.create_task(asyncio.to_thread(run_cycle))
            due = next_slot(now)
        was_enabled = active
        try:
            with control.connect() as db:
                offset = int(db.execute("SELECT value FROM control WHERE key='offset'").fetchone()[0])
            result = await asyncio.to_thread(telegram_call, token, "getUpdates", {"offset": offset, "timeout": 2, "allowed_updates": ["message"]}, 10)
            for update in result.get("result", []):
                control.process_update(update, owner)
        except TelegramError as exc:
            await asyncio.sleep(max(3, exc.retry_after))
        await asyncio.to_thread(deliver_one, token, owner)
        await asyncio.sleep(0.2)


def main() -> int:
    try:
        token, owner = read_config()
        with control.connect():
            pass
    except (OSError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    print("Telegram-бот запущен. Отклики отключены.", flush=True)
    try:
        with control.single_instance():
            asyncio.run(run_bot(token, owner))
    except KeyboardInterrupt:
        print("Остановлено.")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
