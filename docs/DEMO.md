# Three-minute demo script

**The message:** the problem starts upstream of the invoice (master data, purchasing discipline, intake, exception design). The AI quick-fix failed because it automated intake on top of all that. Fix the foundations, then automate on clean inputs behind a control gate.

## Before the audience arrives (2 minutes)

1. `make seed` (fresh database, 14 documents in both inboxes), then `make run` and open http://127.0.0.1:8010.
2. Extraction must be in the cache: `make extract` once with `GEMINI_API_KEY` in `.env` (after that the demo runs offline). Without a key, set `EXTRACTOR=fixture` in `.env` and say so: the fields are then ground truth, not Gemini output, and the UI labels them.
3. Reset both scenarios so nothing is processed yet: open http://127.0.0.1:8010/inbox?scenario=asis and press **Reset**, then http://127.0.0.1:8010/inbox?scenario=tobe and press **Reset**.
4. Keep these tabs ready: Inbox (A), Vendor master (A), Compare.

## 0:00 – 0:30 · The as-is world (scenario A)

- **Inbox** (`?scenario=asis`): two mailboxes. Three documents sit in the Berlin store mailbox and will only reach AP after about 7 business days; nothing is registered yet.
- **Vendor master**: "**28 accounts / 12 suppliers = 2.3**", the same ratio as the case (2,800 / 1,200). Point at Nordwind: five accounts, spelled differently, with different IBANs and terms, created by `store.berlin01`, `ap.temp` and a Paris store. *"Anyone could create a supplier."*

## 0:30 – 1:15 · Run the as-is process

- Press **Run scenario**. The step log scrolls: the quick-fix tool matches suppliers **by name**.
- Open **A-02** (the Nordwind reminder, printed "NORDWIND LOGISTICS"): it lands on duplicate account V-000117 and is **posted a second time**, 27,846.00 EUR.
- Open **A-04** (Bright Agency credit note): it lands on V-000119, not on the invoice's account, so the credit stays **unapplied**.
- **Exception cockpit**: a single bucket, "**Email loop — untracked**": nine documents, no owner, no SLA.

## 1:15 – 2:15 · The to-be world (scenario B)

- Switch to **B — To-be**. **Vendor master**: 16 accounts / 12 suppliers, every account linked to its party, VAT ID and IBAN filled, terms from the contract.
- **Inbox**: one intake channel; every document is registered on arrival.
- Press **Run scenario**. Walk through three invoice pages:
  - **B-02**: resolved by **VAT ID** to the same supplier as B-01, same normalised invoice number and amount → **blocked duplicate**, the supplier gets a status reply.
  - **B-06**: 150 × 44.00 against PO 4500109 at 42.00, +300.00 outside the tolerance → exception to the **buyer, Sofia Brandt**, SLA 2 days, with the reason in one sentence. (With a key: **Draft message to owner** writes the two-sentence note, labelled "Draft by Gemini — reviewed by AP".)
  - **B-10**: coffee for the Berlin store, 180.00 EUR, no PO → auto-approved under the 500 EUR delegation-of-authority limit; the store manager is informed.
- **Exception cockpit**: four exceptions, each **with an owner and an SLA** (Jonas Weber, Sofia Brandt, Tim Koch → Sofia Brandt, Marco Ruiz), plus info tasks.

## 2:15 – 3:00 · The comparison

- **Compare**, same 14 documents:
  - Touchless **35.7% → 71.4%**; exceptions 9 untracked → 4 owned with an SLA.
  - Duplicate postings **2 → 0** (duplicate blocked); credit notes unapplied **1 → 0**; wrong-entity postings **2 → 0**.
  - Simulated cash leakage **29,646.00 EUR → 0.00**.
  - PO / contract coverage **30.8% → 92.3%**: the upstream fix is what makes automation possible.
  - Cycle time, shown both ways: the sample average (**13.1 → 0.5 days**) and the case's typical failure, a non-PO invoice sent to a store (**25 → 3 days**, 0 under the DoA limit).
- Close on the architecture message in the left navigation: the ERP stays the **system of record**; the **control gate sits in front of it**. Gemini only reads documents, with a confidence per field; every decision is a deterministic, explainable rule (open any invoice's gate trace).

## If asked

- *"Is the 26-day cycle reproduced?"* The reference path is 25 days; the sample average is lower because the 14 documents include clean PO invoices (Assumptions, section 6).
- *"What does the AI do?"* Document understanding only: structured output with per-field confidence; below 0.80 on a critical field the document goes to human review.
- *"Is this an ERP?"* No: mock tables modelled on Dynamics 365 Finance concepts, read-only; see the footer and the Assumptions page.
