# Neo4j Public Stack: Off-VPC Sample App

`sample-public-app` is a `uv`-managed Python project that connects from a
developer laptop to a deployed `neo4j-ee` **Public** stack. It reads the
public SSM contract under `/neo4j-ee/<stack>/`, fetches the admin password
from Secrets Manager, opens a Bolt driver against the public NLB, runs a
self-contained graph demo, and prints a JSON result.

- **What it does:** discovery via SSM, password via Secrets Manager, Bolt over the public NLB, fintech demo graph, routing-table summary on multi-node stacks
- **What it requires:** an existing `neo4j-ee` Public stack
- **What it demonstrates:** the public SSM contract, reading the producer-formatted `bolt-url` directly (no client-side scheme derivation), and the "single public address for all cores" routing pattern

For Private or ExistingVpc stacks, use [`../sample-private-app/`](../sample-private-app/)
instead. That sample runs as a Lambda inside the VPC and discovers the
internal NLB through the private SSM contract.

## Quick Start

### Prerequisites

- An existing `neo4j-ee` Public stack (any of: plaintext single-node, plaintext three-node, TLS three-node)
- `uv` installed locally
- AWS credentials with read access to SSM (`/neo4j-ee/<stack>/*`) and the Secrets Manager secret published by the stack
- The laptop's egress IP must be in the stack's `AllowedCIDR`

### Workflow

Run from `sample-public-app/`:

```bash
# Against the most recent EE deploy (reads neo4j-ee/.deploy/*.txt for stack + region)
uv run sample-public-app

# Or target a specific stack
uv run sample-public-app test-ee-1779401520

# Explicit region (overrides the deploy file)
uv run sample-public-app test-ee-1779401520 --region us-east-1
```

The script exits non-zero on connect, auth, or query failure. If the named
stack does not publish the public-contract `number-of-servers` key (which
only the Public template writes), the script fails with a readable error
pointing at `sample-private-app/`.

## Architecture

```text
Developer laptop
    |
    | (1) GetParametersByPath  /neo4j-ee/<stack>/*
    | (2) GetSecretValue       neo4j/<stack>/password
    | (3) Bolt over the public NLB
    v
Public NLB (internet-facing)
    |
    +-- bolt://<nlb-dns>:7687             (single-node, plaintext)
    +-- neo4j://<nlb-dns>:7687            (three-node, plaintext)
    +-- neo4j+s://<advertised-dns>:7687   (TLS, terminated at the NLB)
    |
    +--+---+
    v  v   v
  Neo4j cluster (1 or 3 nodes)
```

The script never touches the VPC. SSM, Secrets Manager, and Bolt traffic
all go over the public internet using the laptop's credentials.

## Platform Contract

The Public template publishes SSM parameters under
`/neo4j-ee/<stack-name>/`. Clients should read these values rather than
hard-code the NLB DNS name, secret ARN, or scheme choice.

| Parameter | What the sample uses it for |
|---|---|
| `/neo4j-ee/<stack>/bolt-url` | Producer-formatted Bolt URI; used directly, no scheme derivation |
| `/neo4j-ee/<stack>/password-secret-arn` | Import the Neo4j password secret by ARN |
| `/neo4j-ee/<stack>/nlb-dns` | NLB DNS name; published for consumers that need the raw host |
| `/neo4j-ee/<stack>/advertised-dns` | Advertised DNS; published only when `UsePublicTLS` is set |
| `/neo4j-ee/<stack>/number-of-servers` | `1` for single-node, `3` for cluster; signals the Public stack type to off-VPC consumers |
| `/neo4j-ee/<stack>/region` | Records the deploy region so consumers do not need a separate config |
| `/neo4j-ee/<stack>/stack-name` | Records the stack name for back-reference |

The presence of `number-of-servers` is the signal that this is a Public
stack. The Private and ExistingVpc contracts use a different key set
(VPC ID, private subnet IDs, endpoint SG IDs) that an off-VPC consumer
cannot use.

## Connection Pattern

The Public template publishes a pre-formatted `bolt-url` whose scheme
already encodes the deployed TLS and cluster mode. The sample reads that
value and passes it straight to the driver:

| Deploy mode | `bolt-url` value |
|---|---|
| Plaintext, single-node | `bolt://<nlb-dns>:7687` |
| Plaintext, three-node | `neo4j://<nlb-dns>:7687` |
| TLS, single-node or three-node | `neo4j+s://<advertised-dns>:7687` |

On TLS stacks the driver validates the server certificate against the
system CA bundle. `neo4j+ssc://` (skip cert verification) is **not**
published on the public path: it would mask a misconfigured SAN or
chain, both of which the public deploy explicitly wants to catch. The
private sample uses `+ssc` because that stack auto-imports a self-signed
cert; the public stack expects a real CA-trusted cert when TLS is on.

### "Single public address for all cores"

Every cluster member advertises the NLB DNS for Bolt, not a per-node IP.
That is intentional and is the documented Neo4j pattern for fronting a
cluster with a single public address. The bootstrap sets
`server.bolt.advertised_address` to the NLB DNS on every topology, so all
three cores publish the same Bolt host in the routing table.

When the driver requests routing information against a three-node stack,
the table contains three identical NLB entries. The driver deduplicates
those entries into one logical connection pool. From the driver's point
of view the cluster looks like a single endpoint that can serve both
writers and readers; the actual fan-out (writes go to the leader, reads
to followers) is performed by the NLB and the cluster, not by the
driver's `LeastConnectedLoadBalancingStrategy`.

This is why the sample's output includes a `routing_table` summary on
three-node stacks: it surfaces the cluster's view (one writer, two
readers) even though the driver's connection pool only sees one host.
A reader who expects the driver to spread reads across distinct per-node
addresses should know the NLB performs that role here.

References:

- Neo4j Operations Manual, [Leadership, routing, and load balancing](https://neo4j.com/docs/operations-manual/current/clustering/setup/routing/)
- Neo4j Operations Manual, [Configure network connectors](https://neo4j.com/docs/operations-manual/current/configuration/connectors/)
- Neo4j Driver Manual, [Client applications](https://neo4j.com/docs/driver-manual/4.0/client-applications/)

## Client Checklist

1. Read the parent EE deployment output file or take a stack name as input.
2. Resolve the region from the `Region` line in `.deploy/<stack>.txt`, the `--region` flag, or the boto3 session default.
3. Read the public-contract parameters from `/neo4j-ee/<stack>/` with `GetParametersByPath`.
4. Fail fast if `number-of-servers` is absent — that stack is Private or ExistingVpc and the off-VPC path will not work; point the user at `sample-private-app/`.
5. Fetch the admin password from the secret ARN published in SSM.
6. Use `bolt-url` directly as the driver URI. The producer encoded the scheme; no client-side derivation.
7. Open the driver, run queries, close the driver. The pool is short-lived in this sample; long-running clients should cache it.
8. Surface the driver's routing-table view on three-node stacks so the reader can see writer and reader counts even though the connection pool sees one host.

## What The Script Returns

A JSON object printed to stdout. Always present:

```json
{
  "stack_name": "test-ee-1779401520",
  "region": "us-east-1",
  "bolt_uri": "neo4j+s://neo4j-demo.example.com:7687",
  "bolt_scheme": "neo4j+s",
  "tls_enabled": true,
  "edition": "enterprise",
  "nodes_created": 12,
  "relationships_created": 9,
  "graph_sample": [
    {"customer": "Alice Chen", "account_type": "checking", "amount": 2400.0, "merchant": "StripePayments"}
  ]
}
```

Added for three-node stacks:

```json
{
  "servers": [
    {"name": "<uuid>", "state": "Enabled", "health": "Available"}
  ],
  "routing_table": {
    "writers": 1,
    "readers": 2
  }
}
```

Single-node stacks skip both the `servers` summary and the
`routing_table` block: direct `bolt://` is the correct connection mode
for a one-node deployment and there is no routing table to summarize.

## Project Structure

```text
sample-public-app/
├── pyproject.toml                       # uv project + sample-public-app entrypoint
├── README.md
└── src/
    └── sample_public_app/
        ├── __init__.py
        └── cli.py                       # discovery, URI selection, demo, JSON output
```
