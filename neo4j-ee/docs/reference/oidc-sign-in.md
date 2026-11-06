# Neo4j EE: Single Sign-On (OIDC) Reference

Neo4j Browser and Bloom can sign users in through an OpenID Connect identity provider. Setting `OidcIssuerUri` turns this on. Password login stays enabled next to it, and operator tools keep using the native `neo4j` password.

The step-by-step procedure, including the Hume integration, is in [OIDC Run Book](../run-books/oidc.md).

## Sign-In Settings

| Template parameter | `deploy.py` flag | `.env` key | Purpose |
|---|---|---|---|
| `OidcIssuerUri` | `--oidc-issuer-uri` | `OIDC_ISSUER_URI` | Turns single sign-on on. Neo4j reads `<issuer>/.well-known/openid-configuration`. |
| `EnableInternetEgress` | `--enable-internet-egress` | `ENABLE_INTERNET_EGRESS` | Creates the NAT gateways. A Private stack requires `true` when `OidcIssuerUri` is set. |
| `OidcClientId` | `--oidc-client-id` | `OIDC_CLIENT_ID` | Comma-separated client IDs accepted as the token audience. A token from any listed client is accepted. |
| `OidcBrowserClientId` | `--oidc-browser-client-id` | `OIDC_BROWSER_CLIENT_ID` | The client that Browser and Bloom sign in with. It must be in `OidcClientId`. When it is empty, the first ID in `OidcClientId` is used. |
| `OidcPrincipalClaim` | `--oidc-principal-claim` | `OIDC_PRINCIPAL_CLAIM` | The token claim that becomes the Neo4j username. Every token that clients present must carry it. The default is `sub`. |
| `OidcGroupsClaim` | `--oidc-groups-claim` | `OIDC_GROUPS_CLAIM` | The token claim that lists the user's groups. |
| `OidcAdminGroup` | `--oidc-admin-group` | `OIDC_ADMIN_GROUP` | The group mapped to the Neo4j `admin` role. |
| `OidcReaderGroup` | `--oidc-reader-group` | `OIDC_READER_GROUP` | The group mapped to the built-in `reader` role. |
| `OidcGroupRoleMapping` | `--oidc-group-role-mapping` | `OIDC_GROUP_ROLE_MAPPING` | Additional mappings for any other role, in the form `group=role1,role2;group2=role3`. The roles must exist in Neo4j, because the mapping does not create them. |
| `AdvertisedDNS` | `--advertised-dns` | `ADVERTISED_DNS` | The name that Browser, the drivers and the redirect addresses use. |
| `CreatePrivateDns` | `--create-private-dns` | `CREATE_PRIVATE_DNS` | Creates a Route 53 private record for the advertised name. |
| `PrivateDnsZoneName` | `--private-dns-zone` | `PRIVATE_DNS_ZONE` | The private hosted zone to create. |

A user in no mapped group signs in with only the `PUBLIC` role. A token with no groups claim gets the same result.

## Token Design

Clients present different tokens to the same provider:

| Client | Token | Issued to |
|---|---|---|
| Neo4j Browser and Bloom | ID token | `OidcBrowserClientId` |
| An application that forwards a user's sign-in over Bolt, such as Hume | Access token | Another client in `OidcClientId` |

Every token must meet three requirements:

- **Audience:** The `aud` claim must match an ID in `OidcClientId`.
- **Username:** The token must carry `OidcPrincipalClaim`. Neo4j rejects a token without it, with `JWT user claim must be a string` in `security.log`.
- **Groups:** The token must carry `OidcGroupsClaim` for its group mappings to apply.

The template sets `token_type_principal` and `token_type_authentication` to `id_token`, and no parameter changes them. Neo4j documents the setting as client configuration, and Browser and Bloom sign in with the ID token. The audience stays a list of client IDs because an ID token's audience is always the client. An access token's audience is usually an API. Okta uses `api://default`, and Entra ID needs a custom scope.

The `id_token` setting does not block access tokens. A Bolt client can present either token, and a token that meets the three requirements signs in. An application can send the token with the bearer scheme or the basic scheme. Hume uses basic, with the fixed username `__keycloak_token` and the token as the password. Neo4j rejects a basic sign-in that also names a realm, so leave the realm unset.

Neo4j does not document this behaviour. These results come from a test on Neo4j 2026.10.1 with Cognito. Both tokens gave the same `sub` and the same roles under both schemes. Repeat the test before relying on it with another identity provider.

### Amazon Cognito

Set the principal claim to `sub` and the groups claim to `cognito:groups`. Both claims are in the ID token and the access token. Cognito puts `username` only in the access token and `cognito:username` only in the ID token, so neither works when clients present both tokens.

A Cognito access token has no `aud` claim by default. A pre token generation trigger on the V2_0 event can add it, and the value must equal the session's client ID. The trigger needs the Essentials or Plus feature plan. The stack creates no trigger. The identity provider's owner adds it.

### Why `sub`

Neo4j recommends `sub` because it is the only claim guaranteed to be unique and stable. The Neo4j username is then an opaque ID in `SHOW CURRENT USER` and the audit logs, and it changes if the user pool is recreated. Grant privileges through group mappings, not to a single username.

## Group Mapping

`OidcAdminGroup`, `OidcReaderGroup` and `OidcGroupRoleMapping` merge into one `group_to_role_mapping` setting, in that order. Each group name is quoted in `neo4j.conf`:

```
dbms.security.oidc.sso.authorization.group_to_role_mapping="neo4j-admins"=admin;"spine-analysts"=spine_analyst
```

An explicit mapping turns off Neo4j's automatic match between group and role names. List every group that needs a role, even when its name matches the role. Any of the three parameters requires `OidcGroupsClaim`.

The mapping does not create the roles. A user mapped to a missing role still signs in, and `SHOW CURRENT USER` lists the role name. That role grants no privileges until you create it with `CREATE ROLE`. A sign-in after that gets the role's privileges.

## Network Access

The nodes fetch the provider's signing keys over the internet. A Private stack reaches the internet through the NAT gateways, which exist only when `EnableInternetEgress` is `true`. The stack rejects `OidcIssuerUri` without it, and so does `deploy.py`. An ExistingVpc stack must provide that route itself. The node security group allows outbound port 443 to the internet only when `OidcIssuerUri` is set.

## Redirect Addresses

The identity provider must allow these redirect addresses for the Browser client. Replace `<AdvertisedDNS>` with the stack's advertised name:

```
https://<AdvertisedDNS>:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri
https://<AdvertisedDNS>:7473/browser/
https://<AdvertisedDNS>:7473/bloom/
```

`sso` is the provider ID the stack configures. Skip the `bloom` address when Bloom is not installed. Amazon Cognito matches each address exactly, including the query string.

## Changing Sign-In Settings on a Running Stack

A stack update changes the launch template, but running nodes keep their old configuration. The node Auto Scaling groups have no update policy. To apply a change, replace the nodes one at a time:

1. Terminate one node through its Auto Scaling group. Do not lower the desired capacity.
2. Wait until the replacement is InService and healthy in every NLB target group.
3. Move to the next node.

Each replacement takes about 8 minutes. The data volume moves to the replacement.

## Checking a Node

Open a session on a node through SSM and look for the `dbms.security.oidc.sso.*` lines in `/etc/neo4j/neo4j.conf`. At boot, `/var/log/cloud-init-output.log` logs `OIDC enabled` or `OIDC disabled`.

## Troubleshooting Sign-In

| Symptom | Cause | Fix |
|---|---|---|
| `DNS_PROBE_POSSIBLE` or "This site can't be reached" | The advertised name is missing from `/etc/hosts`. | Add `127.0.0.1 <AdvertisedDNS>` to `/etc/hosts`. |
| The page does not load | The Browser tunnel on 7473 is closed. | Restart `./browser-tunnel.sh`. |
| SSO is disabled in the login form, or the connection fails | The Bolt tunnel on 7687 is closed. | Restart `./bolt-tunnel.sh` and reload the page. |
| The provider shows `redirect_mismatch` | The redirect address is not registered on the Browser client. | Register the [redirect addresses](#redirect-addresses). Check that `OidcBrowserClientId` names the client that has them. If it still fails, URL-decode `redirect_uri` from the address bar and register it exactly as shown. |
| A user who should be admin gets only `PUBLIC` | The user is not in the admin group, or the groups claim or group name is wrong. | Check the user's groups in the identity provider and the `OIDC_*` values. |
| A token is rejected, and `security.log` shows `JWT user claim must be a string` | The token does not carry `OidcPrincipalClaim`. | Use a claim that is in every token clients present. See [Token Design](#token-design). |
| A client gets `Unauthorized`, and `security.log` shows `[__keycloak_token]: failed to log in: invalid principal or credentials` | The log gives no reason. A realm in the basic sign-in produces this line. | Leave the realm unset. In Hume, leave `hume.driver.neo4j.security.realm` unset. |
| A query fails with `ACCESS on database '<name>' is not allowed` for a user with roles `[PUBLIC]` | The user has no mapped group, or the token has no groups claim. | Add the user to a mapped group, or map the user's group. See [Group Mapping](#group-mapping). |
| A stack update to sign-in settings has no effect | Running nodes keep their old configuration. | [Replace the nodes one at a time](#changing-sign-in-settings-on-a-running-stack). |
| A client inside the VPC cannot resolve the advertised name | The private DNS record is missing. | Deploy with `CREATE_PRIVATE_DNS=true` and a `PRIVATE_DNS_ZONE`. |
| The name does not resolve for about 7 minutes after the record is created | Route 53 holds a negative cache entry. | Wait and retry. |
| SSM closes a tunnel with "Your session timed out due to inactivity" | The tunnel was idle. | Restart it with the same command. |
