# Sample Public App: Proposal and Phased Plan

## Proposal

### ELI5

The public Neo4j stack is internet-reachable, so a client only needs
the NLB hostname and the admin password. Today the only example client
we ship (`sample-private-app/`) is built for the private topology and
bakes in VPC plumbing that does not apply on the public side. A small,
self-contained public sample shows the off-VPC path and gives the
public template a discovery contract a developer can read from outside
the VPC.

### Removed

- Nothing is removed from existing stacks or samples.

### Fixed

- The public template publishes connection facts only as CloudFormation
  Outputs, while the private template already publishes them under
  `/neo4j-ee/<stack>/*` in SSM. After this change the public template
  publishes its own minimal SSM contract under the same path prefix, so
  a public consumer reads from SSM with `GetParametersByPath` the same
  way private operator tooling does.

### Added

- `neo4j-ee/sample-public-app/`: a local Python project (`uv` managed)
  that reads the public stack's SSM parameters, fetches the admin
  password from Secrets Manager, opens a Bolt driver against the public
  NLB, runs a self-contained graph demo, and prints a JSON result.
- A public-side SSM parameter contract published by the public template:
  `nlb-dns`, `password-secret-arn`, `advertised-dns` (only when TLS is
  enabled), `bolt-tls`, `number-of-servers`, `region`, `stack-name`,
  all under `/neo4j-ee/<stack>/`.

### Deliberately not doing

- **Not touching the private sample.** No shared module extraction, no
  changes to `sample-private-app/lambda/handler.py`, no change to its
  deploy or invoke flow. The two samples differ on the things that
  matter (TLS scheme, in-VPC vs off-VPC discovery, single-node vs
  cluster routing). Sharing a Cypher snippet is not worth the Lambda
  packaging risk.
- **Not adding `bolt-tls` or `number-of-servers` to the private or
  existing-vpc SSM contracts.** No current reader needs them on those
  topologies. Adding them ossifies the private contract for a use case
  that does not exist yet.
- **No CloudFormation stack for the public sample.** A Lambda with a
  public Function URL would mostly demonstrate Lambda packaging, not
  public Neo4j consumption. The real public-stack use case is a
  developer or app outside the VPC opening a Bolt driver against the
  NLB, and a local `uv`-managed script is the smallest demonstration
  of that path.
- **No copy of the resilience harness or TLS conformance probes.** The
  private sample owns both because they depend on VPC-internal SSM
  access to cluster instances and on the self-signed auto-import path.
- **No new VPC, subnet, or endpoint keys in the public contract.**
  Public consumers do not need them, and shipping them invites private
  consumers to read the wrong stack.

### Decisions

- **SSM parameter contract on the public template, not Outputs-only.**
  The private template already publishes `/neo4j-ee/<stack>/*`.
  Extending the same path prefix to the public template gives an
  off-VPC consumer one listable discovery pattern; CloudFormation
  Outputs require `DescribeStacks` on a named stack and are not
  listable by prefix.
- **Public SSM contract lists exactly the keys an off-VPC consumer
  needs.** Seven keys: `nlb-dns`, `password-secret-arn`,
  `advertised-dns` (only when TLS is on), `bolt-tls`,
  `number-of-servers`, `region`, `stack-name`. The VPC-internal keys
  on the private contract (`vpc-id`, `private-subnet-*`,
  `private-route-table-*`, `external-sg-id`, `vpc-endpoint-sg-id`,
  `private-dns-*`) mean nothing to a public consumer and would mislead
  if published. Dropped: full parity with the private contract.
- **`advertised-dns` is a conditional SSM resource, not an empty
  string or sentinel.** SSM `String` parameters reject empty values,
  and a magic value like `none` invents a contract consumers have to
  remember. Creating the resource only when TLS is on lets a consumer
  read the path with `GetParametersByPath` and treat absence as
  plaintext.
- **Public sample is fully self-contained.** No import of code from
  `sample-private-app/`. The fintech demo Cypher
  (`sample-private-app/lambda/handler.py:67-94`, the `_MERGE_FINTECH`
  block: Customers, Accounts, Transactions, Merchants) and the
  post-demo `SHOW SERVERS` / `dbms.routing.getRoutingTable` snippets
  (`sample-private-app/lambda/handler.py:256-279`) are duplicated
  verbatim in the public sample. Dropped: shared module extraction
  (high churn for ~30 lines of Cypher).
- **Connection pattern follows what the cluster advertises, not what
  the topology "feels like".** The bootstrap sets
  `server.bolt.advertised_address` to the NLB DNS on every topology
  (`templates/src/partials/configure-tls.sh:11-12, 63`), so all three
  cluster members publish the same Bolt host in the routing table.
  That is Neo4j's documented "single public address for all cores"
  pattern: the driver dedupes the entries into one logical pool and
  the NLB fans connections out to actual members. `neo4j://<nlb-dns>`
  is therefore a valid client URI on a public three-node plaintext
  stack; it is not "internal addresses leaking out". Sources:
  Neo4j Operations Manual,
  [Leadership, routing, and load balancing](https://neo4j.com/docs/operations-manual/current/clustering/setup/routing/)
  and
  [Configure network connectors](https://neo4j.com/docs/operations-manual/current/configuration/connectors/);
  Neo4j Driver Manual,
  [Client applications](https://neo4j.com/docs/driver-manual/4.0/client-applications/).
  Implication for the sample's docs: the NLB performs the read/write
  fan-out, not the driver. The driver still resolves `WRITE` vs
  `READ` correctly because every routing-table entry points at the
  same NLB, which forwards to whichever instance is currently leader.
  Dropped: advertising per-node addresses (would require per-node
  public IPs or a Route 53 record per node and is out of scope for
  the public marketplace topology).

### Where to look

- Public template assembly: `templates/build.py` composes
  `neo4j-public.template.yaml` from partials in `templates/src/`. The
  public template today publishes the password secret in
  `password-secret.yaml` but has no `stack-config-*.yaml` partial. The
  new `stack-config-public.yaml` absorbs the password secret resource
  and replaces `password-secret.yaml` in the public spec's
  `resource_partials` tuple, mirroring how `stack-config.yaml` on the
  private side owns both the secret and the SSM keys derived from it.
- Build pipeline: `templates/build.py`. After edits, regenerate the
  three templates and rely on `build.py --verify` (pre-commit and CI)
  to catch drift.
- Template contract tests: `neo4j-ee/tests/test_template_partials.py`
  is where the rendered-template assertions live. Add a test that
  asserts the public-side SSM parameter set is exactly the seven keys
  and that `advertised-dns` is gated on `UsePublicTLS`.
- New sample home: `neo4j-ee/sample-public-app/` at the same level as
  `sample-private-app/`.
- Public-stack documentation: `neo4j-ee/docs/PUBLIC.md` and the
  top-level `neo4j-ee/README.md`.

### Done when

- `neo4j-public.template.yaml` publishes the seven public SSM keys
  under `/neo4j-ee/<stack>/`, with `advertised-dns` gated on
  `UsePublicTLS`. A committed test asserts the exact set.
- `build.py --verify` passes and `cfn-lint` is clean on the regenerated
  public template. The private and existing-vpc templates are
  byte-identical to before.
- `neo4j-ee/sample-public-app/` runs end-to-end against a deployed
  public stack in all three URI-branch configurations (plaintext
  single-node, plaintext three-node, TLS three-node) and prints a
  JSON result that includes edition, nodes and relationships created,
  a graph sample, and (for three-node stacks) a routing-table summary
  with one writer and two readers.
- A reader landing in `neo4j-ee/README.md` finds both samples and a
  one-line summary of when each applies.

---

## Phased Plan

### Goal

Ship a small, self-contained public-stack sample app and the minimal
public-template SSM contract it depends on, without touching the
private sample or the private/existing-vpc contracts.

### Assumptions

- The public template already exposes everything the SSM contract
  needs: `AdvertisedDNS` parameter, `Neo4jPasswordSecret` resource,
  `Neo4jNetworkLoadBalancer` resource, `NumberOfServers` parameter,
  `UsePublicTLS` condition, plus `AWS::Region` and `AWS::StackName`.
  Cross-checked against the current `templates/src/` partials and
  `neo4j-public.template.yaml`; confirmed present.
- A public stack deployed in both `--enable-public-tls` (with a
  caller-supplied ACM cert) and plaintext modes is reachable from the
  developer's machine for the Phase 3 smoke runs.

### Risks

- **Frozen contract surface.** Once published, the public SSM paths
  are effectively a public API; renaming later is a breaking change.
  Lock the path names with the private contract's naming style and
  document them before merge.
- **TLS scheme mismatch.** Public TLS uses a real ACM cert and should
  validate under the system CA bundle (`neo4j+s://`), unlike the
  private sample's self-signed auto-import path (`neo4j+ssc://`). The
  public sample must pick `+s` when `bolt-tls=true`, not `+ssc`.
- **Reader expectation gap on the routing model.** A reader of the
  sample's output may expect `neo4j://` to spread reads across
  specific cluster members the way a typical multi-host routing
  driver does. In this topology the driver's routing table contains
  three identical NLB entries (see the Decisions section), so the
  effective load balancer is the NLB and not the driver's
  `LeastConnectedLoadBalancingStrategy`. `sample-public-app/README.md`
  must call this out explicitly under "Connection Pattern" so a
  reader does not mistake the architecture or try to debug a
  non-bug.

### Phase 1: Public SSM parameter contract

Status: Complete

Outcome: `neo4j-public.template.yaml` (regenerated from partials)
publishes seven SSM parameters under `/neo4j-ee/<stack>/`:
`nlb-dns`, `password-secret-arn`, `advertised-dns` (only when TLS is
enabled), `bolt-tls`, `number-of-servers`, `region`, `stack-name`.
The private and existing-vpc templates are byte-identical to before.

Checklist:

- [x] Add a new partial `templates/src/stack-config-public.yaml`
      modeled on `stack-config.yaml`'s structure but limited to the
      seven public keys. Move the `Neo4jPasswordSecret` resource from
      `password-secret.yaml` into the new partial so the password
      secret and the SSM parameter that points at it are declared
      together; delete `password-secret.yaml`.
- [x] In the new partial, set each value from what the public template
      already knows: `nlb-dns` from `!GetAtt
      Neo4jNetworkLoadBalancer.DNSName`, `password-secret-arn` from
      `!Ref Neo4jPasswordSecret`, `advertised-dns` from `!Ref
      AdvertisedDNS` with `Condition: UsePublicTLS`, `bolt-tls` as
      `!If [UsePublicTLS, 'true', 'false']`, `number-of-servers` from
      `!Ref NumberOfServers`, `region` from `!Ref AWS::Region`,
      `stack-name` from `!Ref AWS::StackName`.
- [x] Update `_PUBLIC_SPEC.resource_partials` in `templates/build.py`
      to swap `password-secret.yaml` for `stack-config-public.yaml`.
- [x] Regenerate templates with `python build.py` and confirm
      `python build.py --verify` passes. The private and existing-vpc
      template files must show zero diff.
- [x] Add a test in `neo4j-ee/tests/test_template_partials.py` that
      asserts the rendered `neo4j-public.template.yaml` declares
      exactly the seven SSM parameter names listed above, that
      `advertised-dns` carries `Condition: UsePublicTLS`, and that
      `bolt-tls` is the `UsePublicTLS`-driven string.
- [x] Run `cfn-lint` on `neo4j-public.template.yaml`.

Outcome (actual): new partial added, `password-secret.yaml` deleted,
`build.py` swapped, public template regenerated with +57 lines (six
new SSM parameters; the password-secret resource was already at the
same position so no diff there), private and existing-vpc rendered
templates byte-identical to git, new contract test
`test_public_template_publishes_exact_ssm_contract` added to
`RenderedTemplateContractTests`. All 95 unit tests pass,
`build.py --verify` clean, `cfn-lint` clean on all three templates.

Validation: `python -m unittest discover -s tests`, `python build.py
--verify`, `cfn-lint templates/neo4j-public.template.yaml`. The
private and existing-vpc templates are unchanged in git.

### Phase 2: sample-public-app

Status: Implementation complete; live-stack validation deferred to Phase 3

Outcome: `neo4j-ee/sample-public-app/` exists with a `uv`-managed
Python project that takes an EE stack name, reads the public SSM
contract for that stack, fetches the password from Secrets Manager,
opens a Bolt driver, runs a self-contained demo, and prints a JSON
result.

Checklist:

- [x] Create `neo4j-ee/sample-public-app/` with `pyproject.toml`, a
      `README.md` stub, and an entrypoint script registered as a
      `uv run` command.
- [x] Resolve the stack name from an explicit argument or by reading
      the most recently modified file under `neo4j-ee/.deploy/`,
      matching `sample-private-app/`'s ergonomics.
- [x] Read SSM parameters under `/neo4j-ee/<stack>/` with
      `GetParametersByPath`. Detect a non-public stack by the absence
      of `bolt-tls` (the public contract always publishes it; the
      private/existing-vpc contracts do not) and fail with a readable
      error that points at `sample-private-app/`.
- [x] Fetch the admin password from the secret ARN published in SSM.
- [x] Construct the Bolt URI by branching off the SSM values:
      `bolt-tls=true` uses `neo4j+s://<advertised-dns>:7687`,
      `bolt-tls=false` with `number-of-servers=3` uses
      `neo4j://<nlb-dns>:7687`, `bolt-tls=false` with
      `number-of-servers=1` uses `bolt://<nlb-dns>:7687`.
- [x] Open the driver, run the demo (fintech MERGE, read-back,
      `CALL dbms.components()` for edition, and on three-node stacks
      `CALL dbms.routing.getRoutingTable({}, 'neo4j')` plus a
      `SHOW SERVERS` summary), close the driver, print the result as
      JSON.
- [x] Exit non-zero on connect, auth, or query failure.

Outcome (actual): `neo4j-ee/sample-public-app/` created with
`pyproject.toml` exposing `sample-public-app` as a `uv run` entrypoint,
a README stub, and `src/sample_public_app/{__init__.py, cli.py}`. The
CLI resolves stack/region from `--region`, an explicit stack arg, or
the most recent `.deploy/*.txt`; reads `/neo4j-ee/<stack>/` via
`GetParametersByPath`; requires the seven public-contract keys
(`advertised-dns` excepted unless `bolt-tls=true`) and points
non-public stacks at `sample-private-app/`; fetches the password from
the SSM-published secret ARN; branches the Bolt URI per the three
configurations; runs the fintech demo with `dbms.components`, a graph
read-back, and on three-node stacks `dbms.routing.getRoutingTable` +
`SHOW SERVERS`; exits non-zero on AWS, driver, or Cypher errors.
`uv run sample-public-app --help` builds the project and prints usage;
`build_bolt_uri` was unit-checked across all three branches.

Validation: `uv run sample-public-app --help` (project builds and
entrypoint resolves); `build_bolt_uri` confirmed to produce
`neo4j+s://<advertised-dns>:7687`, `neo4j://<nlb-dns>:7687`, and
`bolt://<nlb-dns>:7687` for the three SSM-value combinations. The
"runs to completion against a deployed public stack" requirement is
covered by Phase 3.

### Phase 3: End-to-end smoke

Status: Complete

Outcome: The public sample is verified against the three public-stack
configurations that exercise all three URI branches in the connection
rule: plaintext single-node (`bolt://`), plaintext three-node
(`neo4j://`), and TLS three-node (`neo4j+s://`). TLS single-node
shares the `neo4j+s://` branch with TLS three-node and is left as a
manual smoke.

Checklist:

- [x] Deploy a public single-node plaintext stack and run the sample;
      confirm `bolt://<nlb-dns>` is used, the response includes
      `edition: enterprise`, and the routing-table block is absent
      (single-node stacks do not run the routing-table query).
- [x] Deploy a public three-node plaintext stack and run the sample;
      confirm `neo4j://<nlb-dns>` is used, the response includes
      `edition: enterprise`, and the routing-table block reports one
      writer and two readers.
- [x] Deploy a public three-node TLS stack with a real ACM cert and
      run the sample; confirm `neo4j+s://<advertised-dns>` is used
      and the driver verifies under the system CA bundle without a
      `+ssc` fallback.
- [x] Tear down each stack after its run.

Outcome 3.1 (actual): stack `test-ee-1779399532` deployed in
us-east-2 (Public, 1 server, plain TCP). Sample app produced
`bolt_uri: bolt://test-ee-1779399532-nlb-...:7687`, `bolt_scheme:
bolt`, `tls_enabled: false`, `edition: enterprise`, 12 nodes and 9
relationships created, fintech graph sample populated. No
routing-table block in the response (correct for single-node). One
non-blocking driver warning surfaced from `.single()` on
`CALL dbms.components()` (multi-row result); the call returns the
first row correctly and doesn't fail the run, but tightening the
query (e.g., `WHERE name = 'Neo4j Kernel'`) is a follow-up. Stack
torn down.

Outcome 3.2 (actual): stack `test-ee-1779400281` deployed in us-east-2
(Public, 3 servers, plain TCP). Sample app produced `bolt_uri:
neo4j://test-ee-1779400281-nlb-...:7687`, `bolt_scheme: neo4j`,
`tls_enabled: false`, `edition: enterprise`, 12 nodes and 9
relationships created, three servers all `Enabled`/`Available`,
`routing_table: {writers: 1, readers: 2}`. Same non-blocking
`.single()` warning. Stack torn down.

Outcome 3.3 (actual): hosted zone `neo4j-templates.com`
(Z03489902QH1YQLGOW9ZE) and DNS-validated ACM cert
`neo4j-demo.neo4j-templates.com` (us-east-1) prepared via
`scripts/certificate.py --auto-route53` — full audit in
`neo4j-ee/worklog/hosted-zone.md`. Stack `test-ee-1779401520`
deployed in us-east-1 (Public, 3 servers, `--enable-public-tls`).
Post-deploy CNAME `neo4j-demo.neo4j-templates.com -> <nlb-dns>`
UPSERTed into the zone; DNS resolved within seconds. Sample app
produced `bolt_uri:
neo4j+s://neo4j-demo.neo4j-templates.com:7687`, `bolt_scheme:
neo4j+s`, `tls_enabled: true`, `edition: enterprise`, 12 nodes
and 9 relationships created, three servers `Enabled`/`Available`,
`routing_table: {writers: 1, readers: 2}`. Driver validated the
server cert under the system CA bundle on the first attempt (no
`+ssc` fallback). Same non-blocking `.single()` warning. Stack
tear-down initiated; advertised-dns CNAME removed from the zone.

Validation: three successful runs with the expected URI scheme and
response shape per run.

### Phase 4: Documentation and cross-linking

Status: Complete

Outcome: Readers find the right sample for the topology they
deployed.

Checklist:

- [x] Write `neo4j-ee/sample-public-app/README.md` covering the
      workflow, the seven-key public SSM contract it consumes, the
      connection-pattern rules (`+s` vs `neo4j://` vs `bolt://`), and
      the JSON shape it returns. Mirror the private sample's README
      structure (Quick Start, Architecture, Platform Contract,
      Connection Pattern, Client Checklist, What The Script Returns).
      The "Connection Pattern" section must state that every cluster
      member advertises the NLB DNS for Bolt, so the routing table
      contains three identical entries and the NLB performs the
      read/write fan-out (not the driver). Link the three Neo4j docs
      cited in the Decisions section so a reader can verify this is
      the documented "single public address for all cores" pattern
      rather than a misconfiguration.
- [x] Add a "Verify with the sample app" subsection to
      `neo4j-ee/docs/PUBLIC.md`, placed between "Retrieve the
      Password" and "Observability Checks". Three to five lines: how
      to run the script and a link to
      `neo4j-ee/sample-public-app/README.md`. No new file under
      `docs/`.
- [x] Update `neo4j-ee/README.md` to list both samples with a
      one-line "use this when" pointer for each: `sample-public-app`
      for connecting from a laptop to a Public stack,
      `sample-private-app` for the Lambda-in-VPC client against
      Private and ExistingVpc stacks.

Outcome (actual): `sample-public-app/README.md` rewritten with the
full structure (Quick Start, Architecture, Platform Contract,
Connection Pattern with the single-public-address explanation and the
three Neo4j doc links, Client Checklist, What The Script Returns,
Project Structure). `docs/PUBLIC.md` gained a "Verify with the sample
app" subsection between Retrieve the Password and Observability
Checks; its TOC entry was added too. The Certificates section in
`docs/PUBLIC.md` also gained the post-deploy CNAME UPSERT command,
the DNS-resolution verification step, and the teardown CNAME-delete
note; a pointer to `worklog/hosted-zone.md` covers the zone bootstrap
case. `neo4j-ee/README.md` now lists both samples in a "Sample
Applications" table with a "use when" pointer for each, and the Files
table picks up `sample-public-app/` and `scripts/certificate.py`.

Validation: a reader landing on `neo4j-ee/README.md` reaches either
sample in one click and can tell from the descriptions which one
applies.

### Completion criteria

- All four phases complete with their listed validation passing.
- `build.py --verify`, the contract tests, and `cfn-lint` all pass on
  the regenerated public template; the private and existing-vpc
  templates are byte-identical to before.
- The private sample is untouched and continues to work without
  re-validation.
- The public sample succeeds against all three configurations in
  Phase 3 (plaintext single-node, plaintext three-node, TLS
  three-node) and prints a JSON result that includes edition, nodes
  and relationships created, a graph sample, and (for three-node
  stacks) a routing-table summary.
- Both samples are reachable from `neo4j-ee/README.md` with a
  one-line description of when each applies.
