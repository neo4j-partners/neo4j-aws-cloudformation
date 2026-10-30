# templates/

## Convention

| Location | Purpose |
|---|---|
| `templates/` | Generated CloudFormation templates — **do not edit directly** |
| `templates/src/` | Source partials — **edit these** |

To regenerate output templates after editing a partial:

```bash
python templates/build.py
```

## Output templates

| File | Marketplace display name | Target buyer |
|---|---|---|
| `neo4j-public.template.yaml` | Public | Proof of concept, demos, evaluation |
| `neo4j-private.template.yaml` | Private | Production and staging, AWS-managed networking |
| `neo4j-private-existing-vpc.template.yaml` | Private, Existing VPC | Enterprise with pre-existing VPC infrastructure |

## Source partials (src/)

Each output template is assembled by `build.py` from a fixed list of partials in `src/` (parameter blocks, conditions, IAM, security groups, networking, observability, ASG, userdata). Edit the partials and regenerate; do not edit the output templates directly.

All three templates share `src/userdata.sh`. Boot-time helper functions live in `src/partials/` and are inlined into the generated CloudFormation UserData block.
