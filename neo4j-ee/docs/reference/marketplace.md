# Neo4j EE: Marketplace Architecture and CloudFormation Requirements

Reference document for the three-template split. Covers the key design decisions behind each template and the CloudFormation requirements AWS Marketplace enforces. For a description of each template's topology and target use case, see the [README](../../README.md#templates).

---

## Network Security Guidance

AWS Marketplace AMI policy pages distinguish mandatory product requirements from recommended best practices. The current AMI requirements page does not appear to state a blanket hard rejection rule for every plaintext application protocol, but the official AMI best practices recommend encrypting network traffic with SSL/TLS where possible and using valid, up-to-date certificates.

The same best-practices page also tells sellers to document the minimum required ports and recommended source IP ranges for administrative access. Those controls are necessary, but they are not substitutes for encryption in transit. A security group `AllowedCIDR` restriction controls which source IPs can initiate a connection at the AWS network layer; it does not encrypt traffic. An attacker controlling a permitted client, a routed network segment, a traffic mirror, or a compromised workload in the same reachable private network can read Neo4j credentials, Cypher queries, and query results in plaintext.

The practical consequence for this listing: the templates restrict Neo4j Browser and Bolt access with `AllowedCIDR` and route private operator access through SSM. Private and Private Existing VPC always terminate TLS at the NLB, for Browser HTTPS on port 7473 and for Bolt on port 7687. Public serves plain HTTP on 7474 and plaintext Bolt on 7687 unless the buyer sets `EnableTLS` to `true`. The two private templates can create a Route 53 private DNS alias for `AdvertisedDNS` when `CreatePrivateDns` is `true`. No template creates a public DNS record.

Related Marketplace policy points:

- AMIs must comply with the current AWS Marketplace product requirements, and AWS says those policies are regularly updated to align with evolving security guidelines.
- Password-based authentication for instance services is prohibited, with limited exceptions for non-administrative application access when strong one-time passwords are used and changed immediately after first login.
- Products with deployment-time external dependencies must disclose those dependencies, and sellers are responsible for their availability and security.

References:

- [AMI-based product requirements for AWS Marketplace](https://docs.aws.amazon.com/marketplace/latest/userguide/product-and-ami-policies.html)
- [Best practices for building AMIs for use with AWS Marketplace](https://docs.aws.amazon.com/marketplace/latest/userguide/best-practices-for-building-your-amis.html)

---

## Key Design Decisions

### Bolt TLS availability

Private and Private Existing VPC require `CertificateArn` and `AdvertisedDNS`. Both parameters default to empty, and the `CertificateArnRequired` and `AdvertisedDnsRequired` rules reject a launch that leaves either one blank. The NLB terminates TLS on Browser port 7473 and Bolt port 7687 using that ACM certificate. Public is plain TCP on 7474 and 7687 unless `EnableTLS` is `true`. The `PublicTlsRequiresCertificateAndDns` rule then requires `CertificateArn` and `AdvertisedDNS`, and Browser moves to 7473.

### Plugin licenses and default-off contract

Bloom and Graph Data Science are gated behind two CFN parameters each: `InstallBloom` plus `BloomLicenseSecretArn`, and `InstallGDS` plus `GdsLicenseSecretArn`. Both `Install*` parameters default to `false`, and both `*LicenseSecretArn` parameters default to empty, so a default Marketplace launch installs neither plugin and grants no Secrets Manager access for licenses.

Two enforcement layers guard the contract:

- **Parameter-shape failures** are rejected by `AWS::CloudFormation::Rules`. Setting `InstallBloom=true` with an empty `BloomLicenseSecretArn` fails parameter validation before any resource is created, regardless of whether the stack is launched from the console, the API, the CLI, or Service Catalog. The same applies to GDS.
- **Runtime failures** are rejected by the bootstrap script, which fetches the license and copies the plugin JAR. Its `fail()` helper prints the error and exits non-zero. UserData runs with `set -euo pipefail`, so that non-zero exit trips the UserData `ERR` trap, which calls `cfn-signal --success false`. UserData is the only layer that calls `cfn-signal`. Its own `fail()` helper signals directly, because Bash's `ERR` trap does not fire on an explicit `exit 1`. Without these paths the stack would wait out the ASG signal timeout instead of failing fast. Cases covered: ARN pointing at a non-existent secret, ARN in a different region from the stack, empty secret payload, IAM `secretsmanager:GetSecretValue` denied, plugin JAR missing from the AMI.

The instance role grants `secretsmanager:GetSecretValue` for each license ARN through an `Fn::If` block that resolves to `AWS::NoValue` when the matching `Install*=true AND non-empty *LicenseSecretArn` condition is false. The `AllowedPattern` on each license ARN parameter is `secret:[A-Za-z0-9/_+=.@-]+`, the Secrets Manager secret-name character set; an earlier permissive pattern `secret:.+` admitted `arn:...:secret:*`, which CloudFormation would pass into the IAM `Resource` field and IAM would evaluate as a wildcard, broadening the grant beyond a single secret.

License material is never embedded in template content, parameter values, outputs, metadata, UserData, stack events, or stack drift output. Buyers pass only the ARN. The bootstrap fetches the secret each time an instance launches, so an ASG replacement instance writes the license files to its new root volume.

### AllowedCIDR default

Template 2 defaults `AllowedCIDR` to `10.0.0.0/16` because it creates that VPC. Template 3 has no default. Any CIDR default in Template 3 is likely wrong for most enterprise buyers because their VPC CIDR is unknown to the template. A wrong default silently misconfigures the security group. The parameter description instructs the buyer to enter their VPC CIDR explicitly, with examples (`10.0.0.0/16`, `172.16.0.0/12`).

### Operator bastion in private templates

Both Template 2 and Template 3 create a `t4g.small` bastion instance reachable only through AWS Systems Manager Session Manager. It has no ingress rules. The bastion is not optional: the NLB is internal and cluster nodes have no public IPs, so there is no direct path from an operator's machine without it. Template descriptions must explain that the bastion is created, what it does, and that it uses SSM rather than SSH. Buyers in environments with strict instance provisioning policies need this information before they launch.

### VPC endpoint creation in Template 3

Template 2 creates VPC interface endpoints for nine AWS services inside the VPC it creates: `ssm`, `ssmmessages`, `ec2messages`, `logs`, `secretsmanager`, `ec2`, `cloudformation`, `autoscaling` and `acm`. It also creates an S3 gateway endpoint. Node egress reaches AWS only through these endpoints. Template 3 deploys into a buyer-controlled VPC that may already have those interface endpoints. Creating a duplicate endpoint fails the deployment.

A single boolean parameter, `CreateVpcEndpoints`, defaults to `true`. When set to `false`, the caller supplies the security group attached to the existing endpoints via `ExistingEndpointSgId`. The `ExistingEndpointSgIdRequired` rule rejects a launch that sets `false` without it. The stack adds port 443 ingress from the Neo4j nodes and the operator bastion to that security group. The parameter description tells buyers to set `false` if their VPC already has these endpoints, because duplicates fail the deployment. It also warns that the existing endpoints must cover all nine services, or the nodes fail at boot.

### NumberOfServers

All three templates offer 1 or 3 nodes, default 3. Limiting Template 1 to a single node would prevent buyers who need to test clustering behavior before moving to a private deployment. The AllowedCIDR parameter already prevents unrestricted public access. Instance costs are the buyer's decision.

---

## CloudFormation Best Practices for Marketplace

### Security group CIDR restriction

Application ports must be restricted to a parameterized CIDR range, never open to `0.0.0.0/0`. The `AllowedCIDR` parameter must carry an `AllowedPattern` that rejects `0.0.0.0/0` explicitly. All three templates use the same pattern, which also requires each octet to be 0 to 255 and the prefix length to be 8 or more:

```yaml
AllowedCIDR:
  Type: String
  AllowedPattern: ^(?!0\.0\.0\.0\/0)(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(\/([89]|[1-2][0-9]|3[0-2]))$
  ConstraintDescription: Must be a valid CIDR range with a prefix length of 8 or more. 0.0.0.0/0 is not accepted.
```

The NLB security group applies this CIDR to Neo4j Browser and Bolt ports. Private and Private Existing VPC use 7473 for Browser over HTTPS and 7687 for Bolt. Public uses 7474 for Browser and 7687 for Bolt, and moves Browser to 7473 when `EnableTLS` is `true`. The instance-facing external security group accepts those ports only from the NLB security group. Intra-cluster ports (5000, 6000, 7000, 7688) are restricted to the internal security group using a self-referential ingress rule. No port 22 ingress is created; operator access goes through SSM Session Manager.

### IAM least privilege

EC2 instances must use an IAM role, not long-term access keys. Each permission must name specific actions and scope resources to the minimum necessary:

- CloudFormation signaling: restrict to the current stack ARN using `!Sub`.
- EBS attachment (`ec2:AttachVolume`): scoped to volume and instance ARNs in the stack's region and account. Tag conditions limit it to volumes tagged with this stack's `StackID` and `Role=neo4j-cluster-data`, and to instances tagged with this stack's `StackID`.
- Describe calls (`ec2:DescribeVolumes`, `ec2:DescribeInstances`, `ec2:DescribeTags`): resource `*` is unavoidable because these Describe APIs do not support resource-level permissions.
- Auto Scaling discovery (`autoscaling:DescribeAutoScalingGroups`): required for cluster node discovery; resource `*` is unavoidable.
- Secrets Manager (`secretsmanager:GetSecretValue`): scoped to the password secret ARNs (`!Ref Neo4jPasswordSecret`, `!Ref Neo4jAppPasswordSecret`). The stack KMS key encrypts both secrets, so the role also holds `kms:Decrypt` scoped to `!GetAtt Neo4jStackKey.Arn`. When `InstallBloom=true` and `BloomLicenseSecretArn` is non-empty, an `Fn::If` statement adds a grant scoped to that ARN; the same pattern applies to GDS via `GdsLicenseSecretArn`. Each license grant collapses to `AWS::NoValue` when the matching plugin is off or the ARN is empty, so a default Marketplace launch grants no Secrets Manager access for licenses.

### Auto Scaling groups for all topologies

Single-node deployments must still use an Auto Scaling group (MinSize: 1, MaxSize: 1, DesiredCapacity: 1). The ASG replaces a failed instance automatically and the new instance reattaches the persistent EBS data volume. Three-node deployments use one ASG per node, each pinned to a single AZ.

### Multi-AZ using dynamic selection

Subnets must reference AZs dynamically using `Fn::GetAZs` and `Fn::Select`. Never hardcode AZ names (`us-east-1a`, `us-west-2b`). By default, Public and Private three-node deployments place one node in each of the first three AZs for the region. The optional `AvailabilityZones` parameter overrides that choice for the subnets. Private Existing VPC places each node in the subnet the caller supplies:

```yaml
AvailabilityZone:
  Fn::Select:
    - 0
    - Fn::GetAZs: !Ref 'AWS::Region'
```

### IMDSv2 enforcement

The LaunchTemplate must set `HttpTokens: required`. This is a hard Marketplace requirement for AMI-based products:

```yaml
MetadataOptions:
  HttpTokens: required
  HttpEndpoint: enabled
```

### AMI ID via SSM Parameter Store

The `ImageId` parameter must use `AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>` type so the template resolves the correct AMI for each region. AWS Marketplace replaces this SSM parameter path with its own at subscription time. Hardcoded AMI IDs are region-specific and will fail outside the region where they were created.

### NAT gateways, not NAT instances

Private subnet outbound internet access must use managed NAT gateways (`AWS::EC2::NatGateway`). The Private template creates them only when `EnableInternetEgress` is `true`. The default is `false`, and the nodes then reach AWS only through VPC endpoints. NAT instances require patching, do not scale automatically, and represent a single point of failure. NAT gateways are AWS-managed, highly available within an AZ, and support high bandwidth without configuration.

For single-node deployments, one NAT gateway in one public subnet is sufficient. For three-node deployments, one NAT gateway per AZ prevents AZ-level failure from blocking outbound traffic for nodes in other AZs.

### Password handling

The `Password` parameter must carry `NoEcho: true` so it does not appear in the CloudFormation console or event log. Restrict `AllowedPattern` to alphanumerics only; lookahead-based patterns that admit arbitrary characters permit shell metacharacters (`$`, `` ` ``, `\`, `;`, `|`) which are evaluated inside double-quoted bash assignments and can execute arbitrary code as root at boot:

```yaml
Password:
  Type: String
  NoEcho: true
  MinLength: 12
  AllowedPattern: ^[a-zA-Z0-9]{12,}$
  ConstraintDescription: Must be at least 12 characters and contain only letters and numbers (no special characters).
```

The password must not appear in the EC2 LaunchTemplate UserData. `NoEcho: true` only hides the value in CloudFormation API responses; it has no effect on UserData, which is stored in plaintext (base64-encoded) in the LaunchTemplate and is readable by any IAM principal with `ec2:DescribeLaunchTemplateVersions`. Instead, store the parameter value in a Secrets Manager secret and have the instance retrieve it at boot after the error trap is set:

```bash
password=$(aws secretsmanager get-secret-value \
  --secret-id "neo4j/${stackName}/password" \
  --query SecretString --output text \
  --region "${region}")
```

The instance IAM role must have `secretsmanager:GetSecretValue` scoped to the password secret ARNs. The templates create two: `neo4j/<stack>/password` for the admin user and `neo4j/<stack>/app-password` for the application user. The role also needs `kms:Decrypt` on the stack KMS key that encrypts the secrets.

### Parameter grouping metadata

The `AWS::CloudFormation::Interface` metadata block controls how parameters appear in the CloudFormation console. Group parameters by concern so buyers do not encounter TLS certificate fields before they have configured basic cluster settings. The Private template groups its parameters this way:

```yaml
Metadata:
  AWS::CloudFormation::Interface:
    ParameterGroups:
      - Label:
          default: "AWS Marketplace Parameters"
        Parameters:
          - ImageId
      - Label:
          default: "Neo4j Configuration"
        Parameters:
          - Password
          - NumberOfServers
          - InstanceType
          - DataDiskSize
          - DataVolumeKmsKeyId
          - Node1SnapshotId
      - Label:
          default: "Plugin licensing (Bloom, GDS)"
        Parameters:
          - InstallBloom
          - BloomLicenseSecretArn
          - InstallGDS
          - GdsLicenseSecretArn
      - Label:
          default: "Network"
        Parameters:
          - AvailabilityZones
          - NlbDeletionProtection
          - EnableInternetEgress
      - Label:
          default: "TLS"
        Parameters:
          - CertificateArn
          - AdvertisedDNS
          - BoltTlsScheme
      - Label:
          default: "Private DNS"
        Parameters:
          - CreatePrivateDns
          - PrivateDnsZoneName
          - PrivateDnsHostedZoneId
      - Label:
          default: "Security & Monitoring"
        Parameters:
          - AllowedCIDR
          - AlertEmail
      - Label:
          default: "Single sign-on (OIDC)"
        Parameters:
          - OidcIssuerUri
          - OidcClientId
          - OidcBrowserClientId
          - OidcPrincipalClaim
          - OidcGroupsClaim
          - OidcAdminGroup
          - OidcReaderGroup
          - OidcGroupRoleMapping
```

Public replaces the TLS group with an "Optional Public TLS" group of `EnableTLS`, `CertificateArn` and `AdvertisedDNS`. It has no Private DNS group and no `EnableInternetEgress` parameter. Private Existing VPC replaces the Network group with an "Existing VPC" group for the VPC, subnet, endpoint, `AllowedCIDR` and NLB deletion protection parameters, and keeps `AlertEmail` in a "Monitoring" group.

### Architectural diagrams

Each template requires a separate diagram submitted to the Marketplace seller portal. Dimensions: 1100x700 pixels. Use current AWS service icons. The diagram must accurately represent what that template deploys: a diagram showing NAT gateways and VPC endpoints is incorrect for the Public template; a diagram showing VPC creation is incorrect for the Existing VPC template.

---

## References

1. [Best practices for building AMIs for use with AWS Marketplace](https://docs.aws.amazon.com/marketplace/latest/userguide/best-practices-for-building-your-amis.html) — Official Marketplace AMI guidelines. Recommends SSL/TLS for network traffic where possible and valid, up-to-date certificates.

2. [CloudFormation Templates 101 for Sellers in AWS Marketplace](https://aws.amazon.com/blogs/awsmarketplace/cloudformation-templates-101-for-sellers-in-aws-marketplace/) — Covers IAM roles, Auto Scaling, password handling, and cluster deployment patterns for Marketplace sellers.

3. [Taking NAT to the Next Level in AWS CloudFormation Templates](https://aws.amazon.com/blogs/apn/taking-nat-to-the-next-level-in-aws-cloudformation-templates/) — NAT gateway vs. NAT instance comparison with CloudFormation examples; the case for per-AZ NAT gateways.

4. [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html) — Official Marketplace requirements: AMI parameter handling, network security, nested stack parameters, maximum template count per listing.

5. [AWS CloudFormation template guidelines for AMI-based products](https://aws.amazon.com/blogs/awsmarketplace/aws-cloudformation-template-guidelines-ami-based-products-aws-marketplace/) — Confirms templates are topology slots, not version slots; describes how Marketplace handles AMI parameter injection.

6. [NAT gateway use cases](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-scenarios.html) — Architecture diagrams, routing table configuration, and testing private subnet internet access via NAT.

7. [Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) — SSM Session Manager as the replacement for SSH-based bastion access; no inbound ports required.

8. [AWS Marketplace AMI buyer guide — topology selection](https://docs.aws.amazon.com/marketplace/latest/buyerguide/buyer-server-products.html) — Describes CloudFormation templates as topology selectors from the buyer's perspective.
