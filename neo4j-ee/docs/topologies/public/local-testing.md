# Neo4j EE Public: Local Deployment and Testing

## Basic Testing

The simplest possible deploy: a 3-node cluster on the default `t3.medium` instance with plain TCP on both Browser (7474) and Bolt (7687). No certificate, no DNS, no TLS flags.

```bash
cd neo4j-ee
./deploy.py --mode Public --region us-east-1
```

`deploy.py` restricts `AllowedCIDR` to `<your-public-ip>/32` automatically. The NLB DNS, password secret ARN, and a ready-to-run password retrieval command are written to `.deploy/<stack-name>.txt`. Clients connect with `neo4j://<NLB DNS>:7687` and Browser at `http://<NLB DNS>:7474`.

## Build

Regenerate the output template after editing any file in `templates/src/`:

```bash
cd neo4j-ee/templates
python build.py
```

Commit both the edited partial and the regenerated `neo4j-public.template.yaml`.

## Certificates

Public stacks default to plain TCP on Bolt. To terminate TLS at the NLB, create an ACM certificate first and pass its ARN to `deploy.py`. The `scripts/certificate.py` helper covers both flows and writes `.deploy/cert-<dns>.json` with the resulting ARN.

**Self-signed certificate (testing only)**

Generates an X.509 certificate with `openssl` and imports it into ACM. No domain ownership or DNS validation required. Clients must connect with `neo4j+ssc://` so the driver skips certificate validation.

```bash
./scripts/certificate.py --region us-east-1 --dns neo4j.test.local --selfsign
```

**DNS-validated certificate with an existing domain**

Requests a public ACM certificate validated by DNS. The `--dns` value can be any subdomain you control. For example, `neo4j-demo.example.com` works if `example.com` is in a Route 53 hosted zone on the same account.

```bash
# Route 53 zone on this account: auto-create the validation CNAME and poll until issued
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com --auto-route53

# DNS hosted elsewhere: print the CNAME, add it to your provider, then poll until issued
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com

# Print the CNAME and exit; rerun without --no-wait to poll for issuance
./scripts/certificate.py --region us-east-1 --dns neo4j-demo.example.com --no-wait
```

If the registered domain has no public hosted zone yet, create one and point the registrar at its NS records before requesting the certificate.

Pass the resulting ARN to `deploy.py` to enable TLS:

```bash
./deploy.py --mode Public --region us-east-1 \
  --number-of-servers 3 --enable-public-tls \
  --cert-arn <arn> --advertised-dns neo4j-demo.example.com
```

After the stack reaches `CREATE_COMPLETE`, look up the NLB DNS in `.deploy/<stack-name>.txt` (the `Neo4jInternalDNS` line) and UPSERT a CNAME from `--advertised-dns` to the NLB so the cert SAN matches what clients resolve. The advertised name must resolve to the NLB before any client opens a `neo4j+s://` connection.

```bash
aws route53 change-resource-record-sets \
  --hosted-zone-id <zone-id> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "neo4j-demo.example.com",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{"Value": "<NLB DNS from .deploy/<stack>.txt>"}]
      }
    }]
  }'

# Verify it resolves to the NLB (typically a few seconds in Route 53):
dig +short neo4j-demo.example.com @8.8.8.8
```

Clients then connect with `neo4j+s://neo4j-demo.example.com:7687`. The driver validates the server cert against the system CA bundle on the first attempt; `neo4j+ssc://` is not required and indicates the cert chain or SAN is wrong if used as a fallback.

On teardown, remove the advertised-dns CNAME with a matching `DELETE` change-batch so the record does not linger pointing at a non-existent NLB. The ACM certificate is reusable across deploys and stays in place.

## Deploy

```bash
cd neo4j-ee

# 3-node cluster, t3.medium, random region
./deploy.py --mode Public

# Single instance
./deploy.py --mode Public --number-of-servers 1

# Memory-optimized instance
./deploy.py --mode Public r8i.xlarge

# Pin region (avoids 10-20 min AMI copy)
./deploy.py --mode Public --region us-east-1

# Use the published Marketplace AMI
./deploy.py --mode Public --marketplace

# Enable CloudWatch alarm email notifications
./deploy.py --mode Public --alert-email you@example.com

# Optional TLS at the NLB (see Certificates above)
./deploy.py --mode Public --region us-east-1 --enable-public-tls \
  --cert-arn <arn> --advertised-dns <dns>
```

See [Local Testing guide](../../guides/local-testing.md) for AMI selection and the full flag behavior.

`deploy.py` detects your public IP automatically and restricts the security group to `<your-ip>/32`. Pass `--allowed-cidr` to override. The script writes outputs to `.deploy/<stack-name>.txt`.

Stack creation takes 5-10 minutes.

> **Note:** The test runner (`uv run test-neo4j --edition ee`) must execute from the same egress IP used at deploy time, or the security group blocks Bolt and HTTP connections. For CI, pass `--allowed-cidr` with a static egress IP at deploy time.

## Functional and Cluster Tests

Run the full test suite against a deployed stack:

```bash
cd ../test_neo4j
uv run test-neo4j --edition ee                     # most recent stack
uv run test-neo4j --edition ee --stack <name>      # specific stack
```

The suite covers connectivity, cluster topology, NLB scheme, volume configuration, security group rules, IMDSv2 enforcement, JDWP absence, and EBS resilience. All 29 functional checks pass on a healthy 3-node public stack.

## Tear Down

```bash
./teardown.sh <stack-name>
./teardown.sh --delete-volumes <stack-name>   # also permanently deletes EBS volumes
```
