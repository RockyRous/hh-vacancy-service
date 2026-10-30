"""Классификация дополнительных шагов hh без отправки откликов."""

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping


@dataclass(frozen=True)
class FormDecision:
    status: str  # manual, unknown, clear
    reason: str


class _QuestionFields(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = dict(attrs).get("name") or ""
        if tag in {"input", "textarea", "select"} and name.startswith("task_"):
            self.names.add(name)


class _ResponseForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.forms = 0
        self.safe = True
        self.xsrf = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form" and attr.get("name") == "vacancy_response":
            self.inside = True
            self.forms += 1
            if attr.get("method", "").lower() != "post" or attr.get("action"):
                self.safe = False
        elif self.inside and tag in {"input", "textarea", "select"}:
            if tag == "input" and attr.get("name") == "_xsrf" and attr.get("type", "").lower() == "hidden":
                self.xsrf = True
            else:
                self.safe = False

    def handle_endtag(self, tag):
        if tag == "form" and self.inside:
            self.inside = False


def is_simple_response_form(page: str) -> bool:
    """Узнать наблюдавшуюся простую форму; это не гарантирует отсутствие других шагов hh."""
    parser = _ResponseForm()
    parser.feed(page)
    return parser.forms == 1 and parser.safe and parser.xsrf and not parser.inside


def detect_questions_in_html(page: str) -> FormDecision:
    """Искать поля анкеты; их отсутствие не подтверждает отсутствия анкеты."""
    parser = _QuestionFields()
    parser.feed(page)
    if parser.names:
        return FormDecision("manual", f"application form contains {len(parser.names)} task fields")
    return FormDecision("unknown", "recognized question fields not found")


def classify_form(vacancy: Mapping[str, Any], *, preflight: str = "unknown") -> FormDecision:
    """Проверить известные признаки; отсутствие теста не доказывает отсутствие формы.

    preflight='clear' допустим только после отдельной подтверждённой проверки
    выбранным адаптером. Эта функция сама сетевых запросов не выполняет.
    """
    if preflight not in {"unknown", "clear", "questions"}:
        raise ValueError("unsupported preflight status")
    vacancy_type = vacancy.get("type")
    if vacancy.get("response_url") or (isinstance(vacancy_type, dict) and vacancy_type.get("id") == "direct"):
        return FormDecision("manual", "external application form")
    test = vacancy.get("test")
    if vacancy.get("has_test") is True or isinstance(test, dict):
        return FormDecision("manual", "test or questionnaire attached")
    if preflight == "questions":
        return FormDecision("manual", "employer questions detected")
    if preflight == "clear":
        return FormDecision("clear", "application form checked")
    return FormDecision("unknown", "additional questions could not be excluded")


def classify_apply_result(error_code: str) -> FormDecision:
    """Нормализовать известный отказ hh; неизвестный результат требует разбора."""
    if error_code.lower().replace("_", "-") == "test-required":
        return FormDecision("manual", "hh requires a test or questionnaire")
    return FormDecision("unknown", "unrecognized response; investigate before retry")
