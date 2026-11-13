# AI Usage Disclosure

AI tools were used as pair-programming assistants during this assignment. Below is an honest breakdown of the tools used, how they contributed, the decisions I owned personally, and how I verified the final output.

## Tools used

* **Claude (Anthropic)** & **Gemini**: Used for architectural brainstorming, code scaffolding, unit test generation, and reviewing edge cases. No other AI tools were used.

## What I used them for

* **Requirements & Edge-Case Analysis:** Reviewed the Assignment A specification, schemas, and data generator rules to identify dirty-data anomalies (malformed timestamps, null fields, duplicate events, and PII exposure risks).
* **Implementation Scaffolding:** Generated initial Python modules under `solution/` for ingestion, normalization, PII protection, SQLite schema management, derived call-state logic, and CLI reporting.
* **Test & Oracle Generation:** Drafted `pytest` unit tests for normalizers and business rules, alongside an independent reference implementation (`solution/tests/oracle.py`) to cross-verify aggregate report metrics across multiple seeds.
* **Documentation Formatting:** Assisted with structuring the initial drafts of `SUBMISSION.md`, `solution/README.md`, and this disclosure file.

## What I wrote or decided myself

* **Core Architectural & Schema Decisions:** Decided to keep the stack lightweight (Python 3.11+ standard library + SQLite without heavy ORM overhead) so the pipeline is fast, deterministic, and easy to inspect or extend live.
* **Idempotency & PII Strategy:** Confirmed the two-layer deduplication approach (file-level content hashing to skip re-ingested drops safely, paired with event/lead-level constraints for overlapping records across distinct drops) and enforced strict PII isolation so raw customer names and phone numbers never appear in plaintext inside the database or output reports.
* **Spec Ambiguity Resolution:** Reviewed every assumption documented in `SUBMISSION.md` against `docs/GLOSSARY.md` and the starter schemas to ensure consistent disposition mapping and timestamp handling.
* **Local Verification & Cleanup:** Set up the local virtual environment, audited the generated codebase line-by-line, resolved test runner issues, and executed the full `pytest` suite locally.

## Where the AI was wrong or unhelpful

1. **Contradictory Duplicate-File Test:** An initial AI-generated test ingested two byte-identical event files while asserting a row-level duplicate count. Because the pipeline intentionally skips already-ingested files via content hashing, the test failed on a `None` return value. I corrected the test fixture to use distinct file contents containing overlapping event IDs so row-level deduplication was properly exercised.
2. **CLI Subprocess Path Assumption:** A CLI test invoked `python -m solution` without configuring `PYTHONPATH` from the test working directory, causing a `ModuleNotFoundError`. I updated the test helper so CLI execution works reliably regardless of the working directory.
3. **Sandbox Execution Limits:** Because the AI environment could not run `pytest` natively, I validated and executed the full test suite locally in my own environment to confirm all tests pass.

## How I verified AI-generated code

* **Full Local `pytest` Execution:** Installed dependencies in a clean virtual environment and ran the complete `pytest` suite locally—all unit, integration, and oracle parity tests pass.
* **Cross-Seed & Scale Parity:** Executed the pipeline across multiple generator seeds (including large-scale runs) and diffed outputs against the independent reference oracle to verify exact metric accuracy.
* **Idempotency & Crash Recovery Checks:** Confirmed that re-running ingestion on previously processed files leaves the SQLite database unchanged, incremental drops match full-batch ingestion, and simulated mid-file failures roll back cleanly without partial writes.
* **Plaintext PII Audit:** Scanned the generated SQLite database and report outputs against raw synthetic CSV inputs to verify zero plaintext PII leakage.