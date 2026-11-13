import json
import os
import subprocess
import sys

from .helpers import REPO, generated


def run(args, env_extra=None, cwd=REPO):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(env_extra or {})
    env["PYTHONPATH"] = str(REPO)          # lets the tests run `-m solution` from any cwd
    return subprocess.run([sys.executable, "-m", "solution", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def test_documented_workflow_works_from_repo_root(tmp_path):
    data = generated(21)
    dbp = str(tmp_path / "x.db")
    out = tmp_path / "reports"
    r1 = run(["ingest", str(data / "drop_1"), "--db", dbp])
    r2 = run(["ingest", str(data / "drop_2"), "--db", dbp])
    r3 = run(["report", "--out", str(out), "--db", dbp])
    assert (r1.returncode, r2.returncode, r3.returncode) == (0, 0, 0), (r1.stderr, r2.stderr, r3.stderr)
    assert sorted(p.name for p in out.iterdir()) == [
        "batch_summary.csv", "daily_overview.csv", "data_quality.json", "lead_report.csv", "rejections.csv"]
    again = run(["ingest", str(data / "drop_1"), "--db", dbp])
    assert "already ingested, skipped" in again.stdout and "created" not in again.stdout
    assert (tmp_path / "x.db.key").exists()


def test_env_vars_configure_db_and_key_and_cwd_does_not_matter(tmp_path):
    data = generated(22)
    extra = {"LEDGER_DB": str(tmp_path / "e.db"), "LEDGER_PII_KEY": "k-for-test"}
    assert run(["ingest", str(data)], extra, cwd=tmp_path).returncode == 0
    assert run(["report", "--out", str(tmp_path / "o")], extra, cwd="/").returncode == 0
    assert not (tmp_path / "e.db.key").exists()
    assert json.loads((tmp_path / "o" / "data_quality.json").read_text())["events_total"] > 0


def test_errors_are_clean_exit_codes(tmp_path):
    r = run(["report", "--out", str(tmp_path / "o"), "--db", str(tmp_path / "missing.db")])
    assert r.returncode == 2 and "database not found" in r.stderr and "Traceback" not in r.stderr
    r = run(["ingest", str(tmp_path / "nope"), "--db", str(tmp_path / "e.db")])
    assert r.returncode == 2 and "not a directory" in r.stderr


def test_wrong_pii_key_on_existing_database_is_refused(tmp_path):
    data = generated(23)
    dbp = str(tmp_path / "k.db")
    assert run(["ingest", str(data / "drop_1"), "--db", dbp], {"LEDGER_PII_KEY": "one"}).returncode == 0
    r = run(["ingest", str(data / "drop_2"), "--db", dbp], {"LEDGER_PII_KEY": "two"})
    assert r.returncode == 2 and "PII key" in r.stderr
