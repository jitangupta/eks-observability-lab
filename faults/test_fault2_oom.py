import copy
import unittest

from fault2_oom import (
    FAULT_MEMORY,
    container_index,
    fault_resources,
    memory_bytes,
    restoration_stage,
    fault_strategy,
    workload_patch,
    summarize_fault_pods,
)


def deployment(resources=None):
    return {
        "metadata": {
            "name": "productcatalogservice",
            "namespace": "online-boutique",
            "uid": "uid-1",
            "resourceVersion": "42",
        },
        "spec": {
            "replicas": 1,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
            },
            "template": {
                "metadata": {"labels": {"app": "productcatalogservice"}},
                "spec": {
                    "containers": [
                        {"name": "sidecar", "resources": {}},
                        {
                            "name": "server",
                            "resources": resources
                            or {
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "128Mi"},
                            },
                        },
                    ]
                },
            },
        },
    }


class ResourceTests(unittest.TestCase):
    def test_memory_quantities_and_locked_fault(self):
        self.assertEqual(memory_bytes("1Mi"), 2**20)
        self.assertEqual(memory_bytes("128Mi"), 128 * 2**20)
        self.assertEqual(memory_bytes("1Gi"), 2**30)
        original = deployment()["spec"]["template"]["spec"]["containers"][1]["resources"]
        baseline = copy.deepcopy(original)
        fault = fault_resources(original)
        self.assertEqual(original, baseline)
        self.assertEqual(fault["requests"], {"cpu": "100m", "memory": FAULT_MEMORY})
        self.assertEqual(fault["limits"], {"cpu": "200m", "memory": FAULT_MEMORY})

    def test_refuses_an_already_low_baseline(self):
        current = {"requests": {"memory": "4Mi"}, "limits": {"memory": "4Mi"}}
        with self.assertRaisesRegex(RuntimeError, "already at or below"):
            fault_resources(current)

    def test_json_patch_is_atomic_and_bound_to_resource_version_and_resources(self):
        current = deployment()
        replacement = fault_resources(current["spec"]["template"]["spec"]["containers"][1]["resources"])
        strategy = fault_strategy(current["spec"]["strategy"])
        patch = workload_patch(current, replacement, strategy)
        self.assertEqual(container_index(current), 1)
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "42"})
        self.assertEqual(patch[1]["op"], "test")
        self.assertEqual(patch[2]["op"], "replace")
        self.assertEqual(patch[2]["value"], replacement)
        self.assertEqual(patch[3]["op"], "test")
        self.assertEqual(patch[4], {"op": "replace", "path": "/spec/strategy", "value": {"type": "Recreate"}})


class PodEvidenceTests(unittest.TestCase):
    def test_summarizes_complete_oom_crashloop_evidence(self):
        pods = {
            "items": [
                {
                    "metadata": {"name": "productcatalogservice-new", "creationTimestamp": "2026-08-02T00:00:00Z"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {
                                "name": "server",
                                "ready": False,
                                "restartCount": 3,
                                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                "lastState": {
                                    "terminated": {
                                        "reason": "OOMKilled",
                                        "exitCode": 137,
                                        "startedAt": "2026-08-02T00:00:01Z",
                                        "finishedAt": "2026-08-02T00:00:01Z",
                                    }
                                },
                            }
                        ],
                    },
                }
            ]
        }
        result = summarize_fault_pods(pods)
        self.assertTrue(result["oomkilled"])
        self.assertTrue(result["exit_137"])
        self.assertTrue(result["crash_loop_backoff"])
        self.assertEqual(result["max_restarts"], 3)


class RestorationTests(unittest.TestCase):
    def setUp(self):
        self.previous = {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}
        self.injected = {"requests": {"memory": "4Mi"}, "limits": {"memory": "4Mi"}}
        self.rolling = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"}}
        self.recreate = {"type": "Recreate"}

    def stage(self, resources, strategy):
        return restoration_stage(resources, strategy, self.previous, self.rolling, self.injected, self.recreate)

    def test_recognizes_all_journaled_recovery_stages(self):
        self.assertEqual(self.stage(self.injected, self.recreate), "resources")
        self.assertEqual(self.stage(self.injected, self.rolling), "resources")
        self.assertEqual(self.stage(self.previous, self.recreate), "strategy")
        self.assertEqual(self.stage(self.previous, self.rolling), "complete")

    def test_refuses_unjournaled_drift(self):
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite drift"):
            self.stage({"limits": {"memory": "32Mi"}}, self.recreate)


if __name__ == "__main__":
    unittest.main()
