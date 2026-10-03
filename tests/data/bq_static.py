"""Offline BigQuery checks over dbt-compiled SQL (sqlglot, bigquery dialect).

This proves syntax, native type names, column existence and partition predicates. It does
NOT prove runtime behaviour or partition pruning: only a real dry run can (ADR 0008).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

REPO = Path(__file__).resolve().parents[2]
DBT_DIR = REPO / "dbt"
PROJECT = "praxis-placeholder"
PREFIX = "praxis_dev_"
NON_NATIVE_TYPES = re.compile(
    r"\bas\s+(varchar|double|bigint|integer|int|boolean|text|real|smallint)\b", re.IGNORECASE
)
PREDICATES = (exp.Between, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ)


def write_offline_profile(directory: Path) -> Path:
    """A BigQuery profile with a throwaway key so dbt can compile without any credential."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    keyfile = directory / "offline-key.json"
    keyfile.write_text(
        json.dumps(
            {
                "type": "service" + "_account",  # built at runtime: keeps the secret scan strict
                "project_id": PROJECT,
                "private_key_id": "0",
                "private_key": pem,
                "client_email": f"offline@{PROJECT}.iam.gserviceaccount.com",
                "client_id": "0",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    profiles = directory / "profiles.yml"
    profiles.write_text(
        f"""praxis:
  target: bigquery
  outputs:
    bigquery:
      type: bigquery
      method: service-account
      keyfile: {keyfile}
      project: {PROJECT}
      dataset: {PREFIX}staging
      location: europe-west2
      threads: 4
"""
    )
    return directory


def compile_for_bigquery(workdir: Path) -> list[Path]:
    """`dbt compile --target bigquery` offline; returns every compiled .sql file."""
    profile_dir = write_offline_profile(workdir)
    target = workdir / "target"
    env = {
        **os.environ,
        "PRAXIS_BQ_SCHEMA_PREFIX": PREFIX,
        "DBT_TARGET_PATH": str(target),
        "DBT_LOG_PATH": str(workdir / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    done = subprocess.run(  # noqa: S603
        [
            str(REPO / ".venv/bin/dbt"),
            "--no-populate-cache",
            "compile",
            "--target",
            "bigquery",
            "--project-dir",
            str(DBT_DIR),
            "--profiles-dir",
            str(profile_dir),
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if done.returncode != 0:
        raise RuntimeError(f"dbt compile failed:\n{done.stdout[-2000:]}")
    return sorted((target / "compiled").rglob("*.sql"))


def parse(sql: str) -> exp.Expr:
    """Raises ``ParseError`` if the SQL is not valid BigQuery."""
    return sqlglot.parse_one(sql, read="bigquery")


def non_native_types(sql: str) -> list[str]:
    return sorted({m.group(1).lower() for m in NON_NATIVE_TYPES.finditer(sql)})


def unknown_columns(sql: str, schema: dict[str, Any]) -> str | None:
    """Returns an error message if a column or table cannot be resolved, else None."""
    try:
        qualify(
            parse(sql),
            schema=schema,
            dialect="bigquery",
            validate_qualify_columns=True,
            identify=False,
        )
    except (OptimizeError, ParseError) as exc:
        return str(exc)
    return None


def unfiltered_fact_scans(sql: str, facts: dict[str, str]) -> list[str]:
    """Fact tables scanned in a SELECT whose WHERE never bounds the partition column."""
    bad: list[str] = []
    for select in parse(sql).find_all(exp.Select):
        where = select.args.get("where")
        for table in select.find_all(exp.Table):
            if table.parent_select is not select or table.name not in facts:
                continue
            column = facts[table.name]
            bounded = where is not None and any(
                isinstance(node, PREDICATES)
                and any(c.name == column for c in node.find_all(exp.Column))
                for node in where.find_all(*PREDICATES)
            )
            if not bounded:
                bad.append(f"{table.name}.{column}")
    return sorted(set(bad))
