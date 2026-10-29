"""Простой предварительный рейтинг: совпадение роли, навыков и задач."""

import re

VERSION = "2026-10-29-v4"

ROLE = {
    "backend": ("python", "backend", "бэкенд", "бекенд", "бэкэнд"),
    "ai": (" ai ", "ai-", "ai/", "llm", "ml engineer", "ml-инженер", "искусственн", "машинного обучения"),
    "lead": ("tech lead", "team lead", "тимлид", "техлид", "ведущий", "руководитель разработки", "head of engineering", "cto", "технический директор"),
}
SKILLS = {
    "backend": ("python", "fastapi", "django", "postgresql", "sql", "redis", "rabbitmq", "celery", "docker", "pytest", "rest api"),
    "ai": ("python", "fastapi", "postgresql", "docker", "rag", "llm", "langchain", "langgraph", "openai", "pgvector", "pytest"),
    "lead": ("python", "fastapi", "django", "postgresql", "sql", "docker", "pytest", "redis", "rabbitmq", "rest api", "prometheus"),
}
FOCUS = {
    "backend": ("backend", "бэкенд", "backend-сервис", "интеграц", "api", "микросервис", "асинхрон"),
    "ai": ("rag", "llm", "агент", "векторн", "генеративн", "искусственн", "языков", "retrieval"),
    "lead": ("архитектур", "наставнич", "ментор", "code review", "ревью", "техническ", "команд", "лидер"),
}
OFF_ROLE = ("тестировщик", "тестирования", "автотестирован", "qa automation", "qa engineer", "sdet", "аналитик данных", "data analyst", "data scientist", "data engineer", "data engineering", "аналитик bi", "seo", "дизайнер", "frontend developer", "devops", "mlops", "revops", "информационной безопасности", "младший", "junior")


def contains(text: str, term: str) -> bool:
    if term.startswith(" "):
        return term in text
    if re.fullmatch(r"[a-z0-9+ #]+", term):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text))
    return term in text


def required_match(text: str, term: str) -> bool:
    """Не засчитывать простые отрицания рядом с названием навыка."""
    for sentence in re.split(r"[.;!?\n]", text):
        if not contains(sentence, term):
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])" if re.fullmatch(r"[a-z0-9+ #]+", term) else re.escape(term)
        for match in re.finditer(pattern, sentence):
            before = sentence[max(0, match.start() - 35):match.start()]
            after = sentence[match.end():match.end() + 30]
            if re.search(r"(?:не требуется|не нужен|не нужна|не обязательно|без знания)\s*$", before):
                continue
            if re.match(r"\s*(?:не требуется|не нужен|не нужна|не обязателен)", after):
                continue
            if contains(sentence, term):
                return True
    return False


def score_vacancy(title: str, description: str) -> dict:
    title_text = " " + title.lower() + " "
    text = (title + " " + description).lower()
    if not description.strip():
        return {"version": VERSION, "status": "unknown", "scores": {}, "selected": None, "reason": "нет описания вакансии"}
    unrelated = any(contains(title_text, word) for word in OFF_ROLE)
    ml_only = any(contains(title_text, word) for word in ("ml engineer", "ml-инженер", "машинного обучения")) and not any(contains(description.lower(), word) for word in ("llm", "rag", "агент", "large language model"))
    no_python = not required_match(description.lower(), "python") and not contains(title_text, "python")
    results = {}
    for profile in ("backend", "ai", "lead"):
        roles = [term.strip() for term in ROLE[profile] if contains(title_text, term)]
        skills = [term for term in SKILLS[profile] if required_match(description.lower(), term)]
        tasks = [term for term in FOCUS[profile] if contains(text, term)]
        role_points = 35 if roles else 0
        skill_points = min(40, len(skills) * 8)
        task_points = min(25, len(tasks) * 5)
        total = role_points + skill_points + task_points
        if unrelated or no_python or ml_only:
            total = min(total, 49)
        results[profile] = {"score": total, "role_points": role_points, "skill_points": skill_points, "task_points": task_points, "role_matches": roles, "skills": skills, "tasks": tasks, "title_mismatch": unrelated, "no_python_signal": no_python, "ml_without_llm_signal": ml_only}
    selected = max(results, key=lambda profile: (results[profile]["score"], results[profile]["role_points"], results[profile]["skill_points"], -list(results).index(profile)))
    return {"version": VERSION, "status": "scored", "scores": results, "selected": selected, "below_threshold": all(r["score"] < 50 for r in results.values())}
