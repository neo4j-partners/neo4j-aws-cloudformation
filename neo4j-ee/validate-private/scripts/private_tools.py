"""Shared helpers for validate-private uv scripts."""

from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent
VALIDATE_PRIVATE_DIR = SCRIPTS_DIR.parent
EE_DIR = VALIDATE_PRIVATE_DIR.parent
DEPLOY_DIR = EE_DIR / ".deploy"

sys.path.insert(0, str(EE_DIR / "src"))
from neo4j_ee.outputs import (  # noqa: E402
    read_outputs,
    require_field as _require_field,
    require_private_mode as _require_private_mode,
    resolve_bolt_scheme,
    resolve_direct_bolt_scheme,
    resolve_outputs_file as _resolve_outputs_file,
)


def resolve_outputs_file(stack_name: str | None) -> Path:
    try:
        return _resolve_outputs_file(DEPLOY_DIR, stack_name)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit("Run deploy.py first, or pass a stack name.")


def require_field(fields: dict[str, str], key: str, source: Path) -> str:
    try:
        return _require_field(fields, key, source)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


def require_private_mode(fields: dict[str, str]) -> None:
    try:
        _require_private_mode(fields)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
