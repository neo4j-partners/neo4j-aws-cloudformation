"""CLI entry point: uv run run-cypher"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from validate_private.config import load_config
from validate_private.runner import Neo4jQueryError, run_cypher

log = logging.getLogger(__name__)

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"


def _resolve_outputs_path(stack: str | None) -> Path:
    if stack:
        return _DEPLOY_DIR / f"{stack}.txt"
    if _DEPLOY_DIR.is_dir():
        txt_files = sorted(
            _DEPLOY_DIR.glob("*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if txt_files:
            return txt_files[0]
    raise FileNotFoundError(
        f"No deployment found in {_DEPLOY_DIR}. "
        "Run deploy.py first, or pass a stack name as the first argument."
    )


def main() -> None:
    # Positional dispatch: 1 arg = cypher (latest stack); 2 args = stack + cypher
    args = sys.argv[1:]
    if not args:
        print("Usage: run-cypher [stack-name] '<cypher>'", file=sys.stderr)
        sys.exit(1)
    elif len(args) == 1:
        stack, cypher = None, args[0]
    else:
        stack, cypher = args[0], args[1]

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stderr)

    try:
        outputs_path = _resolve_outputs_path(stack)
        config = load_config(outputs_path)
    except (FileNotFoundError, ValueError) as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    log.info("  Stack:  %s", config.stack_name)
    log.info("  Region: %s", config.region)
    log.info("")

    try:
        rows = run_cypher(config, cypher)
    except Neo4jQueryError as exc:
        log.error("Query failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    print(json.dumps(rows, indent=2))
