"""Сравнить локальные ручные метки с версиями рейтинга, без сети."""

import json
import sqlite3
from pathlib import Path

from scan import ROOT


def main() -> None:
    path = ROOT / "data/review-2026-10-29.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    run_id = review["run_id"]
    with sqlite3.connect(ROOT / "data/vacancies.sqlite3") as db:
        current = {vid: json.loads(result) for vid, result in db.execute("SELECT vacancy_id,result FROM scores WHERE run_id=?", (run_id,))}
        older = {}
        for vid, result in db.execute("SELECT vacancy_id,result FROM score_history WHERE run_id=? ORDER BY id", (run_id,)):
            item = json.loads(result)
            if item.get("version") == "2026-10-29-v4":
                older[vid] = item
        observations = {vid: json.loads(data) for vid, data in db.execute("SELECT vacancy_id,data FROM observations WHERE run_id=?", (run_id,))}
    lines = ["# Ручная проверка оценок", "", "Разметка предварительная: только смысловое соответствие, без автоматического решения об отклике. Пограничные случаи не включены в подсчёт ошибок. Следующие 10 случаев не являются независимым контрольным набором: некоторые просматривались при предыдущей настройке.", ""]
    for label, group in (("Настройка: первые 20", review["labels"][:20]), ("Дополнительные 10: не независимый контроль", review["labels"][20:])):
        lines += [f"## {label}", ""]
        for version, results in (("v4", older), ("текущая", current)):
            false_positive = false_negative = choice_error = count = 0
            for entry in group:
                result = results.get(entry["id"])
                if entry["fit"] == "uncertain" or not result:
                    continue
                count += 1
                admitted = not result["below_threshold"]
                expected = entry["fit"] == "yes"
                false_positive += int(admitted and not expected)
                false_negative += int(not admitted and expected)
                choice_error += int(admitted and expected and result["selected"] != entry["profile"])
            lines.append(f"- {version}: проверено {count}, лишних допусков {false_positive}, пропущенных подходящих {false_negative}, неверный выбор резюме {choice_error}.")
        lines += [""]
        for entry in group:
            item = observations.get(entry["id"], {})
            result = current.get(entry["id"], {})
            current_label = "не оценена" if not result else "ниже порога" if result["below_threshold"] else "допущена: " + result["selected"]
            lines.append(f"- {item.get('name', entry['id'])} — {item.get('url', '')} | вручную: {entry['fit']} {entry['profile'] or ''} | скрипт: {current_label} | {entry['reason']}")
        lines.append("")
    target = ROOT / "reports/review.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Отчёт:", target)


if __name__ == "__main__":
    main()
