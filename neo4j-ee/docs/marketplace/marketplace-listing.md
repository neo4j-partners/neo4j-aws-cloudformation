# Neo4j EE: Marketplace Listing for Version 2026.10.1-2

This plan covers the public **Neo4j Enterprise Edition** listing. Version 2026.10.1 replaced the single `neo4j.template.yaml` with two CloudFormation delivery options, Public and Private. It went live on 2026-11-15. Version 2026.10.1-2 carries the same AMI and fixes a stack name failure in both templates. See [Stack Name Failure in 2026.10.1](#stack-name-failure-in-2026101).

Related documents:

1. [Delivery option: Public](delivery-option-public.md)
2. [Delivery option: Private](delivery-option-private.md)
3. [Delivery option: Private Existing VPC](delivery-option-private-existing-vpc.md). This option is not listed.
4. [Marketplace architecture and CloudFormation requirements](../reference/marketplace.md)
5. [AMI build and publishing](../../marketplace/README.md)

## One Version, Two Delivery Options

Each submission is one new version with two delivery options, Public and Private. It is not two versions.

1. **Three templates per version:** AWS allows up to three AMI with CloudFormation delivery options in one version. See [Add a new version](https://docs.aws.amazon.com/marketplace/latest/userguide/single-ami-versions.html). A single AMI product holds at most three CloudFormation templates. See [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html).
2. **One request:** All delivery options must go in the same Add version request. A delivery option cannot be added to a version after it exists.
3. **Private Existing VPC is not listed:** The decision of 2026-11-16 is to leave the Private Existing VPC delivery option off the listing. Version 2026.10.1 shipped without it, and 2026.10.1-2 omits it too. The template stays in the repo for direct CloudFormation deployment.
4. **One AMI:** The version holds one AMI. Every delivery option consumes it. AWS Marketplace replaces the `ImageId` parameter in each template with its own SSM alias for that AMI.
5. **New version for any change:** The Update version information request changes only release notes, usage instructions and AMI usage or security group details. A changed template or AMI needs another new version with its own title and a fresh AMI scan.
6. **Dry run:** The Test "Add version" request scans the AMI without adding a version. Use it to catch scan findings before the real request.

## Current Listing State

The state below was read from the seller account `385155106615` on 2026-11-16.

| Item | Value |
|---|---|
| Product | Neo4j Enterprise Edition |
| Product ID | `prod-vwigx4rbvvb5s` |
| Entity ID | `513a3a85-20f7-4809-bb9f-aab293ce1e1d` |
| Product code | `4t4a9h39qkq4tbo3n9zd51p25` |
| Public versions | `2026.10.1`, created 2026-11-15, and `2025.12.0`, created 2025-12-17 |
| 2026.10.1 AMI | `ami-005b8f58c0af513d3`, the Marketplace copy of `ami-07c29b53e376c4184` |
| 2026.10.1 SSM alias | `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1` resolves to `ami-005b8f58c0af513d3` |
| 2026.10.1 delivery options | "Neo4j Enterprise Edition, Private" and "Neo4j Enterprise Edition, Public" |
| 2025.12.0 AMI | `ami-0c90416fa08820c34` |
| 2025.12.0 delivery option | One option titled "Neo4j Enterprise Edition" that deploys `neo4j.template.yaml` |
| Restricted versions | 4.4, 5.x, Latest CalVer, CalVer - latest, 2025, 2025.x |
| Region availability | 38 Regions, with future Region support set to All |

The 2026.10.1 short description, long description, usage instructions and recommended instance type match [delivery-option-private.md](delivery-option-private.md) and [delivery-option-public.md](delivery-option-public.md). The product-level description still holds the older marketing text.

## Stack Name Failure in 2026.10.1

Both 2026.10.1 templates fail at create when the stack name is long. Step 8 of the submission found the failure on 2026-11-16.

1. **Cause:** The templates named the Network Load Balancer `<StackName>-nlb` and the target groups `<StackName>-bolt-tg` and `<StackName>-https-tg`. Public names its browser target group `<StackName>-http-tg` when TLS is off. AWS limits each of these names to 32 characters. A stack name can be up to 128 characters.
2. **Effect:** Private fails for a stack name longer than 23 characters. Public fails for one longer than 24 characters. The stack rolls back after other resources have started. The 2026.10.1 version cannot be fixed in place, because a changed template needs a new version.
3. **Fix:** The three networking partials no longer set `Name` on the load balancer and the target groups. CloudFormation generates the names. No tooling looked these resources up by name.
4. **Guard:** `test_short_limit_names_are_not_built_from_stack_name` in `RenderedTemplateContractTests` fails when a load balancer, target group, IAM role, Lambda function or S3 bucket builds its name from `AWS::StackName`. It failed on all nine names before the fix.
5. **Stack updates:** `Name` is a replacement property. Updating a 2026.10.1 stack to 2026.10.1-2 replaces the load balancer and both target groups, and the load balancer DNS name changes. Private clients that connect through the private DNS record follow the change. Clients that use the load balancer DNS name directly must switch to the new name in the stack outputs.

### Customer Path Test of 2026.10.1

On 2026-11-16, a subscribed buyer account launched both published templates in us-east-1. Each launch used the template URL from the version and kept the `ImageId` default. The test stacks were deleted afterwards with `teardown.sh --delete-volumes`.

1. **Long stack names:** Private stack `test-ee-mpbuyer-1794839373` and Public stack `test-ee-mpbuyerpub-1794839442` failed at create. The target groups returned "Target group name ... cannot be longer than '32' characters."
2. **Private:** Stack `neo4j-mp-priv` passed preflight 12 of 12 and `validate-private` 20 of 20. It ran 3 `r8i.xlarge` nodes with a self-signed certificate and a private DNS record.
3. **Public:** Stack `neo4j-mp-pub` passed `test-neo4j` 37 of 38, including a follower replacement. Check 23, "AMI build-mode tag", failed because of the test, not the stack. It skipped Marketplace AMIs only when the deploy record had no `AmiId`, and `deploy.py --marketplace` always records `AmiId`. The check now passes with a note whenever `AmiSource` is `marketplace`.

## Version Information

| Field | Value |
|---|---|
| Version title | `2026.10.1-2` |
| Release notes | `https://neo4j.com/release-notes/` |

The version title must be exactly `2026.10.1-2`. AWS Marketplace builds the SSM alias from the title, so this title creates `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1-2`. Every template defaults `ImageId` to that path in `templates/src/parameters-common.yaml`. A different title leaves the default pointing at an alias that does not exist, and a launch that keeps the default fails. The alias does not exist yet.

The title keeps the Neo4j version visible. The `-2` suffix marks the second listing build of Neo4j 2026.10.1. The AMI and the Neo4j version are unchanged.

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
12. **Release gates after PR #146:** On 2026-11-15, fresh stacks from the fixed templates passed. These runs used the Public and Private templates before commit 175e187. That commit changed only the template Description and Metadata. Commit 28388d9 then changed the bastion in both Private templates. The Public resources these runs tested are the ones uploaded. The uploaded Private templates differ in the bastion, and no gate has run against them. Private Existing VPC was not run again, because only its parameter descriptions changed.
    1. Public passed `test-neo4j` 38 of 38.
    2. Private passed preflight 12 of 12 and the release suite 28 of 28.
13. **Bastion instance type fix:** The Marketplace scan rejected the Private template because resource `Neo4jOperatorBastion` used `t4g.small`. The scan requires every instance type in a template to be one of the product's instance types. The product lists only x86_64 types, and `t4g.small` is arm64. Commit 28388d9 changed the bastion in the Private and Private Existing VPC templates to `t3.medium` with the x86_64 Amazon Linux 2023 image. `t3.medium` is already on the product. The decision of 2026-11-15 is to resubmit without a release gate on the new bastion.
14. **Published as 2026.10.1:** AWS published version 2026.10.1 from this AMI on 2026-11-15. Marketplace's copy is `ami-005b8f58c0af513d3`. The customer path test of 2026-11-16 ran the new bastion. See [Customer Path Test of 2026.10.1](#customer-path-test-of-2026101).
15. **Reused in 2026.10.1-2:** The decision of 2026-11-16 is to carry this AMI into 2026.10.1-2. The stack name fix does not touch the AMI. Marketplace has not yet confirmed that it accepts a source AMI that another version already uses. The Test "Add version" request in step 4 answers that.

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

Every delivery option uses `ImageId` as the AMI parameter name and `r8i.xlarge` as the recommended instance type.

The Private Existing VPC template is not a delivery option. Its file, [delivery-option-private-existing-vpc.md](delivery-option-private-existing-vpc.md), keeps the drafted values in case the option is listed later.

### Architecture Diagram Size

The AWS pages disagree on diagram size.

1. **cloudformation.html:** [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html) asks for 1100x700 pixels, without stretching or cropping.
2. **single-ami-versions.html:** [Add a new version](https://docs.aws.amazon.com/marketplace/latest/userguide/single-ami-versions.html) allows up to 1560x878 at 16:9, or 1560x3120.
3. **Live listing:** The 2025.12.0 diagram `aws-3-instance-cluster.png` is 2366x3083. AWS accepted that image even though it exceeds both stated sizes.

All three new diagrams are 1100x700. That matches the first page exactly and fits inside the second page's 16:9 limit. `publish-assets.py` refuses to upload a diagram of any other size.

## Regions

AWS requires a test launch of each template through the CloudFormation console in every Region enabled for the product. See [Template prerequisites](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html#template-prerequisites). The live listing enables 38 Regions, and future Region support is set to All.

The decision of 2026-11-16 defers the Region restriction below. The listing keeps its 38 Regions for 2026.10.1-2, and the restriction stays an open item.

The decision of 2026-11-14 is to enable 9 Regions: us-east-1, us-east-2, us-west-2, eu-west-1, eu-central-1, eu-west-2, ap-northeast-1, ap-southeast-1 and ap-southeast-2. All 9 offer `r8i` and `t3.medium`. Test launches run in us-east-1 only. That departs from the AWS rule above. A reviewer may ask for test launches in the other 8 Regions.

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
2. **Bastion type:** The Private and Private Existing VPC templates add a `t3.medium` bastion. That type is offered in every checked Region.
3. **Opt-in Regions are untested:** The seller account cannot launch a test stack in a Region it has not enabled.
4. **Narrowing restricts 29 live Regions:** The Region list is a product-level setting, not part of the version. Narrowing it needs a separate Restrict Region request. Subscribed buyers keep using the product in a restricted Region. AWS blocks new instances there, which could stop Auto Scaling replacements for existing stacks. The decision of 2026-11-14 is to restrict them anyway.

## Connection Disclosure

The AWS Marketplace product usage policy requires disclosure of external dependencies and ongoing connections. The Seller Portal has no separate field for it. The disclosure appears in three places. Each delivery option file holds it inside the usage instructions. Each template's Description summarizes it. Each template's `ExternalConnectivityDisclosure` metadata lists every connection, with its caller, purpose, route and data sent.

Brief deploy steps come first, then a link to the topology's operator guide. A section titled "Connections this product makes" follows, with one labelled paragraph per source of traffic. If the AMI scan reports that the UserData script connects to external endpoints, reply by pointing to the usage instructions and the `ExternalConnectivityDisclosure` metadata.

The facts behind the paragraphs:

1. **AWS APIs:** The nodes call EC2, Auto Scaling, CloudFormation, Secrets Manager, Systems Manager and CloudWatch Logs over HTTPS. The Private templates reach these through VPC interface endpoints.
2. **Trust anchor:** The two Private templates read the certificate chain from Certificate Manager. They store the CA certificate in the Parameter Store parameter `/neo4j-ee/<stack>/ca-pem`. Public never does this.
3. **Amazon S3:** Node egress allows HTTPS to the same-Region S3 prefix list. The Private template adds an S3 gateway endpoint. The Private Existing VPC template creates none.
4. **No downloads:** APOC, GDS and Bloom are copied from the AMI. A deployment downloads no software.
5. **OIDC:** When `OidcIssuerUri` is set, Neo4j fetches the issuer's discovery document and signing keys over the internet.
6. **Usage report:** `templates/src/neo4j-base.conf` does not set `dbms.usage_report.enabled`. Neo4j therefore keeps its default and tries to send a usage report to `udc.neo4j.com`. On Public, the report reaches `udc.neo4j.com` through the internet gateway. On Private and Private Existing VPC, the node security group opens port 443 to the internet only when `OidcIssuerUri` is set. The report goes out only then. The decision of 2026-11-14 is to keep the report on and disclose it.
7. **Lambda:** The two Private templates run a Lambda function on stack create, update and delete. On create and update, it calls `ec2:DescribePrefixLists` to find the S3 prefix list ID.
8. **Bastion AMI:** The two Private templates launch the bastion from the public AWS parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`.
9. **Time sync:** A Private node checked on 2026-11-14 selects the Amazon Time Sync Service at 169.254.169.123. It also lists eight addresses from `pool time.aws.com`, which stay unreachable without internet egress. The sources come from generated files under `/run/chrony.d`.
10. **Updates:** The same node has no dnf timer and no `dnf-automatic` package. `update-motd.timer` runs every 12 to 24 hours. Its script `/etc/update-motd.d/70-available-updates` asks dnf whether updates exist and installs nothing. The node's `amazonlinux.repo` points dnf at the Regional S3 bucket `al2023-repos-<region>-de612dc2`. The `neo4j.repo` stays disabled.

## Publishing Script

`marketplace/publish-assets.py` uploads the six listing assets and prints the portal values. Run it from `neo4j-ee/`:

```bash
AWS_PROFILE=marketplace ./marketplace/publish-assets.py --version 2026.10.1-2
```

The script stops before any AWS call unless all of these hold:

1. Each template's `ImageId` default ends in `/2026.10.1-2`.
2. Each diagram is a 1100x700 PNG.
3. `templates/build.py --verify` passes.

It then confirms the caller is in account `385155106615` and asks before it changes anything. After confirmation it does the following:

1. **Ingestion role:** It checks that `aws_marketplace_ami_ingestion` exists with `AWSMarketplaceAmiIngestion` attached. It never creates or changes the role.
2. **Bucket:** It creates `neo4j-ee-marketplace-assets-385155106615` in us-east-1 if missing. ACLs stay blocked. A bucket policy grants public `s3:GetObject` on the bucket's objects.
3. **Uploads:** It uploads the three templates and three diagrams under the version prefix, for example `2026.10.1-2/`. The Private Existing VPC template and diagram are uploaded too, although the listing does not use them. It fetches each URL without credentials and stops unless it returns 200.
4. **Output:** It prints the version title, the role ARN and, for each delivery option, the template link and diagram link.

The release owner ran the script on 2026-11-14. It created the bucket `neo4j-ee-marketplace-assets-385155106615` and uploaded the six assets under `2026.10.1/`. The release owner ran it again on 2026-11-15, after PR #146 removed the `AvailabilityZones` parameter. That run replaced the six assets with the files from `main` at 287411b. The third run on 2026-11-15 followed commit 175e187, which added the connection disclosure to the templates. It replaced the six assets with the files from `main` at 175e187. The fourth run on 2026-11-15 followed commit 28388d9, which changed the bastion instance type. It replaced the six assets with the files from `main` at 28388d9. Each URL returned 200 without credentials. The script printed these version values:

| Field | Value |
|---|---|
| Version title | `2026.10.1` |
| IAM access role ARN | `arn:aws:iam::385155106615:role/aws_marketplace_ami_ingestion` |
| AMI parameter name | `ImageId` |

The script ran for 2026.10.1-2 on 2026-11-16 from commit `ee417a1`. The uploaded Public and Private templates are byte-identical to the committed templates. The templates and diagrams open without credentials.

## Submission Steps

Version 2026.10.1 completed steps 1 through 8 of the earlier plan. Step 8 found the stack name failure. The steps below cover 2026.10.1-2.

1. Run `./marketplace/test-ami.sh` against `ami-07c29b53e376c4184` and confirm every check passes. This step passed on 2026-11-14. The AMI is unchanged, so it does not run again.
2. Run the release gate for Public and Private from the fixed templates. Give each stack a name longer than 29 characters, so the gate proves the fix. See [validate-private/README.md](../../validate-private/README.md) for the Private gate. This step passed on 2026-11-16 in us-east-1, with 3 `t3.medium` nodes and the Marketplace defaults. Each stack launched the fixed template from the working tree with `ImageId` set to the 2026.10.1 alias, because the 2026.10.1-2 alias does not exist yet. That alias resolves to the same AMI. The test stacks were deleted afterwards.
    1. Private stack `neo4j-ee-gate-private-longname-1794844204` has a 41-character name. It passed preflight 12 of 12 and the release suite 28 of 28.
    2. Public stack `neo4j-ee-gate-public-longname-1794844204` has a 40-character name. It passed `test-neo4j` 37 of 38. Check 36, the cluster topology after the follower replacement, saw 2 of 3 servers Enabled. A query a few minutes later showed 3 Enabled servers and 3 online `neo4j` replicas. The wait before check 36 lasted 120 seconds and checked only server state. It now lasts up to 300 seconds, waits for the online replicas too, and logs a warning when it times out.
3. The release owner runs `publish-assets.py --version 2026.10.1-2` from the commit that holds the fix and keeps its output. This step ran on 2026-11-16 from commit `ee417a1`. The uploaded Public and Private templates match the committed templates, and every link opens without credentials.
4. In the Seller Portal, open the product and choose **Request changes**, then **Test "Add version"**. Enter the version and AMI fields from this document. Wait for the scan result. This dry run also shows whether Marketplace accepts the reused AMI. The real request in step 5 runs the same scan.
5. Fix any scan finding, then choose **Request changes**, then **Add new version** with the version and AMI fields from this document.
6. Add the Public and Private delivery options in that same request. Copy each field from its delivery option file and the script output.
7. After AWS publishes the version, confirm that `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.10.1-2` resolves to the new AMI.
8. Launch each delivery option once from a subscribed buyer account with the published template URL and the `ImageId` default. Use a stack name longer than 29 characters.
9. Restrict versions 2026.10.1 and 2025.12.0 so that new buyers see only 2026.10.1-2. Existing stacks on those versions keep running.

## Open Items

1. **Stack name failure:** Version 2026.10.1 is public and fails for long stack names. Steps 2 through 9 above replace it.
2. **Version 2025.12.0:** The decision of 2026-11-16 is to restrict it. Step 9 does that together with 2026.10.1.
3. **Region restriction:** The 9-Region restriction is deferred. The listing keeps 38 Regions with future Region support set to All.
