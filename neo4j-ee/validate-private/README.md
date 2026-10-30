# validate-private

Operator tooling for Neo4j EE Private-mode stacks. All commands run from this directory (`neo4j-ee/validate-private/`).

For a full operator walkthrough, see [`../docs/PRIVATE.md`](../docs/PRIVATE.md).

---

## uv commands

These commands use the Neo4j Python driver and boto3 via SSM to reach the cluster through the operator bastion. Credentials are resolved on the bastion using its IAM role — they never appear on the operator's laptop or in CloudTrail.

| Command | What it does | Runtime |
|---|---|---|
| `uv run preflight [stack]` | Run 12 required checks (+ 1 informational): stack status, bastion SSM, Python driver, cypher-shell, secret, TLS params (`CertificateArn`/`AdvertisedDNS`), contract SSM params, VPC endpoints, and endpoint reachability probes. Exits 0 only if all required checks pass. | 45–75s |
| `uv run validate-private [--stack <name>]` | Run validation checks: Bolt, server edition, listen address, memory config, data directory, APOC, GDS, cluster roles, blocklist, TLS enforcement | 30–45s |
| `uv run validate-private --suite tls [--stack <name>]` | Run only the end-to-end TLS enforcement audit: elbv2 listener/target-group control-plane audit, in-VPC `openssl`/`curl`/`getent` data-plane probes, and a per-node `neo4j.conf` SSL-key read. Asserts plaintext is refused, not just that TLS is available. | 20–35s |
| `uv run validate-private --suite release [--stack <name>] --expected-cypher-default <value> [--expected-neo4j-version X] [--min-java-major N]` | Run the default validation checks (including the TLS audit) plus Neo4j RPM, Java, and Cypher default assertions. Optional version flags make the release gate fail on mismatch. | 45–75s |
| `uv run run-cypher [stack] '<cypher>'` | Execute a Cypher query and print JSON rows to stdout | 5–10s |
| `uv run admin-shell [stack]` | Open an interactive `cypher-shell` session on the bastion | Interactive |
| `uv run ssm-check-sessions [stack]` | List active SSM sessions for the stack's Neo4j instance(s) and operator bastion. | <5s |

All commands default to the most recently modified file in `../.deploy/`. Pass a stack name to target a specific deployment.

---

## scripts/

Operator helper scripts. Run Python helpers with `uv run <script>.py`; they use the normal AWS credential chain. All accept an optional stack name as the first positional argument.

| Script | What it does | Runtime |
|---|---|---|
| `scripts/get-password.sh [stack]` | Print the Neo4j password from Secrets Manager to stdout | <5s |
| `uv run scripts/preflight.py [stack]` | Direct script form of `uv run preflight`. | 45–75s |
| `uv run scripts/smoke-write.py [stack] [N=20]` | Run N `CREATE ... DELETE` write operations through the cluster. Fails if any iteration fails. | ~60s at N=20 |
| `uv run scripts/browser-tunnel.py [stack]` | Open a port-forward tunnel to the NLB on port 7473 (HTTPS). Open `https://localhost:7473` once the tunnel starts. | Interactive |
| `uv run scripts/bolt-tunnel.py [stack]` | Open a port-forward tunnel to the NLB on port 7687 (Bolt). Connect a local driver or client to the URI printed by the script. | Interactive |
| `uv run scripts/ssm_check_sessions.py [stack]` | Direct script form of `uv run ssm-check-sessions`. | <5s |
| `uv run scripts/ssm_tunnel_test.py --stack-file ../.deploy/<stack>.txt --combo 0` | Diagnostic: test the production SSM port-forward subprocess settings against the bastion. Omit `--combo` to run the full flag matrix. | Varies |

`scripts/common.sh` remains only for the Bash-only helpers.

---

## Prerequisites

- **AWS CLI v2** and **Session Manager Plugin** (`brew install --cask session-manager-plugin`) for `admin-shell` and the tunnel scripts
- **uv** for the Python commands (`brew install uv`)
- A deployed Private-mode stack with its output file in `../.deploy/`

Run `uv run preflight` first to confirm the stack and bastion are ready before using the other tools.

## Release gate

The lightweight post-release gate is:

1. Resolve the expected Neo4j version from the same `stable/latest` package source used by the deployment.
2. Confirm the expected Java major version for that Neo4j release.
3. Deploy a fresh EE Private stack from the rendered templates.
4. Run `uv run preflight <stack>`.
5. Run `uv run validate-private --suite release --stack <stack> --expected-neo4j-version <version> --min-java-major <major> --expected-cypher-default <value>`.
6. Run a targeted resilience case only when the release changes boot, volume, ASG, or cluster-recovery behavior.
7. Tear the stack down.
