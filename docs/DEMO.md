# Four-minute demo script (shown at slide 10)

**The message:** the machine resolves what arrives clean, people resolve what needs judgement. Gemini reads and classifies; deterministic rules decide; every exception has a type, a named owner and an SLA.

## What this demo shows (and what it does not)

**How to frame it** (about 15 seconds, at the move from slide 10, inside the first block): *"Not a forecast and not a product: the target process of slide 9 on twelve supplier documents, today (A) and redesigned (B). You will see where each root cause bites, and what the foundations and the gate each change."*

Scenario B assumes the phase-1 foundations are in place (slide 11): one intake address with registration on arrival, commitments by spend category, named exception owners, and a clean vendor master with one record per supplier and legal entity (1.17 against the target of 1.2 or less; phase 1 itself cleans the top 200 suppliers first). The gate itself is phase 2 (slide 11). The table and the questions below are for preparation, not to read aloud during the four minutes.

| Demo moment | What the audience sees | Where it sits in the deck |
|---|---|---|
| 1. Receive next email | "Registered B-05 · … · clock started" on the one intake address | **RC3** · several mailboxes, nothing registered on arrival (slide 5); stage 04 of slide 9. In A this invoice (A-05) arrives at ap@ and is registered only when AP opens the mailbox the next business day. The two documents sent to the store mailbox (A-02, A-09) wait seven business days |
| 2. Gemini output | Fields, a confidence per field and the document type; nothing is decided | The lesson of the **AI quick-fix** (slide 6): *"The AI reads and classifies. Rules decide."* Part of the "Understand" layer of slide 10: the same model (Gemini 3.8 Flash) and the same output, read through the Gemini API with a key rather than on the Agent Platform |
| 2. Rule log: Supplier (tax ID) | The supplier is found by tax ID, at supplier level, not by name | **RC1** · vendor master with no owner (slide 5). FitOut has a single record in A too, so RC1 shows later: in the cockpit B-02 is blocked; in Compare, A-02 (the reminder) is found by name on the duplicate record V-000117 and posted a second time ("duplicate posting"), the duplicate payment of slide 7 in the making. Accounts per supplier 2.33 → 1.17 |
| 2. Rule log: Commitment → Exception | The PO exists but no service confirmation is recorded | **RC2** · purchases with nothing to match against (slide 5). The fix: a commitment before the invoice, by spend category, with services confirmed by the requester (stages 01 and 03 of slide 9) |
| 2. Outcome | A typed exception, a named owner (receiver / requester) and an SLA; optionally a two-sentence draft by Gemini, reviewed by AP; nothing is sent | **RC4** · exceptions by email, no owner, no clock (slide 5). The fix: every exception has a type, an owner and an SLA (slide 5; taxonomy in A3). The draft stands in for "the agent drafts the message" (slide 9, stage 06); here it is one Gemini call. The agent itself (slide 10, "Act") is phase 3 (slide 11; E5 in A8) and is not built |
| 3. Exception cockpit | 3 open, 1 past SLA; a duplicate blocked before posting; a human review above the approval limit | **RC4**: the exception cockpit that the weekly review of exceptions and SLAs works from (slides 5 and 9); the review itself is governance. Two controls of slide 7 are on this page: the duplicate check at supplier level (B-02 blocked before posting) and an approval limit with a named next approver (B-01; one simulated CHF 25,000 limit per entity, not the full delegation matrix). The other two show in Compare: credit notes linked to their invoice (A-04 "unapplied credit", B-04 "credit note linked") and terms from the master (A-01, A-06 "terms paid early", A-02 "terms paid late") |
| 4. Compare | The four metrics of slide 11, A vs B | Slide 11, defined as in A6 with three differences named in each tooltip: card / catalogue counts as a commitment; inactive records are counted, as in the case's 2,800 ÷ 1,200 (A6 writes "active records", which would give 2.00 in A); cycle time runs from arrival, so A includes the wait before registration. It also illustrates the split of slide 6: of the 11 documents that end up posted, 8 go through with no human touch (the credit note included) and 3 go to a named owner; the reminder is blocked. The split follows from how the twelve were chosen; it is not a measurement |

**RC5** (governance) is not software. The demo shows only what governance would review: the four metrics of slide 11, with today's process (A) as the reference; the real baseline is frozen in phase 0 from real data. The owners RC5 names (a P2P process owner and a vendor master owner) and the monthly CFO review are not in the demo.

**What the demo does not show** (it is in the deck):
- Governance and change: the P2P process owner and the vendor master owner, the weekly and monthly reviews, policy resistance in stores (slides 5, 9 and 11; risk register A7).
- The roadmap and its entry criteria (slide 11, pipeline A8), the controls matrix and segregation of duties (slide 7, A4), the business case built in phase 0 (slide 11).
- The real architecture (slide 10, A5): the Gmail intake, Gemini on the Agent Platform inside the trust layer (EU Data Boundary, customer-managed keys, audit logs), Document AI for poor scans, Model Armor (the screening step here is simulated), the Gemini Enterprise agent, BigQuery and Looker, and the integration with the real ERP. Here the ERP is a set of read-only mock tables.
- A live model call and real books: the Gemini readings were made in advance with gemini-3.8-flash and are replayed from the cache. "Post" is the gate's decision written to a mock table, with no ledger and no payment run behind it.

## Before the session (2 minutes)

1. **Cache-only mode.** In `.env`, leave `GEMINI_API_KEY` blank. The app then reads the committed Gemini readings in `data/cache/` and never calls the network. (With `EXTRACTOR=fixture` it reads ground-truth test data instead, and every page says so.) The app reads `.env` only when it starts.
2. `make run` and open http://127.0.0.1:8010. A fresh database starts demo-ready.
3. Press **Reset demo** in the header and confirm. Both scenarios are loaded and run offline; the to-be inbox opens with the panel "An email is on its way to ap@velox.com". The flash must say "Demo reset: both scenarios loaded and run (offline)."
4. Keep three tabs ready: **Inbox** (B — To-be), **Exception cockpit** (B), **Compare**. Vendor master (A) is useful for questions.

## 0:00 – 0:45 · 1. Inbox: registered on arrival

- Frame the demo (above), then press **Receive next email**. A new card appears on the one intake address: "**Registered B-05 · Fri 2026-10-02 14:02 · ap@velox.com · clock started**".
- Say: *"From this minute the invoice exists and the clock runs."* In scenario A this invoice waits in ap@ until AP opens the mailbox the next business day; an invoice sent to a store mailbox waits about seven.

## 0:45 – 2:00 · 2. One invoice through the gate

- Open **B-05** (the FitOut milestone invoice of the worked example in A5) and press **Run the control gate**. It reads B-05 from the cache and fills the **Gemini output** panel and the rule log together; before that, the panel says "Not read yet".
- **Gemini output**: the fields with a confidence per field, the document type (Invoice) and one provenance line, "Read by gemini-3.8-flash (cached)". Say: *"Gemini reads and classifies. It never decides."*
- Read the **rule log** line by line: Registered on arrival → Screened → Read with Gemini → Confidence → Document type → Supplier (tax ID) → Legal entity → Duplicate → **Commitment → Exception**: the PO exists but no service confirmation is recorded.
- The **outcome**: *Exception: PO exists, no receipt or confirmation* · owner **Jonas Weber · Receiver / requester** (store development manager) · **SLA 2 business days**, with the due date. Optional: **Draft message to owner** shows two sentences labelled "Draft by Gemini — reviewed by AP"; nothing is sent.

## 2:00 – 2:45 · 3. Exception cockpit

- Open the **Exception cockpit** (B): "**3 open · 1 past SLA**", grouped by type, each with owner, SLA and days open.
- The one past SLA: **B-06**, price or quantity mismatch, owner the buyer Sofia Brandt, 4 days open against an SLA of 2.
- B-01 is a **Human review**: above the approval limit, with the next approver. B-02 is a **Block**: a payment reminder that reproduces B-01; AP replies to the supplier with the status.

## 2:45 – 4:00 · 4. Comparison A vs B

- Open **Compare**: the same twelve documents, the four metrics side by side (simulated durations):
  - **First-pass match rate** 27.3% → 72.7%
  - **Accounts per supplier** 2.33 → 1.17
  - **Touchless rate** 0% → 72.7%
  - **Invoice cycle time** (business days, median) 15 → 0, the same day
  - and the small indicator **Registered same day** 0% → 100%.
- Close with the line: *"The machine resolves what arrives clean, people resolve what needs judgement."*

## Questions to prepare

- *"You designed the invoices to get this result."* Yes, on purpose. Three are clean PO invoices that both processes post. Each of the others carries one mechanism behind a symptom of the case: a reminder that reproduces an invoice, a credit note on another vendor record, a posting to the wrong legal entity, a PO never keyed, recurring spend with no PO, a store mailbox. In B they trigger four of the twelve exception types of A3; the other eight are covered by a second test set and the unit tests. The point is not the percentages: every outcome is explained by one line of the rule log.
- *"Accounts per supplier 1.17: you assumed it. How much of B is the gate?"* Yes. B starts from the clean master and the commitments that phase 1 delivers, so that tile shows the target and how it is measured, not something the gate achieves. Most of the first-pass gain is also phase 1: in A, three POs were never keyed (documents 5, 6 and 11), there is no catalogue, and AP matches POs only. What the gate itself changes is touchless and cycle time. That is the deck's order: fix the foundations, then automate (slide 2).
- *"First-pass match is 72.7%; the target is 90%."* Three of the eleven are built to miss the first pass: the reminder that reproduces B-01 (blocked as a duplicate, but still counted as an invoice received), B-05 with no service confirmation and B-06 with a price variance. B-01 matches its contract; it goes to a person only because it is above the approval limit. The 90% is the month-24 target over the whole invoice population (slide 11).
- *"Cycle time 0 days; the target is 3."* In the simulation a clean invoice costs 0 days once it passes the gate, and 8 of the 11 documents that end up posted are clean, so the median is 0 by construction. In practice, ERP posting, the approval workflow and batch timing add time. The 3 days of slide 11 is the month-24 target (5 at month 12), set against benchmarks (A6: APQC median 5.0 calendar days, top performers 2.8), not read from this simulation. What the simulation does show is the tail: an exception costs its SLA (2 days for B-01 and B-05), and B-06, resolved past its SLA, takes 5.
- *"And the 26 days of the case?"* The as-is median is 15 simulated days, for two reasons: four of the twelve are posted by AP in 2 days (three clean PO invoices and the credit note), and six of the eight email-loop documents arrive at ap@, so they skip the seven days in a store mailbox (15–21 days). The durations are calibrated on the worst path: a non-PO invoice sent to a store takes 25 days on average (the app's Assumptions page, section 6); here A-09 takes 27.
- *"Is your as-is representative?"* Not of the mix: 8 of 12 documents go to the email loop against the case's 45%, because the twelve were chosen so that each mechanism appears once. Phase 0 measures the real mix. The durations per path are calibrated on the case.
- *"B-05 is 48,000 EUR: why is it not above the approval limit?"* It is (about CHF 45,120), but the gate stops at the first rule that fails, so the approval limit is not checked yet. When Jonas confirms the service, the gate runs again, as in A5 ("Confirmed → receipt recorded in the ERP → gate re-runs"), and the amount then sends it to the next approver. The simulated 2 days count only the confirmation.
- *"Every confidence reads 100%: what does it mean?"* It is the model's own score in the structured output, not a calibrated probability; the cached readings of these PDFs are all 0.95 or higher. That is why rules decide, and why the deck adds Document AI for poor scans (slide 10) and a golden test set (A7).
- *"This is the A5 invoice, but it does not match A5."* Three differences, for simplicity: A5 shows confidence 0.97, the demo 100% (the real reading); A5 has the agent send a Chat message with a confirm button, the demo shows a draft that is not sent (the agent is phase 3); A5 counts 1.5 days, the demo charges the full SLA of 2.
- *"Are you rebuilding the ERP's controls?"* No. The gate checks on arrival so that every problem gets a type and an owner before anything is posted (slide 10 lists the rules of the gate). The ERP keeps matching, the duplicate check and the approval workflow switched on as the system-of-record control (slide 8; Q2 in A8).
- *"Why Swiss francs?"* Velox's group limits (approval limit, catalogue limit) are set in the group currency. Invoices keep their own currency; for the limit checks only, amounts are converted at fixed simulated rates (Assumptions page, sections 1 and 8).
- *"Where do these numbers come from?"* The Assumptions page lists every simulated number with its reason, including the durations and the approval limit.
- *"Why does the reminder not get paid twice?"* The duplicate rule runs at supplier level on invoice number, or on amount and date. In A the reminder lands on a second vendor record and is posted again.
- *"Is this an ERP?"* No: read-only mock tables; the ERP stays the system of record. See the footer.

## Notes

- **Receive the next email before Compare.** Until B-05 arrives, B shows 11 documents (first-pass match and touchless 80.0%); after *Run the control gate* on B-05 it shows the twelve and the values above. That is expected.
- Nothing depends on the network: Reset demo, Run scenario and Run the control gate read the cache or the fixtures only. **Reset demo** restarts the demo at any time. Without an API key the draft box shows the cached draft and no "Draft again" button.
