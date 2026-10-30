#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

NAME_PREFIX = "neo4j-ee-base"


def find_amis_in_region(region: str, profile: str) -> list[dict]:
    session = boto3.Session(profile_name=profile, region_name=region)
    ec2 = session.client("ec2")
    response = ec2.describe_images(
        Owners=["self"],
        Filters=[{"Name": "name", "Values": [f"{NAME_PREFIX}*"]}],
    )
    return [
        {
            "region": region,
            "ami_id": img["ImageId"],
            "name": img["Name"],
            "state": img["State"],
            "created": img["CreationDate"],
        }
        for img in response["Images"]
    ]


def all_regions(profile: str) -> list[str]:
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    ec2 = session.client("ec2")
    response = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    return [r["RegionName"] for r in response["Regions"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Find neo4j-ee-base AMIs across all regions")
    parser.add_argument("--profile", default="default", help="AWS profile (default: default)")
    args = parser.parse_args()

    regions = all_regions(args.profile)
    print(f"Searching {len(regions)} regions for {NAME_PREFIX}* AMIs...\n")

    results: list[dict] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(find_amis_in_region, r, args.profile): r for r in regions}
        for future in as_completed(futures):
            region = futures[future]
            try:
                results.extend(future.result())
            except Exception as e:
                errors.append(f"  {region}: {e}")

    if not results:
        print("No AMIs found.")
    else:
        results.sort(key=lambda x: (x["created"], x["region"]), reverse=True)
        col = "{:<15} {:<25} {:<10} {:<30} {}"
        print(col.format("REGION", "AMI ID", "STATE", "CREATED", "NAME"))
        print("-" * 100)
        for r in results:
            print(col.format(r["region"], r["ami_id"], r["state"], r["created"], r["name"]))
        print(f"\n{len(results)} AMI(s) found across {len({r['region'] for r in results})} region(s).")

    if errors:
        print("\nErrors:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)


if __name__ == "__main__":
    main()
