# CloudWatch Memory Monitoring for Neo4j EE

## Checking current memory usage

```bash
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START=$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)

aws cloudwatch get-metric-statistics \
  --namespace CWAgent \
  --metric-name mem_used_percent \
  --dimensions Name=InstanceId,Value=<instance-id> \
  --start-time $START \
  --end-time $END \
  --period 60 \
  --statistics Average \
  --region <region>
```

Get the instance ID from `.deploy/<stack-name>.txt` via the ASG:

```bash
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names <Neo4jNode1ASGName from .deploy file> \
  --region <region> \
  --query 'AutoScalingGroups[0].Instances[*].[InstanceId,HealthStatus]'
```

Verify the agent is installed and reporting before querying:

```bash
aws cloudwatch list-metrics \
  --namespace CWAgent \
  --dimensions Name=InstanceId,Value=<instance-id> \
  --region <region>
```

## Enabling memory metrics on a running stack

The AMI already contains the CloudWatch Agent, and the bootstrap starts it with a config that
ships the security, debug, and cloud-init logs. The agent does not collect memory metrics,
and the instance role has no permission to publish them. Two steps add them.

Do not replace the agent config with `fetch-config` or the `AmazonCloudWatch-ManageAgent`
`configure` action. Either one overwrites the bootstrap config and stops the log collection
that the failed-authentication alarm depends on. Append a second config instead.

**1. Allow the instance role to publish agent metrics**

Add an inline policy limited to the `CWAgent` namespace. This is narrower than the managed
`CloudWatchAgentServerPolicy`, which also grants log group creation and SSM parameter reads.

```bash
# Get the role name from the instance profile
PROFILE_NAME=<stack-name>-Neo4jInstanceProfile-<suffix>
ROLE=$(aws iam get-instance-profile \
  --instance-profile-name $PROFILE_NAME \
  --query 'InstanceProfile.Roles[0].RoleName' --output text)

aws iam put-role-policy \
  --role-name $ROLE \
  --policy-name Neo4jCWAgentMetrics \
  --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "cloudwatch:PutMetricData",
    "Resource": "*",
    "Condition": { "StringEquals": { "cloudwatch:namespace": "CWAgent" } }
  }]
}'
```

**2. Append the metrics config on each instance**

```bash
aws ssm send-command \
  --instance-ids <instance-id> \
  --document-name AWS-RunShellScript \
  --parameters '{"commands": [
    "cat > /tmp/cw-memory.json <<EOF",
    "{\"metrics\": {\"append_dimensions\": {\"InstanceId\": \"\\${aws:InstanceId}\"}, \"metrics_collected\": {\"mem\": {\"measurement\": [\"mem_used_percent\"]}, \"disk\": {\"measurement\": [\"disk_used_percent\"], \"resources\": [\"/\"]}}}}",
    "EOF",
    "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a append-config -m ec2 -s -c file:/tmp/cw-memory.json",
    "rm -f /tmp/cw-memory.json"
  ]}' \
  --region <region>
```

Check the result with the `get-command-invocation` call shown earlier, then query the metric.
Nodes that the ASG replaces start from the bootstrap config, so repeat step 2 on a new node.

## Why this matters for Neo4j EE

Neo4j's page cache and GDS in-memory graph projections consume most available RAM. The EC2
`mem_used_percent` metric is the only way to observe this from outside the instance. The
standard EC2 CloudWatch namespace does not include memory. At 88%+ utilization on an
`r8i.xlarge` (32 GB), there is insufficient headroom for concurrent GDS workloads.

The templates do not collect memory metrics by default. Adding that to the template would need
`cloudwatch:PutMetricData` in the instance role and a metrics section in the bootstrap agent
config.
