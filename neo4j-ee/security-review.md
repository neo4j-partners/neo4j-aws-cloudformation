# Security Review: Neo4j EE Private Template as Used by Hume

Date: 2026-10-31
Branch: `feat/tls-self-signed-ssm-output`
Template reviewed: `templates/neo4j-private.template.yaml` and the source partials under `templates/src/` that render it, plus `templates/src/userdata.sh`, `templates/src/bootstrap/neo4j-bootstrap.sh`, `templates/src/partials/*.sh`, `templates/src/neo4j-base.conf`, `marketplace/create-ami.sh`, `deploy.py`, and `scripts/certificate.py`.
Consumer reviewed: GraphAware Hume, repo `hume-aws-cloudformation`, files `stack/neo4j_env.py`, `stack/hume-ecs.template.yaml`, `stack/cognito.py`, `docs/neo4j-ee.md`, `docs/security-and-tls.md`.

This review changes no template, script, or config. It records findings only.

## 1. Overview

### 1.1 What the Private template deploys

The Private template builds a new VPC at `10.0.0.0/16` with three public subnets at `10.0.1.0/24` to `10.0.3.0/24` and three private subnets at `10.0.10.0/24` to `10.0.12.0/24`. The public subnets hold only NAT gateways. Each private subnet holds one Neo4j node in its own single-instance Auto Scaling group, with a retained, encrypted gp3 data volume pinned to the same Availability Zone. An internal Network Load Balancer terminates TLS on ports 7473 and 7687 with an ACM certificate and re-encrypts to a per-instance self-signed certificate. A t4g.small bastion in the first private subnet is the only operator entry point, reached through SSM Session Manager with no inbound rules. Interface endpoints for SSM, SSM Messages, CloudWatch Logs, and Secrets Manager plus an S3 gateway endpoint keep AWS control traffic inside the VPC.

The stack stores the admin password in Secrets Manager at `neo4j/<stack>/password`, publishes integration values under the SSM prefix `/neo4j-ee/<stack>/`, and optionally configures OIDC single sign-on and a Route 53 private hosted zone for the advertised DNS name.

### 1.2 How Hume uses it

Hume deploys its ECS stack into the Neo4j VPC and creates no network of its own. The Hume run book uses `MODE=Private`, three nodes, `CREATE_PRIVATE_DNS=true`, zone `neo4j.local`, advertised name `neo4j-oidc.neo4j.local`, region `us-east-1`, Bloom and GDS off, and the shared Cognito user pool `us-east-1_KYOsI4jUW` for OIDC. The integration contract, confirmed by the Hume session owners, is:

| Neo4j value | Where Hume reads it | What Hume does with it |
|---|---|---|
| SSM `advertised-dns` | `neo4j_env.py` | Becomes `Neo4jHost`. Every Bolt connection uses `neo4j+s://<host>:7687` with full certificate and hostname verification. |
| SSM `vpc-id` | `neo4j_env.py` | Becomes `VpcId`. Hume runs inside the Neo4j VPC. |
| SSM `external-sg-id` | `neo4j_env.py` | Becomes `Neo4jNlbSecurityGroupId`. Hume adds one ingress rule on 7687 from its own instance security group, as a separate `AWS::EC2::SecurityGroupIngress` resource, `hume-ecs.template.yaml:829`. |
| SSM `vpc-endpoint-sg-id` | `neo4j_env.py` | Hume adds one ingress rule on 443 from its instance security group so its tasks can use the Neo4j interface endpoints, `hume-ecs.template.yaml:839`. |
| SSM `password-secret-arn` | `neo4j_env.py` | ECS injects the secret into Hume containers at task start. The task execution role has `GetSecretValue` on that one ARN. |
| SSM `private-subnet-1-id`, `private-subnet-2-id` | `neo4j_env.py` | Hume's container instance and tasks run in the first private subnet. |
| SSM `nlb-dns` | `neo4j_env.py:131-190` | Used once at setup to fetch the NLB certificate. |
| Output `Neo4jOperatorBastionId` | `neo4j_env.py` | Hume runs `openssl s_client` on the bastion through SSM Run Command to capture the NLB leaf certificate, then builds a JKS truststore from it and stores it in a Hume secret. |
| Public subnets by CIDR | `neo4j_env.py:112-128` | Hume finds the three public subnets by the fixed CIDRs for its internet-facing ALB and its RDS subnet group. It fails unless exactly three are found. |

Hume connects as the built-in `neo4j` admin user. The user name is hard-coded in the Hume config import at `hume-ecs.template.yaml:3203` and `:3318`. The config import copies the admin password into Hume's own secret store twice, as the Neo4j resource and as `spine-neo4j-sa-password`. The Spine demo load task also runs as admin. A redesign approved by the Hume team will move the demo load onto a Neo4j seed node over SSM Run Command, where the node reads the password secret with its own instance role.

Hume's api and media tasks reach the identity provider on 443 through the Neo4j NAT gateways. Hume must be deleted before the Neo4j stack because the Neo4j teardown removes the VPC, NLB, and NAT gateways that Hume depends on.

### 1.3 Method

Each layer was traced from template parameter to shell to `neo4j.conf`: parameters and rules, IAM, security groups, networking and endpoints, secrets and SSM publication, UserData and bootstrap, TLS, OIDC, logging, the AMI builder, and the Hume consumer code. An independent vulnerability pass produced candidate findings, and each one was checked against the source before it was kept. `cfn-lint 1.48.1` reports no findings on the rendered Private template. The cfn-guard compliance ruleset was not run because the rendered template exceeds the size the tool accepts in one call.

### 1.4 What is already done well

- **Shell injection is closed.** Every parameter that reaches the shell carries a restrictive `AllowedPattern`, and the OIDC values travel base64-encoded, `templates/build.py` and `neo4j-bootstrap.sh:30-38`. The `set_neo4j_conf` helper escapes sed metacharacters.
- **The password never enters UserData or outputs.** UserData fetches it from Secrets Manager at boot and passes it through the environment, `userdata.sh:23-39`.
- **IMDSv2 is required** on the launch template and the bastion, and the Cypher IP blocklist covers the IMDS range, so Cypher cannot read the instance role credentials. The blocklist is enforced at four layers.
- **Encryption at rest and in transit** is in place. Root and data volumes are encrypted. The NLB uses `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` and Bolt on the instance is `tls_level REQUIRED`, `configure-tls.sh:81`.
- **Network segmentation is correct.** The NLB is internal, nodes accept 7473 and 7687 only from the NLB security group, cluster ports only from the internal security group, and the bastion has no ingress.
- **The AMI builder** runs `dnf update`, disables SSH password and root login, enforces IMDSv2 on the AMI, and refuses to deregister an AMI that a live launch template references.
- **Hume's side is careful.** Hume verifies the certificate and host name with `neo4j+s`, injects the password through ECS secrets rather than parameters, and adds its security group rules as separate resources so a Hume delete removes them cleanly.

### 1.5 Summary of findings

| Severity | Count | Items |
|---|---|---|
| Critical | 0 | None. No unauthenticated remote path to data or credentials was found. |
| High | 3 | H1 shared superuser, H2 unrestricted egress, H3 bastion holds the superuser secret |
| Medium | 9 | M1 to M9 |
| Low | 6 | L1 to L6 |

The three High items share one root cause. The only database principal the stack provisions is the superuser, and nothing constrains where a node or the bastion can send traffic. Fixing H1 and H2 removes most of the practical impact of the Medium and Low items.

## 2. High

### H1. Hume and every operator share the `neo4j` superuser, and the stack provisions no least-privilege user

- **Where:** `templates/src/stack-config.yaml:1-10` creates the only credential. `templates/src/outputs-private.yaml` publishes `Username` as `neo4j`. Hume hard-codes `user: neo4j` at `hume-ecs.template.yaml:3203` and `:3318` and copies the password into its own secret store.
- **Problem:** The integration contract is "read this secret", and the secret is the superuser. A compromised Hume task, a leaked Hume database, or a leaked task definition yields full admin on every database. Hume's role model, including the SPINE roles, cannot restrict what Hume can do. The earlier Hume review of 2026-10-21 raised the same point as its top finding. Because `preserve_client_ip.enabled` is false on the target groups, `security.log` records the NLB address, so actions by Hume and by operators are indistinguishable.
- **Fix:** On first boot of node 1, after Neo4j is up, create an application role and user from a second secret at `neo4j/<stack>/app-password`, grant the role only the privileges the consumer needs, and publish that ARN as `app-password-secret-arn` under the SSM prefix. Keep the superuser secret operator-only. Hume has said it will point `Neo4jPasswordSecretArn` at the new secret and drop the hard-coded user name. Document that the Spine demo load, once moved to the seed node, should also run as a scoped user rather than admin.

### H2. Neo4j nodes have unrestricted egress to the internet, so any authenticated user can exfiltrate data through APOC and `LOAD CSV`

- **Where:** `templates/src/security-groups.yaml` defines `Neo4jExternalSecurityGroup` and `Neo4jInternalSecurityGroup` with no `SecurityGroupEgress`, so the default allow-all egress applies. `templates/src/networking-private.yaml` routes `0.0.0.0/0` from every private subnet to a NAT gateway. `templates/src/neo4j-base.conf:10-12` allow and unrestrict `apoc.*`. The blocklist on line 9 covers only private, link-local, and IPv6 local ranges.
- **Problem:** `apoc.load.json`, `apoc.load.jsonParams`, and `LOAD CSV` run in read mode, so the built-in `reader` role can call them against any public host. A user with reader credentials, an OIDC user mapped to reader, or a compromised Hume task holding the superuser password can post query results to an external endpoint. DNS-based exfiltration works even if HTTP were blocked. Nothing logs or alarms on this because the login succeeded.
- **Fix:** Add explicit egress to both node security groups: 443 to `VpcEndpointSecurityGroup`, cluster ports to self, and 443 to the package mirrors only if boot-time install stays, see M1. Narrow `dbms.security.procedures.allowlist` and `unrestricted` to the procedures the product needs and drop `apoc.load.*` and `apoc.import.*` from `unrestricted`, see M6. Consider a parameter that lets a customer disable NAT egress entirely once the AMI carries everything needed at boot.

### H3. The bastion instance role hands the superuser password to anyone who can open a session on it

- **Where:** `templates/src/iam.yaml:79-106` grants `Neo4jBastionRole` `secretsmanager:GetSecretValue` and `DescribeSecret` on the password secret plus `ssm:GetParameter*` on `/neo4j-ee/<stack>/*`. The bastion ID is a stack output. `Neo4jBastionSecurityGroup` has no egress rules and its subnet routes to the NAT gateway. No SSM session logging or session document restriction is configured.
- **Problem:** A principal with only `ssm:StartSession` or `ssm:SendCommand` on the bastion can read the superuser password with the instance role, open `cypher-shell`, which is preinstalled, and create a persistent user. Hume's certificate fetch already requires `ssm:SendCommand` on the bastion, so the set of principals with that permission is wider than operators. CloudTrail cannot tell this from legitimate operator use because the credential is the instance role.
- **Fix:** Treat the bastion as a jump host, not a credential holder. Remove `GetSecretValue` from the bastion role and have `validate-private` and `admin-shell` read the secret with the operator's own identity, or add a resource policy on the secret conditioned on `aws:PrincipalArn` or `aws:SourceIdentity`. Enable Session Manager logging to CloudWatch or S3 with a KMS key. Add egress rules to the bastion security group limited to the endpoint security group on 443 and the NLB security group on 7473 and 7687. Publishing the certificate trust anchor, see M3, removes Hume's need for `SendCommand` on the bastion entirely.

## 3. Medium

### M1. Neo4j Enterprise is installed unpinned from `stable/latest` on every boot and every ASG replacement

- **Where:** `templates/src/partials/install-neo4j.sh:4` runs `yum -y install neo4j-enterprise`. The AMI bakes the repo and GPG key only, `marketplace/create-ami.sh:205`. The bastion UserData runs `pip install 'neo4j>=6,<7' boto3` and adds the Neo4j repo at boot, `templates/src/networking-private.yaml:524-532`. `create-ami.sh:195` downloads the AWS CLI zip without checking the published signature.
- **Problem:** GPG signing protects integrity but not version drift. A node replaced by the ASG months later installs a newer release than its peers, and that release was never run through the release gate, `assert_security_invariant`, or the G3 audit. The architecture document records this as AD-1 and prohibits boot-path package installs for `openssl` for the same reason. The bastion installs PyPI packages as root on the host that holds the superuser secret, see H3, with a loose version range and no hashes.
- **Fix:** Pin the version. Preferred: bake the `neo4j-enterprise` RPM, `cypher-shell`, and the bastion Python packages into the AMI, since the Marketplace listing already names a version in the `ImageId` SSM path. Alternative: a `Neo4jVersion` parameter and `yum -y install neo4j-enterprise-<version>`. Use `pip install --require-hashes` for the bastion. Verify the AWS CLI zip signature in `create-ami.sh`. Add a contract test that no boot-path install is unpinned.

### M2. The default Bolt scheme is `neo4j+ssc`, and a plain SSM parameter tells consumers to skip verification

- **Where:** `templates/src/parameters-bolt-tls-scheme.yaml` defaults `BoltTlsScheme` to `neo4j+ssc`. `templates/src/stack-config.yaml` publishes `bolt-url` and `tls-self-signed` as SSM `String` parameters. `deploy.py` auto-imports a 365-day self-signed certificate when no `--cert-arn` is passed, `scripts/certificate.py`.
- **Problem:** The published Bolt URL defaults to a scheme that disables certificate validation, and a mutable parameter is the advertised switch to turn verification off. Anyone with `ssm:PutParameter` on the prefix can flip a consumer into unverified mode or change the host without touching the Neo4j stack. Hume ignores both values and uses `neo4j+s` with a pinned truststore, which is the right choice, but the Marketplace default steers other buyers the other way.
- **Fix:** Default `BoltTlsScheme` to `neo4j+s` and make `+ssc` an explicit opt-in for test stacks. Drop `tls-self-signed` or mark it informational. Publish a trust anchor instead, see M3.

### M3. The stack publishes no trust anchor, so Hume pins the NLB leaf certificate on first use through SSM Run Command

- **Where:** `neo4j_env.py:131-190` runs `openssl s_client -connect <nlb>:7687 -servername <host>` on the bastion and writes the result to `stack/.deploy/neo4j-ca.pem`. Hume's truststore replaces the JVM default roots and the truststore password is printed by the JVM, `hume-ecs.template.yaml` around line 320.
- **Problem:** This is trust on first use. A certificate swapped on the listener before Hume's first deploy is pinned. A legitimate certificate rotation, including ACM Private CA renewal, breaks Hume until the truststore secret is rebuilt. The fetch requires `ssm:SendCommand` on the bastion and couples Hume to the `Neo4jOperatorBastionId` output.
- **Fix:** Publish the issuing certificate as the contract: the PEM of the self-signed root or the ACM Private CA ARN, as an SSM parameter such as `/neo4j-ee/<stack>/ca-pem`. When a self-signed listener certificate is used, issue it from a stack-owned CA so renewals stay trusted. Hume has asked for exactly this.

### M4. Password policy is weak, the secret has no rotation and no customer-managed key, and rotation would not reach Hume

- **Where:** `templates/src/parameters-common.yaml` sets `AllowedPattern: ^[a-zA-Z0-9]{8,}$`. `templates/src/stack-config.yaml:1-10` sets no `KmsKeyId` and no rotation. Hume imports the password into its own store at config import time.
- **Problem:** Eight alphanumeric characters is the floor for the superuser of a database shared across Hume, Bloom, Browser, and operators. Nothing ever rotates it, and because Hume keeps a copy, a Secrets Manager rotation would leave Hume with the old value until a re-import. The Hume team confirms this flow is untested.
- **Fix:** Raise `MinLength` to 12 and allow symbols. The value never touches a shell directly, so `^[^\s]{12,}$` is safe. Encrypt the secret with a customer-managed key so the key policy can gate reads. Document a rotation runbook that runs `ALTER CURRENT USER SET PASSWORD` from the bastion and triggers the Hume re-import. The application user from H1 makes rotation cheaper because only the consumer secret changes.

### M5. The password is placed on the `neo4j-admin` command line after the service starts, and it stays in the UserData environment afterwards

- **Where:** `templates/src/partials/install-neo4j.sh:11-13` runs `service neo4j start` and then `neo4j-admin dbms set-initial-password "${initialPassword}"`. `templates/src/userdata.sh:39-48` exports `password` and never unsets it before `cfn-signal` and `alternatives` run. The architecture document prohibits argv for the password, `docs/architecture/template-architecture.md:416`.
- **Problem:** For the duration of the command the password is readable in `/proc/<pid>/cmdline` by any local process. The window is short and only SSM reaches the host today, so this is a defense gap rather than an exploit. Neo4j documents `set-initial-password` as a pre-start operation, so the ordering should be confirmed. Stacks do validate, which suggests the command wins the race on current instance sizes.
- **Fix:** Run `set-initial-password` before `service neo4j start` and feed the value from stdin or a 0600 file. Add `unset password` in UserData right after the bootstrap returns. Update the test at `tests/test_template_partials.py:807` to assert the value is absent from argv.

### M6. `procedures.unrestricted` and `procedures.allowlist` cover every APOC, GDS, and Bloom procedure

- **Where:** `templates/src/neo4j-base.conf:10` and `:12`. `templates/src/partials/configure-neo4j.sh:87` also maps `semantics.extension=/rdf` as an unmanaged extension that is never installed.
- **Problem:** `unrestricted` removes the security sandbox from a procedure. Only a handful need it. Granting it to a whole namespace means every current and future procedure in that namespace runs with full internal access, which bypasses the fine-grained role model Enterprise customers rely on. The dead `/rdf` mapping will activate silently if a future plugin provides that package.
- **Fix:** Enumerate the procedures the product needs in both keys. Remove `semantics.extension=/rdf` until n10s is a supported plugin.

### M7. OIDC keeps password login as an always-on fallback, accepts tokens for every listed client, and the Cognito client allows the password grant

- **Where:** `templates/src/partials/configure-oidc.sh:37-38` sets `authentication_providers` and `authorization_providers` to `oidc-sso,native`. The `audience` value is the full `OidcClientId` list. On the Hume side, `stack/cognito.py` creates the `neo4j` Browser client with `ALLOW_USER_PASSWORD_AUTH` and a placeholder redirect of `https://localhost:7473/browser/`.
- **Problem:** An organisation that enables SSO with MFA still has the superuser password as a parallel path that SSO policy cannot govern. A token issued to the Hume web client is valid for Neo4j and the reverse. The password grant on the Browser client lets anyone with a phished Cognito password mint an ID token without the hosted UI, so `neo4j-admins` membership becomes admin on Neo4j with no MFA. The Hume team confirmed the `neo4j` client has no client secret, which is correct for a public PKCE client.
- **Fix:** Ask the Hume team to disable `ALLOW_USER_PASSWORD_AUTH` on the Neo4j client and register the real Browser callback instead of the placeholder. Keep the audience list; the Hume team owns the client split.
- **Decision (2026-10-31):** Native login stays in both provider lists. The `neo4j_app` user from H1 and every service account authenticate natively, and the bootstrap creates that user over native auth on first boot, so a parameter that removes `native` breaks the stack and the application. A switch to drop `native` was built in batch B and then removed. The residual risk is accepted with the 12-character alphanumeric minimum from M4, the unconditional failed-authentication alarm from L1, the identity-bound bastion from H3, and SSO group mapping for human users.

### M8. IAM statements are wider than the actions require

- **Where:** `templates/src/iam.yaml:29-37` grants `cloudformation:DescribeStack*` and `cloudformation:SignalResource` on `*`. Line 55 hard-codes `arn:aws:logs` instead of `${AWS::Partition}`. `ManagedPolicyArns` also hard-code `arn:aws:iam::aws:policy/...`.
- **Problem:** Both CloudFormation actions accept a stack ARN. On `*`, a compromised node can enumerate every stack's outputs in the account and send signals to unrelated stacks during their rollouts. The hard-coded partition breaks the template in GovCloud and China regions and fails Marketplace region-agnostic guidance.
- **Fix:** Scope the CloudFormation statement to `arn:${AWS::Partition}:cloudformation:${AWS::Region}:${AWS::AccountId}:stack/${AWS::StackName}/*`. Keep the EC2 and Auto Scaling describe actions on `*` in their own statement, since they offer no resource-level scoping. Use `${AWS::Partition}` everywhere.

### M9. VPC endpoints carry no endpoint policy

- **Where:** `templates/src/networking-private.yaml:548-623` creates the ssm, ssmmessages, logs, and secretsmanager interface endpoints and the S3 gateway endpoint with no `PolicyDocument`.
- **Problem:** Without a policy, each endpoint is a transparent path to the whole service in the region. The S3 gateway is the natural exfiltration route for any credential obtained inside the VPC, including a Hume task role, because it bypasses NAT and leaves only a prefix-list entry in flow logs.
- **Fix:** Attach policies conditioned on `aws:PrincipalAccount` equal to the stack account to every endpoint. Scope the Secrets Manager endpoint to `neo4j/<stack>/*` and the license secrets.

## 4. Low

### L1. Attribution is weak: client IP not preserved, no NLB access logs, no SSM session logging, log groups on the AWS-managed key, alarms only with an email

- **Where:** `templates/src/networking-private.yaml` target groups set `preserve_client_ip.enabled=false` and the NLB has no `access_logs.s3.enabled`. `templates/src/observability.yaml` log groups set no `KmsKeyId`. The SNS topic, metric filter, and alarm exist only when `AlertEmail` is set.
- **Problem:** Every `security.log` entry shows an NLB address, so the failed-login alarm cannot say who failed or where to block. A buyer who leaves `AlertEmail` blank gets no alarm at all.
- **Fix:** Enable NLB access logs, which are supported on TLS listeners. Preserving the client IP is preferable but was turned off to avoid hairpin failures from the bastion, so verify before changing it. Attach a KMS key to the log groups. Always create the alarm and make the subscription optional. Enable Session Manager logging.

### L2. `AllowedCIDR` validation and internal ports are looser than they look

- **Where:** `templates/src/parameters-private.yaml` rejects only the literal `0.0.0.0/0`, so `0.0.0.0/1` and `128.0.0.0/1` pass. The default admits the whole VPC. `templates/src/security-groups.yaml:92-117` opens 2003, 2004, and 3637 inside the cluster with no corresponding listener in the bootstrap.
- **Fix:** Require a prefix length of at least 8 in the pattern. Remove 2003, 2004, and 3637 unless a Graphite or other listener is configured. Keep the VPC-wide default but document that Hume-style consumers should rely on security-group references rather than the CIDR.

### L3. The Cypher IP blocklist omits loopback and some reserved ranges

- **Where:** `templates/src/neo4j-base.conf:9`.
- **Problem:** `127.0.0.0/8`, `::1/128`, `0.0.0.0/8`, `100.64.0.0/10`, and the rest of `169.254.0.0/16` are open. Nothing sensitive listens on loopback today, so this is a gap, not an exploit. Enabling the Prometheus endpoint or the HTTP Query API on loopback would change that.
- **Fix:** Extend the list. Keep `assert_security_invariant` presence-only as the architecture requires, and assert the content in `Neo4jBaseConfTests` instead.

### L4. `Node1SnapshotId` has no pattern, and a snapshot restore makes the published secret non-authoritative

- **Where:** `templates/src/parameters-common.yaml` and `templates/src/partials/install-neo4j.sh:12`.
- **Problem:** When node 1 restores from a snapshot, `isFirstBoot` is false and `set-initial-password` never runs, so the live password is whatever the snapshot holds, not the value in Secrets Manager. The parameter also accepts any string.
- **Fix:** Add `AllowedPattern: ^$|^snap-[0-9a-f]{8,17}$`. Document that the secret is not authoritative after a restore and require the operator to rotate.

### L5. `ServerSeedProvider` is enabled for the Spine demo

- **Where:** `templates/src/neo4j-base.conf:13`.
- **Problem:** `ServerSeedProvider` lets an admin create a database from a file in another node's seeds directory. This is acceptable because only admins can use it, and Hume needs it for the demo. It widens what a compromised admin credential can do, which is one more reason for H1.
- **Fix:** No change required. Mention it in the product documentation.

### L6. The coming Spine redesign sends SSM Run Command to Neo4j nodes

- **Where:** Hume design approved 2026-10-31, not yet implemented.
- **Problem:** Hume tooling will execute commands on cluster nodes. Nodes already hold `GetSecretValue` on the password secret, so no new IAM is needed on the Neo4j side, but the set of principals who can run code on a database node grows to include the Hume operator.
- **Fix:** Document the IAM boundary. Scope `ssm:SendCommand` for Hume operators to the seed node by tag, and log the commands.

## 5. CloudFormation best practices

`cfn-lint` is clean. The items below are structural improvements rather than defects.

- **Use `${AWS::Partition}` everywhere.** The logs policy and `ManagedPolicyArns` hard-code `arn:aws`, see M8.
- **Set `HttpPutResponseHopLimit: 1` explicitly** in `MetadataOptions` on the launch template and the bastion. The default is 1, but an explicit value survives future changes and satisfies most guard rules.
- **Avoid `Fn::GetAZs` for node placement.** Marketplace guidance says templates must not depend on a particular Availability Zone, and `GetAZs` can return a zone the account cannot launch in. Offer an optional `AvailabilityZones` list parameter and fall back to `GetAZs`.
- **Add the missing parameters to the interface metadata.** `BoltTlsScheme` and `Node1SnapshotId` are absent from `ParameterGroups`, so they render at the bottom of the console with no grouping.
- **Enable NLB deletion protection** behind a parameter for production use, and document stack termination protection in the run book.
- **Create `Neo4jPrivateRouteTable2` and `Neo4jPrivateRouteTable3` conditionally.** They exist unconditionally with no association in single-node mode.
- **Tag every billable resource.** NAT gateways, EIPs, the secret, and the log groups lack the `aws-apn-id` tag that the other resources carry.
- **Keep `DeletionPolicy: Retain` on the password secret** or document that stack delete removes it while the data volumes survive. Today a retained volume outlives the secret that opens it.
- **Prefer `AWS::EC2::SecurityGroupEgress` resources** for the node security groups so the intent is visible, see H2.
- **Publish the public subnet IDs** as SSM parameters, see section 7.

## 6. Marketplace deployment best practices

- **Disclose deployment-time external dependencies.** The boot path reaches `yum.neo4j.com`, `debian.neo4j.com`, PyPI, and the AWS CLI download. Marketplace policy requires sellers to disclose such dependencies and holds them responsible for their availability and security. Baking them into the AMI, see M1, removes the disclosure and the risk.
- **Password-based authentication.** Marketplace prohibits password-based authentication for instance services except for non-administrative host services without another method. The Neo4j admin password is an application credential rather than a host login, SSH password login is disabled, and OIDC is available. Confirm with the Marketplace reviewer that the current listing's treatment still applies, and lead the usage instructions with the OIDC path.
- **Document the minimally privileged IAM roles.** The usage instructions must explain what `Neo4jRole` and `Neo4jBastionRole` can do, why describe actions are on `*`, and that the seller has no access to customer instances.
- **Document the bastion.** The listing must say that a t4g.small instance is created, that it uses SSM rather than SSH, what its role can read, see H3, and how to remove it.
- **Document the ports and recommended source ranges.** The listing should state 7473 and 7687, the `AllowedCIDR` default, and that consumers should use security-group references.
- **AMI age and scanning.** AMIs older than two years are rejected and must pass vulnerability scanning. Keep the rebuild cadence and the `Test 'Add version'` check in `create-ami.sh` in the release process.
- **The `ImageId` default pins a product version path.** `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.03.1` is replaced by Marketplace at listing time, which is correct. Keep the template free of hard-coded AMI IDs.
- **Architecture diagrams.** Each of the three templates needs its own 1100 by 700 diagram that matches what it deploys, including the bastion and endpoints for Private.
- **Three-template limit.** The product is at the maximum of three templates per single-AMI listing. Any new topology must replace one.

## 7. Changes the Hume integration needs from this template

These are the contract changes the Hume team asked for, with the finding each one resolves.

1. **A dedicated application user and secret**, published as `app-password-secret-arn`. Resolves H1 and simplifies M4.
2. **The certificate trust anchor** as an SSM parameter or secret. Resolves M3 and removes Hume's dependency on `Neo4jOperatorBastionId` and `ssm:SendCommand`, which also narrows H3.
3. **Public subnet IDs** as `public-subnet-1-id`, `public-subnet-2-id`, `public-subnet-3-id`. Removes the fixed-CIDR guess in `neo4j_env.py:112-128`, which breaks the moment the VPC layout changes.
4. **A stable `external-sg-id`.** Hume relies on it staying the security group that gates 7687 on the NLB. Keep the name and meaning fixed across releases.
5. **A documented teardown order.** Hume must be deleted first. Add a check to `teardown.sh` that refuses to run while a stack references the VPC.
6. **Three nodes remain required** for Hume because the single-node layout has one zone and Hume's load balancer needs two public subnets. Document this in the Hume run book and the Neo4j listing.
7. **Rotation.** Hume re-reads the secret at task start but its imported copy is stale after a rotation. The application user from item 1 plus a documented re-import step closes this.

## 8. Open items not verified in this review

- Whether `neo4j-admin dbms set-initial-password` after `service neo4j start` is reliable on every instance size, see M5.
- The cfn-guard compliance ruleset was not run on the rendered template.
- Hume's RDS subnet group uses the Neo4j public subnets with `PubliclyAccessible: false`. That belongs to the Hume review, but the Neo4j template could offer private subnets for data stores if it publishes subnet IDs, see section 7.
