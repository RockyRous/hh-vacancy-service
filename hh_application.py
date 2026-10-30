"""Одна отправка на hh и необязательная проверка текста по ID чата из ответа."""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import control
import scan
from scan import UA


class Disabled(Exception):
    pass


class Restricted(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SafeGetRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, enabled):
        super().__init__()
        self.enabled = enabled

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if not self.enabled():
            raise Disabled("hh выключен")
        if target.scheme != "https" or target.hostname not in {"hh.ru", "spb.hh.ru"}:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def session(path=None):
    source = path or Path.home() / ".codex/hh-session/session.json"
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    cookies = data.get("cookies")
    if not isinstance(cookies, dict) or not cookies.get("hhtoken") or not cookies.get("_xsrf"):
        raise ValueError("В сессии hh нет авторизации или XSRF")
    if any(not isinstance(k, str) or not isinstance(v, str) or any(c in k + v for c in "\r\n;") for k, v in cookies.items()):
        raise ValueError("Некорректный формат сессии hh")
    return cookies


class HhApplication:
    def __init__(self, cookies, enabled=control.enabled, opener=None):
        self.cookies = cookies
        self.enabled = enabled
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.get_opener = opener or urllib.request.build_opener(SafeGetRedirect(enabled))
        self.chat_id = None

    def request(self, url, *, data=None, headers=None):
        target = urllib.parse.urlsplit(url)
        if target.scheme != "https" or target.netloc != "hh.ru":
            raise ValueError("Недопустимый адрес hh")
        if not self.enabled():
            raise Disabled("hh выключен")
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        request = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Cookie": cookie,
            "X-XSRFToken": self.cookies["_xsrf"], **(headers or {})})
        try:
            with (self.opener if data is not None else self.get_opener).open(request, timeout=25) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post(self, vacancy_id, resume_id, letter):
        if not str(vacancy_id).isdigit() or not resume_id or not letter.strip() or "\ufffd" in letter or "???" in letter:
            raise ValueError("Некорректные данные отклика")
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
        fields = {"_xsrf": self.cookies["_xsrf"], "vacancy_id": str(vacancy_id),
                  "resume_hash": resume_id, "ignore_postponed": "true", "letter": letter, "lux": "true"}
        data = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
        data = (data + f"--{boundary}--\r\n").encode("utf-8")
        status, raw = self.request("https://hh.ru/applicant/vacancy_response/popup", data=data, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://hh.ru/vacancy/{vacancy_id}", "Origin": "https://hh.ru"})
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            body = None
        if isinstance(body, dict):
            chat_id = str(body.get("chat_id") or "")
            self.chat_id = chat_id if chat_id.isdigit() else None
        return status, body

    def verify(self, vacancy_id, letter):
        if not self.chat_id:
            cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            for number in range(50):
                if not self.enabled():
                    raise Disabled("hh выключен")
                try:
                    page = scan.fetch(f"https://hh.ru/applicant/negotiations?filter=active&page={number}", cookie)
                except scan.StopScan as exc:
                    if "HTTP 401" in str(exc) or "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                        raise Restricted(str(exc)) from None
                    raise
                match = scan.STATE.search(page)
                state = json.loads(html.unescape(match.group(1))) if match else {}
                history = state.get("applicantNegotiations") or {}
                if history.get("filterInUse") != "active" or not isinstance(history.get("topicList"), list):
                    return None
                topic = next((t for t in history["topicList"] if str(t.get("vacancyId")) == str(vacancy_id)), None)
                if topic:
                    chat = str(topic.get("chatId") or "")
                    self.chat_id = chat if chat.isdigit() else None
                    break
                try:
                    if number + 1 >= int(history["pageCount"]):
                        break
                except (KeyError, ValueError, TypeError):
                    return None
            if not self.chat_id:
                return None
        chat = self.chat_state(self.chat_id)
        me = chat.get("currentParticipantId")
        items = (chat.get("messages") or {}).get("items")
        if me is None or not isinstance(items, list):
            return None
        own = [m.get("text", "").strip() for m in items if m.get("participantId") == me and m.get("text")]
        if letter.strip() in own:
            return True
        # Чат может быть неполным, поэтому отсутствие текста не доказывает несовпадение.
        return None

    def chat_state(self, chat_id):
        if not str(chat_id).isdigit():
            raise ValueError("invalid chat ID")
        status, raw = self.request("https://hh.ru/")
        if status in (401, 403, 429):
            raise Restricted(f"hh HTTP {status}")
        if status != 200:
            raise ValueError("hh build unavailable")
        match = re.search(r'"build"\s*:\s*"(\d+(?:\.\d+){2,})"|sentry-release=xhh(?:%40|@)(\d+(?:\.\d+){2,})', raw.decode("utf-8"))
        if not match:
            raise ValueError("hh build missing")
        version = match.group(1) or match.group(2)
        status, raw = self.request(f"https://hh.ru/chat/{chat_id}", headers={"X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json", "X-Static-Version": version})
        if status in (401, 403, 429):
            raise Restricted(f"hh HTTP {status}")
        if status != 200:
            raise ValueError("chat unavailable")
        state = json.loads(raw.decode("utf-8"))
        return ((state.get("chatData") or {}).get("chat") or {})
