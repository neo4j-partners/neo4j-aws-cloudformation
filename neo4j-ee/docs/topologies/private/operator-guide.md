# Neo4j EE Private: Operator Guide

Applies to any running private stack, whether deployed from the Marketplace or from source.

All `uv run` commands (`admin-shell`, `run-cypher`, `validate-private`) must be run from `neo4j-ee/validate-private/`. Shell scripts under `scripts/` must be run from `neo4j-ee/validate-private/` as well.

## Prerequisites

**AWS tooling**

```bash
aws --version                                    # AWS CLI v2
brew install --cask session-manager-plugin       # required for SSM port-forward tunnels
session-manager-plugin --version
```

**Python tooling**

```bash
python3 --version   # 3.11+
brew install uv     # package manager for validate-private and test suite
```

**Neo4j tooling**

```bash
brew install cypher-shell   # required by uv run admin-shell
```

**IAM permissions**

These are the minimum permissions the operator's local IAM principal (user or assumed role) needs to run the tools in this guide. Each permission corresponds to API calls made from the operator's machine. The cluster nodes use a separate IAM role scoped to what they need at boot.

| Permission | Resource | Used by |
|---|---|---|
| `cloudformation:DescribeStacks`, `cloudformation:DescribeStackResources` | The stack ARN | `uv run preflight`, `deploy.py` (reads stack outputs) |
| `cloudformation:ListStackResources` | The stack ARN | `uv run validate-private`, which finds the node ASGs, and `teardown.sh` |
| `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:StartSession`, `ssm:DescribeInstanceInformation` | The bastion instance | `uv run scripts/browser-tunnel.py`, `uv run scripts/bolt-tunnel.py`, `admin-shell`, `run-cypher`, `validate-private`, `uv run preflight` (bastion ping check) |
| `ssm:GetParameter`, `ssm:GetParametersByPath` | `/neo4j-ee/<stack-name>/*` | Any tool that resolves the NLB DNS or security group IDs from the platform contract. Every `uv run` tool that connects to Neo4j reads `bolt-url`, and on a `neo4j+s` stack it also reads `ca-pem`. The release suite reads `ca-pem` on every stack. |
| `secretsmanager:GetSecretValue`, `secretsmanager:DescribeSecret` | `neo4j/<stack-name>/password` | `get-password.sh`, `uv run preflight`, and every `uv run` tool that connects to Neo4j. The tools read the password with your credentials and keep it in memory. |
| `secretsmanager:GetSecretValue` | `neo4j-<stack-name>-app-password` | `uv run validate-private --suite release`, which runs the APOC check as `neo4j_app` |
| `kms:Decrypt` | The stack key in `/neo4j-ee/<stack-name>/kms-key-arn` | `get-password.sh` and every tool that reads a password secret |

**Deploying identity**

The identity that creates the stack also creates the two password secrets. Secrets Manager encrypts them with the stack KMS key on that identity's behalf. The identity therefore needs `kms:GenerateDataKey` on the key. The key does not exist until the stack creates it, so scope the grant with the `kms:ViaService` condition for `secretsmanager.<region>.amazonaws.com` instead of a key ARN. An identity with `AdministratorAccess` already has this permission.

**Plugin licenses (optional)**

Bloom and Graph Data Science are off by default in the Marketplace templates (`InstallBloom=false`, `InstallGDS=false`). The local `deploy.py` helper flips both defaults on for internal validation; this section describes the buyer-facing template contract. To enable either plugin at launch, first create a Secrets Manager secret holding the license file contents, then pass its ARN to the matching CFN parameter:

```bash
aws secretsmanager create-secret \
  --name neo4j/bloom-license \
  --secret-string file:///path/to/bloom.license \
  --region <region>
```

Launch the stack with `InstallBloom=true` and `BloomLicenseSecretArn=<ARN returned above>`. The same pattern applies to GDS via `InstallGDS=true` with `GdsLicenseSecretArn`. The instance role is granted `secretsmanager:GetSecretValue` scoped to the specific ARN you supplied, and only when the matching `Install*=true` parameter is set; a default launch grants no Secrets Manager access for licenses.

Failures are surfaced at two layers:

- `AWS::CloudFormation::Rules` reject `InstallBloom=true` with an empty `BloomLicenseSecretArn` (and the same for GDS) at parameter validation, before any resource is created. Console, CLI, SDK, and Service Catalog stack create and update calls all go through this gate.
- If the runtime fetch or plugin install fails on boot (unreachable secret, wrong region, empty or malformed payload, IAM denial, missing JAR in the AMI), UserData calls `cfn-signal --success false` so the stack moves to `CREATE_FAILED` within minutes instead of waiting out the ASG signal timeout.

## Preflight Check

Before running any other tool, confirm the stack and bastion are ready:

```bash
cd neo4j-ee/validate-private
uv run preflight                     # most recent deployment
uv run preflight <stack-name>        # specific deployment
```

Expected output on a healthy stack:

```
=== Preflight Checks ===

  Stack:   test-ee-1776575131
  Region:  us-east-1
  Bastion: i-0abc123def456789

  [PASS] Stack status = CREATE_COMPLETE or UPDATE_COMPLETE
  [PASS] Bastion SSM PingStatus = Online
  [PASS] session-manager-plugin on local PATH
  [PASS] Secret 'neo4j/test-ee-1776575131/password' exists
  [PASS] SSM tunnel opens and RETURN 1 succeeds
  [PASS] TLS params set: CertificateArn, AdvertisedDNS
  [PASS] Contract SSM params: vpc-id, nlb-dns, external-sg-id, password-secret-arn, vpc-endpoint-sg-id
  [PASS] VPC interface endpoints: secretsmanager, logs, ssm, ssmmessages, ec2messages, ec2, cloudformation, autoscaling, acm
  [PASS] Endpoint reachable: secretsmanager.us-east-1.amazonaws.com
  [PASS] Endpoint reachable: logs.us-east-1.amazonaws.com
  [PASS] Endpoint reachable: ssm.us-east-1.amazonaws.com
  [PASS] Endpoint reachable: ssmmessages.us-east-1.amazonaws.com
  [INFO] Operational SSM params: region, stack-name, private-subnet-1-id, private-subnet-2-id

  12 passed, 0 failed
```

If the bastion SSM check fails immediately after a fresh deploy, the bastion UserData may still be running. Wait 2-3 minutes and retry. If the `session-manager-plugin` check fails, install the plugin as shown under [Prerequisites](#prerequisites).

## Access via Bastion

All operator access goes through the `t4g.small` bastion via SSM. The bastion holds no Neo4j credentials. It relays an SSM port-forward from your laptop to the NLB. The `uv run` tools open that port-forward themselves and run the Neo4j driver locally. Run `uv run` commands from `neo4j-ee/validate-private/`.

| Tool | How it connects | Tunnel needed? |
|---|---|---|
| `uv run admin-shell` | Local `cypher-shell` over a port-forward on 7687 the tool opens | No. It needs an `/etc/hosts` line. |
| `uv run run-cypher` | Local driver over a port-forward the tool opens | No |
| `uv run validate-private` | Local driver over a port-forward the tool opens | No |
| `uv run scripts/smoke-write.py` | Local driver over a port-forward the tool opens | No |
| Local driver or client tool | Bolt connection from your laptop | Yes, the Bolt tunnel on 7687 |
| Neo4j Browser | HTTPS for the web UI + Bolt for queries | Yes, both tunnels on 7473 and 7687 |

### Session logging

The stack creates a Session Manager preferences document. Its name is the `Neo4jSessionDocumentName` stack output. Interactive shell sessions started with this document stream their input and output to the `/neo4j/<stack-name>/sessions` CloudWatch log group. The stack KMS key encrypts that log group. Pass the document when you open a shell on the bastion:

```bash
aws ssm start-session --target <bastion-instance-id> --document-name <Neo4jSessionDocumentName>
```

Port-forward sessions are not logged by this document. Session Manager does not record the bytes of a port-forward. The `uv run` tools, the tunnel scripts and `browse.py` all use port-forward sessions. Their audit trail is the CloudTrail `StartSession` event plus the Neo4j security log and query log on each node.

## Bolt Tunnel

Use the Bolt tunnel when you want to connect a local driver, client tool, or script to the cluster from your laptop.

```bash
uv run scripts/bolt-tunnel.py      # localhost:7687 -> NLB:7687  (blocks; Ctrl-C to close)
```

Map `AdvertisedDNS` to `127.0.0.1` in `/etc/hosts`, as the script prints, so the certificate SAN matches. Connect with `bolt+s://<AdvertisedDNS>:7687` when the NLB serves a certificate your laptop trusts. Connect with `bolt+ssc://<AdvertisedDNS>:7687` when it serves the self-signed certificate that `deploy.py` imports by default, or any other certificate your laptop does not trust. The script prints `bolt+ssc` only for the certificate that `deploy.py` imports. For any other untrusted certificate, change the scheme to `bolt+ssc` yourself. Use direct `bolt` mode rather than `neo4j` routing mode from a laptop tunnel because routed mode returns the NLB's internal DNS name, which is not reachable from your laptop.

The Bolt tunnel is also required alongside the Browser Tunnel when using Neo4j Browser.

## Browser Tunnel

Use this to open the Neo4j Browser web UI. The browser makes two connections: HTTPS to load the UI (port 7473) and Bolt to run queries (port 7687). Both tunnels must be open simultaneously.

**Single-command option.** Run from `neo4j-ee/`:

```bash
uv run scripts/browse.py                   # most recent deployment
uv run scripts/browse.py <stack-name>      # specific deployment
```

`browse.py` reads `.deploy/<stack-name>.txt`, opens both SSM port-forward tunnels in the same shell, and prints the URL and the username. It prints the password secret ARN. With `--show-password` it fetches the password from Secrets Manager with your AWS credentials and prints it. Add the `/etc/hosts` line it prints, then open the `https://<AdvertisedDNS>:7473` URL. Press Ctrl+C to close both tunnels.

**Two-terminal option.** Run from `neo4j-ee/validate-private/`:

```bash
uv run scripts/browser-tunnel.py   # localhost:7473 -> NLB:7473  (blocks; Ctrl-C to close)
uv run scripts/bolt-tunnel.py      # localhost:7687 -> NLB:7687  (blocks; Ctrl-C to close)
```

From `neo4j-ee/`, `./scripts/browser-tunnel.sh [stack-name]` and `./scripts/bolt-tunnel.sh [stack-name]` run the same two scripts. They set `AWS_PROFILE` to `marketplace` when it is unset.

The certificate SAN is the stack's `AdvertisedDNS`, so add `127.0.0.1 <AdvertisedDNS>` to your laptop's `/etc/hosts`. `browser-tunnel.py` prints the exact line. Then open `https://<AdvertisedDNS>:7473`. The self-signed certificate triggers a browser warning that you must accept. When prompted for a connection URL, enter the Bolt URI printed by `uv run scripts/browse.py` or `uv run scripts/bolt-tunnel.py`. For the password, see [Retrieve the Password](#retrieve-the-password).

> **Note:** Connection strings inside Neo4j Browser show the internal NLB DNS hostname, or `AdvertisedDNS` when the stack creates its private hosted zone. Substitute `localhost` when connecting through the tunnel.

> **Note:** Writes through Neo4j Browser go to whichever node the NLB selects, which may not be the leader, producing a `NotALeader` error. Use `uv run admin-shell` for writes.

## Retrieve the Password

```bash
./scripts/get-password.sh

# To capture it:
PASSWORD=$(./scripts/get-password.sh 2>/dev/null)
```

The password is stored in Secrets Manager at `neo4j/<stack-name>/password` as a plain string: the password value itself, not JSON. The stack KMS key encrypts it, so the caller needs `kms:Decrypt` on that key. See [IAM permissions](#prerequisites).

The `uv run` tools read the password from Secrets Manager with your own AWS credentials. They keep it in process memory and never write it to disk or pass it on a command line. The bastion role has no access to the secret. For Neo4j Browser, you type the password into the login form.

## Single Sign-On (OIDC)

Neo4j Browser and Bloom can sign users in through an OpenID Connect identity provider. Setting `OidcIssuerUri` turns this on. Password login stays enabled next to it, and operator tools keep using the native `neo4j` password.

- **Settings, token design, group mapping and troubleshooting:** [Single Sign-On (OIDC) Reference](../../reference/oidc-sign-in.md)
- **Step-by-step deployment, including the Hume integration:** [OIDC Run Book](../../run-books/oidc.md)

## Admin Shell

For Cypher queries and write operations:

```bash
cd neo4j-ee/validate-private
uv run admin-shell                     # most recent deployment
uv run admin-shell <stack-name>        # specific deployment
```

Runs `cypher-shell` on your laptop over an SSM port-forward on `localhost:7687`. If a Bolt tunnel is already open on 7687, `admin-shell` reuses it. The shell connects with the routed `neo4j+ssc://` scheme, so writes go to the current leader. The password is read with your AWS credentials and passed to `cypher-shell` in its environment only.

The routing table names the cluster's advertised Bolt host, so that host must resolve to `127.0.0.1` on your laptop. Add `127.0.0.1 <host>` to `/etc/hosts`. When the line is missing, `admin-shell` prints the exact line to add and exits. The same line serves Neo4j Browser when Bolt advertises `AdvertisedDNS`.

```
neo4j@neo4j> CREATE (n:Test {msg: "hello"}) RETURN n;
neo4j@neo4j> MATCH (n:Test) DELETE n;
neo4j@neo4j> :exit
```

## Ad-Hoc Cypher Queries

```bash
cd neo4j-ee/validate-private
uv run run-cypher "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition"

# Output is JSON; pipe to jq for formatting
uv run run-cypher "SHOW SERVERS YIELD name, address, state, health" | jq .

# Target a specific stack
uv run run-cypher <stack-name> "MATCH (n) RETURN count(n) AS total"
```

## Observability

```bash
cd neo4j-ee
./scripts/test-observability.sh                  # most recent deployment
./scripts/test-observability.sh <stack-name>     # specific deployment
```

| Step | What it checks | Typical duration |
|---|---|---|
| `cloudwatch` | CloudWatch agent active on all nodes | <1 min |
| `logs` | Application log group exists with expected stream count | <1 min |
| `flowlogs` | VPC flow log group exists and has ENI streams | <1 min |
| `alarm` | Failed-auth alarm transitions to ALARM after 12 bad login attempts | ~7 min |
| `cloudtrail` | Account-level check: the region's CloudTrail event history holds a management event from the last 24 hours. It is not a stack resource. | <1 min |
