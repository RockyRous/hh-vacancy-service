import unittest
import html
import json

from scoring import salary_upper_from_text, score_vacancy
from score_run import vacancy_text
from scan import StopScan


class ScoringTests(unittest.TestCase):
    def test_python_backend_is_routed_to_backend(self):
        result = score_vacancy("Senior Python Backend Developer", "Python FastAPI PostgreSQL SQL Docker Redis. Разработка backend API и интеграций.")
        self.assertEqual(result["selected"], "backend")
        self.assertGreaterEqual(result["scores"]["backend"]["score"], 50)
        self.assertEqual(len(result["scores"]), 3)

    def test_ai_role_routes_to_ai(self):
        result = score_vacancy("AI Engineer", "Python FastAPI LLM RAG LangChain pgvector агенты и векторный поиск.")
        self.assertEqual(result["selected"], "ai")

    def test_qa_title_caps_all_profiles(self):
        result = score_vacancy("Python QA Automation / SDET", "Python FastAPI PostgreSQL SQL Docker RAG LLM, интеграции и архитектура команды")
        self.assertTrue(result["below_threshold"])
        self.assertTrue(all(value["score"] < 50 for value in result["scores"].values()))

    def test_missing_description_is_unknown(self):
        self.assertEqual(score_vacancy("Python Developer", "")["status"], "unknown")

    def test_extracts_and_cleans_hh_vacancy_description(self):
        value = {"vacancyView": {"vacancyFull": {"vacancy": {"description": "<p>Python &amp; FastAPI</p>"}}}}
        page = '<template id="HH-Lux-InitialState">' + html.escape(json.dumps(value)) + '</template>'
        self.assertEqual(vacancy_text(page), "Python & FastAPI")
        with self.assertRaises(StopScan):
            vacancy_text("<html>unrelated</html>")

    def test_lead_data_engineer_does_not_become_lead_backend(self):
        result = score_vacancy("Senior/Tech Lead Data Engineer", "Python PostgreSQL Docker архитектура команда code review")
        self.assertTrue(result["below_threshold"])

    def test_ai_role_without_python_is_below_threshold(self):
        result = score_vacancy("AI RevOps Specialist", "LLM RAG агенты LangChain Docker FastAPI PostgreSQL")
        self.assertTrue(result["below_threshold"])

    def test_ai_engineering_in_security_is_not_excluded_by_domain(self):
        result = score_vacancy("Старший инженер по информационной безопасности (AI)", "Python, RAG, LLM, LangChain: разработка AI агентов и интеграций")
        self.assertGreaterEqual(result["scores"]["ai"]["score"], 50)

    def test_backend_tech_lead_can_match_even_if_language_unspecified(self):
        result = score_vacancy("Senior Backend Engineer / Tech Lead", "Backend API интеграции и асинхронная обработка. SQL Docker, архитектура, code review, команда.")
        self.assertGreaterEqual(result["scores"]["lead"]["score"], 50)

    def test_node_only_backend_lead_is_not_accepted_for_python(self):
        result = score_vacancy("Lead Backend Engineer", "Node.js TypeScript PostgreSQL Redis Docker, архитектура, API, интеграции, команда и code review.")
        self.assertTrue(result["below_threshold"])

    def test_rust_role_with_legacy_python_is_not_a_python_role(self):
        result = score_vacancy("Middle Backend разработчик (Rust/С++)", "Основной язык Rust, часть legacy сервисов написана на Python. API SQL Docker архитектура.")
        self.assertTrue(result["below_threshold"])

    def test_infrastructure_cto_without_python_is_not_admitted(self):
        result = score_vacancy("Технический директор (CTO)", "Управление серверной инфраструктурой, архитектура, команда, Docker, SQL, code review")
        self.assertTrue(result["below_threshold"])

    def test_test_automation_is_below_threshold(self):
        result = score_vacancy("Инженер по автоматизации тестирования", "Python FastAPI PostgreSQL SQL Docker и API")
        self.assertTrue(result["below_threshold"])

    def test_cv_model_training_without_llm_is_below_threshold(self):
        result = score_vacancy("ML Engineer / Инженер машинного обучения", "Python обучение моделей компьютерного зрения, PostgreSQL, Docker, FastAPI")
        self.assertTrue(result["below_threshold"])

    def test_python_does_not_match_pythons(self):
        result = score_vacancy("Backend Developer", "Pythons are reptiles")
        self.assertNotIn("python", result["scores"]["backend"]["skills"])

    def test_negated_skill_not_counted(self):
        result = score_vacancy("Backend Developer", "Не требуется Python. Docker не требуется. Нужен PostgreSQL.")
        skills = result["scores"]["backend"]["skills"]
        self.assertNotIn("python", skills)
        self.assertNotIn("docker", skills)
        self.assertIn("postgresql", skills)

    def test_explicit_salary_range_in_description(self):
        result = salary_upper_from_text("доход 120 000 – 170 000 ₽ на руки", {"noCompensation": {}})
        self.assertEqual(result["to"], 170000)
        self.assertTrue(result["below_threshold"])
        boundary = salary_upper_from_text("оклад 150 000 – 200 000 ₽", {})
        self.assertFalse(boundary["below_threshold"])

    def test_ambiguous_or_other_currency_not_extracted(self):
        self.assertIsNone(salary_upper_from_text("доход 120 000 – 170 000 USD", {}))
        self.assertIsNone(salary_upper_from_text("зарплата 100 000 – 150 000 ₽ или зарплата 200 000 – 300 000 ₽", {}))
        self.assertIsNone(salary_upper_from_text("оклад 100 000 – 150 000 ₽", {"currencyCode": "USD"}))

    def test_existing_upper_bound_not_overwritten(self):
        self.assertIsNone(salary_upper_from_text("оклад 100 000 – 150 000 ₽", {"to": 250000, "currencyCode": "RUR"}))


if __name__ == "__main__":
    unittest.main()
