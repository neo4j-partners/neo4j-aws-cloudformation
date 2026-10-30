"""Dotenv-style settings file helpers.

deploy.py reads its settings from `neo4j-ee/.env`. Keep the loading here so the
precedence rule stays in one place.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def load_env_file(path: Path) -> list[str]:
    """Export each setting in path that the environment does not already set.

    A real environment variable wins over the file. Returns the names that were
    taken from the file, or an empty list when the file does not exist.
    """
    if not path.is_file():
        return []
    settings = {
        name: value
        for name, value in dotenv_values(path).items()
        if value is not None and name not in os.environ
    }
    os.environ.update(settings)
    return list(settings)
