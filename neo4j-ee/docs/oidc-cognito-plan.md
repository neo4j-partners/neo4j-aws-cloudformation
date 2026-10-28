# Neo4j EE OIDC with Amazon Cognito: Phased Plan

Status: **Draft, awaiting Ryan's approval.** Nothing in this plan is implemented.

## Summary

Neo4j Enterprise Edition can check a Cognito login token itself and turn the user's Cognito groups into Neo4j roles. It does this through its built-in OIDC support. No plugin is needed. Today the EE templates turn on password login only.

This plan first proves, by hand and on a throwaway stack, that Neo4j accepts a Cognito token from the Hume test pool. Only after that proof does it make the setting a permanent, optional template feature. Hume forwarding each user's token to Neo4j comes last. That step is blocked on an answer from GraphAware.

The Hume side already works without any of this. On 2026-10-27, `hume-admin` signed in to Hume through Cognito and got the Hume ADMINISTRATOR role. Hume reaches Neo4j with the `spine-neo4j-sa` service account over password login. That setup is called model A below, and it does not depend on this plan.

## Proposal

**Removed:**
- Nothing.

**Fixed:**
- Neo4j EE stacks accept password login only. They can then accept password login and Cognito tokens side by side.

**Added:**
- A manual, reversible proof that Neo4j accepts a Cognito ID token and maps groups to roles. This is Phase 1.
- Optional OIDC stack parameters that are blank and off by default. This is Phase 2.
- An optional Neo4j Browser single sign-on setup. This is Phase 3.
- Hume forwarding each user's token to Neo4j, called model B. This is Phase 4, and it is blocked.

### Deliberately not doing

- **Machine-to-machine OIDC into Neo4j.** Cognito client-credentials tokens are access tokens with no `aud` claim. Neo4j checks the audience, so those tokens cannot pass. Service accounts stay on password login.
- **Turning off password login.** The admin password, `preflight`, `validate-private` and Hume's service account all depend on it. Password login stays on in every phase.
- **Changing `test-ee-1792647880` in Phase 1 or Phase 2**, unless Ryan chooses that option below. The Hume demo depends on that stack.
- **Changing the AMI.** OIDC settings depend on stack inputs. Under the architecture's Placement Decision Rule they belong in a runtime overlay function and never in the AMI.
- **Changing the Marketplace defaults.** The feature stays off unless the new parameters are set.
- **Supporting the GraphAware SSO plugin realm.** This plan covers only Neo4j's built-in OIDC.

### Decisions

1. **Phase 1 validates the ID token, not the access token.** Cognito access tokens carry `client_id` and no `aud`. Neo4j's audience check needs `aud`, and only the Cognito ID token has it. The Hume session confirmed these claims on the live pool. Dropped: access-token validation.
2. **Phase 1 reuses the `hume-web` app client.** Its ID tokens already carry `aud` set to that client. It also already allows password sign-in from a script, so Phase 1 needs no Cognito change. Model B would forward tokens issued to `hume-web` too, so Neo4j will have to accept that audience in any case. Dropped for Phase 1: a separate Neo4j client. Phase 3 revisits this question.
3. **Phase 1 maps the existing `hume-admins` group to the Neo4j `admin` role, and maps nothing else.** This needs no change to the pool. It shows the difference between `hume-admin` and the ungrouped `hume-user`. Phase 2 moves to dedicated Neo4j groups so that being a Hume admin does not by itself make someone a Neo4j admin.
4. **Phase 1 changes the config by hand over SSM Run Command, not through the template.** The goal is to prove the Neo4j settings before anyone writes template code. Editing `/etc/neo4j/neo4j.conf` can be undone in two ways: remove the keys and restart, or replace the instance. That file is on the root volume, so a replacement instance boots with the template's clean config. `docs/PRIVATE.md` already uses the same kind of SSM edit for password recovery.
5. **Phase 2 passes the OIDC values to the instance encoded, not as plain `name=value` UserData lines.** Today the UserData preamble writes unquoted shell assignments. A group mapping contains `;` and `"`, which the shell would read as syntax. The exact encoding is an implementation choice. The required outcome is that no parameter value can be run as shell code.

### Where to look

- Architecture contract and Placement Decision Rule: `docs/architecture/template-architecture.md`.
- Runtime overlay functions: `templates/src/partials/`. The TLS overlay `configure-tls.sh` is the closest model to follow.
- Order of calls in the bootstrap: `templates/src/bootstrap/neo4j-bootstrap.sh`.
- Shell assignments in the UserData preamble: `templates/build.py`.
- Value export and the single `cfn-signal` owner: `templates/src/userdata.sh`.
- Operator tooling, tunnels and checks: `validate-private/`.
- Cognito pool harness and run book: `hume-aws-cloudformation/stack/cognito.py` and `docs/oidc-setup.md` in the Hume repo.

## Inputs from the Hume session

- Issuer: `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_KOZWGYuCP`. The pool lives outside both stacks, and `cognito.py teardown` deletes it.
- App client `hume-web` is `7ageo3egfachcshnjl2pmca4im`. It is a public client with no secret, uses the code flow, and allows password sign-in from a script.
- Users: `hume-admin` is in group `hume-admins`. `hume-user` is in no group.
- User passwords are in Secrets Manager at `hume/hume-ecs-1792649200/cognito/<user>`. Scripts read them through the API and never print them.

## Answers to the Hume session's questions

**1. What is the minimal Phase 1?** Your guess is right, with two notes. First, Neo4j must also read the username from the ID token, not only check the token. Second, the test should connect straight to one server through the Bolt tunnel. A routing `neo4j://` connection would fail from a laptop, because the cluster advertises the internal NLB name, and that name only resolves inside the VPC.

**2. Where does Phase 1 run?** Ryan decides. The trade-off:

| | New 1-node test stack (recommended) | `test-ee-1792647880` |
|---|---|---|
| Risk to Hume demo | None | A config typo stops Neo4j from starting. Neo4j 5 rejects invalid config. Hume loses Neo4j until the change is reverted. |
| Extra cost | One small Private stack for a few hours. See Cost and teardown. | None |
| Restarts | One node | A rolling restart of three nodes, waiting for each to rejoin |
| What it proves | Neo4j accepts Cognito tokens next to password login | The same, plus that `spine-neo4j-sa` keeps working on the live demo |
| Undo | Delete the stack | Remove the keys and restart each node, or replace each instance |

The extra proof from the live stack is small. Phase 1 on a new stack already shows that password login keeps working, because `preflight` and `validate-private` use it.

**3. Clients: reuse `hume-web` or a new client?** Phase 1 reuses `hume-web` (Decision 2). Neo4j Browser login in Phase 3 needs its own redirect URI. The Browser redirect has the form `https://<host>:7473/browser/?idp_id=<provider name>&auth_flow_step=redirect_uri`. In a Private stack a person's browser reaches Neo4j through the SSM tunnel, so the host is `localhost`. Cognito allows `localhost` callback URLs. Adding that redirect to `hume-web` would mix Neo4j's login setup into Hume's client. Phase 3 should therefore add a separate public client for Neo4j Browser. Whether Neo4j accepts more than one audience value is not yet verified. Phase 1 checks it, because the answer decides whether Hume's client and Neo4j's own client can both be accepted at once.

**4. Group-to-role mapping and roles.**
- **Phase 1:** `hume-admins` maps to the built-in `admin` role, and nothing else is mapped. `hume-user` is the negative case.
- **Phase 2 and later:** Use dedicated Cognito groups, for example a Neo4j admins group mapped to `admin` and a Neo4j readers group mapped to the built-in `reader` role. Create custom Neo4j roles only when a group needs grants that no built-in role covers. Those roles are created once through Cypher and stored in the system database. The system database lives on the retained data volume, so the roles survive instance replacement.

**5. How does the config reach every node, and is it reversible?**
- **Phase 1:** An SSM Run Command edits `neo4j.conf` and restarts Neo4j on the one node. It is reversible (Decision 4). It does not survive instance replacement, which is the right property for a test.
- **Phase 2:** The OIDC values become optional stack parameters. A runtime overlay function in the bootstrap applies them on every boot of a new instance, so ASG self-heal keeps them. A stack update that changes them replaces the instances the normal way. Clearing the parameters turns the feature off on the next replacement.

**6. Token lifetime.** Cognito ID tokens last one hour by default. The pool can set them between five minutes and one day. The Phase 1 script runs for seconds, so expiry does not matter there. Anything that holds a Neo4j connection on a user's token must refresh it, and model B is the main case. That means Hume must use the refresh token and give the driver the new ID token before the old one expires. The Neo4j 5 drivers have a token-manager hook for this. How the server treats an open session whose token expires is not yet verified. Phase 4 must test it.

**7. Later phases and the GraphAware dependency.** See Phase 2 to Phase 4 below. Phase 4 is blocked on GraphAware's answer.

**8. Cost and teardown.** See the last section.

## Goal

Prove that Neo4j EE accepts Cognito ID tokens next to password login. Then make that an optional, tested template feature, without risk to the Hume demo or the Marketplace defaults.

## Assumptions

- The Cognito pool and the `hume-web` client above stay in place until Phase 1 is done.
- Private stacks give the Neo4j nodes outbound HTTPS through NAT, which lets them reach the Cognito discovery and key endpoints. The private networking template routes each private subnet through a NAT gateway. A live check is still needed.
- The Neo4j version that `deploy.py` installs supports OIDC with the PKCE flow and ID-token validation. Neo4j 5 does.

## Risks

- **Strict config validation.** Neo4j 5 refuses to start on a bad or duplicate config key. In Phase 1 that means a failed restart. In Phase 2 a bad parameter value would fail every new instance, and ASG self-heal would retry in a loop. Mitigation: a template rule checks the parameters, and partial unit tests cover them.
- **Cypher IP blocklist.** Hypothesis: `internal.dbms.cypher_ip_blocklist` only limits Cypher-started outbound calls and does not block Neo4j's own fetch of Cognito's signing keys. Phase 1 confirms this. Cognito endpoints are public addresses outside the blocked ranges in any case.
- **Ungrouped users.** Hypothesis: a token with no mapped group signs in with only the `PUBLIC` role and cannot read graph data. Neo4j might instead reject the login outright. Phase 1 records which of the two happens.
- **Model B design.** Model B may not apply if GraphAware's Neo4jSSO resource can only use their own plugin realm.

## Phase checklist

### Phase 0: Decisions

Status: Complete (2026-10-27)

- [x] Ryan chooses where Phase 1 runs: a new 1-node stack or `test-ee-1792647880`. Decision: a new 1-node stack.
- [x] Ryan approves reusing `hume-web` and the `hume-admins` group for Phase 1.
- [x] Ryan approves the Phase 1 cost. Ryan also approved starting, stopping and tearing down the Phase 1 stack.

Validation: The decisions are recorded in this file.

### Phase 1: Manual proof on one node

Status: Complete (2026-10-27).

Outcome: A script signs in to Cognito as `hume-admin` and connects to Neo4j with the ID token as a bearer token. It gets the `admin` role. `hume-user` gets less, and password login still works.

- [x] Deploy the Phase 1 stack, or confirm the chosen existing stack is healthy with `preflight`. Stack `test-ee-1793131395`: 1 node, Private, no Bloom or GDS. `preflight` passed 12 of 12.
- [x] Confirm the Neo4j node can reach `cognito-idp.us-east-1.amazonaws.com` over 443. The discovery document returned HTTP 200 from the node through NAT.
- [x] Add an OIDC provider for Cognito with SSM Run Command. Password login stays on. The provider checks the ID token, uses `hume-web` as the audience, reads the username and the `cognito:groups` claim, and maps `hume-admins` to `admin`. Keys are replaced, never duplicated.
- [x] Restart Neo4j and confirm in `debug.log` that the provider loaded.
- [x] Write a throwaway script that gets an ID token through password sign-in and connects through the Bolt tunnel with the Python driver's bearer auth. Passwords and tokens are never printed.
- [x] As `hume-admin`: Neo4j reports the user's roles as including `admin`, and an admin-only command succeeds.
- [x] As `hume-user`: record whether login fails or succeeds with only `PUBLIC`. Either way the user cannot read graph data.
- [ ] Send an access token, an expired or altered token, and a token for a different audience. All are rejected. Partly done: the access token and an altered token were rejected. An expired token and a token for a different audience were not tested, because no second client or old token was at hand.
- [x] Test whether the audience setting accepts two values.
- [x] `preflight` and `validate-private` still pass over password login.
- [x] Revert the change and confirm Neo4j comes back with password login only.
- [x] If the stack was a throwaway, tear it down. Done 2026-10-27: Ryan chose a fresh pool and a new stack for the Hume rebuild. `test-ee-1793131395` and its data volume are deleted.

Validation: Record the script output, with secrets redacted, and the results of each check in this file's Notes.

Notes:

- `hume-admin` ID token: authenticated as `hume-admin` with roles `admin` and `PUBLIC`. It could read the seeded probe node, write, and run `SHOW USERS`.
- `hume-user` ID token: authenticated as `hume-user` with role `PUBLIC` only. The hypothesis held: login succeeds and the user has no data access. The probe count was 0 for this user and 1 for the admin. Writes and `SHOW USERS` failed with `Neo.ClientError.Security.Forbidden`.
- A missing read privilege hides nodes and raises no error. A read check must seed data first. A count on an empty graph proves nothing.
- `hume-admin` access token: rejected with `Neo.ClientError.Security.Unauthorized`. This confirms Decision 1.
- Altered ID token signature: rejected with `Neo.ClientError.Security.Unauthorized`.
- Two audience values: the audience setting accepts a comma-separated list. With a dummy client first and `hume-web` second, `hume-admin` still authenticated as `admin`. Phase 3 can therefore accept both Hume's client and a separate Neo4j Browser client.
- Cypher IP blocklist: the hypothesis held. The blocklist stayed in place and Neo4j still fetched Cognito's discovery and signing keys.
- Username claim: Phase 1 used `cognito:username`, because the test users' ID tokens carry no `email`.
- `validate-private`: 16 of 16 passed over password login while OIDC was on.
- After revert: no `dbms.security.oidc` keys and no provider-list key remain. A bearer token is rejected with "Unsupported authentication token".
- Unrelated finding: `/etc/neo4j/neo4j.conf` has 29 identical `server.jvm.additional=-XX:+ExitOnOutOfMemoryError` lines. The pre-OIDC backup has the same 29, so the bootstrap produced them, not this test. This is not fixed here. It needs a separate look.
- Phase 1 script: `oidc_phase1.py` in the session scratchpad. It is throwaway. Phase 2 should rebuild the token test as a `validate-private` script.

### Phase 2: Optional template parameters

Status: Pending. This phase needs a passing Phase 1.

Outcome: All three EE templates accept optional OIDC parameters. When the parameters are blank, the rendered template and the node config are unchanged. When they are set, every node, including ASG replacements, boots with Cognito OIDC next to password login.

- [ ] Add a parameter partial to all three templates: discovery URI, audience, username claim, groups claim and group-to-role mapping. All default to blank.
- [ ] Add a template rule: setting the discovery URI requires an audience. Add the parameters to the console parameter groups.
- [ ] Pass the values to the instance so that no value can run as shell code (Decision 5).
- [ ] Add a runtime overlay function in a new partial. It does nothing when OIDC is off. It keeps password login in the provider list and runs before the security invariant check.
- [ ] Unit tests: OIDC off sets no keys, OIDC on sets the expected keys, and password login is always in the provider list.
- [ ] A rendered-template contract test confirms the parameters exist in all three templates. `build.py --verify` passes.
- [ ] Add `deploy.py` flags for the new parameters.
- [ ] Add an optional `validate-private` check for the provider list when OIDC is on.
- [ ] Update `docs/PRIVATE.md` and the overlay examples in the architecture doc.
- [ ] On a new 3-node stack: the Phase 1 script passes against each node. A routing connection run from the bastion passes. One node is terminated, and its replacement still accepts tokens.
- [ ] Tear down the Phase 2 stack.

Validation: Unit tests, `cfn-lint` on all three templates, `build.py --verify`, and the 3-node test above.

### Phase 3: Neo4j Browser single sign-on (optional)

Status: Pending. This phase needs Phase 2 and Ryan's go-ahead.

Outcome: A person can open Neo4j Browser through the SSM tunnel, choose Cognito, sign in and get a role from their group.

- [ ] Add a separate public Cognito client for Neo4j Browser, with the `localhost` tunnel redirect URI.
- [ ] Make Neo4j accept both audiences, if the Phase 1 test showed this works. If not, record the limit and choose one.
- [ ] Confirm that the Browser can load the Cognito discovery document. Browser-side cross-origin rules could block it. This is not verified yet.
- [ ] Document the steps in `docs/PRIVATE.md`.

Validation: A manual Browser sign-in as `hume-admin` and as `hume-user`.

### Phase 4: Hume forwards user tokens (model B)

Status: **Blocked on GraphAware.** The open question to GraphAware: does a `Hume.Orchestra.Resource.Neo4jSSO` resource pass the user's token to Neo4j, and can it use Neo4j's built-in OIDC instead of the `plugin-graphaware-keycloak-sso` realm?

Outcome: Hume opens Neo4j sessions on each user's own ID token, so Neo4j roles apply per user. Background work stays on `spine-neo4j-sa`.

- [ ] Get GraphAware's answer, and rewrite this phase to match it.
- [ ] Hume refreshes ID tokens before they expire. Test what happens to an open session when its token expires.
- [ ] Confirm that Neo4j query logs show the Cognito user, not the service account, for user queries.

Validation: To be defined after GraphAware answers.

## Cost and teardown

All costs below are estimates. They have not been checked against live pricing.

| Phase | What runs | Rough cost | Teardown |
|---|---|---|---|
| 1, new stack | 1-node Private stack: one t3.medium, one NAT gateway, the NLB, the SSM bastion, interface VPC endpoints and EBS | A few dollars per day. Plan for a few hours. | `./teardown.sh <stack> --delete-volumes`. Data volumes are retained by default, so the flag is needed. |
| 1, existing stack | Nothing new | None | Remove the keys and restart each node, or replace each instance. |
| 2 | 3-node Private stack: three NAT gateways and three nodes | About three times Phase 1, for a few hours | Same as Phase 1, new stack |
| 3 | One more Cognito app client | No charge for the client | Delete the client, or `cognito.py teardown` for the whole pool |
| 4 | Changes to the Hume stack | To be estimated when unblocked | Hume stack teardown |

Cognito sign-ins in all phases are a handful of monthly active users. Treat that as negligible, but it has not been checked against the pool's pricing tier.

## Completion criteria

- [ ] Phase 1 shows that a Cognito ID token authenticates, maps a group to a Neo4j role, and leaves password login working.
- [ ] Phase 2 ships optional parameters that leave blank-parameter templates and node config unchanged. Tests enforce this.
- [ ] Phase 3 is done or explicitly dropped.
- [ ] Phase 4 is rewritten after GraphAware answers, or dropped.
- [ ] Every test stack is torn down, including its retained volumes.
