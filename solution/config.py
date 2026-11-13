"""Paths, environment variable names and the one error type used by the pipeline."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DEFAULT_DB = "ledger.db"

ENV_DB = "LEDGER_DB"
ENV_SCHEMAS = "LEDGER_SCHEMAS"
ENV_PII_KEY = "LEDGER_PII_KEY"


class LedgerError(Exception):
    """A fatal, user-facing problem (bad schema, wrong PII key, unreadable file...)."""


def default_db_path() -> str:
    return os.environ.get(ENV_DB) or DEFAULT_DB


def default_schema_dir() -> Path:
    return Path(os.environ.get(ENV_SCHEMAS) or DEFAULT_SCHEMA_DIR)
