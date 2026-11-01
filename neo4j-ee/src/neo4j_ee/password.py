"""Read the Neo4j admin password from Secrets Manager.

The deploy record holds no password. Tools read the secret that the record's
Neo4jPasswordSecretArn output names, with the caller's own AWS credentials,
and keep the value in memory.
"""

from __future__ import annotations

from pathlib import Path

import boto3

from neo4j_ee.outputs import require_field


def fetch_admin_password(fields: dict[str, str], source: Path) -> str:
    """Return the admin password from the secret named in the deploy record."""
    secret_arn = require_field(fields, "Neo4jPasswordSecretArn", source)
    region = require_field(fields, "Region", source)
    client = boto3.client("secretsmanager", region_name=region)
    return client.get_secret_value(SecretId=secret_arn)["SecretString"]
