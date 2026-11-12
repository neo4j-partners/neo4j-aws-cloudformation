# Marketplace

Internal instructions for Neo4j employees managing the EE Marketplace listing.

## Scripts

| Script | Purpose |
|---|---|
| `create-ami.sh` | Builds the base AMI: resolves the latest AL2023 AMI, patches OS, installs static deployment tooling, hardens SSH, creates AMI, writes ID to `ami-id.txt` |
| `test-ami.sh` | Verifies the AMI via SSM Run Command (no SSH required): checks SSH hardening, OS identity, and that the eMudhra roots are absent from the system, Java and AWS CLI trust stores |

## Updating the AMI

Run from the `neo4j-ee/` directory against the `marketplace` AWS profile:

```bash
AWS_PROFILE=marketplace ./marketplace/create-ami.sh
```

The script resolves the latest Amazon Linux 2023 AMI from SSM, patches it with
`dnf update -y`, installs static deployment tooling, hardens SSH, creates the
AMI in `us-east-1`, enforces IMDSv2, and writes the new AMI ID to
`marketplace/ami-id.txt`.

For default-account iteration before Marketplace submission, just run the
script against any non-Marketplace account:

```bash
./marketplace/create-ami.sh
```

The build mode defaults from the caller's account: `marketplace` only in
account `385155106615`, `iteration` everywhere else. The single guard is that
`marketplace` mode refuses to run outside that account; iteration builds just
run. Set `AMI_BUILD_MODE` explicitly only to override the account-derived
default.

Then test it:

```bash
AWS_PROFILE=marketplace ./marketplace/test-ami.sh
```

The test script launches a temporary instance from the AMI, runs verification
checks over SSM, reports pass/fail, and terminates the instance on exit.

## Submitting a New Version to Marketplace

After the AMI passes testing:

1. Upload the three EE templates and one architecture diagram per template to
   an S3 bucket in `us-east-1` that the Marketplace seller account owns.
   AWS Marketplace copies them to its own buckets at submission.

```
s3://<bucket>/neo4j-private.template.yaml
s3://<bucket>/neo4j-public.template.yaml
s3://<bucket>/neo4j-private-existing-vpc.template.yaml
```

   Each diagram must be 1100x700 pixels and show only what its template
   deploys. The Private diagram is `marketplace/neo4j-private-architecture.png`.

2. Open the [AWS Marketplace Seller Portal](https://aws.amazon.com/marketplace/management/) and navigate to:
   **Products > Server > Request changes > Update versions > Add version**

3. Fill in the version form:
   - **AMI ID:** the ID from `marketplace/ami-id.txt`
   - **IAM access role ARN:** `arn:aws:iam::385155106615:role/aws_marketplace_ami_ingestion`
   - **CloudFormation template links:** the three S3 URLs from step 1
   - **Architecture diagrams:** the S3 URL of each template's diagram from step 1

4. Submit for scanning. AWS will validate the AMI against Marketplace security
   requirements. Once approved, publish the new version.

The `ImageId` parameter in the templates uses `AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>`.
AWS Marketplace injects the correct SSM parameter path at subscription time.
There is no AMI ID to update in the templates themselves.

The `ImageId` default in `templates/src/parameters-common.yaml` names a version, for example `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1`. Publish each new version with that same SSM alias. Otherwise, a deploy that relies on the default fails at parameter resolution.

## Listing Description Updates

When publishing a version that changes the plugin or license contract, edit the
listing description and "How to Use" / parameter guidance in the AWS Marketplace
Seller Portal so buyers see the current contract before launch. The templates
default to:

- `InstallBloom=false` and `InstallGDS=false`. A default launch installs neither
  plugin and grants no Secrets Manager access for licenses.
- Enabling Bloom or Graph Data Science requires a Secrets Manager secret
  created in advance, whose ARN is passed as `BloomLicenseSecretArn` or
  `GdsLicenseSecretArn`.
- Stacks that request a plugin without supplying its license ARN are rejected
  at parameter validation by `AWS::CloudFormation::Rules` before any resource
  is created. Runtime fetch or install failures cause `CREATE_FAILED` within
  minutes via `cfn-signal --success false` rather than waiting out the ASG
  signal timeout.

The Seller Portal description is not generated from the templates; update it
directly when these defaults change.
