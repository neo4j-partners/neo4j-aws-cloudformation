import json
import logging
import os
import random
import socket
import ssl
import time
import urllib.request

import boto3
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError

log = logging.getLogger(__name__)

ssm = boto3.client("ssm")
sm = boto3.client("secretsmanager")

_driver = None


def _base_scheme():
    return "bolt" if os.environ.get("NEO4J_NUMBER_OF_SERVERS") == "1" else "neo4j"


def _bolt_scheme():
    base = _base_scheme()
    if os.environ.get("NEO4J_BOLT_TLS") == "true":
        return f"{base}+ssc"
    return base


def _nlb_dns():
    return ssm.get_parameter(
        Name=os.environ["NEO4J_SSM_NLB_PATH"]
    )["Parameter"]["Value"]


def _password():
    return sm.get_secret_value(
        SecretId=os.environ["NEO4J_SECRET_ARN"]
    )["SecretString"]


def _init_driver():
    return GraphDatabase.driver(
        f"{_bolt_scheme()}://{_nlb_dns()}:7687",
        auth=("neo4j", _password()),
    )


def _get_driver():
    global _driver
    if _driver is None:
        _driver = _init_driver()
    return _driver


def _reset_driver():
    global _driver
    if _driver is not None:
        _driver.close()
    _driver = _init_driver()
    return _driver


_MERGE_FINTECH = """
MERGE (c1:Customer {id: 'c1', name: 'Alice Chen', segment: 'SMB'})
MERGE (c2:Customer {id: 'c2', name: 'Bob Patel', segment: 'Enterprise'})
MERGE (c3:Customer {id: 'c3', name: 'Carol Wu', segment: 'SMB'})

MERGE (a1:Account {id: 'acc1', type: 'checking', balance: 84200.00})
MERGE (a2:Account {id: 'acc2', type: 'checking', balance: 210000.00})
MERGE (a3:Account {id: 'acc3', type: 'savings',  balance: 55000.00})

MERGE (m1:Merchant {id: 'm1', name: 'StripePayments', category: 'payments'})
MERGE (m2:Merchant {id: 'm2', name: 'AmazonAWS',      category: 'cloud'})
MERGE (m3:Merchant {id: 'm3', name: 'WeWorkSpaces',   category: 'office'})

MERGE (t1:Transaction {id: 'txn1', amount: 2400.00,  currency: 'USD', ts: '2026-04-01'})
MERGE (t2:Transaction {id: 'txn2', amount: 18700.00, currency: 'USD', ts: '2026-04-02'})
MERGE (t3:Transaction {id: 'txn3', amount: 6500.00,  currency: 'USD', ts: '2026-04-03'})

MERGE (c1)-[:OWNS]->(a1)
MERGE (c2)-[:OWNS]->(a2)
MERGE (c3)-[:OWNS]->(a3)

MERGE (a1)-[:ORIGINATED_FROM]->(t1)
MERGE (a2)-[:ORIGINATED_FROM]->(t2)
MERGE (a3)-[:ORIGINATED_FROM]->(t3)

MERGE (t1)-[:AT]->(m1)
MERGE (t2)-[:AT]->(m2)
MERGE (t3)-[:AT]->(m3)
"""


def _probe_plaintext_bolt_refused(nlb_dns, password):
    """A non-TLS Bolt connect must fail: NLB listener is TLS and the instance
    sets server.bolt.tls_level=REQUIRED. Success here is a hard failure."""
    base = _base_scheme()
    try:
        drv = GraphDatabase.driver(
            f"{base}://{nlb_dns}:7687",
            auth=("neo4j", password),
            connection_timeout=8,
        )
        try:
            drv.verify_connectivity()
        finally:
            drv.close()
    except Exception as exc:
        return True, f"plaintext {base}:// rejected ({type(exc).__name__})"
    return False, f"plaintext {base}://{nlb_dns}:7687 unexpectedly connected"


def _probe_strict_tls(nlb_dns, password):
    """Informational: strict +s verifies the served cert against the system CA
    bundle. Pass means a publicly-trusted cert; failure is expected and
    acceptable for the auto-imported self-signed ACM cert."""
    base = _base_scheme()
    try:
        drv = GraphDatabase.driver(
            f"{base}+s://{nlb_dns}:7687",
            auth=("neo4j", password),
            connection_timeout=8,
        )
        try:
            drv.verify_connectivity()
        finally:
            drv.close()
    except Exception as exc:
        return False, f"{base}+s:// not CA-trusted ({type(exc).__name__})"
    return True, f"{base}+s:// verified against system CA bundle"


def _unverified_ctx():
    """NLB re-encrypts to the instance self-signed cert by design (AD-4), so
    these probes connect with TLS but without trust verification."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _probe_https_7473(nlb_dns):
    """HTTPS Browser endpoint must answer 200; cert trust is intentionally not
    verified here (the identity check is _probe_cert_identity)."""
    ctx = _unverified_ctx()
    req = urllib.request.Request(f"https://{nlb_dns}:7473/")
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            code = resp.status
    except Exception as exc:
        return False, f"GET https://{nlb_dns}:7473/ failed ({type(exc).__name__})"
    return code == 200, f"GET https://{nlb_dns}:7473/ -> {code}"


def _probe_plaintext_http_7474_refused(nlb_dns):
    """No plaintext HTTP listener exists on 7474 in TLS mode; a TCP connect
    must fail. Success here is a hard failure."""
    try:
        with socket.create_connection((nlb_dns, 7474), timeout=5):
            pass
    except OSError as exc:
        return True, f"7474 connect refused ({type(exc).__name__})"
    return False, f"{nlb_dns}:7474 unexpectedly accepted a plaintext connection"


def _probe_cert_identity(nlb_dns, advertised_dns):
    """The served cert SAN/CN must equal AdvertisedDNS: Jetty enforces
    sniHostCheck, so a mismatch breaks HTTPS with 400 Invalid SNI. Fetch the
    cert unverified, then re-handshake trusting only that cert with hostname
    checking on — ssl enforces the SAN/CN == AdvertisedDNS match for us."""
    try:
        fetch_ctx = _unverified_ctx()
        with socket.create_connection((nlb_dns, 7473), timeout=10) as raw:
            with fetch_ctx.wrap_socket(
                raw, server_hostname=advertised_dns
            ) as tls:
                der = tls.getpeercert(binary_form=True)
        pem = ssl.DER_cert_to_PEM_cert(der)

        verify_ctx = ssl.create_default_context(cadata=pem)
        verify_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        with socket.create_connection((nlb_dns, 7473), timeout=10) as raw:
            with verify_ctx.wrap_socket(raw, server_hostname=advertised_dns):
                pass
    except ssl.CertificateError as exc:
        return False, f"served cert does not match {advertised_dns}: {exc}"
    except Exception as exc:
        return False, f"cert identity probe failed ({type(exc).__name__}: {exc})"
    return True, f"served cert valid for {advertised_dns}"


def _tls_conformance(nlb_dns, password):
    """End-to-end TLS conformance probe run from inside the VPC.

    Hard checks (fail the probe): plaintext Bolt refused, HTTPS 7473 = 200,
    plaintext HTTP 7474 refused, served cert identity == AdvertisedDNS.
    Strict +s is informational (self-signed is a supported mode).
    """
    advertised_dns = os.environ.get("NEO4J_ADVERTISED_DNS", "")
    if not advertised_dns or os.environ.get("NEO4J_BOLT_TLS") != "true":
        return {
            "applicable": False,
            "passed": True,
            "detail": "plaintext stack (no AdvertisedDNS) — TLS probe skipped",
        }

    hard = {
        "plaintext_bolt_refused": _probe_plaintext_bolt_refused(nlb_dns, password),
        "https_7473_ok": _probe_https_7473(nlb_dns),
        "plaintext_http_7474_refused": _probe_plaintext_http_7474_refused(nlb_dns),
        "cert_identity": _probe_cert_identity(nlb_dns, advertised_dns),
    }
    strict_passed, strict_detail = _probe_strict_tls(nlb_dns, password)

    return {
        "applicable": True,
        "advertised_dns": advertised_dns,
        "passed": all(passed for passed, _ in hard.values()),
        "checks": {name: {"passed": p, "detail": d} for name, (p, d) in hard.items()},
        "strict_tls_info": {"passed": strict_passed, "detail": strict_detail},
    }


def lambda_handler(event, context):
    # The TLS conformance probe opens extra TLS/Bolt connections (one
    # intentionally to a closed port up to its timeout), so it runs only when
    # the caller explicitly asks. deploy-sample-private-app.py sends this as
    # the post-deploy gate; the Function URL demo path never sets it.
    run_probe = isinstance(event, dict) and event.get("tls_probe") is True
    try:
        driver = _get_driver()
        return _run(driver, run_probe)
    except AuthError:
        driver = _reset_driver()
        return _run(driver, run_probe)
    except Exception as exc:
        log.exception("lambda_handler failed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": type(exc).__name__, "message": str(exc)}),
        }


def _run(driver, run_probe=False):
    with driver.session(database="neo4j") as session:
        result = session.run(_MERGE_FINTECH)
        summary = result.consume()
        nodes_created = summary.counters.nodes_created
        rels_created = summary.counters.relationships_created

        edition_row = session.run(
            "CALL dbms.components() YIELD name, versions, edition"
        ).single()
        edition = edition_row["edition"] if edition_row else "unknown"

        routing_rows = []
        if os.environ.get("NEO4J_NUMBER_OF_SERVERS") != "1":
            routing_rows = session.run(
                "CALL dbms.routing.getRoutingTable({}, 'neo4j')"
            ).data()

        graph_sample = session.run(
            """
            MATCH (c:Customer)-[:OWNS]->(a:Account)-[:ORIGINATED_FROM]->(t:Transaction)-[:AT]->(m:Merchant)
            RETURN c.name AS customer, a.type AS account_type, t.amount AS amount, m.name AS merchant
            ORDER BY t.ts
            """
        ).data()

    with driver.session(database="system") as sys_session:
        servers = sys_session.run("SHOW SERVERS").data()

    writers = 0
    readers = 0
    for row in routing_rows:
        for server in row.get("servers", []):
            role = server.get("role")
            if role == "WRITE":
                writers += 1
            elif role == "READ":
                readers += len(server.get("addresses", []))

    body = {
        "tls_enabled": os.environ.get("NEO4J_BOLT_TLS") == "true",
        "bolt_scheme": _bolt_scheme(),
        "edition": edition,
        "nodes_created": nodes_created,
        "relationships_created": rels_created,
        "graph_sample": graph_sample,
        "servers": [
            {
                "name": s.get("name", s.get("address", "")),
                "state": s.get("state", ""),
                "health": s.get("health", ""),
            }
            for s in servers
        ],
        "routing_table": {
            "writers": writers,
            "readers": readers,
        },
    }

    if run_probe:
        body["tls_conformance"] = _tls_conformance(_nlb_dns(), _password())

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, indent=2),
    }


# ---------------------------------------------------------------------------
# Resilience handler: stop a follower via SSM and verify it rejoins.
# ---------------------------------------------------------------------------

_ec2 = boto3.client("ec2")
_ssm = boto3.client("ssm")

STOP_WAIT_TIMEOUT_S = 90
START_WAIT_TIMEOUT_S = 180
POLL_INTERVAL_S = 3


def resilience_handler(event, context):
    try:
        try:
            driver = _get_driver()
            return _resilience(driver)
        except AuthError:
            driver = _reset_driver()
            return _resilience(driver)
    except Exception as exc:
        log.exception("resilience_handler failed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": type(exc).__name__, "message": str(exc)}),
        }


def _resilience(driver):
    ee_stack = os.environ["NEO4J_STACK_NAME"]
    ee_stack_id = os.environ["NEO4J_STACK_ID"]

    with driver.session(database="system") as s:
        preflight = s.run("SHOW SERVERS").data()
    unhealthy = [r for r in preflight if r.get("health") != "Available"]
    if unhealthy:
        raise RuntimeError(f"Cluster is already degraded; refusing to run test: {unhealthy}")

    followers, leader = _cluster_roles(driver)
    if not followers:
        raise RuntimeError("No FOLLOWER members found in SHOW DATABASE neo4j YIELD serverID, writer")

    instance_ids = _neo4j_instance_ids(ee_stack_id)
    if len(instance_ids) < 2:
        raise RuntimeError(f"Expected >=2 Neo4j instances, found {len(instance_ids)}")

    uuid_by_instance = _read_server_ids(instance_ids)
    follower_instances = [i for i, u in uuid_by_instance.items() if u in followers]
    if not follower_instances:
        raise RuntimeError("Could not map any instance to a FOLLOWER UUID")

    target_instance = random.choice(follower_instances)
    target_uuid = uuid_by_instance[target_instance]

    log.info("Target follower: instance=%s server=%s", target_instance, target_uuid)

    t0 = time.time()
    _ssm_run_document(target_instance, os.environ["SSM_DOC_STOP_NEO4J"], "stop neo4j")
    time_to_stop_issued = round(time.time() - t0, 2)

    t1 = time.time()
    stop_health = _wait_for_health(driver, target_uuid, {"Unavailable", "Down", "Unknown"}, STOP_WAIT_TIMEOUT_S)
    time_to_unavailable = round(time.time() - t1, 2)

    t2 = time.time()
    _ssm_run_document(target_instance, os.environ["SSM_DOC_START_NEO4J"], "start neo4j")
    time_to_start_issued = round(time.time() - t2, 2)

    t3 = time.time()
    start_health = _wait_for_health(driver, target_uuid, {"Available"}, START_WAIT_TIMEOUT_S)
    time_to_available = round(time.time() - t3, 2)

    with driver.session(database="system") as s:
        final_servers = s.run("SHOW SERVERS").data()

    body = {
        "ee_stack": ee_stack,
        "target_instance_id": target_instance,
        "target_server_uuid": target_uuid,
        "leader_server_uuid": leader,
        "time_to_stop_issued_s": time_to_stop_issued,
        "time_to_unavailable_s": time_to_unavailable,
        "observed_stop_health": stop_health,
        "time_to_start_issued_s": time_to_start_issued,
        "time_to_available_s": time_to_available,
        "observed_start_health": start_health,
        "final_servers": [
            {
                "name": s.get("name", ""),
                "state": s.get("state", ""),
                "health": s.get("health", ""),
            }
            for s in final_servers
        ],
    }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, indent=2),
    }


def _cluster_roles(driver):
    with driver.session(database="system") as s:
        db_rows = s.run("SHOW DATABASE neo4j YIELD serverID, writer").data()
    followers = [r["serverID"] for r in db_rows if not r["writer"]]
    leader = next((r["serverID"] for r in db_rows if r["writer"]), None)
    return followers, leader


def _neo4j_instance_ids(ee_stack_id):
    # EE ASG propagates tag "StackID" = the full CFN stack ARN onto each instance.
    resp = _ec2.describe_instances(
        Filters=[
            {"Name": "tag:StackID", "Values": [ee_stack_id]},
            {"Name": "tag:Role", "Values": ["neo4j-cluster-node"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    return [i["InstanceId"] for r in resp["Reservations"] for i in r["Instances"]]


def _read_server_ids(instance_ids):
    cmd_id = _ssm.send_command(
        InstanceIds=instance_ids,
        DocumentName=os.environ["SSM_DOC_READ_SERVER_ID"],
    )["Command"]["CommandId"]

    mapping = {}
    deadline = time.time() + 60
    pending = set(instance_ids)
    while pending and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        for iid in list(pending):
            try:
                inv = _ssm.get_command_invocation(CommandId=cmd_id, InstanceId=iid)
            except _ssm.exceptions.InvocationDoesNotExist:
                continue
            status = inv["Status"]
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                pending.discard(iid)
                if status == "Success":
                    mapping[iid] = inv["StandardOutputContent"].strip()
    if not mapping:
        raise RuntimeError("SSM read of /var/lib/neo4j/data/server_id returned no output")
    return mapping


def _ssm_run_document(instance_id, document_name, action):
    cmd_id = _ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName=document_name,
    )["Command"]["CommandId"]
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        try:
            inv = _ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except _ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] == "Success":
            return
        if inv["Status"] in ("Failed", "Cancelled", "TimedOut"):
            raise RuntimeError(
                f"SSM document for {action} on {instance_id} ended {inv['Status']}: "
                f"{inv.get('StandardErrorContent','')}"
            )
    raise TimeoutError(f"SSM document for {action} on {instance_id} did not complete in 60s")


def _wait_for_health(driver, server_uuid, wanted, timeout_s):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            with driver.session(database="system") as s:
                rows = s.run("SHOW SERVERS").data()
            for r in rows:
                if r.get("name") == server_uuid:
                    last = r.get("health")
                    if last in wanted:
                        return last
                    break
        except Exception as e:
            last = f"query-error: {e.__class__.__name__}"
        time.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"Server {server_uuid} health did not reach {wanted} within {timeout_s}s (last={last})")
