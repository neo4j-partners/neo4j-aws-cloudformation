# Neo4j EE: App Password Rotation Run Book

This run book changes the password of the `neo4j_app` user on a running Private or Existing VPC stack. It needs no template change and no node replacement.

The password lives in two places. One is the secret `neo4j/<stack-name>/app-password`. The other is the Neo4j `system` database. The steps below keep the two in step:

1. [Store the new password](#store-the-new-password) as a pending version of the secret.
2. [Change the password in Neo4j](#change-the-password-in-neo4j).
3. [Promote the new password](#promote-the-new-password) to the current version.
4. [Check the login](#check-the-login) on every node.
5. [Update the consumers](#update-the-consumers).

**Downtime:** New logins with the old password fail from step 2 until each consumer rereads the secret. Schedule the rotation with the owners of those consumers.

## How the rotation works

- **The password stays out of your shell.** Secrets Manager generates the value, and a pipe on your machine carries it straight back to Secrets Manager. Each node reads it with its own IAM role. It never lands in a shell variable, your history, or a command argument.
- **The pending version is safe to abandon.** Readers keep getting `AWSCURRENT` until step 3. If step 2 fails, the secret and the database still match.
- **Only the leader accepts the change.** Authentication lives in the replicated `system` database, so `ALTER USER` works only on its leader. The steps send the command to every node, and the other nodes refuse it.
- **A replaced node keeps the new password.** A node creates `neo4j_app` only on its first boot. A node that reattaches a volume skips that step.

## Before you start

You need these permissions:

| Action | Resource |
|---|---|
| `secretsmanager:GetRandomPassword` | `*` |
| `secretsmanager:ListSecretVersionIds`, `secretsmanager:PutSecretValue`, `secretsmanager:UpdateSecretVersionStage` | The `app-password` secret |
| `kms:GenerateDataKey`, `kms:Decrypt` | The key in `/neo4j-ee/<stack-name>/kms-key-arn` |
| `ssm:SendCommand` | The stack's nodes and the `AWS-RunShellScript` document |
| `ssm:ListCommandInvocations` | `*` |

The nodes need no new permissions. Their role already reads both password secrets.

Run every step from one shell, in one empty working directory. The steps share these variables and functions:

```bash
export STACK=<stack-name>
export REGION=<region>

run_on_nodes() {
  sed -e "s/@STACK@/$STACK/g" -e "s/@REGION@/$REGION/g" -e "s/@STAGE@/$2/g" \
    "$1" > rendered.json
  aws ssm send-command --region "$REGION" \
    --document-name AWS-RunShellScript \
    --targets "Key=tag:aws:cloudformation:stack-name,Values=$STACK" \
      "Key=tag:aws:cloudformation:logical-id,Values=Neo4jNode1ASG,Neo4jNode2ASG,Neo4jNode3ASG" \
    --max-errors 100% \
    --parameters file://rendered.json \
    --query Command.CommandId --output text
}

node_results() {
  aws ssm list-command-invocations --region "$REGION" --command-id "$1" --details \
    --query 'CommandInvocations[].[InstanceId,Status,CommandPlugins[0].Output]' \
    --output text
}
```

The targets select the Neo4j nodes and leave out the bastion.

Write the two node scripts. They hold no secrets:

```bash
cat > rotate.tmpl.json <<'EOF'
{"commands": [
  "admin=$(aws secretsmanager get-secret-value --secret-id neo4j/@STACK@/password --query SecretString --output text --region @REGION@)",
  "pw=$(aws secretsmanager get-secret-value --secret-id neo4j/@STACK@/app-password --version-stage @STAGE@ --query SecretString --output text --region @REGION@)",
  "[[ \"$pw\" =~ ^[A-Za-z0-9]+$ ]] || { echo 'Password is not alphanumeric'; exit 1; }",
  "{ echo \":param {pw: '$pw'}\"; echo 'ALTER USER neo4j_app SET PASSWORD $pw CHANGE NOT REQUIRED;'; } | NEO4J_USERNAME=neo4j NEO4J_PASSWORD=\"$admin\" cypher-shell -a bolt+ssc://localhost:7687 -d system --format plain"
]}
EOF

cat > login.tmpl.json <<'EOF'
{"commands": [
  "pw=$(aws secretsmanager get-secret-value --secret-id neo4j/@STACK@/app-password --version-stage @STAGE@ --query SecretString --output text --region @REGION@)",
  "echo 'SHOW CURRENT USER YIELD user RETURN user;' | NEO4J_USERNAME=neo4j_app NEO4J_PASSWORD=\"$pw\" cypher-shell -a bolt+ssc://localhost:7687 -d system --format plain"
]}
EOF
```

## Store the new password

Record the current version, then generate the new password and store it as `AWSPENDING`. The value passes through a pipe, so it is never printed.

```bash
CURRENT_ID=$(aws secretsmanager list-secret-version-ids --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --query "Versions[?contains(VersionStages, 'AWSCURRENT')].VersionId" \
  --output text)

PENDING_ID=$(aws secretsmanager get-random-password --region "$REGION" \
    --password-length 32 --exclude-punctuation \
    --query RandomPassword --output text \
  | tr -d '\n' \
  | aws secretsmanager put-secret-value --region "$REGION" \
    --secret-id "neo4j/$STACK/app-password" \
    --version-stages AWSPENDING \
    --secret-string file:///dev/stdin \
    --query VersionId --output text)

echo "current=$CURRENT_ID pending=$PENDING_ID"
```

Both IDs must print. Keep them for [Roll back](#roll-back).

The password must stay alphanumeric. `--exclude-punctuation` guarantees that. Both the node script and the first-boot user creation refuse any other value.

## Change the password in Neo4j

Send the script to every node:

```bash
ID=$(run_on_nodes rotate.tmpl.json AWSPENDING)
node_results "$ID"
```

Repeat `node_results "$ID"` until no node shows `Pending` or `InProgress`. Read the status of each node:

| Result | Meaning | Next step |
|---|---|---|
| One node shows `Success`. The others fail with `51N29`, "must be executed on the current 'LEADER' server". | The rotation worked. The followers refused it, as expected. | Continue. |
| One node on a single-server stack shows `Success`. | The rotation worked. | Continue. |
| More than one node shows `Success`. | Leadership moved during the run. The value is the same on each node. | Continue. |
| No node shows `Success`. | The password did not change. | Fix the error and rerun this step. `AWSCURRENT` has not moved. |

## Promote the new password

Move `AWSCURRENT` to the new version, then clear `AWSPENDING`. Secrets Manager gives the old version the `AWSPREVIOUS` label.

```bash
aws secretsmanager update-secret-version-stage --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --version-stage AWSCURRENT \
  --move-to-version-id "$PENDING_ID" \
  --remove-from-version-id "$CURRENT_ID"

aws secretsmanager update-secret-version-stage --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --version-stage AWSPENDING \
  --remove-from-version-id "$PENDING_ID"
```

## Check the login

Log in as `neo4j_app` on every node with the promoted password:

```bash
ID=$(run_on_nodes login.tmpl.json AWSCURRENT)
node_results "$ID"
```

Every node must show `Success` and print `"neo4j_app"`. This check works on any stack. The release suite covers the same login only when `InstallAPOC` is true, and it skips the check otherwise.

If a node fails with an authentication error, wait 30 seconds and run the check again. A node that keeps failing has not applied the change, so [roll back](#roll-back).

## Update the consumers

Every consumer must read the secret again. The ARN and the KMS key do not change, so no consumer needs a new grant.

- **An application that reads the secret at start:** Restart the application.
- **A copy of the password held elsewhere:** Update the copy. Hume holds a copy in its own database. Follow the Neo4j app password rotation steps in the `hume-aws-cloudformation` repository, which rerun its configuration import.

## Roll back

If a step failed before promotion and `ALTER USER` succeeded, restore the old password and clear the pending version:

```bash
ID=$(run_on_nodes rotate.tmpl.json AWSCURRENT)
node_results "$ID"

aws secretsmanager update-secret-version-stage --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --version-stage AWSPENDING \
  --remove-from-version-id "$PENDING_ID"
```

After promotion, restore the old password from `AWSPREVIOUS`, then move `AWSCURRENT` back:

```bash
ID=$(run_on_nodes rotate.tmpl.json AWSPREVIOUS)
node_results "$ID"

aws secretsmanager update-secret-version-stage --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --version-stage AWSCURRENT \
  --move-to-version-id "$CURRENT_ID" \
  --remove-from-version-id "$PENDING_ID"
```

Read the status table in [Change the password in Neo4j](#change-the-password-in-neo4j) for both commands. Then repeat [Check the login](#check-the-login) and [Update the consumers](#update-the-consumers).

If you lost the two IDs, list them with their stages:

```bash
aws secretsmanager list-secret-version-ids --region "$REGION" \
  --secret-id "neo4j/$STACK/app-password" \
  --query 'Versions[].[VersionId,VersionStages]'
```

## Restore a volume after a rotation

The password in Neo4j lives on the data volume. A volume restored from a snapshot taken before the rotation holds the old password. After the restore, rerun [Change the password in Neo4j](#change-the-password-in-neo4j) with `AWSCURRENT` in place of `AWSPENDING`, then repeat [Check the login](#check-the-login).
