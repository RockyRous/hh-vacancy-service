"""Механизм одной попытки. Транспорт передаётся вызывающим кодом; к боту не подключён."""

from datetime import datetime

import applications
import application_history
import control
from hh_application import HhApplication, Restricted, session


def send_one_hh(db, *, db_path=control.DEFAULT_DB, **kwargs):
    """Подключить настоящий транспорт к механизму; все свидетельства обязательны."""
    if not control.enabled(db_path):
        return "off"
    client = HhApplication(session(), enabled=lambda: control.enabled(db_path))
    return send_one(db, enabled=lambda: control.enabled(db_path),
                    pause=lambda: control.set_enabled(False, db_path, reason="неясный исход отклика hh"),
                    post=client.post, verify=client.verify, **kwargs)


def send_one(db, *, item, score, profiles, templates, run_id, now, evidence,
             enabled, pause, post, verify=None):
    """Проверить условия, сохранить намерение и вызвать post максимум один раз.

    evidence — результаты проверок вызывающего кода, а не настройки пользователя.
    Сейчас полноценный источник этих свидетельств и сетевой транспорт не подключены.
    """
    if now.tzinfo is None:
        raise ValueError("timezone required")
    if not enabled():
        return "off"
    selected = score.get("selected")
    template = applications.select_letter(selected, templates)
    try:
        published = datetime.fromisoformat(item.get("published") or "")
        today = (published.tzinfo is not None and
                 published.astimezone(applications.MOSCOW).date() == now.astimezone(applications.MOSCOW).date())
    except (ValueError, TypeError):
        today = False
    salary = score.get("salary_from_description") or {}
    valid = (
        item.get("form_status") == "clear" and item.get("salary_status") == "pass"
        and not salary.get("below_threshold") and score.get("status") == "scored"
        and score.get("below_threshold") is False and score.get("version")
        and today and profiles.get(selected) and template and template.get("id")
        and template.get("version") and evidence.get("history_complete") is True
        and evidence.get("scoring_validated") is True and evidence.get("resume_snapshot")
        and evidence.get("vacancy_snapshot") and evidence.get("form_checked") is True
    )
    if not valid:
        return "blocked"
    snapshot = {"resume": evidence["resume_snapshot"], "vacancy": evidence["vacancy_snapshot"],
                "score": score, "form_status": item["form_status"]}
    vid = str(item["id"])
    if not applications.reserve_attempt(
        db, vacancy_id=vid, resume_id=profiles[selected], run_id=run_id,
        letter=template["text"], template_id=template["id"],
        template_version=str(template["version"]), now=now, snapshot=snapshot,
    ):
        return "duplicate_or_limit"
    if not enabled():
        with db:
            db.execute("UPDATE application_attempts SET status='not_sent',detail='disabled before request' WHERE vacancy_id=? AND status='sending'", (vid,))
        return "off"
    try:
        http_status, body = post(vid, profiles[selected], template["text"])
    except Exception:
        # Запрос мог дойти до hh; исключение никогда не означает разрешение на повтор.
        http_status, body = None, None
    verified = None
    verification_restricted = False
    if http_status in (200, 201) and isinstance(body, dict) and not body.get("error") and verify and enabled():
        try:
            verified = verify(vid, template["text"])
            if verified not in (True, False, None):
                verified = None
        except Restricted:
            verification_restricted = True
        except Exception:
            verified = None
    # Реальный формат success пока не зафиксирован; точное письмо — независимое подтверждение.
    if verified is True and body.get("success") is not False and applications.classify_response(http_status, body)[0] == "uncertain":
        body = {**body, "success": True}
    status, should_pause = applications.finish_attempt(db, vid, http_status, body, now, letter_verified=verified)
    if status == "manual":
        application_history.setup(db)
        application_history.queue_manual(db, vacancy_id=vid,
            url=item.get("url") or f"https://hh.ru/vacancy/{vid}", title=item.get("name") or vid,
            reason="hh требует тест или анкету", seen_at=now, profile=selected,
            resume_id=profiles[selected])
    if should_pause or verification_restricted:
        pause()
    return status
