# Neo4j EE: Delivery Option for the Private Template

This file holds the Seller Portal values for the Private delivery option in version `2026.10.1-2`. The version fields and the AMI fields are in [marketplace-listing.md](marketplace-listing.md).

## Fields

| Field | Value |
|---|---|
| Delivery option title | Neo4j Enterprise Edition, Private |
| Recommended instance type | `r8i.xlarge` |
| CloudFormation template link | `https://neo4j-ee-marketplace-assets-385155106615.s3.us-east-1.amazonaws.com/2026.10.1-2/neo4j-private.template.yaml` |
| AMI parameter name | `ImageId` |
| Architecture diagram link | `https://neo4j-ee-marketplace-assets-385155106615.s3.us-east-1.amazonaws.com/2026.10.1-2/neo4j-private-architecture.png` |
| Source template | `templates/neo4j-private.template.yaml` |
| Source diagram | `marketplace/neo4j-private-architecture.png`, 1100x700 |

## Short Description

```text
Deploys a 1 or 3 node Neo4j Enterprise Edition cluster into private subnets of a new VPC, with TLS and an SSM-only operator bastion.
```

## Long Description

```text
Deploys a cluster of 1 or 3 Neo4j Enterprise Edition nodes into private subnets of a new VPC. Each node runs in its own Auto Scaling group and keeps its data on a retained EBS volume. An internal Network Load Balancer terminates TLS for Neo4j Browser and Bolt with your ACM certificate. A t3.medium operator bastion is reachable only through AWS Systems Manager Session Manager, with no SSH key and no inbound rules. The stack creates VPC endpoints for the AWS services the nodes call, so the nodes need no internet access. Set EnableInternetEgress to true to add NAT gateways. Use this option for production workloads that stay inside your AWS network.
```

## Usage Instructions

Paste this text into the delivery option's Usage instructions field. It is 3,279 characters, under the 4,000 limit.

```text
How to deploy

1. Choose a Password of 12 or more letters and numbers.
2. TLS is required. Supply CertificateArn, an ACM certificate in this Region, and AdvertisedDNS, the name on it. Set CreatePrivateDns to true to map that name in Route 53.
3. EnableInternetEgress defaults to false. OIDC sign-in needs it set to true.
4. InstallBloom and InstallGDS default to false. To install one, pass its license secret ARN from Secrets Manager.
5. The cluster has no public address. Run the Neo4jSSMHTTPSCommand output with the Session Manager plugin, map AdvertisedDNS to 127.0.0.1, and open https://<AdvertisedDNS>:7473. Neo4jPasswordRetrieveCommand prints the password.

Deleting the stack keeps the data volumes, secrets, KMS key and log bucket.

Full guide: https://github.com/neo4j-partners/neo4j-aws-cloudformation/blob/main/neo4j-ee/docs/topologies/private/operator-guide.md

External Connections this product makes

On deployment: The product downloads nothing. The AMI carries Neo4j, APOC, Graph Data Science and Bloom.

Inside the instance: The boot script reads a session token, the instance id and the Availability Zone from the EC2 instance metadata service at 169.254.169.254. The instance gets its role credentials from the same address. The clock syncs with the Amazon Time Sync Service at 169.254.169.123.

Operating system: Every 12 to 24 hours, Amazon Linux 2023 asks the Amazon Linux repository in your Region whether updates exist. It installs nothing.

Boot script: The boot script calls AWS CloudFormation to read its configuration and signal the stack. It reads the Neo4j password and any license secrets from AWS Secrets Manager, which decrypts them with AWS KMS. It calls Amazon EC2 and EC2 Auto Scaling to find the cluster members and attach the data volume. It reads the certificate chain from AWS Certificate Manager and stores the CA certificate in Systems Manager Parameter Store.

Agents: The Amazon CloudWatch agent sends the Neo4j logs to Amazon CloudWatch Logs. The AWS Systems Manager agent calls Systems Manager for Session Manager.

Lambda function: One AWS Lambda function runs on stack create, update and delete. On create and update, it looks up the Region's S3 prefix list in Amazon EC2. It sends each result to CloudFormation through a pre-signed Amazon S3 URL. It runs outside the VPC.

Bastion: The operator bastion launches from the public Amazon Linux 2023 parameter in Systems Manager. It holds no Neo4j credentials.

Run by AWS for the stack: CloudFormation creates the VPC, the VPC endpoints, the load balancer, the KMS key, the secrets, the CloudWatch alarms and an Amazon SNS topic. The load balancer writes access logs to an S3 bucket in your account. VPC flow logs go to CloudWatch Logs. The product's code calls none of these services.

Scope: These calls use the stack's own IAM roles and stay in your account and Region. The nodes reach AWS through the stack's VPC interface endpoints and its S3 gateway endpoint.

Internet egress: The nodes reach the internet only when OidcIssuerUri is set. OidcIssuerUri needs EnableInternetEgress set to true. Neo4j then fetches the provider's discovery document and signing keys over HTTPS. It also sends a usage report to udc.neo4j.com.

Customer data: Neo4j sends no graph data outside your account.
```
