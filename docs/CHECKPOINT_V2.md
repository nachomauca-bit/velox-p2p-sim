# Checkpoint: alignment with the final deck (brief v2, section 8)

State on 26 Sep 2026, fixture mode and the committed Gemini cache give the same outcomes. B values are after *Receive next email* and *Run the control gate* on B-05 (all twelve documents).

## The twelve invoices and their outcome in A and B

| No | Document | A — As-is (no gate, AP keys) | B — To-be (control gate) |
|---|---|---|---|
| 1 | Nordwind Logistics, monthly logistics, contract CT-2025-001, 27,846.00 EUR | Email loop — untracked (no PO), then posted | **Human review**: amount above approval limit (≈ CHF 26,175 > CHF 25,000) → Stefan Keller, next approver, SLA 2 |
| 2 | Nordwind payment reminder reproducing invoice 1, sent to the store mailbox | Email loop, then **posted a second time** on duplicate record V-000117 | **Block**: duplicate of B-01; AP replies with status |
| 3 | Bright Agency, campaign, PO 4500117 + service confirmation | Posted by AP | **Post** |
| 4 | Bright Agency credit note on invoice 3 | Posted by AP on record V-000119: **unapplied credit** | **Post**, credit note linked to invoice 3 |
| 5 | FitOut Partners, milestone 2, PO 4500123 without service confirmation (the demo's next email) | Email loop — untracked (PO never keyed) | **Exception**: PO exists, no receipt or confirmation → Jonas Weber, receiver / requester, SLA 2 |
| 6 | Atlas Displays, 150 × 44.00 against PO 4500109 at 42.00 | Email loop — untracked (PO never keyed) | **Exception**: price or quantity mismatch → Sofia Brandt, buyer, SLA 2; **past SLA** in the cockpit |
| 7 | Shopsys, SaaS subscription, PO 4500131 + confirmation (USD) | Posted by AP | **Post** |
| 8 | QuickPrint, goods, PO 4500114 + receipt | Posted by AP | **Post** |
| 9 | Kaffee & Co, 180.00 EUR, no PO, sent to the store | Email loop — untracked (7 days in the store mailbox first) | **Post**, card / catalogue CAT-2026-001 (within CHF 500) |
| 10 | Cleanspace, cleaning France, contract CT-2025-003 | Email loop, then posted to the **wrong legal entity** (VDE record) | **Post**, contract match |
| 11 | SecureNet, managed service, PO 4500128 + confirmation | Email loop — untracked (PO never keyed) | **Post** |
| 12 | Harbor Freight, freight, contract CT-2025-004 (USD) | Email loop — untracked (no PO) | **Post**, contract match |

B: 8 Post, 2 Exception, 1 Block, 1 Human review (brief v2 section 4 asked for roughly 9 handled without a person and 3 to a named owner: here 8 posted touchless + 1 Block, and 3 to a named owner). A: 4 posted by AP, 8 in the untracked email loop.

## The four metrics (deck slide 11, definitions of A6)

| Metric | A | B | Deck baseline → target |
|---|---|---|---|
| First-pass match rate | 27.3% (3 of 11) | 72.7% (8 of 11) | <30% → 90% |
| Accounts per supplier | 2.33 (28 ÷ 12) | 1.17 (14 ÷ 12) | 2.33 → ≤1.2 |
| Touchless rate | 0% (0 of 12) | 72.7% (8 of 11) | ≈0% → 70% |
| Invoice cycle time (business days, median, simulated) | 15 (P90 21) | 0, same day (P90 2) | 26 → 3 |
| Registered same day (small indicator) | 0% | 100% | — |

Before B-05 is received, B shows 11 documents: first-pass match and touchless 80.0% (8 of 10).

## Where the UI wording differs from the deck

- The metric tile says **"Accounts per supplier"** (brief v2 section 1); slide 11 prints "Vendor master: accounts per supplier".
- **Cycle time**: A's median is 15 simulated business days, not the case's 26: four of the twelve (three clean PO invoices and the credit note) are posted by AP in 2 days, and six of the eight email-loop documents arrive at ap@ and skip the seven days in a store mailbox. A non-PO invoice sent to a store takes 25 on average (Assumptions, section 6); A-09 takes 27. B's median is 0 by construction: a clean invoice costs 0 days once it passes the gate, and 8 of the 11 documents that end up posted are clean. The deck's 3 days is the month-24 target set against benchmarks (A6), not a figure the simulation can reproduce.
- **First-pass match in B** is 72.7%, below the deck's 90% target: three of the eleven miss the first pass by design (B-02, the reminder, blocked but still counted as an invoice received; B-05, no service confirmation; B-06, price variance). B-01 matches its contract and goes to a person only for the approval limit. Card / catalogue counts as a commitment (brief v2 section 1); deck A6 names PO and contract.
- **Metric definitions against A6**: A6 defines vendor master quality on **active** records; the demo counts inactive records too, as the case's 2,800 ÷ 1,200 and slide 11's baseline of 2.33 do (active only, A would be 2.00). A6 runs cycle time from registration; the demo runs it from arrival, so A includes the wait before registration (from registration, A would be 14).
- **Credit note**: the outcome is Post with the badge "credit note linked"; the outcome card links to the invoice it credits (the badge does not repeat the invoice number).
- **B-05 and the worked example of A5**: the demo's B-05 is the A5 invoice, with three differences: confidence 100% (the real reading) instead of 0.97; a draft message that is not sent instead of the agent's Chat message with a confirm button (the agent is phase 3); the full SLA of 2 days instead of 1.5. B-05 is also above the approval limit (about CHF 45,120), but the gate stops at the first failed rule, so the limit is checked only when the gate runs again after the confirmation.
- **Block**: the rule log names AP (Marco Ruiz) as the one who replies to the supplier; SLA "—", no owner task, as in A3.
- **As-is words**: scenario A has no gate, so it does not use the four outcomes: "Posted by AP", "Email loop — untracked", "Blocked (same account)". Its log is called "Processing log".
- **Screening** is simulated (`Screened → pass` for every file); the UI does not name Model Armor.
- **Stage strip**: the mock ERP shows purchase orders and receipts on one page, so that page highlights both "Buy" and "Receive or confirm".
- The approval matrix (one CHF 25,000 limit and one next approver per entity) and the FX rates to CHF are **simulated** and declared on the Assumptions page.
- Types of A3 that the twelve documents do not trigger (No PO, PO not found, Duplicate vendor record, Unknown vendor, Wrong legal entity, Credit note without invoice, Low extraction confidence, Supplier payment-status query) are covered by test set v2 and the unit tests.

## Open points

- No open bugs known. Full suite: 1,400+ tests pass; the cached real readings give the expected result for all 24 case-document results.
- Owner-message drafts are cached for B-01, B-05 and B-06. Without a key, a draft for another document says the key is needed; in fixture mode drafts are unavailable by design.
- The charts on the Metrics page load Chart.js from a CDN; offline, the data table under each chart still shows the numbers.
- Test set v2, document 13: the real model reads the degraded scan confidently, so live mode posts it where fixture mode sends it to Human review (explained in LIVE_VALIDATION.md).
