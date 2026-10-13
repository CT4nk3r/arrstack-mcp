import logging
import unittest
from unittest.mock import Mock, patch

import httpx

from arrstack_mcp import server

SECRET = "s3cr3t-api-key-value"


class RedactTextTests(unittest.TestCase):
    def test_masks_query_and_form_credentials(self):
        text = "GET /sabnzbd/api?mode=queue&apikey=abcdef123&output=json token=xyz password=hunter2"
        masked = server._redact(text)
        for value in ("abcdef123", "xyz", "hunter2"):
            self.assertNotIn(value, masked)
        self.assertIn("mode=queue", masked)
        self.assertIn("apikey=***", masked)

    def test_does_not_touch_words_that_merely_contain_pass(self):
        self.assertEqual(server._redact("bypass=1"), "bypass=1")

    def test_masks_configured_secret_values_anywhere(self):
        with patch.object(server, "SONARR_API_KEY", SECRET):
            masked = server._redact(f'upstream said: bad header "X-Api-Key: {SECRET}"')
        self.assertNotIn(SECRET, masked)

    def test_short_secrets_are_not_masked_as_literals(self):
        with patch.object(server, "QBT_PASS", "admin"):
            self.assertEqual(server._redact("logged in as admin"), "logged in as admin")


class LoggingTests(unittest.TestCase):
    def test_httpx_request_logging_is_quiet(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_records_from_any_logger_are_redacted(self):
        with self.assertLogs("some.library", level="INFO") as captured:
            logging.getLogger("some.library").info("fetching %s", "http://x/api?apikey=abc123def")
        self.assertNotIn("abc123def", captured.output[0])
        self.assertIn("apikey=***", captured.output[0])

    def test_real_httpx_log_line_is_redacted(self):
        # Even if someone turns httpx logging back on, the URL key is masked.
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        httpx_logger = logging.getLogger("httpx")
        old_level = httpx_logger.level
        httpx_logger.setLevel(logging.INFO)
        try:
            with self.assertLogs("httpx", level="INFO") as captured:
                with httpx.Client(transport=transport) as client:
                    client.get("http://sab:8080/sabnzbd/api", params={"mode": "queue", "apikey": SECRET})
        finally:
            httpx_logger.setLevel(old_level)
        joined = "\n".join(captured.output)
        self.assertIn("HTTP Request", joined)
        self.assertNotIn(SECRET, joined)


class ToolOutputTests(unittest.TestCase):
    def test_http_error_body_is_redacted(self):
        response = Mock()
        response.status_code = 500
        response.text = f"error for apikey={SECRET}"
        error = httpx.HTTPStatusError("boom", request=Mock(), response=response)
        out = server._http_error("sab", error)
        self.assertNotIn(SECRET, out)
        self.assertIn("HTTP 500", out)

    def test_request_error_text_is_redacted(self):
        error = httpx.ConnectError(f"failed for http://sab/api?apikey={SECRET}")
        self.assertNotIn(SECRET, server._http_error("sab", error))


if __name__ == "__main__":
    unittest.main()
