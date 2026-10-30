# Neo4j EE Marketplace: Architecture and CloudFormation Requirements

Reference document for the three-template split. Covers the key design decisions behind each template and the CloudFormation requirements AWS Marketplace enforces. For a description of each template's topology and target use case, see the [README](../README.md#templates).

---

## Network Security Guidance

AWS Marketplace AMI policy pages distinguish mandatory product requirements from recommended best practices. The current AMI requirements page does not appear to state a blanket hard rejection rule for every plaintext application protocol, but the official AMI best practices recommend encrypting network traffic with SSL/TLS where possible and using valid, up-to-date certificates.

The same best-practices page also tells sellers to document the minimum required ports and recommended source IP ranges for administrative access. Those controls are necessary, but they are not substitutes for encryption in transit. A security group `AllowedCIDR` restriction controls which source IPs can initiate a connection at the AWS network layer; it does not encrypt traffic. An attacker controlling a permitted client, a routed network segment, a traffic mirror, or a compromised workload in the same reachable private network can read Neo4j credentials, Cypher queries, and query results in plaintext.

The practical consequence for this listing: the templates restrict Neo4j Browser and Bolt access with `AllowedCIDR`, route private operator access through SSM, and retain optional Bolt TLS parameters for customers or local test flows that need encrypted Bolt on port 7687. The current templates do not implement stack-managed Browser HTTPS or a stack-managed public/private DNS alias.

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

Private and Private Existing VPC require `CertificateArn` and `AdvertisedDNS`. The NLB terminates TLS on Browser port 7473 and Bolt port 7687 using that ACM certificate. Public is plain TCP on 7474 and 7687 unless TLS is enabled with a certificate, in which case Browser moves to 7473.

### Plugin licenses and default-off contract

Bloom and Graph Data Science are gated behind two CFN parameters each: `InstallBloom` plus `BloomLicenseSecretArn`, and `InstallGDS` plus `GdsLicenseSecretArn`. Both `Install*` parameters default to `false`, and both `*LicenseSecretArn` parameters default to empty, so a default Marketplace launch installs neither plugin and grants no Secrets Manager access for licenses.

Two enforcement layers guard the contract:

- **Parameter-shape failures** are rejected by `AWS::CloudFormation::Rules`. Setting `InstallBloom=true` with an empty `BloomLicenseSecretArn` fails parameter validation before any resource is created, regardless of whether the stack is launched from the console, the API, the CLI, or Service Catalog. The same applies to GDS.
- **Runtime failures** are rejected by UserData via a `fail()` helper that calls `cfn-signal --success false` directly. Bash's `ERR` trap does not fire on `exit 1`, so any explicit failure path must signal CloudFormation before exiting; without `fail()` the stack would wait out the ASG signal timeout instead of failing fast. Cases covered: ARN pointing at a non-existent secret, ARN in a different region from the stack, secret payload empty or malformed, IAM `secretsmanager:GetSecretValue` denied, plugin JAR missing from the AMI.

The instance role grants `secretsmanager:GetSecretValue` for each license ARN through an `Fn::If` block that resolves to `AWS::NoValue` when the matching `Install*=true AND non-empty *LicenseSecretArn` condition is false. The `AllowedPattern` on each license ARN parameter is `secret:[A-Za-z0-9/_+=.@-]+`, the Secrets Manager secret-name character set; an earlier permissive pattern `secret:.+` admitted `arn:...:secret:*`, which CloudFormation would pass into the IAM `Resource` field and IAM would evaluate as a wildcard, broadening the grant beyond a single secret.

License material is never embedded in template content, parameter values, outputs, metadata, UserData, stack events, or stack drift output. Buyers pass only the ARN; UserData fetches the secret on every boot so license files survive ASG instance replacement on the root volume.

### AllowedCIDR default

Template 2 defaults `AllowedCIDR` to `10.0.0.0/16` because it creates that VPC. Template 3 has no default. Any CIDR default in Template 3 is likely wrong for most enterprise buyers because their VPC CIDR is unknown to the template. A wrong default silently misconfigures the security group. The parameter description instructs the buyer to enter their VPC CIDR explicitly, with examples (`10.0.0.0/16`, `172.16.0.0/12`).

### Operator bastion in private templates

Both Template 2 and Template 3 create a `t4g.small` bastion instance reachable only through AWS Systems Manager Session Manager. It has no ingress rules. The bastion is not optional: the NLB is internal and cluster nodes have no public IPs, so there is no direct path from an operator's machine without it. Template descriptions must explain that the bastion is created, what it does, and that it uses SSM rather than SSH. Buyers in environments with strict instance provisioning policies need this information before they launch.

### VPC endpoint creation in Template 3

Template 2 creates VPC interface endpoints for Systems Manager and Secrets Manager inside the VPC it creates. Template 3 deploys into a buyer-controlled VPC that may already have those endpoints. Creating a duplicate endpoint fails the deployment.

A single boolean parameter, `CreateVpcEndpoints`, defaults to `true`. When set to `false`, the caller supplies an existing endpoint security group via `ExistingEndpointSgId`. The parameter description states plainly: set this to `false` if your VPC already has these endpoints, otherwise the deployment will fail with a duplicate resource error.

### NumberOfServers

All three templates offer 1 or 3 nodes, default 3. Limiting Template 1 to a single node would prevent buyers who need to test clustering behavior before moving to a private deployment. The AllowedCIDR parameter already prevents unrestricted public access. Instance costs are the buyer's decision.

---

## CloudFormation Best Practices for Marketplace

### Security group CIDR restriction

Application ports must be restricted to a parameterized CIDR range, never open to `0.0.0.0/0`. The `AllowedCIDR` parameter must carry an `AllowedPattern` that rejects `0.0.0.0/0` explicitly:

```yaml
AllowedCIDR:
  Type: String
  AllowedPattern: '^(?!0\.0\.0\.0/0$)(\d{1,3}\.){3}\d{1,3}/\d{1,2}$'
  ConstraintDescription: The value 0.0.0.0/0 is not permitted.
```

The NLB security group applies this CIDR to Neo4j Browser and Bolt ports. The current templates use 7474 for Browser and 7687 for Bolt. The instance-facing external security group accepts those ports only from the NLB security group. Intra-cluster ports (5000, 6000, 7000, 7688, 2003, 2004, 3637) are restricted to the internal security group using a self-referential ingress rule. No port 22 ingress is created; operator access goes through SSM Session Manager.

### IAM least privilege

EC2 instances must use an IAM role, not long-term access keys. Each permission must name specific actions and scope resources to the minimum necessary:

- CloudFormation signaling: restrict to the current stack ARN using `!Sub`.
- EBS operations (`ec2:AttachVolume`, `ec2:DescribeVolumes`, `ec2:DescribeInstances`): resource `*` is acceptable because EBS volumes lack ARN-level scoping on all relevant APIs.
- Auto Scaling discovery (`autoscaling:DescribeAutoScalingGroups`): required for cluster node discovery; resource `*` is unavoidable.
- Secrets Manager (`secretsmanager:GetSecretValue`): scoped to the password secret ARN (`!Ref Neo4jPasswordSecret`). When `InstallBloom=true` and `BloomLicenseSecretArn` is non-empty, an `Fn::If` statement adds a grant scoped to that ARN; the same pattern applies to GDS via `GdsLicenseSecretArn`. Each license grant collapses to `AWS::NoValue` when the matching plugin is off or the ARN is empty, so a default Marketplace launch grants no Secrets Manager access for licenses.

### Auto Scaling groups for all topologies

Single-node deployments must still use an Auto Scaling group (MinSize: 1, MaxSize: 1, DesiredCapacity: 1). The ASG replaces a failed instance automatically and the new instance reattaches the persistent EBS data volume. Three-node deployments use one ASG per node, each pinned to a single AZ.

### Multi-AZ using dynamic selection

Subnets must reference AZs dynamically using `Fn::GetAZs` and `Fn::Select`. Never hardcode AZ names (`us-east-1a`, `us-west-2b`). Three-node deployments place one node in each of the first three AZs for the region:

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

Private subnet outbound internet access must use managed NAT gateways (`AWS::EC2::NatGateway`). NAT instances require patching, do not scale automatically, and represent a single point of failure. NAT gateways are AWS-managed, highly available within an AZ, and support high bandwidth without configuration.

For single-node deployments, one NAT gateway in one public subnet is sufficient. For three-node deployments, one NAT gateway per AZ prevents AZ-level failure from blocking outbound traffic for nodes in other AZs.

### Password handling

The `Password` parameter must carry `NoEcho: true` so it does not appear in the CloudFormation console or event log. Restrict `AllowedPattern` to alphanumerics only; lookahead-based patterns that admit arbitrary characters permit shell metacharacters (`$`, `` ` ``, `\`, `;`, `|`) which are evaluated inside double-quoted bash assignments and can execute arbitrary code as root at boot:

```yaml
Password:
  Type: String
  NoEcho: true
  MinLength: 8
  AllowedPattern: '^[a-zA-Z0-9]{8,}$'
  ConstraintDescription: Must be at least 8 characters and contain only letters and numbers (no special characters).
```

The password must not appear in the EC2 LaunchTemplate UserData. `NoEcho: true` only hides the value in CloudFormation API responses; it has no effect on UserData, which is stored in plaintext (base64-encoded) in the LaunchTemplate and is readable by any IAM principal with `ec2:DescribeLaunchTemplateVersions`. Instead, store the parameter value in a Secrets Manager secret and have the instance retrieve it at boot after the error trap is set:

```bash
password=$(aws secretsmanager get-secret-value \
  --secret-id "neo4j/${stackName}/password" \
  --query SecretString --output text \
  --region "${region}")
```

The instance IAM role must have `secretsmanager:GetSecretValue` scoped to the password secret ARN.

### Parameter grouping metadata

The `AWS::CloudFormation::Interface` metadata block controls how parameters appear in the CloudFormation console. Group parameters by concern so buyers do not encounter TLS certificate fields before they have configured basic cluster settings:

```yaml
Metadata:
  AWS::CloudFormation::Interface:
    ParameterGroups:
      - Label:
          default: "AWS Marketplace"
        Parameters:
          - ImageId
      - Label:
          default: "Cluster Configuration"
        Parameters:
          - NumberOfServers
          - InstanceType
          - DataDiskSize
      - Label:
          default: "Network Access"
        Parameters:
          - AllowedCIDR
      - Label:
          default: "Plugin licensing (Bloom, GDS)"
        Parameters:
          - InstallBloom
          - BloomLicenseSecretArn
          - InstallGDS
          - GdsLicenseSecretArn
      - Label:
          default: "TLS"
        Parameters:
          - CertificateArn
          - AdvertisedDNS
```

### Architectural diagrams

Each template requires a separate diagram submitted to the Marketplace seller portal. Dimensions: 1100x700 pixels. Use current AWS service icons. The diagram must accurately represent what that template deploys: a diagram showing NAT gateways and VPC endpoints is incorrect for the Public template; a diagram showing VPC creation is incorrect for the Existing VPC template.

---

## References

1. [Best practices for building AMIs for use with AWS Marketplace](https://docs.aws.amazon.com/marketplace/latest/userguide/best-practices-for-building-your-amis.html) — Official Marketplace AMI guidelines. Recommends SSL/TLS for network traffic where possible and valid, up-to-date certificates.

2. [CloudFormation Templates 101 for Sellers in AWS Marketplace](https://aws.amazon.com/blogs/awsmarketplace/cloudformation-templates-101-for-sellers-in-aws-marketplace/) — Covers IAM roles, Auto Scaling, password handling, and cluster deployment patterns for Marketplace sellers.

2. [Taking NAT to the Next Level in AWS CloudFormation Templates](https://aws.amazon.com/blogs/apn/taking-nat-to-the-next-level-in-aws-cloudformation-templates/) — NAT gateway vs. NAT instance comparison with CloudFormation examples; the case for per-AZ NAT gateways.

4. [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html) — Official Marketplace requirements: AMI parameter handling, network security, nested stack parameters, maximum template count per listing.

5. [AWS CloudFormation template guidelines for AMI-based products](https://aws.amazon.com/blogs/awsmarketplace/aws-cloudformation-template-guidelines-ami-based-products-aws-marketplace/) — Confirms templates are topology slots, not version slots; describes how Marketplace handles AMI parameter injection.

6. [NAT gateway use cases](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-scenarios.html) — Architecture diagrams, routing table configuration, and testing private subnet internet access via NAT.

7. [Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) — SSM Session Manager as the replacement for SSH-based bastion access; no inbound ports required.

8. [AWS Marketplace AMI buyer guide — topology selection](https://docs.aws.amazon.com/marketplace/latest/buyerguide/buyer-server-products.html) — Describes CloudFormation templates as topology selectors from the buyer's perspective.
