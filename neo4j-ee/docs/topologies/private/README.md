# Neo4j EE: Private

`neo4j-private.template.yaml` deploys a Neo4j Enterprise cluster in private subnets behind an internal Network Load Balancer.

- **What it deploys:** Neo4j EE cluster (1 or 3 nodes) in private subnets behind an internal NLB
- **Public exposure:** No instance has a public IP.
- **Operator access:** dedicated `t4g.small` bastion via AWS Systems Manager Session Manager
- **When to use:** production, staging, and regulated workloads


## Guides

- [Operator Guide](operator-guide.md): connect to, check and operate a running stack. Start here if you deployed from AWS Marketplace.
- [Architecture](architecture.md): network layout, resources, routing and security design.
- [Local Deployment and Testing](local-testing.md): template-specific steps for deploying from source. General `deploy.py` behavior is in the [Local Testing guide](../../guides/local-testing.md).
- [Single Sign-On (OIDC) Reference](../../reference/oidc-sign-in.md) and [OIDC Run Book](../../run-books/oidc.md): optional sign-in through an identity provider.
- [App Password Rotation Run Book](../../run-books/app-password-rotation.md): rotate the `neo4j_app` password on a running stack.
- [EBS Snapshots](../../guides/ebs-snapshots.md): snapshot the data volumes of a deployed stack.
