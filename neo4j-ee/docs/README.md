# Neo4j EE Documentation

Documentation for the Neo4j Enterprise Edition CloudFormation templates. For the template overview, parameters and quick start, see the [neo4j-ee README](../README.md).

## Pick your starting point

| You are | Start here |
|---|---|
| Operating a stack deployed from AWS Marketplace | The Operator Guide for your topology, below |
| Deploying from source to test a change | [Local Testing guide](guides/local-testing.md), then your topology's Local Deployment and Testing doc |
| Changing the templates, AMI, UserData or bootstrap | [Template Architecture](architecture/template-architecture.md), the binding contract |

## Topologies

Each topology has the same four documents.

| Topology | Overview | Operator Guide | Architecture | Local Deployment and Testing |
|---|---|---|---|---|
| Public | [README](topologies/public/README.md) | [Operator Guide](topologies/public/operator-guide.md) | [Architecture](topologies/public/architecture.md) | [Local Testing](topologies/public/local-testing.md) |
| Private | [README](topologies/private/README.md) | [Operator Guide](topologies/private/operator-guide.md) | [Architecture](topologies/private/architecture.md) | [Local Testing](topologies/private/local-testing.md) |
| Private, Existing VPC | [README](topologies/private-existing-vpc/README.md) | [Operator Guide](topologies/private-existing-vpc/operator-guide.md) | [Architecture](topologies/private-existing-vpc/architecture.md) | [Local Testing](topologies/private-existing-vpc/local-testing.md) |

## Guides

Task-oriented, apply to more than one topology.

- [Local Testing](guides/local-testing.md): deploy a test stack from source with `deploy.py`.
- [EBS Snapshots](guides/ebs-snapshots.md): snapshot the data volumes of a deployed stack.
- [Memory Monitoring](guides/memory-monitoring.md): read CloudWatch memory metrics and enable them on a running stack.

## Run books

Ordered procedures with a rollback or teardown.

- [OIDC Sign-In](run-books/oidc.md): deploy a Private cluster with single sign-on, including the Hume integration.
- [App Password Rotation](run-books/app-password-rotation.md): rotate the `neo4j_app` password on a running Private or Existing VPC stack.

## Reference

- [Single Sign-On (OIDC) Reference](reference/oidc-sign-in.md): settings, token design, group mapping and sign-in troubleshooting.
- [Marketplace Requirements](reference/marketplace.md): the CloudFormation and network requirements AWS Marketplace enforces.

## Architecture

- [Template Architecture](architecture/template-architecture.md): the AMI, Template, UserData and Bootstrap contract. Read it before changing a template.
- Diagrams in [`architecture/images/`](architecture/images/).

## Conventions

- Files use lowercase kebab-case. A directory holds one kind of document: `guides/`, `run-books/`, `reference/`, `architecture/`, `topologies/`.
- Each document starts with an `# Neo4j EE: ...` title.
- Link between documents with relative paths. After a move or rename, check that every link and anchor still resolves.
