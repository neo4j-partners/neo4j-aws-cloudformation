# Neo4j Community Edition — AWS Marketplace

CloudFormation template and tooling for the Neo4j Community Edition AWS Marketplace listing.

## Quick Start — Console Deployment

If you deployed from the AWS console, you can test your stack by generating a local outputs file and running the test suite.

```bash
cd neo4j-ce

# Set your neo4j password in .env (see .env.sample)
cp .env.sample .env
# edit .env with your password

# Generate the outputs file (reads password from .env)
./generate-outputs.sh --stack-name <stack-name> --region <region>

# Full test (connectivity + EBS resilience via instance termination)
cd test_ce
uv run test-ce

# Test a specific deployment
uv run test-ce --stack <stack-name>

# Quick connectivity-only test
uv run test-ce --simple

# Custom timeout for ASG replacement (default 600s)
uv run test-ce --timeout 900
```

**Simple mode** (`--simple`) checks: HTTP API, authentication, Bolt connectivity, and APOC availability.

**Full mode** (default) adds: writes sentinel data, terminates the instance, waits for ASG to replace it, then verifies the data persisted on the new instance via the retained EBS volume.

The test suite reads connection info from `.deploy/<stack-name>.txt` (newest deployment by default).

## Quick Start — CLI Deployment

All scripts use the `marketplace` AWS CLI profile. Export it once so every command picks it up:

```bash
export AWS_PROFILE=marketplace
```

### 1. Build the Base AMI

```bash
./marketplace/create-ami.sh
```

This builds the base OS AMI (Neo4j is installed at deploy time from yum) and writes the ID to `marketplace/ami-id.txt`. See [marketplace/README.md](marketplace/README.md) for details.

### 2. Test the AMI

```bash
# Test (reads ami-id.txt automatically)
./test-ami.sh
```

### 3. Deploy the Stack

```bash
./deploy.py                        # default: t3.medium, random region
./deploy.py r8i                    # memory optimized (r8i.large)
./deploy.py --region eu-west-1     # specific region (AMI auto-copied)
./deploy.py r8i --region us-east-2 # both
```

The script reads the AMI ID from `marketplace/ami-id.txt` (written by `create-ami.sh`), picks a random region (or uses `--region`), copies the AMI cross-region if needed, deploys the stack, waits for completion, then writes connection details and deploy context to `.deploy/<stack-name>.txt`.

Cross-region AMI copies can take 10-20+ minutes (especially to distant regions like `ap-southeast-*`). For a quick test, use `--region us-east-1` to skip the copy.

Multiple deployments can coexist — each gets its own output file in `.deploy/`.

To look up connection details for a deployed stack directly from CloudFormation:

```bash
aws cloudformation describe-stacks --stack-name <stack-name> --region <region> --query 'Stacks[0].Outputs' --output table
```

This returns the Neo4j Browser URL (`http://<EIP>:7474`), Bolt URI, username, and data volume ID.

### 4. Test the Stack

Both test tools read connection details from `.deploy/<stack-name>.txt`. If the stack was created with `deploy.py`, this file already exists. For stacks created through the Marketplace console (or any other method), generate it first:

```bash
./generate-outputs.sh --stack-name <stack-name> --region <region>
# reads the neo4j password from .env (STACK_PASSWORD=...)
```

Then run the tests:

```bash
cd test_ce
uv run test-ce                     # tests the most recent deployment in .deploy/
uv run test-ce --stack <stack-name> # tests a specific deployment
```

The Python test suite in `test_ce/` reads from `.deploy/` (most recently modified file by default) and runs two levels of testing:

**Simple mode** (`--simple`) — connectivity + Neo4j configuration:
1. **HTTP API** — GET the discovery endpoint, verify `neo4j_version` is present
2. **Authentication** — POST a Cypher statement with Basic Auth, expect HTTP 200
3. **Bolt connectivity** — connect via the Neo4j driver and execute `RETURN 1`
4. **APOC plugin** — call `apoc.version()` (skipped if APOC not installed)
5. **Neo4j server status** — verify Community Edition via `dbms.components()`
6. **Listen address** — confirm bound to `0.0.0.0`
7. **Advertised address** — confirm matches the Elastic IP
8. **Memory configuration** — verify heap and page cache are set
9. **Data directory** — confirm `/data` (the persistent EBS mount)

**Full mode** (default) — simple mode + infrastructure validation + EBS persistence:
10. **CloudFormation stack status** — verify `CREATE_COMPLETE`
11. **Security group ports** — verify 7474 and 7687 are open
12. **Elastic IP association** — verify EIP is associated with an instance
13. **ASG configuration** — verify min=max=desired=1
14. **EBS data volume** — verify volume is attached to the running instance
15. **Write sentinel data** — create a `Sentinel` node with a unique test ID
16. **Terminate EC2 instance** — kill the running instance via the ASG
17. **Wait for ASG replacement** — poll the ASG until a new instance is InService
18. **Re-run connectivity tests** — verify all checks on the replacement
19. **Verify sentinel data persisted** — confirm the sentinel node survived instance replacement (proves the EBS volume was reattached)

```bash
uv run test-ce                              # full mode (connectivity + infra + EBS resilience)
uv run test-ce --simple                     # connectivity + config checks only
uv run test-ce --stack <stack-name>         # test a specific deployment
uv run test-ce --password pw                # override password from outputs file
uv run test-ce --timeout 900                # ASG replacement timeout in seconds (default: 600)
```

> The `test-stack.sh` bash script runs similar checks (HTTP, Bolt, auth, stack status, security groups, Neo4j config, APOC, Movies dataset). It requires `cypher-shell` and also accepts `--stack <stack-name>`. If no password is available in the outputs file, both tools prompt interactively.

### 5. Tear Down

```bash
./teardown.sh                  # tears down the most recent deployment
./teardown.sh <stack-name>     # tears down a specific deployment
```

Deletes the CloudFormation stack, the SSM parameter created by `deploy.py`, any cross-region AMI copy, and removes the deployment file from `.deploy/`.

## What Gets Deployed

- VPC with a single public subnet
- Elastic IP (stable public address, re-associated on instance replacement)
- Auto Scaling Group (fixed at 1 instance) with EC2 health checks
- GP3 EBS data volume (encrypted, persists across instance replacement)
- Security group allowing inbound on 7474 and 7687

## Files

| File | Purpose |
|---|---|
| `neo4j.template.yaml` | CloudFormation template |
| `deploy.py` | Local deploy helper — creates stack, waits, writes outputs to `.deploy/` |
| `generate-outputs.sh` | Creates `.deploy/<stack>.txt` from any existing stack (Marketplace deploys, etc.) |
| `test-stack.sh` | Bash test suite (HTTP, Bolt, auth, stack status, security groups, Neo4j config, APOC) |
| `test_ce/` | Python test suite — connectivity, Neo4j config, infrastructure, EBS resilience |
| `teardown.sh` | Deletes the stack, SSM parameter, copied AMI, and deployment file |
| `docs/` | Architecture documentation. See [docs/README.md](docs/README.md) |
| `marketplace/` | AMI build scripts and Marketplace publishing instructions |
| `marketplace/ami-id.txt` | AMI ID from last build (gitignored) |
| `.deploy/` | Deployment output files — one per stack (gitignored) |
