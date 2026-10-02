import sys
import unittest

from app.security import protect, unprotect


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI requires Windows")
class NativeEncryptionTests(unittest.TestCase):
    def test_real_dpapi_round_trip(self):
        value = "paper2zh-local-test-中文"
        encrypted = protect(value)
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotIn(value, encrypted)
        self.assertEqual(unprotect(encrypted), value)


if __name__ == "__main__":
    unittest.main()
