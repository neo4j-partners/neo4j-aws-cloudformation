"""Unit tests for deploy.lookup_vpc_cidr (no AWS calls; fake ec2 client)."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402


class FakeEc2:
    def __init__(self, cidr: str | None = None, error: Exception | None = None):
        self.cidr = cidr
        self.error = error
        self.vpc_ids: list[str] = []

    def describe_vpcs(self, VpcIds: list[str]) -> dict[str, list[dict[str, str]]]:
        self.vpc_ids = VpcIds
        if self.error is not None:
            raise self.error
        return {"Vpcs": [{"VpcId": VpcIds[0], "CidrBlock": self.cidr}]}


class LookupVpcCidrTests(unittest.TestCase):
    def test_returns_primary_cidr(self) -> None:
        ec2 = FakeEc2(cidr="10.42.0.0/16")
        cidr = deploy.lookup_vpc_cidr("vpc-0123", "us-east-1", client=ec2)
        self.assertEqual(cidr, "10.42.0.0/16")
        self.assertEqual(ec2.vpc_ids, ["vpc-0123"])

    def test_missing_vpc_exits_with_region_hint(self) -> None:
        error = ClientError(
            {"Error": {"Code": "InvalidVpcID.NotFound", "Message": "x"}},
            "DescribeVpcs",
        )
        with self.assertRaises(SystemExit) as ctx:
            deploy.lookup_vpc_cidr("vpc-0123", "eu-west-1", client=FakeEc2(error=error))
        msg = str(ctx.exception.code)
        self.assertIn("vpc-0123", msg)
        self.assertIn("eu-west-1", msg)
        self.assertIn("--allowed-cidr", msg)


if __name__ == "__main__":
    unittest.main()
