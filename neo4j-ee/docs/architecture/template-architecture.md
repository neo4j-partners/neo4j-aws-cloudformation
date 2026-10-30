# Template Architecture: AMI, Template, UserData, and Bootstrap

This is the finalized, hardened architecture for the Neo4j Enterprise Edition
CloudFormation templates and the durable contract for changing them. It records
the design rationale, what was fixed, the goals, and the non-functional
requirements with the tests that enforce them, so future changes are placed by
an explicit rule instead of re-opening a debate that has churned across many
iterations. When this document disagrees with the repository, prefer the code
as built and update this document.

---

## 1. Overview and Design Rationale

A Neo4j EE deployment is produced by four layers, each with a single owner:

- **AMI**: a hardened Amazon Linux 2023 base image with OS patches, base
  packages, the Neo4j yum repo and GPG key, the CloudFormation helpers, and SSH
  hardening. It contains no Neo4j configuration and no orchestration logic.
- **Template**: the rendered CloudFormation YAML. It carries the static Neo4j
  configuration and the orchestration body as `AWS::CloudFormation::Init`
  metadata on the single shared `Neo4jLaunchTemplate`.
- **UserData**: a thin wrapper. It resolves runtime values, runs `cfn-init`,
  invokes the bootstrap, and is the sole owner of `cfn-signal`.
- **Bootstrap**: the orchestration body delivered by cfn-init. It installs
  Neo4j and plugins, applies configuration, asserts the security invariant, and
  starts the service.

The instability that prompted this design was a recurring oscillation over
where Neo4j configuration should live. Two goals were in tension:

1. **Minimize AWS Marketplace resubmission churn.** Anything baked into the AMI
   requires an AMI rebuild, Marketplace submission, and instance replacement on
   running stacks to change. Logic that lives in the template is fixed by a
   template update with zero Marketplace churn.
2. **Make the security baseline un-droppable.** The
   `internal.dbms.cypher_ip_blocklist` key is a documented security invariant.
   It must be owned by one version-controlled artifact, guarded by tests, and
   impossible to silently drop.

The first design tried to satisfy goal 2 by moving configuration into the AMI,
which directly sabotages goal 1. It would have made the single most important
security key the single most expensive key in the system to ever change or
correct.

The resolved pattern satisfies both goals at once. Static configuration is
owned by a committed `templates/src/neo4j-base.conf`. `build.py` renders that
file verbatim into the template as cfn-init metadata. At boot the bootstrap
applies every key through the safe `set_neo4j_conf` primitive. Configuration is
one readable, version-controlled, test-guarded artifact, and every fix to it
remains a template update. Nothing config-related goes into the AMI.

---

## 2. What Was Fixed

Three concrete failure modes from earlier iterations are now closed:

- **`conf.d` does not exist.** No current Neo4j version has a `neo4j.conf.d/`
  drop-in mechanism. This was verified for the 5.26 LTS line and the 2025-2026
  calendar series. `neo4j.conf` is the single configuration source with no
  fragment merging, so fragments installed to a drop-in path would never be
  read. This rejection is version-independent.
- **Config in the AMI is incoherent with the churn goal.** Baking
  configuration maximizes the exact Marketplace resubmission churn the design
  exists to minimize, and it makes the security invariant the most expensive
  key to correct. Config in the AMI is only coherent if Neo4j itself is also
  baked in, which it deliberately is not.
- **Blind concatenation is unsafe.** The Neo4j package ships a `neo4j.conf`
  with keys already present, some uncommented. Neo4j does not reliably resolve
  duplicate keys. Configuration is applied only through `set_neo4j_conf`, which
  uncomments, replaces, or appends a key idempotently. Appending a duplicate of
  an already-set key is prohibited.

The deeper fix is structural: single sources of truth (one base-conf file, one
shared LaunchTemplate, explicit function contracts) plus contract tests that
fail when a change drifts back toward the rejected designs.

---

## 3. Key Goals

- **Zero Marketplace churn for the common break.** The most common version
  break is a renamed, removed, or default-changed setting. That class is fixed
  by editing `neo4j-base.conf` and rebuilding templates, with no AMI rebuild.
- **One version-controlled source of truth for static config.** Every fixed
  Neo4j key lives in `neo4j-base.conf`, never as an inline `set_neo4j_conf`
  call in a partial.
- **Defense in depth on the security invariant.** The blocklist is enforced at
  four layers: source file, build-time contract test, runtime fail-closed
  check, and post-deploy audit.
- **Marketplace-reviewable template shape.** A short UserData wrapper plus
  declarative cfn-init metadata is what Marketplace review expects and is far
  more reviewable than a large inline blob.
- **No cross-template or cross-node drift.** The orchestration body and
  base-conf are authored once on the single shared LaunchTemplate, never
  duplicated across the one-to-three ASGs.

---

## 4. The Placement Decision Rule

Any future configuration value or logic is placed by this rule. Apply the tests
in order. The first match wins.

1. **Does it depend on a stack input or an instance-runtime value** (load
   balancer DNS, ASG peers, instance RAM, a Secrets Manager ARN)? If yes, it is
   a **runtime overlay function** in a partial, taking that value as an
   explicit argument. Examples in `partials/configure-neo4j.sh`:
   `configure_network_advertised_addresses`, `configure_memory_recommendation`,
   `configure_cluster`, `configure_bolt_tls`, `configure_plugin_settings`.
2. **Is it a Neo4j configuration key with a fixed value on every deployment?**
   If yes, it is a **line in `templates/src/neo4j-base.conf`**, never an inline
   `set_neo4j_conf` call.
3. **Is it OS-level, immutable, and independent of Neo4j** (SSH hardening,
   pinned system user, base packages, OS patches, the cfn helpers)? If yes, it
   is **baked in the AMI** by `marketplace/create-ami.sh`.
4. **Is it CloudFormation signaling or metadata resolution that cannot be
   delegated** (`cfn-signal`, IMDSv2 fetch, tag lookups, `cfn-init`)? If yes,
   it **stays in UserData** (`templates/src/userdata.sh`).

**Configuration never reaches branch 3.** That is the rule the first design
violated. The rule is reusable: the next addition is placed by these four
branches, not by ad hoc judgement, so it does not re-open the debate.

---

## 5. Architecture Decisions

### AD-1: Deploy-time install, unpinned, with a release-gated verification

Neo4j Enterprise and its JRE dependency are installed at boot from
`yum.neo4j.com/stable/latest`. No Neo4j or Java version is pinned in the AMI or
templates, so the resolved Neo4j version and its Cypher default-language
behavior track whatever `stable/latest` serves at launch.

**Rationale.** Pinning forces a Marketplace resubmission for every release
customers should receive, and it shifts the entire Neo4j security-patch cadence
onto this project, gated by Marketplace review latency. Within a calendar
series, patch releases are rarely breaking. The render-from-committed-base
design makes the most common version break, a renamed or removed or
default-changed setting, fixable by editing `neo4j-base.conf` with no AMI
rebuild. The common case is cheap by design and the rare case is expensive,
not the reverse.

**This decision is sound only while the release-gated verification exists and
passes.** There is an exposure window between `stable/latest` rolling forward
and verification catching a break. During that window new launches and ASG
self-heal use the new version. The dangerous case is ASG self-heal: a broken
`stable/latest` turns instance replacement into an availability failure with no
automated recovery. The compensating control is a rigorous release-triggered
verification run, not version pinning. That window and that risk are the reason
verification is a required release gate, not an optional step.

**Failure playbook.**

- **Config-shaped break**, such as a renamed, removed, or default-changed
  setting: fix by editing `neo4j-base.conf` and rebuilding templates. No AMI
  rebuild. This is the common case.
- **Binary-shaped break**, such as an unmet Java requirement, a repo
  restructure, a GPG key rotation, or `aws-cfn-bootstrap` being dropped: no
  template fix exists. The only response is an emergency version pin or a
  base-image fix plus an AMI rebuild and resubmission. This is the rare case.

### AD-2: Runtime fail-closed on the security invariant, presence-only

After configuration is applied and before `cfn-signal`, the bootstrap runs
`assert_security_invariant`. It reads `internal.dbms.cypher_ip_blocklist` from
`neo4j.conf` and calls `fail()` if it is absent or empty. The non-zero exit
trips UserData's ERR trap, which signals failure. The instance never enters
service without the invariant.

**The check is deliberately scoped to presence and non-emptiness only. It must
not validate the CIDR list contents.** Exact-content correctness is asserted by
the build-time contract tests, where a failure is caught before Marketplace
submission rather than at a customer launch.

**Rationale.** The asset at risk is the customer's own AWS credentials. The
blocklist stops Cypher procedures such as `apoc.load.json` from reaching
`169.254.169.254` and private VPC ranges. For a Marketplace product, refusing
to serve an instance missing this guard is the security-correct default. On the
happy path the key is always set, so the check never fires. The only realistic
way it breaks a good deployment is a false positive from over-validation. A
check that asserts exact CIDR contents would fail good instances the first time
the list is legitimately tuned or Neo4j normalizes the value, breaking every
launch and every ASG self-heal with no template fix. Presence-only keeps the
false-positive surface minimal.

**Widening this runtime check into content validation is prohibited by this
decision.** Any change that grows the check re-opens this decision.

### AD-3: Bootstrap delivered through cfn-init metadata on the shared LaunchTemplate

The orchestration body (`templates/src/bootstrap/neo4j-bootstrap.sh` with
partials inlined) and `neo4j-base.conf` are delivered by
`AWS::CloudFormation::Init` metadata attached to the single shared
`Neo4jLaunchTemplate`. They are not baked in the AMI and not inlined in
UserData. `build.py` renders this metadata. UserData runs
`cfn-init --resource Neo4jLaunchTemplate`, exports resolved runtime values as
named environment variables, invokes the bootstrap, then signals
CloudFormation.

Key properties:

- **`cfn-signal` has exactly one owner: UserData.** A non-zero exit from
  cfn-init or the bootstrap trips the ERR trap and signals failure. Execution
  never reaches `cfn-signal --success true`. This preserves protection against
  the worst Marketplace outcome, a wedged stack that is never signaled. Do not
  split the signaling owner between UserData and the bootstrap.
- **The Secrets Manager password travels by exported environment variable,
  never on the argument vector.** argv is visible in the process list and
  cloud-init logs. The bootstrap takes no positional arguments and reads no
  ambient global; every required runtime variable is validated in its prologue
  and `fail()`s if unset.
- **Content is embedded as plain YAML literal blocks, not `Fn::Sub`.** The
  bootstrap uses bash `${...}` pervasively. `Fn::Sub` would require escaping
  every shell expansion and would break byte-determinism. Literal-block
  embedding keeps the rendered metadata byte-deterministic.
- **Metadata lives on the one shared LaunchTemplate**, authored once per
  rendered template, never triplicated across the one-to-three ASGs. This
  removes the cross-template and cross-node divergence class.
- **UserData has a hard 16 KB cap after base64.** Init metadata does not count
  against the cap. UserData stays a small wrapper with headroom.
- **No new IAM privilege.** The instance role already grants
  `cloudformation:DescribeStack*`, which covers cfn-init.

The base-conf staging path is `/opt/neo4j/conf/neo4j-base.conf`, not
`/var/lib/neo4j`. `/var/lib/neo4j` is owned by the `neo4j-enterprise` RPM and
cfn-init runs before the package is installed. `/opt/neo4j` is collision-free
against the RPM and is the shared home of the bootstrap, so base-conf and
bootstrap form one cleanly cfn-init-owned artifact set.

### AD-4: TLS terminates and re-encrypts at the NLB; instance certs are self-signed

Client TLS terminates at the Network Load Balancer using an ACM certificate,
and the NLB re-encrypts to Neo4j on the instance. Neo4j serves its own
per-instance self-signed certificate on Bolt (`7687`) and HTTPS (`7473`), with
`dbms.ssl.policy.*.client_auth NONE`. The TLS listeners use the
`ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` security policy, the AWS-recommended
hybrid post-quantum policy. It negotiates ML-KEM hybrid key exchange with
capable clients while remaining backward compatible with TLS 1.3-only and TLS
1.2-only clients, so adopting it does not break existing clients. NLB selects
the matching backend policy for the re-encrypt leg automatically.
`configure_tls` is a
runtime overlay function (Placement Decision Rule branch 1): it depends on
`AdvertisedDNS`, the load balancer DNS name, and whether Bolt may advertise
`AdvertisedDNS`, and takes all three as explicit arguments.

**The TLS signal is a non-empty `AdvertisedDNS`, never a secret output.**
Private and ExistingVpc always pass a non-empty `AdvertisedDNS` and TLS is
mandatory there, enforced at parameter-validation time by the
`CertificateArnRequired` and `AdvertisedDnsRequired` Rules. Public is opt-in
through the `UsePublicTLS` condition; without it the Public NLB stays plain TCP
on `7474`/`7687`. Operator and test tooling resolve the Bolt scheme from
non-empty `AdvertisedDNS` and connect with `neo4j+ssc://`. There is no
`BoltTlsSecretArn` output and no secret-driven TLS path.

**The certificate SAN and CN must be `AdvertisedDNS`.** Neo4j's Jetty enforces
`sniHostCheck` on HTTPS only, so the served certificate must match the
browser's `Host`, which is `AdvertisedDNS`. A mismatch fails HTTPS with
`400 Invalid SNI`. This is why `AdvertisedDNS` is both the certificate SAN/CN
and the host placed in `server.https.advertised_address` and
`server.default_advertised_address`. `server.default_advertised_address` in
particular must stay `AdvertisedDNS`: it is Jetty's no-SNI fallback host, and
the NLB HTTPS health check on `7473` connects without SNI; if it is not the
cert SAN, Jetty answers the health check with `400 Invalid SNI` and every
`7473` target goes unhealthy.

**Bolt advertises `AdvertisedDNS` only when the stack's private DNS record
resolves it in-VPC; otherwise it advertises the NLB DNS.** Bolt is a routed
protocol: the cluster publishes `server.bolt.advertised_address` in its
routing table, and every `neo4j://`-style client must resolve it to refresh
routing. A driver that sends its dialled address in the routing context gets
that address echoed back, but a driver that sends none gets the advertised
address, and a `neo4j+s://` driver then verifies it against the certificate
SAN. The template hands the decision to the bootstrap as the
`boltAdvertiseDns` UserData value:

- **Private:** `true` only under `CreatePrivateDnsHostedZone`, that is
  `CreatePrivateDns=true` with an empty `PrivateDnsHostedZoneId`. Private
  builds a new VPC, so a caller-supplied zone cannot already be associated
  with it and its record would not resolve in-VPC.
- **ExistingVpc:** `CreatePrivateDns`. A caller-supplied zone can already be
  associated with the caller's VPC. PRIVATE-EXISTING-VPC.md makes that
  association a caller requirement.
- **Public:** unset, so `false`. Public has no private DNS.

When `true`, `server.bolt.advertised_address` is `AdvertisedDNS` and stays
inside the SAN. Otherwise `AdvertisedDNS` may be only a synthetic cert SAN
with no in-VPC record, so `server.bolt.advertised_address` is the
always-resolvable NLB DNS. Bolt TLS has no `sniHostCheck`, so the self-signed
cert (SAN = `AdvertisedDNS`) is still accepted by `neo4j+ssc://` clients,
which encrypt without trust verification. Because the value is a stack
parameter baked into each node at boot, a stack update that turns
`CreatePrivateDns` off deletes the record while running nodes still advertise
it. The nodes must be replaced after such an update.

Open item: a customer certificate whose `AdvertisedDNS` is served by a record
outside the stack (a customer-managed CNAME, or Public with
`UsePublicTLS`) still advertises the NLB DNS for Bolt. A future template
parameter may opt Bolt into `AdvertisedDNS` for that case.

**Operator tooling uses `neo4j+ssc://`, production clients use `neo4j+s://`.**
The instance certificate is self-signed and the NLB does not validate it on
re-encryption (`client_auth NONE`), so trust verification is impossible for
operator tunnels that reach the NLB by IP. `neo4j+ssc://` encrypts without
trust verification and is the documented operator and test scheme. Production
clients that map `AdvertisedDNS` to the NLB and trust the ACM-fronted name use
`neo4j+s://`. SSM command descriptions and tunnel scripts use the `+ssc` form.

**`openssl` is baked into the AMI, not installed on the boot path.** It is
OS-level, immutable, and Neo4j-independent, so it belongs in the AMI
(Placement Decision Rule branch 3). `configure_tls` runs `openssl` to generate
the per-instance self-signed cert and `fail()`s if it is absent. Generating
the cert at boot is acceptable because it has no network dependency and is
idempotent: an existing cert pair is reused, so ASG self-heal does not depend
on a package mirror. A `dnf install` on the boot path would, which is the
AD-1 binary-shaped failure class with full ASG-self-heal blast radius.

**The Browser target group uses an L7 health check.** The Browser target group
health check is `HTTPS` for TLS topologies and `HTTP` for Public without TLS,
both against `/` with a `200` matcher, so a target is healthy only when Neo4j
is actually serving. The NLB does not validate the target certificate, so the
self-signed instance cert is accepted, and TLS 1.2 is sufficient for an NLB
HTTPS health check. The Bolt target group stays a `TCP` health check because
Bolt is a binary protocol with no L7 health check available. A TCP-only
Browser check is prohibited: it reports a wedged Neo4j as healthy.

**Private DNS is an optional, stack-owned convenience.** When
`CreatePrivateDns` is true, the Private and ExistingVpc templates create a
Route 53 alias record mapping `AdvertisedDNS` to the internal NLB, in a hosted
zone the stack creates or in an existing zone passed as
`PrivateDnsHostedZoneId`. `EvaluateTargetHealth` is `false`: the NLB is the
only target and there is no failover record, so health-evaluated DNS would
only add an empty-target-group failure mode during provisioning. Public never
creates Route 53 private DNS resources. Which zone holds the record decides
whether Bolt advertises `AdvertisedDNS`, as described in the Bolt paragraph
above.

**TLS enforcement is audited post-deploy, not assumed.** Like the cypher IP
blocklist invariant, TLS is checked at independent layers and the runtime
audit is the last one. Build-time contract tests pin the rendered listeners,
target groups, and `SslPolicy` (NFR-12, NFR-13, NFR-14). After deploy,
`validate-private --suite tls` proves enforcement, not mere availability,
from three angles: an elbv2 control-plane audit (listeners `Protocol=TLS`
with the post-quantum `SslPolicy`, Browser target group `HTTPS`, Bolt `TCP`),
in-VPC data-plane probes from the bastion (`openssl`/`curl`/`getent`:
plaintext `7474` refused, HTTPS `7473` answers `200`, Bolt `7687` requires a
TLS handshake, the served certificate's SAN/CN equals `AdvertisedDNS`,
`AdvertisedDNS` resolves inside the VPC), and a per-node `neo4j.conf` SSL-key
read over SSM (`server.bolt.tls_level=REQUIRED`, `https.enabled=true`,
`http.enabled=false`). `preflight` fails closed if `CertificateArn` or
`AdvertisedDNS` is absent on a Private or ExistingVpc stack. The
`sample-private-app` Lambda runs the equivalent probe end-to-end from a real
in-VPC client and the deploy script exits non-zero on any hard failure
(plaintext accepted, HTTPS down, or served-cert identity mismatch); strict
`+s` verification is reported informationally because the auto-imported
self-signed certificate is a supported mode. A negative result is a real
finding, never papered over.

---

## 6. The AMI / Template / UserData / Bootstrap Split

| Layer | Owns | Changeable by | Examples |
|---|---|---|---|
| **AMI** (`marketplace/create-ami.sh`) | OS-level, immutable, Neo4j-independent concerns | AMI rebuild + Marketplace submission + instance replacement | OS patches; AWS CLI v2; `python3.11`, `jq`, `openssl`; `amazon-cloudwatch-agent`; `aws-cfn-bootstrap` (`cfn-init`, `cfn-signal`); Neo4j yum repo + GPG key; `neo4j` system user (uid/gid 500); SSH hardening; IMDSv2 enforcement |
| **Template** (rendered by `build.py`) | Static Neo4j config and orchestration body as cfn-init metadata on `Neo4jLaunchTemplate`; the per-topology CloudFormation resources | Template update, zero Marketplace churn | `neo4j-base.conf` contents; the inlined bootstrap script; NLB TLS listeners and target groups; ACM `CertificateArn` and `AdvertisedDNS` parameters; the mandatory-TLS Rules; the optional Route 53 private DNS resources (AD-4) |
| **UserData** (`templates/src/userdata.sh`) | CloudFormation signaling and metadata resolution that cannot be delegated | Template update | IMDSv2 token + instance-id/AZ fetch; stack-id and logical-id tag lookups; password fetch from Secrets Manager; `cfn-init`; export of runtime env vars; bootstrap invocation; `cfn-signal`; ERR trap |
| **Bootstrap** (`templates/src/bootstrap/neo4j-bootstrap.sh` + partials) | Boot-time install, configuration application, security assertion, service start | Template update | `install_neo4j_from_yum`, `install_apoc`, `install_plugin`; `apply_base_conf`; runtime overlay functions including `configure_tls` (AD-4); `assert_security_invariant`; `start_neo4j` |

The AMI carries no Neo4j configuration key, no value, and no conf-mutating
logic. The same AMI is reused across all three topologies: Public, Private, and
Private Existing VPC.

---

## 7. The Security Invariant and Defense in Depth

`internal.dbms.cypher_ip_blocklist` is the single most protected key in the
system. Its value in `neo4j-base.conf` covers the IMDS credential range
(`169.254.169.0/24`), the RFC1918 VPC-internal ranges (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`), and the IPv6 unique-local and link-local
ranges. Removing it would let any Cypher user steal the instance IAM role
credentials and pivot into the VPC.

It is enforced at four independent layers:

1. **Source of truth.** It is a line in committed `templates/src/neo4j-base.conf`.
2. **Build-time contract.** `build.py` embeds the file verbatim. The
   `Neo4jBaseConfTests` and `RenderedTemplateContractTests` assert the exact
   content. A defective build fails before Marketplace submission.
3. **Runtime fail-closed.** `assert_security_invariant` aborts the boot before
   `cfn-signal` if the key is absent or empty (AD-2, presence-only).
4. **Post-deploy audit.** The G3 conf-key audit in `validate-private` asserts
   the key is present and non-empty on every node of a live stack.

The related access-control keys in `neo4j-base.conf`
(`dbms.security.procedures.unrestricted`,
`dbms.security.procedures.allowlist`, `dbms.security.http_auth_allowlist`) are
static keys and follow branch 2 of the Placement Decision Rule.

---

## 8. Prohibitions

Each item below is a rejected design. A change that reintroduces it is a
regression, not an improvement.

- **Do not put Neo4j configuration in the AMI.** It maximizes Marketplace
  churn and makes the security invariant the most expensive key to correct.
- **Do not use a `conf.d` drop-in mechanism.** It does not exist in any current
  Neo4j version. Fragments would never be read.
- **Do not blindly concatenate config files.** Neo4j does not resolve duplicate
  keys. Apply config only through `set_neo4j_conf`.
- **Do not pass the Secrets Manager password as a positional argument.** argv
  is visible in the process list and cloud-init logs.
- **Do not widen the runtime security check into content validation.** It would
  fail good instances the first time the list is legitimately tuned, with no
  template fix and full ASG-self-heal blast radius.
- **Do not split the `cfn-signal` owner between UserData and the bootstrap.**
  It produces double-signaled or never-signaled stacks.
- **Do not embed the bootstrap through `Fn::Sub`.** The bootstrap uses bash
  `${...}` pervasively; `Fn::Sub` breaks escaping and byte-determinism.
- **Do not replicate metadata across the ASGs.** It reintroduces the
  cross-template and cross-node drift the design exists to prevent.
- **Do not bake the bootstrap script into the AMI.** It makes every
  orchestration fix an AMI rebuild and Marketplace resubmission.
- **Do not add a static Neo4j key as an inline `set_neo4j_conf` call.** Fixed
  keys belong in `neo4j-base.conf` (Placement Decision Rule branch 2).
- **Do not `dnf`- or `yum`-install any package on the boot path.** `openssl`
  and every other OS package belong in the AMI. A boot-path package install is
  the AD-1 binary-shaped failure class with full ASG-self-heal blast radius.
- **Do not use a TCP-only health check for the Browser target group.** It
  reports a wedged Neo4j as healthy. The Browser check is L7 (AD-4).
- **Do not key TLS off a secret output.** The TLS signal is a non-empty
  `AdvertisedDNS`. There is no `BoltTlsSecretArn`.
- **Do not set the certificate SAN or CN to anything but `AdvertisedDNS`.**
  Neo4j's Jetty `sniHostCheck` fails HTTPS with `400 Invalid SNI` on a
  mismatch (AD-4).
- **Do not require client auth or a trusted CA on the instance TLS policies.**
  The NLB re-encrypts without presenting a client certificate; the instance
  cert is self-signed by design (AD-4).

---

## 9. Non-Functional Requirements and Enforcing Tests

Tests are the executable specification. The design churned because the
rationale lived in people's heads and was re-litigated each time. A regression
toward a rejected design now fails a test rather than passing a review.

NFR numbers are stable identifiers, not a sequence. Some are cited directly in
code comments (`NFR-9` in `userdata.sh`, `NFR-6` in `build.py`), so the numbers
are kept fixed and gaps in the list below are intentional.

| NFR | Requirement | Enforced by |
|---|---|---|
| NFR-1 | No static or security key from `neo4j-base.conf` appears as an inline `set_neo4j_conf` call in any partial | Guard test that greps every `partials/*.sh` |
| NFR-2 | `neo4j-base.conf` content, including the blocklist, is exactly as specified | `Neo4jBaseConfTests` |
| NFR-3 | `marketplace/create-ami.sh` writes no Neo4j config key, value, or conf-mutating logic | AMI source assertion; no config in AMI |
| NFR-4 | Configuration is applied only through the idempotent `set_neo4j_conf` primitive, never blind append | `set_neo4j_conf` unit tests; NFR-1 guard |
| NFR-5 | Runtime values pass as named, validated, exported env vars; no ambient global; password never on argv | Bootstrap prologue validation; isolation tests |
| NFR-6 | `build.py --verify` passes: committed templates are byte-identical to a fresh assembly | `build.py --verify` in CI |
| NFR-9 | Boot fails closed before `cfn-signal` if the blocklist is absent or empty; the check stays presence-only | `assert_security_invariant` unit tests; AD-2 |
| NFR-10 | `cfn-init` and `cfn-signal` are present on the built image | `test-ami.sh` CHECK 11 (PATH resolution of both helpers) |
| NFR-11 | Rendered UserData of every template is well under the 16 KB post-base64 cap | Build-time size guard |
| NFR-12 | Private and ExistingVpc NLB listeners and target groups are `Protocol: TLS` on `7473`/`7687` with the ACM cert and `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`; no plain `7474` in the data path; Public TLS is gated on `UsePublicTLS` (AD-4) | `NlbAndRoutingTests.test_private_existing_listeners_and_tgs_are_tls`, `test_public_listeners_are_tls_conditional` |
| NFR-13 | The Browser target group uses an L7 health check (`HTTPS` for TLS, `HTTP` for Public plain) against `/` with a `200` matcher; the Bolt target group uses `TCP` (AD-4) | `NlbAndRoutingTests.test_private_existing_listeners_and_tgs_are_tls` |
| NFR-14 | `openssl` is baked into the AMI and never `dnf`-installed on the boot path (AD-4) | `test_create_ami_bakes_openssl`, `test_configure_tls_does_not_install_packages_at_boot` |
| NFR-15 | Bolt advertises `AdvertisedDNS` only when `boltAdvertiseDns` is `true`: Private under `CreatePrivateDnsHostedZone`, ExistingVpc under `CreatePrivateDns`, never on Public; otherwise it advertises the NLB DNS (AD-4) | `RenderedTemplateContractTests.test_bolt_advertise_dns_reaches_userdata_on_private_templates_only`, `ShellPartialTests.test_configure_tls_bolt_advertises_advertised_dns_when_resolvable`, `test_configure_tls_generates_certs_and_sets_keys` |

`RenderedTemplateContractTests` additionally assert the rendered cfn-init
metadata, the single-LaunchTemplate placement, and the embedded base-conf
content (AD-3, security invariant layer 2).
