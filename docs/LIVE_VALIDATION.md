# Live validation — real Gemini extractions

Generated 2026-09-25 18:14 by `python -m app.validate_live` (`make validate`) from the extraction cache; no API call is made by this report. Ground truth: `tests/fixtures*/` (what is printed on each PDF). Expected gate results: `tests/golden.yaml`, `tests/golden_v2.yaml`.

## 1. Extraction accuracy (PDFs; the UBL e-invoice and the email-body invoice need no model)

- Documents extracted by a model: **38**
- Models: gemini-3.8-flash (38)
- Fields matching the ground truth: **684 of 684** (100.0%)
- Latency per document: median 5.0 s, max 7.3 s
- Tokens: 56,810 input, 42,948 output (incl. thinking)
- Cost of these extractions at Google's list price of 25 Sep 2026: about **0.20 USD**

| Field | Correct | Of |
|---|---|---|
| `doc_type` | 38 | 38 |
| `supplier_name` | 38 | 38 |
| `supplier_vat_id` | 38 | 38 |
| `supplier_iban` | 38 | 38 |
| `supplier_country` | 38 | 38 |
| `bill_to_name` | 38 | 38 |
| `bill_to_vat_id` | 38 | 38 |
| `invoice_number` | 38 | 38 |
| `invoice_date` | 38 | 38 |
| `due_date` | 38 | 38 |
| `payment_terms_days` | 38 | 38 |
| `currency` | 38 | 38 |
| `net_total` | 38 | 38 |
| `tax_total` | 38 | 38 |
| `gross_total` | 38 | 38 |
| `po_numbers` | 38 | 38 |
| `referenced_invoice_number` | 38 | 38 |
| `lines` | 38 | 38 |

### Differences

None.

### Critical fields below the 0.80 confidence threshold (to-be routes these to human review)

None.

## 2. Control-gate results on the real extractions

- **Case documents (v1)**: 28 of 28 document × scenario results as expected
- **Test set v2**: 51 of 52 document × scenario results as expected, 1 explained difference(s) (below)

### Explained differences

| Set | Doc | Scenario | Golden (fixture mode) | Live | Why |
|---|---|---|---|---|---|
| v2 | 13 | tobe | {"outcome": "human_review", "exception_type": "human_review", "owner_name": "Marco Ruiz", "sla_days": 1, "posted": false, "touchless": false} | {"outcome": "posted", "exception_type": null, "owner_name": null, "sla_days": null, "posted": true, "touchless": true} | In fixture mode this degraded scan carries simulated low confidences, so the golden file expects human review. Gemini read the real scan correctly and with high confidence, so the to-be gate treats it like the clean invoice it is (DoA auto-approval under 500 EUR). The confidence threshold still routes a document to review whenever the model itself is unsure. |
