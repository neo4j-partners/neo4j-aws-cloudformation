# Neo4j EE Private, Existing VPC: Local Deployment and Testing

See [Local Testing guide](../../guides/local-testing.md) for AMI selection, the self-signed certificate, and the private DNS options. This section covers the Existing VPC specifics.

## Build

Regenerate the output template after editing any file in `templates/src/`:

```bash
cd neo4j-ee/templates
python build.py
```

Commit both the edited partial and the regenerated `neo4j-private-existing-vpc.template.yaml`.

## Deploy

Pass the VPC, subnet, and route table IDs at deploy time. `--private-route-table-1` is required, and a three-node cluster also needs `--subnet-2` and `--subnet-3`:

```bash
cd neo4j-ee

# 1-node memory-optimized instance in us-east-2
./deploy.py --mode ExistingVpc \
  --number-of-servers 1 \
  --region us-east-2 \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-1 subnet-0123456789abcdef0 \
  --private-route-table-1 rtb-0123456789abcdef0 \
  r8i.xlarge

# 3-node (three subnets required, one per AZ)
./deploy.py --mode ExistingVpc \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-1 subnet-11111111111111111 \
  --subnet-2 subnet-22222222222222222 \
  --subnet-3 subnet-33333333333333333 \
  --private-route-table-1 rtb-0123456789abcdef0

# With Marketplace AMI
./deploy.py --marketplace --mode ExistingVpc \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-1 subnet-0123456789abcdef0 \
  --private-route-table-1 rtb-0123456789abcdef0

# VPC already has interface endpoints; skip endpoint creation
./deploy.py --mode ExistingVpc \
  --vpc-id vpc-0123456789abcdef0 --subnet-1 subnet-0123456789abcdef0 \
  --private-route-table-1 rtb-0123456789abcdef0 \
  --create-vpc-endpoints false \
  --existing-endpoint-sg-id sg-0123456789abcdef0
```

The deploy script writes outputs to `.deploy/<stack-name>.txt`. Stack creation takes 10-20 minutes (includes AMI copy if the region differs from the source AMI region; pin `--region` to the source region to skip the copy).

For a deeper local check after template edits, run:

```bash
python3 templates/build.py --verify
python3 -m py_compile deploy.py
```

`cfn-lint templates/neo4j-private-existing-vpc.template.yaml` is also useful, but the current template emits known `W1030` warnings for optional blank ExistingVpc parameters. Treat any other warnings or errors as failures.

## Create a Test VPC

For automated testing, `scripts/create-test-vpc.py` provisions a minimal private-networking VPC (`10.42.0.0/16`) and writes all resource IDs to `.deploy/vpc-<ts>.txt`. `deploy.py` reads that file automatically when `--mode ExistingVpc` and no `--vpc-id` is provided:

```bash
# Path A: template creates endpoints (default)
scripts/create-test-vpc.py --region us-east-1
./deploy.py --mode ExistingVpc --number-of-servers 3

# Path B: VPC already has endpoints
scripts/create-test-vpc.py --region us-east-1 --with-endpoints
./deploy.py --mode ExistingVpc --number-of-servers 1 \
  --create-vpc-endpoints false

# Select a specific VPC file when multiple exist
./deploy.py --mode ExistingVpc --vpc-file .deploy/vpc-<ts>.txt
```

Tear down the test VPC after the stack is deleted:

```bash
scripts/teardown-test-vpc.py       # deletes test VPC, NAT gateways, subnets, endpoints, removes vpc-*.txt
```

`teardown-test-vpc.py` defaults to the most recently modified `vpc-*.txt` in `.deploy/`. Pass a VPC deployment name (`vpc-<ts>`) to target a specific one.

> **Note:** The template does not create NAT Gateways. The test VPC created by `create-test-vpc.py` does provision NAT Gateways; tear it down promptly after testing.

## Path A: Template Creates Endpoints (CI Gate)

```bash
cd neo4j-ee

# 1. Create test VPC (no endpoints)
scripts/create-test-vpc.py --region us-east-1

# 2. Deploy 3-node cluster (auto-detects vpc-*.txt)
./deploy.py --mode ExistingVpc --number-of-servers 3
STACK=$(ls -t .deploy/test-ee-*.txt | head -1 | xargs basename | sed 's/\.txt$//')

# 3. Preflight (12 checks: stack, bastion, endpoints)
cd validate-private
uv run preflight "$STACK"

# 4. Basic cluster validation (8 checks)
uv run validate-private --stack "$STACK"

# 5. Failover suite
uv run validate-private --stack "$STACK" --suite failover

# 6. Resilience suite
uv run validate-private --stack "$STACK" --suite resilience

# 7. Tear down. See Tear Down below.
```

## Path B: Pre-Existing Endpoints

```bash
cd neo4j-ee

# 1. Create test VPC with endpoints
scripts/create-test-vpc.py --region us-east-1 --with-endpoints

# 2. Deploy 1-node cluster (auto-detects VPC file and reads EndpointSgId)
./deploy.py --mode ExistingVpc --number-of-servers 1 \
  --create-vpc-endpoints false
STACK=$(ls -t .deploy/test-ee-*.txt | head -1 | xargs basename | sed 's/\.txt$//')

# 3. Preflight: endpoint reachability confirms the wiring the template added
cd validate-private
uv run preflight "$STACK"

# 4. Basic cluster validation (cluster roles: 1 writer for 1-node is PASS)
uv run validate-private --stack "$STACK"

# 5. Tear down. See Tear Down below.
```

## Tear Down

Always tear down the stack before the test VPC. The stack adds `AWS::EC2::SecurityGroupIngress` rules to VPC resources; if those rules remain when the VPC is deleted, the deletion stalls.

```bash
# 1. Delete the stack (removes SecurityGroupIngress rules automatically)
cd neo4j-ee
./teardown.sh --delete-volumes <stack-name>

# 2. Delete the test VPC and its NAT Gateways, subnets, and endpoints
scripts/teardown-test-vpc.py
```

`teardown-test-vpc.py` defaults to the most recently modified `vpc-*.txt` in `.deploy/`. Pass a VPC deployment name (`vpc-<ts>`) to target a specific one.
