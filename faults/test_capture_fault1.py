import tempfile
import unittest
from pathlib import Path

from capture_fault1 import read_grafana_service_account_token


class GrafanaTokenTests(unittest.TestCase):
    def write_token_file(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "grafana.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_selects_labeled_service_account_without_using_ingest_token(self):
        path = self.write_token_file(
            "token = ingest-secret\n\ngrafana service account: service-account-secret\n"
        )
        self.assertEqual(read_grafana_service_account_token(path), "service-account-secret")

    def test_accepts_one_raw_token_and_rejects_ambiguous_content(self):
        self.assertEqual(read_grafana_service_account_token(self.write_token_file("raw-secret\n")), "raw-secret")
        with self.assertRaisesRegex(RuntimeError, "must contain"):
            read_grafana_service_account_token(self.write_token_file("token = ambiguous-secret\n"))


if __name__ == "__main__":
    unittest.main()
