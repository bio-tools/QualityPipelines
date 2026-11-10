import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from resqui.core import CheckResult, Context
from resqui.plugins.fairchecker import FAIRChecker

EXAMPLE_JSON = os.path.join(
    os.path.dirname(__file__), "..", "tmp", "fair-checker-example.json"
)


def _load_example():
    with open(EXAMPLE_JSON) as f:
        return json.load(f)


def _fake_requests_get_factory(report):
    def fake_get(url, params=None, timeout=None):
        return SimpleNamespace(
            status_code=200,
            json=lambda: report,
            raise_for_status=lambda: None,
        )
    return fake_get


class TestFAIRCheckerPlugin(unittest.TestCase):

    def _make_plugin(self, report):
        plugin = FAIRChecker.__new__(FAIRChecker)
        plugin.context = Context(github_token="token")
        plugin.executor = SimpleNamespace()
        plugin._cache = {}
        plugin._container = None
        plugin._base_url = "http://127.0.0.1:5000"
        # skip container startup; serve the fake report instead
        plugin._start_container = lambda: None
        plugin._fake_get = _fake_requests_get_factory(report)
        return plugin

    @patch("resqui.plugins.fairchecker.requests.get")
    def test_parse_example_report(self, mock_get):
        plugin = self._make_plugin(_load_example())
        mock_get.side_effect = plugin._fake_get

        result = plugin.unique_ids("https://github.com/computproteomics/OmicsQ", "main")

        self.assertIn("F1A", plugin._cache[("https://github.com/computproteomics/OmicsQ", "main")])
        self.assertEqual(result.output, "true")
        self.assertTrue(result.success)
        self.assertEqual(result.status_id, "schema:CompletedActionStatus")
        self.assertIn("F1A", result.evidence)

    @patch("resqui.plugins.fairchecker.requests.get")
    def test_all_indicator_methods(self, mock_get):
        plugin = self._make_plugin(_load_example())
        mock_get.side_effect = plugin._fake_get

        for method, abbreviation in [
            ("unique_ids", "F1A"),
            ("persistent_ids", "F1B"),
            ("structured_metadata", "F2A"),
            ("shared_vocabularies_for_metadata", "F2B"),
            ("machine_readable_format", "I1"),
            ("use_shared_ontologies", "I2"),
            ("external_links", "I3"),
            ("license_metadata", "R11"),
        ]:
            result = getattr(plugin, method)(
                "https://github.com/computproteomics/OmicsQ", "main"
            )
            self.assertIsInstance(result, CheckResult)
            self.assertIn(abbreviation, result.process)

    @patch("resqui.plugins.fairchecker.requests.get")
    def test_score_mapping(self, mock_get):
        plugin = self._make_plugin(_load_example())
        mock_get.side_effect = plugin._fake_get
        report = plugin.execute("https://github.com/computproteomics/OmicsQ", "main")
        # F1A has score 2 -> success, output "true"
        self.assertTrue(report["F1A"].success)
        self.assertEqual(report["F1A"].output, "true")
        # F2A has score 0 -> failure, output "false"
        self.assertFalse(report["F2A"].success)
        self.assertEqual(report["F2A"].output, "false")


if __name__ == "__main__":
    unittest.main()
