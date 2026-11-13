import os
import stat

import pytest

from solution import db
from solution.config import LedgerError
from solution.pii import Pii, load_pii

from .helpers import Env, ev, generated, lead_row
from .oracle import o_phone


def test_phone_hash_is_keyed_and_stable():
    a, b = Pii(b"k1"), Pii(b"k2")
    assert a.phone_hash("+919876543210") == a.phone_hash("+919876543210")
    assert a.phone_hash("+919876543210") != b.phone_hash("+919876543210")
    assert len(a.phone_hash("+919876543210")) == 64
    assert "9876543210" not in a.phone_hash("+919876543210")


def test_mask_and_last4():
    assert Pii.mask_phone(Pii.phone_last4("+919876543210")) == "+91******3210"


def test_fields_round_trip_and_are_not_plaintext():
    p = Pii(b"k")
    token = p.encrypt_fields({"customer_name": "Aarav Menon"})
    assert b"Aarav" not in token
    assert p.decrypt_fields(token) == {"customer_name": "Aarav Menon"}
    with pytest.raises(LedgerError):
        Pii(b"other").decrypt_fields(token)


def test_key_file_created_once_with_private_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_PII_KEY", raising=False)
    dbp = str(tmp_path / "x.db")
    a = load_pii(dbp)
    key_file = tmp_path / "x.db.key"
    assert key_file.exists()
    if os.name == "posix":
        # Unix permission bits are only meaningful on POSIX. On Windows os.stat() reports a
        # synthetic mode (0o666 for any writable file) because access is governed by NTFS
        # ACLs, so asserting 0o600 there would fail for a reason unrelated to the code.
        assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert load_pii(dbp).fingerprint == a.fingerprint     # stable across runs


def test_env_key_is_used_and_beats_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_PII_KEY", "secret-from-env")
    dbp = str(tmp_path / "y.db")
    assert load_pii(dbp).fingerprint == load_pii(dbp).fingerprint
    assert not (tmp_path / "y.db.key").exists()
    monkeypatch.setenv("LEDGER_PII_KEY", "different")
    assert load_pii(dbp).fingerprint != Pii(__import__("hashlib").sha256(b"secret-from-env").digest()).fingerprint


def test_wrong_key_is_refused(tmp_path):
    env = Env(tmp_path)
    with pytest.raises(LedgerError, match="PII key"):
        db.check_pii_key(env.conn, Pii(b"another").fingerprint)


def test_no_plaintext_pii_in_database_file(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(name="Zubin Quillfeather", phone="+91 98765 43210", ref="REF-77"),
               lead_row(name="Nobody Rejected", phone="abc-invalid-77", ref="REF-78")])
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z", phone="(+91) 9876543210"),
                ev("e2", "call.initiated", "c2", "2026-10-03T10:00:00Z", phone="12-invalid-phone-99")])
    env.reports()
    env.conn.close()
    blob = open(env.db_path, "rb").read()
    for needle in [b"Zubin", b"Quillfeather", b"Nobody", b"9876543210", b"98765 43210", b"98765",
                   b"invalid-77", b"invalid-phone-99", b"+91987"]:
        assert needle not in blob, needle
    assert b"REF-77" in blob                                   # non-PII reference is stored


def test_rejection_report_contains_only_safe_columns(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(name="Secret Person", phone="bad-phone", ref="R1")])
    from solution.reports import build_reports
    text = build_reports(env.conn)["rejections.csv"]
    assert text == "workflow_id,batch_id,row_number,reason\nmotor_renewal,batch_001,1,invalid_phone\n"
    assert "Secret" not in text and "bad-phone" not in text


def test_generated_data_leaves_no_plaintext_pii_anywhere(tmp_path):
    import csv
    from .test_seeds import ingest_all
    data = generated(11)
    env = ingest_all(tmp_path, [data / "drop_1", data / "drop_2"])
    out = tmp_path / "reports"
    from solution.reports import write_reports
    write_reports(env.conn, out)
    env.conn.close()
    names, phones = set(), set()
    for f in data.glob("drop_*/leads/*/*.csv"):
        for row in csv.DictReader(open(f, encoding="utf-8", newline="")):
            n = row.get("customer_name") or row.get("Insured Name") or row.get("Name")
            p = row.get("phone") or row.get("Mobile No") or row.get("Contact")
            if n and n.strip():
                names.add(n.strip())
            if p and o_phone(p):
                phones.add(o_phone(p)[3:])
    assert len(names) > 20 and len(phones) > 500
    scan = [open(env.db_path, "rb").read()] + [p.read_bytes() for p in out.iterdir()]
    scan.append((tmp_path / "ledger.db.key").read_bytes() if (tmp_path / "ledger.db.key").exists() else b"")
    blob = b"\n".join(scan)
    assert not [n for n in names if n.encode() in blob]
    leaked = [p for p in phones if p.encode() in b"\n".join(scan[:1])]
    assert not leaked
    for path in out.iterdir():                               # reports: only masked phones
        data_text = path.read_text(encoding="utf-8")
        assert not [p for p in phones if p in data_text]


def test_gitignore_covers_key_file_and_databases_at_repo_root():
    import fnmatch
    from .helpers import REPO
    patterns = [ln.strip() for ln in (REPO / ".gitignore").read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    for name in ("ledger.db.key", "ledger.db", "custom.db"):
        assert any(fnmatch.fnmatch(name, p) for p in patterns), name
