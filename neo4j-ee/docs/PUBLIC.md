# Neo4j EE: Public

`neo4j-public.template.yaml` deploys a Neo4j Enterprise cluster in public subnets with an internet-facing Network Load Balancer.

- **What it deploys:** Neo4j EE cluster (1 or 3 nodes) in public subnets behind an internet-facing NLB
- **Public exposure:** every instance has a public IP; client traffic gated by `AllowedCIDR` at the NLB security group
- **Operator access:** direct from your machine — no bastion, no SSM tunneling
- **When to use:** proof-of-concept, demos, evaluation. Production and regulated workloads should use the [Private template](PRIVATE.md)

> **Marketplace operator** (deployed from AWS Marketplace, running stack):
> Start with [Prerequisites](#prerequisites) and the [Operator Guide](#operator-guide) below.
>
> **Template developer** (working on the templates, deploying from source):
> Start with [Local Deployment and Testing](#local-deployment-and-testing).
> The [Operator Guide](#operator-guide) applies once your stack is running.

## Contents

- [Operator Guide](#operator-guide)
  - [Prerequisites](#prerequisites)
  - [Access](#access)
  - [Retrieve the Password](#retrieve-the-password)
  - [Verify with the sample app](#verify-with-the-sample-app)
  - [Observability Checks](#observability-checks)
- [Architecture](#architecture)
  - [Network Topology](#network-topology)
  - [AWS Resources Created](#aws-resources-created)
  - [Security Configuration](#security-configuration)
  - [NLB Routing](#nlb-routing)
  - [EBS Persistence](#ebs-persistence)
  - [Two-Layer Security Group Design](#two-layer-security-group-design)
- [Local Deployment and Testing](#local-deployment-and-testing)
  - [Basic Testing](#basic-testing)
  - [Build](#build)
  - [Certificates](#certificates)
  - [Deploy](#deploy)
  - [Functional and Cluster Tests](#functional-and-cluster-tests)
  - [Tear Down](#tear-down)

---

## Operator Guide

Applies to any running public stack, whether deployed from the Marketplace or from source.

### Prerequisites

**AWS tooling**

```bash
aws --version         # AWS CLI v2
```

**IAM permissions**

These are the minimum permissions the operator's local IAM principal (user or assumed role) needs to run the tools in this guide. Each permission corresponds to API calls made from the operator's machine. The cluster nodes use a separate IAM role scoped to what they need at boot.

| Permission | Resource | Used by |
|---|---|---|
| `cloudformation:DescribeStacks` | The stack ARN | `deploy.py` (reads stack outputs), observability and teardown scripts |
| `secretsmanager:GetSecretValue`, `secretsmanager:DescribeSecret` | `neo4j/<stack-name>/password` | Retrieving the Neo4j admin password |
| `ssm:SendCommand`, `ssm:GetCommandInvocation`, `ssm:DescribeInstanceInformation` | The cluster EC2 instances | `test-observability.sh` (checks CloudWatch agent via SSM Run Command) |

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

### Access

Connect directly from your machine — no SSM tunneling required.

- **Neo4j Browser:** `http://<NLB DNS>:7474`
- **Bolt:** `neo4j://<NLB DNS>:7687`
- **Ingress filter:** connections from outside `AllowedCIDR` are dropped at the NLB security group

Connection details are in the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name <stack-name> \
  --region <region> \
  --query 'Stacks[0].Outputs' \
  --output table
```

### Retrieve the Password

The Neo4j admin password is stored in Secrets Manager at `neo4j/<stack-name>/password` as a plain string: the password value itself, not JSON. The secret ARN is in the stack outputs as `Neo4jPasswordSecretArn`.

```bash
aws secretsmanager get-secret-value \
  --secret-id <password-secret-arn> \
  --query SecretString --output text

# Or use the ready-to-run command from the stack outputs:
aws cloudformation describe-stacks \
  --stack-name <stack-name> --region <region> \
  --query 'Stacks[0].Outputs[?OutputKey==`Neo4jPasswordRetrieveCommand`].OutputValue' \
  --output text | bash
```

### Verify with the sample app

`sample-public-app/` is a `uv`-managed Python script that reads the public SSM contract, fetches the password, opens a Bolt driver against the NLB, runs a small demo, and prints a JSON result. Use it to confirm the stack is reachable and the routing table looks right.

```bash
cd sample-public-app
uv run sample-public-app                       # most recent EE deploy
uv run sample-public-app <stack-name>          # specific stack
```

The full workflow, the seven-key SSM contract it consumes, the URI selection rules (`bolt://` vs `neo4j://` vs `neo4j+s://`), and the JSON shape it returns are documented in [`sample-public-app/README.md`](../sample-public-app/README.md).

### Observability Checks

Verify CloudWatch agent, application logs, VPC flow logs, failed-auth alarm, and CloudTrail:

```bash
./test-observability.sh                  # most recent deployment
./test-observability.sh <stack-name>     # specific deployment
./test-observability.sh --step <name>    # single step
```

| Step | What it checks | Typical duration |
|---|---|---|
| `cloudwatch` | CloudWatch agent active on all nodes (via SSM Run Command) | <1 min |
| `logs` | Application log group exists with the expected stream count | <1 min |
| `flowlogs` | VPC flow log group exists and has ENI streams | <1 min |
| `alarm` | Failed-auth alarm transitions to ALARM after 12 bad login attempts | ~7 min |
| `cloudtrail` | A multi-region CloudTrail trail exists and is logging | <1 min |

---

## Architecture

![Neo4j EE Public Architecture](images/neo4j-public-architecture.png)

### Network Topology

Three-node cluster:
- VPC with three public subnets, one per AZ
- Internet-facing NLB distributing traffic across all three subnets
- Three EC2 instances with public IPs, no NAT Gateways, no private subnets
- Internal security group restricting cluster ports (5000, 6000, 7000, 7688) to cluster members only

Single-instance:
- VPC with one public subnet
- Internet-facing NLB in that subnet
- One EC2 instance with a public IP

### AWS Resources Created

| AWS Resource | What it creates |
|---|---|
| VPC | New VPC with public subnets: one per AZ for a 3-node cluster, one for a single instance |
| Internet Gateway | Outbound internet access; no NAT Gateways needed |
| Internet-facing NLB | Listeners on 7474 (HTTP Browser) and 7687 (Bolt) |
| EC2 instances | 1 or 3 Neo4j nodes with public IPs; no NAT, no private subnets |
| ASG per node | One Auto Scaling Group per Neo4j node, fixed at `MinSize=MaxSize=DesiredCapacity=1`, for self-healing |
| EBS data volumes | One GP3 volume per node with `DeletionPolicy: Retain`; survives stack deletion |
| Security groups | `NLBSecurityGroup` (AllowedCIDR on Browser and Bolt ports to the NLB); `ExternalSecurityGroup` (NLBSecurityGroup as source on Browser and Bolt ports to the instances); `InternalSecurityGroup` (cluster ports 5000/6000/7000/7688 between cluster members only) |
| Secrets Manager | Neo4j admin password at `neo4j/<stack>/password` |
| CloudWatch | Log group, VPC flow logs, failed-auth alarm, CloudTrail trail |

### Security Configuration

| Setting | Value | Notes |
|---|---|---|
| `AllowedCIDR` | Required | CIDR allowed to reach Browser and Bolt ports. `0.0.0.0/0` is rejected. `deploy.py` defaults to `<your-public-ip>/32`. |
| NLB security group | Filters external traffic | `AllowedCIDR` on 7474/7687 |
| Instance security group | Sources from NLB SG | Allows both forwarded client traffic and NLB health checks without hardcoding a VPC CIDR |
| IMDSv2 | Enforced | Instance metadata requires session tokens; IMDSv1 requests are rejected |
| JDWP (port 5005) | Disabled | Remote debug port is closed and the JVM debug flag is stripped from `neo4j.conf` at boot |
| Bolt TLS | Optional test flow | `deploy.py --tls` can enable self-signed Bolt TLS on 7687 for local testing. Browser remains HTTP on 7474. |

### NLB Routing

At boot, each cluster node advertises the NLB DNS name for Bolt routing and keeps Neo4j Browser on HTTP port 7474. Server-side routing directs writes to the leader and reads to followers automatically.

| Access pattern | URI | Notes |
|---|---|---|
| Direct from internet | `neo4j://<NLB DNS>:7687` | No customer domain is required; use for public evaluation only. |
| Direct node IP (same subnet) | `neo4j://<node-ip>:7687` | Bypasses NLB; single node, no failover. |

Public stacks do not manage public DNS records.

### EBS Persistence

Each node has a dedicated GP3 EBS data volume. `DeletionPolicy: Retain` keeps the volume when the stack is deleted or the ASG replaces an instance. On each new instance launch, UserData resolves the correct NVMe device by matching the EBS volume serial number against `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*` and mounts the volume without reformatting.

### Two-Layer Security Group Design

> **Why two SGs?** NLB health checks originate from the NLB's private VPC IPs, not from `AllowedCIDR`. Applying `AllowedCIDR` directly to the instance SG blocks health checks and fails all NLB targets.

- **`Neo4jNLBSecurityGroup` (on the NLB):** allows `AllowedCIDR` on Browser/Bolt ports — filters external client traffic without hardcoding any VPC CIDR
- **`Neo4jExternalSecurityGroup` (on the instances):** sources from `Neo4jNLBSecurityGroup` via `SourceSecurityGroupId` — allows both forwarded client traffic and NLB health checks

This pattern works for any marketplace deployment without knowing the VPC CIDR at template-authoring time.

---

## Local Deployment and Testing

### Basic Testing

The simplest possible deploy: a 3-node cluster on the default `t3.medium` instance with plain TCP on both Browser (7474) and Bolt (7687). No certificate, no DNS, no TLS flags.

```bash
cd neo4j-ee
./deploy.py --mode Public --region us-east-1
```

`deploy.py` restricts `AllowedCIDR` to `<your-public-ip>/32` automatically. The NLB DNS, password secret ARN, and a ready-to-run password retrieval command are written to `.deploy/<stack-name>.txt`. Clients connect with `neo4j://<NLB DNS>:7687` and Browser at `http://<NLB DNS>:7474`.

### Build

Regenerate the output template after editing any file in `templates/src/`:

```bash
cd neo4j-ee/templates
python build.py
```

Commit both the edited partial and the regenerated `neo4j-public.template.yaml`.

### Certificates

Public stacks default to plain TCP on Bolt. To terminate TLS at the NLB, create an ACM certificate first and pass its ARN to `deploy.py`. The `scripts/certificate.py` helper covers both flows and writes `.deploy/cert-<dns>.json` with the resulting ARN.

**Self-signed certificate (testing only)**

Generates an X.509 certificate with `openssl` and imports it into ACM. No domain ownership or DNS validation required. Clients must connect with `neo4j+ssc://` so the driver skips certificate validation.

```bash
./scripts/certificate.py --region us-east-1 --dns neo4j.test.local --selfsign
```

**DNS-validated certificate with an existing domain**

Requests a public ACM certificate validated by DNS. The `--dns` value can be any subdomain you control. For example, `neo4j-demo.example.com` works if `example.com` is in a Route 53 hosted zone on the same account.

```bash
# Route 53 zone on this account: auto-create the validation CNAME and poll until issued
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com --auto-route53

# DNS hosted elsewhere: print the CNAME, add it to your provider, then poll until issued
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com

# Print the CNAME and exit; rerun without --no-wait to poll for issuance
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com --no-wait
```

If the registered domain has no public hosted zone yet, create one and point the registrar at its NS records before requesting the certificate; `worklog/hosted-zone.md` walks through that bootstrap end-to-end.

Pass the resulting ARN to `deploy.py` to enable TLS:

```bash
./deploy.py --mode Public --region us-east-1 \
  --number-of-servers 3 --enable-public-tls \
  --cert-arn <arn> --advertised-dns neo4j-demo.example.com
```

After the stack reaches `CREATE_COMPLETE`, look up the NLB DNS in `.deploy/<stack-name>.txt` (the `Neo4jInternalDNS` line) and UPSERT a CNAME from `--advertised-dns` to the NLB so the cert SAN matches what clients resolve. The advertised name must resolve to the NLB before any client opens a `neo4j+s://` connection.

```bash
aws route53 change-resource-record-sets \
  --hosted-zone-id <zone-id> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "neo4j-demo.example.com",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{"Value": "<NLB DNS from .deploy/<stack>.txt>"}]
      }
    }]
  }'

# Verify it resolves to the NLB (typically a few seconds in Route 53):
dig +short neo4j-demo.example.com @8.8.8.8
```

Clients then connect with `neo4j+s://neo4j-demo.example.com:7687`. The driver validates the server cert against the system CA bundle on the first attempt; `neo4j+ssc://` is not required and indicates the cert chain or SAN is wrong if used as a fallback.

On teardown, remove the advertised-dns CNAME with a matching `DELETE` change-batch so the record does not linger pointing at a non-existent NLB. The ACM certificate is reusable across deploys and stays in place.

### Deploy

```bash
cd neo4j-ee

# 3-node cluster, t3.medium, random region
./deploy.py --mode Public

# Single instance
./deploy.py --mode Public --number-of-servers 1

# Memory-optimized instance
./deploy.py --mode Public r8i.xlarge

# Pin region (avoids 10-20 min AMI copy)
./deploy.py --mode Public --region us-east-1

# Use the published Marketplace AMI
./deploy.py --mode Public --marketplace

# Enable CloudWatch alarm email notifications
./deploy.py --mode Public --alert-email you@example.com

# Optional self-signed Bolt TLS test flow
./deploy.py --mode Public --tls
```

`deploy.py` detects your public IP automatically and restricts the security group to `<your-ip>/32`. Pass `--allowed-cidr` to override. The script writes outputs to `.deploy/<stack-name>.txt`.

Stack creation takes 5-10 minutes.

> **Note:** The test runner (`uv run test-neo4j --edition ee`) must execute from the same egress IP used at deploy time, or the security group blocks Bolt and HTTP connections. For CI, pass `--allowed-cidr` with a static egress IP at deploy time.

### Functional and Cluster Tests

Run the full test suite against a deployed stack:

```bash
cd ../test_neo4j
uv run test-neo4j --edition ee                     # most recent stack
uv run test-neo4j --edition ee --stack <name>      # specific stack
```

The suite covers connectivity, cluster topology, NLB scheme, volume configuration, security group rules, IMDSv2 enforcement, JDWP absence, and EBS resilience. All 29 functional checks pass on a healthy 3-node public stack.

### Tear Down

```bash
./teardown.sh <stack-name>
./teardown.sh --delete-volumes <stack-name>   # also permanently deletes EBS volumes
```
