import json
import unittest
from pathlib import Path
from app.xbrl import inspect_report
class DomainTests(unittest.TestCase):
    def test_fact(self): self.assertEqual(inspect_report(Path("fixtures/report.xml").read_bytes())["facts"][0]["value"],"120")
    def test_context_reference(self): self.assertIn("c1",inspect_report(Path("fixtures/report.xml").read_bytes())["contexts"])
    def test_invalid_xml(self):
        with self.assertRaises(Exception): inspect_report(b"<xbrl")

class FixtureTests(unittest.TestCase):
    def test_fixture_directory(self): self.assertTrue(Path("fixtures").is_dir())
