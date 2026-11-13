"""Command line interface:

    python -m solution ingest data/drop_1 [data/drop_2 ...]   (or a parent of drop_* dirs)
    python -m solution report --out reports/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db
from .config import LedgerError, default_db_path, default_schema_dir
from .pii import load_pii
from .pipeline import ingest_paths
from .reports import write_reports
from .schema_loader import load_dispositions, load_schemas


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=default_db_path(),
                        help="SQLite file (default: $LEDGER_DB or ./ledger.db)")
    common.add_argument("--schemas", default=str(default_schema_dir()),
                        help="directory with workflow schemas + dispositions.csv")
    p = argparse.ArgumentParser(prog="python -m solution", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", parents=[common], help="ingest one or more drops")
    ing.add_argument("paths", nargs="+", help="drop directory (or a directory of drops)")
    rep = sub.add_parser("report", parents=[common], help="write the five report files")
    rep.add_argument("--out", required=True, help="output directory")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ingest":
            schemas = load_schemas(Path(args.schemas))
            catalogue = load_dispositions(Path(args.schemas))
            pii = load_pii(args.db)
            conn = db.connect(args.db)
            try:
                db.init_db(conn)
                db.check_pii_key(conn, pii.fingerprint)
                db.sync_dispositions(conn, catalogue)
                ingest_paths(conn, pii, schemas, args.paths)
            finally:
                conn.close()
        else:
            if not Path(args.db).is_file():
                raise LedgerError(f"database not found: {args.db} (run `ingest` first)")
            catalogue = load_dispositions(Path(args.schemas))
            conn = db.connect(args.db)
            try:
                db.init_db(conn)
                db.sync_dispositions(conn, catalogue)
                names = write_reports(conn, args.out)
            finally:
                conn.close()
            print(f"wrote {', '.join(names)} to {args.out}")
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
