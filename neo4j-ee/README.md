# Neo4j Enterprise Edition: AWS CloudFormation

CloudFormation templates and operator tooling for the Neo4j Enterprise Edition AWS Marketplace listing. Each template deploys a one or three-node Neo4j cluster fronted by a Network Load Balancer. Every node runs in a dedicated Auto Scaling group for self-healing, with a GP3 EBS data volume that is retained across stack deletion. The Private and Private Existing VPC templates terminate TLS at the NLB by default (HTTPS Browser on 7473, Bolt on 7687); the Public template is plain TCP unless `--enable-public-tls` is set with a certificate. Three topologies are available to match different infrastructure requirements: a public new-VPC deployment for evaluation, a private new-VPC deployment for production, and a private deployment into an existing VPC for environments with pre-existing network infrastructure.

---

## Templates

| Template | Topology | Use case | Guide |
|---|---|---|---|
| **Public** | New VPC, public subnets, internet-facing NLB | Proof of concept, demos, and evaluation. Instances have public IPs; no bastion required. Direct Bolt and Browser access from `AllowedCIDR`. | [docs/PUBLIC.md](docs/PUBLIC.md) |
| **Private** | New VPC, private subnets, internal NLB, SSM bastion | Production and staging where AWS manages the VPC. Instances have no public IPs; operator access via SSM Session Manager port forwarding through a dedicated bastion. | [docs/PRIVATE.md](docs/PRIVATE.md) |
| **Private, Existing VPC** | Your VPC, private subnets, internal NLB, SSM bastion | Production into a pre-existing network: peered VPC, Transit Gateway, or Direct Connect. No VPC or NAT provisioning; requires caller-supplied VPC and subnet IDs. | [docs/PRIVATE-EXISTING-VPC.md](docs/PRIVATE-EXISTING-VPC.md) |

---

## Sample Applications

Two samples cover the two consumer paths. Pick the one that matches the topology you deployed.

| Sample | Use when | What it shows |
|---|---|---|
| [`sample-public-app/`](sample-public-app/README.md) | Connecting from a laptop to a **Public** stack | The off-VPC discovery flow: seven-key public SSM contract, password retrieval from Secrets Manager, topology-aware Bolt URI selection (`bolt://` / `neo4j://` / `neo4j+s://`), and the routing-table view on three-node clusters. `uv`-managed Python script. |
| [`sample-private-app/`](sample-private-app/README.md) | Connecting from inside the VPC to a **Private** or **ExistingVpc** stack | The in-VPC application flow: SSM and VPC-endpoint security group wiring, two Python 3.13 Lambdas behind IAM-authenticated Function URLs, and a resilience test that stops and restarts a follower via SSM. |

---

## For Marketplace Users

Deploy from the AWS Marketplace listing. By default, no Neo4j plugins are installed: the templates set `InstallBloom=false` and `InstallGDS=false`. To enable Bloom or Graph Data Science at launch, create a Secrets Manager secret holding the license file contents and pass its ARN as `BloomLicenseSecretArn` or `GdsLicenseSecretArn`. Stacks that request a plugin without supplying its license ARN are rejected at parameter validation before any resource is created. See the "Plugin licenses (optional)" prerequisite in the topology guide for your deployment.

Once the stack is complete, the guide for your topology covers prerequisites, accessing the cluster, retrieving the password, observability checks, and tear down:

- **Public:** [docs/PUBLIC.md — Operator Guide](docs/PUBLIC.md#operator-guide)
- **Private:** [docs/PRIVATE.md — Operator Guide](docs/PRIVATE.md#operator-guide)
- **Private, Existing VPC:** [docs/PRIVATE-EXISTING-VPC.md — Operator Guide](docs/PRIVATE-EXISTING-VPC.md#operator-guide)

---

## Local Development

All scripts read `AWS_PROFILE` from the environment and fall back to the `default` profile:

```bash
export AWS_PROFILE=<your-profile>   # omit to use your default profile
```

> **Marketplace publishing scripts**: run `marketplace/create-ami.sh` against the `neo4j-marketplace` account (`385155106615`) with `AWS_PROFILE=marketplace` for Marketplace builds. For local AMI iteration only, `marketplace/create-ami.sh` also supports the guarded default-account mode `AMI_BUILD_MODE=iteration AWS_PROFILE=default`. `marketplace/test-ami.sh` should use the same account that owns the AMI under test. All other scripts work with any account that has CloudFormation, SSM, EC2, and IAM permissions.

### Deploy

```bash
# Private (default): 3-node cluster, t3.medium
./deploy.py --region us-east-1

# Public
./deploy.py --mode Public --region us-east-1

# Existing VPC
./deploy.py --mode ExistingVpc --vpc-id vpc-xxxx --subnet-1 subnet-xxxx

# Common flags (apply to all modes)
./deploy.py --number-of-servers 1           # single instance instead of 3-node
./deploy.py r8i.xlarge                      # memory-optimized instance type
./deploy.py --marketplace                   # use the published Marketplace AMI
./deploy.py --alert-email you@example.com   # enable CloudWatch alarm emails

# Private/ExistingVpc terminate TLS at the NLB by default: with no --cert-arn a
# self-signed ACM cert is auto-imported so tooling connects with neo4j+ssc://.
# Public is plain TCP unless TLS is opted in:
./deploy.py --mode Public --enable-public-tls --cert-arn <acm-arn> --advertised-dns <dns>
```

> **`deploy.py` defaults differ from Marketplace template defaults.** `deploy.py` installs both Bloom and Graph Data Science by default so local validation exercises the same plugin surface customers tend to enable. The Marketplace templates default `InstallBloom=false` and `InstallGDS=false`. The buyer-facing flow is covered in the topology guides ([Public](docs/PUBLIC.md#prerequisites), [Private](docs/PRIVATE.md#prerequisites), [Private Existing VPC](docs/PRIVATE-EXISTING-VPC.md#prerequisites)).

For licensed local validation, place the license files at
`neo4j-ee/.licenses/bloom.license` and `neo4j-ee/.licenses/gds.license`;
`deploy.py` uploads any matching local file to Secrets Manager, installs it on
each Neo4j node, records the secret ARN in `.deploy/<stack>.txt`, and
`teardown.sh` removes auto-created license secrets. Use
`--bloom-license-secret-id` or `--gds-license-secret-id` to reuse existing
Secrets Manager secrets, or `--no-local-licenses` to skip local file discovery.

### Look Up Connection Details

```bash
aws cloudformation describe-stacks \
  --stack-name <stack-name> \
  --region <region> \
  --query 'Stacks[0].Outputs' \
  --output table
```

### Tear Down

```bash
./teardown.sh                         # most recent deployment
./teardown.sh <stack-name>            # specific deployment
./teardown.sh --delete-volumes        # also permanently deletes EBS data volumes
```

EBS data volumes have `DeletionPolicy: Retain` and survive stack deletion by design. `teardown.sh` prints the retained volume IDs.

Multiple deployments can coexist; each gets its own file in `.deploy/`.

---

## Modifying Templates

> **Required reading before changing any template, the AMI, UserData, or Neo4j config:** [docs/architecture/template-architecture.md](docs/architecture/template-architecture.md). It is the finalized architecture contract. Place every new config value or boot logic by its Placement Decision Rule, and treat its Prohibitions as binding.

The three output templates are assembled from source partials in `templates/src/`. Edit the partials, then regenerate:

```bash
cd templates
python build.py
```

Commit both the edited partial(s) and the regenerated template file(s). The `--verify` flag checks whether the committed output is current, useful in CI:

```bash
python build.py --verify
```

---

## Marketplace Reference

[docs/marketplace-reference.md](docs/marketplace-reference.md) covers the CloudFormation best practices required for the AWS Marketplace listing: security group patterns, IAM scoping, IMDSv2, AMI parameter type, and the full requirements checklist.

---

## Files

| File | Purpose |
|---|---|
| `templates/neo4j-private.template.yaml` | CloudFormation template for private, new VPC deployments |
| `templates/neo4j-public.template.yaml` | CloudFormation template for public, new VPC deployments |
| `templates/neo4j-private-existing-vpc.template.yaml` | CloudFormation template for private, existing VPC deployments |
| `templates/src/` | Source partials assembled by `build.py` |
| `templates/build.py` | Assembles the three templates from `templates/src/` partials |
| `deploy.py` | Deploy helper: creates stack, waits, writes outputs to `.deploy/` |
| `teardown.sh` | Deletes the stack, SSM parameter, copied AMI, secrets, and deployment file |
| `test-observability.sh` | Observability checks: CloudWatch, logs, flow logs, alarm, CloudTrail |
| `validate-private/` | Operator tooling for Private and ExistingVpc stacks |
| `marketplace/` | AMI build and test scripts |
| `marketplace/create-ami.sh` | Builds AMI, writes ID to `marketplace/ami-id.txt` |
| `marketplace/test-ami.sh` | SSM-based AMI verification; no SSH key required |
| `scripts/create-test-vpc.py` | Creates a minimal test VPC for ExistingVpc template testing |
| `scripts/teardown-test-vpc.py` | Deletes a test VPC created by `create-test-vpc.py` |
| `sample-public-app/` | Off-VPC sample app connecting to a Public cluster |
| `sample-private-app/` | Sample Lambda app connecting to a private cluster |
| `scripts/certificate.py` | Request a DNS-validated ACM cert (or import a self-signed one) and record it under `.deploy/` |
| `.deploy/` | Deployment output files: one per stack (gitignored) |
