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
  packages, the CloudFormation helpers, and SSH hardening. It also carries
  Neo4j Enterprise and `cypher-shell` at one pinned version, with the Neo4j
  yum repo disabled (AD-1). It contains no project-authored Neo4j
  configuration and no orchestration logic.
- **Template**: the rendered CloudFormation YAML. It carries the static Neo4j
  configuration and the orchestration body as `AWS::CloudFormation::Init`
  metadata on the single shared `Neo4jLaunchTemplate`.
- **UserData**: a thin wrapper. It resolves runtime values, runs `cfn-init`,
  invokes the bootstrap, and is the sole owner of `cfn-signal`.
- **Bootstrap**: the orchestration body delivered by cfn-init. It verifies the
  baked Neo4j install, installs plugins from the package, applies
  configuration, asserts the security invariant, and starts the service.

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
remains a template update. No project-authored configuration goes into the
AMI.

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
  key to correct. Neo4j itself is now baked in at a pinned version (AD-1).
  Its configuration still is not. A config fix stays a template update that
  reaches new launches and ASG self-heal without an AMI rebuild.
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
   is **baked in the AMI** by `marketplace/create-ami.sh`. The pinned Neo4j
   and `cypher-shell` packages are also baked in the AMI, by AD-1. Their
   configuration is not.
4. **Is it CloudFormation signaling or metadata resolution that cannot be
   delegated** (`cfn-signal`, IMDSv2 fetch, tag lookups, `cfn-init`)? If yes,
   it **stays in UserData** (`templates/src/userdata.sh`).

**Configuration never reaches branch 3.** That is the rule the first design
violated. The rule is reusable: the next addition is placed by these four
branches, not by ad hoc judgement, so it does not re-open the debate.

---

## 5. Architecture Decisions

### AD-1: Neo4j is baked into the AMI at a pinned version

`marketplace/create-ami.sh` installs Neo4j Enterprise, `cypher-shell` and
their JRE dependency at one pinned version. Marketplace builds require
`NEO4J_VERSION`. Iteration builds resolve the newest version in the Neo4j repo
when it is unset. The builder records the version in the AMI description and
in the `Neo4jVersion` tag. It leaves the Neo4j repo disabled and the service
disabled. Neo4j never starts in the AMI, so the image holds no database files.
The package's own `neo4j.conf` is the only Neo4j configuration on the image.
The bootstrap edits it at boot through `set_neo4j_conf`.

The node boot path installs no package. `verify_neo4j_installed` fails the
boot if `neo4j-enterprise` or `/usr/bin/cypher-shell` is missing, then enables
the service. Plugins are copied from the package's `labs/` and `products/`
directories, so they always match the server version.

**Rationale.** A boot-time install made every launch and every ASG self-heal
depend on a package mirror and on whatever version it served that day. A
mirror outage, a repo restructure or a bad release turned instance replacement
into an availability failure with no template fix. A boot-time install also
needed an internet route from every node. Security review findings M1 and H2
flagged that route as an exfiltration path. Baking removes both problems.
Every node in a stack runs the version that passed the release gate. Self-heal
replaces a node with the same version.

**Cost.** Each Neo4j release that customers should receive needs an AMI
rebuild and a Marketplace submission. The project owns the Neo4j
security-patch cadence, and Marketplace review latency gates it. A running
stack moves to a new version only by replacing its instances onto the new AMI.

**Release gate.** The gate runs on the candidate AMI before Marketplace
submission. `test-ami.sh` checks the baked packages against the `Neo4jVersion`
tag. A fresh Private deployment from that AMI must then pass `preflight` and
`validate-private --suite release`. A result from an earlier AMI does not
count.

**Failure playbook.**

- **Config-shaped break**, such as a renamed, removed, or default-changed
  setting: fix by editing `neo4j-base.conf` and rebuilding templates. No AMI
  rebuild. This is the common case.
- **Binary-shaped break**, such as an unmet Java requirement or a broken
  package: the release gate catches it on the candidate AMI before customers
  see it. Fix it in `create-ami.sh` or keep shipping the previous version.
  Running stacks are unaffected because their AMI does not change.

**What changed.** AD-1 originally installed Neo4j at boot from
`yum.neo4j.com/stable/latest` with no version pinned. Pinning was rejected
because it forces a Marketplace submission for every release and moves the
Neo4j patch cadence onto this project. That design accepted an exposure window.
Between `stable/latest` rolling forward and verification catching a break, new
launches and ASG self-heal used the new version. Its playbook said a
binary-shaped break had no template fix and needed an emergency pin or an AMI
rebuild. Security review finding M1 replaced the design, because node egress
is now limited to VPC endpoints (H2) and the boot path can no longer reach a
package mirror. The project now accepts the Marketplace cost that the old
design avoided.

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
- **The Secrets Manager passwords travel by exported environment variable,
  never on the argument vector.** argv is visible in the process list and
  cloud-init logs. The bootstrap takes no positional arguments and reads no
  ambient global; every required runtime variable is validated in its prologue
  and `fail()`s if unset. UserData unsets both passwords as soon as the
  bootstrap returns (AD-6).
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
on a package mirror. A `dnf install` on the boot path would. AD-1 removed
that dependency for Neo4j, and the same rule applies to `openssl`.

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

### AD-5: Stack data is encrypted with a stack key and retained on delete

Every stack creates one customer-managed KMS key, `Neo4jStackKey`, with
rotation on. It encrypts the password secret, both CloudWatch log groups and
the alert SNS topic. The key exists on every deploy, so no encryption path
depends on an optional parameter.

**The key policy delegates use to IAM, never to a bare account grant.** The
account-root statement holds key management actions only. Secrets Manager use
goes through a root statement conditioned on `kms:ViaService` for Secrets
Manager in the stack region and on `kms:CallerAccount`. A principal also needs
an IAM `kms:Decrypt` grant on the key to read a secret. `Neo4jRole` carries
that grant. CloudWatch Logs gets its own statement, limited by an `ArnLike`
condition on `kms:EncryptionContext:aws:logs:arn` to the stack's
`/neo4j/<stack>/*` and `/aws/vpc/flowlogs/<stack>` groups.

**The stack publishes the key by full ARN.** Every template writes
`/neo4j-ee/<stack>/kms-key-arn` unconditionally, with the value
`!GetAtt Neo4jStackKey.Arn`. A consumer that reads a password secret needs an
IAM `kms:Decrypt` grant on that ARN. The key is single-Region, so the ARN names
the stack's region. The identity that deploys the stack needs
`kms:GenerateDataKey` through Secrets Manager, because Secrets Manager
encrypts the secrets on that identity's behalf.

**Credentials and the audit trail survive a stack delete.** The password
secret, the stack key and the NLB access-log bucket carry
`DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`. A stack delete
therefore never destroys a credential or the access record by accident. The
key is retained with the secret, so a retained secret stays readable.
`teardown.sh --delete-volumes` removes all three after the stack is gone. It
force-deletes the secrets, empties and deletes the bucket, and schedules the
key for deletion with the 7-day minimum window. Without the flag, teardown
prints their identifiers.

**The NLB writes access logs.** The log bucket uses SSE-S3, blocks public
access, enforces bucket-owner object ownership and expires objects after
90 days. Its policy denies any request without TLS. Log delivery may write
only under `nlb/AWSLogs/<account>/`. `preserve_client_ip` stays at its
default.

**The failed-auth alarm and its topic always exist.** Only the email
subscription depends on `AlertEmail`. A stack without an email address still
records alarm state.

**Instance metadata is reachable only from the instance itself.** Every
launch template, including the operator bastion, sets
`HttpPutResponseHopLimit: 1` alongside `HttpTokens: required`. A container or
forwarded hop cannot obtain an IMDSv2 token.

**There is no `tls-self-signed` flag.** No template publishes a parameter or
output that tells a client to skip certificate verification.

**The stack publishes the certificate trust anchor.** Private and ExistingVpc
own the SSM parameter `/neo4j-ee/<stack>/ca-pem`. The template creates it with
the placeholder `pending`, and every ASG depends on it. Every node replaces
the placeholder before it signals CloudFormation. The bootstrap function
`publish_trust_anchor` runs right after `configure_tls`. It reads the
`CertificateArn` certificate from ACM and publishes its chain. When ACM
returns no chain, as for a self-signed import, it publishes the certificate
itself. The parameter is a `String` on the Advanced tier. A chain up to the
8 KB Advanced limit fits. A longer chain fails `put-parameter`, and the node
fails closed. A failed ACM read, a value that does not start with
`-----BEGIN CERTIFICATE-----`, or a failed write stops the node before
`cfn-signal`. A malformed ACM response therefore never overwrites a good
anchor. The node role holds `acm:GetCertificate` on the certificate and
`ssm:PutParameter` on the ca-pem ARN only. UserData passes the ARN
base64-encoded, like the OIDC values, because the `CertificateArn` pattern
admits shell metacharacters.

**Public publishes no trust anchor.** The Public template creates no ca-pem
parameter and never calls `publish_trust_anchor`. A Public TLS client trusts
the public CA that issued its ACM certificate.

**Consumers fail closed until the anchor is published.** The parameter is
part of the platform contract:

| Parameter | Value | Consumer rule |
|---|---|---|
| `/neo4j-ee/<stack>/ca-pem` | PEM chain of the NLB certificate, or the certificate itself when ACM holds no chain | `pending`, or any value that does not start with `-----BEGIN CERTIFICATE-----`, means the anchor is not yet published. The consumer refuses to connect. |

A consumer needs `ssm:GetParameter` on the ca-pem ARN, or
`ssm:GetParametersByPath` on `/neo4j-ee/<stack>/` for readers that load the
whole path, no KMS. The parameter is not encrypted with the stack key.

**A stack update can reset the anchor to `pending`.** CloudFormation rewrites
the placeholder when an update changes any property of the parameter. A
contract test pins every property, so no template change does that by
accident. After a reset, consumers fail closed until a node is replaced.

**The anchor refreshes only when a node is replaced.** The ASGs carry no
`UpdatePolicy`. A `CertificateArn` change updates the NLB listeners and the
launch template, and running nodes keep running. ca-pem keeps the old chain
until a replacement node publishes the new one. Certificate rotation
therefore ends with replacing one node.

### AD-6: Applications connect as a non-superuser, and no password reaches argv

**Each stack has an application user.** The stack generates a second secret,
`neo4j/<stack>/app-password`. It is 32 alphanumeric characters, encrypted with
`Neo4jStackKey` and retained like the admin secret. The stack publishes its ARN
as `app-password-secret-arn` and the username as `app-username`. The user is
`neo4j_app` and holds the built-in `architect` role. Applications use it. The
`neo4j` superuser is for operators.

**The sample app's main Lambda is the reference consumer.** The main Lambda in
`sample-private-app` connects as `neo4j_app`. Its role can read only the
application secret and decrypt with the stack key. The optional resilience
Lambda is test tooling. It connects as `neo4j` because `SHOW SERVERS` is an
admin-only command.

**Every node tries to create the user on its first boot.** The user lives in
the replicated `system` database. Only the `system` leader accepts the write.
The bootstrap connects to `localhost` over a direct Bolt connection, which gets
no server-side routing. A `neo4j://` connection would follow the routing table
to the advertised address, which is the load balancer. The leader therefore
creates the user, and the other nodes retry until `SHOW USERS` shows it with
the `architect` role. A node fails closed after 600 seconds. The 600 seconds
start after `service neo4j start`, so cluster formation counts against them.
That fits inside the 20-minute creation policy on the supported sizes. A node that
reattaches an existing data volume skips the step.

**The initial password is set before the first start.** `start_neo4j` runs
`neo4j-admin dbms set-initial-password` before `service neo4j start`. The
command runs as the `neo4j` service user through `runuser`, so the auth file
it writes has the right owner. The password is in a temporary file passed as
an `@file` argument, so argv holds only the file path. The file is owned by
`neo4j` with mode 0400 and is removed right after the command.
`cypher-shell` reads the admin password from `NEO4J_PASSWORD` and the
application password from a `:param` line on stdin.

**Password login stays on next to single sign-on.** With an OIDC issuer set,
both provider lists are `oidc-sso,native`. The stack has no switch to remove
`native`. The application user `neo4j_app` and service accounts sign in with
passwords, so a stack without `native` could not serve applications. The
bootstrap also creates `neo4j_app` over a password login. Security review
finding M7 is an accepted risk for this reason. The `Password` parameter
requires at least 12 alphanumeric characters. The failed-authentication
alarm is always on. Single sign-on users get roles only through the group
mapping. The `neo4j` admin password remains the break-glass path when the
identity provider is down.

**The operator bastion holds no credentials.** The Private and ExistingVpc
bastion relays SSM port-forward sessions to the load balancer. Its role reads
the stack's `/neo4j-ee/<stack>/` parameters and writes Session Manager logs.
It reads no secret and decrypts with no key. The `validate-private` tools read
the admin password with the operator's own AWS credentials and keep it in
memory. They run the Neo4j driver and `cypher-shell` on the operator machine
through the tunnel. The bastion UserData installs no package. Its security
group allows outbound port 443 to the endpoint security group and to the S3
prefix list. It also allows ports 7473 and 7687 to the load balancer security
group. The S3 rule leaves the same residual that AD-7 accepts for the nodes.
Security review finding H3 flagged the old bastion, whose role could read the
superuser password, as a credential source for anyone able to open a session
on it.

**Interactive sessions are logged to the stack.** The stack creates a Session
Manager preferences document and publishes its name as the
`Neo4jSessionDocumentName` output. A shell session started with that document
through `--document-name` streams to the `/neo4j/<stack>/sessions` log group.
The group is encrypted with `Neo4jStackKey` and keeps 90 days. Both the
bastion role and the node role can write to it. Session Manager does not log
port-forwarding sessions. The operator tools therefore leave no session
transcript. CloudTrail `StartSession` events and the Neo4j security and query logs are
the trail for those sessions.

### AD-7: Node egress reaches only VPC endpoints, cluster peers and the identity provider

**Private and ExistingVpc nodes have no general internet route.** The node
security groups allow outbound port 443 to the endpoint security group and to
the S3 prefix list. They also allow the cluster ports 5000, 6000, 7000 and
7688 to the Internal security group. When `OidcIssuerUri` is set, the `OidcEnabled` condition adds outbound port 443 to
any address. Neo4j needs that rule to fetch the identity provider's discovery
document and signing keys. Security review finding H2 flagged the old
allow-all egress as an exfiltration path for a compromised node.

**The boot path calls only AWS APIs behind interface endpoints.** The stack
creates nine interface endpoints: `ssm`, `ssmmessages`, `ec2messages`, `logs`,
`secretsmanager`, `ec2`, `cloudformation`, `autoscaling` and `acm`. The node
boot path needs each one. An ExistingVpc stack with `CreateVpcEndpoints=false`
relies on the customer's endpoints, which must cover all nine services. A node
that cannot reach one of them fails at boot.

**Endpoint policies are account-wide.** Each endpoint policy allows any action
by any principal in the stack account. Hume and other workloads in the account
share these endpoints, so a policy scoped to the node role would break them.
The node IAM role is the control on what a node can call. The S3 gateway
endpoint allows the AWS-owned package and agent buckets plus buckets owned by
the stack account, for principals in the stack account.

**Cost.** Interface endpoints bill per hour in each Availability Zone. The
five endpoints added for H2 are `ec2messages`, `ec2`, `cloudformation`,
`autoscaling` and `acm`. Across three zones they cost about $110 a month per
stack, before data processing charges.

**Nodes reach only same-region S3.** A node egress rule allows port 443 to
the region's AWS-managed S3 prefix list. It restores SSM Agent updates, patch
baselines and Hume seeding from a same-region bucket. The prefix list is
regional, so a cross-region S3 URL still fails from a node. In Private, the
traffic uses the stack's S3 gateway endpoint, so nodes reach only the AWS-owned
package and agent buckets and buckets in the stack account. ExistingVpc creates
no gateway endpoint. There, the customer's gateway endpoint policy limits the
buckets, or traffic leaves through the customer's NAT route to any same-region
bucket.

**A custom resource looks up the S3 prefix list.** CloudFormation has no
intrinsic that returns a managed prefix-list ID. `S3PrefixList` is backed by
an inline Lambda that calls `ec2:DescribePrefixLists` for
`com.amazonaws.<region>.s3`. Its role grants only that call and writes to its
own log group. The Lambda runs outside the VPC. The custom resource fails the
stack operation unless exactly one prefix list matches.

**Empty egress lists carry a placeholder rule.** CloudFormation adds an
allow-all egress rule to a security group whose `SecurityGroupEgress` list is
empty. The endpoint security group therefore carries one rule to
`127.0.0.1/32`, which removes the default and matches no real traffic. The
NLB security group carries the same placeholder plus egress to the External
security group on the Browser and Bolt ports.

**In ExistingVpc, the endpoint security group admits `AllowedCIDR`.** Private
DNS on a created endpoint applies to the whole VPC. Other workloads in the
customer's VPC resolve the nine service names to these endpoints. Without
ingress from `AllowedCIDR`, those workloads would lose access to the services.

**Public nodes keep HTTPS egress.** Public stacks have no interface endpoints,
so nodes reach AWS APIs over the internet. Node egress is port 443 to any
address plus the cluster ports. HTTPS exfiltration remains possible on Public,
and `PUBLIC.md` states this.

**Accepted residual: DNS exfiltration.** The VPC resolver answers queries for
any public name, so a compromised node can encode data in DNS queries. Security
groups do not filter traffic to the resolver. Route 53 Resolver DNS Firewall is
the future control for this path.

---

## 6. The AMI / Template / UserData / Bootstrap Split

| Layer | Owns | Changeable by | Examples |
|---|---|---|---|
| **AMI** (`marketplace/create-ami.sh`) | OS-level, immutable, Neo4j-independent concerns, plus the pinned Neo4j packages (AD-1) | AMI rebuild + Marketplace submission + instance replacement | OS patches; AWS CLI v2; `python3.11`, `jq`, `openssl`; `amazon-cloudwatch-agent`; `aws-cfn-bootstrap` (`cfn-init`, `cfn-signal`); Neo4j yum repo + GPG key, repo disabled; `neo4j-enterprise` and `cypher-shell` at the pinned `NEO4J_VERSION`, service disabled and never started; `neo4j` system user (uid/gid 500); SSH hardening; IMDSv2 enforcement |
| **Template** (rendered by `build.py`) | Static Neo4j config and orchestration body as cfn-init metadata on `Neo4jLaunchTemplate`; the per-topology CloudFormation resources | Template update, zero Marketplace churn | `neo4j-base.conf` contents; the inlined bootstrap script; NLB TLS listeners and target groups; ACM `CertificateArn` and `AdvertisedDNS` parameters; the mandatory-TLS Rules; the optional Route 53 private DNS resources (AD-4) |
| **UserData** (`templates/src/userdata.sh`) | CloudFormation signaling and metadata resolution that cannot be delegated | Template update | IMDSv2 token + instance-id/AZ fetch; stack-id and logical-id tag lookups; admin and application password fetch from Secrets Manager; `cfn-init`; export of runtime env vars; bootstrap invocation; unset of both passwords; `cfn-signal`; ERR trap |
| **Bootstrap** (`templates/src/bootstrap/neo4j-bootstrap.sh` + partials) | Boot-time install verification, plugin install, configuration application, security assertion, service start | Template update | `verify_neo4j_installed`, `install_apoc`, `install_plugin`; `apply_base_conf`; runtime overlay functions including `configure_tls` (AD-4); `assert_security_invariant`; `start_neo4j`; `create_app_user` (AD-6) |

The AMI carries no project-authored Neo4j configuration key, no value, and no
conf-mutating logic. The only `neo4j.conf` on the image is the package
default. The same AMI is reused across all three topologies: Public, Private,
and Private Existing VPC.

### Node tool contract

SSM Run Command runs commands on a node as root without a login `PATH`.
Callers must use absolute paths. The project promises two tool paths on every
node:

- `/usr/local/bin/aws` is AWS CLI v2. `create-ami.sh` installs it, and
  `test-ami.sh` CHECK 6 asserts it.
- `/usr/bin/cypher-shell` comes from the `cypher-shell` package at the same
  version as the server. `test-ami.sh` CHECK 7 and CHECK 12 assert it, and
  `verify_neo4j_installed` fails the boot without it.

Hume's `stack/seed_nodes.py` is a known consumer of both paths. Moving either
path is a breaking change. It needs a notice to consumers before the AMI
ships. No other path on the node is part of the contract.

---

## 7. The Security Invariant and Defense in Depth

`internal.dbms.cypher_ip_blocklist` is the single most protected key in the
system. Its value in `neo4j-base.conf` covers the full IPv4 link-local range
`169.254.0.0/16`, which holds the IMDS credential endpoint `169.254.169.254`. It
covers the RFC1918 VPC-internal ranges `10.0.0.0/8`, `172.16.0.0/12` and
`192.168.0.0/16`. It covers loopback `127.0.0.0/8` and `::1/128`, the
"this network" range `0.0.0.0/8`, and the shared address space
`100.64.0.0/10`. It also covers the IPv6 unique-local, link-local and
multicast ranges. Removing it would let any Cypher user steal the instance IAM
role credentials and pivot into the VPC. The loopback ranges stop Cypher from
reaching the local Neo4j HTTP and Bolt listeners.

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
- **Do not pass a Secrets Manager password as a positional argument.** argv
  is visible in the process list and cloud-init logs. This covers the
  bootstrap, `neo4j-admin` and `cypher-shell` (AD-6).
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
- **Do not install any package on the node boot path, including Neo4j and
  pip packages.** Neo4j, `cypher-shell`, `openssl` and every other package
  belong in the AMI. A boot-path install needs a package mirror and an
  internet route on every launch and every ASG self-heal (AD-1).
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
| NFR-16 | `Neo4jStackKey` is unconditional, rotated and retained; the password secret is retained and encrypted with it; both log groups use it (AD-5) | `SecurityReviewHardeningTests.test_password_secret_is_retained_and_uses_the_stack_key`, `test_log_groups_are_encrypted_with_the_stack_key` |
| NFR-17 | The NLB writes access logs to an encrypted, private, 90-day bucket; deletion protection is a parameter; the failed-auth alarm and topic are unconditional (AD-5) | `SecurityReviewHardeningTests.test_nlb_writes_access_logs_and_exposes_deletion_protection`, `test_failed_auth_alarm_is_unconditional` |
| NFR-18 | Every launch template sets `HttpPutResponseHopLimit: 1` (AD-5) | `SecurityReviewHardeningTests.test_every_launch_template_sets_hop_limit_one` |
| NFR-19 | `cloudformation:*` grants name the stack ARN; no ARN in any template hard-codes the `aws` partition | `RenderedTemplateSecurityTests.test_cloudformation_actions_are_scoped_to_this_stack`, `test_arns_use_the_partition_pseudo_parameter` |
| NFR-20 | No template publishes `tls-self-signed` or the `IsSelfSignedTls` condition (AD-5) | `RenderedTemplateContractTests.test_tls_self_signed_contract_is_removed` |
| NFR-21 | The application secret is generated, alphanumeric, retained and encrypted with the stack key; the node role reads it; the stack publishes `app-password-secret-arn` and `app-username`; the bootstrap creates `neo4j_app` with `architect` after start, on first boot only (AD-6) | `SecurityReviewBatchBTests.test_app_password_secret_is_generated_alphanumeric_and_retained`, `test_node_role_reads_both_password_secrets`, `test_app_user_ssm_parameters`, `test_bootstrap_creates_app_user_after_start_on_first_boot`; `ShellPartialTests.test_create_app_user_*` |
| NFR-22 | The initial password is set as `neo4j` from a 0400 `@file` before the first start; no password appears in argv; UserData unsets both passwords before signaling (AD-6) | `ShellPartialTests.test_install_neo4j_commands_and_first_boot_password`, `test_set_initial_password_failure_fails_closed_and_removes_file`; `SecurityReviewBatchBTests.test_userdata_fetches_app_password_and_unsets_secrets_before_signal` |
| NFR-23 | Every template publishes `kms-key-arn` unconditionally as `!GetAtt Neo4jStackKey.Arn` (AD-5) | `RenderedTemplateContractTests.test_kms_key_arn_is_published_unconditionally_as_full_arn`, the three `test_*_publishes_exact_ssm_contract` tests |
| NFR-24 | Neo4j and `cypher-shell` are baked into the AMI at the pinned version with the repo and service disabled; the node boot path installs no package and fails closed without them (AD-1) | `ShellPartialTests.test_create_ami_bakes_pinned_neo4j_and_never_starts_it`, `test_verify_neo4j_installed_*`; `RenderedTemplateContractTests.test_node_boot_path_installs_no_packages`; `test-ami.sh` CHECK 7, 8, 10 and 12 |
| NFR-25 | No security group has an empty egress list; Private and ExistingVpc node egress is the endpoint SG on 443, cluster self rules and OIDC-conditional 443 only; Public node egress is 443 and cluster only; the interface endpoints cover the nine node services, each with an account-scoped policy; endpoint SG ingress comes from the nodes and the bastion (AD-7) | `SecurityReviewBatchCTests.test_no_security_group_has_an_empty_egress_list`, `test_node_security_groups_declare_inline_egress`, `test_private_node_egress_is_endpoints_cluster_and_oidc_only`, `test_public_node_egress_is_https_and_cluster_only`, `test_interface_endpoints_cover_every_node_service`, `test_every_vpc_endpoint_has_an_account_scoped_policy`, `test_endpoint_sg_ingress_comes_from_nodes_and_bastion` |
| NFR-26 | Private and ExistingVpc node egress to S3 is port 443 to the prefix list returned by the `S3PrefixList` custom resource for `com.amazonaws.${AWS::Region}.s3`; the lookup Lambda runs outside the VPC, its role grants only `ec2:DescribePrefixLists` and its own log group, and it fails unless exactly one prefix list matches; Public has no lookup (AD-7) | `SecurityReviewBatchCTests.test_s3_egress_uses_the_looked_up_prefix_list`, `test_s3_prefix_list_function_is_least_privilege_and_fails_loudly`, `test_private_node_egress_is_endpoints_cluster_and_oidc_only` |
| NFR-27 | The bastion role grants no `secretsmanager` or `kms` action; bastion egress is the endpoint SG and the S3 prefix list on 443 and the NLB SG on 7473 and 7687 only; the bastion UserData installs no package; the Session Manager preferences document streams to the encrypted `/neo4j/<stack>/sessions` group and is a stack output (AD-6) | `SecurityReviewBatchDTests.test_bastion_role_holds_no_credential_grants`, `test_bastion_and_nodes_can_stream_to_the_session_log_group`, `test_bastion_egress_is_endpoints_nlb_and_s3_only`, `test_bastion_userdata_installs_nothing`, `test_session_preferences_log_to_the_encrypted_stack_log_group` |
| NFR-28 | Private and ExistingVpc own `ca-pem` with fixed properties and the value `pending`; every ASG depends on it; the node role reads only the stack certificate and writes only ca-pem; UserData passes the ARN base64-encoded; every node publishes a PEM value after `configure_tls` and before start and `cfn-signal`, fails closed otherwise and never writes a non-PEM value; Public publishes nothing (AD-5) | `SecurityReviewBatchETests.test_ca_pem_parameter_properties_stay_fixed`, `test_public_publishes_no_trust_anchor`, `test_every_asg_waits_for_the_ca_pem_parameter`, `test_node_role_can_read_the_certificate_and_write_only_ca_pem`, `test_private_userdata_passes_the_arn_base64_and_the_publish_flag`, `test_trust_anchor_published_after_tls_and_before_start_and_signal`; `ShellPartialTests.test_publish_trust_anchor_*`; the release-suite check "ca-pem published and verifies the NLB chain" |

`RenderedTemplateContractTests` additionally assert the rendered cfn-init
metadata, the single-LaunchTemplate placement, and the embedded base-conf
content (AD-3, security invariant layer 2).
