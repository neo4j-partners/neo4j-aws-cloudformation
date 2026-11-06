# Neo4j CE Documentation

Documentation for the Neo4j Community Edition CloudFormation template. For the quick start, deploy and test commands, see the [neo4j-ce README](../README.md).

## Architecture

- [Deployment Architecture](architecture/deployment-architecture.md): why the data volume is separate from compute, the design decisions, and the boot sequence.
- Diagrams in [`architecture/images/`](architecture/images/).

## Related documents

These stay next to the code they describe.

- [Marketplace](../marketplace/README.md): update the listing and build the base AMI.

## Conventions

- Files use lowercase kebab-case. A directory holds one kind of document: `architecture/` here, matching the layout under `neo4j-ee/docs/`.
- Each document starts with an `# Neo4j CE: ...` title.
- Link between documents with relative paths. After a move or rename, check that every link and anchor still resolves.
