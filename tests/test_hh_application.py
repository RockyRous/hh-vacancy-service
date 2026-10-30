import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from hh_application import Disabled, HhApplication, NoRedirect


class Response:
    status = 200
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self.body


class HhApplicationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.client = HhApplication({"hhtoken": "fake", "_xsrf": "fake"}, opener=self)

    def open(self, request, timeout):
        self.requests.append(request)
        return Response(json.dumps({"success": True, "chat_id": 42}).encode())

    def test_single_utf8_post_and_fields(self):
        status, body = self.client.post("123", "resume", "Здравствуйте!")
        self.assertEqual((status, body["success"]), (200, True))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.full_url, "https://hh.ru/applicant/vacancy_response/popup")
        self.assertIn("Здравствуйте!".encode(), request.data)
        self.assertIn(b'name="resume_hash"', request.data)
        self.assertEqual(self.client.chat_id, "42")

    def test_error_and_no_redirect_or_retry(self):
        def fail(request, timeout):
            self.requests.append(request)
            raise HTTPError(request.full_url, 400, "error", {}, io.BytesIO(b'{"error":"test-required"}'))
        with patch.object(self.client.opener, "open", fail):
            self.assertEqual(self.client.post("123", "r", "text"), (400, {"error": "test-required"}))
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_off_blocks_every_request(self):
        self.client.enabled = lambda: False
        with self.assertRaises(Disabled):
            self.client.post("123", "r", "text")
        self.assertEqual(self.requests, [])

    def test_verification_reads_only_own_exact_letter(self):
        self.client.chat_id = "42"
        state = {"chatData": {"chat": {"currentParticipantId": 1, "messages": {"items": [
            {"participantId": 2, "text": "Здравствуйте!"}, {"participantId": 1, "text": "Здравствуйте!"}]}}}}
        page = ('<template id="HH-Lux-InitialState">' + json.dumps(state, ensure_ascii=False) + '</template>').encode()
        with patch.object(self.client.opener, "open", return_value=Response(page)):
            self.assertTrue(self.client.verify("123", "Здравствуйте!"))
            self.assertIsNone(self.client.verify("123", "Другое письмо"))


if __name__ == "__main__":
    unittest.main()
