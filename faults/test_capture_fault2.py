import tempfile
import unittest
from pathlib import Path

from capture_fault2 import assert_cart_healthy, latest_passing_verifier, read_grafana_service_account_token


class CaptureTests(unittest.TestCase):
    def write_token(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "grafana.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_token_reader_never_selects_ingest_token(self):
        path = self.write_token("token = ingest-secret\n\ngrafana service account: api-secret\n")
        self.assertEqual(read_grafana_service_account_token(path), "api-secret")

    def test_cart_proof_requires_authorized_success_and_unauthorized_failure(self):
        report = {
            "checks": [
                {
                    "id": "active-probes",
                    "status": "PASS",
                    "detail": "Authorized probe connected; unauthorized probe was denied",
                    "evidence": {
                        "outcomes": {
                            "authorized": {"exit_code": 0},
                            "unauthorized": {"exit_code": 1},
                        }
                    },
                }
            ]
        }
        self.assertIn("connected", assert_cart_healthy(report)["detail"])
        report["checks"][0]["evidence"]["outcomes"]["authorized"]["exit_code"] = 1
        with self.assertRaisesRegex(RuntimeError, "did not prove"):
            assert_cart_healthy(report)

    def test_latest_passing_verifier_ignores_newer_failure(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "verification-fault2-20260802T000000Z.json").write_text(
            '{"state":"fault2","outcome":"PASS"}', encoding="utf-8"
        )
        (root / "verification-fault2-20260802T000100Z.json").write_text(
            '{"state":"fault2","outcome":"FAIL"}', encoding="utf-8"
        )
        selected = latest_passing_verifier(root, "fault2")
        self.assertIsNotNone(selected)
        self.assertTrue(selected[0].name.endswith("000000Z.json"))


if __name__ == "__main__":
    unittest.main()
