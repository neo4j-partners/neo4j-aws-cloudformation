# Neo4j EE Private, Existing VPC: Operator Guide

Applies to any running ExistingVpc stack, whether deployed from the Marketplace or from source.

## Prerequisites

**AWS tooling, Python tooling, and IAM permissions** are the same as the Private template. See [Prerequisites in the Private Operator Guide](../private/operator-guide.md#prerequisites).

**VPC requirements:**
- Private subnets with outbound internet routing (NAT Gateway, Transit Gateway, or equivalent)
- One subnet for a single-instance deployment; three subnets in different AZs for a three-node cluster
- No pre-existing VPC interface endpoints for `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling`, or `acm` when `CreateVpcEndpoints=true`. Creating a duplicate endpoint fails the deployment.

**`AllowedCIDR`** defaults to `10.0.0.0/16`. Pass `--allowed-cidr` explicitly if your VPC uses a different CIDR.

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
./test-observability.sh                  # most recent deployment
./test-observability.sh <stack-name>     # specific deployment
```

Same five steps as the Private template: CloudWatch agent, log streams, VPC flow logs, failed-auth alarm, and an account-level CloudTrail check.
