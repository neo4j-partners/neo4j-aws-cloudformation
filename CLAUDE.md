# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AWS CloudFormation templates for deploying Neo4j on AWS. The active product is the Enterprise Edition Marketplace listing under `neo4j-ee/`; that is where nearly all development happens.

- **neo4j-ee/** — Enterprise Edition. 1 or 3-node clusters, each node in its own ASG, fronted by a Network Load Balancer. Three topologies built from one set of source partials: Public (new VPC, public subnets), Private (new VPC, private subnets, SSM bastion), Private Existing VPC (caller-supplied VPC/subnets). Optional Bolt TLS, Bloom, and GDS.
- **neo4j-ce/** — Community Edition. Single instance with ASG self-healing, persistent EBS volume, Elastic IP. Largely independent of EE; older single-file template (`neo4j.template.yaml`) with its own `test_ce/` suite. Not the focus of current work.

Scripts read `AWS_PROFILE` from the environment and fall back to the `default` profile. Marketplace AMI builds run against the `neo4j-marketplace` account (`385155106615`) with `AWS_PROFILE=marketplace`.

The rest of this document covers **neo4j-ee/** unless stated otherwise. All EE commands run from `neo4j-ee/`.

## The Four-Layer Architecture (read the contract first)

**Before changing any EE template, the AMI, UserData, Neo4j config, or bootstrap: read `neo4j-ee/docs/architecture/template-architecture.md`.** It is the finalized, test-enforced architecture contract. Place every new value or logic by its **Placement Decision Rule** (section 4); treat the **Prohibitions** (section 8) as binding. The notes below summarize it but do not replace it.

A deployment is produced by four layers, each with one owner:

| Layer | Owner file | Changeable by | Holds |
|---|---|---|---|
| **AMI** | `marketplace/create-ami.sh` | AMI rebuild + Marketplace submission + instance replacement | OS patches, base packages, cfn helpers, Neo4j yum repo + GPG key, `neo4j` system user, SSH hardening, IMDSv2. **No Neo4j config, no orchestration.** |
| **Template** | rendered by `build.py` | Template update, zero Marketplace churn | Static Neo4j config (`neo4j-base.conf`) + the bootstrap, both as `AWS::CloudFormation::Init` metadata on the single shared `Neo4jLaunchTemplate` |
| **UserData** | `templates/src/userdata.sh` | Template update | IMDSv2 fetch, tag lookups, password fetch, `cfn-init`, env-var export, bootstrap invocation, **sole owner of `cfn-signal`** |
| **Bootstrap** | `templates/src/bootstrap/neo4j-bootstrap.sh` + `partials/*.sh` | Template update | Install Neo4j/plugins, apply config, assert security invariant, start service |

**Placement Decision Rule** (first match wins): (1) depends on a stack input or runtime value → a runtime overlay function in a partial taking it as an explicit arg; (2) a fixed Neo4j config key on every deploy → a line in `templates/src/neo4j-base.conf`, never an inline `set_neo4j_conf` call; (3) OS-level, immutable, Neo4j-independent → AMI; (4) CloudFormation signaling/metadata resolution → UserData. **Config never reaches branch 3.**

## Build System

The three output templates are generated; never hand-edit them. Edit partials in `templates/src/`, then regenerate:

```bash
cd templates
python build.py            # regenerate the three neo4j-*.template.yaml files
python build.py --verify    # CI/pre-commit check: committed output is byte-identical to a fresh build
```

`build.py` inlines `# include partials/<name>.sh` directives in `neo4j-bootstrap.sh`, embeds the bootstrap and `neo4j-base.conf` verbatim as cfn-init metadata literal blocks (not `Fn::Sub`), and assembles per-topology YAML partials (`*-public.yaml`, `*-existing-vpc.yaml`, base). Commit both the edited partial(s) and the regenerated `*.template.yaml`. The pre-commit hook and CI both run `build.py --verify`; a stale committed template fails the build.

## Common Commands

```bash
# Tests (run from neo4j-ee/)
python -m unittest discover -s tests          # contract + partial unit tests
python -m unittest tests.test_template_partials.ShellPartialTests   # one test class
cfn-lint templates/neo4j-private.template.yaml
cfn-lint templates/neo4j-private-existing-vpc.template.yaml   # clean; W1030 scoped per-resource in source

# Deploy (deploy.py defaults to Private, 3-node, t3.medium; installs Bloom+GDS — differs from Marketplace defaults of false/false)
./deploy.py --region us-east-1
./deploy.py --mode Public --region us-east-1
./deploy.py --mode ExistingVpc --vpc-id vpc-xxxx --subnet-1 subnet-xxxx --subnet-2 subnet-xxxx --subnet-3 subnet-xxxx --private-route-table-1 rtb-xxxx
./deploy.py --number-of-servers 1            # single node
./deploy.py --marketplace                    # use published Marketplace AMI
# Private/ExistingVpc terminate TLS at the NLB by default: with no --cert-arn a
# self-signed ACM cert is auto-imported (clients use neo4j+ssc://). Public is
# plain TCP unless: ./deploy.py --mode Public --enable-public-tls --cert-arn <arn> --advertised-dns <dns>

# Tear down (EBS data volumes are DeletionPolicy: Retain by design)
./teardown.sh                    # most recent deployment
./teardown.sh <stack-name>
./teardown.sh --delete-volumes   # also delete retained EBS volumes

# AMI (Marketplace: AWS_PROFILE=marketplace; local iteration: AMI_BUILD_MODE=iteration AWS_PROFILE=default)
./marketplace/create-ami.sh      # writes marketplace/ami-id.txt
./marketplace/test-ami.sh        # SSM-based AMI verification, no SSH
```

Each deployment writes `.deploy/<stack-name>.txt` (gitignored); operator/test tooling defaults to the most recently modified file there, or pass a stack name.

## Operator Tooling for Private Stacks (validate-private/)

`neo4j-ee/validate-private/` is a `uv` project (Python ≥3.11). It reaches private clusters through the SSM bastion; credentials resolve on the bastion via its IAM role and never touch the operator laptop. Run from `validate-private/`:

```bash
uv run preflight [stack]                       # 12 required readiness checks; gate before other tools
uv run validate-private [--stack <name>]       # Bolt, edition, memory, APOC/GDS, cluster roles, blocklist
uv run validate-private --suite release --expected-cypher-default <v> [--expected-neo4j-version X] [--min-java-major N]
uv run run-cypher [stack] '<cypher>'
uv run admin-shell [stack]                      # interactive cypher-shell on the bastion
uv run scripts/smoke-write.py [stack] [N]       # N CREATE/DELETE write cycles
uv run scripts/browser-tunnel.py [stack]        # port-forward NLB :7473
uv run scripts/bolt-tunnel.py [stack]           # port-forward NLB :7687
```

The `--suite release` run plus `preflight` is the post-release gate; the full procedure is in `validate-private/README.md` and `docs/PRIVATE.md`.

## Security Invariant: `internal.dbms.cypher_ip_blocklist`

The single most protected key. In `templates/src/neo4j-base.conf` it covers `169.254.169.0/24` (IMDS credential exfiltration), `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (VPC SSRF), and the IPv6 unique-local/link-local ranges. Removing it lets any Cypher user steal the instance IAM role credentials and pivot into the VPC. Enforced at four independent layers: source file → build-time contract test (`Neo4jBaseConfTests`, `RenderedTemplateContractTests`) → runtime fail-closed `assert_security_invariant` (presence-only; **do not widen to content validation**) → post-deploy G3 conf-key audit in `validate-private`.

## AMI Lifecycle Invariant

`create-ami.sh` refuses to deregister an AMI any launch template in the account still references; re-running the builder against a live stack's AMI would orphan that stack's launch templates and break ASG self-heal. The G6 guard also asserts every launch template's AMI still exists before resilience tests run.

## Debugging Deployed Stacks

On the EC2 instance: `/var/log/cloud-init-output.log` (UserData + bootstrap) then `/var/log/neo4j/debug.log` (Neo4j). For private stacks reach the instance via the SSM bastion (`uv run admin-shell`, tunnel scripts).
