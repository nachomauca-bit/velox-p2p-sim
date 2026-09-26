# Live validation — real Gemini extractions

Generated 2026-09-26 10:53 by `python -m app.validate_live` (`make validate`) from the extraction cache; no API call is made by this report. Ground truth: `tests/fixtures*/` (what is printed on each PDF). Expected gate results: `tests/golden.yaml`, `tests/golden_v2.yaml`.

## 1. Extraction accuracy (PDFs; the UBL e-invoice and the email-body invoice need no model)

- Documents extracted by a model: **36**
- Models: gemini-3.8-flash (36)
- Fields matching the ground truth: **648 of 648** (100.0%)
- Latency per document: median 4.9 s, max 7.3 s
- Tokens: 53,874 input, 40,388 output (incl. thinking)
- Cost of these extractions at Google's list price of 25 Sep 2026: about **0.19 USD**

| Field | Correct | Of |
|---|---|---|
| `doc_type` | 36 | 36 |
| `supplier_name` | 36 | 36 |
| `supplier_vat_id` | 36 | 36 |
| `supplier_iban` | 36 | 36 |
| `supplier_country` | 36 | 36 |
| `bill_to_name` | 36 | 36 |
| `bill_to_vat_id` | 36 | 36 |
| `invoice_number` | 36 | 36 |
| `invoice_date` | 36 | 36 |
| `due_date` | 36 | 36 |
| `payment_terms_days` | 36 | 36 |
| `currency` | 36 | 36 |
| `net_total` | 36 | 36 |
| `tax_total` | 36 | 36 |
| `gross_total` | 36 | 36 |
| `po_numbers` | 36 | 36 |
| `referenced_invoice_number` | 36 | 36 |
| `lines` | 36 | 36 |

### Differences

None.

### Critical fields below the 0.80 confidence threshold (to-be routes these to human review)

None.

## 2. Control-gate results on the real extractions

- **Case documents (v1)**: 24 of 24 document × scenario results as expected
- **Test set v2**: 51 of 52 document × scenario results as expected, 1 explained difference(s) (below)

### Explained differences

| Set | Doc | Scenario | Golden (fixture mode) | Live | Why |
|---|---|---|---|---|---|
| v2 | 13 | tobe | {"outcome": "human_review", "exception_type": "human_review", "owner_name": "Marco Ruiz", "sla_days": 1, "account": null, "posted": false, "touchless": false, … | {"outcome": "posted", "exception_type": null, "owner_name": null, "sla_days": null, "account": "V-000109", "posted": true, "touchless": true, "first_pass_match… | In fixture mode this degraded scan carries simulated low confidences, so the golden file expects human review. Gemini read the real scan correctly and with high confidence, so the to-be gate treats it like the clean invoice it is (a small store purchase matched to the store's card / catalogue commitment, within the CHF 500 limit). The confidence threshold still routes a document to review whenever the model itself is unsure. |
