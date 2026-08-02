import unittest

from verify import (
    endpoint_cidrs_are_restricted,
    find_nacl_fault_entries,
    has_permission,
    is_exact_fault_entry,
    permission_is_exact,
    public_ingress_sources,
)


class EndpointTests(unittest.TestCase):
    def test_accepts_narrow_ipv4_ranges(self):
        self.assertTrue(endpoint_cidrs_are_restricted(["203.0.113.7/32"]))
        self.assertTrue(endpoint_cidrs_are_restricted(["198.51.100.0/24"]))

    def test_rejects_world_ipv4_ipv6_and_broad_ranges(self):
        self.assertFalse(endpoint_cidrs_are_restricted(["0.0.0.0/0"]))
        self.assertFalse(endpoint_cidrs_are_restricted(["::/0"]))
        self.assertFalse(endpoint_cidrs_are_restricted(["10.0.0.0/16"]))


class NaclTests(unittest.TestCase):
    def setUp(self):
        self.entry = {
            "RuleNumber": 50,
            "Egress": False,
            "Protocol": "6",
            "RuleAction": "deny",
            "CidrBlock": "10.10.0.0/16",
            "PortRange": {"From": 7070, "To": 7070},
        }

    def test_finds_and_validates_exact_fault_entry(self):
        self.assertEqual(find_nacl_fault_entries([self.entry], 50), [self.entry])
        self.assertTrue(is_exact_fault_entry(self.entry, "10.10.0.0/16"))

    def test_rejects_wrong_direction_or_source(self):
        changed = dict(self.entry, Egress=True)
        self.assertFalse(is_exact_fault_entry(changed, "10.10.0.0/16"))
        self.assertFalse(is_exact_fault_entry(self.entry, "10.11.0.0/16"))


class SecurityGroupTests(unittest.TestCase):
    def test_exact_permission_and_public_source_detection(self):
        permissions = [{
            "IpProtocol": "tcp",
            "FromPort": 7070,
            "ToPort": 7070,
            "IpRanges": [{"CidrIp": "10.10.0.0/16"}],
            "Ipv6Ranges": [],
            "PrefixListIds": [],
            "UserIdGroupPairs": [],
        }]
        self.assertTrue(has_permission(permissions, protocol="tcp", from_port=7070, to_port=7070, cidr="10.10.0.0/16"))
        self.assertTrue(permission_is_exact(permissions[0], protocol="tcp", from_port=7070, to_port=7070, cidr="10.10.0.0/16"))
        self.assertEqual(public_ingress_sources(permissions), [])

        permissions[0]["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
        self.assertFalse(permission_is_exact(permissions[0], protocol="tcp", from_port=7070, to_port=7070, cidr="10.10.0.0/16"))
        self.assertEqual(public_ingress_sources(permissions), [{"protocol": "tcp", "cidr": "0.0.0.0/0"}])


if __name__ == "__main__":
    unittest.main()
