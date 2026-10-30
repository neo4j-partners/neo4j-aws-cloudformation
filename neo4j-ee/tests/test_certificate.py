"""Unit tests for the ACM helpers in scripts/certificate.py.

The verify helpers are the gate between operator-supplied --cert-arn and
stack creation: a cert that does not cover AdvertisedDNS makes the
published Neo4jBoltUrl break every client at first connect, so the
matching logic (including wildcard SANs) is worth pinning.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))

import certificate  # noqa: E402


class _FakeAcm:
    def __init__(self, *, domain_name: str = "", sans: list[str] | None = None) -> None:
        self._cert = {"DomainName": domain_name, "SubjectAlternativeNames": sans or []}

    def describe_certificate(self, *, CertificateArn: str) -> dict:
        return {"Certificate": dict(self._cert)}


class DnsMatchesCertNameTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(certificate._dns_matches_cert_name("neo4j.example.com", "neo4j.example.com"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(certificate._dns_matches_cert_name("Neo4j.Example.com", "neo4j.EXAMPLE.com"))

    def test_wildcard_matches_one_label(self) -> None:
        self.assertTrue(certificate._dns_matches_cert_name("neo4j.example.com", "*.example.com"))

    def test_wildcard_rejects_extra_labels(self) -> None:
        self.assertFalse(certificate._dns_matches_cert_name("a.b.example.com", "*.example.com"))

    def test_wildcard_rejects_bare_apex(self) -> None:
        self.assertFalse(certificate._dns_matches_cert_name("example.com", "*.example.com"))

    def test_unrelated_domain(self) -> None:
        self.assertFalse(certificate._dns_matches_cert_name("neo4j.example.com", "neo4j.other.com"))


class VerifyCertMatchesDnsTests(unittest.TestCase):
    def test_matches_via_domain_name(self) -> None:
        acm = _FakeAcm(domain_name="neo4j.example.com")
        certificate._verify_cert_matches_dns(acm, "arn:aws:acm:us-east-1:1:certificate/x", "neo4j.example.com")

    def test_matches_via_san(self) -> None:
        acm = _FakeAcm(domain_name="example.com", sans=["neo4j.example.com", "api.example.com"])
        certificate._verify_cert_matches_dns(acm, "arn:aws:acm:us-east-1:1:certificate/x", "neo4j.example.com")

    def test_matches_via_wildcard_san(self) -> None:
        acm = _FakeAcm(domain_name="example.com", sans=["*.example.com"])
        certificate._verify_cert_matches_dns(acm, "arn:aws:acm:us-east-1:1:certificate/x", "neo4j.example.com")

    def test_mismatch_raises_with_diagnostic(self) -> None:
        acm = _FakeAcm(domain_name="other.example.com", sans=["api.example.com"])
        with self.assertRaises(ValueError) as ctx:
            certificate._verify_cert_matches_dns(
                acm, "arn:aws:acm:us-east-1:1:certificate/x", "neo4j.example.com"
            )
        msg = str(ctx.exception)
        self.assertIn("neo4j.example.com", msg)
        self.assertIn("arn:aws:acm:us-east-1:1:certificate/x", msg)
        self.assertIn("other.example.com", msg)


if __name__ == "__main__":
    unittest.main()
