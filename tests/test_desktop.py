"""Regression guard for pywebview's recursive JS API inspection."""

import unittest

from desktop import DesktopBridge


class DesktopBridgeTests(unittest.TestCase):
    def test_only_import_method_is_public_to_pywebview(self):
        bridge = DesktopBridge()
        bridge._window = object()
        public = {name for name in dir(bridge) if not name.startswith("_")}
        self.assertEqual(public, {"import_pdf"})


if __name__ == "__main__":
    unittest.main()
