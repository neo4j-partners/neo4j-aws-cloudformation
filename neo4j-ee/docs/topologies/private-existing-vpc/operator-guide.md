# Neo4j EE Private, Existing VPC: Operator Guide

Applies to any running ExistingVpc stack, whether deployed from the Marketplace or from source.

## Prerequisites

**AWS tooling, Python tooling, and IAM permissions** are the same as the Private template. See [Prerequisites in the Private Operator Guide](../private/operator-guide.md#prerequisites).

**VPC requirements:**
- Private subnets with outbound internet routing (NAT Gateway, Transit Gateway, or equivalent)
- One subnet for a single-instance deployment; three subnets in different AZs for a three-node cluster
- Subnets in the first three Availability Zones of the Region. The stack creates its data volumes in the first three Availability Zones that `Fn::GetAZs` returns for the Region. These are normally the first three in alphabetical order, such as `us-east-1a`, `us-east-1b` and `us-east-1c`. A node attaches the volume in its own zone and fails to boot when it finds none. A single instance needs `PrivateSubnet1Id` in the first of those zones. A three-node cluster needs one subnet in each of the three. In an account with a default VPC, `Fn::GetAZs` returns only the zones that have a default subnet.
- No pre-existing VPC interface endpoints for `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling`, or `acm` when `CreateVpcEndpoints=true`. Creating a duplicate endpoint fails the deployment.

**`AllowedCIDR`** is required and has no template default. It must include the bastion's subnet and every client subnet, so enter your VPC CIDR. The NLB security group admits Bolt and Browser only from `AllowedCIDR`. A value that misses the bastion's subnet still creates a healthy stack, but preflight fails with `Unable to retrieve routing information`. When you omit `--allowed-cidr`, `deploy.py` uses the VPC's primary CIDR. Pass `--allowed-cidr` if your subnets sit in a secondary CIDR.

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

## Access, Admin Tools, and Password

Bastion access and all operator tools (`uv run preflight`, `validate-private`, `admin-shell`, `run-cypher`, `uv run scripts/smoke-write.py`, `uv run scripts/browser-tunnel.py`, `uv run scripts/bolt-tunnel.py`) are identical to the Private template. See [the Private Operator Guide](../private/operator-guide.md) from "Access via Bastion" onward.

## Observability Checks

```bash
./scripts/test-observability.sh                  # most recent deployment
./scripts/test-observability.sh <stack-name>     # specific deployment
```

Same five steps as the Private template: CloudWatch agent, log streams, VPC flow logs, failed-auth alarm, and an account-level CloudTrail check.
