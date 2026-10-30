#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Request and issue an ACM certificate for Neo4j stack deployment.

Typical usage:

    # Real DNS-validated cert, auto-create the Route 53 validation record:
    ./scripts/certificate.py --region us-east-1 --dns neo4j.example.com --auto-route53

    # Real DNS-validated cert, print the CNAME and poll for issuance:
    ./scripts/certificate.py --region us-east-1 --dns neo4j.example.com

    # Print the CNAME only; add to DNS and rerun later:
    ./scripts/certificate.py --region us-east-1 --dns neo4j.example.com --no-wait

    # Self-signed cert imported into ACM (for testing; clients use neo4j+ssc://):
    ./scripts/certificate.py --region us-east-1 --dns neo4j.test.local --selfsign

On success the script writes a record to ``neo4j-ee/.deploy/cert-<sanitized-dns>.json``
with ``{cert_arn, domain_name, region, self_signed}`` and prints a ready-to-paste
deploy.py command.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import boto3


SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent / ".deploy"


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "Request an ACM certificate for Neo4j stack deployment. "
            "Without --selfsign, requests a DNS-validated public cert and prints the "
            "CNAME (or auto-creates it in Route 53 with --auto-route53). With "
            "--selfsign, generates and imports a self-signed cert for testing. "
            "Writes the result to neo4j-ee/.deploy/cert-<dns>.json."
        ),
    )
    p.add_argument(
        "--region",
        required=True,
        metavar="REGION",
        help="AWS region. Must match the region you will pass to deploy.py.",
    )
    p.add_argument(
        "--dns",
        required=True,
        metavar="DNS_NAME",
        help=(
            "DNS name for the certificate SAN. Any subdomain you control works — "
            "for example neo4j-test.example.com if example.com is in Route 53. "
            "This becomes the --advertised-dns value for deploy.py; clients connect "
            "to neo4j+s://<dns>:7687 (or neo4j+ssc:// for --selfsign). "
            "After the stack deploys, you must still create a Route 53 alias or "
            "CNAME pointing this name to the NLB."
        ),
    )
    p.add_argument(
        "--auto-route53",
        action="store_true",
        help=(
            "Automatically create the DNS validation CNAME in Route 53. "
            "Looks up the hosted zone whose name is the longest matching suffix of "
            "--dns. Requires route53:ListHostedZones and "
            "route53:ChangeResourceRecordSets. Ignored with --selfsign."
        ),
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "Print the validation CNAME and exit without waiting for issuance. "
            "Add the CNAME to your DNS provider, then rerun (without --no-wait) "
            "to poll for completion. Ignored with --selfsign."
        ),
    )
    p.add_argument(
        "--selfsign",
        action="store_true",
        help=(
            "Generate a self-signed certificate and import it into ACM. "
            "No domain ownership or DNS validation required. "
            "For testing only: clients must connect with neo4j+ssc:// (skip cert "
            "validation) instead of neo4j+s://."
        ),
    )
    return p.parse_args()


def _import_self_signed(acm, domain_name: str) -> str:
    """Generate a self-signed cert with openssl and import it into ACM."""
    with tempfile.TemporaryDirectory() as tmp:
        key_path = os.path.join(tmp, "key.pem")
        cert_path = os.path.join(tmp, "cert.pem")
        subprocess.run(
            [
                "openssl", "req", "-x509",
                "-newkey", "rsa:2048",
                "-keyout", key_path,
                "-out", cert_path,
                "-days", "365",
                "-nodes",
                "-subj", f"/CN={domain_name}",
                "-addext", f"subjectAltName=DNS:{domain_name}",
            ],
            check=True,
            capture_output=True,
        )
        cert_pem = Path(cert_path).read_bytes()
        key_pem = Path(key_path).read_bytes()

    resp = acm.import_certificate(Certificate=cert_pem, PrivateKey=key_pem)
    return resp["CertificateArn"]


def _dns_matches_cert_name(dns_name: str, pattern: str) -> bool:
    """Match dns_name against a cert CN/SAN, supporting one leading wildcard label."""
    dns_name = dns_name.lower()
    pattern = pattern.lower()
    if pattern == dns_name:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return dns_name.endswith(suffix) and dns_name.count(".") == pattern.count(".")
    return False


def _verify_cert_matches_dns(acm, cert_arn: str, dns_name: str) -> None:
    """Verify the ACM cert's CN or SubjectAlternativeNames include dns_name.

    Defense-in-depth check before stack creation: the published Neo4jBoltUrl
    uses AdvertisedDNS as the host. A cert whose CN/SAN does not cover that
    host produces a TLS validation failure on every client connection, exactly
    the footgun the published bolt URL is meant to remove.
    """
    resp = acm.describe_certificate(CertificateArn=cert_arn)
    cert = resp.get("Certificate", {})
    sans = cert.get("SubjectAlternativeNames") or []
    candidates = [n for n in [cert.get("DomainName", ""), *sans] if n]
    if any(_dns_matches_cert_name(dns_name, c) for c in candidates):
        return
    raise ValueError(
        f"ACM certificate {cert_arn} CN/SANs {candidates} do not cover "
        f"AdvertisedDNS={dns_name!r}. The published Neo4jBoltUrl uses "
        f"AdvertisedDNS as the host, so every client connection would fail "
        f"TLS validation. Pass --advertised-dns that matches the cert, or "
        f"supply a cert whose SAN covers the advertised DNS name."
    )


def _find_existing_cert(acm, domain_name):
    paginator = acm.get_paginator("list_certificates")
    for page in paginator.paginate(
        CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]
    ):
        for cert in page["CertificateSummaryList"]:
            if cert.get("DomainName") == domain_name:
                return cert["CertificateArn"]
    return None


def _wait_for_cname(acm, cert_arn, timeout=120):
    """Poll until ACM populates the validation CNAME (typically 30-60s)."""
    deadline = time.time() + timeout
    print("  Waiting for validation CNAME...", end="", flush=True)
    while time.time() < deadline:
        resp = acm.describe_certificate(CertificateArn=cert_arn)
        cert = resp["Certificate"]
        status = cert.get("Status")
        if status in ("FAILED", "REVOKED", "INACTIVE", "EXPIRED"):
            print()
            failure = cert.get("FailureReason", "unknown")
            sys.exit(
                f"ERROR: Certificate entered status {status} (reason: {failure}). "
                "This often means ACM cannot validate the domain — confirm you own "
                "it and can create DNS records for it."
            )
        opts = cert.get("DomainValidationOptions", [])
        if opts and opts[0].get("ResourceRecord"):
            print(" ready.", flush=True)
            return opts[0]["ResourceRecord"]
        print(".", end="", flush=True)
        time.sleep(10)
    print()
    sys.exit("ERROR: Timed out waiting for ACM validation CNAME after 2 minutes.")


def _find_route53_zone(route53, domain_name):
    paginator = route53.get_paginator("list_hosted_zones")
    best_zone = None
    best_len = 0
    domain_dot = domain_name.rstrip(".") + "."
    for page in paginator.paginate():
        for zone in page["HostedZones"]:
            zone_name = zone["Name"]
            if domain_dot.endswith(zone_name) and len(zone_name) > best_len:
                best_zone = zone
                best_len = len(zone_name)
    return best_zone


def _create_route53_cname(route53, zone_id, cname_name, cname_value):
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": cname_name,
                        "Type": "CNAME",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": cname_value}],
                    },
                }
            ]
        },
    )


def _wait_for_issuance(acm, cert_arn, timeout=900):
    """Poll until the certificate status is ISSUED."""
    deadline = time.time() + timeout
    print("Waiting for issuance ", end="", flush=True)
    while time.time() < deadline:
        resp = acm.describe_certificate(CertificateArn=cert_arn)
        status = resp["Certificate"]["Status"]
        if status == "ISSUED":
            print(f" {status}", flush=True)
            return
        if status in ("FAILED", "REVOKED", "INACTIVE", "EXPIRED"):
            print()
            sys.exit(f"ERROR: Certificate entered terminal status: {status}")
        print(".", end="", flush=True)
        time.sleep(10)
    print()
    sys.exit(
        "ERROR: Timed out waiting for certificate issuance (15 minutes). "
        "Check that the validation CNAME is in place, then rerun."
    )


def _sanitized_dns(domain_name: str) -> str:
    return domain_name.replace(".", "-")


def _write_deploy_record(domain_name: str, region: str, cert_arn: str, self_signed: bool) -> Path:
    DEPLOY_DIR.mkdir(exist_ok=True)
    path = DEPLOY_DIR / f"cert-{_sanitized_dns(domain_name)}.json"
    payload = {
        "cert_arn": cert_arn,
        "domain_name": domain_name,
        "region": region,
        "self_signed": self_signed,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def main():
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()

    acm = boto3.client("acm", region_name=args.region)
    domain_name = args.dns

    if args.selfsign:
        print(f"Generating self-signed certificate for {domain_name} in {args.region}...")
        cert_arn = _import_self_signed(acm, domain_name)
        print(f"  Certificate ARN: {cert_arn}")
        record_path = _write_deploy_record(domain_name, args.region, cert_arn, self_signed=True)
        print(f"  Wrote {record_path}")
        print()
        print("WARNING: self-signed cert — clients must use neo4j+ssc:// not neo4j+s://")
        print()
        print("To deploy:")
        print(
            f"  ./deploy.py --cert-arn {cert_arn} --advertised-dns {domain_name}"
        )
        return

    existing_arn = _find_existing_cert(acm, domain_name)
    if existing_arn:
        resp = acm.describe_certificate(CertificateArn=existing_arn)
        status = resp["Certificate"]["Status"]
        if status == "ISSUED":
            print(f"Certificate for {domain_name} already exists and is ISSUED.")
            print(f"  Certificate ARN: {existing_arn}")
            record_path = _write_deploy_record(domain_name, args.region, existing_arn, self_signed=False)
            print(f"  Wrote {record_path}")
            print()
            print("To deploy:")
            print(
                f"  ./deploy.py --cert-arn {existing_arn} "
                f"--advertised-dns {domain_name}"
            )
            return
        print(f"Found existing certificate for {domain_name} (status: {status}).")
        cert_arn = existing_arn
    else:
        print(f"Requesting ACM certificate for {domain_name} in {args.region}...")
        resp = acm.request_certificate(
            DomainName=domain_name,
            ValidationMethod="DNS",
        )
        cert_arn = resp["CertificateArn"]
        print(f"  Certificate ARN: {cert_arn}")

    print()
    record = _wait_for_cname(acm, cert_arn)
    cname_name = record["Name"]
    cname_value = record["Value"]

    print()
    print("Validation CNAME (add this to your DNS zone):")
    print(f"  Name:  {cname_name}")
    print(f"  Value: {cname_value}")

    if args.auto_route53:
        route53 = boto3.client("route53")
        print()
        print(f"Looking up Route 53 hosted zone for {domain_name}...")
        zone = _find_route53_zone(route53, domain_name)
        if not zone:
            sys.exit(
                f"ERROR: No Route 53 hosted zone found matching {domain_name}. "
                "Add the validation CNAME to your DNS provider manually and rerun "
                "without --auto-route53."
            )
        zone_id = zone["Id"].split("/")[-1]
        zone_name = zone["Name"].rstrip(".")
        print(f"  Found zone {zone_id} ({zone_name}). Creating validation record...")
        _create_route53_cname(route53, zone_id, cname_name, cname_value)
        print("  Created.")

    if args.no_wait:
        print()
        if not args.auto_route53:
            print("Add the CNAME above to your DNS provider, then rerun to poll for issuance:")
        else:
            print("Rerun to poll for issuance:")
        print(f"  ./scripts/certificate.py --region {args.region} --dns {domain_name}")
        return

    if not args.auto_route53:
        print()
        print(
            "Add the CNAME above to your DNS provider. "
            "Polling will continue until ACM validates the record (typically 1-5 minutes "
            "after the CNAME propagates)."
        )

    print()
    _wait_for_issuance(acm, cert_arn)

    record_path = _write_deploy_record(domain_name, args.region, cert_arn, self_signed=False)

    print()
    print("Certificate ready.")
    print(f"  Certificate ARN: {cert_arn}")
    print(f"  Wrote {record_path}")
    print()
    print("To deploy:")
    print(f"  ./deploy.py --cert-arn {cert_arn} --advertised-dns {domain_name}")


if __name__ == "__main__":
    main()
