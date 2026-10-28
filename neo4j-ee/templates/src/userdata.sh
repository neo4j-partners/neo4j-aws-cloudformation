_token=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
_instance_id=$(curl -s -H "X-aws-ec2-metadata-token: $_token" http://169.254.169.254/latest/meta-data/instance-id)
_az=$(curl -s -H "X-aws-ec2-metadata-token: $_token" http://169.254.169.254/latest/meta-data/placement/availability-zone)

# ERR traps do not run for explicit `exit 1`, so fail() signals CloudFormation
# before exiting to avoid waiting for the ASG signal timeout.
fail() {
  echo "ERROR: $*" >&2
  if [[ -n "${logicalId:-}" ]]; then
    cfn-signal --success false --stack "$stackName" --resource "$logicalId" --region "$region" || true
  fi
  exit 1
}

[[ -n "${_instance_id}" && -n "${_az}" ]] || fail "IMDSv2 metadata unavailable."
logicalId=$(aws ec2 describe-tags --region "$region" --filters "Name=resource-id,Values=${_instance_id}" "Name=key,Values=aws:cloudformation:logical-id" --query "Tags[0].Value" --output text 2>/dev/null || true)
trap 'if [[ -n "${logicalId:-}" ]]; then cfn-signal --success false --stack "$stackName" --resource "$logicalId" --region "$region"; fi' ERR
_stack_id=$(aws ec2 describe-tags --region "$region" \
  --filters "Name=resource-id,Values=${_instance_id}" "Name=key,Values=aws:cloudformation:stack-id" \
  --query "Tags[0].Value" --output text 2>/dev/null || true)
[[ -n "${_stack_id}" && "${_stack_id}" != "None" ]] || fail "stack-id tag missing."

password=$(aws secretsmanager get-secret-value \
  --secret-id "neo4j/${stackName}/password" \
  --query SecretString --output text \
  --region "${region}")

# cfn-init writes the template-owned bootstrap and base conf from the shared
# LaunchTemplate's AWS::CloudFormation::Init metadata. A non-zero exit trips
# the ERR trap and signals failure before reaching cfn-signal --success true,
# per NFR-9. The instance role already grants cloudformation:DescribeStack*.
cfn-init --stack "$stackName" --resource Neo4jLaunchTemplate --region "$region"

# Hand every runtime value to the bootstrap as named, exported environment
# variables. The Secrets Manager password travels by env, never on argv, so it
# is not exposed in the process list or cloud-init logs.
export stackName region nodeCount loadBalancerDNSName advertisedDNS
export installGDS installBloom gdsLicenseSecretArn bloomLicenseSecretArn
export password _stack_id _instance_id _az
export oidcIssuerUriB64 oidcClientIdB64 oidcBrowserClientIdB64 oidcPrincipalClaimB64
export oidcGroupsClaimB64 oidcAdminGroupB64 oidcReaderGroupB64

/opt/neo4j/bin/neo4j-bootstrap.sh

# cfn-signal shebang (#!/usr/bin/python3 -s) requires cfnbootstrap under python3.9;
# switch the default only after signaling so cfn-signal can find its dependencies.
cfn-signal --success true --stack "$stackName" --resource "$logicalId" --region "$region"
alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 100
