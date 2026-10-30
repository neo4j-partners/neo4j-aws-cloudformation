# OIDC Single Sign-On Run Book (Private Stack)

This run book covers signing in to Neo4j Browser with single sign-on on a Private stack. It starts from deploying the stack and ends with tear down. The examples use the Amazon Cognito test pool `hume-test-idp`, but the steps are the same for any OpenID Connect provider.

Password login always stays enabled next to SSO. Operator tools such as `validate-private` keep using the native `neo4j` password.

## 1. Prerequisites

On your laptop:

- AWS CLI v2 and the Session Manager plugin.
- `uv`.
- AWS credentials for the stack's account. The examples use `AWS_PROFILE=marketplace` in `us-east-1`.

In the identity provider:

- An issuer URL. For the test pool it is `https://cognito-idp.us-east-1.amazonaws.com/us-east-1_cFcevWce0`.
- One or more app clients. The test pool has two:

| Client | ID | Used by |
|---|---|---|
| neo4j | `6sqtujmk6rqlhlqhdvkp0777pf` | Neo4j Browser and Bloom |
| hume-web | `io6nr2172kegbomo7ek5aisdc` | Hume |

- A group whose members get the Neo4j admin role. The test pool uses `neo4j-admins`.

## 2. Deploy the stack with OIDC

Run from `neo4j-ee/`:

```bash
AWS_PROFILE=marketplace ./deploy.py --region us-east-1 \
  --expected-account 385155106615 \
  --number-of-servers 3 --no-bloom --no-gds \
  --advertised-dns neo4j-oidc.neo4j.local \
  --oidc-issuer-uri https://cognito-idp.us-east-1.amazonaws.com/us-east-1_cFcevWce0 \
  --oidc-client-id io6nr2172kegbomo7ek5aisdc,6sqtujmk6rqlhlqhdvkp0777pf \
  --oidc-browser-client-id 6sqtujmk6rqlhlqhdvkp0777pf \
  --oidc-principal-claim cognito:username \
  --oidc-groups-claim cognito:groups \
  --oidc-admin-group neo4j-admins
```

What each OIDC flag does:

| Flag | Template parameter | Purpose |
|---|---|---|
| `--oidc-issuer-uri` | `OidcIssuerUri` | Turns SSO on. Neo4j reads `<issuer>/.well-known/openid-configuration`. |
| `--oidc-client-id` | `OidcClientId` | Client IDs accepted as the ID token audience. A token from any listed client is accepted. |
| `--oidc-browser-client-id` | `OidcBrowserClientId` | The client Browser and Bloom sign in with. It must be in `--oidc-client-id`. If omitted, the first ID in `--oidc-client-id` is used, so the order of that list matters only when this flag is omitted. |
| `--oidc-principal-claim` | `OidcPrincipalClaim` | The ID token claim that becomes the Neo4j username. The default is `sub`. |
| `--oidc-groups-claim` | `OidcGroupsClaim` | The ID token claim that lists the user's groups. |
| `--oidc-admin-group` | `OidcAdminGroup` | The group mapped to the Neo4j `admin` role. |
| `--oidc-reader-group` | `OidcReaderGroup` | The group mapped to the built-in `reader` role. |

`--advertised-dns` sets the name that Browser, the drivers and the redirect URLs use. The rest of this run book uses `neo4j-oidc.neo4j.local`.

The deploy writes `.deploy/<stack-name>.txt`. The tunnel scripts read the bastion ID and region from it.

### Changing OIDC settings on a running stack

A stack update changes the launch template, but running nodes keep their old config. The node ASGs have no update policy. To apply the change, replace the nodes one at a time. Terminate one node through its ASG without lowering the desired capacity. Wait until its replacement is InService and healthy in every NLB target group, then move to the next node. Each replacement takes about 8 minutes, and the data volume carries over.

## 3. Register the redirect URLs in the identity provider

The identity provider must allow Browser's redirect URL for the Browser client. Cognito matches the URL exactly, including the query string. Register these callback URLs on the Browser client:

```
https://neo4j-oidc.neo4j.local:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri
https://neo4j-oidc.neo4j.local:7473/browser/
https://neo4j-oidc.neo4j.local:7473/bloom/
```

`sso` in the first URL is the provider ID the stack configures. For the test pool these are already registered on the neo4j client. The Hume project manages that pool, so coordinate changes to it with that project.

### What the Browser client needs

The Browser client must have these settings in addition to the callback URLs:

| Setting | Value | Why |
|---|---|---|
| Client secret | None | Browser is a public client and uses PKCE. It cannot keep a secret. |
| Allowed OAuth flows | Authorization code grant | Browser uses the code flow with PKCE. |
| Allowed OAuth scopes | `openid`, `profile`, `email` | Browser requests these scopes. |
| Identity providers | Cognito user pool | Users sign in with pool accounts. |

The user pool also needs a domain for its hosted login page. The test pool uses `https://hume-test-idp.auth.us-east-1.amazoncognito.com`.

### Option A: AWS console

1. Open **Amazon Cognito**, then **User pools**, then the pool.
2. Open **App clients** and choose the Browser client.
3. Open the **Login pages** tab and choose **Edit** under **Managed login pages configuration**.
4. Under **Allowed callback URLs**, add each URL from the list above.
5. Choose **Save changes**.

### Option B: AWS CLI

`update-user-pool-client` resets every setting you leave out to its default. Do not call it with only `--callback-urls`, or you will wipe the client's scopes, flows and other settings. Instead, read the full client, add the URLs, and write the full client back:

```bash
export AWS_PROFILE=marketplace AWS_REGION=us-east-1
POOL_ID=us-east-1_cFcevWce0
CLIENT_ID=6sqtujmk6rqlhlqhdvkp0777pf
HOST=neo4j-oidc.neo4j.local

aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --query UserPoolClient --output json \
| jq --arg h "$HOST" '
    .CallbackURLs = ((.CallbackURLs // []) + [
      "https://\($h):7473/browser/?idp_id=sso&auth_flow_step=redirect_uri",
      "https://\($h):7473/browser/",
      "https://\($h):7473/bloom/"
    ] | unique)
    | del(.CreationDate, .LastModifiedDate, .ClientSecret)' \
> /tmp/browser-client.json

# Review the file before applying it.
jq .CallbackURLs /tmp/browser-client.json

aws cognito-idp update-user-pool-client --cli-input-json file:///tmp/browser-client.json
rm /tmp/browser-client.json
```

The `jq` step keeps any URLs already registered and removes the read-only fields that the update call rejects.

### Check the registration

Send Cognito the same authorize request Browser makes. Replace the domain with your pool's login domain:

```bash
curl -s -o /dev/null -w '%{redirect_url}\n' -G \
  https://hume-test-idp.auth.us-east-1.amazoncognito.com/oauth2/authorize \
  --data-urlencode client_id="$CLIENT_ID" \
  --data-urlencode response_type=code \
  --data-urlencode 'scope=openid profile email' \
  --data-urlencode "redirect_uri=https://$HOST:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri" \
  --data-urlencode code_challenge_method=S256 \
  --data-urlencode "code_challenge=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
```

A redirect to `.../login` means the URL is registered. A redirect to `.../error?error=redirect_mismatch` means it is not, or that it is registered on a different client than `OidcBrowserClientId`.

## 4. Point the advertised name at your laptop

The advertised name only resolves inside the VPC. Map it to localhost so the browser reaches the tunnels:

```bash
echo "127.0.0.1 neo4j-oidc.neo4j.local" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

Use the advertised name in the browser, not `localhost`. Browser builds the redirect URL from the address bar, and the identity provider only accepts the registered name.

The first lookup can take a few seconds, because macOS tries mDNS for `.local` names first.

## 5. Open the tunnels

Browser needs two tunnels through the SSM bastion. One serves the web page and SSO discovery on 7473, and the other carries Bolt on 7687. Run each in its own terminal from `neo4j-ee/` and leave both open:

```bash
# Terminal 1: Browser and SSO discovery
./browser-tunnel.sh <stack-name>

# Terminal 2: Bolt
./bolt-tunnel.sh <stack-name>
```

If you omit the stack name, each script uses the most recent deployment in `.deploy/`. Both scripts default `AWS_PROFILE` to `marketplace`. Set `AWS_PROFILE` to use another profile.

Each tunnel is ready when it prints `Waiting for connections...`.

SSM closes a tunnel after a period of inactivity, with the message "Your session timed out due to inactivity". Restart it with the same command.

## 6. Sign in to Neo4j Browser

1. Open `https://neo4j-oidc.neo4j.local:7473/browser/`.
2. The stack's certificate is self-signed, so Chrome shows a security warning. Click **Advanced**, then **Proceed**.
3. Set the Connect URL to `neo4j+s://neo4j-oidc.neo4j.local:7687`. The cluster serves Bolt over TLS.
4. Choose **SSO** and sign in on the identity provider's page.
5. Browser returns to `/browser/` and connects.

### Test user passwords

The test pool's passwords are in Secrets Manager under `hume/hume-test-idp/cognito/<user>`. Each is stored as a plain string. This copies one to the clipboard without showing it:

```bash
AWS_PROFILE=marketplace aws secretsmanager get-secret-value --region us-east-1 \
  --secret-id hume/hume-test-idp/cognito/neo4j-admin \
  --query SecretString --output text | pbcopy
```

### Expected roles

Run `SHOW CURRENT USER` in Browser to check the roles.

| User | Group | Neo4j roles | What the user can do |
|---|---|---|---|
| `neo4j-admin` | `neo4j-admins` | `admin`, `PUBLIC` | Read, write and administer. |
| `hume-admin` | `hume-admins` | `PUBLIC` | Sign in only. The graph looks empty, and writes and `SHOW USERS` are forbidden. |
| `hume-user` | none | `PUBLIC` | Same as `hume-admin`. |

`hume-admins` is not mapped to a Neo4j role on purpose. Hume admins are not Neo4j admins.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `DNS_PROBE_POSSIBLE` or "This site can't be reached" | The advertised name is missing from `/etc/hosts`. | Run step 4. |
| The page does not load | The Browser tunnel on 7473 is closed. | Restart `./browser-tunnel.sh`. |
| SSO is disabled in the login form, or the connection fails | The Bolt tunnel on 7687 is closed. | Restart `./bolt-tunnel.sh` and reload the page. |
| Cognito shows `redirect_mismatch` | Browser's redirect URL is not registered on the Browser client. | Register the URLs in step 3. Check `OidcBrowserClientId` names the client that has them. |
| A user who should be admin gets only `PUBLIC` | The user is not in the admin group, or the groups claim or group name is wrong. | Check the user's groups in the identity provider and the `Oidc*` parameters on the stack. |
| A stack update to OIDC settings has no effect | Running nodes keep their old config. | Replace the nodes one at a time, as described in section 2. |

To see the OIDC settings a node is running with, check `/etc/neo4j/neo4j.conf` on the node through SSM. Look for the `dbms.security.oidc.sso.*` lines. `/var/log/cloud-init-output.log` logs `OIDC enabled` or `OIDC disabled` at boot.

## 8. Clean up

Remove the hosts entry when you are done:

```bash
sudo sed -i '' '/neo4j-oidc\.neo4j\.local/d' /etc/hosts
```

Tear down the stack from `neo4j-ee/`. The EBS data volumes are retained unless you pass `--delete-volumes`:

```bash
AWS_PROFILE=marketplace ./teardown.sh --delete-volumes --expected-account 385155106615 <stack-name>
```
