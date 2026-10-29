import unittest

from form_handling import classify_apply_result, classify_form


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

    def test_unknown_error_does_not_authorize_retry(self):
        self.assertEqual(classify_apply_result("unrecognized").status, "unknown")


if __name__ == "__main__":
    unittest.main()
