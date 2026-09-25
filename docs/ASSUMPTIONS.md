# Assumptions

Every simulated number in the demo, with one line of reasoning and the place in the code where it is defined. If a number changes in the code, this page must change with it (the tests in `tests/test_seed.py`, `tests/test_sim.py` and `tests/test_normalize.py` check the main counts quoted here).

## 1. Purpose and honesty statement

- The app simulates the redesigned Accounts Payable (procure-to-pay) invoice process of a fictional retailer, **Velox Retail**, for 14 supplier documents (the 12 of the brief plus two clean, everyday invoices; see section 11) under two scenarios: **A (as-is)** and **B (to-be)**.
- All companies, people, VAT IDs, bank accounts, invoice numbers and email addresses are fictional. IBANs are built with valid ISO 13616 check digits (`world.make_iban`) so they look and validate like real ones, but they belong to no one.
- The "ERP" is a handful of mock tables modelled on Dynamics 365 Finance **concepts** (legal entity, party / global address book, vendor account, contract, purchase order, product receipt, pending vendor invoice), with plain names. It is not an ERP: no ledger, payments, users, roles or currency conversion.
- Document understanding is done by a Gemini model through the Google GenAI SDK. Everything else is deterministic rules.
- Durations are simulated with a lookup table (section 6). The only random element is the length of the as-is email loop, and it is seeded.
- Single source of truth: `app/world.py` (clean world and the 14 documents); `app/seed.py` derives the dirty world by explicit rules.

## 2. Simulated calendar

- Business days are Monday to Friday, with no public holidays (`sim.add_business_days`, `sim.business_days_between`). Three countries' holiday calendars would add complexity without changing the message.
- Document dates run from 28 Sep to 15 Oct 2026: invoice dates 28 Sep – 2 Oct, arrival in the mailboxes 1 – 15 Oct (the Nordwind reminder arrives on 15 Oct).
- Purchase orders are dated 15 Jun – 21 Sep 2026 and receipts 14 Aug – 1 Oct 2026, so commitments exist before the invoices arrive. These are the to-be dates; in as-is, rule D6 removes the POs that were never keyed in the ERP.

## 3. Seed — clean world (scenario B, to-be)

Defined in `app/world.py`, loaded as-is into scenario `tobe` by `seed.seed_scenario`.

| Item | Count | Defined in | Reasoning |
|---|---|---|---|
| Legal entities | 3 | `world.LEGAL_ENTITIES` | VDE Velox Retail GmbH (Berlin, EUR), VFR Velox Retail SAS (Paris, EUR), VUS Velox Retail Inc. (New York, USD) |
| Suppliers (parties) | 12 | `world.PARTIES` | One per real supplier; mirrors the supplier types in the case |
| Vendor accounts | 16 | `world.CLEAN_ACCOUNTS` | One per party per legal entity it actually serves (in D365 accounts are per legal entity and share one party) |
| Purchase orders | 14 | `world.PURCHASE_ORDERS` | The 8 POs of the brief plus 6 background POs (the brief asks for about 14) |
| Receipt lines / receipt documents | 11 / 9 | `world.RECEIPTS` | About 9 receipts per the brief; one receipt document may cover several PO lines |
| Contract rows | 4 (3 supplier contracts) | `world.CONTRACTS` | Contracts are per legal entity; Cleanspace serves VDE and VFR, so it has two rows |

### Suppliers and vendor accounts

"Accounts as-is" counts every account that really belongs to the supplier, linked to its party or not (rules D1, D2, D5 below).

| No | Supplier | Country | Type | Agreed terms (days) | Entities served | Accounts to-be | Accounts as-is |
|---|---|---|---|---|---|---|---|
| 1 | Nordwind Logistics GmbH | DE | 3PL carrier, recurring contract | 30 | VDE | 1 | 5 |
| 2 | Bright Agency SARL | FR | Marketing agency, services with PO | 45 | VFR | 1 | 2 |
| 3 | FitOut Partners Ltd | GB | Store fit-out, milestone invoices with PO | 30 | VDE | 1 | 1 |
| 4 | Cleanspace Facilities BV | NL | Facilities, recurring contract | 30 | VDE, VFR | 2 | 4 |
| 5 | Shopsys Software Inc. | US | IT SaaS, subscription with PO | 30 | VDE, VUS | 2 | 3 |
| 6 | Atlas Displays SL | ES | Store fixtures (goods) with PO and receipt | 60 | VDE, VFR | 2 | 3 |
| 7 | Metro Media GmbH | DE | Regional marketing, services with PO | 30 | VDE | 1 | 3 |
| 8 | QuickPrint SAS | FR | Marketing collateral (goods) with PO | 30 | VDE, VFR | 2 | 2 |
| 9 | Kaffee & Co OHG | DE | Small local supplier to a Berlin store, no PO | 14 | VDE | 1 | 1 |
| 10 | SecureNet AG | CH | IT vendor, service with PO | 30 | VDE | 1 | 1 |
| 11 | Harbor Freight Forwarders Inc. | US | 3PL, recurring contract | 30 | VUS | 1 | 1 |
| 12 | Lumen Store Lighting Ltd | GB | Store lighting (goods) with PO | 30 | VDE | 1 | 2 |
| | **Total** | | | | | **16** | **28** |

### Purchase orders and receipts

Requester / buyer are people from `world.PEOPLE`. "As-is" = the PO exists in scenario A (rule D6). "Doc" = the sample document that invoices it.

| PO | Entity | Supplier (account) | Type | Lines | Total | Requester / buyer | Receipt or service confirmation | As-is | Doc |
|---|---|---|---|---|---|---|---|---|---|
| 4500101 | VDE | FitOut Partners Ltd (V-000103) | service | 1 × 40,000.00 (milestone 1) | 40,000.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0412, confirmed | yes | — |
| 4500105 | VFR | Atlas Displays SL (V-000115) | goods | 80 × 42.00 | 3,360.00 EUR | Luc Bernard / Julien Moreau | PR-26-0433, 80 of 80 | yes | — |
| 4500107 | VDE | Shopsys Software Inc. (V-000114) | service | 1 × 2,400.00 | 2,400.00 EUR | Felix Braun / Sofia Brandt | none (open) | no | — |
| 4500109 | VDE | Atlas Displays SL (V-000106) | goods | 150 × 42.00 | 6,300.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0451, 150 of 150 | no | 6 |
| 4500112 | VDE | Lumen Store Lighting Ltd (V-000112) | goods | 80 × 40.00 + 40 × 50.00 | 5,200.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0456, 80 + 20 = **100 of 120** | no | 12 |
| 4500114 | VFR | QuickPrint SAS (V-000108) | goods | 400 × 3.50 + 10,000 × 0.10 | 2,400.00 EUR | Camille Martin / Julien Moreau | PR-26-0447, all received | yes | 9 |
| 4500117 | VFR | Bright Agency SARL (V-000102) | service | 1 × 12,000.00 | 12,000.00 EUR | Camille Martin / Julien Moreau | PR-26-0449, confirmed | yes | 3 |
| 4500119 | VDE | QuickPrint SAS (V-000116) | goods | 500 × 2.20 | 1,100.00 EUR | Anna Schulz / Sofia Brandt | none (open) | no | — |
| 4500121 | VFR | Bright Agency SARL (V-000102) | service | 1 × 6,000.00 | 6,000.00 EUR | Camille Martin / Julien Moreau | none (open) | no | — |
| 4500123 | VDE | FitOut Partners Ltd (V-000103) | service | 1 × 48,000.00 (milestone 2) | 48,000.00 EUR | Jonas Weber / Sofia Brandt | **none** (deliberate) | no | 5 |
| 4500126 | VDE | Metro Media GmbH (V-000107) | service | 1 × 7,500.00 | 7,500.00 EUR | Anna Schulz / Sofia Brandt | PR-26-0458, confirmed | yes | 8 |
| 4500128 | VDE | SecureNet AG (V-000110) | service | 1 × 4,800.00 | 4,800.00 EUR | Felix Braun / Sofia Brandt | PR-26-0459, confirmed | no | 13 |
| 4500130 | VDE | SecureNet AG (V-000110) | service | 1 × 3,500.00 | 3,500.00 EUR | Felix Braun / Sofia Brandt | none (open) | no | — |
| 4500131 | VUS | Shopsys Software Inc. (V-000105) | service | 1 × 9,600.00 | 9,600.00 USD | Emily Carter / Daniel Price | PR-26-0461, confirmed | yes | 7 |

- The 8 POs of the brief are 4500109, 4500112, 4500114, 4500117, 4500123, 4500126, 4500128 and 4500131; the other 6 are background POs (earlier milestone, other entities, not yet delivered).
- 9 POs have a receipt document (11 receipt lines, because 4500112 and 4500114 have two lines each). 5 have none: four are simply not delivered yet; **4500123 (FitOut milestone 2) deliberately has no service confirmation**.
- **Lumen 4500112**: 100 of 120 units received (line 2: 20 of 40).

### Contracts

| Contract | Supplier | Entity | Expected monthly range (net) | Terms (days) | Owner |
|---|---|---|---|---|---|
| CT-2025-001 | Nordwind Logistics GmbH | VDE | 20,000–26,000 EUR | 30 | Nina Hoffmann |
| CT-2025-002 | Cleanspace Facilities BV | VDE | 3,000–3,400 EUR | 30 | Katrin Lange |
| CT-2025-003 | Cleanspace Facilities BV | VFR | 3,000–3,400 EUR | 30 | Claire Dubois |
| CT-2025-004 | Harbor Freight Forwarders Inc. | VUS | 15,000–19,000 USD | 30 | Ryan Brooks |

### Small modelling choices

- **Agreed terms on the party** (`party.agreed_terms_days`): only 3 suppliers have a recurring contract, so the party carries the terms agreed with every supplier (contract or supplier agreement). To-be accounts copy it; terms drift (D3) is measured against it.
- **Buyer on the PO** (`buyer_name`, `buyer_email`): the brief's PO has a requester only, but price and quantity exceptions go to the buyer (section 9 of the brief). Buyers: Sofia Brandt (DE), Julien Moreau (FR), Daniel Price (US).
- **Owners** are people from the seed: Master Data owner Lena Fischer, AP specialist Marco Ruiz, plus the requester, buyer and receiver of each PO and the owner of each contract.
- **Receipt ID shared by lines**: one receipt document covers several PO lines (PR-26-0456 covers both Lumen lines, PR-26-0447 both QuickPrint lines). Service confirmations are receipt rows with quantity 1 and kind `service_confirmation`.
- **US bank details**: US suppliers have no IBAN, so the `iban` column stores "ABA routing + account" (for example `ABA 121000248 ACCT 4839201756`). US parties and the VUS entity store their EIN in the VAT ID column; SecureNet stores its Swiss UID (CHE-419.287.563).
- **Net vs gross**: PO tolerance and contract ranges compare **net** amounts, because VAT is not part of the commitment and several invoices are reverse charge (0 VAT). The duplicate check uses **gross**, the amount that would be paid twice. Example: document 1 is 23,400.00 net, inside the Nordwind range of 20,000–26,000; its gross, 27,846.00, would fall outside.
- **Currencies**: the brief's simplifications are kept: SecureNet 4,800 CHF became 4,800 EUR, and Lumen 5,200 GBP became 5,200 EUR. FitOut (UK) also invoices in EUR. Only VUS business is in USD (Shopsys, Harbor Freight). The currency is stored and shown, **never converted**.
- **Clean accounts** are created by `finance.de`, `finance.fr` or `finance.us` (one per entity) and carry the note "Validated through the vendor request workflow; linked to its party."

## 4. Dirty world (scenario A, as-is) — rules D1–D6

`seed.derive_dirty_accounts` applies the rules to the clean accounts in the order **D1, D2, D3, D5, D4**. D4 runs last so it also covers accounts created by D1, D2 and D5. D6 applies to purchase orders (`seed.rule_d6_po_discipline`). Every changed or created account records its rules in `vendor_account.corruption_rules`.

### D1 — spelling duplicates (`seed.D1_DUPLICATES`)

Suppliers 1, 2, 4 and 7 get 1–2 extra accounts in the same legal entity, with a different spelling, a different IBAN and different terms (14 / 45 / 60), and no party link. They copy the supplier's VAT ID (D4 later empties some identifiers).

| New account | Copy of | Entity | Display name | Terms (agreed) | Created by | Created on | Note |
|---|---|---|---|---|---|---|---|
| V-000117 | V-000101 Nordwind Logistics GmbH | VDE | NORDWIND LOGISTICS | 14 (30) | ap.temp | 2023-11-06 | Created to pay an urgent reminder |
| V-000118 | V-000101 Nordwind Logistics GmbH | VDE | Nordwind Logistik GmbH | 60 (30) | store.berlin01 | 2024-08-19 | Created by the Berlin store for a delivery invoice |
| V-000119 | V-000102 Bright Agency SARL | VFR | Bright Agency | 60 (45) | finance.fr | 2024-02-27 | Created from a credit note email |
| V-000120 | V-000104 Cleanspace Facilities BV | VDE | Clean Space Facilities B.V. | 14 (30) | store.berlin01 | 2022-05-09 | Created by a store for monthly cleaning |
| V-000121 | V-000113 Cleanspace Facilities BV | VFR | CLEANSPACE FACILITIES | 45 (30) | store.paris02 | 2025-10-14 | Created by a Paris store |
| V-000122 | V-000107 Metro Media GmbH | VDE | Metro Media | 45 (30) | ap.temp | 2023-03-01 | Temporary account for a campaign invoice |
| V-000123 | V-000107 Metro Media GmbH | VDE | Metro-Media GmbH | 60 (30) | store.berlin01 | 2024-11-25 | Created by the Berlin store |

### D2 — cross-entity spread (`seed.D2_CROSS_ENTITY`)

| New account | Copy of | Entity | Display name | Terms (agreed) | Created by | Created on | Note |
|---|---|---|---|---|---|---|---|
| V-000124 | V-000101 Nordwind Logistics GmbH | VFR | Nordwind Logistics GmbH | 45 (30) | store.paris02 | 2025-02-11 | Created by a Paris store to pay a delivery |

Nordwind serves VDE only; a store user opened an account in VFR with the same name and IBAN and different terms.

### D3 — terms drift (`seed.D3_TERMS_DRIFT`)

The brief says "5 random accounts"; the code uses a **fixed list** spread across entities, so the result is reproducible.

| Account | Supplier | Entity | Agreed terms | Account terms |
|---|---|---|---|---|
| V-000103 | FitOut Partners Ltd | VDE | 30 | 60 |
| V-000105 | Shopsys Software Inc. | VUS | 30 | 45 |
| V-000106 | Atlas Displays SL | VDE | 60 | 30 |
| V-000110 | SecureNet AG | VDE | 30 | 14 |
| V-000113 | Cleanspace Facilities BV | VFR | 30 (contract CT-2025-003) | 60 |

### D5 — inactive leftovers (`seed.D5_INACTIVE`)

Status `inactive`, no party link, old names and terms equal to the agreed terms. They carry the supplier's VAT ID and an old bank account (D4 later empties the IBAN of V-000125 and the VAT ID of V-000127).

| Account | Old name | Entity | Belonged to | Created by | Created on | Note |
|---|---|---|---|---|---|---|
| V-000125 | Nordwind Spedition GmbH | VDE | Nordwind Logistics GmbH | finance.de | 2016-05-02 | Old name before the 2019 rebrand |
| V-000126 | Atlas Display Systems SL | VDE | Atlas Displays SL | finance.de | 2018-03-12 | Old company name |
| V-000127 | Shopsys Inc | VUS | Shopsys Software Inc. | finance.us | 2017-10-01 | Replaced by a newer account |
| V-000128 | Lumen Lighting UK Ltd | VDE | Lumen Store Lighting Ltd | finance.de | 2019-07-15 | Old trading name |

### D4 — missing identifiers (`seed.D4_MISSING`)

8 of 28 accounts (29%; the brief says 30%) have an empty VAT ID or IBAN: 6 without VAT ID, 2 without IBAN.

| Account | Display name | Entity | Emptied field | Account created by rule |
|---|---|---|---|---|
| V-000109 | Kaffee & Co OHG | VDE | VAT ID | clean account |
| V-000117 | NORDWIND LOGISTICS | VDE | VAT ID | D1 |
| V-000119 | Bright Agency | VFR | VAT ID | D1 |
| V-000120 | Clean Space Facilities B.V. | VDE | IBAN | D1 |
| V-000122 | Metro Media | VDE | VAT ID | D1 |
| V-000124 | Nordwind Logistics GmbH | VFR | VAT ID | D2 |
| V-000125 | Nordwind Spedition GmbH | VDE | IBAN | D5 |
| V-000127 | Shopsys Inc | VUS | VAT ID | D5 |

### D6 — weak PO discipline (`world.POSpec.in_asis`, `seed.rule_d6_po_discipline`)

The requester gave the supplier a PO number, but for most POs the order was never keyed and approved in the ERP. Only these POs exist in as-is: 4500101, 4500105, 4500114, 4500117, 4500126 and 4500131, that is **6 of 14 (43%; the brief says about 40%)**. Receipts follow their POs: **7 of 11 receipt lines** (6 of 9 receipt documents). The POs of documents 5 (4500123), 6 (4500109), 12 (4500112) and 13 (4500128) do not exist in as-is.

### Resulting numbers

| Measure | To-be | As-is | Case |
|---|---|---|---|
| Vendor accounts | 16 | 28 | 2,800 |
| Suppliers (parties) | 12 | 12 | about 1,200 |
| Accounts per supplier | 1.3 | **2.3** | 2,800 / 1,200 ≈ 2.3 |
| Accounts linked to a party | 16 | 16 (12 unlinked) | — |
| Accounts missing VAT ID or IBAN | 0 | 8 (29%) | — |
| Linked accounts with terms different from the agreed terms | 0 | 5 (D3) | "terms on vendor accounts differ from contracts" |
| Unlinked D1 / D2 accounts with non-agreed terms | — | 8 of 8 | — |
| Inactive accounts | 0 | 4 | — |
| Purchase orders | 14 | 6 (43%) | — |
| Receipt lines | 11 | 7 | — |
| Contract rows | 4 | 4 | — |

`created_by` in as-is: finance.de 13, finance.fr 5, finance.us 3, store.berlin01 3, ap.temp 2, store.paris02 2. Store users and a temporary AP login created 7 of the 28 accounts: anyone could create a vendor. In to-be only finance users appear (finance.de 10, finance.fr 4, finance.us 2).

## 5. Intake and registration

- Two simulated mailboxes (`world.AP_MAILBOX`, `world.STORE_MAILBOX`): **ap@velox.com** (channel `ap_mailbox`, documents 1, 3, 4, 5, 6, 7, 9, 11, 12, 13, 14) and **store.berlin01@velox.com** (channel `store_mailbox`, documents 2, 8, 10). Real email intake is out of scope; `POST /intake/webhook` is the hook for it.
- **To-be**: every document is registered on arrival (`registered_on = received_on`, delay 0): both mailboxes feed one intake step and the gate registers documents itself, including those sent to the store.
- **As-is**: ap@ documents are registered when AP opens and keys them, **+1 business day**; store-mailbox documents after the store forwards them, **+7 business days** (`sim.registration_delay_days`, values from `sim.DURATIONS`). In the app, as-is documents show "Not registered yet" with the expected date from `sim.registration_date`.

| No | Mailbox | Received | Registered as-is | Business days |
|---|---|---|---|---|
| 1 | ap@velox.com | Thu 01 Oct 08:42 | Fri 02 Oct | 1 |
| 2 | store.berlin01@velox.com | Thu 15 Oct 16:05 | Mon 26 Oct | 7 |
| 3 | ap@velox.com | Thu 01 Oct 09:15 | Fri 02 Oct | 1 |
| 4 | ap@velox.com | Fri 02 Oct 11:30 | Mon 05 Oct | 1 |
| 5 | ap@velox.com | Thu 01 Oct 14:02 | Fri 02 Oct | 1 |
| 6 | ap@velox.com | Fri 02 Oct 10:21 | Mon 05 Oct | 1 |
| 7 | ap@velox.com | Thu 01 Oct 07:55 | Fri 02 Oct | 1 |
| 8 | store.berlin01@velox.com | Fri 02 Oct 13:47 | Tue 13 Oct | 7 |
| 9 | ap@velox.com | Thu 01 Oct 10:33 | Fri 02 Oct | 1 |
| 10 | store.berlin01@velox.com | Thu 01 Oct 12:10 | Mon 12 Oct | 7 |
| 11 | ap@velox.com | Fri 02 Oct 09:05 | Mon 05 Oct | 1 |
| 12 | ap@velox.com | Mon 05 Oct 08:30 | Tue 06 Oct | 1 |
| 13 | ap@velox.com | Fri 02 Oct 14:20 | Mon 05 Oct | 1 |
| 14 | ap@velox.com | Thu 01 Oct 16:40 | Fri 02 Oct | 1 |

To-be registers each document at its received time. Average registration lag: as-is (11 × 1 + 3 × 7) / 14 = 2.3 business days; to-be 0.

## 6. Simulated durations

Lookup table `sim.DURATIONS` (business days). The as-is values are calibrated so that the store path matches the case's average cycle of 26 business days.

| Scenario | Activity | Business days | Key in `sim.DURATIONS` | Reasoning |
|---|---|---|---|---|
| As-is | Store mailbox forwarding to AP | 7 | `store_forwarding` | Store staff forward supplier invoices to AP when they get to them, not on a schedule |
| As-is | AP opens ap@ and the quick-fix tool keys the invoice | 1 | `ap_open_and_key` | AP picks up the mailbox the next business day |
| As-is | Email loop (untracked follow-up) | 8–16, mean 12 | `email_loop_min`, `email_loop_max` | About 45% of invoices in the case need email back-and-forth to find a PO, a receipt or an approver, with no owner and no SLA |
| As-is | Email approval | 4 | `email_approval` | Approval by email to the cost-centre owner |
| As-is | Posting | 1 | `posting` | Keyed into the ERP the next business day |
| To-be | Registration | 0 | `registration` | Registered on arrival |
| To-be | Extraction and gate | 0 | `extraction_and_gate` | Minutes, not days |
| To-be | Workflow approval | 1 | `workflow_approval` | Approval task in the workflow, one business day |
| To-be | Posting | 0 | `posting` | Posted by the gate |
| To-be | Exception resolution | the SLA of the exception type | `taxonomy.EXCEPTION_TYPES` | Assumes the SLA is met |

- **As-is derivation**: a non-PO invoice sent to a store takes store forwarding 7 + AP keying 1 + email loop 12 + email approval 4 + posting 1 = **25 business days** (21–29 with the loop's 8–16), close to the case's 26.
- **Email loop**: `sim.email_loop_days(key)` draws a whole number of days uniformly from 8 to 16 with `random.Random("2026:<key>")`, seed `sim.EMAIL_LOOP_SEED = 2026`. The same document always gets the same length; over 500 keys the mean is about 12 (checked in `tests/test_sim.py`).
- **To-be**: touchless documents take 0 days; exceptions take their SLA (1–2 days in the sample, assumed met), plus 1 day of workflow approval for a non-PO invoice above the delegation-of-authority limit.
- **Cycle per document** (`sim.cycle_breakdown`, `sim.cycle_days`, stored in `gate_decision.simulated_days`): as-is "matched" = (store forwarding 7 if the store mailbox) + AP keying 1 + posting 1; as-is "email loop" = the same + the seeded loop + email approval 4; to-be "touchless" = 0; to-be "exception" = SLA (+ approval 1 where needed).
- **Exception cockpit snapshot** (`sim.cockpit_as_of`): the queue is shown as it stands at the close (17:00) of the business day on which its last item was registered (to-be: Mon 5 Oct 2026, when document 12 arrives). Ages are counted to that moment, so B-05 and B-08 are due that day and B-06 and B-12 are still within their SLA; the simulation itself assumes every SLA is met. The as-is email-loop bucket lists all nine documents with their days in the loop and posting date.
- **What the comparison shows** (decision of 24 Sep 2026): the durations are **not** tuned to make the sample average equal the case's 26 days. The KPIs show both, labelled: the **sample average** (as-is about 13 business days, because the 14 documents include several clean PO invoices that the quick-fix tool posted in 2 days; to-be 0.5) and the **reference path "non-PO invoice sent to a store"** (as-is 7 + 1 + 12 + 4 + 1 = 25, the case's 26; to-be 2 + 1 = 3, or 0 under the DoA limit). A finance audience can see why the sample differs from the population.

## 7. Extraction

- **Model**: a Gemini model through the Google GenAI SDK (`google-genai`). `GEMINI_MODEL` defaults to `gemini-2.5-flash` (`config.GEMINI_MODEL`). If the API rejects the configured model as unavailable (Google now limits 2.5 Flash to existing users), extraction falls back automatically to the newest GA (non-preview) Flash model the key can use, found with the SDK's model list and never hard-coded. The model actually used is logged and stored with each extraction.
- **Call**: one call per PDF (document part plus system instruction), structured output with the JSON schema `extract.InvoiceExtraction`: 19 fields, each with a value (nullable) and a confidence from 0 to 1. Temperature 0 for Gemini 1.x/2.x models; for Gemini 3+ the API default is kept, because Google recommends not lowering it (`extract._temperature_for`). Results are cached, so re-runs are identical. Latency and token usage (output including thinking tokens) are logged per call.
- **Cache**: `data/cache/<SHA-256 of the PDF>.json`. Re-runs read the cache and never call the API unless forced (`make extract FORCE=1`, `--force-extract`, or "Force re-extract" on an invoice page). Seeding never calls the API; "Load sample documents" calls it only for PDFs not yet in the cache, and only when `GEMINI_API_KEY` is set. After the first extraction the demo works offline.
- **Critical fields** (`extract.CRITICAL_FIELDS`): supplier identity (any one of supplier VAT ID, IBAN or name), invoice number, gross total, bill-to name.
- **Confidence threshold 0.80** (`config.CONFIDENCE_THRESHOLD`): in to-be, a critical field below 0.80 sends the document to human review (AP specialist, SLA 1 day). As-is has no threshold. The gate applies it in phase 2; phase 1 shows it next to the confidence bars.
- **EXTRACTOR=fixture**: reads ground-truth JSON from `tests/fixtures/` (built from `world.DOCUMENTS` by `tests/make_fixtures.py`, confidence 0.99 for printed fields and 0 for absent ones). It never calls the API, is labelled "fixture (ground truth, no API call)" in the UI and is never presented as Gemini output. The tests always run in this mode.

## 8. Control-gate parameters (`app/gate.py`)

| Parameter | Value | Defined in | Reasoning |
|---|---|---|---|
| PO amount tolerance | 2% or 50 EUR, whichever is larger, on net amounts | brief section 8 | Absorbs rounding and small freight; document 6 (+300.00 on 6,300.00, 4.8%) is outside |
| Goods and services | Goods lines need a receipt for the invoiced quantity; service lines need a service confirmation | brief section 8 | Three-way match |
| Vendor resolution (to-be) | Party level: VAT ID exact, then IBAN exact, then name similarity | brief section 8 | Identifiers first, names last |
| Vendor resolution (as-is, naive) | First active account (lowest account ID) whose display name equals the printed supplier name (case-insensitive, trimmed, not normalised); else the first active account with name similarity ≥ 90; else a new account is created. VAT ID and IBAN are not used | brief section 8 ("pick the first name hit"); checked in `tests/test_seed.py` | Models the quick-fix tool's name lookup. It lands document 2 on V-000117, document 4 on V-000119 and document 11 on V-000104 (VDE) |
| Name similarity | rapidfuzz `token_set_ratio` ≥ 90 on normalised names | `normalize.NAME_SIMILARITY_THRESHOLD` | All D1 spelling duplicates score 91 or more against their supplier; names of different suppliers in the seed score 44 or less |
| Name normalisation | Lowercase, punctuation removed, legal suffixes stripped: AG, BV, GmbH, Inc, Limited, LLC, Ltd, OHG, SA, SARL, SAS, SL | `normalize.LEGAL_SUFFIXES` | The brief's list plus SAS, SA, LLC and Limited, which appear in the seed or are common |
| Duplicate invoice | Same party + normalised invoice number + gross total within 1%. The party of an earlier document counts even if that document stopped at human review (`lookup_party_id`) | brief section 8; `normalize.normalise_invoice_number` | Normalisation removes spaces, punctuation, leading zeros, a leading label ("Invoice no.", "Rechnung Nr.", "Facture n°") and words such as COPY, REMINDER, DUPLICATE, KOPIE, DUPLICATA, COPIE, COPIA |
| Recurring contract match | No PO, recurring contract in the bill-to entity, net amount inside the monthly range, period not yet invoiced | brief section 8; `world.CONTRACTS` | Contract spend does not need a PO per month |
| Delegation of authority | Non-PO invoice under 500 (gross, invoice currency) from a known vendor is auto-approved; the requester is informed | brief section 8; `gate.SCENARIOS` (`doa_auto_approve_limit`) | Low-value spend; document 10 is 180.00 gross (151.26 net). Gross, because it is the amount that will be paid |
| Non-PO requester | Kaffee & Co (VDE) -> Paul Neumann, manager of store Berlin 01 and owner of its cost centre DE-STR-B01 | `world.NON_PO_REQUESTERS` | The brief gives no requester for non-PO spend; the store that orders the coffee owns it |
| Bill-to entity | VAT ID of the bill-to block, else the bill-to name compared with the legal suffix kept (GmbH / SAS / Inc.) | `app/gate.py` | Stripping the suffix would make the three Velox entities identical |
| Processing order | By registration time, then document ID; duplicates and credit notes are checked against documents processed earlier in the same run | `gate.run_scenario` | Mirrors arrival order: document 1 is registered before its resend (document 2), document 3 before its credit note (document 4) |
| Payment terms | To-be takes terms from the master; different terms on the invoice raise the info flag `terms_variance` | brief section 8 | Documents 1, 2 and 6 print terms different from the agreed terms |
| Confidence threshold | 0.80 on critical fields | `config.CONFIDENCE_THRESHOLD` | See section 7 |

### Robustness to real extractions

The fixtures are perfect ground truth; a real Gemini extraction can differ. These rules keep a plausible reading error from flipping an outcome silently (`app/gate.py`, tested in `tests/test_gate.py`):

| Situation | Rule |
|---|---|
| PO number printed as "PO 4500117", "P.O. #4500117" or with several numbers | `normalize.normalise_po_number` strips labels and separators; every quoted PO is tried and the first that exists in the ERP is used |
| Invoice lines in another order, split or repeated | Lines are mapped to PO lines by description first (position only as a tie-break); quantities and amounts are summed per PO line before the tolerance, quantity and receipt checks |
| Lines that do not add up to the net total (e.g. a line not read) | To-be: human review ("the invoice lines do not add up to the net total"); as-is: email loop. Never posted touchless |
| No lines read | Header-level check: net total against the PO total within tolerance, and every PO line received or confirmed; otherwise human review with a readable reason |
| Net total missing | Derived as gross − tax, else the sum of the lines; if still unknown, a contract match is not attempted (human review) |
| Currency written as "eur", "€", "$" or "£" | Normalised to the ISO code before comparing with the PO and posting |
| Dates printed day-first (30.09.2026, 30/09/2026) | Accepted; an unreadable invoice date never matches a contract period (human review) |
| Bill-to name with the store appended ("Velox Retail GmbH Store Berlin 01") | Mapped by the longest whole-word prefix that names exactly one Velox entity; an unknown bill-to is never counted as a wrong-entity posting |
| An invoice with a negative total | To-be: human review ("negative total, probable credit note"); the DoA auto-approval needs 0 < gross < 500 |
| Values read back from the database or from memory | Every number stored in a decision is a float, so a re-run gives byte-identical results |

Exception types, owners and SLAs (brief section 9; single source of truth `app/taxonomy.py`). Routing of the sample documents: document 5 -> requester Jonas Weber (service not confirmed); document 6 -> buyer Sofia Brandt (price); document 12 -> receiver Tim Koch first (confirm whether the 20 missing panels arrived), then buyer Sofia Brandt (credit from the supplier) — both readings of the brief (decision of 24 Sep 2026); document 8 -> AP specialist Marco Ruiz; document 2 -> blocked, the supplier gets an automatic status reply and Marco Ruiz is informed. When a printed PO number does not exist in the ERP (`po_not_found`), no requester can be derived, so the AP specialist gets the correct PO from the supplier; a PO of another supplier goes to its requester.

| Type | Label | Owner | SLA (business days) |
|---|---|---|---|
| `no_po` | Invoice without purchase order | Requester, then cost-centre owner approval | 2 |
| `po_no_receipt` | PO exists, no receipt or service confirmation | Receiver / requester | 2 |
| `price_qty_mismatch` | Price or quantity outside tolerance | Buyer | 2 |
| `po_not_found` | PO number not found or wrong vendor | Requester | 2 |
| `duplicate_vendor_account` | Supplier has more than one account (info) | Master Data owner (Lena Fischer) | 5 |
| `unknown_vendor` | Supplier not in master | Master Data owner (Lena Fischer) | 2 |
| `wrong_legal_entity` | Billed to the wrong Velox entity | AP specialist (Marco Ruiz) | 1 |
| `duplicate_invoice` | Same invoice already registered or posted | AP specialist (Marco Ruiz) | 0 (blocked) |
| `credit_note_without_invoice` | Credit note references no known invoice | AP specialist (Marco Ruiz) | 2 |
| `human_review` | Low extraction confidence | AP specialist (Marco Ruiz) | 1 |
| `terms_variance` (info, not blocking) | Invoice payment terms differ from the master | AP specialist (Marco Ruiz) | — |
| `email_loop` (as-is only) | Untracked manual follow-up | — | — |

## 9. KPIs (`app/metrics.py`)

The same definitions as the case deck (brief section 12). Each KPI shows its formula on hover and in a footnote.

| KPI | Formula | Group |
|---|---|---|
| PO / contract coverage | Invoices (credit notes excluded) whose printed PO exists in the ERP, or — to-be only, because as-is has no contract logic — whose supplier has a recurring contract with the billed entity ÷ invoices | Upstream |
| Accounts per supplier | Vendor accounts (incl. inactive) ÷ parties | Upstream |
| Accounts with VAT ID and IBAN | Accounts with both identifiers ÷ accounts | Upstream |
| Accounts with agreed terms | Accounts whose terms equal the recurring contract's (else the supplier's agreed terms) ÷ accounts | Upstream |
| Registration lag | Mean business days from receipt to registration | Upstream |
| Touchless rate | Documents fully handled with no human step (posted without exception, blocked duplicate with an automatic status reply, credit note applied) ÷ documents. As-is "touchless" means no exception follow-up after intake; the intake itself is delayed (section 5) | Downstream |
| Exception rate | Documents with an exception or human review (as-is: the untracked email loop) ÷ documents; also by type | Downstream |
| Duplicates blocked / duplicate postings | Blocked duplicates; postings of an invoice posted more than once (as-is: documents 1 and 2 are **2 postings of 1 invoice**) | Downstream |
| Credit notes applied / unapplied | From the credit-note application of each credit note | Downstream |
| Wrong-entity postings | Postings to a legal entity other than the one billed | Downstream |
| Average cycle | Mean simulated business days per document; also the mean over documents with a human step, and the reference path "non-PO invoice sent to a store" (section 6) | Downstream |
| Simulated cash leakage | Amount of the repeated postings of duplicated invoices + unapplied credit notes; plus the **count** of postings on invoice terms that differ from the agreed terms (a count only, as the brief asks) | Downstream |

Expected values for the 14 documents (checked against `tests/golden.yaml`): to-be touchless 10 of 14 (71.4%), 4 exceptions, 1 duplicate blocked, 1 credit note applied, cash leakage 0.00; as-is touchless 5 of 14 (35.7%), 9 documents in the email loop, 2 postings of 1 duplicated invoice, 1 unapplied credit note, 2 wrong-entity postings (documents 8 and 11), 3 postings on non-agreed terms (documents 1, 2 and 6; 1 and 2 are the same invoice), cash leakage 29,646.00 EUR (27,846.00 + 1,800.00).

## 10. The 14 sample documents

Defined in `world.DOCUMENTS`, rendered as PDFs by `app/invoices_gen.py`. "ap@" = ap@velox.com, "store" = store.berlin01@velox.com. "Designed to show" is `DocumentSpec.designed_to_show`.

| No | Supplier (printed name) | Channel | Bill-to | Net | Gross | PO / contract | Designed to show |
|---|---|---|---|---|---|---|---|
| 1 | Nordwind Logistics GmbH | ap@ | VDE | 23,400.00 | 27,846.00 EUR | Contract CT-2025-001 | Recurring contract match (to-be) vs 'no PO' email loop (as-is). Invoice terms 14 days differ from the contract (30). |
| 2 | NORDWIND LOGISTICS | store | VDE | 23,400.00 | 27,846.00 EUR | Contract CT-2025-001 | Duplicate blocked (to-be) vs posted twice on another vendor account (as-is). Sent from the receivables system with the short name 'NORDWIND LOGISTICS'. |
| 3 | Bright Agency SARL | ap@ | VFR | 12,000.00 | 14,400.00 EUR | PO 4500117 | Clean PO + service confirmation -> touchless. |
| 4 | Bright Agency | ap@ | VFR | -1,500.00 | -1,800.00 EUR | Credit for INV-2026-0457 | Credit applied to invoice #3 (to-be) vs unapplied on a different vendor account (as-is). Issued from a different template with the short name 'Bright Agency'. |
| 5 | FitOut Partners Ltd | ap@ | VDE | 48,000.00 | 48,000.00 EUR | PO 4500123 | PO exists but no service confirmation -> exception to the receiver with SLA (to-be) vs email loop (as-is). |
| 6 | Atlas Displays SL | ap@ | VDE | 6,600.00 | 6,600.00 EUR | PO 4500109 | Unit price 44.00 vs PO 42.00: price mismatch outside tolerance -> exception to the buyer. Invoice terms 30 days differ from the master (60). |
| 7 | Shopsys Software Inc. | ap@ | VUS | 9,600.00 | 9,600.00 USD | PO 4500131 | Touchless (PO + service confirmed). |
| 8 | Metro Media GmbH | store | VFR | 7,500.00 | 7,500.00 EUR | PO 4500126 | Billed to Velox Retail SAS (VFR) but PO 4500126 belongs to VDE: wrong legal entity -> exception (to-be) vs silent wrong posting (as-is). |
| 9 | QuickPrint SAS | ap@ | VFR | 2,400.00 | 2,880.00 EUR | PO 4500114 | Touchless goods 3-way match (PO, receipt, invoice). |
| 10 | Kaffee & Co OHG | store | VDE | 151.26 | 180.00 EUR | none | Low-value non-PO invoice sent to the store: to-be auto-approves under the DoA threshold; as-is waits 7+ days in the store mailbox. |
| 11 | Cleanspace Facilities BV | ap@ | VFR | 3,200.00 | 3,200.00 EUR | Contract CT-2025-003 | No PO, recurring contract match (amount inside the expected monthly range). |
| 12 | Lumen Store Lighting Ltd | ap@ | VDE | 5,200.00 | 5,200.00 EUR | PO 4500112 | 120 units invoiced, only 100 received (line 2: 40 invoiced, 20 received): quantity mismatch -> exception to the receiver, then the buyer; partial. |
| 13 | SecureNet AG | ap@ | VDE | 4,800.00 | 4,800.00 EUR | PO 4500128 | Clean PO + service confirmation -> touchless (to-be); the PO was never keyed in the as-is ERP -> email loop. |
| 14 | Harbor Freight Forwarders Inc. | ap@ | VUS | 17,250.00 | 17,250.00 USD | Contract CT-2025-004 | No PO, recurring contract match inside the monthly range -> touchless (to-be) vs email loop (as-is). |

The brief quotes net amounts, except document 10, where 180 EUR is the gross amount.

### Deliberate traps

- **Document 2** is the same invoice as document 1 (NWL-2026-00913, same amounts), resent two weeks later to the store mailbox as "PAYMENT REMINDER — COPY OF INVOICE" with a COPY watermark. It is printed as **"NORDWIND LOGISTICS"**, the short name used by Nordwind's receivables system, which is exactly the display name of the D1 duplicate account V-000117.
- **Document 4** (credit note CN-2026-0031) is printed as **"Bright Agency"** from a different template, which is exactly the display name of the D1 duplicate account V-000119 in VFR.
- **Payment terms**: documents 1 and 2 print 14 days (contract: 30); document 6 prints 30 days (master: 60). All other invoices print the agreed terms; the credit note prints none.
- **Document 8** is billed to VFR, where Metro Media has no vendor account, while its PO 4500126 belongs to VDE. It is the only document billed to an entity its supplier does not serve.
- **Document 11** is billed to VFR. Cleanspace has one account in each entity with the same display name, and the naive as-is lookup takes the first one, V-000104 in VDE: a second wrong-entity posting in as-is.
- **Document 10** is billed to Velox Retail GmbH "Attn: Store Berlin 01" at the store's address, without the Velox VAT ID in the bill-to block.
- **Document 6** invoices 150 × 44.00 against a PO at 150 × 42.00; **document 12** invoices 40 panels of which only 20 were received; **document 5** has no service confirmation.

### VAT treatment per document

Illustrative, chosen to vary the tax blocks the extractor has to read; not tax advice.

| No | Invoice number | Invoice date | Tax | Tax amount | Legal mention printed |
|---|---|---|---|---|---|
| 1 | NWL-2026-00913 | 30 Sep | VAT 19% | 4,446.00 | Domestic German supply |
| 2 | NWL-2026-00913 | 30 Sep | VAT 19% | 4,446.00 | Same as document 1 |
| 3 | INV-2026-0457 | 29 Sep | VAT 20% | 2,400.00 | Domestic French supply |
| 4 | CN-2026-0031 | 02 Oct | VAT 20% | -300.00 | Credit note on document 3 |
| 5 | 2026-091 | 30 Sep | VAT 0% | 0.00 | Reverse charge (Article 196, Directive 2006/112/EC) |
| 6 | AD-2026/0788 | 28 Sep | VAT 0% | 0.00 | Intra-Community supply, exempt (Article 138, Directive 2006/112/EC) |
| 7 | SS-100482 | 01 Oct | Sales tax | 0.00 | Sales tax not applicable to this service |
| 8 | MM-2026-212 | 30 Sep | VAT 0% | 0.00 | Reverse charge (Article 196, Directive 2006/112/EC) |
| 9 | QP-26-1043 | 29 Sep | VAT 20% | 480.00 | Domestic French supply |
| 10 | 2026/117 | 30 Sep | VAT 19% | 28.74 | Domestic German supply |
| 11 | CSF-26-10355 | 30 Sep | VAT 0% | 0.00 | Reverse charge (Article 196, Directive 2006/112/EC) |
| 12 | LSL-INV-5521 | 30 Sep | VAT 0% | 0.00 | Export of goods outside the UK, zero-rated |
| 13 | SN-2026-3307 | 01 Oct | VAT 0% | 0.00 | Reverse charge, services supplied from Switzerland |
| 14 | HFF-2026-0930 | 30 Sep | Sales tax | 0.00 | Freight and customs services, no sales tax |

## 11. Decisions taken on 24 Sep 2026 (before phase 2)

1. **Two clean documents added** (13 SecureNet, 14 Harbor Freight). With only the brief's 12 documents, 4 are designed as to-be exceptions, so the to-be touchless rate could not exceed 8 of 12 (67%), below the brief's target of 70% or more. Real AP volume is mostly clean PO and contract invoices; the two additions use suppliers, a PO and a contract that were already in the seed. Result: 10 of 14 (71.4%).
2. **Cycle time shown two ways** (section 6): sample average and the reference path, instead of tuning durations.
3. **Duplicate KPI**: "2 postings of 1 invoice" (documents 1 and 2 in as-is).
4. **Document 12** routed to the receiver first, then the buyer (both readings of the brief).
5. **Document 10** requester: Paul Neumann, store manager Berlin 01.
