"""SQLite schema and small connection helpers.

Transactions are managed explicitly (isolation_level=None): each source file is ingested
inside one BEGIN IMMEDIATE ... COMMIT, so a crash never leaves a half-written file.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from .config import LedgerError

SCHEMA_VERSION = "1"

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Disposition catalogue, mirrored from dispositions.csv so reports need only the DB.
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code        TEXT NOT NULL,
    label       TEXT NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success  IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

-- One row per ingested input file: the idempotency anchor and home of per-file counters.
-- file_key = kind|workflow|sha256(bytes); a file whose key exists is skipped untouched.
CREATE TABLE IF NOT EXISTS source_files (
    source_file_id  INTEGER PRIMARY KEY,
    file_key        TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id     TEXT,            -- leads only
    batch_id        TEXT,            -- leads only (file stem)
    drop_name       TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    rows_total      INTEGER NOT NULL DEFAULT 0,  -- non-blank lead rows / non-empty event lines
    malformed_count INTEGER NOT NULL DEFAULT 0,  -- events only
    duplicate_count INTEGER NOT NULL DEFAULT 0,  -- events only
    ingested_at     TEXT NOT NULL
);

-- No plaintext PII: phone is an HMAC + last 4, other PII fields are an encrypted blob.
CREATE TABLE IF NOT EXISTS leads (
    lead_id              INTEGER PRIMARY KEY,
    workflow_id          TEXT NOT NULL,
    phone_hash           TEXT NOT NULL CHECK (length(phone_hash) = 64),
    phone_last4          TEXT NOT NULL CHECK (length(phone_last4) = 4
                                              AND phone_last4 NOT GLOB '*[^0-9]*'),
    external_ref         TEXT NOT NULL CHECK (external_ref <> ''),
    batch_id             TEXT NOT NULL,  -- file stem of the first valid row; never updated
    first_source_file_id INTEGER NOT NULL REFERENCES source_files (source_file_id),
    attrs_json           TEXT NOT NULL DEFAULT '{}',  -- normalised non-PII optional fields
    pii_enc              BLOB NOT NULL,               -- encrypted non-phone PII fields
    UNIQUE (workflow_id, phone_hash)
);
CREATE INDEX IF NOT EXISTS ix_leads_report ON leads (workflow_id, external_ref);

-- Audit of every non-blank lead row. Never holds raw values, so rejections carry no PII.
CREATE TABLE IF NOT EXISTS lead_rows (
    source_file_id INTEGER NOT NULL REFERENCES source_files (source_file_id),
    row_number     INTEGER NOT NULL CHECK (row_number >= 1),
    outcome        TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reject_reason  TEXT,
    field_warnings INTEGER NOT NULL DEFAULT 0 CHECK (field_warnings >= 0),
    lead_id        INTEGER REFERENCES leads (lead_id),
    PRIMARY KEY (source_file_id, row_number),
    CHECK ((outcome = 'rejected') = (reject_reason IS NOT NULL)),
    CHECK (outcome <> 'rejected' OR (lead_id IS NULL AND field_warnings = 0))
);

-- The interaction ledger: append-only. Orphans live here too; "linked" is never stored,
-- it is derived by joining (workflow_id, phone_hash) -> leads (or call_id for overrides),
-- so a late-arriving lead reconciles old events with no UPDATE.
CREATE TABLE IF NOT EXISTS events (
    event_pk         INTEGER PRIMARY KEY,
    event_id         TEXT NOT NULL UNIQUE,
    event_type       TEXT,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_us   INTEGER,   -- UTC epoch microseconds; NULL if unparseable
    phone_hash       TEXT,      -- NULL for overrides and for missing/invalid phones
    disposition_code TEXT,
    payload_json     TEXT NOT NULL,
    source_file_id   INTEGER NOT NULL REFERENCES source_files (source_file_id),
    line_number      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_phone ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call  ON events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'interaction ledger is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def check_pii_key(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Refuse to continue with a different PII key than the one the data was written with."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    elif row[0] != fingerprint:
        raise LedgerError(
            "the PII key does not match the one this database was created with "
            "(set LEDGER_PII_KEY or restore the <db>.key file); phone matching would break")


def sync_dispositions(conn: sqlite3.Connection, catalogue: dict) -> None:
    """Make the dispositions table equal the CSV; writes nothing if already identical."""
    want = {(wf, code): (label, int(term), int(succ))
            for (wf, code), (label, term, succ) in catalogue.items()}
    have = {(r[0], r[1]): (r[2], r[3], r[4]) for r in conn.execute(
        "SELECT workflow_id, code, label, is_terminal, is_success FROM dispositions")}
    if want == have:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM dispositions")
        conn.executemany("INSERT INTO dispositions VALUES (?,?,?,?,?)",
                         [(wf, c, *v) for (wf, c), v in sorted(want.items())])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def make_file_key(kind: str, workflow_id: str | None, data: bytes) -> str:
    return f"{kind}|{workflow_id or ''}|{hashlib.sha256(data).hexdigest()}"


def file_already_ingested(conn: sqlite3.Connection, file_key: str) -> bool:
    return conn.execute("SELECT 1 FROM source_files WHERE file_key = ?", (file_key,)).fetchone() is not None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
