# Assignment A: Interaction Ledger & Reporting Pipeline

Python 3.11+, SQLite, `pytest`. One extra dependency, `cryptography`, used to encrypt the
non-phone PII fields (customer name) at rest.

## Install, run, test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# generate sample data (not committed; `data/` is gitignored)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# run (from the repository root)
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/

pytest                      # whole suite (~15 s; it runs the generator on several seeds)
```

* `ingest` takes one or more drop directories, or a parent directory of `drop_*` folders
  (so `python -m solution ingest data` ingests everything, in natural order).
* The database is `./ledger.db` (`--db PATH` or `$LEDGER_DB`). Delete it to start fresh.
* Schemas are read in place from `assignment-a-data/schemas` (`--schemas` / `$LEDGER_SCHEMAS`).
* PII key: `$LEDGER_PII_KEY`; if unset, a random key is generated on first use and stored in
  `<db>.key` (mode 0600, gitignored). Without the same key a database can't be extended; the
  program refuses to run with a different key instead of silently failing to match phones.
* Exit code 0 on success (bad rows/events are counted, never fatal), 2 on a fatal error
  (missing path, wrong key, unreadable schema).

## Layout

```
solution/
  cli.py            argparse: ingest / report
  pipeline.py       drop discovery + ordering (leads first, then events)
  config.py         paths, env var names, LedgerError
  schema_loader.py  workflow schemas + dispositions.csv -> plain dataclasses/dicts
  normalize.py      pure: header, phone, date, decimal, enum, timestamp helpers
  pii.py            HMAC phone hash, Fernet blob, masking, key handling
  db.py             DDL, triggers, sync of the disposition catalogue, file keys
  ingest_leads.py   CSV -> validate -> dedupe/merge -> leads, lead_rows
  ingest_events.py  JSONL -> malformed/duplicate handling -> append-only events
  derive.py         pure: link events, calls, effective disposition, buckets
  reports.py        the five output files
  tests/            pytest suite + an independent reference implementation (oracle.py)
```

## Design in one page

**Two kinds of state.** *Ingested facts* (leads, lead-row audit, the event ledger) are written
once, per source file, in one transaction. *Derived state* (which events link to which lead,
calls, effective dispositions, buckets, orphan/unknown counts) is **never stored**. `report`
recomputes it from the facts every time. That is what makes late-arriving leads, re-runs
and "drop 1 then drop 2 == everything at once" correct by construction: there is no cache to
get stale and no backfill step to forget.

### Database (SQLite)

| Table | Purpose |
|---|---|
| `source_files` | One row per ingested file, keyed by `kind \| workflow \| sha256(bytes)`. The idempotency anchor, plus per-file counters (rows, malformed, duplicates). |
| `leads` | One row per `(workflow_id, phone_hash)` (UNIQUE). Stores `phone_hash`, `phone_last4`, `external_ref`, the permanent `batch_id`, plain optional fields as JSON, and an **encrypted** blob for other PII. |
| `lead_rows` | One audit row per non-blank lead row: `created` / `merged` / `rejected`, the reason, warning count. **Never holds raw values**, so rejections carry no PII. CHECK constraints force "rejected ⇒ reason, no lead, 0 warnings". |
| `events` | The append-only interaction ledger. `event_id` UNIQUE. Holds `phone_hash` (never the phone), `call_id`, UTC-microsecond time, disposition code, payload. `BEFORE UPDATE`/`BEFORE DELETE` triggers make mutation fail at the DB level. |
| `dispositions` | Mirror of `dispositions.csv`, so reports need only the DB. |
| `meta` | Schema version and the PII key fingerprint. |

There is deliberately no `quarantine`/`orphans` table and no `lead_id` on events. An orphan is
simply an event whose `(workflow_id, phone_hash)` matches no lead yet (or an override whose
call has no linked events). When the lead arrives, the same join now matches. Nothing is moved
or updated, so the ledger stays append-only.

### Lead ingestion
Header cells are normalised exactly as specified and matched against each schema field's name
and aliases (leftmost matching column wins; unknown columns are ignored; a missing required
column rejects every row with `missing_required:<field>` instead of failing the file). Blank
rows are skipped and not counted; row numbers count non-blank rows. Fields are validated in
schema order; the first failing required field is the single rejection reason. An invalid
optional value becomes null plus one warning. Warnings are only counted for non-rejected rows
(including rows that turn out to be merged duplicates). Identity is `(workflow_id, canonical
phone)`; the first valid row creates the lead and fixes `batch_id` forever; later valid rows
overwrite only non-null values; defaults are applied after merging.

### Event ingestion
Per non-empty line: not JSON / not an object / no `event_id` → malformed; `event_id` already
in the ledger (any drop, any file) → duplicate; otherwise stored via
`INSERT ... ON CONFLICT(event_id) DO NOTHING`. Orphans are stored too, so they take part in
dedupe. Timestamps are normalised to UTC microseconds (naive = IST), so ordering is by instant,
never by text.

### Derived state (`derive.py`, pure functions)
1. Non-override events link via `(workflow_id, phone_hash)`; overrides link through the call
   (`workflow_id`, `call_id`) of a linked event. Unlinked events are orphans.
2. A linked `disposition.*` event whose code is not in the workflow's catalogue is counted as
   unknown and ignored (an unlinked one counts only as an orphan; it moves to "unknown" when its
   lead arrives, which is why these two counts are computed from final state).
3. A call exists only if it has a linked `call.initiated`. Its date is that event's IST date.
4. Effective disposition = latest valid override, else latest valid set, else none. Ties on
   `occurred_at` break on `event_id`, so results never depend on arrival order.
5. Bucket: `yet_to_reach_out` → `completed` → `in_progress_telephony_failed` → `in_progress_dialed`.

### PII
* Phone: canonicalised to `+91XXXXXXXXXX` first, then `HMAC-SHA256(key, canonical)`. A *keyed*
  hash because the phone space (~4×10⁹) is trivially brute-forced against a plain hash. Matching
  is an equality join on `(workflow_id, phone_hash)`. `phone_last4` is stored separately so
  reports can produce `+91******1234` without the phone.
* Customer name (and any other `pii: true` non-phone field): Fernet-encrypted JSON blob. No
  report reads it; it is recoverable only with the key.
* Invalid phones, raw event lines and rejected rows' contents are never stored. Logs and CLI
  output contain counts only.
* Subkeys for the HMAC and the encryption are derived from one master secret.

### Idempotency
Every file is hashed (SHA-256 of its bytes). If `kind|workflow|hash` is already in
`source_files`, the file is skipped before any write: not even a timestamp changes, and the
SQLite file is byte-identical after a re-run (verified). Re-running a lead file would otherwise
re-count merged duplicates, and re-running an events file would inflate `duplicate_events_dropped`
and `events_total`, so event-id dedupe alone isn't enough. Each file's rows and its
`source_files` row are committed in one `BEGIN IMMEDIATE` transaction, so a crash leaves nothing
behind and the re-run redoes the whole file.

## Assumptions / interpretations (where the spec is silent)

| Topic | Decision |
|---|---|
| Header normalisation | Applied literally (lower, trim, collapse `_`/whitespace runs); same function for schema names/aliases. If two columns match one field, the leftmost wins. |
| Required non-phone typed field invalid | `invalid_<type>`. Unreachable with the shipped schemas (only phone is a required typed field); `invalid_phone` is the specified case. |
| Dates | Zero-padded `DD`/`MM`, 4-digit year, exactly the four listed formats; `Sept`, `1/2/2026`, times, etc. are invalid. |
| Decimals | Rounded half-up to 2 places. Anything not `digits[.digits]` after prefix/comma removal (`NaN`, `1e3`, `-5`) is invalid. |
| Disposition codes | Matched exactly against `dispositions.csv` (no case folding), per workflow. |
| Event with other missing fields | Only invalid JSON / non-object / missing or empty `event_id` is malformed. Other gaps are stored; an event without a usable `workflow_id`/phone is an orphan; one without a parseable `occurred_at` is kept in the ledger but cannot be a call start or a "latest" disposition. |
| Calls | Keyed by `(lead, call_id)`. Two `call.initiated` for one call → the call starts at the earliest. If one `call_id` links to two leads (never generated) the override attaches to the lower `lead_id`. |
| Override older than the set | An override still wins ("latest override, else latest set"). |
| `batch_summary` | Only batches that have at least one lead. |
| `rejections_by_reason` | Only reasons that occurred (not zero-filled). |
| `connectivity_rate` | Python `f"{connected/total:.4f}"`. |
| Same bytes, new path | Treated as already ingested (content-addressed). Same path with changed bytes is a new file. |
| Undecodable bytes | CSV decoded as UTF-8 (BOM stripped) with replacement characters; an invalid event line is malformed. Neither crashes ingest. |
| Unknown workflow folder under `leads/` | Warned and skipped; not recorded, so it can be ingested after a schema is added. |
| Ingest order | Drops in the order given (a parent directory: natural sort), workflows and files in sorted filename order, leads before events within a drop. |

## Not done / known limits
* Derived state is recomputed from scratch on each `report` (about 6 s at the large scale), not
  incrementally. Simple and always correct; see `SUBMISSION.md` for the trade-off.
* Single writer (SQLite). Two `ingest` processes at once would serialise on the write lock.
* Key rotation is not implemented (it would need a key version column and a re-hash pass).
