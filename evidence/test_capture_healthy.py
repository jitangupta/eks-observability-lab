import json
import unittest

from capture_healthy import (
    alert_state,
    find_verification_check,
    parse_promtool_scalar,
    stamp_from_utc,
    summarize_pods,
)


class CaptureHealthyTests(unittest.TestCase):
    def test_stamp_from_utc_is_path_safe(self):
        self.assertEqual(stamp_from_utc("2026-08-02T16:01:27Z"), "20260802T160127Z")

    def test_parse_promtool_scalar(self):
        output = '{cluster="c1", job="cross-region-cart"} => 0.012345 @[1770000000.000]\n'
        self.assertEqual(parse_promtool_scalar(output), 0.012345)

    def test_parse_promtool_scalar_selects_one_labeled_series(self):
        output = (
            '{job="c1-frontend"} => 1 @[1770000000.000]\n'
            '{job="cross-region-cart"} => 0.012345 @[1770000000.000]\n'
        )
        self.assertEqual(
            parse_promtool_scalar(output, required_label='job="cross-region-cart"'),
            0.012345,
        )

    def test_parse_promtool_scalar_rejects_missing_value(self):
        with self.assertRaises(ValueError):
            parse_promtool_scalar("no data returned")

    def test_summarize_pods_records_readiness_and_restarts(self):
        document = {
            "items": [
                {
                    "metadata": {"name": "frontend-abc"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "restartCount": 2}],
                    },
                }
            ]
        }
        summary = summarize_pods(document)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["unhealthy"], [])
        self.assertEqual(summary["pods"][0]["restarts"], 2)

    def test_summarize_pods_flags_unready_pod(self):
        document = {
            "items": [
                {
                    "metadata": {"name": "cartservice-abc"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": False, "restartCount": 0}],
                    },
                }
            ]
        }
        self.assertEqual(summarize_pods(document)["unhealthy"], ["cartservice-abc"])

    def test_find_verification_check_requires_unique_match(self):
        document = {"checks": [{"id": "active-probes", "status": "PASS"}]}
        self.assertEqual(find_verification_check(document, "active-probes")["status"], "PASS")
        with self.assertRaises(RuntimeError):
            find_verification_check(document, "waf")

    def test_alert_state_prefers_status_then_label(self):
        self.assertEqual(alert_state({"status": {"state": "active"}}), "active")
        self.assertEqual(alert_state({"labels": {"alertstate": "normal"}}), "normal")
        self.assertEqual(alert_state(json.loads("{}")), "unknown")


if __name__ == "__main__":
    unittest.main()
