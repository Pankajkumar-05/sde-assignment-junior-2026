# Submission

## Candidate
- **Name:** Pankaj Kumar A
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** 8 hours

## How to run
```bash
# setup (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# data (not committed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# pipeline (from the repository root)
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2          # or: python -m solution ingest data
python -m solution report --out reports/

# tests
pytest
```
State lives in `./ledger.db` (`--db` / `$LEDGER_DB`) and the PII key in `./ledger.db.key`
or `$LEDGER_PII_KEY`. Delete both for a clean run. Full details: [`solution/README.md`](solution/README.md).

## Design
Two kinds of state. **Facts** are written once per source file inside one transaction:
`leads` (one per `(workflow, phone_hash)`), `lead_rows` (an audit row per lead row, no raw
values), and `events` (the append-only ledger, protected by triggers). **Derived state**: event
to lead linking, calls, effective dispositions, buckets and the orphan/unknown counts: is never
stored; `report` recomputes it from the facts. So an event that arrives before its lead needs
no reconciliation job: it is stored as-is, and the join simply starts matching once the lead
exists.

```
CSV/JSONL --sha256--> source_files gate --(new file)--> one transaction per file:
   lead rows:  header map -> validate (schema order) -> normalise -> HMAC phone -> create/merge
   event lines: parse -> malformed? -> INSERT ON CONFLICT(event_id) DO NOTHING (dup?) -> ledger
report:  load leads + ledger + catalogue -> link -> calls -> effective disposition -> buckets -> 5 files
```

Schema (details and constraints in `solution/db.py`):
- `source_files(file_key UNIQUE = kind|workflow|sha256, counters)`: idempotency anchor.
- `leads(workflow_id, phone_hash, phone_last4, external_ref, batch_id, attrs_json, pii_enc)`,
  `UNIQUE(workflow_id, phone_hash)`.
- `lead_rows(source_file_id, row_number, outcome, reject_reason, field_warnings)` with CHECKs
  (rejected ⇒ reason, no lead, zero warnings).
- `events(event_id UNIQUE, type, workflow_id, call_id, occurred_at_us, phone_hash, code, payload)`
  with `BEFORE UPDATE/DELETE` triggers; indexed on `(workflow_id, phone_hash)` and `(workflow_id, call_id)`.
- `dispositions`, `meta`.

PII: phone → HMAC-SHA256 under a secret key (matching) + last 4 digits (masking); other PII
(name) → Fernet-encrypted blob. No plaintext PII, raw rows or raw event lines are stored.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Derived state recomputed at `report` time, never stored | Correct under late leads, re-runs and any ingest split by construction; nothing to invalidate | Materialised `event_links`/`calls` tables updated at ingest (faster reports, but needs backfill logic and breaks "append-only" purity if events were updated) |
| No orphan/quarantine table; orphan = event with no matching lead (yet) | Reconciles itself; ledger never mutated | A `quarantine` table with a re-link pass on each lead ingest |
| File identity = SHA-256 of bytes (+ kind, workflow) | Survives copies/renames; changed content is correctly "new" | Drop name + filename (breaks if a drop is re-delivered from another path) |
| Counters (`events_total`, malformed, duplicates, lead rows) kept per source file | A re-run can't double-count; reports sum them | Counters recomputed from stored rows (impossible for malformed lines, which are not stored) |
| Keyed HMAC for phone, Fernet for name | A plain hash of a 10-digit phone is brute-forceable; name must stay recoverable for a real dialer | Hash-only name (no extra dependency, unrecoverable) |
| `phone_last4` stored in plain | Needed for the required mask; it is the output form the spec asks for | Encrypt full phone and decrypt at report time |
| Time stored as UTC integer microseconds | Mixed `Z`/offset/naive text can't be compared as strings; IST date derived from the instant | Store ISO strings |
| Equal-timestamp ties broken by `event_id` | Deterministic regardless of ingest order | Ingest order (not stable across splits) |
| Disposition codes matched exactly | Spec says "not in dispositions.csv"; the generator's typos (`RENEWD`) are unambiguous | Case-insensitive match |
| Other spec gaps | See the full table in `solution/README.md` ("Assumptions") | |

## Failure modes considered
- **Bad input:** malformed/empty/non-object/non-UTF-8 event lines are counted, never fatal; CSV with BOM,
  extra/missing/re-ordered columns, ragged rows, blank rows, all-invalid files are handled per row.
- **Re-runs:** an ingested file is skipped before any write; the SQLite file is byte-identical afterwards (tested).
- **Crash mid-file:** one transaction per file (rows + `source_files` row); simulated crashes in both ingesters roll back fully and the re-run succeeds (tested).
- **Late data:** events before leads, overrides before their call, unknown-code events before their lead: all
  resolve at the next `report` (tested, including incremental == all-at-once).
- **Wrong PII key:** the key fingerprint is stored; a different key aborts the ingest instead of silently never matching.
- **Not handled:** concurrent writers (SQLite serialises them); key rotation; a corrected re-delivery of a
  file with the same name but different content is treated as a new file (its rows merge as duplicates), not a replacement;
  streaming ingestion of files larger than memory (files are read whole).

## Testing
`pytest` (about 190 test cases, ~15 s):
- unit tests for every normaliser (phone edge cases incl. a valid 10-digit number starting `91`, all date/decimal/enum/header forms, timestamp/IST boundaries);
- lead ingestion (headers, aliases, BOM, missing columns, blank rows and row numbers, first-failure order, warnings, merge/null-merge, permanent batch id);
- event ingestion (malformed, blank, duplicates, orphans, reconcile, overrides, unknown dispositions, append-only triggers);
- pure derivation (effective disposition, ties, buckets, order independence);
- report exactness (formats, sorting, IST dates, masking, data-quality keys);
- idempotency, crash atomicity, incremental == all-at-once, PII leak scans of the DB file and reports;
- **an independent reference implementation (`tests/oracle.py`) diffed against the pipeline on several generator seeds**, plus invariants (bucket totals, call totals, row accounting).

Not tested: concurrent writers, very large single files, key rotation.
Caveat: the oracle shares my reading of the spec, so it finds implementation bugs, not misreadings.

## Results
- `--scale large` (about 144k lead rows, 633k events), measured in a sandbox VM: ingest drop_1 16.8 s, ingest drop_2 22.5 s,
  report 5.6 s: about 45 s end to end; a full re-run (everything skipped) 0.2 s; DB ≈ 270 MB. Output matched the oracle exactly.
- Things worth noting in the data: with seed 42 about 5% of events are orphans in the final state (calls for
  rejected leads and never-uploaded numbers); after `drop_1` alone the orphan share is far higher and falls as
  `drop_2` delivers the leads; `field_warnings` come mostly from free-text languages (`Hindi`, `english`) and junk
  dates/amounts.

## If I had another day
1. Truly incremental derivation (persist per-call facts, recompute only leads touched by a new file); trade-off is invalidation logic vs. today's simple full recompute.
2. A `reprocess`/`replace file` command for corrected re-deliveries, instead of treating them as new files.
3. Move report aggregation into SQL views so a BI tool can query the same logic.
4. Key versioning + rotation, and KMS-backed key storage.
5. Production notes for 100k leads/day: scheduler per drop, per-file retries (safe because ingestion is idempotent), Postgres with partitioned `events`, backfills by re-pointing at old drops.
