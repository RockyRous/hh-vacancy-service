"""Однократный read-only сбор вакансий hh по локальным профилям."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from form_handling import classify_form, detect_questions_in_html
import control

ROOT = Path(__file__).resolve().parent
MOSCOW = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
STATE = re.compile(r'<template[^>]*id=["\']HH-Lux-InitialState["\'][^>]*>(.*?)</template>', re.S)


class StopScan(Exception):
    pass


class HhRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {"hh.ru", "spb.hh.ru"}:
            return None
        if not control.enabled():
            raise StopScan("hh is switched off")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, cookies: str) -> str:
    if not control.enabled():
        raise StopScan("hh is switched off")
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": cookies, "Accept-Language": "ru-RU,ru;q=0.9"})
    try:
        with urllib.request.build_opener(HhRedirect()).open(request, timeout=25) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if response.status != 200 or final.hostname not in {"hh.ru", "spb.hh.ru"} or "/account/login" in final.path:
                raise StopScan("unexpected response or redirect")
            raw = response.read()
            if b"<title>DDoS-Guard" in raw[:1000]:
                raise StopScan("protection challenge")
            return raw.decode(response.headers.get_content_charset() or "utf-8", errors="strict")
    except urllib.error.HTTPError as exc:
        raise StopScan(f"hh HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise StopScan(f"network or decoding error: {type(exc).__name__}") from None


def parse_search(page: str, resume_id: str) -> tuple[list[dict], bool, int | None]:
    match = STATE.search(page)
    if not match:
        raise StopScan("search state missing; login or layout may have changed")
    try:
        state = json.loads(html.unescape(match.group(1)))
        result = state["vacancySearchResult"]
        params = result["proxiedSearchFormParams"]
        if resume_id not in json.dumps(params):
            raise StopScan("search response does not confirm requested resume")
        rows = result["vacancies"]
        if not isinstance(rows, list):
            raise ValueError("vacancies is not a list")
        paging = result["paging"]
        next_page = paging.get("next")
        if not isinstance(next_page, dict) or "disabled" not in next_page:
            raise ValueError("next page state missing")
        return rows, not next_page["disabled"], result.get("totalResults")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise StopScan("unrecognized search response") from None


def normalize(row: dict, today: datetime.date) -> dict:
    vid = str(row.get("vacancyId") or "")
    if not vid.isdigit():
        raise StopScan("vacancy ID missing in search result")
    publication = row.get("publicationTime") or {}
    stamp = publication.get("$") if isinstance(publication, dict) else None
    try:
        value = datetime.fromisoformat(stamp)
        if value.tzinfo is None:
            raise ValueError("missing timezone")
        date_status = "today" if value.astimezone(MOSCOW).date() == today else "other_day"
    except (TypeError, ValueError):
        date_status = "unknown"
    salary = row.get("compensation")
    salary = salary if isinstance(salary, dict) else {}
    upper, currency = salary.get("to"), salary.get("currencyCode")
    excluded = isinstance(upper, (int, float)) and not isinstance(upper, bool) and currency in {"RUR", "RUB"} and upper < 200000
    company = row.get("company") or {}
    return {"id": vid, "name": row.get("name") or "Без названия", "company": (company.get("visibleName") or company.get("name") or "") if isinstance(company, dict) else "", "url": f"https://hh.ru/vacancy/{vid}", "published": stamp, "date_status": date_status, "salary": salary, "salary_status": "below_200000" if excluded else "pass", "form_status": "not_checked", "form_reason": "", "profiles": []}


def scan(profiles: dict, cookie: str, db_path: Path, report_path: Path, pause: float = 0.5, max_pages: int = 100) -> int:
    started = datetime.now(MOSCOW)
    today = started.date()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL, error TEXT, profile_pages TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS observations (run_id INTEGER NOT NULL, vacancy_id TEXT NOT NULL, data TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY (run_id, vacancy_id))")
    run_id = conn.execute("INSERT INTO runs(started,status) VALUES (?,?)", (started.isoformat(), "running")).lastrowid
    conn.commit()
    found: dict[str, dict] = {}
    raw_rows: dict[str, dict] = {}
    profile_pages: dict[str, dict] = {}
    status, error = "complete", ""
    try:
        for profile, resume_id in profiles.items():
            seen_pages: set[tuple[str, ...]] = set()
            profile_pages[profile] = {"pages": 0, "complete": False, "total": None}
            for page in range(max_pages):
                if datetime.now(MOSCOW).date() != today:
                    raise StopScan("Moscow date changed during scan")
                params = urllib.parse.urlencode({"resume": resume_id, "from": "resumelist", "page": page, "items_on_page": 20, "search_period": 1, "order_by": "publication_time"})
                rows, has_next, total = parse_search(fetch("https://hh.ru/search/vacancy?" + params, cookie), resume_id)
                fingerprint = tuple(str(row.get("vacancyId") or "") for row in rows)
                if has_next and not rows:
                    raise StopScan("empty search page with next page")
                if fingerprint and fingerprint in seen_pages:
                    raise StopScan("search page repeated")
                seen_pages.add(fingerprint)
                for raw in rows:
                    if not isinstance(raw, dict):
                        raise StopScan("invalid vacancy in search results")
                    item = normalize(raw, today)
                    vid = item["id"]
                    if vid not in found:
                        found[vid], raw_rows[vid] = item, raw
                    if profile not in found[vid]["profiles"]:
                        found[vid]["profiles"].append(profile)
                profile_pages[profile].update(pages=page + 1, total=total)
                print(f"{profile}: страница {page + 1}, вакансий {len(rows)}, найдено {total}", flush=True)
                if not has_next:
                    profile_pages[profile]["complete"] = True
                    break
                time.sleep(pause)
            else:
                raise StopScan(f"page limit reached for {profile}")

        eligible = [v for v in found.values() if v["date_status"] == "today" and v["salary_status"] == "pass"]
        for number, item in enumerate(eligible, 1):
            if datetime.now(MOSCOW).date() != today:
                raise StopScan("Moscow date changed during form checks")
            raw = raw_rows[item["id"]]
            decision = classify_form({"response_url": raw.get("responseUrl"), "has_test": raw.get("hasTest")})
            if decision.status != "manual":
                url = "https://hh.ru/applicant/vacancy_response?" + urllib.parse.urlencode({"vacancyId": item["id"]})
                page = fetch(url, cookie)
                decision = detect_questions_in_html(page)
            item["form_status"], item["form_reason"] = decision.status, decision.reason
            print(f"Формы: {number}/{len(eligible)} — {item['id']}: {decision.status}", flush=True)
            time.sleep(pause)
    except (StopScan, OSError) as exc:
        status, error = "partial", str(exc)
    except Exception as exc:
        status, error = "partial", f"unexpected processing error: {type(exc).__name__}"
    finally:
        for vid, item in found.items():
            conn.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?)", (run_id, vid, json.dumps(item, ensure_ascii=False), json.dumps(raw_rows[vid], ensure_ascii=False)))
        conn.execute("UPDATE runs SET finished=?,status=?,error=?,profile_pages=? WHERE id=?", (datetime.now(MOSCOW).isoformat(), status, error, json.dumps(profile_pages, ensure_ascii=False), run_id))
        conn.commit()
        conn.close()
        report = render_report(run_id, started, status, error, profile_pages, found)
        report_path.write_text(report, encoding="utf-8")
        print(report, flush=True)
    return 0 if status == "complete" else 1


def salary_text(salary: dict) -> str:
    low, high = salary.get("from"), salary.get("to")
    if low is None and high is None:
        return "не указана"
    amount = f"{low}–{high}" if low is not None and high is not None else f"от {low}" if low is not None else f"до {high}"
    currency = salary.get("currencyCode") or "валюта не указана"
    gross = salary.get("gross")
    tax = "gross" if gross is True else "net" if gross is False else "до/после налогов не указано"
    mode = salary.get("mode") or "период не указан"
    return f"{amount} {currency}, {tax}, {mode}"


def render_report(run_id: int, started: datetime, status: str, error: str, pages: dict, vacancies: dict) -> str:
    rows = list(vacancies.values())
    today = [v for v in rows if v["date_status"] == "today"]
    passed = [v for v in today if v["salary_status"] == "pass"]
    lines = [f"# Обход {run_id} — {started.isoformat()}", f"Статус: {'завершён' if status == 'complete' else 'неполный'}" + (f"; причина: {error}" if error else ""), f"Уникальных: {len(rows)}; сегодня: {len(today)}; дата неизвестна: {sum(v['date_status'] == 'unknown' for v in rows)}; ниже зарплатного порога: {sum(v['salary_status'] == 'below_200000' for v in today)}; прошло зарплату: {len(passed)}.", f"Форма найдена: {sum(v['form_status'] == 'manual' for v in passed)}; не удалось исключить: {sum(v['form_status'] == 'unknown' for v in passed)}; не проверено: {sum(v['form_status'] == 'not_checked' for v in passed)}.", "", "## Профили"]
    for name, info in pages.items():
        lines.append(f"- {name}: страниц {info['pages']}, всего у hh {info['total']}, закончен: {info['complete']}")
    lines += ["", "## Вакансии за сегодня, прошедшие зарплатный фильтр", ""]
    for v in passed:
        label = {"manual": "обнаружена анкета или внешняя форма", "unknown": "отсутствие анкеты не подтверждено", "not_checked": "не проверено", "clear": "проверено"}.get(v['form_status'], v['form_status'])
        lines.append(f"- {v['name']} — {v['company']} | {v['url']} | профили: {', '.join(v['profiles'])} | форма: {label} | зарплата: {salary_text(v['salary'])}")
    lines += ["", "Смысловая оценка вакансий ещё не выполнялась. Отправок откликов этот скрипт не делает."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Один read-only проход по вакансиям hh")
    parser.add_argument("--profiles", type=Path, default=ROOT / "data/profiles.json")
    parser.add_argument("--session", type=Path, default=Path.home() / ".codex/hh-session/session.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data/vacancies.sqlite3")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/latest.md")
    args = parser.parse_args()
    try:
        profiles = json.loads(args.profiles.read_text(encoding="utf-8-sig"))
        session = json.loads(args.session.read_text(encoding="utf-8-sig"))
        if not isinstance(profiles, dict) or len(profiles) != 3 or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{38}", v) for v in profiles.values()):
            raise ValueError("profiles must contain three resume IDs")
        cookies = session.get("cookies")
        if not isinstance(cookies, dict) or not cookies.get("hhtoken"):
            raise ValueError("hh session has no login cookie")
        cookie = "; ".join(f"{key}={value}" for key, value in cookies.items())
        return scan(profiles, cookie, args.db, args.report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Ошибка конфигурации: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        with control.single_instance():
            raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
