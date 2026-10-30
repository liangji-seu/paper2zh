from __future__ import annotations

import unittest

from app.core import _no_proxy_with_host


class NoProxyTests(unittest.TestCase):
    def test_adds_api_host_and_keeps_existing_entries(self):
        self.assertEqual(_no_proxy_with_host("localhost,127.0.0.1", "api.deepseek.com"), "localhost,127.0.0.1,api.deepseek.com")

    def test_does_not_duplicate_host_case_insensitively(self):
        self.assertEqual(_no_proxy_with_host("localhost,API.DEEPSEEK.COM", "api.deepseek.com"), "localhost,API.DEEPSEEK.COM")

    def test_wildcard_stays_wildcard(self):
        self.assertEqual(_no_proxy_with_host("*", "api.deepseek.com"), "*")
        self.assertEqual(_no_proxy_with_host(None, "api.deepseek.com"), "api.deepseek.com")


if __name__ == "__main__":
    unittest.main()
