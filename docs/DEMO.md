# Four-minute demo script (shown at slide 10)

**The message:** the machine resolves what arrives clean, people resolve what needs judgement. Gemini reads and classifies; deterministic rules decide; every exception has a type, a named owner and an SLA.

## Before the session (2 minutes)

1. **Cache-only mode.** In `.env`, leave `GEMINI_API_KEY` blank. The app then reads the committed Gemini readings in `data/cache/` and never calls the network. (With `EXTRACTOR=fixture` it reads ground-truth test data instead, and every page says so.) The app reads `.env` only when it starts.
2. `make run` and open http://127.0.0.1:8010. A fresh database starts demo-ready.
3. Press **Reset demo** in the header and confirm. Both scenarios are loaded and run offline; the to-be inbox opens with the panel "An email is on its way to ap@velox.com". The flash must say "Demo reset: both scenarios loaded and run (offline)."
4. Keep three tabs ready: **Inbox** (B — To-be), **Exception cockpit** (B), **Compare**. Vendor master (A) is useful for questions.

## 0:00 – 0:45 · 1. Inbox: registered on arrival

- Press **Receive next email**. A new card appears on the one intake address: "**Registered B-05 · Fri 2026-10-02 14:02 · ap@velox.com · clock started**".
- Say: *"From this minute the invoice exists and the clock runs."* In scenario A the same invoice would wait in a mailbox until AP opens it; in the store mailbox, about seven business days.

## 0:45 – 2:00 · 2. One invoice through the gate

- Open **B-05** (a milestone invoice for a store fit-out).
- **Gemini output**: the fields with a confidence per field and the document type (Invoice), and one provenance line, "Read by gemini-3.8-flash (cached)". Say: *"Gemini reads and classifies. It never decides."*
- Press **Run the control gate**. Read the **rule log** line by line: Registered on arrival → Screened → Read with Gemini → Confidence → Document type → Supplier (tax ID) → Legal entity → Duplicate → **Commitment → Exception**: the PO exists but no service confirmation is recorded.
- The **outcome**: *Exception: PO exists, no receipt or confirmation* · owner **Jonas Weber · Receiver / requester** (store development manager) · **SLA 2 business days**, with the due date. Optional: **Draft message to owner** shows two sentences labelled "Draft by Gemini — reviewed by AP"; nothing is sent.

## 2:00 – 2:45 · 3. Exception cockpit

- Open the **Exception cockpit** (B): "**3 open · 1 past SLA**", grouped by type, each with owner, SLA and days open.
- The one past SLA: **B-06**, price or quantity mismatch, owner the buyer Sofia Brandt, 4 days open against an SLA of 2.
- B-01 is a **Human review**: above the approval limit, with the next approver. B-02 is a **Block**: a payment reminder that reproduces B-01; AP replies to the supplier with the status.

## 2:45 – 4:00 · 4. Comparison A vs B

- Open **Compare**: the same twelve invoices, the four metrics side by side (simulated durations):
  - **First-pass match rate** 27.3% → 72.7%
  - **Accounts per supplier** 2.33 → 1.17
  - **Touchless rate** 0% → 72.7%
  - **Invoice cycle time** (business days, median) 15 → 0, the same day
  - and the small indicator **Registered same day** 0% → 100%.
- Close with the line: *"The machine resolves what arrives clean, people resolve what needs judgement."*

## Notes

- **Receive the next email before Compare.** Until B-05 arrives, B shows 11 documents (first-pass match and touchless 80.0%); after *Run the control gate* on B-05 it shows the twelve and the values above. That is expected.
- Nothing depends on the network: Reset demo, Run scenario and Run the control gate read the cache or the fixtures only. **Reset demo** restarts the demo at any time.
- If asked:
  - *"Where do these numbers come from?"* The Assumptions page lists every simulated number with its reason, including the durations and the approval limit.
  - *"Why does the reminder not get paid twice?"* The duplicate rule runs at supplier level on invoice number or on amount and date; in A the reminder lands on a second vendor record and is posted again.
  - *"Is this an ERP?"* No: read-only mock tables, the ERP stays the system of record; see the footer.
