# Neo4j EE: Public

`neo4j-public.template.yaml` deploys a Neo4j Enterprise cluster in public subnets with an internet-facing Network Load Balancer.

- **What it deploys:** Neo4j EE cluster (1 or 3 nodes) in public subnets behind an internet-facing NLB
- **Public exposure:** every instance has a public IP; client traffic gated by `AllowedCIDR` at the NLB security group
- **Operator access:** You connect directly from your machine. There is no bastion and no SSM tunnel.
- **When to use:** proof-of-concept, demos, evaluation. Production and regulated workloads should use the [Private template](../private/README.md)


## Guides

- [Operator Guide](operator-guide.md): connect to, check and operate a running stack. Start here if you deployed from AWS Marketplace.
- [Architecture](architecture.md): network layout, resources, routing and security design.
- [Local Deployment and Testing](local-testing.md): template-specific steps for deploying from source. General `deploy.py` behavior is in the [Local Testing guide](../../guides/local-testing.md).
