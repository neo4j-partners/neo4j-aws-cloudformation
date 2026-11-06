# Neo4j EE: Private, Existing VPC

`neo4j-private-existing-vpc.template.yaml` deploys a Neo4j Enterprise cluster into a VPC you supply.

- **What it deploys:** bastion, internal NLB, cluster ASGs, EBS volumes, security groups, SSM contract
- **What it does not deploy:** VPC, subnets, internet gateway and NAT Gateways. Outbound routing is the caller's responsibility.
- **You provide:** `VpcId` and `PrivateSubnet1Id/2Id/3Id` (one subnet per AZ for a 3-node cluster)
- **When to use:** the cluster must live inside an existing network, such as a peered VPC, a Transit Gateway, a shared services account, or a VPC managed by a separate infrastructure team.


## Guides

- [Operator Guide](operator-guide.md): connect to, check and operate a running stack. Start here if you deployed from AWS Marketplace.
- [Architecture](architecture.md): network layout, resources, routing and security design.
- [Local Deployment and Testing](local-testing.md): template-specific steps for deploying from source. General `deploy.py` behavior is in the [Local Testing guide](../../guides/local-testing.md).
