# Neo4j EE: Delivery Option for the Public Template

This file holds the Seller Portal values for the Public delivery option in version `2026.10.1`. The version fields and the AMI fields are in [marketplace-listing.md](marketplace-listing.md).

## Fields

| Field | Value |
|---|---|
| Delivery option title | Neo4j Enterprise Edition, Public |
| Recommended instance type | `r8i.xlarge` |
| CloudFormation template link | `https://neo4j-ee-marketplace-assets-385155106615.s3.us-east-1.amazonaws.com/2026.10.1/neo4j-public.template.yaml` |
| AMI parameter name | `ImageId` |
| Architecture diagram link | `https://neo4j-ee-marketplace-assets-385155106615.s3.us-east-1.amazonaws.com/2026.10.1/neo4j-public-architecture.png` |
| Source template | `templates/neo4j-public.template.yaml` |
| Source diagram | `marketplace/neo4j-public-architecture.png`, 1100x700 |

## Short Description

```text
Deploys a 1 or 3 node Neo4j Enterprise Edition cluster in a new VPC with public subnets, behind an internet-facing Network Load Balancer.
```

## Long Description

```text
Deploys a cluster of 1 or 3 Neo4j Enterprise Edition nodes in a new VPC with public subnets. Each node runs in its own Auto Scaling group and keeps its data on a retained EBS volume. An internet-facing Network Load Balancer serves Neo4j Browser and Bolt. The load balancer accepts traffic only from the AllowedCIDR range you supply. TLS is optional. Set EnableTLS to true and supply an ACM certificate to serve Browser and Bolt over TLS. Use this option for evaluation and for workloads that clients reach over the internet.
```

## Usage Instructions

Paste this text into the delivery option's Usage instructions field. It is 2,538 characters, under the 4,000 limit.

```text
How to deploy

1. Choose a Password of 12 or more letters and numbers.
2. Set AllowedCIDR to the range that may reach Neo4j, such as your office IP as x.x.x.x/32. 0.0.0.0/0 is rejected.
3. TLS is off by default. For TLS, set EnableTLS to true and supply CertificateArn, an ACM certificate in this Region, and AdvertisedDNS, the name on it.
4. InstallBloom and InstallGDS default to false. To install one, pass its license secret ARN from Secrets Manager.
5. Open the Neo4jHttpsUrl output and sign in as neo4j. Neo4jPasswordRetrieveCommand prints the password.

Deleting the stack keeps the data volumes, secrets, KMS key and log bucket.

Full guide: https://github.com/neo4j-partners/neo4j-aws-cloudformation/blob/main/neo4j-ee/docs/topologies/public/operator-guide.md

Connections this product makes

On deployment: The product downloads nothing. The AMI carries Neo4j, APOC, Graph Data Science and Bloom.

Inside the instance: The boot script reads a session token, the instance id and the Availability Zone from the EC2 instance metadata service at 169.254.169.254. The instance gets its role credentials from the same address. The clock syncs with the Amazon Time Sync Service at 169.254.169.123.

Operating system: Every 12 to 24 hours, Amazon Linux 2023 asks the Amazon Linux repository in your Region whether updates exist. It installs nothing.

Boot script: The boot script calls AWS CloudFormation to read its configuration and signal the stack. It reads the Neo4j password and any license secrets from AWS Secrets Manager, which decrypts them with AWS KMS. It calls Amazon EC2 and EC2 Auto Scaling to find the cluster members and attach the data volume.

Agents: The Amazon CloudWatch agent sends the Neo4j logs to Amazon CloudWatch Logs. The AWS Systems Manager agent calls Systems Manager for Session Manager.

Run by AWS for the stack: CloudFormation creates the VPC, the load balancer, the KMS key, the secrets, the CloudWatch alarms and an Amazon SNS topic. The load balancer writes access logs to an S3 bucket in your account. VPC flow logs go to CloudWatch Logs. The product's code calls none of these services.

Scope: These calls use the stack's own IAM roles and stay in your account and Region. The nodes reach the Regional AWS endpoints over the internet on port 443.

Usage report: Neo4j sends a usage report to udc.neo4j.com.

Identity provider: When you set OidcIssuerUri, Neo4j fetches the provider's discovery document and signing keys over HTTPS.

Customer data: Neo4j sends no graph data outside your account.
```
