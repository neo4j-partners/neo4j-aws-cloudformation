# Neo4j EE: Private

`neo4j-private.template.yaml` deploys a Neo4j Enterprise cluster in private subnets behind an internal Network Load Balancer.

- **What it deploys:** Neo4j EE cluster (1 or 3 nodes) in private subnets behind an internal NLB
- **Public exposure:** No instance has a public IP.
- **Operator access:** dedicated `t4g.small` bastion via AWS Systems Manager Session Manager
- **When to use:** production, staging, and regulated workloads

> **Marketplace operator** (deployed from AWS Marketplace, running stack):
> Start with [Prerequisites](#prerequisites) and the [Operator Guide](#operator-guide) below.
>
> **Template developer** (working on the templates, deploying from source):
> Start with [Local Deployment and Testing](#local-deployment-and-testing).
> The [Operator Guide](#operator-guide) applies once your stack is running.

## Contents

- [Operator Guide](#operator-guide)
  - [Prerequisites](#prerequisites)
  - [Preflight Check](#preflight-check)
  - [Access via Bastion](#access-via-bastion)
  - [Bolt Tunnel](#bolt-tunnel)
  - [Browser Tunnel](#browser-tunnel)
  - [Retrieve the Password](#retrieve-the-password)
  - [Single Sign-On (OIDC)](#single-sign-on-oidc)
  - [Admin Shell](#admin-shell)
  - [Ad-Hoc Cypher Queries](#ad-hoc-cypher-queries)
  - [Observability](#observability)
- [Architecture](#architecture)
  - [Network Topology](#network-topology)
  - [AWS Resources Created](#aws-resources-created)
  - [NLB Routing](#nlb-routing)
  - [Operator Bastion: NLB Hairpin](#operator-bastion-nlb-hairpin)
  - [Platform Contract](#platform-contract)
  - [Password Security Model](#password-security-model)
  - [Bolt TLS](#bolt-tls)
- [Local Deployment and Testing](#local-deployment-and-testing)
  - [Build](#build)
  - [Deploy](#deploy)
  - [Preflight and Basic Validation](#preflight-and-basic-validation)
  - [Smoke Test](#smoke-test)
  - [Failover Suite](#failover-suite)
  - [Resilience Suite](#resilience-suite)
  - [Tear Down](#tear-down)
  - [Troubleshooting](#troubleshooting)

---

## Operator Guide

Applies to any running private stack, whether deployed from the Marketplace or from source.

All `uv run` commands (`admin-shell`, `run-cypher`, `validate-private`) must be run from `neo4j-ee/validate-private/`. Shell scripts under `scripts/` must be run from `neo4j-ee/validate-private/` as well.

### Prerequisites

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
| `cloudformation:ListStackResources` | The stack ARN | `uv run validate-private`, which finds the node ASGs, and `teardown.sh` and `recover.sh` |
| `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:StartSession`, `ssm:DescribeInstanceInformation` | The bastion instance | `uv run scripts/browser-tunnel.py`, `uv run scripts/bolt-tunnel.py`, `admin-shell`, `run-cypher`, `validate-private`, `uv run preflight` (bastion ping check) |
| `ssm:GetParameter`, `ssm:GetParametersByPath` | `/neo4j-ee/<stack-name>/*` | Any tool that resolves the NLB DNS or security group IDs from the platform contract. Every `uv run` tool that connects to Neo4j reads `bolt-url`, and on a `neo4j+s` stack it also reads `ca-pem`. The release suite reads `ca-pem` on every stack. |
| `secretsmanager:GetSecretValue`, `secretsmanager:DescribeSecret` | `neo4j/<stack-name>/password` | `get-password.sh`, `uv run preflight`, and every `uv run` tool that connects to Neo4j. The tools read the password with your credentials and keep it in memory. |
| `secretsmanager:GetSecretValue` | `neo4j/<stack-name>/app-password` | `uv run validate-private --suite release`, which runs the APOC check as `neo4j_app` |
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

### Preflight Check

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

### Access via Bastion

All operator access goes through the `t4g.small` bastion via SSM. The bastion holds no Neo4j credentials. It relays an SSM port-forward from your laptop to the NLB. The `uv run` tools open that port-forward themselves and run the Neo4j driver locally. Run `uv run` commands from `neo4j-ee/validate-private/`.

| Tool | How it connects | Tunnel needed? |
|---|---|---|
| `uv run admin-shell` | Local `cypher-shell` over a port-forward on 7687 the tool opens | No. It needs an `/etc/hosts` line. |
| `uv run run-cypher` | Local driver over a port-forward the tool opens | No |
| `uv run validate-private` | Local driver over a port-forward the tool opens | No |
| `uv run scripts/smoke-write.py` | Local driver over a port-forward the tool opens | No |
| Local driver or client tool | Bolt connection from your laptop | Yes, the Bolt tunnel on 7687 |
| Neo4j Browser | HTTPS for the web UI + Bolt for queries | Yes, both tunnels on 7473 and 7687 |

#### Session logging

The stack creates a Session Manager preferences document. Its name is the `Neo4jSessionDocumentName` stack output. Interactive shell sessions started with this document stream their input and output to the `/neo4j/<stack-name>/sessions` CloudWatch log group. The stack KMS key encrypts that log group. Pass the document when you open a shell on the bastion:

```bash
aws ssm start-session --target <bastion-instance-id> --document-name <Neo4jSessionDocumentName>
```

Port-forward sessions are not logged by this document. Session Manager does not record the bytes of a port-forward. The `uv run` tools, the tunnel scripts and `browse.py` all use port-forward sessions. Their audit trail is the CloudTrail `StartSession` event plus the Neo4j security log and query log on each node.

### Bolt Tunnel

Use the Bolt tunnel when you want to connect a local driver, client tool, or script to the cluster from your laptop.

```bash
uv run scripts/bolt-tunnel.py      # localhost:7687 -> NLB:7687  (blocks; Ctrl-C to close)
```

Map `AdvertisedDNS` to `127.0.0.1` in `/etc/hosts`, as the script prints, so the certificate SAN matches. Connect with `bolt+s://<AdvertisedDNS>:7687` when the NLB serves a certificate your laptop trusts. Connect with `bolt+ssc://<AdvertisedDNS>:7687` when it serves the self-signed certificate that `deploy.py` imports by default, or any other certificate your laptop does not trust. The script prints `bolt+ssc` only for the certificate that `deploy.py` imports. For any other untrusted certificate, change the scheme to `bolt+ssc` yourself. Use direct `bolt` mode rather than `neo4j` routing mode from a laptop tunnel because routed mode returns the NLB's internal DNS name, which is not reachable from your laptop.

The Bolt tunnel is also required alongside the Browser Tunnel when using Neo4j Browser.

### Browser Tunnel

Use this to open the Neo4j Browser web UI. The browser makes two connections: HTTPS to load the UI (port 7473) and Bolt to run queries (port 7687). Both tunnels must be open simultaneously.

**Single-command option.** Run from `neo4j-ee/`:

```bash
uv run browse.py                   # most recent deployment
uv run browse.py <stack-name>      # specific deployment
```

`browse.py` reads `.deploy/<stack-name>.txt`, opens both SSM port-forward tunnels in the same shell, and prints the URL and the username. It prints the password secret ARN. With `--show-password` it fetches the password from Secrets Manager with your AWS credentials and prints it. Add the `/etc/hosts` line it prints, then open the `https://<AdvertisedDNS>:7473` URL. Press Ctrl+C to close both tunnels.

**Two-terminal option.** Run from `neo4j-ee/validate-private/`:

```bash
uv run scripts/browser-tunnel.py   # localhost:7473 -> NLB:7473  (blocks; Ctrl-C to close)
uv run scripts/bolt-tunnel.py      # localhost:7687 -> NLB:7687  (blocks; Ctrl-C to close)
```

From `neo4j-ee/`, `./browser-tunnel.sh [stack-name]` and `./bolt-tunnel.sh [stack-name]` run the same two scripts. They set `AWS_PROFILE` to `marketplace` when it is unset.

The certificate SAN is the stack's `AdvertisedDNS`, so add `127.0.0.1 <AdvertisedDNS>` to your laptop's `/etc/hosts`. `browser-tunnel.py` prints the exact line. Then open `https://<AdvertisedDNS>:7473`. The self-signed certificate triggers a browser warning that you must accept. When prompted for a connection URL, enter the Bolt URI printed by `uv run browse.py` or `uv run scripts/bolt-tunnel.py`. For the password, see [Retrieve the Password](#retrieve-the-password).

> **Note:** Connection strings inside Neo4j Browser show the internal NLB DNS hostname, or `AdvertisedDNS` when the stack creates its private hosted zone. Substitute `localhost` when connecting through the tunnel.

> **Note:** Writes through Neo4j Browser go to whichever node the NLB selects, which may not be the leader, producing a `NotALeader` error. Use `uv run admin-shell` for writes.

### Retrieve the Password

```bash
./scripts/get-password.sh

# To capture it:
PASSWORD=$(./scripts/get-password.sh 2>/dev/null)
```

The password is stored in Secrets Manager at `neo4j/<stack-name>/password` as a plain string: the password value itself, not JSON. The stack KMS key encrypts it, so the caller needs `kms:Decrypt` on that key. See [IAM permissions](#prerequisites).

The `uv run` tools read the password from Secrets Manager with your own AWS credentials. They keep it in process memory and never write it to disk or pass it on a command line. The bastion role has no access to the secret. For Neo4j Browser, you type the password into the login form.

### Single Sign-On (OIDC)

Neo4j Browser and Bloom can sign users in through an OpenID Connect identity provider. Setting `OidcIssuerUri` turns this on. Password login stays enabled next to it, and operator tools keep using the native `neo4j` password.

The step-by-step procedure, including the Hume integration, is in [oidc-run-book.md](oidc-run-book.md).

#### Sign-In Settings

| Template parameter | `deploy.py` flag | `.env` key | Purpose |
|---|---|---|---|
| `OidcIssuerUri` | `--oidc-issuer-uri` | `OIDC_ISSUER_URI` | Turns single sign-on on. Neo4j reads `<issuer>/.well-known/openid-configuration`. |
| `EnableInternetEgress` | `--enable-internet-egress` | `ENABLE_INTERNET_EGRESS` | Creates the NAT gateways. A Private stack requires `true` when `OidcIssuerUri` is set. |
| `OidcClientId` | `--oidc-client-id` | `OIDC_CLIENT_ID` | Comma-separated client IDs accepted as the ID token audience. A token from any listed client is accepted. |
| `OidcBrowserClientId` | `--oidc-browser-client-id` | `OIDC_BROWSER_CLIENT_ID` | The client that Browser and Bloom sign in with. It must be in `OidcClientId`. When it is empty, the first ID in `OidcClientId` is used. |
| `OidcPrincipalClaim` | `--oidc-principal-claim` | `OIDC_PRINCIPAL_CLAIM` | The ID token claim that becomes the Neo4j username. The default is `sub`. |
| `OidcGroupsClaim` | `--oidc-groups-claim` | `OIDC_GROUPS_CLAIM` | The ID token claim that lists the user's groups. |
| `OidcAdminGroup` | `--oidc-admin-group` | `OIDC_ADMIN_GROUP` | The group mapped to the Neo4j `admin` role. |
| `OidcReaderGroup` | `--oidc-reader-group` | `OIDC_READER_GROUP` | The group mapped to the built-in `reader` role. |
| `AdvertisedDNS` | `--advertised-dns` | `ADVERTISED_DNS` | The name that Browser, the drivers and the redirect addresses use. |
| `CreatePrivateDns` | `--create-private-dns` | `CREATE_PRIVATE_DNS` | Creates a Route 53 private record for the advertised name. |
| `PrivateDnsZoneName` | `--private-dns-zone` | `PRIVATE_DNS_ZONE` | The private hosted zone to create. |

A user in neither group signs in with only the `PUBLIC` role.

Neo4j validates the ID token, not the access token. For Amazon Cognito, set the principal claim to `cognito:username` and the groups claim to `cognito:groups`. The Cognito access token has no `aud` claim, so Neo4j rejects it.

The nodes fetch the provider's signing keys over the internet. A Private stack reaches the internet through the NAT gateways, which exist only when `EnableInternetEgress` is `true`. The stack rejects `OidcIssuerUri` without it, and so does `deploy.py`. An ExistingVpc stack must provide that route itself. The node security group allows outbound port 443 to the internet only when `OidcIssuerUri` is set.

#### Redirect Addresses

The identity provider must allow these redirect addresses for the Browser client. Replace `<AdvertisedDNS>` with the stack's advertised name:

```
https://<AdvertisedDNS>:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri
https://<AdvertisedDNS>:7473/browser/
https://<AdvertisedDNS>:7473/bloom/
```

`sso` is the provider ID the stack configures. Skip the `bloom` address when Bloom is not installed. Amazon Cognito matches each address exactly, including the query string.

#### Changing Sign-In Settings on a Running Stack

A stack update changes the launch template, but running nodes keep their old configuration. The node Auto Scaling groups have no update policy. To apply a change, replace the nodes one at a time:

1. Terminate one node through its Auto Scaling group. Do not lower the desired capacity.
2. Wait until the replacement is InService and healthy in every NLB target group.
3. Move to the next node.

Each replacement takes about 8 minutes. The data volume moves to the replacement.

#### Checking a Node

Open a session on a node through SSM and look for the `dbms.security.oidc.sso.*` lines in `/etc/neo4j/neo4j.conf`. At boot, `/var/log/cloud-init-output.log` logs `OIDC enabled` or `OIDC disabled`.

#### Troubleshooting Sign-In

| Symptom | Cause | Fix |
|---|---|---|
| `DNS_PROBE_POSSIBLE` or "This site can't be reached" | The advertised name is missing from `/etc/hosts`. | Add `127.0.0.1 <AdvertisedDNS>` to `/etc/hosts`. |
| The page does not load | The Browser tunnel on 7473 is closed. | Restart `./browser-tunnel.sh`. |
| SSO is disabled in the login form, or the connection fails | The Bolt tunnel on 7687 is closed. | Restart `./bolt-tunnel.sh` and reload the page. |
| The provider shows `redirect_mismatch` | The redirect address is not registered on the Browser client. | Register the [redirect addresses](#redirect-addresses). Check that `OidcBrowserClientId` names the client that has them. If it still fails, URL-decode `redirect_uri` from the address bar and register it exactly as shown. |
| A user who should be admin gets only `PUBLIC` | The user is not in the admin group, or the groups claim or group name is wrong. | Check the user's groups in the identity provider and the `OIDC_*` values. |
| A stack update to sign-in settings has no effect | Running nodes keep their old configuration. | [Replace the nodes one at a time](#changing-sign-in-settings-on-a-running-stack). |
| A client inside the VPC cannot resolve the advertised name | The private DNS record is missing. | Deploy with `CREATE_PRIVATE_DNS=true` and a `PRIVATE_DNS_ZONE`. |
| The name does not resolve for about 7 minutes after the record is created | Route 53 holds a negative cache entry. | Wait and retry. |
| SSM closes a tunnel with "Your session timed out due to inactivity" | The tunnel was idle. | Restart it with the same command. |

### Admin Shell

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

### Ad-Hoc Cypher Queries

```bash
cd neo4j-ee/validate-private
uv run run-cypher "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition"

# Output is JSON; pipe to jq for formatting
uv run run-cypher "SHOW SERVERS YIELD name, address, state, health" | jq .

# Target a specific stack
uv run run-cypher <stack-name> "MATCH (n) RETURN count(n) AS total"
```

### Observability

```bash
cd neo4j-ee
./test-observability.sh                  # most recent deployment
./test-observability.sh <stack-name>     # specific deployment
```

| Step | What it checks | Typical duration |
|---|---|---|
| `cloudwatch` | CloudWatch agent active on all nodes | <1 min |
| `logs` | Application log group exists with expected stream count | <1 min |
| `flowlogs` | VPC flow log group exists and has ENI streams | <1 min |
| `alarm` | Failed-auth alarm transitions to ALARM after 12 bad login attempts | ~7 min |
| `cloudtrail` | A multi-region CloudTrail trail exists and is logging | <1 min |

---

## Architecture

![Neo4j EE Private Architecture](images/neo4j-private-architecture.png)

### Network Topology

**Three-node cluster:**
- VPC with three public subnets (one per AZ) for the optional NAT Gateways, and three private subnets (one per AZ) hosting the Neo4j instances
- Internal NLB with TLS listeners on 7473 (HTTPS Browser) and 7687 (Bolt), targets spread across all three private subnets
- Three Neo4j EC2 instances in private subnets, forming a Raft cluster
- Three NAT Gateways for outbound traffic from the cluster members, only when `EnableInternetEgress` is `true`
- One `t4g.small` operator bastion in a private subnet, not registered as an NLB target
- VPC interface endpoints for `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling`, and `acm` with `PrivateDnsEnabled: true`

**Single instance:**
- VPC with one public subnet (NAT Gateway) and one private subnet (EC2 instance)
- Internal NLB in the private subnet
- One NAT Gateway, only when `EnableInternetEgress` is `true`
- One `t4g.small` operator bastion

`EnableInternetEgress` defaults to `false`. Without it, the stack creates no NAT gateway and the nodes have no route out of the VPC. Set it to `true` for OIDC sign-in or for a Hume stack that shares the VPC.

The `AvailabilityZones` parameter is optional. Every deployment, including a single node, needs exactly three zones in it. A shorter list fails stack creation with an `Fn::Select` template error. Leave it blank to use the first three zones that `Fn::GetAZs` returns.

### AWS Resources Created

| AWS Resource | What it creates |
|---|---|
| VPC | New VPC with public subnets for the optional NAT Gateways and private subnets for the Neo4j instances; 3 AZs for a cluster, 1 AZ for a single instance |
| NAT Gateways | One per AZ for outbound internet access from the cluster nodes. Created only when `EnableInternetEgress` is `true` |
| Internal NLB | TLS listeners on port 7473 (HTTPS Browser) and 7687 (Bolt); routable from inside the VPC only |
| EC2 instances | 1 or 3 Neo4j nodes in private subnets; no public IPs |
| ASG per node | One Auto Scaling Group per Neo4j node, fixed at `MinSize=MaxSize=DesiredCapacity=1`, for self-healing |
| EBS data volumes | One GP3 volume per node with `DeletionPolicy: Retain`; survives stack deletion |
| Operator bastion | `t4g.small` in a private subnet, not registered as an NLB target; receives SSM sessions for operator access |
| VPC interface endpoints | `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling`, and `acm` with `PrivateDnsEnabled: true`. Each endpoint policy limits callers to principals in the stack account. AWS service calls need no NAT |
| Security groups | NLB SG (AllowedCIDR on 7473/7687 to the NLB); External SG (NLB SG as source on 7473/7687 to instances); Internal SG (cluster ports 5000/6000/7000/7688 between members only); Endpoint SG (gating access to the VPC endpoints) |
| SSM parameters | `/neo4j-ee/<stack>/` prefix; publishes VPC ID, NLB DNS, security group IDs, and secret ARN for downstream consumers |
| Secrets Manager | Neo4j admin password at `neo4j/<stack>/password` |
| CloudWatch | Log group, VPC flow logs, failed-auth alarm, CloudTrail trail |

### NLB Routing

At boot, each cluster node sets:

```
server.bolt.advertised_address = <nlb-dns>:7687
dbms.routing.default_router    = SERVER
```

When the stack creates its own private hosted zone, the Bolt address is `<AdvertisedDNS>:7687` instead. That happens with `CreatePrivateDns=true` and an empty `PrivateDnsHostedZoneId`. The stack then owns a private record that points `AdvertisedDNS` at the NLB, and the certificate covers that name. A `neo4j+s://` driver that sends no address in its routing context still gets a name it can verify.

Every routing table entry points back to the NLB. A driver connecting with `neo4j://` receives a routing table containing only the NLB, sends all subsequent requests through it, and lets Neo4j server-side routing handle leader vs. follower direction.

- **A zone you pass in:** A zone passed in `PrivateDnsHostedZoneId` keeps Bolt on the NLB name. This template creates a new VPC, so that zone cannot already be associated with it. The Existing VPC template treats a passed zone differently. See [PRIVATE-EXISTING-VPC.md](PRIVATE-EXISTING-VPC.md#relationship-to-the-private-template).
- **Peered VPCs with private DNS:** When Bolt advertises `AdvertisedDNS`, a client in a peered VPC must be able to resolve it. Associate the private hosted zone with that VPC.
- **Turning private DNS off:** A stack update from `CreatePrivateDns=true` to `false` deletes the DNS record. Running nodes keep advertising `AdvertisedDNS` until they are replaced, so routed clients fail in the meantime. Replace the nodes one at a time right after the update. Each replacement takes about 8 minutes.
- **Your own DNS record:** A certificate passed with `--cert-arn` and a CNAME you manage outside the stack still leaves Bolt on the NLB name. A future template parameter may let Bolt advertise `AdvertisedDNS` in that case too.

| Access pattern | URI | Notes |
|---|---|---|
| Same VPC | `neo4j://<nlb-dns>:7687` | Full cluster failover |
| Peered VPC / Transit Gateway | `neo4j://<nlb-dns>:7687` | NLB DNS resolves to private IPs reachable through the peering route |
| SSM tunnel | `bolt+s://<AdvertisedDNS>:7687` | Skips routing table; reliable for operator access. Map `AdvertisedDNS` to `127.0.0.1`. Use `bolt+ssc` with the self-signed default certificate. |
| Direct node IP | `bolt://<node-ip>:7687` | Bypasses NLB; single node, no failover |

**`neo4j://` through an SSM tunnel requires a custom resolver.** Via SSM, the driver connects to `localhost:7687`. The server returns a routing table with the NLB DNS, or `AdvertisedDNS` when the stack creates its private hosted zone. The driver then tries to connect to that address, which resolves to private IPs inside the VPC. Those IPs are not reachable from the operator's laptop. Use the direct URI that `bolt-tunnel.py` prints for SSM access.

### Operator Bastion: NLB Hairpin

> **Why a dedicated bastion?** To prevent NLB hairpin failures where a cluster node tunneling SSM through itself sees its own IP as the source and silently drops the reply. One connection in three fails.

The bastion is `t4g.small`, sits in the same private subnet as the cluster, and is not registered as an NLB target.

**The failure.** When SSM tunnels ran through a Neo4j cluster member instead of a dedicated bastion, every third connection timed out. NLB target health was fine; VPC endpoints, security groups, and routing all checked out.

**Root cause.** NLB target groups default to `preserve_client_ip.enabled=true`. With preservation on, each target sees the real source IP of incoming connections. When a cluster node tunnels an SSM session outward, the source IP of each new connection is that node's own private IP. The NLB flow-hashes across the three registered targets. One time in three, the hash selects the node that originated the connection. That instance then receives a packet with source IP equal to its own IP. The kernel treats that as invalid and silently drops the reply.

**The fix.** Two layers ship together:

| Layer | Mechanism |
|---|---|
| `preserve_client_ip.enabled=false` on both NLB target groups | Each target sees the NLB ENI IP as the source. A `src == dst` collision is structurally impossible. |
| Dedicated non-target bastion | The bastion's IP is outside the NLB target pool. Hairpin cannot happen by construction. |

Either mitigation alone closes the observed failure. Both ship together so the template stays correct if a future edit changes the target-group attributes or the operator-access pattern.

**Trade-off.** With client IP preservation disabled, `security.log` on the Neo4j instances records the NLB's private ENI IP as the connection source, not the real operator IP. For an internal-only deployment, per-client attribution at the Neo4j layer is already obscured by the NLB, which is an acceptable trade-off. Customers requiring the true peer IP at the application layer can retrieve it via NLB Proxy Protocol v2.

### Platform Contract

The stack publishes resource IDs via SSM under `/neo4j-ee/<stack-name>/` so that applications and operator tooling can wire themselves up without knowing stack internals. `uv run preflight` validates both groups on every run.

**Contract parameters:** All five are required.

| Parameter | Purpose |
|---|---|
| `/neo4j-ee/<stack>/vpc-id` | VPC the application should attach to |
| `/neo4j-ee/<stack>/nlb-dns` | Internal NLB DNS for Bolt connections |
| `/neo4j-ee/<stack>/external-sg-id` | Security group that accepts client/app ingress on 7473 and 7687 |
| `/neo4j-ee/<stack>/password-secret-arn` | Secrets Manager ARN for the Neo4j password |
| `/neo4j-ee/<stack>/vpc-endpoint-sg-id` | Security group attached to the VPC interface endpoints |

**Operational parameters:** These parameters are informational.

| Parameter | Purpose |
|---|---|
| `/neo4j-ee/<stack>/region` | AWS region |
| `/neo4j-ee/<stack>/stack-name` | Stack name |
| `/neo4j-ee/<stack>/private-subnet-1-id` | First private subnet |
| `/neo4j-ee/<stack>/private-subnet-2-id` | Second private subnet |
| `/neo4j-ee/<stack>/private-route-table-1-id` | Route table for the first private subnet |
| `/neo4j-ee/<stack>/app-password-secret-arn` | Secrets Manager ARN for the `neo4j_app` application user password |
| `/neo4j-ee/<stack>/app-username` | Application username, `neo4j_app`. The user has the built-in `architect` role. |
| `/neo4j-ee/<stack>/kms-key-arn` | Full ARN of the stack KMS key that encrypts both password secrets |
| `/neo4j-ee/<stack>/ca-pem` | PEM trust anchor for the NLB certificate. The value is `pending` until a node publishes it. See [Bolt TLS](#bolt-tls). |

**VPC interface endpoints.** The regional service hostnames for `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling`, and `acm` resolve to private IPs inside the VPC. No endpoint URL overrides are needed in application code, and no NAT data-processing charges apply to AWS service calls.

The `vpc-endpoint-sg-id` parameter is the mechanism by which applications opt into reaching these endpoints. Each application adds its own security group to the endpoint SG's ingress on port 443. Opening the endpoint SG to the whole VPC CIDR would allow any workload in the VPC to call SSM and Secrets Manager via PrivateLink. The published SG ID approach requires each application to explicitly opt in, creating an auditable per-application record and a clean removal path.

**Node egress.** The node security groups allow outbound traffic only to the endpoint SG on port 443, to same-region S3 on port 443, and to other cluster members on ports 5000, 6000, 7000, and 7688. A node has no route to the internet unless `EnableInternetEgress` is `true`. When `OidcIssuerUri` is set, the nodes also allow outbound port 443 to any address so Neo4j can fetch the identity provider's signing keys.

**S3 access from nodes.** Nodes reach only same-region S3. The S3 rule targets the region's AWS-managed S3 prefix list, so a URL for a bucket in another region fails. Traffic goes through the S3 gateway endpoint, whose policy allows only the AWS-owned package and agent buckets and buckets in the stack account. This access keeps SSM Agent updates, patch baselines, and Hume seeding from a stack-account bucket working. CloudFormation has no intrinsic that returns a prefix-list ID, so a small Lambda-backed custom resource looks it up at deploy time. The Lambda runs outside the VPC and can call only `ec2:DescribePrefixLists` and write its own log group.

**Placeholder egress rules.** CloudFormation adds an allow-all egress rule to a security group whose egress list is empty. The endpoint SG therefore carries a single placeholder egress rule to `127.0.0.1/32` and has no other egress. The NLB SG carries the same placeholder plus egress to the instances on the Browser and Bolt ports.

**Reading a password secret.** An application that reads `password-secret-arn` or `app-password-secret-arn` needs two grants. The first is `secretsmanager:GetSecretValue` on the secret. The second is `kms:Decrypt` on the key in `kms-key-arn`. Without the KMS grant, `GetSecretValue` fails with `AccessDeniedException`. The key policy allows decryption only through Secrets Manager in the stack's region.

**The stack key is single-Region and retained.** The key exists only in the stack's region. It carries `DeletionPolicy: Retain`, like both secrets, so a retained secret stays readable after the stack is deleted. `teardown.sh --delete-volumes` schedules the key for deletion with the 7-day minimum window.

See [`sample-private-app/README.md`](../sample-private-app/README.md) for the full application connection pattern.

### Password Security Model

- **Allowed characters:** The pattern `^[a-zA-Z0-9]{12,}$` allows only alphanumerics, 12 or more. It prevents shell metacharacter injection, such as `Test1$(cmd)` executing as root during boot.
- **Default length:** `deploy.py` generates a 32-character value
- **Storage:** Secrets Manager at `neo4j/<stack-name>/password`
- **Never in UserData:** `NoEcho: true` only suppresses values in CloudFormation API responses, not UserData. Any IAM principal with `ec2:DescribeLaunchTemplateVersions` can read UserData
- **Retrieval at boot:** Cluster nodes fetch the password with their IAM role. Its `secretsmanager:GetSecretValue` grant covers the admin and application password secrets, plus a license secret only when the matching `Install*` flag is set.

### Bolt TLS

The NLB terminates TLS on the Browser port (7473) and the Bolt port (7687) using the ACM certificate named by the `CertificateArn` parameter. The stack also requires `AdvertisedDNS`, and the certificate SAN and CN must both equal that name. The NLB re-encrypts traffic to the instances, which serve a self-signed certificate.

For local testing, `deploy.py` imports a self-signed ACM certificate when you pass no `--cert-arn`. Clients connect with `neo4j+ssc://`. See [TLS and DNS in LOCAL-TESTING.md](LOCAL-TESTING.md#tls-and-dns).

For production, use a stable customer-owned DNS name rather than the generated NLB DNS:

1. Obtain or import an ACM certificate whose SAN and CN are that DNS name.
2. Create a private DNS record that points the name to the internal NLB, or pass `--create-private-dns` to `deploy.py`.
3. Set `CertificateArn` and `AdvertisedDNS` as CloudFormation parameters when deploying the stack.

**Trust anchor.** Every node publishes the chain of the `CertificateArn` certificate to `/neo4j-ee/<stack>/ca-pem` before it signals CloudFormation. When ACM holds no chain, as for a self-signed import, the node publishes the certificate itself. Clients verify the NLB certificate against this value. A consumer needs `ssm:GetParameter` on the ca-pem ARN, or `ssm:GetParametersByPath` on `/neo4j-ee/<stack>/` for readers that load the whole path, no KMS. The parameter is not encrypted with the stack key.

**Consumers fail closed until the anchor is published.** The template creates the parameter with the value `pending`. A value of `pending`, or any value that does not start with `-----BEGIN CERTIFICATE-----`, means no node has published the anchor yet. A consumer must refuse to connect in that case. A stack update that changes the parameter's properties resets the value to `pending` until a node is replaced.

**Existing VPC.** Nodes read the certificate through the `acm` interface endpoint. With `CreateVpcEndpoints=false`, your endpoints must include `acm`, as [VPC Interface Endpoint Design](PRIVATE-EXISTING-VPC.md#vpc-interface-endpoint-design) describes.

**Operator tools.** On a `neo4j+s` stack the `uv run` tools verify the NLB chain against `ca-pem`. They skip the hostname check because the SSM tunnel replaces the server name. On a `neo4j+ssc` stack they use `+ssc`. `admin-shell` uses `+ssc` on both.

**Residual risk.** Cluster-internal ports (5000, 6000, 7000, 7688) remain plaintext between cluster members, protected by the cluster security group. An actor with `ec2:RunInstances` permission to launch into the cluster subnet with the cluster security group, `ec2:CreateTrafficMirrorSession` targeting a cluster ENI, or root on any cluster node can observe replication traffic on the wire. For regulated workloads or shared-tenancy accounts where IAM blast radius extends beyond the cluster operator team, request cluster TLS before deploying.

#### Certificate Rotation

TLS terminates at the NLB. A rotation changes the NLB listeners and the published anchor. The nodes keep their own self-signed certificates.

1. Import or issue the new certificate in ACM. Its SAN and CN must equal `AdvertisedDNS`.
2. Update the stack with the new `CertificateArn`. The NLB listeners serve the new certificate once the update completes. Running nodes do not republish the anchor.
3. Terminate one node. Its ASG launches a replacement, which publishes the new chain to `/neo4j-ee/<stack>/ca-pem` before it signals. One node is enough, because the NLB holds the certificate and the other nodes need no change.
4. Confirm that `ca-pem` changed. Its version increases, and its value matches the chain of the new certificate:

   ```bash
   aws ssm get-parameter --name /neo4j-ee/<stack-name>/ca-pem \
     --query 'Parameter.[Version,LastModifiedDate]' --region <region>
   ```

5. Run the release suite from `validate-private/`. Its trust anchor check completes a TLS handshake against the new `ca-pem`.
6. Retire the old certificate after every consumer has reread `ca-pem`.

> **Warning:** From the `CertificateArn` update until the replacement node publishes, `ca-pem` still holds the old chain. The stack update does not reset it to `pending`, because the parameter's properties do not change. A consumer that verifies against the old chain rejects the new certificate and fails closed. A consumer that pinned the old anchor keeps failing until it rereads `ca-pem`. Terminate the node right after the update to keep this window short.

---

## Local Deployment and Testing

For template developers deploying from source. The [Operator Guide](#operator-guide) covers day-to-day stack usage once the stack is running.

### Build

Regenerate the output template after editing any file in `templates/src/`:

```bash
cd neo4j-ee/templates
python build.py
```

Commit both the edited partial and the regenerated `neo4j-private.template.yaml`.

### Deploy

See [LOCAL-TESTING.md](LOCAL-TESTING.md) for deploy commands, AMI selection, and the TLS and DNS options. The default mode is Private, so no `--mode` flag is needed.

For a deeper local check after template edits, run:

```bash
python3 templates/build.py --verify
python3 -m py_compile deploy.py
cfn-lint templates/neo4j-private.template.yaml
```

### Preflight and Basic Validation

```bash
cd neo4j-ee/validate-private

uv run preflight <stack-name>                  # 12 checks: stack, bastion, endpoints, tunnel

uv run validate-private                        # most recent deployment
uv run validate-private --stack <stack-name>   # specific deployment
```

`validate-private` runs 8 checks over an SSM tunnel through the bastion: Bolt connectivity, server edition, listen address, memory configuration, data directory, APOC, GDS, and cluster roles (1 writer, expected follower count). Each check takes 3-5 seconds. Total time under 35 seconds.

### Smoke Test

```bash
uv run scripts/smoke-write.py                       # 20 CREATE/DELETE iterations
uv run scripts/smoke-write.py <stack-name> 50       # custom iteration count
```

Runs write operations through the cluster with a local Neo4j driver over an SSM tunnel through the bastion. The driver routes each write to the current leader.

### Failover Suite

```bash
cd neo4j-ee/validate-private
uv run validate-private --stack <stack-name> --suite failover
```

The four cases stop and start the Neo4j service with `systemctl` via SSM. The suite terminates no instance. The node ASGs replace a node only when EC2 reports the instance impaired. A service restart therefore keeps the node on its instance.

| Case | What it does | Typical runtime |
|---|---|---|
| `follower-with-data` | Stop a follower, write data, restart, verify data visible | ~60 s |
| `leader` | Stop the leader, verify election, write on new leader | ~90 s |
| `rolling` | Stop each node in turn; cluster stays available throughout | ~4-15 min |
| `reads` | Stop two followers; verify reads still served by remaining nodes | ~90 s |

### Resilience Suite

```bash
cd neo4j-ee/validate-private
uv run validate-private --stack <stack-name> --suite resilience
```

Two cases that terminate EC2 instances and wait for ASG replacement:

| Case | What it does | Timeout |
|---|---|---|
| `single-loss` | Terminate 1 node; verify EBS reattach, sentinel data intact, quorum reforms | 900 s |
| `total-loss` | Terminate all 3 nodes; verify all 3 volumes reattach, sentinel intact | 1200 s |

Each case writes a sentinel file to the data volume before termination and verifies it survives on the replacement instance, confirming `DeletionPolicy: Retain` and NVMe device resolution both work.

The node ASGs use EC2 health checks. The NLB health check governs routing only. A hung Neo4j process therefore keeps its instance. The NLB stops routing to that node until an operator restarts the service.

### Tear Down

```bash
./teardown.sh <stack-name>
./teardown.sh --delete-volumes <stack-name>   # also permanently deletes EBS volumes
```

If the sample private app is deployed, tear it down first or stack deletion will stall on its security group ingress rules:

```bash
cd neo4j-ee/sample-private-app && ./teardown-sample-private-app.sh <stack>
cd neo4j-ee && ./teardown.sh --delete-volumes <stack>
```

### Troubleshooting

**"Bastion SSM PingStatus = Online" fails**
The bastion UserData may still be running in the first 3 minutes after stack creation. Retry after waiting. If the check still fails after 10 minutes, confirm the bastion's IAM role has `AmazonSSMManagedInstanceCore` and the VPC has `ssm` and `ssmmessages` interface endpoints.

**"AccessDenied" on `GetSecretValue` or `GetParameter`**
The operator tools read the secret and the SSM parameters with your own AWS credentials. The bastion role has no access to the secret. Grant your identity the permissions in [IAM permissions](#prerequisites).

**"not on PATH: session-manager-plugin"**
Every `uv run` tool that connects to Neo4j opens an SSM port-forward from your machine. Install the Session Manager plugin as shown under [Prerequisites](#prerequisites).

**"NotALeader" error in Neo4j Browser**
The NLB routed a write to a follower. Use `uv run admin-shell` for writes: `neo4j://` routing directs writes to the leader automatically.

**Mounting an existing EBS snapshot and resetting the password**

> **TL;DR:** Disable auth on every node, set the password via Cypher, then re-enable auth on every node. In a cluster, the password change succeeds only on the system database leader, which replicates it to every node.

When a data volume is restored from a snapshot, the Neo4j system database on that volume already contains the auth store from the original deployment. The password in Secrets Manager will not match, producing `Neo.ClientError.Security.Unauthorized` on every connection.

`neo4j-admin dbms set-initial-password` does not help here. It only takes effect when the system database has never been initialized. On a snapshot restore the system database already exists, so the command is silently ignored.

The correct procedure is to disable authentication temporarily, set the password via Cypher, then re-enable:

```bash
# 1. Remove any existing auth_enabled entries and disable auth
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "systemctl stop neo4j",
    "sed -i \"/dbms.security.auth_enabled/d\" /etc/neo4j/neo4j.conf",
    "echo \"dbms.security.auth_enabled=false\" >> /etc/neo4j/neo4j.conf",
    "systemctl start neo4j"
  ]' --region <region>

# 2. Once Neo4j is up, set the password to match Secrets Manager.
#    The node reads the secret with its own role, so the password never
#    appears in the SSM command. cypher-shell reads it from stdin as a
#    parameter. The password is alphanumeric by the template's AllowedPattern,
#    so the double quotes around it are safe.
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "export NEO4J_PASSWORD=\"\"",
    "for i in $(seq 1 30); do cypher-shell -a bolt+ssc://localhost:7687 -u neo4j \"RETURN 1\" >/dev/null 2>&1 && break || sleep 3; done",
    "pw=$(aws secretsmanager get-secret-value --secret-id neo4j/<stack-name>/password --query SecretString --output text --region <region>)",
    "printf \":param {pw: \\\"%s\\\"}\\nALTER USER neo4j SET PASSWORD \\$pw CHANGE NOT REQUIRED;\\n\" \"$pw\" | cypher-shell -a bolt+ssc://localhost:7687 -u neo4j"
  ]' --region <region>

# 3. Re-enable auth
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "systemctl stop neo4j",
    "sed -i \"/dbms.security.auth_enabled/d\" /etc/neo4j/neo4j.conf",
    "echo \"dbms.security.auth_enabled=true\" >> /etc/neo4j/neo4j.conf",
    "systemctl start neo4j"
  ]' --region <region>
```

Get `<node-id>` from the ASG:

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <Neo4jNode1ASGName> \
  --region <region> \
  --query 'AutoScalingGroups[0].Instances[*].InstanceId' \
  --output text
```

The `<Neo4jNode1ASGName>` value is in `.deploy/<stack-name>.txt`. A three-node cluster also has `Neo4jNode2ASGName` and `Neo4jNode3ASGName`.

**Three-node cluster:** The auth store lives in the replicated system database. `ALTER USER` succeeds only on the system database leader, and the system database replicates the change to every node. On a follower, step 2 fails with `51N29`: "The command 'ALTER USER' must be executed on the current 'LEADER' server". That error is expected. Run the steps in this order:

1. Run step 1 on every node. The leader cannot be chosen in advance, because restarting a node to disable auth moves leadership.
2. Run step 2 on every node. Exactly one node succeeds. The followers return `51N29`.
3. Run step 3 on every node.

This order needs no leader discovery. To find the leader anyway, run this query with cypher-shell on any node after step 1:

```cypher
SHOW DATABASE system YIELD serverID, writer WHERE writer RETURN serverID
```

Match the result against each node's server ID. The `server_id` file holds one version byte followed by the 16-byte UUID:

```bash
python3 -c "import uuid; print(uuid.UUID(bytes=open('/var/lib/neo4j/data/server_id','rb').read()[1:]))"
```

> **Note:** The restarts in steps 1 and 3 do not replace the node. The node ASGs replace a node only when EC2 reports the instance impaired. The NLB stops routing to a node while Neo4j is down and resumes once its TCP check passes.

> **Note:** `dbms.security.auth_enabled` may appear multiple times in `neo4j.conf` if the recovery procedure is run more than once. Neo4j 5 treats duplicate keys as a fatal config error and refuses to start. The `sed -i "/dbms.security.auth_enabled/d"` step above removes all occurrences before adding a single clean entry, which prevents this.

Reference: [Recover admin user and password, Neo4j Operations Manual](https://neo4j.com/docs/operations-manual/current/authentication-authorization/password-and-user-recovery/)

---

## EBS Snapshots

`snapshot.sh` creates EBS snapshots of all Neo4j data volumes for a deployed stack. Snapshots are incremental and stored in AWS-managed S3; snapshot size reflects used blocks only, not the allocated volume size.

```bash
./snapshot.sh                  # most recent deployment
./snapshot.sh <stack-name>     # specific deployment
./snapshot.sh --list           # list all snapshots for the most recent deployment
./snapshot.sh --list <stack-name>  # list all snapshots for a specific deployment
```

The script reads `.deploy/<stack-name>.txt` for volume IDs and region. Each snapshot is tagged with the stack name and date. For a three-node cluster all three volumes are snapshotted in the same run.

`--list` queries by the `stack` tag and shows snapshot ID, timestamp, state, progress, volume size, and description.

**Note on memory.** The `neo4j-admin database backup` recovery phase runs inside the Neo4j JVM and competes with the running database for heap. On an `r8i.xlarge` (32 GB) with a 19.3 GB heap and a 21.5 GB store, the combined footprint exceeds available RAM and triggers an OOM kill. EBS snapshots do not touch the JVM; they are taken at the block-device level and are safe to run on any instance size without restarting Neo4j.
