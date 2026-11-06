# Neo4j EE Public: Operator Guide

Applies to any running public stack, whether deployed from the Marketplace or from source.

## Prerequisites

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
| `kms:Decrypt` | The stack key in `/neo4j-ee/<stack-name>/kms-key-arn` | Retrieving the Neo4j admin password |

**Deploying identity**

The identity that creates the stack also creates the two password secrets. Secrets Manager encrypts them with the stack KMS key on that identity's behalf. The identity therefore needs `kms:GenerateDataKey` on the key. The key does not exist until the stack creates it, so scope the grant with the `kms:ViaService` condition for `secretsmanager.<region>.amazonaws.com` instead of a key ARN. An identity with `AdministratorAccess` already has this permission.
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

## Access

Connect directly from your machine. No SSM tunnel is required.

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

## Retrieve the Password

The Neo4j admin password is stored in Secrets Manager at `neo4j/<stack-name>/password` as a plain string: the password value itself, not JSON. The secret ARN is in the stack outputs as `Neo4jPasswordSecretArn`. The stack KMS key encrypts the secret, so the caller needs `kms:Decrypt` on that key. The stack publishes the key's full ARN at `/neo4j-ee/<stack-name>/kms-key-arn`. The key exists only in the stack's region and is retained with the secret when the stack is deleted.

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

## Verify with the sample app

`sample-public-app/` is a `uv`-managed Python script that reads the public SSM contract, fetches the password, opens a Bolt driver against the NLB, runs a small demo, and prints a JSON result. Use it to confirm the stack is reachable and the routing table looks right.

```bash
cd sample-public-app
uv run sample-public-app                       # most recent EE deploy
uv run sample-public-app <stack-name>          # specific stack
```

The full workflow, the six SSM contract keys it requires, the URI selection rules (`bolt://` vs `neo4j://` vs `neo4j+s://`), and the JSON shape it returns are documented in [`sample-public-app/README.md`](../../../sample-public-app/README.md).

## Observability Checks

Verify CloudWatch agent, application logs, VPC flow logs, failed-auth alarm, and an account-level CloudTrail check:

```bash
./scripts/test-observability.sh                  # most recent deployment
./scripts/test-observability.sh <stack-name>     # specific deployment
./scripts/test-observability.sh --step <name>    # single step
```

| Step | What it checks | Typical duration |
|---|---|---|
| `cloudwatch` | CloudWatch agent active on all nodes (via SSM Run Command) | <1 min |
| `logs` | Application log group exists with the expected stream count | <1 min |
| `flowlogs` | VPC flow log group exists and has ENI streams | <1 min |
| `alarm` | Failed-auth alarm transitions to ALARM after 12 bad login attempts | ~7 min |
| `cloudtrail` | Account-level check: the region's CloudTrail event history holds a management event from the last 24 hours. It is not a stack resource. | <1 min |
