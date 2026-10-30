# Neo4j Private Cluster: Lambda Demo App

`sample-private-app.template.yaml` deploys a Python Lambda inside a Neo4j EE Private or ExistingVpc stack. The Lambda connects to Neo4j over Bolt through the internal NLB, writes a small graph, reads it back, and returns cluster health details.

- **What it deploys:** a Python Lambda in the EE stack's private subnets, exposed through a Function URL with `AWS_IAM` auth
- **What it requires:** an existing `neo4j-ee` Private or ExistingVpc stack
- **What it demonstrates:** SSM parameter discovery, Secrets Manager password retrieval, security group wiring to the NLB and VPC interface endpoints, and topology-aware Bolt URI selection
- **Optional:** `--enable-resilience` adds a second Lambda that stops and starts a follower to validate cluster failover

[`docs/PRIVATE.md`](../docs/PRIVATE.md) covers operator access from a laptop via SSM port forwarding. This README covers application access from inside the VPC.

## Quick Start

### Prerequisites

- An existing `neo4j-ee` Private or ExistingVpc stack
- `uv` installed locally
- AWS credentials available through the normal SDK/CLI credential chain, such as `AWS_PROFILE`
- Permissions for CloudFormation, Lambda, IAM, EC2, S3, SSM, and Secrets Manager

### Workflow

Run from `sample-private-app/`:

```bash
# Deploy against the most recent EE stack
uv run deploy-sample-private-app.py

# Or target a specific EE stack
uv run deploy-sample-private-app.py test-ee-1776575131

# Invoke the Lambda
./invoke.sh

# Optional: deploy with --enable-resilience, then run the stop/start test
./validate.sh

# Tear down before deleting the parent EE stack
./teardown-sample-private-app.sh
```

`deploy-sample-private-app.py` reads the EE stack outputs and SSM parameters, packages the Lambda, uploads it to S3, deploys the CloudFormation stack with boto3, writes the Function URL to SSM, writes local deployment metadata under `../.deploy/`, and generates `invoke.sh` plus `validate.sh`.

## Architecture

```text
Internet
    |
    v
Lambda Function URL  (HTTPS, AWS_IAM auth)
    |
    +-- Lambda  (private subnet, Neo4j VPC)
           |  NEO4J_SSM_NLB_PATH       -> /neo4j-ee/<stack>/nlb-dns
           |  NEO4J_SECRET_ARN         -> neo4j/<stack>/password
           |  NEO4J_NUMBER_OF_SERVERS  -> 1 or 3
           |  NEO4J_BOLT_TLS           -> true when the stack sets AdvertisedDNS (TLS at the NLB)
           v
    Internal NLB  (<scheme>://<nlb-dns>:7687)
           |
    +------+------+
    v      v      v
  Neo4j-1 Neo4j-2 Neo4j-3
```

The Lambda security group has two egress paths:

- TCP 7687 to the Neo4j NLB security group for Bolt
- TCP 443 to the VPC interface endpoint security group for SSM, Secrets Manager, and CloudWatch Logs

The sample stack also adds matching ingress rules to the EE stack security groups. Stack deletion removes those rules automatically.

## Platform Contract

The EE stack publishes SSM parameters under `/neo4j-ee/<stack-name>/`. Application stacks should read these values rather than hard-code VPC IDs, subnet IDs, security group IDs, DNS names, or secret ARNs.

| Parameter | What the app uses it for |
|---|---|
| `/neo4j-ee/<stack>/vpc-id` | Attach Lambda to the correct VPC |
| `/neo4j-ee/<stack>/nlb-dns` | Connect to the internal NLB on Bolt port 7687 |
| `/neo4j-ee/<stack>/external-sg-id` | NLB security group used as the egress target on port 7687 |
| `/neo4j-ee/<stack>/password-secret-arn` | Import the Neo4j password secret by ARN |
| `/neo4j-ee/<stack>/vpc-endpoint-sg-id` | Add ingress 443 from the app SG; add egress 443 to the endpoint SG |
| `/neo4j-ee/<stack>/private-subnet-1-id` | Place Lambda in the first private subnet |
| `/neo4j-ee/<stack>/private-subnet-2-id` | Place Lambda in the second private subnet for clustered stacks; omitted when `NumberOfServers=1` |

## Connection Pattern

At cold start the Lambda resolves the internal NLB DNS name from SSM and the Neo4j password from Secrets Manager. Both AWS API calls route through private VPC endpoints.

The deployer passes two topology fields:

- `NumberOfServers=1` uses direct `bolt://`
- `NumberOfServers=3` uses routed `neo4j://`
- If the parent EE stack output sets a non-empty `AdvertisedDNS` (TLS terminated at the NLB), the Lambda uses the `+ssc` variant so the driver tolerates the self-signed test certificate

The driver is created lazily on first invocation and cached across warm starts. If the cached driver hits an `AuthError`, the handler closes it, rebuilds a fresh driver, and retries once.

## Client Checklist

1. Read the parent stack name or the EE deployment output file.
2. Read platform parameters from `/neo4j-ee/<stack>/`: `nlb-dns`, `vpc-id`, `external-sg-id`, `password-secret-arn`, `vpc-endpoint-sg-id`, and the private subnet IDs you need.
3. Place the client in private subnets with DNS resolution enabled.
4. Create an application-owned security group.
5. Wire Bolt access: app SG egress to the NLB SG on TCP 7687, and NLB SG ingress from the app SG on TCP 7687.
6. Wire AWS API access when reading SSM or Secrets Manager privately: app SG egress to the endpoint SG on TCP 443, and endpoint SG ingress from the app SG on TCP 443.
7. Retrieve the Neo4j password from the Secrets Manager ARN published by the EE stack.
8. Connect to the internal NLB DNS name on port 7687.
9. Cache the driver or connection pool across requests, then close it during process shutdown.

## What The Lambda Returns

```json
{
  "tls_enabled": true,
  "bolt_scheme": "neo4j+ssc",
  "edition": "enterprise",
  "nodes_created": 12,
  "relationships_created": 9,
  "graph_sample": [
    {"customer": "Alice Chen", "account_type": "checking", "amount": 2400.0, "merchant": "StripePayments"}
  ],
  "servers": [
    {"name": "...", "state": "Enabled", "health": "Available"}
  ],
  "routing_table": {
    "writers": 1,
    "readers": 2
  }
}
```

Single-server deployments skip the routing-table query because direct Bolt mode is the correct connection mode for a one-node stack.

### TLS conformance probe

The probe opens extra TLS and Bolt connections (one intentionally to a closed port, up to its timeout), so it does not run on the normal demo path. It runs only when the invocation event sets `{"tls_probe": true}`, which `deploy-sample-private-app.py` sends once after deploy as a gate. When it runs, the response gains a `tls_conformance` block:

```json
"tls_conformance": {
  "applicable": true,
  "advertised_dns": "neo4j.example.internal",
  "passed": true,
  "checks": {
    "plaintext_bolt_refused": {"passed": true, "detail": "plaintext neo4j:// rejected (ServiceUnavailable)"},
    "https_7473_ok": {"passed": true, "detail": "GET https://.../ -> 200"},
    "plaintext_http_7474_refused": {"passed": true, "detail": "7474 connect refused (ConnectionRefusedError)"},
    "cert_identity": {"passed": true, "detail": "served cert valid for neo4j.example.internal"}
  },
  "strict_tls_info": {"passed": false, "detail": "neo4j+s:// not CA-trusted (ServiceUnavailable)"}
}
```

The hard checks fail the probe (`passed: false`) if a plaintext Bolt connection is accepted, HTTPS on 7473 does not answer 200, plaintext HTTP 7474 is reachable, or the served certificate's SAN/CN does not equal `AdvertisedDNS`. `strict_tls_info` records whether `neo4j+s://` verifies against the system CA bundle; it is informational because the auto-imported self-signed certificate is a supported mode and is expected to fail strict verification. For a plaintext stack the probe is skipped with `applicable: false`. `deploy-sample-private-app.py` invokes the Lambda after deploy and exits non-zero if `tls_conformance.passed` is false, so a broken TLS posture fails the deploy rather than going unnoticed.

## Resilience Test

`validate.sh` is only usable when the app was deployed with `--enable-resilience`. It calls the second Lambda Function URL, which:

1. Calls `SHOW DATABASE neo4j YIELD serverID, writer` to find leader and follower server UUIDs.
2. Calls `ec2:DescribeInstances` filtered by `tag:StackID = <Neo4j EE stack ARN>` and `tag:Role = neo4j-cluster-node`.
3. Maps EC2 instance IDs to Neo4j server UUIDs with the stack-owned read-server-id SSM document.
4. Picks a random follower, invokes the stack-owned stop-Neo4j SSM document, and polls `SHOW SERVERS`.
5. Invokes the stack-owned start-Neo4j SSM document and waits for the server to return to `Available`.

The resilience Lambda is test-only and is not deployed by default. When enabled, its role can run only the stack-owned SSM command documents against EC2 instances tagged with the parent EE stack ID. It cannot run arbitrary `AWS-RunShellScript` commands.

## Teardown Ordering

Always delete the sample app before tearing down the parent EE stack. This stack owns ingress rules on the EE stack's security groups. If the EE stack is deleted first, those rules can become orphaned and block deletion.

## Project Structure

```text
sample-private-app/
├── deploy-sample-private-app.py      # Package Lambda, deploy CFN stack, generate invoke.sh + optional validate.sh
├── teardown-sample-private-app.sh    # Delete CFN stack, S3 zip versions, local files
├── invoke.sh                         # Generated at deploy time
├── validate.sh                       # Generated at deploy time; requires --enable-resilience
├── sample-private-app.template.yaml  # CloudFormation template
└── lambda/
    ├── handler.py                    # lambda_handler + resilience_handler
    └── requirements.txt              # neo4j>=6,<7
```
