# Neo4j EE: Marketplace Listing for Version 2026.10.1

This plan covers the next version of the public **Neo4j Enterprise Edition** listing. That version replaces the single `neo4j.template.yaml` with three CloudFormation templates. All three share one AMI.

Related documents:

1. [Delivery option: Public](delivery-option-public.md)
2. [Delivery option: Private](delivery-option-private.md)
3. [Delivery option: Private Existing VPC](delivery-option-private-existing-vpc.md)
4. [Marketplace architecture and CloudFormation requirements](../reference/marketplace.md)
5. [AMI build and publishing](../../marketplace/README.md)

## One Version, Three Delivery Options

The submission is one new version with three delivery options. It is not three versions.

1. **Three templates per version:** AWS allows up to three AMI with CloudFormation delivery options in one version. See [Add a new version](https://docs.aws.amazon.com/marketplace/latest/userguide/single-ami-versions.html). A single AMI product holds at most three CloudFormation templates. See [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html).
2. **One request:** All three delivery options must go in the same Add version request. A delivery option cannot be added to a version after it exists.
3. **One AMI:** The version holds one AMI. Every delivery option consumes it. AWS Marketplace replaces the `ImageId` parameter in each template with its own SSM alias for that AMI.
4. **New version for any change:** The Update version information request changes only release notes, usage instructions and AMI usage or security group details. A changed template or AMI needs another new version with its own title and a fresh AMI scan.
5. **Dry run:** The Test "Add version" request scans the AMI without adding a version. Use it to catch scan findings before the real request.

## Current Listing State

The state below was read from the seller account `385155106615` on 2026-11-14.

| Item | Value |
|---|---|
| Product | Neo4j Enterprise Edition |
| Product ID | `prod-vwigx4rbvvb5s` |
| Entity ID | `513a3a85-20f7-4809-bb9f-aab293ce1e1d` |
| Product code | `4t4a9h39qkq4tbo3n9zd51p25` |
| Public version | `2025.12.0`, created 2025-12-17 |
| Public AMI | `ami-0c90416fa08820c34` |
| Public delivery option | One option titled "Neo4j Enterprise Edition" that deploys `neo4j.template.yaml` |
| Restricted versions | 4.4, 5.x, Latest CalVer, CalVer - latest, 2025, 2025.x |
| Region availability | 37 Regions, with future Region support set to All |

The live usage instructions read "After deployment, the Neo4j web console will be accesible on port 7474." That text fits none of the new templates. Each delivery option file holds replacement text.

## Version Information

| Field | Value |
|---|---|
| Version title | `2026.10.1` |
| Release notes | `https://neo4j.com/release-notes/` |

The version title must be exactly `2026.10.1`. AWS Marketplace builds the SSM alias from the title, so this title creates `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1`. Every template defaults `ImageId` to that path in `templates/src/parameters-common.yaml`. A different title leaves the default pointing at an alias that does not exist, and a launch that keeps the default fails. The alias does not exist yet.

## AMI Fields

| Field | Value |
|---|---|
| Amazon Machine Image ID | `ami-07c29b53e376c4184` |
| IAM access role ARN | `arn:aws:iam::385155106615:role/aws_marketplace_ami_ingestion` |
| Operating system | Amazon Linux |
| Operating system version | Amazon Linux 2023 |
| OS user name | `ec2-user` |
| Scanning port | `22` |
| Architecture | x86_64 |

### AMI Status

`ami-07c29b53e376c4184` is the build recorded in `marketplace/ami-id.txt`.

1. **Build:** The AMI is named `neo4j-ee-base-20261104`. It was created on 2026-11-04 in marketplace mode.
2. **Neo4j version:** The `Neo4jVersion` tag reads `2026.10.1`.
3. **Snapshot:** The root snapshot `snap-07b8cdbf5a083aa76` is unencrypted, as AWS Marketplace requires. It is in us-east-1 and in the seller account.
4. **IMDSv2:** The AMI sets `ImdsSupport` to `v2.0`.
5. **Ingestion role:** The role exists and carries the `AWSMarketplaceAmiIngestion` managed policy.
6. **In use:** Test stack launch templates reference the AMI while those stacks exist. The `create-ami.sh` guard refuses to deregister an AMI in that state.
7. **Test result:** `marketplace/test-ami.sh` passed every check against this AMI on 2026-11-14. The eMudhra trust store audit passed for the system store, the Java store and the AWS CLI bundle.
8. **Pre-fix release gates:** On 2026-11-14 in us-east-1, with Marketplace defaults and 3 `t3.medium` nodes, all three topologies passed. These runs used the templates before the output fix. They do not validate the templates to be uploaded.
   1. Private stack `test-ee-1794707600` passed preflight 12 of 12 and the release suite 28 of 28.
   2. Private Existing VPC stack `test-ee-1794707770` passed preflight 12 of 12 and the release suite 28 of 28.
   3. Public stack `test-ee-1794708577` passed `test-neo4j` 38 of 38, including a follower replacement.
9. **Output fix:** One earlier Public stack failed at create while resolving the `Neo4jHttpsUrl` output. The output read its value back from the stack's SSM parameter through `!GetAtt`. The parameter existed, and the cause is unconfirmed. The fix builds `Neo4jHttpsUrl` and `Neo4jBoltUrl` from the same expression as their SSM parameters. A contract test asserts that each output matches its parameter. Output names and values are unchanged.
10. **Release gates after the output fix:** On 2026-11-14 in us-east-1, with Marketplace defaults and 3 `t3.medium` nodes, all three topologies passed. The three stacks were created at the same time, the condition of the earlier failure. These runs used the templates before PR #146. The test stacks were deleted afterwards.
    1. Public stack `test-ee-1794711123` passed `test-neo4j` 38 of 38.
    2. Private stack `test-ee-1794711119` passed preflight 12 of 12 and the release suite 28 of 28.
    3. Private Existing VPC stack `test-ee-1794711126` passed preflight 12 of 12 and the release suite 28 of 28.
11. **Availability Zone fix:** The Marketplace test scan rejected the Public and Private templates with "Availability zones must not be hardcoded." The finding named the subnets, which took their zones from the `AvailabilityZones` parameter. PR #146 removed that parameter. The subnets now pick their zones with `Fn::Select` over `Fn::GetAZs`. In Private Existing VPC, only the three `PrivateSubnetNId` parameter descriptions changed.
12. **Release gates after PR #146:** On 2026-11-15, fresh stacks from the fixed templates passed. These runs validate the uploaded Public and Private templates. Private Existing VPC was not run again, because only its parameter descriptions changed.
    1. Public passed `test-neo4j` 38 of 38.
    2. Private passed preflight 12 of 12 and the release suite 28 of 28.

### Instance Types

The templates allow `t3.medium` and every `r8i` size from `r8i.large` through `r8i.96xlarge`. The default and recommended type is `r8i.xlarge`.

The live version also lists `m4`, `m5`, `r5`, `r6i`, `r6a` and `r7a` types. Every type the templates allow is already on the listing, so the listing needs no change.

1. **Buyer choice:** A CloudFormation buyer picks the instance type from the template's `InstanceType` parameter in the CloudFormation console. The extra listing types never reach the template.
2. **Removing types:** Removing the older types from the listing could block Auto Scaling replacements for 2025.12.0 stacks that run on them. Leave them on.
3. **t3.medium:** The `t3.medium` type has 2 vCPUs and 4 GiB of burstable capacity. Neo4j's production minimum is 8 GB of memory. Use `t3.medium` for evaluation only.
4. **Smallest production type:** The `r8i.large` type has 16 GiB. It is the smallest production choice.

Every type the listing enables must also be offered in every enabled Region.

## AMI Delivery Option

The 2025.12.0 version offers only a CloudFormation delivery option. Leave the standalone AMI delivery option off for 2026.10.1 as well. A buyer launching the AMI alone gets a host with Neo4j installed and the service disabled. Nothing on that host configures or starts Neo4j.

## Delivery Options

| Delivery option | Template | Diagram | File |
|---|---|---|---|
| Public | `templates/neo4j-public.template.yaml` | `marketplace/neo4j-public-architecture.png` | [delivery-option-public.md](delivery-option-public.md) |
| Private | `templates/neo4j-private.template.yaml` | `marketplace/neo4j-private-architecture.png` | [delivery-option-private.md](delivery-option-private.md) |
| Private Existing VPC | `templates/neo4j-private-existing-vpc.template.yaml` | `marketplace/neo4j-private-existing-vpc-architecture.png` | [delivery-option-private-existing-vpc.md](delivery-option-private-existing-vpc.md) |

Every delivery option uses `ImageId` as the AMI parameter name and `r8i.xlarge` as the recommended instance type.

### Architecture Diagram Size

The AWS pages disagree on diagram size.

1. **cloudformation.html:** [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html) asks for 1100x700 pixels, without stretching or cropping.
2. **single-ami-versions.html:** [Add a new version](https://docs.aws.amazon.com/marketplace/latest/userguide/single-ami-versions.html) allows up to 1560x878 at 16:9, or 1560x3120.
3. **Live listing:** The 2025.12.0 diagram `aws-3-instance-cluster.png` is 2366x3083. AWS accepted that image even though it exceeds both stated sizes.

All three new diagrams are 1100x700. That matches the first page exactly and fits inside the second page's 16:9 limit. `publish-assets.py` refuses to upload a diagram of any other size.

## Regions

AWS requires a test launch of each template through the CloudFormation console in every Region enabled for the product. See [Template prerequisites](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html#template-prerequisites). The live listing enables 37 Regions, and future Region support is set to All.

The decision of 2026-11-14 is to enable 9 Regions: us-east-1, us-east-2, us-west-2, eu-west-1, eu-central-1, eu-west-2, ap-northeast-1, ap-southeast-1 and ap-southeast-2. All 9 offer `r8i`, `t3.medium` and `t4g.small`. Test launches run in us-east-1 only. That departs from the AWS rule above. A reviewer may ask for test launches in the other 8 Regions.

Future Region support changes from All to None. New Regions then stay off until they are added on purpose.

An instance type offering check ran on 2026-11-14 from the seller account.

| Group | Regions | Result |
|---|---|---|
| Default Regions with r8i | us-east-1, us-east-2, us-west-1, us-west-2, ca-central-1, sa-east-1, eu-west-1, eu-west-2, eu-west-3, eu-central-1, eu-north-1, ap-northeast-1, ap-northeast-2, ap-south-1, ap-southeast-1, ap-southeast-2 | `r8i`, `t3.medium` and `t4g.small` are offered |
| Osaka | ap-northeast-3 | No `r8i`. Only `t3.medium` and `t4g.small` are offered |
| Opt-in Regions | af-south-1, ap-east-1, ap-east-2, ap-south-2, ap-southeast-3 through ap-southeast-7, ca-west-1, eu-central-2, eu-south-1, eu-south-2, il-central-1, me-central-1, me-west-1, mx-central-1 | Not checked. The Regions are not enabled in the seller account |
| Bahrain | me-south-1 | Not checked. The call timed out |
| GovCloud | us-gov-east-1, us-gov-west-1 | Not checked. GovCloud needs a separate account |

What the check means:

1. **Osaka fails by default:** The default `r8i.xlarge` does not exist in ap-northeast-3. A buyer there must pick `t3.medium`. The 9-Region decision removes Osaka from the listing.
2. **Bastion type:** The Private and Private Existing VPC templates add a `t4g.small` bastion. That type is offered in every checked Region.
3. **Opt-in Regions are untested:** The seller account cannot launch a test stack in a Region it has not enabled.
4. **Narrowing restricts 28 live Regions:** The Region list is a product-level setting, not part of the version. Narrowing it needs a separate Restrict Region request. Subscribed buyers keep using the product in a restricted Region. AWS blocks new instances there, which could stop Auto Scaling replacements for existing stacks. The decision of 2026-11-14 is to restrict them anyway.

## Connection Disclosure

The AWS Marketplace product usage policy requires disclosure of external dependencies and ongoing connections. The Seller Portal has no separate field for it. Each delivery option file holds the disclosure inside its usage instructions.

Brief deploy steps come first, then a link to the topology's operator guide. A section titled "Connections this product makes" follows, with one labelled paragraph per source of traffic. If the AMI scan reports that the UserData script connects to external endpoints, reply by pointing to the usage instructions.

The facts behind the paragraphs:

1. **AWS APIs:** The nodes call EC2, Auto Scaling, CloudFormation, Secrets Manager, Systems Manager and CloudWatch Logs over HTTPS. The Private templates reach these through VPC interface endpoints.
2. **Trust anchor:** The two Private templates read the certificate chain from Certificate Manager. They store the CA certificate in the Parameter Store parameter `/neo4j-ee/<stack>/ca-pem`. Public never does this.
3. **Amazon S3:** Node egress allows HTTPS to the same-Region S3 prefix list. The Private template adds an S3 gateway endpoint. The Private Existing VPC template creates none.
4. **No downloads:** APOC, GDS and Bloom are copied from the AMI. A deployment downloads no software.
5. **OIDC:** When `OidcIssuerUri` is set, Neo4j fetches the issuer's discovery document and signing keys over the internet.
6. **Usage report:** `templates/src/neo4j-base.conf` does not set `dbms.usage_report.enabled`. Neo4j therefore keeps its default and tries to send a usage report to `udc.neo4j.com`. On Public, the report reaches `udc.neo4j.com` through the internet gateway. On Private and Private Existing VPC, the node security group opens port 443 to the internet only when `OidcIssuerUri` is set. The report goes out only then. The decision of 2026-11-14 is to keep the report on and disclose it.
7. **Lambda:** The two Private templates run a Lambda function on stack create, update and delete. On create and update, it calls `ec2:DescribePrefixLists` to find the S3 prefix list ID.
8. **Bastion AMI:** The two Private templates launch the bastion from the public AWS parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64`.
9. **Time sync:** A Private node checked on 2026-11-14 selects the Amazon Time Sync Service at 169.254.169.123. It also lists eight addresses from `pool time.aws.com`, which stay unreachable without internet egress. The sources come from generated files under `/run/chrony.d`.
10. **Updates:** The same node has no dnf timer and no `dnf-automatic` package. `update-motd.timer` runs every 12 to 24 hours. Its script `/etc/update-motd.d/70-available-updates` asks dnf whether updates exist and installs nothing. The node's `amazonlinux.repo` points dnf at the Regional S3 bucket `al2023-repos-<region>-de612dc2`. The `neo4j.repo` stays disabled.

## Publishing Script

`marketplace/publish-assets.py` uploads the six listing assets and prints the portal values. Run it from `neo4j-ee/`:

```bash
AWS_PROFILE=marketplace ./marketplace/publish-assets.py --version 2026.10.1
```

The script stops before any AWS call unless all of these hold:

1. Each template's `ImageId` default ends in `/2026.10.1`.
2. Each diagram is a 1100x700 PNG.
3. `templates/build.py --verify` passes.

It then confirms the caller is in account `385155106615` and asks before it changes anything. After confirmation it does the following:

1. **Ingestion role:** It checks that `aws_marketplace_ami_ingestion` exists with `AWSMarketplaceAmiIngestion` attached. It never creates or changes the role.
2. **Bucket:** It creates `neo4j-ee-marketplace-assets-385155106615` in us-east-1 if missing. ACLs stay blocked. A bucket policy grants public `s3:GetObject` on the bucket's objects.
3. **Uploads:** It uploads the three templates and three diagrams under the `2026.10.1/` prefix. It fetches each URL without credentials and stops unless it returns 200.
4. **Output:** It prints the version title, the role ARN and, for each delivery option, the template link and diagram link.

The release owner ran the script on 2026-11-14. It created the bucket `neo4j-ee-marketplace-assets-385155106615` and uploaded the six assets under `2026.10.1/`. The release owner ran it again on 2026-11-15, after PR #146 removed the `AvailabilityZones` parameter. That run replaced the six assets with the files from `main` at 287411b. Each URL returned 200 without credentials. Each delivery option file holds its template link and diagram link. The script printed these version values:

| Field | Value |
|---|---|
| Version title | `2026.10.1` |
| IAM access role ARN | `arn:aws:iam::385155106615:role/aws_marketplace_ami_ingestion` |
| AMI parameter name | `ImageId` |

## Submission Steps

1. Run `./marketplace/test-ami.sh` against `ami-07c29b53e376c4184` and confirm every check passes. This step passed on 2026-11-14.
2. Run the release gate for each template. See [validate-private/README.md](../../validate-private/README.md) for the Private gate. All three passed after the output fix on 2026-11-14. Public and Private passed again after PR #146 on 2026-11-15. Private Existing VPC was not run again.
3. The release owner runs `publish-assets.py` and keeps its output. This step ran on 2026-11-14 and again on 2026-11-15.
4. This step is optional. In the Seller Portal, open the product and choose **Request changes**, then **Test "Add version"**. Enter the version and AMI fields from this document. Wait for the scan result. The real request in step 5 runs the same scan.
5. Fix any scan finding, then choose **Request changes**, then **Add new version** with the version and AMI fields from this document.
6. Add the three delivery options in that same request. Copy each field from its delivery option file and the script output.
7. After AWS publishes the version, confirm that `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1` resolves to the new AMI.
8. Launch each delivery option once from the listing page.
9. Submit a Restrict Region request for the 28 live Regions outside the 9, and set future Region support to None.
10. Restrict version 2025.12.0 so that new buyers see only 2026.10.1.

## Open Items

No items are open.
