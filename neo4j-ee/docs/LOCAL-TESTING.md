# Neo4j EE: Local Testing

This guide covers deploying a test stack from source with `deploy.py`. It applies to all three topologies. For day-to-day use of a running stack, see the Operator Guide in the guide for your topology: [Public](PUBLIC.md), [Private](PRIVATE.md), or [Private Existing VPC](PRIVATE-EXISTING-VPC.md).

## Contents

- [Prerequisites](#prerequisites)
- [How deploy.py behaves](#how-deploypy-behaves)
- [Settings in .env](#settings-in-env)
- [Choose an AMI](#choose-an-ami)
- [Deploy](#deploy)
- [TLS and DNS](#tls-and-dns)
- [Validate](#validate)
- [Tear down](#tear-down)

---

## Prerequisites

- **AWS credentials:** Set `AWS_PROFILE`, or the `default` profile is used. The profile must be able to create every resource in the stack, plus the ACM certificate, SSM parameter, Secrets Manager secrets, and temporary S3 bucket that `deploy.py` creates.
- **Tools:** `uv`, `openssl`, the AWS CLI for `teardown.sh` and the checks below, and the Session Manager plugin for private stacks. `deploy.py` is a `uv` script, so it installs `boto3` on first run.
- **Neo4j licence terms:** The bootstrap sets `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes` on every node. Review the Neo4j Enterprise Edition terms before you deploy.
- **Plugin licences:** `deploy.py` installs Bloom and GDS by default, which differs from the Marketplace defaults. Place licence files at `.licenses/bloom.license` and `.licenses/gds.license`, or pass `--no-bloom --no-gds` to skip both plugins.

## How deploy.py behaves

- **Stack name:** The name is always `test-ee-<unix epoch>`. There is no `--stack-name` flag.
- **Region:** Without `--region`, the script picks a random supported region. Always pass `--region`.
- **Outputs file:** Results are written to `.deploy/<stack-name>.txt` only after the stack is created successfully. Operator tooling defaults to the most recently modified file there.
- **Password in plain text:** The script prints the full outputs block, including a `Password = ...` line, and writes the same text to the outputs file. The `.deploy/` directory is gitignored, and `teardown.sh` removes the file. The password is also stored in Secrets Manager at `neo4j/<stack-name>/password`. Do not share terminal logs or the outputs file.
- **Temporary S3 bucket:** The script uploads the template to a temporary bucket and deletes the bucket when it exits.
- **Failed creates:** Stack creation runs with rollback disabled, and the wait allows 30 minutes. A failed create leaves the stack in `CREATE_FAILED` with its resources still running. See [Tear down](#tear-down).

## Settings in .env

Every `deploy.py` flag can be set in `neo4j-ee/.env` instead of on the command line. The file is gitignored.

```
MODE=Private
REGION=us-east-1
NUMBER_OF_SERVERS=3
NO_BLOOM=true
NO_GDS=true
```

- **Key names:** The key is the flag name in upper case with underscores. `--number-of-servers` is `NUMBER_OF_SERVERS`, and `--no-bloom` is `NO_BLOOM`. `INSTANCE_TYPE` sets the positional instance type.
- **Switches:** A flag that takes no value, such as `--no-bloom` or `--marketplace`, takes `true` or `false`.
- **Exceptions:** `--expected-account` reads `EXPECTED_AWS_ACCOUNT`. `--no-create-private-dns` has no key. Set `CREATE_PRIVATE_DNS=false` instead.
- **Precedence:** A flag wins over an environment variable, and an environment variable wins over `.env`.
- **Feedback:** When `deploy.py` takes values from `.env`, it prints their names at the start of the run.
- **Validation:** A value that is not valid for its flag stops the deploy before any AWS call. The message names the key.

`./deploy.py --help` lists every flag. The single sign-on keys are listed in [Sign-In Settings](PRIVATE.md#sign-in-settings).

## Choose an AMI

There is no `--ami-id` flag. The AMI comes from one of two sources.

**Local AMI (default).** `deploy.py` reads the AMI id from `marketplace/ami-id.txt`. If the file is missing, `deploy.py` exits with an error.

1. If `--region` is not `us-east-1`, the script copies the AMI into your region. It reuses an earlier copy when one exists, and waits for a copy that is still pending. A fresh copy typically adds 10 to 20 minutes.
2. The script writes the AMI id to the SSM parameter `/neo4j-ee/test/<stack-name>/ami-id`.
3. The stack receives that SSM path as its `ImageId` parameter.

`ami-id.txt` can point at an AMI that no longer exists. Check before you deploy:

```bash
aws ec2 describe-images --image-ids "$(cat marketplace/ami-id.txt)" --region us-east-1
```

An `InvalidAMIID.NotFound` error, an empty `Images` list, or a state other than `available` means the file is stale. Rebuild the AMI:

```bash
AWS_PROFILE=marketplace ./marketplace/create-ami.sh
./marketplace/test-ami.sh
```

- **What the build does:** `create-ami.sh` builds `neo4j-ee-base-<YYYYMMDD>` in `us-east-1` from a `t3.medium` builder and writes the new id to `marketplace/ami-id.txt`.
- **Same-day rebuilds:** A rerun on the same day deregisters the AMI with the same name and its snapshots. It refuses to do so while a launch template still references that AMI.
- **Other accounts:** An account other than the Marketplace account runs the build in iteration mode.
- **Listing AMIs:** `scripts/find-amis.py` lists the `neo4j-ee-base*` AMIs in each region of an account.

**Marketplace AMI.** `--marketplace` skips all of the above. It deploys this repo's template with the AMI from the Marketplace listing. It does not use the live Marketplace CloudFormation option.

- **Lookup:** `deploy.py` searches for AMIs owned by `aws-marketplace` with the product code `4t4a9h39qkq4tbo3n9zd51p25` and the state `available`. `--marketplace-product-code` overrides the code. The script picks the newest match by `CreationDate`.
- **SSM parameter:** The script writes the AMI id to `/neo4j-ee/test/<stack-name>/ami-id` and passes that path as the stack's `ImageId`. `teardown.sh` deletes the parameter.
- **Subscription:** The account must be subscribed to the Neo4j Marketplace listing. A successful lookup does not prove the subscription. The instance launch is the real check.
- **AMI contents:** The repo's templates expect an AMI built by the current `marketplace/create-ami.sh`. That AMI supplies the `neo4j` system user, the Neo4j yum repo and GPG key, `python3.11`, and `amazon-cloudwatch-agent`. A listing AMI built before that change fails at bootstrap, so `deploy.py --marketplace` exits before creating anything when the newest listing AMI was created before 2026-05-15. `--allow-old-marketplace-ami` overrides the check. The date is `MIN_BOOTSTRAP_AMI_DATE` in `src/neo4j_ee/amis.py`, and it needs a bump if `create-ami.sh` changes what the AMI must carry.
- **Template default:** The `ImageId` default in `templates/src/parameters-common.yaml` is the pinned path `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.03.1`. That path did not exist in `us-east-1` on 2026-10-22. A deploy that relies on the default fails at parameter resolution.

## Deploy

Run all commands from `neo4j-ee/`.

```bash
# Private (default): new VPC, 3 nodes, t3.medium
./deploy.py --region us-east-1 --no-bloom --no-gds

# Single node
./deploy.py --region us-east-1 --number-of-servers 1 --no-bloom --no-gds

# Public
./deploy.py --mode Public --region us-east-1 --no-bloom --no-gds

# Published Marketplace AMI
./deploy.py --marketplace --region us-east-1 --no-bloom --no-gds
```

Stack creation typically takes 5 to 10 minutes. Private mode creates NAT Gateways that bill hourly. A running stack costs money, so tear it down when you finish.

The stack name is `test-ee-<unix seconds>`. Do not start two `deploy.py` runs in the same second. They get the same stack name and the same SSM parameter path, so one run overwrites the other's parameter and a teardown of one removes the parameter the other needs. Start the second run at least a second later.

### Existing VPC

Pass the VPC, the subnets, and the route table. For three nodes, all three subnets are required:

```bash
./deploy.py --mode ExistingVpc --region us-east-1 --no-bloom --no-gds \
  --vpc-id <vpc-id> \
  --subnet-1 <subnet-1-id> --subnet-2 <subnet-2-id> --subnet-3 <subnet-3-id> \
  --private-route-table-1 <route-table-id>
```

For one node, add `--number-of-servers 1` and drop `--subnet-2` and `--subnet-3`.

The script exits with an error when `--private-route-table-1` is missing. Passing `--vpc-id` also turns off automatic use of a VPC file.

To skip these flags, run `scripts/create-test-vpc.py`. It builds a throwaway VPC and writes `.deploy/vpc-<ts>.txt`. When you pass no `--vpc-id`, `deploy.py` reads the VPC, subnets, route table, and endpoint settings from that file. See [Create a Test VPC](PRIVATE-EXISTING-VPC.md#create-a-test-vpc). ExistingVpc itself creates no NAT Gateways, but the test VPC does.

## TLS and DNS

| Mode | Default | Certificate | Client URI |
|---|---|---|---|
| Private | TLS at the NLB on 7473 and 7687 | Self-signed ACM cert, auto-imported when no `--cert-arn` is passed | `neo4j+ssc://` |
| Existing VPC | TLS at the NLB on 7473 and 7687 | Self-signed ACM cert, auto-imported when no `--cert-arn` is passed | `neo4j+ssc://` |
| Public | Plain TCP on 7474 and 7687 | None | `neo4j://` |

### Self-signed certificate (Private and Existing VPC)

With no `--cert-arn`, `deploy.py` generates a certificate with `openssl`. The CN and SAN are both the advertised DNS name, and the certificate is valid for 365 days. The script imports it into ACM, and both NLB TLS listeners serve it.

- **Advertised DNS name:** The default is `neo4j-<stack-name>.neo4j.local`. Override it with `--advertised-dns`.
- **Client trust:** The driver must skip verification, so use `neo4j+ssc://`.
- **No CA file is saved:** The key and certificate exist only in a temporary directory that is deleted after the import. A client that verifies the certificate needs the certificate from the NLB, or your own `--cert-arn`.

The NLB is internal. Fetch the certificate from the bastion, or through `uv run scripts/bolt-tunnel.py` and then use `localhost:7687`:

```bash
echo | openssl s_client -connect <Neo4jInternalDNS>:7687 -servername <AdvertisedDNS> 2>/dev/null \
  | openssl x509 -outform PEM > neo4j-test-cert.pem
```

A verifying client that uses `neo4j+s://` also needs `<AdvertisedDNS>` to resolve to the NLB. One way is `--create-private-dns`, described below.

- **Teardown:** `.deploy/<stack-name>.txt` records `AutoImportedCertificateArn`. `teardown.sh` deletes that certificate and no other.

### Your own certificate

Pass `--cert-arn <acm-arn> --advertised-dns <name>`. The certificate must be in the stack's region. `deploy.py` checks that the certificate covers the advertised name before it creates the stack, and it treats the certificate as a real one, so clients use `neo4j+s://`.

`scripts/certificate.py` creates DNS-validated or self-signed ACM certificates. A self-signed certificate made that way and passed as `--cert-arn` is still treated as real by `deploy.py`, but clients still need `neo4j+ssc://` to connect. See [Certificates](PUBLIC.md#certificates).

### Public TLS

Public stays plain TCP unless you pass `--enable-public-tls --cert-arn <acm-arn> --advertised-dns <name>`. After the stack is created, point the advertised name at the NLB with a CNAME.

### Private DNS

Private DNS is off by default. The advertised name resolves nowhere, and tooling reaches the cluster through the SSM bastion.

To create a Route 53 private record for the advertised name, pass `--create-private-dns` with one of these:

- `--private-dns-zone <name>` creates a new private hosted zone.
- `--private-dns-hosted-zone-id <id>` uses an existing zone.

With private DNS on, each node also advertises that name for Bolt routing instead of the NLB name. On Private this applies only to a zone the stack creates with `--private-dns-zone`. A zone passed with `--private-dns-hosted-zone-id` keeps Bolt on the NLB name.

Setting the zone to `neo4j.local` makes the default advertised name resolve inside the VPC. A new record can take about 7 minutes to resolve, because Route 53 caches the earlier failed lookup.

## Validate

Private and Existing VPC stacks are checked through the bastion. Run these from `neo4j-ee/validate-private/`:

```bash
uv run preflight <stack-name>
uv run validate-private --stack <stack-name>
uv run validate-private --stack <stack-name> --suite release --expected-cypher-default <v>
```

The failover and resilience suites are described in [PRIVATE.md](PRIVATE.md#failover-suite). Public stacks use the suite described in [PUBLIC.md](PUBLIC.md#functional-and-cluster-tests).

## Tear down

```bash
./teardown.sh <stack-name>
./teardown.sh --delete-volumes <stack-name>
```

With no name, `teardown.sh` uses the newest file in `.deploy/` and exits with an error if that file is a `vpc-*.txt`.

**What teardown deletes:**

- The stack, including the NLB, NAT Gateways, and VPC endpoints.
- The SSM parameter `/neo4j-ee/test/<stack-name>/ami-id`.
- The Secrets Manager secret `neo4j/<stack-name>/password`.
- The auto-imported ACM certificate. A certificate you passed with `--cert-arn` is never touched.
- Licence secrets that `deploy.py` created from `.licenses/`.
- The `.deploy/<stack-name>.txt` file.

**What teardown leaves:**

- The base AMI.
- A copied AMI in another region. `teardown.sh` prints its id and does not deregister it.
- EBS data volumes, unless you pass `--delete-volumes`.
- A test VPC from `create-test-vpc.py`. Delete the stack first, then run `scripts/teardown-test-vpc.py`.
- Certificates you supplied.

**If deploy.py fails.** No outputs file is written, so `teardown.sh <stack-name>` cannot find the stack. The stack stays in `CREATE_FAILED` with its resources running. Delete it by hand:

```bash
aws cloudformation delete-stack --stack-name <stack-name> --region <region>
```

Then remove the retained volumes, the SSM parameter, and the secrets by hand.
