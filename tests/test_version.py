import re
import unittest

from tests.harness import ROOT

import opencloddie
from opencloddie import httpd, server


class VersionTest(unittest.TestCase):
    def test_all_versions_match(self):
        client = (ROOT / "opencloddie" / "client" / "opencloddie").read_text(encoding="utf-8")
        match = re.search(r'^VERSION = "([^"]+)"', client, re.MULTILINE)
        self.assertIsNotNone(match, "client VERSION not found")
        versions = {
            opencloddie.__version__,
            httpd.VERSION,
            server.VERSION,
            match.group(1),
        }
        self.assertEqual(len(versions), 1, f"version mismatch: {versions}")
        self.assertRegex(opencloddie.__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
