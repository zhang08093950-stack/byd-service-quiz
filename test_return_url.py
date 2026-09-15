#!/usr/bin/env python3
"""Tests for the ?return= allowlist — the one security-relevant bit of the
return-to-TQC support.

TQC links here with ?return=<its own item page> so the inspector can get back
after answering. That value is attacker-controllable, so an unvalidated
implementation would turn this app into an open redirect.

Run: python3 test_return_url.py
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "service_quiz_app", os.path.join(HERE, "sp_quiz", "app.py"))
quiz = importlib.util.module_from_spec(spec)
sys.modules["service_quiz_app"] = quiz
spec.loader.exec_module(quiz)


class SafeReturnUrlTests(unittest.TestCase):
    def test_accepts_the_tqc_host(self):
        url = "https://byd-tqc.onrender.com/item/C-2-8?country=Uruguay&quarter=2026 Q3"
        self.assertEqual(quiz.safe_return_url(url), url)

    def test_accepts_localhost_for_development(self):
        for url in ("http://localhost:5000/item/C-2-8",
                    "http://127.0.0.1:5000/item/C-2-8"):
            self.assertEqual(quiz.safe_return_url(url), url)

    def test_rejects_missing_value(self):
        for raw in (None, "", "   "):
            self.assertIsNone(quiz.safe_return_url(raw))

    def test_rejects_foreign_hosts(self):
        self.assertIsNone(quiz.safe_return_url("https://evil.example/phish"))

    def test_rejects_protocol_relative(self):
        # No scheme/netloc — must not be treated as a same-domain path.
        self.assertIsNone(quiz.safe_return_url("//evil.example/phish"))

    def test_rejects_non_http_schemes(self):
        for raw in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                    "file:///etc/passwd"):
            self.assertIsNone(quiz.safe_return_url(raw))

    def test_rejects_lookalike_hosts(self):
        for raw in ("https://byd-tqc.onrender.com.evil.example/",
                    "https://evil-byd-tqc.onrender.com/",
                    "https://notbyd-tqc.onrender.com/"):
            self.assertIsNone(quiz.safe_return_url(raw))

    def test_rejects_embedded_credentials(self):
        # userinfo must not be able to smuggle a different host past the check.
        self.assertIsNone(
            quiz.safe_return_url("https://byd-tqc.onrender.com@evil.example/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
