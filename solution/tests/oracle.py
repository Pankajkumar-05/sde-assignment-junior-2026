"""Independent, deliberately naive reference implementation of the assignment spec.

Shares NO code with the solution (no DB, no hashing, different parsing approach). It
re-reads the raw generated files and returns what the five reports should contain, so
the real pipeline can be diffed against it on any seed.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def o_header(h):
    return re.sub(r"[_\s]+", " ", h.strip().lower())


def o_phone(s):
    d = re.sub(r"\D", "", s)
    if len(d) == 12 and d[:2] == "91":
        d = d[2:]
    elif len(d) == 11 and d[0] == "0":
        d = d[1:]
    return "+91" + d if re.fullmatch(r"[6-9][0-9]{9}", d) else None


def o_date_ok(s):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            pass
    return False


def o_decimal_ok(s):
    s = re.sub(r"^(₹|rs\.?|inr)\s*", "", s, flags=re.I).replace(",", "")
    return re.fullmatch(r"\d+(\.\d+)?", s) is not None


def o_ts(s):
    if s.endswith("Z"):
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if "T" in s:
        return datetime.fromisoformat(s)
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def run_oracle(drops, schema_dir):
    schema_dir = Path(schema_dir)
    schemas = {}
    for p in schema_dir.glob("*.json"):
        j = json.loads(p.read_text(encoding="utf-8"))
        schemas[j["workflow_id"]] = j["fields"]
    disp = {}
    for r in csv.DictReader(open(schema_dir / "dispositions.csv", encoding="utf-8")):
        disp[(r["workflow_id"], r["code"])] = (r["is_terminal"] == "true", r["is_success"] == "true")

    leads = {}                      # (wf, phone) -> {"ref", "batch"}
    rejections, rows_total, warnings, merged = [], 0, 0, 0
    for drop in drops:
        for wf_dir in sorted((Path(drop) / "leads").iterdir()):
            fields = schemas[wf_dir.name]
            for f in sorted(wf_dir.glob("*.csv")):
                with open(f, encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.reader(fh))
                col = {}
                for i, h in enumerate(rows[0]):
                    for fd in fields:
                        names = {o_header(fd["name"])} | {o_header(a) for a in fd.get("aliases", [])}
                        if o_header(h) in names and fd["name"] not in col:
                            col[fd["name"]] = i
                n = 0
                for cells in rows[1:]:
                    if not any(c.strip() for c in cells):
                        continue
                    n += 1
                    rows_total += 1
                    vals = {k: (cells[i].strip() if i < len(cells) else "") for k, i in col.items()}
                    reason, w, clean = None, 0, {}
                    for fd in fields:
                        v = vals.get(fd["name"], "")
                        if v == "":
                            if fd.get("required"):
                                reason = f"missing_required:{fd['name']}"
                                break
                            continue
                        t = fd["type"]
                        if t == "phone":
                            ph = o_phone(v)
                            if ph is None:
                                reason = "invalid_phone"
                                break
                            clean["phone"] = ph
                        elif t == "date" and not o_date_ok(v):
                            w += 1
                        elif t == "decimal" and not o_decimal_ok(v):
                            w += 1
                        elif t == "enum" and v.lower() not in fd["values"]:
                            w += 1
                        if fd.get("reference"):
                            clean["ref"] = v
                    if reason:
                        rejections.append((wf_dir.name, f.stem, n, reason))
                        continue
                    warnings += w
                    key = (wf_dir.name, clean["phone"])
                    if key in leads:
                        merged += 1
                        leads[key]["ref"] = clean["ref"]
                    else:
                        leads[key] = {"ref": clean["ref"], "batch": f.stem}

    seen, events, ev_total, malformed, dups = set(), [], 0, 0, 0
    for drop in drops:
        edir = Path(drop) / "events"
        if not edir.is_dir():
            continue
        for f in sorted(edir.glob("*.jsonl")):
            for line in open(f, encoding="utf-8"):
                if not line.strip():
                    continue
                ev_total += 1
                try:
                    e = json.loads(line)
                    eid = e["event_id"]
                except Exception:
                    malformed += 1
                    continue
                if eid in seen:
                    dups += 1
                    continue
                seen.add(eid)
                events.append(e)

    def key_of(e):
        return (e["workflow_id"], o_phone(e["phone"])) if e.get("phone") else None

    call_owner = {}
    for e in events:
        if e["event_type"] != "disposition.override" and key_of(e) in leads:
            call_owner[(e["workflow_id"], e["call_id"])] = key_of(e)
    orphans = unknown = 0
    calls = defaultdict(lambda: defaultdict(lambda: {"i": None, "c": False, "f": False, "ov": [], "st": []}))
    for e in events:
        wf = e["workflow_id"]
        if e["event_type"] == "disposition.override":
            owner = call_owner.get((wf, e["call_id"]))
        else:
            owner = key_of(e) if key_of(e) in leads else None
        if owner is None:
            orphans += 1
            continue
        if e["event_type"].startswith("disposition.") and (wf, e["payload"].get("disposition_code")) not in disp:
            unknown += 1
            continue
        c = calls[owner][e["call_id"]]
        t = o_ts(e["occurred_at"])
        ty = e["event_type"]
        if ty == "call.initiated":
            c["i"] = t
        elif ty == "call.connected":
            c["c"] = True
        elif ty == "call.telephony_failed":
            c["f"] = True
        elif ty == "disposition.set":
            c["st"].append((t, e["event_id"], e["payload"]["disposition_code"]))
        elif ty == "disposition.override":
            c["ov"].append((t, e["event_id"], e["payload"]["disposition_code"]))

    batch = defaultdict(Counter)
    daily = defaultdict(lambda: [0, 0, set()])
    lead_rows = []
    for (wf, phone), L in leads.items():
        cl = []
        for cid, c in calls.get((wf, phone), {}).items():
            if c["i"] is None:
                continue
            pool = sorted(c["ov"]) or sorted(c["st"])
            cl.append((c["i"], cid, c["c"], c["f"], pool[-1][2] if pool else None))
        cl.sort(key=lambda x: (x[0], x[1]))
        if not cl:
            b = "yet_to_reach_out"
        elif any(x[4] and disp[(wf, x[4])][0] for x in cl):
            b = "completed"
        elif all(x[3] for x in cl):
            b = "in_progress_telephony_failed"
        else:
            b = "in_progress_dialed"
        batch[(wf, L["batch"])]["total_leads"] += 1
        batch[(wf, L["batch"])][b] += 1
        succ = False
        for t, cid, conn_, failed, code in cl:
            d = daily[(wf, t.astimezone(IST).strftime("%Y-%m-%d"))]
            d[0] += 1
            d[1] += conn_
            if code and disp[(wf, code)][1]:
                succ = True
                d[2].add(phone)
        cur = [x[4] for x in cl if x[4]]
        lead_rows.append({
            "workflow_id": wf, "external_ref": L["ref"], "batch_id": L["batch"],
            "phone_masked": "+91******" + phone[-4:], "bucket": b, "total_calls": str(len(cl)),
            "connected_calls": str(sum(x[2] for x in cl)),
            "last_call_at": cl[-1][0].astimezone(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30") if cl else "",
            "current_disposition": cur[-1] if cur else "", "ever_success": "true" if succ else "false"})
    lead_rows.sort(key=lambda r: (r["workflow_id"], r["external_ref"], r["batch_id"], r["phone_masked"]))

    bcols = ["total_leads", "yet_to_reach_out", "in_progress_telephony_failed",
             "in_progress_dialed", "completed"]
    return {
        "batch_summary.csv": [dict(workflow_id=k[0], batch_id=k[1], **{c: str(v[c]) for c in bcols})
                              for k, v in sorted(batch.items())],
        "daily_overview.csv": [dict(workflow_id=k[0], date=k[1], total_calls=str(v[0]),
                                    connected_calls=str(v[1]),
                                    connectivity_rate="%.4f" % (v[1] / v[0]),
                                    success_leads=str(len(v[2])))
                               for k, v in sorted(daily.items())],
        "lead_report.csv": lead_rows,
        "rejections.csv": [dict(workflow_id=a, batch_id=b, row_number=str(c), reason=d)
                           for a, b, c, d in sorted(rejections)],
        "data_quality.json": {
            "lead_rows_total": rows_total, "lead_rows_rejected": len(rejections),
            "rejections_by_reason": dict(Counter(r[3] for r in rejections)),
            "duplicate_lead_rows_merged": merged, "field_warnings": warnings,
            "events_total": ev_total, "malformed_events": malformed,
            "duplicate_events_dropped": dups, "orphan_events": orphans,
            "unknown_disposition_events": unknown},
    }
