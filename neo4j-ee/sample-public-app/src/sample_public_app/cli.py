"""Entry point for `uv run sample-public-app`.

Reads the seven-key public SSM contract published by a neo4j-ee Public stack
under `/neo4j-ee/<stack>/`, fetches the admin password from Secrets Manager,
opens a Bolt driver against the public NLB, runs a self-contained fintech
demo, and prints a JSON result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"

_REQUIRED_PUBLIC_KEYS = {
    "nlb-dns",
    "password-secret-arn",
    "bolt-url",
    "number-of-servers",
    "region",
    "stack-name",
}

_MERGE_FINTECH = """
MERGE (c1:Customer {id: 'c1', name: 'Alice Chen', segment: 'SMB'})
MERGE (c2:Customer {id: 'c2', name: 'Bob Patel', segment: 'Enterprise'})
MERGE (c3:Customer {id: 'c3', name: 'Carol Wu', segment: 'SMB'})

MERGE (a1:Account {id: 'acc1', type: 'checking', balance: 84200.00})
MERGE (a2:Account {id: 'acc2', type: 'checking', balance: 210000.00})
MERGE (a3:Account {id: 'acc3', type: 'savings',  balance: 55000.00})

MERGE (m1:Merchant {id: 'm1', name: 'StripePayments', category: 'payments'})
MERGE (m2:Merchant {id: 'm2', name: 'AmazonAWS',      category: 'cloud'})
MERGE (m3:Merchant {id: 'm3', name: 'WeWorkSpaces',   category: 'office'})

MERGE (t1:Transaction {id: 'txn1', amount: 2400.00,  currency: 'USD', ts: '2026-04-01'})
MERGE (t2:Transaction {id: 'txn2', amount: 18700.00, currency: 'USD', ts: '2026-04-02'})
MERGE (t3:Transaction {id: 'txn3', amount: 6500.00,  currency: 'USD', ts: '2026-04-03'})

MERGE (c1)-[:OWNS]->(a1)
MERGE (c2)-[:OWNS]->(a2)
MERGE (c3)-[:OWNS]->(a3)

MERGE (a1)-[:ORIGINATED_FROM]->(t1)
MERGE (a2)-[:ORIGINATED_FROM]->(t2)
MERGE (a3)-[:ORIGINATED_FROM]->(t3)

MERGE (t1)-[:AT]->(m1)
MERGE (t2)-[:AT]->(m2)
MERGE (t3)-[:AT]->(m3)
"""

_GRAPH_SAMPLE_CYPHER = """
MATCH (c:Customer)-[:OWNS]->(a:Account)-[:ORIGINATED_FROM]->(t:Transaction)-[:AT]->(m:Merchant)
RETURN c.name AS customer, a.type AS account_type, t.amount AS amount, m.name AS merchant
ORDER BY t.ts
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a self-contained Bolt demo against a deployed neo4j-ee Public "
            "stack. Reads the public SSM contract under /neo4j-ee/<stack>/."
        )
    )
    parser.add_argument(
        "stack_name",
        nargs="?",
        help=(
            "EE stack name. Defaults to the most recently modified "
            "neo4j-ee/.deploy/*.txt file."
        ),
    )
    parser.add_argument(
        "--region",
        help=(
            "AWS region. Defaults to the Region field in the deploy file or "
            "the boto3 session default."
        ),
    )
    return parser.parse_args()


def _latest_deploy_file() -> Path | None:
    if not _DEPLOY_DIR.is_dir():
        return None
    candidates = sorted(
        _DEPLOY_DIR.glob("*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_deploy_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def resolve_stack_and_region(
    stack_arg: str | None,
    region_arg: str | None,
) -> tuple[str, str | None]:
    """Resolve the EE stack name and bootstrap region.

    Without an explicit stack name we read the most recent deploy file under
    `neo4j-ee/.deploy/`. With one, we still read the matching deploy file
    when present so Region can default from it; `--region` always wins, and
    when neither is set boto3's default region chain takes over.
    """
    if stack_arg:
        candidate = _DEPLOY_DIR / f"{stack_arg.removesuffix('.txt')}.txt"
        path: Path | None = candidate if candidate.is_file() else None
    else:
        path = _latest_deploy_file()

    fields = _load_deploy_fields(path) if path is not None else {}
    stack_name = stack_arg or fields.get("StackName", "")
    if not stack_name:
        raise SystemExit(
            f"ERROR: No stack name given and no deploy file in {_DEPLOY_DIR}. "
            "Run ../deploy.py --mode Public first, or pass a stack name."
        )
    region = region_arg or fields.get("Region") or None
    return stack_name, region


def read_public_ssm_contract(ssm, stack_name: str) -> dict[str, str]:
    prefix = f"/neo4j-ee/{stack_name}/"
    params: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=False):
        for entry in page.get("Parameters", []):
            params[entry["Name"][len(prefix):]] = entry["Value"]

    if not params:
        raise SystemExit(
            f"ERROR: No SSM parameters found under {prefix}. "
            f"Is the stack '{stack_name}' deployed in this region?"
        )

    # The public template publishes `number-of-servers`; the private and
    # existing-vpc templates do not. Absence of the key signals the caller
    # pointed us at a non-public stack.
    if "number-of-servers" not in params:
        raise SystemExit(
            f"ERROR: Stack '{stack_name}' does not publish the public SSM "
            "contract (no `number-of-servers` key). sample-public-app only "
            "supports DeploymentMode=Public. For Private or ExistingVpc "
            "stacks, use ../sample-private-app/ instead."
        )

    missing = _REQUIRED_PUBLIC_KEYS - params.keys()
    if missing:
        raise SystemExit(
            f"ERROR: Incomplete public SSM contract under {prefix} "
            f"(missing keys: {sorted(missing)}). The stack may not be fully "
            "deployed."
        )
    return params


def fetch_password(secrets, secret_arn: str) -> str:
    response = secrets.get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def bolt_uri_from_contract(params: dict[str, str]) -> tuple[str, str]:
    """Return (uri, scheme) from the producer-published bolt-url.

    The producer (the Public template) owns the scheme decision. Off-VPC
    consumers read bolt-url and use it directly; no client-side derivation.
    """
    uri = params["bolt-url"]
    scheme, _, _ = uri.partition("://")
    return uri, scheme


def run_demo(driver, multi_node: bool) -> dict:
    with driver.session(database="neo4j") as session:
        result = session.run(_MERGE_FINTECH)
        summary = result.consume()
        nodes_created = summary.counters.nodes_created
        rels_created = summary.counters.relationships_created

        edition_row = session.run(
            "CALL dbms.components() YIELD name, versions, edition "
            "WHERE name = 'Neo4j Kernel' RETURN edition"
        ).single()
        edition = edition_row["edition"] if edition_row else "unknown"

        graph_sample = session.run(_GRAPH_SAMPLE_CYPHER).data()

        routing_rows: list[dict] = []
        if multi_node:
            routing_rows = session.run(
                "CALL dbms.routing.getRoutingTable({}, 'neo4j')"
            ).data()

    body: dict = {
        "edition": edition,
        "nodes_created": nodes_created,
        "relationships_created": rels_created,
        "graph_sample": graph_sample,
    }

    if multi_node:
        with driver.session(database="system") as sys_session:
            servers = sys_session.run("SHOW SERVERS").data()
        writers = 0
        readers = 0
        for row in routing_rows:
            for server in row.get("servers", []):
                role = server.get("role")
                if role == "WRITE":
                    writers += 1
                elif role == "READ":
                    readers += len(server.get("addresses", []))
        body["servers"] = [
            {
                "name": s.get("name", s.get("address", "")),
                "state": s.get("state", ""),
                "health": s.get("health", ""),
            }
            for s in servers
        ]
        body["routing_table"] = {"writers": writers, "readers": readers}

    return body


def main() -> None:
    args = parse_args()
    try:
        stack_name, region = resolve_stack_and_region(args.stack_name, args.region)
        session = boto3.Session(region_name=region)
        ssm = session.client("ssm")
        secrets = session.client("secretsmanager")

        params = read_public_ssm_contract(ssm, stack_name)
        password = fetch_password(secrets, params["password-secret-arn"])
        uri, scheme = bolt_uri_from_contract(params)
        multi_node = params["number-of-servers"] != "1"

        body = {
            "stack_name": params["stack-name"],
            "region": params["region"],
            "bolt_uri": uri,
            "bolt_scheme": scheme,
            "tls_enabled": scheme in ("neo4j+s", "neo4j+ssc"),
        }

        driver = GraphDatabase.driver(uri, auth=("neo4j", password))
        try:
            body.update(run_demo(driver, multi_node))
        finally:
            driver.close()
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: AWS API call failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Neo4jError as exc:
        print(f"ERROR: Neo4j query failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(body, indent=2, default=str))


if __name__ == "__main__":
    main()
