# Neo4j EE Private: Local Deployment and Testing

For template developers deploying from source. The [Operator Guide](operator-guide.md) covers day-to-day stack usage once the stack is running.

## Build

Regenerate the output template after editing any file in `templates/src/`:

```bash
cd neo4j-ee/templates
python build.py
```

Commit both the edited partial and the regenerated `neo4j-private.template.yaml`.

## Deploy

See [Local Testing guide](../../guides/local-testing.md) for deploy commands, AMI selection, and the TLS and DNS options. The default mode is Private, so no `--mode` flag is needed.

For a deeper local check after template edits, run:

```bash
python3 templates/build.py --verify
python3 -m py_compile deploy.py
cfn-lint templates/neo4j-private.template.yaml
```

## Preflight and Basic Validation

```bash
cd neo4j-ee/validate-private

uv run preflight <stack-name>                  # 12 checks: stack, bastion, endpoints, tunnel

uv run validate-private                        # most recent deployment
uv run validate-private --stack <stack-name>   # specific deployment
```

`validate-private` runs 8 checks over an SSM tunnel through the bastion: Bolt connectivity, server edition, listen address, memory configuration, data directory, APOC, GDS, and cluster roles (1 writer, expected follower count). Each check takes 3-5 seconds. Total time under 35 seconds.

## Smoke Test

```bash
uv run scripts/smoke-write.py                       # 20 CREATE/DELETE iterations
uv run scripts/smoke-write.py <stack-name> 50       # custom iteration count
```

Runs write operations through the cluster with a local Neo4j driver over an SSM tunnel through the bastion. The driver routes each write to the current leader.

## Failover Suite

```bash
cd neo4j-ee/validate-private
uv run validate-private --stack <stack-name> --suite failover
```

The four cases stop and start the Neo4j service with `systemctl` via SSM. The suite terminates no instance. The node ASGs replace a node only when EC2 reports the instance impaired. A service restart therefore keeps the node on its instance.

| Case | What it does | Typical runtime |
|---|---|---|
| `follower-with-data` | Stop a follower, write data, restart, verify data visible | ~60 s |
| `leader` | Stop the leader, verify election, write on new leader | ~90 s |
| `rolling` | Stop each node in turn; cluster stays available throughout | ~4-15 min |
| `reads` | Stop two followers; verify reads still served by remaining nodes | ~90 s |

## Resilience Suite

```bash
cd neo4j-ee/validate-private
uv run validate-private --stack <stack-name> --suite resilience
```

Two cases that terminate EC2 instances and wait for ASG replacement:

| Case | What it does | Timeout |
|---|---|---|
| `single-loss` | Terminate 1 node; verify EBS reattach, sentinel data intact, quorum reforms | 900 s |
| `total-loss` | Terminate all 3 nodes; verify all 3 volumes reattach, sentinel intact | 1200 s |

Each case writes a sentinel file to the data volume before termination and verifies it survives on the replacement instance, confirming `DeletionPolicy: Retain` and NVMe device resolution both work.

The node ASGs use EC2 health checks. The NLB health check governs routing only. A hung Neo4j process therefore keeps its instance. The NLB stops routing to that node until an operator restarts the service.

## Tear Down

```bash
./teardown.sh <stack-name>
./teardown.sh --delete-volumes <stack-name>   # also permanently deletes EBS volumes
```

If the sample private app is deployed, tear it down first or stack deletion will stall on its security group ingress rules:

```bash
cd neo4j-ee/sample-private-app && ./teardown-sample-private-app.sh <stack>
cd neo4j-ee && ./teardown.sh --delete-volumes <stack>
```

## Troubleshooting

**"Bastion SSM PingStatus = Online" fails**
The bastion UserData may still be running in the first 3 minutes after stack creation. Retry after waiting. If the check still fails after 10 minutes, confirm the bastion's IAM role has `AmazonSSMManagedInstanceCore` and the VPC has `ssm` and `ssmmessages` interface endpoints.

**"AccessDenied" on `GetSecretValue` or `GetParameter`**
The operator tools read the secret and the SSM parameters with your own AWS credentials. The bastion role has no access to the secret. Grant your identity the permissions in [IAM permissions](operator-guide.md#prerequisites).

**"not on PATH: session-manager-plugin"**
Every `uv run` tool that connects to Neo4j opens an SSM port-forward from your machine. Install the Session Manager plugin as shown under [Prerequisites](operator-guide.md#prerequisites).

**"NotALeader" error in Neo4j Browser**
The NLB routed a write to a follower. Use `uv run admin-shell` for writes: `neo4j://` routing directs writes to the leader automatically.

**Mounting an existing EBS snapshot and resetting the password**

> **TL;DR:** Disable auth on every node, set the password via Cypher, then re-enable auth on every node. In a cluster, the password change succeeds only on the system database leader, which replicates it to every node.

When a data volume is restored from a snapshot, the Neo4j system database on that volume already contains the auth store from the original deployment. The password in Secrets Manager will not match, producing `Neo.ClientError.Security.Unauthorized` on every connection.

`neo4j-admin dbms set-initial-password` does not help here. It only takes effect when the system database has never been initialized. On a snapshot restore the system database already exists, so the command is silently ignored.

The correct procedure is to disable authentication temporarily, set the password via Cypher, then re-enable:

```bash
# 1. Remove any existing auth_enabled entries and disable auth
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "systemctl stop neo4j",
    "sed -i \"/dbms.security.auth_enabled/d\" /etc/neo4j/neo4j.conf",
    "echo \"dbms.security.auth_enabled=false\" >> /etc/neo4j/neo4j.conf",
    "systemctl start neo4j"
  ]' --region <region>

# 2. Once Neo4j is up, set the password to match Secrets Manager.
#    The node reads the secret with its own role, so the password never
#    appears in the SSM command. cypher-shell reads it from stdin as a
#    parameter. The password is alphanumeric by the template's AllowedPattern,
#    so the double quotes around it are safe.
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "export NEO4J_PASSWORD=\"\"",
    "for i in $(seq 1 30); do cypher-shell -a bolt+ssc://localhost:7687 -u neo4j \"RETURN 1\" >/dev/null 2>&1 && break || sleep 3; done",
    "pw=$(aws secretsmanager get-secret-value --secret-id neo4j/<stack-name>/password --query SecretString --output text --region <region>)",
    "printf \":param {pw: \\\"%s\\\"}\\nALTER USER neo4j SET PASSWORD \\$pw CHANGE NOT REQUIRED;\\n\" \"$pw\" | cypher-shell -a bolt+ssc://localhost:7687 -u neo4j"
  ]' --region <region>

# 3. Re-enable auth
aws ssm send-command --instance-ids <node-id> --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "systemctl stop neo4j",
    "sed -i \"/dbms.security.auth_enabled/d\" /etc/neo4j/neo4j.conf",
    "echo \"dbms.security.auth_enabled=true\" >> /etc/neo4j/neo4j.conf",
    "systemctl start neo4j"
  ]' --region <region>
```

Get `<node-id>` from the ASG:

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <Neo4jNode1ASGName> \
  --region <region> \
  --query 'AutoScalingGroups[0].Instances[*].InstanceId' \
  --output text
```

The `<Neo4jNode1ASGName>` value is in `.deploy/<stack-name>.txt`. A three-node cluster also has `Neo4jNode2ASGName` and `Neo4jNode3ASGName`.

**Three-node cluster:** The auth store lives in the replicated system database. `ALTER USER` succeeds only on the system database leader, and the system database replicates the change to every node. On a follower, step 2 fails with `51N29`: "The command 'ALTER USER' must be executed on the current 'LEADER' server". That error is expected. Run the steps in this order:

1. Run step 1 on every node. The leader cannot be chosen in advance, because restarting a node to disable auth moves leadership.
2. Run step 2 on every node. Exactly one node succeeds. The followers return `51N29`.
3. Run step 3 on every node.

This order needs no leader discovery. To find the leader anyway, run this query with cypher-shell on any node after step 1:

```cypher
SHOW DATABASE system YIELD serverID, writer WHERE writer RETURN serverID
```

Match the result against each node's server ID. The `server_id` file holds one version byte followed by the 16-byte UUID:

```bash
python3 -c "import uuid; print(uuid.UUID(bytes=open('/var/lib/neo4j/data/server_id','rb').read()[1:]))"
```

> **Note:** The restarts in steps 1 and 3 do not replace the node. The node ASGs replace a node only when EC2 reports the instance impaired. The NLB stops routing to a node while Neo4j is down and resumes once its TCP check passes.

> **Note:** `dbms.security.auth_enabled` may appear multiple times in `neo4j.conf` if the recovery procedure is run more than once. Neo4j 5 treats duplicate keys as a fatal config error and refuses to start. The `sed -i "/dbms.security.auth_enabled/d"` step above removes all occurrences before adding a single clean entry, which prevents this.

Reference: [Recover admin user and password, Neo4j Operations Manual](https://neo4j.com/docs/operations-manual/current/authentication-authorization/password-and-user-recovery/)
