# Run book: Neo4j EE with OIDC sign-in

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

For what each setting does, how to change sign-in settings on a running stack, and fixes for sign-in problems, see [Single Sign-On in PRIVATE.md](PRIVATE.md#single-sign-on-oidc).

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

An `InvalidAMIID.NotFound` error, an empty `Images` list, or a state other than `available` means the file is stale. To rebuild the AMI or to use the Marketplace AMI instead, see [Choose an AMI](LOCAL-TESTING.md#choose-an-ami).

You also need the sign-in settings for your identity provider:

- **Cognito from the Hume repository:** Follow the **Write the OIDC settings** step in the Hume run book, `docs/run-book.md#write-the-oidc-settings` in the `hume-aws-cloudformation` repository. It writes `stack/.deploy/.env.neo4jee`.
- **Another identity provider:** Write the same keys by hand. The keys are listed in [Sign-in settings](PRIVATE.md#sign-in-settings). The provider must also allow the three redirect addresses in [Redirect addresses](PRIVATE.md#redirect-addresses).

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
OIDC_PRINCIPAL_CLAIM=cognito:username
OIDC_GROUPS_CLAIM=cognito:groups
OIDC_ADMIN_GROUP=neo4j-admins
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
NO_BLOOM=true
NO_GDS=true
EXPECTED_AWS_ACCOUNT=385155106615
EOF
```

Hume needs three of these values:

- `MODE=Private`. Hume reads values that only the Private template publishes.
- `NUMBER_OF_SERVERS=3`. A one-node stack has no second private subnet and only one public subnet, and Hume needs both.
- `CREATE_PRIVATE_DNS=true` with `PRIVATE_DNS_ZONE`. The stack then creates the private hosted zone and the record for the advertised name. Without the record, the Hume instance cannot resolve the advertised name. Each node advertises that name for Bolt only when the stack creates the zone, so do not replace `PRIVATE_DNS_ZONE` with `PRIVATE_DNS_HOSTED_ZONE_ID`. See [NLB Routing](PRIVATE.md#nlb-routing).

`REGION` is required too. Without it, `deploy.py` picks a random region. `NO_BLOOM` and `NO_GDS` skip the two plugins, which otherwise install by default and need licences.

Every other `deploy.py` flag can go in `.env` as well. See [Settings in .env](LOCAL-TESTING.md#settings-in-env).

## Deploy the stack

```bash
./deploy.py
```

The first line of output starts with `Loaded` and names the keys read from `.env`. Check that the list includes `MODE`, `REGION`, `NUMBER_OF_SERVERS` and the `OIDC_` keys. Stack creation takes 5 to 10 minutes.

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
- **Stack creation fails:** See [Troubleshooting in PRIVATE.md](PRIVATE.md#troubleshooting), then [Tear down](#tear-down) before you retry.
- **A preflight check fails:** See [Preflight Check](PRIVATE.md#preflight-check).

## Check the values Hume reads

Hume's `neo4j_env.py` reads these values from the stack:

| Value | Where it lives |
|---|---|
| `advertised-dns`, `vpc-id`, `external-sg-id`, `vpc-endpoint-sg-id`, `password-secret-arn`, `private-subnet-1-id`, `private-subnet-2-id`, `nlb-dns` | SSM parameters under `/neo4j-ee/<stack-name>/` |
| The bastion instance ID | The stack output `Neo4jOperatorBastionId` |
| Three public subnets, `10.0.1.0/24`, `10.0.2.0/24` and `10.0.3.0/24`, one in each zone | The stack VPC |
| The certificate on port 7687, whose name must match `advertised-dns` | The load balancer |

`external-sg-id` is the load balancer security group, not the instance security group.

Check that all eight parameters exist:

```bash
aws ssm get-parameters-by-path --path "/neo4j-ee/$NEO4J_STACK/" \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sort
```

The list must include all eight names in the table. The stack also publishes other parameters. Hume does not read them.

Check the bastion output:

```bash
aws cloudformation describe-stacks --stack-name "$NEO4J_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='Neo4jOperatorBastionId'].OutputValue" \
  --output text
```

The command prints an instance ID that starts with `i-`.

If `private-subnet-2-id` is missing, the stack has one node. Tear it down and deploy again with `NUMBER_OF_SERVERS=3`.

**Back to Hume.** Go to the **Allow the Neo4j sign-in addresses** step in the Hume run book, `docs/run-book.md#allow-the-neo4j-sign-in-addresses`. Its later steps take the stack name in `$NEO4J_STACK`. Come back to [Sign in to Neo4j Browser](#sign-in-to-neo4j-browser) if you want to test sign-in from your laptop.

## Sign in to Neo4j Browser

This step is optional. Before you start it, register the redirect addresses with the identity provider. For the Hume Cognito pool, that is the **Allow the Neo4j sign-in addresses** step in the Hume run book.

Point the advertised name at your laptop. It only resolves inside the VPC, so map it to localhost:

```bash
echo "127.0.0.1 neo4j-oidc.neo4j.local" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

Open two tunnels through the bastion. Run each in its own terminal from `neo4j-ee/` and leave both open:

```bash
# Terminal 1: Browser page and sign-in discovery on 7473
./browser-tunnel.sh "$NEO4J_STACK"

# Terminal 2: Bolt on 7687
./bolt-tunnel.sh "$NEO4J_STACK"
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

Run `SHOW CURRENT USER` in Browser and check the roles:

| User | Group | Neo4j roles |
|---|---|---|
| `neo4j-admin` | `neo4j-admins` | `admin`, `PUBLIC` |
| `hume-admin` | `hume-admins` | `PUBLIC` |
| `hume-user` | none | `PUBLIC` |

A user with only `PUBLIC` can sign in, but the graph looks empty and writes are refused. `hume-admins` is not mapped to a Neo4j role on purpose.

If sign-in fails, see [Troubleshooting sign-in](PRIVATE.md#troubleshooting-sign-in).

## Tear down

When the stack serves Hume, delete the Hume stack first. The Neo4j teardown deletes the VPC, load balancer and NAT gateways that Hume uses. The **Tear down** step in the Hume run book, `docs/run-book.md#tear-down`, gives the order.

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

If the teardown stops with a warning, fix the problem it names and run the same command again. See [Tear down in LOCAL-TESTING.md](LOCAL-TESTING.md#tear-down) for what the script deletes and what it leaves.
