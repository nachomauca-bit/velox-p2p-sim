"""Live validation: how the real Gemini extractions in the cache compare with the ground truth, and whether the
control gate still gives the expected results (tests/golden.yaml, tests/golden_v2.yaml) when it runs on them.

Never calls the API: it reads data/cache only (run `make extract` / `make extract DATASET=v2` first). Uses its own
temporary database, so the app's data/velox.db is untouched. Writes docs/LIVE_VALIDATION.md.

CLI:  python -m app.validate_live [--no-report]
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Own database and cache-only extraction: set before any app module reads the settings.
_TMP = Path(tempfile.mkdtemp(prefix="velox-validate-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'validate.db').as_posix()}"
os.environ["EXTRACTOR"] = "gemini"

import argparse  # noqa: E402
import json  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any, Optional  # noqa: E402

import yaml  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import config, extract, gate, seed, world  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import GateDecision  # noqa: E402
from app.normalize import (  # noqa: E402
    normalise_currency,
    normalise_iban,
    normalise_invoice_number,
    normalise_po_number,
    normalise_vat,
)

REPORT = config.DOCS_DIR / "LIVE_VALIDATION.md"
GOLDEN = {"v1": config.BASE_DIR / "tests" / "golden.yaml", "v2": config.BASE_DIR / "tests" / "golden_v2.yaml"}
DECISION_COLUMNS = ("outcome", "exception_type", "owner_name", "sla_days", "simulated_days")
# Google's list price for gemini-3.8-flash (paid tier) on 25 Sep 2026, USD per 1M tokens (output incl. thinking).
PRICE_PER_M = {"gemini-3.8-flash": (0.75, 3.75)}

# Expected differences between the live run and the golden files (which describe fixture mode), with the reason.
KNOWN_DIFFERENCES = {
    ("v2", 13, "tobe"): "In fixture mode this degraded scan carries simulated low confidences, so the golden file "
                        "expects human review. Gemini read the real scan correctly and with high confidence, so the "
                        "to-be gate treats it like the clean invoice it is (a small store purchase matched to the "
                        "store's card / catalogue commitment, within the CHF 500 limit). The "
                        "confidence threshold still routes a document to review whenever the model itself is unsure.",
}

# Fields compared with the ground truth; notes are free text and not compared.
COMPARED = [f for f in extract.FIELDS if f != "notes"]


# --------------------------------------------------------------------------------------------
# Field accuracy
# --------------------------------------------------------------------------------------------


def _num_equal(a: Any, b: Any) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


def field_matches(name: str, expected: Any, got: Any) -> bool:
    """Equal in the sense the gate uses the field (normalised identifiers, numbers to the cent)."""
    if expected is None or got is None:
        return expected is None and got is None
    if name in ("supplier_vat_id", "bill_to_vat_id"):
        return normalise_vat(expected) == normalise_vat(got)
    if name == "supplier_iban":
        return normalise_iban(expected) == normalise_iban(got)
    if name in ("invoice_number", "referenced_invoice_number"):
        return normalise_invoice_number(expected) == normalise_invoice_number(got)
    if name == "currency":
        return normalise_currency(expected) == normalise_currency(got)
    if name == "po_numbers":
        return sorted(map(normalise_po_number, expected)) == sorted(map(normalise_po_number, got))
    if name == "lines":
        return len(expected) == len(got) and all(
            _num_equal(e.get("amount"), g.get("amount"))
            and (e.get("quantity") is None or _num_equal(e.get("quantity"), g.get("quantity")))
            for e, g in zip(expected, got))
    if name in ("net_total", "tax_total", "gross_total"):
        return _num_equal(expected, got)
    if name == "supplier_name" or name == "bill_to_name":
        return str(expected).strip().casefold() == str(got).strip().casefold()
    return expected == got


def _fixture_for(spec: world.DocumentSpec) -> Optional[dict[str, Any]]:
    folder = config.FIXTURES_V2_DIR if spec.dataset == "v2" else config.FIXTURES_DIR
    path = folder / f"{Path(spec.filename).stem}.json"
    return json.loads(path.read_text(encoding="utf-8"))["extraction"] if path.exists() else None


def _file_for(spec: world.DocumentSpec) -> Path:
    return (config.INVOICES_V2_DIR if spec.dataset == "v2" else config.INVOICES_DIR) / spec.filename


def field_accuracy() -> dict[str, Any]:
    """Compare every cached model extraction with its ground-truth fixture (PDFs only: UBL and email body skipped)."""
    docs, mismatches, per_field = [], [], defaultdict(lambda: [0, 0])
    tokens_in = tokens_out = 0
    models: Counter = Counter()
    latencies: list[int] = []
    for dataset in world.DATASETS:
        for spec in world.documents_for(dataset):
            truth = _fixture_for(spec)
            if spec.content != "pdf" or truth is None:
                continue
            cached = extract.read_cache(extract.file_sha256(_file_for(spec)))
            if cached is None:
                docs.append({"dataset": dataset, "no": spec.no, "file": spec.filename, "status": "not extracted"})
                continue
            models[cached.model] += 1
            tokens_in += cached.input_tokens or 0
            tokens_out += cached.output_tokens or 0
            if cached.latency_ms:
                latencies.append(cached.latency_ms)
            ok = 0
            for name in COMPARED:
                exp, got = truth[name]["value"], cached.data[name]["value"]
                match = field_matches(name, exp, got)
                per_field[name][0] += match
                per_field[name][1] += 1
                ok += match
                if not match:
                    mismatches.append({"dataset": dataset, "no": spec.no, "field": name, "expected": exp, "got": got,
                                       "confidence": cached.data[name]["confidence"]})
            low = extract_low_confidence(cached.data)
            docs.append({"dataset": dataset, "no": spec.no, "file": spec.filename, "status": "extracted",
                         "fields_ok": ok, "fields": len(COMPARED), "model": cached.model, "low_confidence": low,
                         "min_critical": min_critical(cached.data)})
    return {"docs": docs, "mismatches": mismatches, "per_field": dict(per_field), "models": models,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "latencies": latencies}


def min_critical(data: dict[str, Any]) -> Optional[float]:
    identity = [data[f]["confidence"] for f in extract.SUPPLIER_IDENTITY_FIELDS if data[f]["value"] is not None]
    others = [data[f]["confidence"] for f in ("invoice_number", "gross_total", "bill_to_name")]
    values = ([max(identity)] if identity else [0.0]) + others
    return round(min(values), 2) if values else None


def extract_low_confidence(data: dict[str, Any]) -> list[str]:
    return gate.low_confidence_fields(data, config.CONFIDENCE_THRESHOLD)


# --------------------------------------------------------------------------------------------
# Gate outcomes on the real extractions
# --------------------------------------------------------------------------------------------


def _actual(row: dict[str, Any], key: str) -> Any:
    if key == "account":
        return row["details"].get("account_id")
    if key == "flags":
        return [flag["type"] for flag in row["details"].get("flags") or []]
    if key in DECISION_COLUMNS:
        return row[key]
    return row["details"].get(key)


def gate_agreement() -> dict[str, Any]:
    """Run both scenarios per dataset on the cached extractions (no API) and compare with the golden files."""
    init_db(drop=True)
    results: dict[str, Any] = {}
    with SessionLocal() as session:
        seed.seed_all(session)
        for dataset in world.DATASETS:
            golden = yaml.safe_load(GOLDEN[dataset].read_text(encoding="utf-8"))["documents"]
            rows, missing = {}, []
            for scenario in config.SCENARIOS:
                docs = seed.load_sample_documents(session, scenario, dataset)
                extract.extract_documents(session, docs, allow_api=False)
                missing += [d.doc_id for d in docs if d.extraction is None and d.content_type != "email_body"]
                gate.run_scenario(session, scenario, allow_api=False, log=lambda line: None)
                for d in session.scalars(select(GateDecision).where(GateDecision.scenario == scenario)):
                    if d.details.get("sample_no"):
                        rows[(scenario, d.details["sample_no"])] = {
                            **{c: getattr(d, c) for c in DECISION_COLUMNS}, "details": dict(d.details),
                            "reason": d.reason}
            diffs, known, total = [], [], 0
            for no, per_scenario in golden.items():
                for scenario, expected in per_scenario.items():
                    total += 1
                    row = rows.get((scenario, no))
                    got = {k: _actual(row, k) for k in expected} if row else None
                    if got != expected:
                        (known if (dataset, no, scenario) in KNOWN_DIFFERENCES else diffs).append({"no": no, "scenario": scenario,
                                      "expected": {k: v for k, v in expected.items() if not got or got.get(k) != v},
                                      "got": {k: got.get(k) for k in expected if got.get(k) != expected[k]} if got
                                      else None, "reason": row["reason"] if row else None})
            results[dataset] = {"total": total, "diffs": diffs, "known": known, "missing": sorted(set(missing))}
    return results


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("|", "\\|")
    return text if len(text) <= 160 else text[:159] + "…"


def render(fields: dict[str, Any], gates: dict[str, Any], generated: str) -> str:
    extracted = [d for d in fields["docs"] if d["status"] == "extracted"]
    not_extracted = [d for d in fields["docs"] if d["status"] != "extracted"]
    total_ok = sum(d["fields_ok"] for d in extracted)
    total_fields = sum(d["fields"] for d in extracted)
    lines = [
        "# Live validation — real Gemini extractions",
        "",
        f"Generated {generated} by `python -m app.validate_live` (`make validate`) from the extraction cache; no API "
        "call is made by this report. Ground truth: `tests/fixtures*/` (what is printed on each PDF). Expected gate "
        "results: `tests/golden.yaml`, `tests/golden_v2.yaml`.",
        "",
        "## 1. Extraction accuracy (PDFs; the UBL e-invoice and the email-body invoice need no model)",
        "",
        f"- Documents extracted by a model: **{len(extracted)}**"
        + (f"; not extracted yet: {len(not_extracted)} ({', '.join(f'{d['dataset']} #{d['no']}' for d in not_extracted)})"
           if not_extracted else ""),
        f"- Models: {', '.join(f'{m} ({n})' for m, n in fields['models'].items()) or '—'}",
        f"- Fields matching the ground truth: **{total_ok} of {total_fields}**"
        + (f" ({100 * total_ok / total_fields:.1f}%)" if total_fields else ""),
    ]
    if fields["latencies"]:
        lat = sorted(fields["latencies"])
        lines.append(f"- Latency per document: median {lat[len(lat) // 2] / 1000:.1f} s, max {lat[-1] / 1000:.1f} s")
    if fields["tokens_in"]:
        lines.append(f"- Tokens: {fields['tokens_in']:,} input, {fields['tokens_out']:,} output (incl. thinking)")
        price = PRICE_PER_M.get(next(iter(fields["models"]), ""), None)
        if price:
            cost = fields["tokens_in"] / 1e6 * price[0] + fields["tokens_out"] / 1e6 * price[1]
            lines.append(f"- Cost of these extractions at Google's list price of 25 Sep 2026: about **{cost:.2f} USD**")
    lines += ["", "| Field | Correct | Of |", "|---|---|---|"]
    for name in COMPARED:
        ok, n = fields["per_field"].get(name, [0, 0])
        lines.append(f"| `{name}` | {ok} | {n} |")
    lines += ["", "### Differences", ""]
    if fields["mismatches"]:
        lines += ["| Set | Doc | Field | Expected | Gemini | Confidence |", "|---|---|---|---|---|---|"]
        for m in fields["mismatches"]:
            lines.append(f"| {m['dataset']} | {m['no']} | `{m['field']}` | {_fmt(m['expected'])} | {_fmt(m['got'])} "
                         f"| {m['confidence']:.2f} |")
    else:
        lines.append("None.")
    low = [d for d in extracted if d["low_confidence"]]
    lines += ["", "### Critical fields below the 0.80 confidence threshold (to-be routes these to human review)", ""]
    lines += [f"- {d['dataset']} #{d['no']} `{d['file']}`: {', '.join(d['low_confidence'])} "
              f"(lowest {d['min_critical']})" for d in low] or ["None."]
    lines += ["", "## 2. Control-gate results on the real extractions", ""]
    for dataset, res in gates.items():
        ok = res["total"] - len(res["diffs"]) - len(res["known"])
        lines.append(f"- **{'Case documents (v1)' if dataset == 'v1' else 'Test set v2'}**: {ok} of {res['total']} "
                     "document × scenario results as expected"
                     + (f", {len(res['known'])} explained difference(s) (below)" if res["known"] else "")
                     + (f"; documents without an extraction: {', '.join(res['missing'])}" if res["missing"] else ""))
    for dataset, res in gates.items():
        if res["diffs"]:
            lines += ["", f"### Differences — {dataset}", "", "| Doc | Scenario | Expected | Got | Reason |",
                      "|---|---|---|---|---|"]
            for d in res["diffs"]:
                lines.append(f"| {d['no']} | {d['scenario']} | {_fmt(d['expected'])} | {_fmt(d['got'])} "
                             f"| {_fmt(d['reason'] or '')} |")
    known = [(dataset, d) for dataset, res in gates.items() for d in res["known"]]
    if known:
        lines += ["", "### Explained differences", "", "| Set | Doc | Scenario | Golden (fixture mode) | Live | Why |",
                  "|---|---|---|---|---|---|"]
        for dataset, d in known:
            why = KNOWN_DIFFERENCES[(dataset, d["no"], d["scenario"])].replace("|", "/")
            lines.append(f"| {dataset} | {d['no']} | {d['scenario']} | {_fmt(d['expected'])} | {_fmt(d['got'])} "
                         f"| {why} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the cached Gemini extractions and the gate results.")
    parser.add_argument("--no-report", action="store_true", help="print only; do not write docs/LIVE_VALIDATION.md")
    args = parser.parse_args(argv)
    fields = field_accuracy()
    gates = gate_agreement()
    text = render(fields, gates, datetime.now().strftime("%Y-%m-%d %H:%M"))
    print(text)
    if not args.no_report:
        REPORT.write_text(text, encoding="utf-8", newline="\n")
        print(f"[validate] wrote {REPORT.relative_to(config.BASE_DIR)}")
    all_ok = not fields["mismatches"] and all(not r["diffs"] for r in gates.values())
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
