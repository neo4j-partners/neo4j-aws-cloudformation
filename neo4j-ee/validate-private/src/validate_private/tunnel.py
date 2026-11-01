"""SSM port-forward tunnel from the operator machine to a host inside the VPC.

The tunnel runs ``aws ssm start-session`` with the
``AWS-StartPortForwardingSessionToRemoteHost`` document against the operator
bastion. The bastion only relays bytes. It holds no Neo4j credentials, so the
driver and cypher-shell run locally and the password never leaves this
process.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time

log = logging.getLogger(__name__)

PORT_FORWARD_DOCUMENT = "AWS-StartPortForwardingSessionToRemoteHost"


class TunnelError(RuntimeError):
    pass


def free_local_port() -> int:
    """Return a TCP port on 127.0.0.1 that is free at the time of the call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def missing_prerequisites() -> list[str]:
    """Return the local executables the tunnel needs that are not on PATH."""
    return [
        name for name in ("aws", "session-manager-plugin") if shutil.which(name) is None
    ]


class PortForward:
    """One ``aws ssm start-session`` port-forward process.

    Use as a context manager, or call ``start`` and ``close`` directly.
    ``start`` can be called again after the process dies. With the default
    ``local_port`` of 0 it picks a new free local port each time. A nonzero
    ``local_port`` is used as given.
    """

    def __init__(
        self,
        region: str,
        bastion_id: str,
        remote_host: str,
        remote_port: int,
        local_port: int = 0,
    ) -> None:
        self.region = region
        self.bastion_id = bastion_id
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._requested_port = local_port
        self.local_port = local_port
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr: tempfile._TemporaryFileWrapper[bytes] | None = None

    def __enter__(self) -> PortForward:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout_s: int = 60) -> None:
        if missing := missing_prerequisites():
            raise TunnelError(
                f"Required local tool(s) not on PATH: {', '.join(missing)}. "
                "Install the AWS CLI and the Session Manager plugin."
            )
        self.close()
        self.local_port = self._requested_port or free_local_port()
        cmd = [
            "aws", "ssm", "start-session",
            "--target", self.bastion_id,
            "--document-name", PORT_FORWARD_DOCUMENT,
            "--parameters",
            f"host={self.remote_host},portNumber={self.remote_port},"
            f"localPortNumber={self.local_port}",
            "--region", self.region,
        ]
        # A new process group lets close() stop session-manager-plugin, which
        # the AWS CLI starts as a child. The group id equals the CLI's pid, so
        # close() can signal the plugin even after the CLI itself exits. stdin is detached so the plugin
        # never competes with the caller for the terminal. stderr goes to a
        # temporary file, because a pipe nobody drains can fill during a
        # long session and stall the plugin.
        self._stderr = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            start_new_session=True,
        )
        log.debug(
            "Port-forward %s:%s -> 127.0.0.1:%s via %s (pid %s)",
            self.remote_host, self.remote_port, self.local_port,
            self.bastion_id, self._proc.pid,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                rc = self._proc.returncode
                stderr = self._read_stderr()
                self.close()
                raise TunnelError(
                    f"SSM port-forward to {self.remote_host}:{self.remote_port} "
                    f"via {self.bastion_id} exited (rc={rc}): {stderr}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        self.close()
        raise TunnelError(
            f"SSM port-forward to {self.remote_host}:{self.remote_port} via "
            f"{self.bastion_id} did not open 127.0.0.1:{self.local_port} "
            f"within {timeout_s}s"
        )

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            log.debug("Port-forward pid %s already exited", proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                log.debug("Port-forward pid %s exited before SIGKILL", proc.pid)
            proc.wait()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _read_stderr(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        return self._stderr.read(4000).decode(errors="replace").strip()
