import unittest
import json
from pathlib import Path

from form_handling import classify_apply_result, classify_form, detect_questions_in_html


class FormHandlingTests(unittest.TestCase):
    def test_known_test_is_manual_even_when_preflight_claims_clear(self):
        self.assertEqual(classify_form({"test": {"required": True}}, preflight="clear").status, "manual")

    def test_external_form_is_manual(self):
        self.assertEqual(classify_form({"response_url": "https://example.com/apply"}).status, "manual")

    def test_missing_test_field_is_not_proof_of_no_questions(self):
        self.assertEqual(classify_form({"test": None}).status, "unknown")

    def test_verified_clear_and_questions(self):
        self.assertEqual(classify_form({}, preflight="clear").status, "clear")
        self.assertEqual(classify_form({}, preflight="questions").status, "manual")

    def test_api_and_website_test_errors_are_manual(self):
        for code in ("test_required", "test-required"):
            with self.subTest(code=code):
                self.assertEqual(classify_apply_result(code).status, "manual")

    def test_observed_x5_error_body_is_manual(self):
        fixture = Path(__file__).parent / "fixtures" / "hh-test-required.json"
        body = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(classify_apply_result(body["error"]).status, "manual")

    def test_detects_actual_radio_and_textarea_patterns(self):
        fixture = Path(__file__).parent / "fixtures" / "hh-form-fields.html"
        result = detect_questions_in_html(fixture.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "manual")
        self.assertIn("2 task fields", result.reason)

    def test_unrecognized_page_is_not_assumed_clear(self):
        self.assertEqual(detect_questions_in_html("<form></form>").status, "unknown")

    def test_unknown_error_does_not_authorize_retry(self):
        self.assertEqual(classify_apply_result("unrecognized").status, "unknown")


if __name__ == "__main__":
    unittest.main()
