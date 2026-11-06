# Neo4j EE: OIDC Sign-In Run Book

This run book deploys a Private Neo4j Enterprise Edition cluster with single sign-on through OpenID Connect. It covers the Neo4j steps only. When the cluster is for Hume, the Hume run book in the `hume-aws-cloudformation` repository sends you here and tells you when to go back.

Password login stays enabled next to single sign-on. Operator tools such as `validate-private` keep using the native `neo4j` password.

The examples use the Amazon Cognito pool that the Hume repository creates, with the advertised name `neo4j-oidc.neo4j.local`. Replace that name everywhere if your `.env` uses another.

Do the steps in this order:

1. [Before you start](#before-you-start)
2. [Create the env file](#create-the-env-file)
3. [Deploy the stack](#deploy-the-stack)
4. [Check the values Hume reads](#check-the-values-hume-reads)
5. [Sign in to Neo4j Browser](#sign-in-to-neo4j-browser)
6. [Tear down](#tear-down)

For what each setting does, how to change sign-in settings on a running stack, and fixes for sign-in problems, see [Single Sign-On (OIDC) Reference](../reference/oidc-sign-in.md).

## Before you start

You need these on your laptop:

- AWS CLI v2 and the Session Manager plugin.
- `uv`.
- AWS credentials for the account the stack goes in.

Set up the shell. The examples use the `marketplace` profile in `us-east-1`:

```bash
export AWS_PROFILE=marketplace AWS_DEFAULT_REGION=us-east-1
aws sso login --profile marketplace
aws sts get-caller-identity --query Account --output text
```

The last command prints the account ID. It must be `385155106615` for the `marketplace` profile.

On another account, use your own profile and account ID. Replace `marketplace` and `385155106615` with them in every command below.

`deploy.py` reads the AMI ID from `marketplace/ami-id.txt`. The file is gitignored, and `deploy.py` exits if it is missing. The file can also point at an AMI that no longer exists. Check it from `neo4j-ee/`:

```bash
aws ec2 describe-images --image-ids "$(cat marketplace/ami-id.txt)" --region us-east-1
```

An `InvalidAMIID.NotFound` error, an empty `Images` list, or a state other than `available` means the file is stale. To rebuild the AMI or to use the Marketplace AMI instead, see [Choose an AMI](../guides/local-testing.md#choose-an-ami).

You also need the sign-in settings for your identity provider:

- **Cognito from the Hume repository:** Follow the **Write the OIDC settings** step in the Hume run book, `docs/run-book.md#write-the-oidc-settings` in the `hume-aws-cloudformation` repository. It writes `stack/.deploy/.env.neo4jee`.
- **Another identity provider:** Write the same keys by hand. The keys are listed in [Sign-in settings](../reference/oidc-sign-in.md#sign-in-settings). The provider must also allow the three redirect addresses in [Redirect addresses](../reference/oidc-sign-in.md#redirect-addresses).

Run every command below from `neo4j-ee/` unless the step says otherwise.

## Create the env file

You have `stack/.deploy/.env.neo4jee` from the Hume repository. Copy it into this repository as `neo4j-ee/.env`:

```bash
cp <path to hume-aws-cloudformation>/stack/.deploy/.env.neo4jee .env
```

The file holds the sign-in and DNS settings. It holds no secrets, and `.env` is gitignored:

```
OIDC_ISSUER_URI=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXXXXXX
OIDC_CLIENT_ID=<hume-web client id>,<neo4j client id>
OIDC_BROWSER_CLIENT_ID=<neo4j client id>
OIDC_PRINCIPAL_CLAIM=sub
OIDC_GROUPS_CLAIM=cognito:groups
OIDC_ADMIN_GROUP=neo4j-admins
OIDC_GROUP_ROLE_MAPPING=spine-analysts=spine_analyst;spine-officers=spine_officer
ADVERTISED_DNS=neo4j-oidc.neo4j.local
CREATE_PRIVATE_DNS=true
PRIVATE_DNS_ZONE=neo4j.local
```

Add the deployment settings to the end of the file. Do this after the `cp`, because the copy replaces the whole file:

```bash
cat >> .env <<'EOF'
MODE=Private
REGION=us-east-1
NUMBER_OF_SERVERS=3
ENABLE_INTERNET_EGRESS=true
NO_BLOOM=true
NO_GDS=true
EXPECTED_AWS_ACCOUNT=385155106615
EOF
```

The sign-in values follow the [Token Design](../reference/oidc-sign-in.md#token-design):

- `OIDC_PRINCIPAL_CLAIM=sub` and `OIDC_GROUPS_CLAIM=cognito:groups` name claims that are in both tokens. Browser presents the ID token, and Hume forwards the access token.
- `OIDC_ADMIN_GROUP` and `OIDC_GROUP_ROLE_MAPPING` merge into one Neo4j setting. See [Group Mapping](../reference/oidc-sign-in.md#group-mapping).
- `OIDC_GROUP_ROLE_MAPPING` names roles that the mapping does not create. The Spine demo creates `spine_analyst` and `spine_officer` after the Hume stack is up. Users in those groups can sign in before then, and the roles grant no privileges until they exist.

The Hume access token also needs an `aud` claim. Cognito adds it only through a pre token generation trigger. The Hume repository's `cognito.py sync` attaches that trigger to the pool.

Hume signs in with the basic scheme, using the username `__keycloak_token` and the token as the password. Leave `hume.driver.neo4j.security.realm` unset, because Neo4j rejects a sign-in that names a realm.

Hume needs three of these values:

- `MODE=Private`. Hume reads values that only the Private template publishes.
- `NUMBER_OF_SERVERS=3`. A one-node stack has no second private subnet and only one public subnet, and Hume needs both.
- `CREATE_PRIVATE_DNS=true` with `PRIVATE_DNS_ZONE`. The stack then creates the private hosted zone and the record for the advertised name. Without the record, the Hume instance cannot resolve the advertised name. Each node advertises that name for Bolt only when the stack creates the zone, so do not replace `PRIVATE_DNS_ZONE` with `PRIVATE_DNS_HOSTED_ZONE_ID`. See [NLB Routing](../topologies/private/architecture.md#nlb-routing).

### Enable internet egress

`ENABLE_INTERNET_EGRESS=true` sets the `EnableInternetEgress` template parameter. The flag form is `--enable-internet-egress true`. The stack then creates one NAT gateway per Availability Zone and a default route from the private subnets. Neo4j reaches the OIDC issuer through those NAT gateways. Hume also requires `EnableInternetEgress` set to `true` on the Neo4j stack. The default is `false`. With `OIDC_ISSUER_URI` set and the default, `deploy.py` exits before creating the stack, and CloudFormation rejects the template.

### Other settings

`REGION` is required too. Without it, `deploy.py` picks a random region. `NO_BLOOM` and `NO_GDS` skip the two plugins, which otherwise install by default and need licences.

Every other `deploy.py` flag can go in `.env` as well. See [Settings in .env](../guides/local-testing.md#settings-in-env).

## Deploy the stack

```bash
./deploy.py
```

The first line of output starts with `Loaded` and names the keys read from `.env`. Check that the list includes `MODE`, `REGION`, `NUMBER_OF_SERVERS`, `ENABLE_INTERNET_EGRESS` and the `OIDC_` keys. Stack creation takes 5 to 10 minutes.

When the deploy finishes, it prints the outputs and writes them to `.deploy/<stack-name>.txt`. The stack name has the form `test-ee-<unix seconds>`. Save it, because every later step needs it:

```bash
export NEO4J_STACK=$(basename "$(ls -t .deploy/test-ee-*.txt | head -1)" .txt)
echo "$NEO4J_STACK"
```

Then run the readiness checks from `validate-private/`:

```bash
cd validate-private
uv run preflight "$NEO4J_STACK"
cd ..
```

The summary line must read `12 passed, 0 failed`.

If a step fails:

- **The deploy stops before creating the stack:** A value in `.env` is not valid for its flag. The message names the key.
- **The deploy ignores a value in `.env`:** A flag or an environment variable of the same name is set, and it wins over `.env`. Unset it.
- **Stack creation fails:** See [Troubleshooting in the Private Local Testing guide](../topologies/private/local-testing.md#troubleshooting), then [Tear down](#tear-down) before you retry.
- **A preflight check fails:** See [Preflight Check](../topologies/private/operator-guide.md#preflight-check).

## Check the values Hume reads

Hume's `neo4j_env.py` stops unless all 13 of these SSM parameters exist under `/neo4j-ee/<stack-name>/`:

| Parameter | What it holds |
|---|---|
| `advertised-dns` | The name Hume connects to. The certificate on port 7687 must match it. |
| `vpc-id` | The stack VPC |
| `external-sg-id` | The load balancer security group |
| `vpc-endpoint-sg-id` | The security group on the VPC interface endpoints |
| `app-username` | The Neo4j application user |
| `app-password-secret-arn` | The secret that holds the application user's password |
| `kms-key-arn` | The stack KMS key |
| `ca-pem` | The trust anchor for the load balancer certificate |
| `private-subnet-1-id`, `private-subnet-2-id` | The private subnets |
| `public-subnet-1-id`, `public-subnet-2-id`, `public-subnet-3-id` | The public subnets, one in each zone |

`external-sg-id` is the load balancer security group, not the instance security group.

`kms-key-arn` is the full ARN of the stack KMS key. Hume needs `kms:Decrypt` on it to read the password secrets.

`ca-pem` must hold a certificate. Hume stops on the value `pending`, which means no node has published the anchor yet.

Check all 13 parameters and the `ca-pem` value:

```bash
have=$(aws ssm get-parameters-by-path --path "/neo4j-ee/$NEO4J_STACK/" \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sed 's|.*/||')
for name in advertised-dns vpc-id external-sg-id vpc-endpoint-sg-id \
  app-username app-password-secret-arn kms-key-arn ca-pem \
  private-subnet-1-id private-subnet-2-id \
  public-subnet-1-id public-subnet-2-id public-subnet-3-id; do
  echo "$have" | grep -qx "$name" || echo "MISSING $name"
done
aws ssm get-parameter --name "/neo4j-ee/$NEO4J_STACK/ca-pem" \
  --query Parameter.Value --output text | head -1
```

The output must be one line, `-----BEGIN CERTIFICATE-----`. Any `MISSING` line fails the check.

If `private-subnet-2-id`, `public-subnet-2-id`, or `public-subnet-3-id` is missing, the stack has one node. Tear it down and deploy again with `NUMBER_OF_SERVERS=3`.

The Spine demo load also reads `password-secret-arn`, the admin password secret. `neo4j_env.py` does not read it.

The stack publishes other parameters, such as `nlb-dns`. Hume does not read them.

**Back to Hume.** Go to the **Read the Neo4j values** step in the Hume run book, `docs/run-book.md#read-the-neo4j-values`. It sets the stack name in `$NEO4J_STACK`. Come back to [Sign in to Neo4j Browser](#sign-in-to-neo4j-browser) if you want to test sign-in from your laptop.

## Sign in to Neo4j Browser

This step is optional. Before you start it, register the redirect addresses with the identity provider. For the Hume Cognito pool, `cognito.py env` registered them in the **Write the OIDC settings** step of the Hume run book. Rerun it if the advertised name changed.

Point the advertised name at your laptop. It only resolves inside the VPC, so map it to localhost:

```bash
echo "127.0.0.1 neo4j-oidc.neo4j.local" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

Open two tunnels through the bastion. Run each in its own terminal from `neo4j-ee/` and leave both open:

```bash
# Terminal 1: Browser page and sign-in discovery on 7473
./scripts/browser-tunnel.sh "$NEO4J_STACK"

# Terminal 2: Bolt on 7687
./scripts/bolt-tunnel.sh "$NEO4J_STACK"
```

Each tunnel is ready when it prints `Waiting for connections...`. Both scripts use the `marketplace` profile unless `AWS_PROFILE` is set.

Sign in:

1. Open `https://neo4j-oidc.neo4j.local:7473/browser/`. Use the advertised name, not `localhost`. Browser builds the redirect address from the address bar.
2. The certificate is self-signed, so the browser shows a warning. Click **Advanced**, then **Proceed**.
3. Set the Connect URL to `neo4j+s://neo4j-oidc.neo4j.local:7687`.
4. Choose **SSO** and sign in on the identity provider's page.
5. Browser returns to `/browser/` and connects.

The Hume test pool stores each test user's password in Secrets Manager. This copies the `neo4j-admin` password to the clipboard without showing it:

```bash
aws secretsmanager get-secret-value \
  --secret-id hume/hume-test-idp/cognito/neo4j-admin \
  --query SecretString --output text | pbcopy
```

If the pool was created with `cognito.py --name`, replace `hume-test-idp` with that name.

Run `SHOW CURRENT USER` in Browser and check the roles. The `user` value is the Cognito `sub`, an opaque ID, and it does not match the sign-in name:

| User | Group | Neo4j roles |
|---|---|---|
| `neo4j-admin` | `neo4j-admins` | `admin`, `PUBLIC` |
| `hume-admin` | `hume-admins` | `PUBLIC` |
| `hume-user` | none | `PUBLIC` |

A user with only `PUBLIC` can sign in, but the graph looks empty and writes are refused. On `spine`, Neo4j refuses that user with `ACCESS on database 'spine' is not allowed`. `hume-admins` is not mapped to a Neo4j role on purpose.

If sign-in fails, see [Troubleshooting sign-in](../reference/oidc-sign-in.md#troubleshooting-sign-in).

## Tear down

When the stack serves Hume, delete the Hume stack first. The Neo4j teardown deletes the VPC, load balancer and NAT gateways that Hume uses. Hume requires `EnableInternetEgress` set to `true` on the Neo4j stack, so those NAT gateways exist. The **Tear down** step in the Hume run book, `docs/run-book.md#tear-down`, gives the order.

Delete the stack and its data volumes:

```bash
./teardown.sh --delete-volumes --expected-account 385155106615 "$NEO4J_STACK"
```

The output ends with a banner that reads `Teardown complete.` Without `--delete-volumes`, the data volumes are kept and their IDs are printed.

Remove the hosts entry if you added one:

```bash
sudo sed -i '' '/neo4j-oidc\.neo4j\.local/d' /etc/hosts
```

Remove `.env` if its settings belong to a pool you are about to delete:

```bash
rm .env
```

To delete the Hume Cognito pool, go back to the **Tear down** step in the Hume run book.

If the teardown stops with a warning, fix the problem it names and run the same command again. See [Tear down in the Local Testing guide](../guides/local-testing.md#tear-down) for what the script deletes and what it leaves.
