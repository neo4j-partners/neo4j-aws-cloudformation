"""Sentinel node lifecycle for resilience and failover tests."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validate_private.runner import Neo4jQueryError, run_cypher

if TYPE_CHECKING:
    from validate_private.config import StackConfig

log = logging.getLogger(__name__)


def write_sentinel(config: "StackConfig", test_run_id: str) -> tuple[bool, str]:
    try:
        run_cypher(
            config,
            "CREATE (s:ResilienceSentinel {test_id: $tid, value: 'persistence-check'})",
            params={"tid": test_run_id},
        )
        rows = run_cypher(
            config,
            "MATCH (s:ResilienceSentinel {test_id: $tid}) RETURN s.value AS v",
            params={"tid": test_run_id},
        )
        if rows and rows[0].get("v") == "persistence-check":
            return True, f"Sentinel written (id={test_run_id[:8]}…)"
        return False, "Sentinel not found immediately after creation"
    except (Neo4jQueryError, Exception) as exc:
        return False, str(exc)


def verify_sentinel(config: "StackConfig", test_run_id: str) -> tuple[bool, str]:
    try:
        rows = run_cypher(
            config,
            "MATCH (s:ResilienceSentinel {test_id: $tid}) RETURN s.value AS v",
            params={"tid": test_run_id},
        )
        if not rows:
            return False, "Sentinel NOT found — data volume was lost or reformatted"
        if rows[0].get("v") == "persistence-check":
            return True, f"Sentinel intact (id={test_run_id[:8]}…)"
        return False, f"Unexpected sentinel value: {rows[0].get('v')!r}"
    except (Neo4jQueryError, Exception) as exc:
        return False, str(exc)


def cleanup_sentinel(config: "StackConfig", test_run_id: str) -> None:
    try:
        run_cypher(
            config,
            "MATCH (s:ResilienceSentinel {test_id: $tid}) DELETE s",
            params={"tid": test_run_id},
        )
    except Exception:
        log.warning("  Sentinel cleanup failed (non-fatal) — run manually if needed:")
        log.warning("    MATCH (s:ResilienceSentinel {test_id: '%s'}) DELETE s", test_run_id)
