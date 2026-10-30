#!/usr/bin/env python3
"""
SSM port-forward tunnel diagnostic script.

Tests whether an SSM port-forward tunnel started as a Python subprocess actually
forwards Browser HTTPS traffic, and surfaces exactly which subprocess flags cause
failures. The operator Browser path is HTTPS on 7473 (TLS terminated at the NLB);
see phase 6 of neo4j-ee/worklog/tls.md.

Usage:
    # Prerequisites: deploy a Private-mode stack and note instance ID and NLB DNS.
    STACK_FILE=$(ls -t ../.deploy/*.txt | head -1)
    INSTANCE_ID=<instance-id>
    NLB_DNS=<nlb-dns>

    # Run all flag combinations:
    python3 ssm_tunnel_test.py --instance $INSTANCE_ID --host $NLB_DNS

    # Or load from deploy file automatically:
    python3 ssm_tunnel_test.py --stack-file $STACK_FILE

    # Run a specific combination:
    python3 ssm_tunnel_test.py --instance $INSTANCE_ID --host $NLB_DNS \
        --new-session --stdin-devnull --stdout-devnull --stderr-pipe
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
    requests.packages.urllib3.disable_warnings()
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("Note: 'requests' not installed — requests-based HTTP check will be skipped.")


LOCAL_PORT = 17473   # use a non-standard port to avoid conflicts
# TLS is terminated at the NLB; the cert is self-signed on the test path, so the
# diagnostic must skip chain/hostname verification (the equivalent of +ssc).
_TLS_CTX = ssl._create_unverified_context()
CONNECT_TIMEOUT = 60  # seconds to wait for the port to bind
HTTP_ATTEMPTS = 20    # how many HTTP attempts to make after port is open
HTTP_TIMEOUT = 5      # per-request timeout


# ---------------------------------------------------------------------------
# Core tunnel probe
# ---------------------------------------------------------------------------

def probe_tunnel(
    instance_id: str,
    host: str,
    remote_port: int,
    local_port: int,
    region: str,
    *,
    new_session: bool,
    stdin_devnull: bool,
    stdout_mode: str,   # "devnull" | "pipe" | "inherit"
    stderr_mode: str,   # "devnull" | "pipe" | "inherit"
    label: str,
) -> dict:
    """Start a tunnel with the given flags and probe it. Return a result dict."""

    def _fd(mode: str):
        if mode == "devnull":
            return subprocess.DEVNULL
        if mode == "pipe":
            return subprocess.PIPE
        return None  # inherit

    cmd = [
        "aws", "ssm", "start-session",
        "--target", instance_id,
        "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters",
        f"host={host},portNumber={remote_port},localPortNumber={local_port}",
        "--region", region,
    ]

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL if stdin_devnull else None,
        "stdout": _fd(stdout_mode),
        "stderr": _fd(stderr_mode),
    }
    if new_session:
        popen_kwargs["start_new_session"] = True

    result = {
        "label": label,
        "flags": {
            "new_session": new_session,
            "stdin_devnull": stdin_devnull,
            "stdout": stdout_mode,
            "stderr": stderr_mode,
        },
        "port_open_after_s": None,
        "http_ok": False,
        "http_attempts_before_ok": None,
        "tcp_only_ok": False,
        "error": None,
    }

    proc = subprocess.Popen(cmd, **popen_kwargs)
    print(f"\n[{label}] PID={proc.pid}  flags={result['flags']}")

    try:
        # Step 1: wait for local port to bind
        deadline = time.monotonic() + CONNECT_TIMEOUT
        port_open = False
        for i in range(CONNECT_TIMEOUT):
            try:
                with socket.create_connection(("localhost", local_port), timeout=1):
                    result["port_open_after_s"] = i
                    port_open = True
                    break
            except (ConnectionRefusedError, OSError):
                if proc.poll() is not None:
                    stderr_out = ""
                    if stderr_mode == "pipe" and proc.stderr:
                        stderr_out = proc.stderr.read(1000).decode(errors="replace")
                    result["error"] = f"Process exited early (rc={proc.returncode}). stderr={stderr_out!r}"
                    print(f"  [FAIL] {result['error']}")
                    return result
                time.sleep(1)

        if not port_open:
            result["error"] = f"Port {local_port} never opened within {CONNECT_TIMEOUT}s"
            print(f"  [FAIL] {result['error']}")
            return result

        print(f"  Port open after {result['port_open_after_s']}s")

        # Step 2: TLS handshake + HTTPS data exchange against Browser (7473).
        try:
            with socket.create_connection(("localhost", local_port), timeout=3) as raw:
                raw.settimeout(3)
                with _TLS_CTX.wrap_socket(raw, server_hostname=None) as s:
                    s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
                    data = s.recv(16)
            if data:
                result["tcp_only_ok"] = True
                print(f"  HTTPS data exchange: OK ({data[:16]!r})")
            else:
                print("  HTTPS data exchange: connected but received no data")
        except Exception as e:
            print(f"  HTTPS data exchange: {type(e).__name__}: {e}")

        # Step 3: HTTPS check (if requests is available).
        if HAS_REQUESTS:
            for attempt in range(HTTP_ATTEMPTS):
                try:
                    resp = requests.get(
                        f"https://localhost:{local_port}",
                        timeout=HTTP_TIMEOUT,
                        verify=False,
                    )
                    if resp.status_code == 200:
                        result["http_ok"] = True
                        result["http_attempts_before_ok"] = attempt
                        print(f"  HTTP 200 after {attempt} extra attempts ({attempt * HTTP_TIMEOUT}s)")
                        break
                    else:
                        print(f"  Attempt {attempt}: HTTP {resp.status_code}")
                        break
                except requests.Timeout:
                    print(f"  Attempt {attempt}: ReadTimeout")
                except requests.ConnectionError as e:
                    print(f"  Attempt {attempt}: ConnectionError: {e}")
                time.sleep(1)
            else:
                print(f"  HTTPS never returned 200 after {HTTP_ATTEMPTS} attempts")
        else:
            print("  Skipping HTTPS check (requests not installed)")

    finally:
        # Terminate the tunnel process (and its entire process group if new_session)
        try:
            if new_session:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                if new_session:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()

    return result


# ---------------------------------------------------------------------------
# Flag matrix
# ---------------------------------------------------------------------------

FLAG_MATRIX = [
    # (new_session, stdin_devnull, stdout_mode, stderr_mode, label)
    (True,  True,  "devnull", "pipe",    "production (current code)"),
    (True,  True,  "devnull", "devnull", "new_session + all devnull"),
    (True,  True,  "pipe",   "pipe",    "new_session + stdout pipe"),
    (False, True,  "devnull", "pipe",    "no new_session + devnull"),
    (False, False, "devnull", "pipe",    "no new_session + inherit stdin"),
    (False, False, None,      None,      "no new_session + inherit all"),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SSM port-forward tunnel diagnostic")
    p.add_argument("--instance", help="EC2 instance ID (SSM target)")
    p.add_argument("--host", help="Remote host to forward to (NLB DNS or IP)")
    p.add_argument("--remote-port", type=int, default=7473,
                   help="Remote port (default: 7473, Browser HTTPS)")
    p.add_argument("--local-port", type=int, default=LOCAL_PORT, help=f"Local port (default: {LOCAL_PORT})")
    p.add_argument("--region")
    p.add_argument("--stack-file", type=Path,
                   help="Path to .deploy/<stack>.txt — auto-populates --instance, --host, --region")
    p.add_argument("--combo", type=int, default=None,
                   help="Run only a specific combination index (0-based) instead of all")
    return p.parse_args()


def _load_stack_file(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


def _get_any_instance(asg_name: str, region: str) -> str:
    import json
    out = subprocess.check_output([
        "aws", "autoscaling", "describe-auto-scaling-groups",
        "--auto-scaling-group-names", asg_name,
        "--region", region,
        "--query", "AutoScalingGroups[0].Instances[?LifecycleState=='InService'].InstanceId",
        "--output", "json",
    ])
    ids = json.loads(out)
    if not ids:
        raise RuntimeError(f"No InService instances in ASG {asg_name}")
    return ids[0]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    instance_id = args.instance
    host = args.host
    region = args.region

    if args.stack_file:
        fields = _load_stack_file(args.stack_file)
        host = host or fields.get("Neo4jInternalDNS")
        region = region or fields.get("Region")
        if not instance_id:
            instance_id = fields.get("Neo4jOperatorBastionId")
        if not instance_id:
            asg_name = fields.get("Neo4jNode1ASGName") or fields.get("Neo4jASGName")
            if asg_name:
                print(f"Resolving instance from ASG {asg_name}...")
                instance_id = _get_any_instance(asg_name, region)

    region = region or "us-east-1"

    if not instance_id or not host:
        print("ERROR: --instance and --host are required (or use --stack-file)")
        sys.exit(1)

    print(f"Target instance: {instance_id}")
    print(f"Remote host:     {host}:{args.remote_port}")
    print(f"Local port:      {args.local_port}")
    print(f"Region:          {region}")

    combos = FLAG_MATRIX if args.combo is None else [FLAG_MATRIX[args.combo]]
    results = []

    for new_session, stdin_devnull, stdout_mode, stderr_mode, label in combos:
        result = probe_tunnel(
            instance_id, host, args.remote_port, args.local_port, region,
            new_session=new_session,
            stdin_devnull=stdin_devnull,
            stdout_mode=stdout_mode,
            stderr_mode=stderr_mode,
            label=label,
        )
        results.append(result)
        # Brief pause between combos to let the previous session fully terminate
        time.sleep(3)

    print("\n\n=== RESULTS ===")
    print(f"{'Label':<45} {'Port':<6} {'Data':<5} {'HTTPS':<5} {'Note'}")
    print("-" * 80)
    for r in results:
        port_s = f"{r['port_open_after_s']}s" if r['port_open_after_s'] is not None else "FAIL"
        tcp_s = "OK" if r["tcp_only_ok"] else "FAIL"
        if HAS_REQUESTS:
            http_s = f"OK@{r['http_attempts_before_ok']}" if r["http_ok"] else "FAIL"
        else:
            http_s = "N/A"
        note = r["error"] or ""
        print(f"{r['label']:<45} {port_s:<6} {tcp_s:<5} {http_s:<5} {note}")


if __name__ == "__main__":
    main()
