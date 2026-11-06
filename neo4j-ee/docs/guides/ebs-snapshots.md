# Neo4j EE: EBS Snapshots

`snapshot.sh` creates EBS snapshots of all Neo4j data volumes for a deployed stack. Snapshots are incremental and stored in AWS-managed S3; snapshot size reflects used blocks only, not the allocated volume size.

```bash
./snapshot.sh                  # most recent deployment
./snapshot.sh <stack-name>     # specific deployment
./snapshot.sh --list           # list all snapshots for the most recent deployment
./snapshot.sh --list <stack-name>  # list all snapshots for a specific deployment
```

The script reads `.deploy/<stack-name>.txt` for volume IDs and region. Each snapshot is tagged with the stack name and date. For a three-node cluster all three volumes are snapshotted in the same run.

`--list` queries by the `stack` tag and shows snapshot ID, timestamp, state, progress, volume size, and description.

**Note on memory.** The `neo4j-admin database backup` recovery phase runs inside the Neo4j JVM and competes with the running database for heap. On an `r8i.xlarge` (32 GB) with a 19.3 GB heap and a 21.5 GB store, the combined footprint exceeds available RAM and triggers an OOM kill. EBS snapshots do not touch the JVM; they are taken at the block-device level and are safe to run on any instance size without restarting Neo4j.
