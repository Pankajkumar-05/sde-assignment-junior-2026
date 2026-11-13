"""Discovery and orchestration: turn a drop directory into ordered file ingests."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .config import LedgerError
from .ingest_events import ingest_event_file
from .ingest_leads import ingest_lead_file
from .pii import Pii


def _natural_key(path: Path):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path.name)]


def discover_drops(path: Path) -> list:
    """A drop is a directory with leads/ and/or events/. A parent of drops yields them in order."""
    path = Path(path)
    if not path.is_dir():
        raise LedgerError(f"not a directory: {path}")
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    drops = sorted((p for p in path.iterdir()
                    if p.is_dir() and ((p / "leads").is_dir() or (p / "events").is_dir())),
                   key=_natural_key)
    if not drops:
        raise LedgerError(f"{path} contains no drop (expected leads/ and/or events/)")
    return drops


def ingest_drop(conn: sqlite3.Connection, pii: Pii, schemas: dict, drop: Path, log=print) -> None:
    """Leads first (workflow dirs and files in sorted order), then events (sorted)."""
    leads_dir = drop / "leads"
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            schema = schemas.get(wf_dir.name)
            if schema is None:
                log(f"warning: no schema for workflow {wf_dir.name!r}; skipping {wf_dir}")
                continue
            for f in sorted(p for p in wf_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv"):
                s = ingest_lead_file(conn, pii, schema, f, drop.name)
                label = f"leads  {drop.name}/{wf_dir.name}/{f.name}"
                if s is None:
                    log(f"{label}: already ingested, skipped")
                else:
                    log(f"{label}: rows={s['rows']} created={s['created']} merged={s['merged']}"
                        f" rejected={s['rejected']} warnings={s['warnings']}")
    events_dir = drop / "events"
    if events_dir.is_dir():
        for f in sorted(p for p in events_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jsonl"):
            s = ingest_event_file(conn, pii, f, drop.name)
            label = f"events {drop.name}/{f.name}"
            if s is None:
                log(f"{label}: already ingested, skipped")
            else:
                log(f"{label}: lines={s['lines']} stored={s['stored']}"
                    f" duplicates={s['duplicates']} malformed={s['malformed']}")


def ingest_paths(conn: sqlite3.Connection, pii: Pii, schemas: dict, paths, log=print) -> None:
    for p in paths:
        for drop in discover_drops(Path(p)):
            ingest_drop(conn, pii, schemas, drop, log)
