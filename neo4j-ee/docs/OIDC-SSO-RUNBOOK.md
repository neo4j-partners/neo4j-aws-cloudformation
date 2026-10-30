# Neo4j EE with OIDC and Hume: End-to-End Run Book

This run book takes you from nothing to a Private Neo4j Enterprise Edition stack with single sign-on, and then to a Hume deployment that uses that stack. It is the single source for the Neo4j and OIDC steps. The Hume-only steps live in `docs/run-book.md` in the `hume-aws-cloudformation` repository, and this run book points to them.

The examples use an Amazon Cognito pool created by the Hume repository. The Neo4j steps are the same for any OpenID Connect provider. Skip section 2 and write the `.env` in section 3 by hand.

Password login always stays enabled next to SSO. Operator tools such as `validate-private` keep using the native `neo4j` password.

## 1. Order of operations

| Step | Repository | What happens |
|---|---|---|
| 2 | Hume | Create the Cognito pool, its app clients, groups and test users. |
| 3 | Hume, then Neo4j | Generate `.env.neo4jee`, copy it to `neo4j-ee/.env`, and add the deployment settings. |
| 4 | Neo4j | Deploy the stack. `deploy.py` reads every setting from `.env`. |
| 5 | Hume | Register the Neo4j Browser redirect URLs on the Cognito `neo4j` client. |
| 6 | Neo4j | Sign in to Neo4j Browser through the tunnels. |
| 7 | Hume | Read the stack values, build the truststore and deploy Hume. |
| 9 | Both | Tear down in reverse order: the Hume stack, the Neo4j stack, then the pool. |

Do step 7 only when you need Hume. Steps 2 to 6 give you a working SSO stack on their own.

## 2. Create the identity provider

Run this from the root of the `hume-aws-cloudformation` repository. It needs no Hume stack and no Neo4j stack.

### Prerequisites

On your laptop:

- AWS CLI v2 and the Session Manager plugin.
- `uv`.
- Both repositories checked out.
- AWS credentials for the stack's account. The examples use `AWS_PROFILE=marketplace` in `us-east-1`.

```bash
export AWS_PROFILE=marketplace AWS_DEFAULT_REGION=us-east-1
aws sso login --profile marketplace
aws sts get-caller-identity --query Account --output text   # expect 385155106615
```

Set `HUME_DOMAIN_NAME` in the Hume repository's `.env` before you continue. The pool builds the Hume redirect address from it. Pick a name inside your hosted zone that no running Hume stack uses. If it is unset, the default is `hume.neo4j-templates.com`.

### Create the pool

```bash
uv run stack/cognito.py up
```

If Cognito refuses the domain prefix because another account holds it, run the following. Add `--name <name>` before the subcommand if you want a second pool that does not collide with the first.

```bash
uv run stack/cognito.py teardown && uv run stack/cognito.py up --domain-prefix <other prefix>
```

The command creates no stack resources. It creates:

| Item | Detail |
|---|---|
| User pool | Named `hume-test-idp` by default. Only an admin can create users. |
| Login domain | `<prefix>.auth.<region>.amazoncognito.com`. The prefix defaults to the pool name. |
| App client `hume-web` | Public client with no secret. Used by Hume. Its callback URLs are `https://<HUME_DOMAIN_NAME>:8081` with and without a trailing slash. |
| App client `neo4j` | Public client with no secret. Used by Neo4j Browser and Bloom. It starts with the placeholder callback `https://localhost:7473/browser/`. Section 5 adds the real ones. |
| Group `neo4j-admins` | Mapped to the Neo4j `admin` role. |
| Group `hume-admins` | Mapped to the Hume `ADMINISTRATOR` role. |
| Users | `neo4j-admin` in `neo4j-admins`, `hume-admin` in `hume-admins`, and `hume-user` in no group. |
| Secrets Manager | `hume/<pool name>/cognito/<user>` holds each user's password. |
| Record file | `stack/.deploy/<pool name>.cognito` holds the pool ID, client IDs and issuer. |

Both clients use the authorization code flow with the scopes `openid`, `profile` and `email`, and sign in through the Cognito user pool. No reader group exists. Create one by hand if you want `OIDC_READER_GROUP`:

```bash
aws cognito-idp create-group --user-pool-id <pool id> --group-name neo4j-readers
```

### Read the values back

```bash
uv run stack/cognito.py status
```

The issuer is `https://cognito-idp.<region>.amazonaws.com/<pool id>`. Check that it serves discovery metadata:

```bash
curl -s <issuer>/.well-known/openid-configuration | python3 -c 'import json,sys; print(json.load(sys.stdin)["issuer"])'
```

If the record file is lost, find the IDs with `aws cognito-idp list-user-pools` and `aws cognito-idp list-user-pool-clients`.

## 3. Create `neo4j-ee/.env`

`deploy.py` reads every one of its flags from `neo4j-ee/.env`, so you do not type them on the command line. The Hume repository writes the OIDC and DNS part of the file from the pool record. You add the deployment settings yourself.

From the Hume repository root:

```bash
uv run stack/cognito.py env --advertised-dns neo4j-oidc.neo4j.local
```

It writes `stack/.deploy/.env.neo4jee` from the pool record and makes no AWS call. Add `--dry-run` to print the file without writing it. The private DNS zone defaults to everything after the first label of the advertised name. Pass `--private-dns-zone <zone>` to set it, and the name must end in that zone. Put `--name <pool>` before `env` if you created a pool with another name.

Copy the file into the Neo4j repository as `.env`:

```bash
cp stack/.deploy/.env.neo4jee <path to neo4j-aws-cloudformation>/neo4j-ee/.env
```

The file looks like this:

```
OIDC_ISSUER_URI=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXXXXXX
OIDC_CLIENT_ID=<hume-web client id>,<neo4j client id>
OIDC_BROWSER_CLIENT_ID=<neo4j client id>
OIDC_PRINCIPAL_CLAIM=cognito:username
OIDC_GROUPS_CLAIM=cognito:groups
OIDC_ADMIN_GROUP=neo4j-admins
ADVERTISED_DNS=neo4j-oidc.neo4j.local
CREATE_PRIVATE_DNS=true
PRIVATE_DNS_ZONE=neo4j.local
```

`.env` is gitignored. It holds no secrets.

If you use another identity provider, write the same keys yourself.

### Add the deployment settings

Append the settings for this deployment. Do this after the `cp`, because the copy replaces the whole file.

```bash
cat >> .env <<'EOF'
MODE=Private
REGION=us-east-1
NUMBER_OF_SERVERS=3
NO_BLOOM=true
NO_GDS=true
EXPECTED_AWS_ACCOUNT=385155106615
EOF
```

`REGION` matters. Without it, `deploy.py` picks a random supported region. `NO_BLOOM` and `NO_GDS` matter too. Both plugins install by default and need licences.

### The rule for every flag

Every `deploy.py` flag has a matching key. The key is the flag name in upper case with underscores. `--number-of-servers` is `NUMBER_OF_SERVERS`, and `--no-bloom` is `NO_BLOOM`. A flag that takes no value, such as `--no-bloom` or `--marketplace`, takes `true` or `false`. `INSTANCE_TYPE` sets the positional instance type. `./deploy.py --help` lists every flag.

Two cases differ. `--expected-account` reads `EXPECTED_AWS_ACCOUNT`. The `--no-create-private-dns` switch has no key. Set `CREATE_PRIVATE_DNS=false` instead.

A value that is not valid for its flag stops the deploy before any AWS call. The message names the key.

### What each key sets

| `.env` key | Flag | Template parameter | Purpose |
|---|---|---|---|
| `OIDC_ISSUER_URI` | `--oidc-issuer-uri` | `OidcIssuerUri` | Turns SSO on. Neo4j reads `<issuer>/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID` | `--oidc-client-id` | `OidcClientId` | Client IDs accepted as the ID token audience. A token from any listed client is accepted. |
| `OIDC_BROWSER_CLIENT_ID` | `--oidc-browser-client-id` | `OidcBrowserClientId` | The client Browser and Bloom sign in with. It must be in `OIDC_CLIENT_ID`. If omitted, the first ID in `OIDC_CLIENT_ID` is used. |
| `OIDC_PRINCIPAL_CLAIM` | `--oidc-principal-claim` | `OidcPrincipalClaim` | The ID token claim that becomes the Neo4j username. The default is `sub`. |
| `OIDC_GROUPS_CLAIM` | `--oidc-groups-claim` | `OidcGroupsClaim` | The ID token claim that lists the user's groups. |
| `OIDC_ADMIN_GROUP` | `--oidc-admin-group` | `OidcAdminGroup` | The group mapped to the Neo4j `admin` role. |
| `OIDC_READER_GROUP` | `--oidc-reader-group` | `OidcReaderGroup` | The group mapped to the built-in `reader` role. |
| `ADVERTISED_DNS` | `--advertised-dns` | `AdvertisedDNS` | The name Browser, the drivers and the redirect URLs use. |
| `CREATE_PRIVATE_DNS` | `--create-private-dns` | `CreatePrivateDns` | Creates a Route 53 private record for the advertised name. |
| `PRIVATE_DNS_ZONE` | `--private-dns-zone` | `PrivateDnsZoneName` | The private hosted zone to create. |

The precedence is: a flag wins over a real environment variable, and a real environment variable wins over `.env`. When `deploy.py` takes values from `.env`, it prints their names at the start of the run.

The principal claim is `cognito:username` because Neo4j reads the ID token. The Cognito access token has no `aud` claim, and Neo4j rejects it. The groups claim is the plain claim name `cognito:groups`.

## 4. Deploy the stack

Run from `neo4j-ee/`:

```bash
AWS_PROFILE=marketplace ./deploy.py
```

Every setting comes from `.env`. The command prints `Loaded MODE, ...` with the names it read. A flag on the command line still wins, so `./deploy.py --number-of-servers 1` deploys one node for a single run.

The private DNS record is required for Hume. Without it, the Hume instance cannot resolve the advertised name.

The deploy writes `.deploy/<stack-name>.txt`. The tunnel scripts read the bastion ID and region from it. Note the stack name. Section 7 needs it as `NEO4J_STACK`.

Private stacks reach the internet for the identity provider's keys through a NAT gateway in each private subnet. The nodes need that path to fetch the JWKS. An ExistingVpc stack must provide the route itself.

### Changing OIDC settings on a running stack

A stack update changes the launch template, but running nodes keep their old config. The node ASGs have no update policy. To apply the change, replace the nodes one at a time. Terminate one node through its ASG without lowering the desired capacity. Wait until its replacement is InService and healthy in every NLB target group, then move to the next node. Each replacement takes about 8 minutes, and the data volume carries over.

## 5. Register the redirect URLs

The identity provider must allow Browser's redirect URL for the Browser client. Cognito matches the URL exactly, including the query string. Register these callback URLs on the `neo4j` client:

```
https://neo4j-oidc.neo4j.local:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri
https://neo4j-oidc.neo4j.local:7473/browser/
https://neo4j-oidc.neo4j.local:7473/bloom/
```

`sso` in the first URL is the provider ID the stack configures. Skip the `bloom` URL when Bloom is not deployed.

### Option A: Hume repository script

From the Hume repository root:

```bash
uv run stack/cognito.py callbacks --client neo4j --add "https://neo4j-oidc.neo4j.local:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri"
uv run stack/cognito.py callbacks --client neo4j --add https://neo4j-oidc.neo4j.local:7473/browser/
uv run stack/cognito.py callbacks --client neo4j --add https://neo4j-oidc.neo4j.local:7473/bloom/
uv run stack/cognito.py callbacks --client neo4j        # list
```

The script refuses URLs that are not https. It keeps the client's other settings, and it sets the logout URLs to the same list. The placeholder `https://localhost:7473/browser/` stays.

### Option B: AWS console

1. Open **Amazon Cognito**, then **User pools**, then the pool.
2. Open **App clients** and choose the `neo4j` client.
3. Open the **Login pages** tab and choose **Edit** under **Managed login pages configuration**.
4. Under **Allowed callback URLs**, add each URL from the list above.
5. Choose **Save changes**.

### Option C: AWS CLI

`update-user-pool-client` resets every setting you leave out to its default. Do not call it with only `--callback-urls`, or you will wipe the client's scopes, flows and other settings. Read the full client, add the URLs, and write the full client back:

```bash
export AWS_PROFILE=marketplace AWS_REGION=us-east-1
POOL_ID=<pool id>
CLIENT_ID=<neo4j client id>
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

Send Cognito the same authorize request Browser makes. Replace the domain with your pool's login domain, and set `CLIENT_ID` and `HOST` as above:

```bash
curl -s -o /dev/null -w '%{redirect_url}\n' -G \
  https://<prefix>.auth.us-east-1.amazoncognito.com/oauth2/authorize \
  --data-urlencode client_id="$CLIENT_ID" \
  --data-urlencode response_type=code \
  --data-urlencode 'scope=openid profile email' \
  --data-urlencode "redirect_uri=https://$HOST:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri" \
  --data-urlencode code_challenge_method=S256 \
  --data-urlencode "code_challenge=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
```

A redirect to `.../login` means the URL is registered. A redirect to `.../error?error=redirect_mismatch` means it is not, or that it is registered on a different client than `OIDC_BROWSER_CLIENT_ID`.

## 6. Sign in to Neo4j Browser

### Point the advertised name at your laptop

The advertised name only resolves inside the VPC. Map it to localhost so the browser reaches the tunnels:

```bash
echo "127.0.0.1 neo4j-oidc.neo4j.local" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

Use the advertised name in the browser, not `localhost`. Browser builds the redirect URL from the address bar, and the identity provider only accepts the registered name.

The first lookup can take a few seconds, because macOS tries mDNS for `.local` names first.

### Open the tunnels

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

### Sign in

1. Open `https://neo4j-oidc.neo4j.local:7473/browser/`.
2. The stack's certificate is self-signed, so Chrome shows a security warning. Click **Advanced**, then **Proceed**.
3. Set the Connect URL to `neo4j+s://neo4j-oidc.neo4j.local:7687`. The cluster serves Bolt over TLS.
4. Choose **SSO** and sign in on the identity provider's page.
5. Browser returns to `/browser/` and connects.

### Test user passwords

The test pool's passwords are in Secrets Manager under `hume/<pool name>/cognito/<user>`. Each is stored as a plain string. This copies one to the clipboard without showing it:

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

## 7. Connect Hume to the stack

Run these steps from the root of the `hume-aws-cloudformation` repository. Part 5 to Part 8 of its `docs/run-book.md` hold the full detail, including the exact Hume deploy line. This section gives the order and the values that come from the Neo4j stack.

Set the stack name first:

```bash
export NEO4J_STACK=<stack-name>
```

### 7.1 Read the Neo4j values

```bash
uv run stack/neo4j_env.py "$NEO4J_STACK" --dry-run
uv run stack/neo4j_env.py "$NEO4J_STACK"
```

The script reads the SSM parameters under `/neo4j-ee/<stack>/` and finds the bastion and the public subnets. It fetches the load balancer certificate over SSM and saves it to `stack/.deploy/neo4j-ca.pem`. It writes the `HUME_EXISTING_*` settings into the Hume `.env`. None of the values is a secret.

### 7.2 Build the truststore

The stack certificate is self-signed, and it covers the advertised name. Hume needs it in a Java truststore secret.

```bash
grep -c 'BEGIN CERTIFICATE' stack/.deploy/neo4j-ca.pem   # 1
grep -c 'PRIVATE KEY' stack/.deploy/neo4j-ca.pem         # 0
cd stack && ./deploy.py truststore --ca-file .deploy/neo4j-ca.pem
```

The command prints `HUME_EXISTING_NEO4J_TRUSTSTORE_SECRET_ARN=<arn>`. Add it to the Hume `.env`. It needs `keytool` or Docker.

The Hume instance reads the truststore only at boot. A new Neo4j certificate therefore needs a new Hume instance.

### 7.3 Set the Hume sign-in values

Add the issuer and the `hume-web` client ID to the Hume `.env`. Use the `hume-web` ID only. The `neo4j` client ID goes only to the Neo4j stack.

```
HUME_OIDC_ISSUER_URI=<issuer>
HUME_OIDC_CLIENT_ID=<hume-web client id>
```

The template defaults for the admin group, roles claim and principal claim already match Cognito. Hume reads the access token, so its principal claim is `username`. Neo4j reads the ID token, so its principal claim is `cognito:username`. Both are intentional. Set `HUME_AUTH_PROVIDER=Native` to turn Hume's OIDC off.

### 7.4 Deploy Hume

Follow Part 7 of the Hume `docs/run-book.md`. The Hume `.env` must hold these settings first:

```
HUME_NEO4J_MODE=ExistingEE
HUME_EXISTING_NEO4J_HOST=<advertised-dns>
HUME_EXISTING_VPC_ID=...
HUME_EXISTING_PUBLIC_SUBNET_IDS=...
HUME_EXISTING_PRIVATE_SUBNET_IDS=...
HUME_EXISTING_NEO4J_NLB_SG_ID=...
HUME_EXISTING_VPC_ENDPOINT_SG_ID=...
HUME_EXISTING_NEO4J_PASSWORD_SECRET_ARN=...
HUME_NEO4J_CA_FILE=stack/.deploy/neo4j-ca.pem
HUME_EXISTING_NEO4J_TRUSTSTORE_SECRET_ARN=...
HUME_DOMAIN_NAME=<matches a hume-web callback>
HUME_OIDC_ISSUER_URI=...
HUME_OIDC_CLIENT_ID=...
```

Steps 7.1 to 7.3 fill in every line except `HUME_NEO4J_MODE`. Set it by hand.

Hume connects to `neo4j+s://<advertised-dns>:7687` with the Neo4j service account password from the secret in `HUME_EXISTING_NEO4J_PASSWORD_SECRET_ARN`. This is password login. Signing in to Hume with SSO does not yet forward the user's token to Neo4j. The `hume-web` client ID is in the Neo4j audience list so that forwarding can work later.

### 7.5 Check Hume

Run the checks in Part 8 of the Hume `docs/run-book.md`. These cover the smoke test, browser sign-in as `hume-admin`, and the TLS check from the Hume host to `<advertised-dns>:7687`. For a token check without a browser:

```bash
uv run stack/cognito.py check --stack-name "$HUME_STACK"
```

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `DNS_PROBE_POSSIBLE` or "This site can't be reached" | The advertised name is missing from `/etc/hosts`. | Run the hosts step in section 6. |
| The page does not load | The Browser tunnel on 7473 is closed. | Restart `./browser-tunnel.sh`. |
| SSO is disabled in the login form, or the connection fails | The Bolt tunnel on 7687 is closed. | Restart `./bolt-tunnel.sh` and reload the page. |
| Cognito shows `redirect_mismatch` | Browser's redirect URL is not registered on the Browser client. | Register the URLs in section 5. Check `OIDC_BROWSER_CLIENT_ID` names the client that has them. If it still fails, URL-decode `redirect_uri` from the address bar and add it exactly as shown. |
| A user who should be admin gets only `PUBLIC` | The user is not in the admin group, or the groups claim or group name is wrong. | Check the user's groups in the identity provider and the `OIDC_*` values in `.env`. |
| A stack update to OIDC settings has no effect | Running nodes keep their old config. | Replace the nodes one at a time, as described in section 4. |
| `deploy.py` ignores a value in `.env` | A flag or a real environment variable of the same name is set. | A flag wins over the environment, and the environment wins over `.env`. Unset the variable. |
| The Hume instance cannot resolve the advertised name | The private DNS record is missing. | Redeploy with `CREATE_PRIVATE_DNS=true` and a `PRIVATE_DNS_ZONE`. |
| The name does not resolve for about 7 minutes after the record is created | Route 53 holds a negative cache entry. | Wait and retry. |
| Hume fails with a `PKIX` error | The truststore holds a different certificate than the stack serves. | Rebuild the truststore as in 7.2 and start a new Hume instance. |

To see the OIDC settings a node is running with, check `/etc/neo4j/neo4j.conf` on the node through SSM. Look for the `dbms.security.oidc.sso.*` lines. `/var/log/cloud-init-output.log` logs `OIDC enabled` or `OIDC disabled` at boot.

## 9. Clean up

Tear down in reverse order. The Neo4j teardown force-deletes the VPC, load balancers and NAT gateways that Hume still uses, so delete Hume first.

1. Delete the Hume stack. The command is in Part 10 of the Hume `docs/run-book.md`.
2. Delete the Neo4j stack from `neo4j-ee/`. The EBS data volumes are retained unless you pass `--delete-volumes`:

   ```bash
   AWS_PROFILE=marketplace ./teardown.sh --delete-volumes --expected-account 385155106615 <stack-name>
   ```

3. Delete the pool from the Hume repository root:

   ```bash
   uv run stack/cognito.py teardown
   ```

4. Remove the hosts entry:

   ```bash
   sudo sed -i '' '/neo4j-oidc\.neo4j\.local/d' /etc/hosts
   ```

5. Remove `neo4j-ee/.env` if it holds settings for a pool you deleted.
