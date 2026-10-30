"""Постоянное состояние управления и очередь сообщений владельцу."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data/vacancies.sqlite3"


@contextmanager
def connect(path: Path = DEFAULT_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, text TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    db.execute("INSERT OR IGNORE INTO control VALUES ('enabled', '0')")
    db.execute("INSERT OR IGNORE INTO control VALUES ('offset', '0')")
    db.execute("INSERT OR IGNORE INTO control VALUES ('reason', '')")
    db.commit()
    try:
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def single_instance(path: Path | None = None):
    """Не запускать второй экземпляр или ручной обход параллельно с ботом."""
    lock = path or ROOT / "data/service.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Другой экземпляр уже работает") from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def enabled(path: Path = DEFAULT_DB) -> bool:
    with connect(path) as db:
        return db.execute("SELECT value FROM control WHERE key='enabled'").fetchone()[0] == "1"


def set_enabled(value: bool, path: Path = DEFAULT_DB, reason: str = "") -> None:
    with connect(path) as db:
        db.execute("UPDATE control SET value=? WHERE key='enabled'", ("1" if value else "0",))
        db.execute("UPDATE control SET value=? WHERE key='reason'", (reason,))


def enqueue(text: str, path: Path = DEFAULT_DB) -> None:
    with connect(path) as db:
        db.execute("INSERT INTO notifications(text,next_attempt) VALUES (?,?)", (text, time.time()))


def process_update(update: dict, owner: int, path: Path = DEFAULT_DB) -> bool:
    """Обработать update один раз; состояние, ответ и offset фиксируются вместе."""
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return False
    message = update.get("message") or {}
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    words = str(message.get("text") or "").split(maxsplit=1)
    command = words[0].split("@", 1)[0].lower() if words else ""
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        offset = int(db.execute("SELECT value FROM control WHERE key='offset'").fetchone()[0])
        if update_id < offset:
            return False
        authorized = chat.get("type") == "private" and chat.get("id") == owner and sender.get("id") == owner
        if authorized and command in {"/on", "/off", "/status", "/start"}:
            if command == "/on":
                db.execute("UPDATE control SET value='1' WHERE key='enabled'")
                db.execute("UPDATE control SET value='' WHERE key='reason'")
                reply = "Работа hh включена. По расписанию работает пилот: до одной попытки отклика за проход."
            elif command == "/off":
                db.execute("UPDATE control SET value='0' WHERE key='enabled'")
                db.execute("UPDATE control SET value='выключено владельцем' WHERE key='reason'")
                reply = "Поиск hh выключен. Начатый запрос завершится; новых запросов не будет."
            else:
                state = db.execute("SELECT value FROM control WHERE key='enabled'").fetchone()[0]
                reason = db.execute("SELECT value FROM control WHERE key='reason'").fetchone()[0]
                has_runs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
                last = db.execute("SELECT started,status FROM runs ORDER BY id DESC LIMIT 1").fetchone() if has_runs else None
                queued = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
                reply = f"Работа hh: {'включена' if state == '1' else 'выключена'}. Режим: пилот, до одной попытки отклика за проход; поиск, ручной список и мониторинг. Расписание: 09:00, 12:00, 15:00, 18:00, 21:00 МСК."
                if reason:
                    reply += f" Причина паузы: {reason}."
                reply += f" Последний обход: {last[0]} ({last[1]})." if last else " Обходов пока нет."
                reply += f" Сообщений в очереди: {queued}."
            db.execute("INSERT INTO notifications(text,next_attempt) VALUES (?,?)", (reply, time.time()))
        db.execute("UPDATE control SET value=? WHERE key='offset'", (str(update_id + 1),))
        return authorized
