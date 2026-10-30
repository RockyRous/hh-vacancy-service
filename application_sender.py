"""Механизм одной попытки. Транспорт передаётся вызывающим кодом; к боту не подключён."""

from datetime import datetime

import applications


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
    if applications.classify_response(http_status, body)[0] == "confirmed" and verify and enabled():
        try:
            verified = verify(vid, template["text"])
            if verified not in (True, False, None):
                verified = None
        except Exception:
            verified = None
    status, should_pause = applications.finish_attempt(db, vid, http_status, body, now, letter_verified=verified)
    if should_pause:
        pause()
    return status
