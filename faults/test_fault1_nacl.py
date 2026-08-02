import unittest

from fault1_nacl import (
    FaultScope,
    create_parameters,
    delete_parameters,
    inject_entries,
    inspect_topology,
    is_exact_entry,
    restore_entries,
    scope_document,
    validate_manifest_scope,
)


class FakeEc2:
    def __init__(self, nacls):
        self.nacls = {item["NetworkAclId"]: item for item in nacls}
        self.calls = []

    def create_network_acl_entry(self, **kwargs):
        self.calls.append(("create", kwargs))
        entry = {
            "RuleNumber": kwargs["RuleNumber"],
            "Egress": kwargs["Egress"],
            "Protocol": kwargs["Protocol"],
            "RuleAction": kwargs["RuleAction"],
            "CidrBlock": kwargs["CidrBlock"],
            "PortRange": kwargs["PortRange"],
        }
        self.nacls[kwargs["NetworkAclId"]]["Entries"].append(entry)

    def delete_network_acl_entry(self, **kwargs):
        self.calls.append(("delete", kwargs))
        entries = self.nacls[kwargs["NetworkAclId"]]["Entries"]
        self.nacls[kwargs["NetworkAclId"]]["Entries"] = [
            entry
            for entry in entries
            if not (
                entry["RuleNumber"] == kwargs["RuleNumber"]
                and entry["Egress"] is kwargs["Egress"]
            )
        ]


class Fault1Tests(unittest.TestCase):
    def setUp(self):
        self.scope = FaultScope(
            account_id="123456789012",
            region="us-west-2",
            vpc_id="vpc-123",
            source_cidr="10.10.0.0/16",
            private_subnet_ids=("subnet-a", "subnet-b"),
            nacl_ids=("acl-a", "acl-b"),
            rule_number=50,
        )
        self.baseline = [
            {
                "RuleNumber": 100,
                "Egress": False,
                "Protocol": "-1",
                "RuleAction": "allow",
                "CidrBlock": "0.0.0.0/0",
            },
            {
                "RuleNumber": 100,
                "Egress": True,
                "Protocol": "-1",
                "RuleAction": "allow",
                "CidrBlock": "0.0.0.0/0",
            },
        ]

    def nacls(self):
        return [
            {
                "NetworkAclId": "acl-a",
                "VpcId": "vpc-123",
                "Associations": [{"SubnetId": "subnet-a"}],
                "Entries": [dict(item) for item in self.baseline],
            },
            {
                "NetworkAclId": "acl-b",
                "VpcId": "vpc-123",
                "Associations": [{"SubnetId": "subnet-b"}],
                "Entries": [dict(item) for item in self.baseline],
            },
        ]

    def test_exact_entry_and_api_parameters(self):
        expected = create_parameters(self.scope, "acl-a")
        self.assertTrue(is_exact_entry(expected, self.scope))
        self.assertEqual(expected["Protocol"], "6")
        self.assertEqual(expected["PortRange"], {"From": 7070, "To": 7070})
        self.assertEqual(
            delete_parameters(self.scope, "acl-a"),
            {"NetworkAclId": "acl-a", "RuleNumber": 50, "Egress": False},
        )

    def test_round_trip_inject_and_restore_returns_to_exact_baseline(self):
        nacls = self.nacls()
        ec2 = FakeEc2(nacls)
        self.assertEqual(inspect_topology(self.scope, nacls)["state"], "healthy")

        created = inject_entries(ec2, self.scope)
        self.assertEqual(created, ["acl-a", "acl-b"])
        self.assertEqual(inspect_topology(self.scope, nacls)["state"], "fault1")

        deleted = restore_entries(ec2, self.scope, created)
        self.assertEqual(deleted, ["acl-a", "acl-b"])
        self.assertEqual(inspect_topology(self.scope, nacls)["state"], "healthy")
        self.assertEqual([name for name, _ in ec2.calls], ["create", "create", "delete", "delete"])

    def test_collision_is_detected_and_never_treated_as_exact(self):
        nacls = self.nacls()
        nacls[0]["Entries"].append(
            {
                "RuleNumber": 50,
                "Egress": False,
                "Protocol": "6",
                "RuleAction": "allow",
                "CidrBlock": "10.10.0.0/16",
                "PortRange": {"From": 7070, "To": 7070},
            }
        )
        result = inspect_topology(self.scope, nacls)
        self.assertEqual(result["state"], "collision")

    def test_partial_exact_state_is_visible_for_safe_recovery(self):
        nacls = self.nacls()
        nacls[0]["Entries"].append(
            {
                "RuleNumber": 50,
                "Egress": False,
                "Protocol": "6",
                "RuleAction": "deny",
                "CidrBlock": "10.10.0.0/16",
                "PortRange": {"From": 7070, "To": 7070},
            }
        )
        self.assertEqual(inspect_topology(self.scope, nacls)["state"], "partial")

    def test_out_of_scope_subnet_association_is_rejected(self):
        nacls = self.nacls()
        nacls[0]["Associations"].append({"SubnetId": "subnet-unrelated"})
        with self.assertRaisesRegex(RuntimeError, "out-of-scope subnets"):
            inspect_topology(self.scope, nacls)

    def test_restore_manifest_is_bound_to_current_terraform_scope(self):
        document = {
            "project": "eks-observability-lab",
            "fault": "fault1-nacl",
            "scope": scope_document(self.scope),
            "intended_nacl_ids": ["acl-a", "acl-b"],
        }
        validate_manifest_scope(document, self.scope)
        changed = dict(document)
        changed["scope"] = dict(document["scope"], source_cidr="10.11.0.0/16")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            validate_manifest_scope(changed, self.scope)


if __name__ == "__main__":
    unittest.main()
