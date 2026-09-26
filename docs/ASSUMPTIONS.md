# Assumptions

Every simulated number in the demo, with one line of reasoning and the place in the code where it is defined. If a number changes in the code, this page must change with it (the tests in `tests/test_seed.py`, `tests/test_sim.py`, `tests/test_metrics.py` and `tests/test_normalize.py` check the main counts quoted here).

## 1. Purpose and honesty statement

- The app simulates the accounts payable (procure-to-pay) invoice process of a fictional retailer, **Velox Retail**, for the **12 case documents** of the case deck, under two scenarios: **A (as-is)** and **B (to-be)**.
- All companies, people, tax IDs, bank accounts, invoice numbers and email addresses are fictional. IBANs are built with valid ISO 13616 check digits (`world.make_iban`) so they look and validate like real ones, but they belong to no one.
- The "ERP" is a handful of read-only mock tables named after standard accounts-payable concepts (legal entity, supplier, vendor record, contract, catalogue, purchase order, receipt, posted invoice). It is not an ERP: no ledger, payments, users or roles.
- **Gemini reads and classifies; rules decide.** A Gemini model reads each document (fields, a confidence per field and the document type). Every decision is a deterministic rule.
- Durations are simulated with a lookup table (section 6). The only random element is the length of the as-is email loop, and it is seeded.
- **Group currency CHF.** Velox is a Swiss group: group policy limits (approval limit, catalogue limit) are in CHF. Invoices keep the currency printed on them (EUR or USD). For the limit checks only, amounts are converted at fixed **simulated** rates (section 8).
- Single source of truth: `app/world.py` (clean world and the 12 documents); `app/seed.py` derives the dirty world by explicit rules.

## 2. Simulated calendar

- Business days are Monday to Friday, with no public holidays (`sim.add_business_days`, `sim.business_days_between`). Three countries' holiday calendars would add complexity without changing the message.
- Invoice dates run from 28 Sep to 2 Oct 2026; the documents arrive between **Mon 28 Sep and Fri 2 Oct 2026** (section 5).
- Purchase orders are dated 15 Jun – 21 Sep 2026 and receipts 14 Aug – 1 Oct 2026, so commitments exist before the invoices arrive. These are the to-be dates; in as-is, rule D6 removes the POs that were never keyed in the ERP.
- **The demo's next email**: *Reset demo* loads the 12 documents in both scenarios and runs them, but holds back case document 5 (FitOut Partners, milestone invoice) in to-be. *Receive next email* registers it at its simulated arrival, Fri 2 Oct 2026 14:02, on ap@velox.com, without running the rules; *Run the control gate* on its page then reads it (from the cache) and decides. Until it is received, scenario B shows 11 documents (section 9).

## 3. Seed — clean world (scenario B, to-be)

Defined in `app/world.py`, loaded into scenario `tobe` by `seed.seed_scenario`.

| Item | Count | Defined in | Reasoning |
|---|---|---|---|
| Legal entities | 3 | `world.LEGAL_ENTITIES` | VDE Velox Retail GmbH (Berlin, EUR), VFR Velox Retail SAS (Paris, EUR), VUS Velox Retail Inc. (New York, USD) |
| Suppliers | 12 | `world.PARTIES` | One per real supplier (unique by tax ID); mirrors the supplier types in the case |
| Vendor records | 14 | `world.CLEAN_ACCOUNTS` | One record per supplier per legal entity it actually serves: 12 suppliers, 2 of them in two entities. 14 ÷ 12 = **1.17** records per supplier, the deck's target of 1.2 or less |
| Purchase orders | 12 | `world.PURCHASE_ORDERS` | 6 invoiced by the case documents plus 6 background POs |
| Receipt lines / receipt documents | 11 / 9 | `world.RECEIPTS` | One receipt document may cover several PO lines |
| Contract rows | 4 (3 supplier contracts) | `world.CONTRACTS` | Contracts are per legal entity; Cleanspace serves VDE and VFR, so it has two rows |
| Card / catalogue | 1 | `world.CATALOGUES` | Small store purchases (deck slide 9): Kaffee & Co for store Berlin 01 |
| Approval matrix | 3 rows | `world.APPROVAL_MATRIX` | One approval limit and next approver per legal entity (simulated) |

### Suppliers and vendor records

"Records as-is" counts every record that really belongs to the supplier, linked to it or not (rules D1, D2, D5 below).

| No | Supplier | Country | Type | Agreed terms (days) | Entities served | Records to-be | Records as-is |
|---|---|---|---|---|---|---|---|
| 1 | Nordwind Logistics GmbH | DE | 3PL carrier, recurring contract | 30 | VDE | 1 | 5 |
| 2 | Bright Agency SARL | FR | Marketing agency, services with PO | 45 | VFR | 1 | 2 |
| 3 | FitOut Partners Ltd | GB | Store fit-out, milestone invoices with PO | 30 | VDE | 1 | 1 |
| 4 | Cleanspace Facilities BV | NL | Facilities, recurring contract | 30 | VDE, VFR | 2 | 4 |
| 5 | Shopsys Software Inc. | US | IT SaaS, subscription with PO | 30 | VUS | 1 | 3 |
| 6 | Atlas Displays SL | ES | Store fixtures (goods) with PO and receipt | 60 | VDE, VFR | 2 | 3 |
| 7 | Metro Media GmbH | DE | Regional marketing, services with PO | 30 | VDE | 1 | 3 |
| 8 | QuickPrint SAS | FR | Marketing collateral (goods) with PO | 30 | VFR | 1 | 2 |
| 9 | Kaffee & Co OHG | DE | Small local supplier to a Berlin store, card / catalogue | 14 | VDE | 1 | 1 |
| 10 | SecureNet AG | CH | IT vendor, service with PO | 30 | VDE | 1 | 1 |
| 11 | Harbor Freight Forwarders Inc. | US | 3PL, recurring contract | 30 | VUS | 1 | 1 |
| 12 | Lumen Store Lighting Ltd | GB | Store lighting (goods) with PO | 30 | VDE | 1 | 2 |
| | **Total** | | | | | **14** | **28** |

Metro Media and Lumen stay in the master although their invoices are not among the 12 case documents (section 11).

### Purchase orders and receipts

Requester / buyer are people from `world.PEOPLE`. "As-is" = the PO exists in scenario A (rule D6). "Doc" = the case document that invoices it.

| PO | Entity | Supplier (record) | Type | Lines | Total | Requester / buyer | Receipt or service confirmation | As-is | Doc |
|---|---|---|---|---|---|---|---|---|---|
| 4500101 | VDE | FitOut Partners Ltd (V-000103) | service | 1 × 40,000.00 (milestone 1) | 40,000.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0412, confirmed | yes | — |
| 4500105 | VFR | Atlas Displays SL (V-000115) | goods | 80 × 42.00 | 3,360.00 EUR | Luc Bernard / Julien Moreau | PR-26-0433, 80 of 80 | yes | — |
| 4500109 | VDE | Atlas Displays SL (V-000106) | goods | 150 × 42.00 | 6,300.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0451, 150 of 150 | no | 6 |
| 4500112 | VDE | Lumen Store Lighting Ltd (V-000112) | goods | 80 × 40.00 + 40 × 50.00 | 5,200.00 EUR | Jonas Weber / Sofia Brandt | PR-26-0456, 80 + 20 = 100 of 120 | no | — |
| 4500114 | VFR | QuickPrint SAS (V-000108) | goods | 400 × 3.50 + 10,000 × 0.10 | 2,400.00 EUR | Camille Martin / Julien Moreau | PR-26-0447, all received | yes | 8 |
| 4500117 | VFR | Bright Agency SARL (V-000102) | service | 1 × 12,000.00 | 12,000.00 EUR | Camille Martin / Julien Moreau | PR-26-0449, confirmed | yes | 3 |
| 4500121 | VFR | Bright Agency SARL (V-000102) | service | 1 × 6,000.00 | 6,000.00 EUR | Camille Martin / Julien Moreau | none (open) | no | — |
| 4500123 | VDE | FitOut Partners Ltd (V-000103) | service | 1 × 48,000.00 (milestone 2) | 48,000.00 EUR | Jonas Weber / Sofia Brandt | **none** (deliberate) | no | 5 |
| 4500126 | VDE | Metro Media GmbH (V-000107) | service | 1 × 7,500.00 | 7,500.00 EUR | Anna Schulz / Sofia Brandt | PR-26-0458, confirmed | yes | — |
| 4500128 | VDE | SecureNet AG (V-000110) | service | 1 × 4,800.00 | 4,800.00 EUR | Felix Braun / Sofia Brandt | PR-26-0459, confirmed | no | 11 |
| 4500130 | VDE | SecureNet AG (V-000110) | service | 1 × 3,500.00 | 3,500.00 EUR | Felix Braun / Sofia Brandt | none (open) | no | — |
| 4500131 | VUS | Shopsys Software Inc. (V-000105) | service | 1 × 9,600.00 | 9,600.00 USD | Emily Carter / Daniel Price | PR-26-0461, confirmed | yes | 7 |

- 9 POs have a receipt document (11 receipt lines, because 4500112 and 4500114 have two lines each). 3 have none: 4500121 and 4500130 are simply not delivered yet; **4500123 (FitOut milestone 2) deliberately has no service confirmation**.

### Contracts, catalogue and approval matrix

| Commitment | Supplier | Entity | Expected monthly range (net) / limit | Terms (days) | Owner |
|---|---|---|---|---|---|
| Contract CT-2025-001 | Nordwind Logistics GmbH | VDE | 20,000–26,000 EUR | 30 | Nina Hoffmann |
| Contract CT-2025-002 | Cleanspace Facilities BV | VDE | 3,000–3,400 EUR | 30 | Katrin Lange |
| Contract CT-2025-003 | Cleanspace Facilities BV | VFR | 3,000–3,400 EUR | 30 | Claire Dubois |
| Contract CT-2025-004 | Harbor Freight Forwarders Inc. | VUS | 15,000–19,000 USD | 30 | Ryan Brooks |
| Card / catalogue CAT-2026-001 | Kaffee & Co OHG | VDE (store Berlin 01) | CHF 500 per invoice (gross) | 14 | Paul Neumann |

| Legal entity | Approval limit (gross) | Next approver (Human review above the limit) |
|---|---|---|
| VDE | CHF 25,000 | Stefan Keller, Finance director DE |
| VFR | CHF 25,000 | Helene Girard, Finance director FR |
| VUS | CHF 25,000 | Michael Grant, Finance director US |

The approval matrix is **simulated**: one limit per entity, one next approver. A real delegation matrix has several levels per cost centre.

### Small modelling choices

- **Agreed terms on the supplier** (`party.agreed_terms_days`): only 3 suppliers have a recurring contract, so the supplier carries the terms agreed with it (contract or supplier agreement). To-be records copy them; terms drift (D3) is measured against them.
- **Buyer on the PO** (`buyer_name`, `buyer_email`): price and quantity mismatches go to the buyer. Buyers: Sofia Brandt (DE), Julien Moreau (FR), Daniel Price (US).
- **Owners** are named people: master data owner Lena Fischer, AP specialist Marco Ruiz, the requester, buyer and receiver of each PO, the owner of each contract or catalogue, and the next approvers. Jonas Weber is the store development manager DE.
- **Receipt ID shared by lines**: one receipt document covers several PO lines (PR-26-0456 covers both Lumen lines, PR-26-0447 both QuickPrint lines). Service confirmations are receipt rows with quantity 1 and kind `service_confirmation`.
- **US bank details**: US suppliers have no IBAN, so the bank column stores "ABA routing + account" (for example `ABA 121000248 ACCT 4839201756`). US suppliers and the VUS entity store their EIN as the tax ID; SecureNet stores its Swiss UID (CHE-419.287.563).
- **Net vs gross**: PO tolerance and contract ranges compare **net** amounts, because VAT is not part of the commitment and several invoices are reverse charge (0 VAT). The duplicate check, the approval limit and the catalogue limit use **gross**, the amount that would be paid. Example: document 1 is 23,400.00 net, inside the Nordwind range of 20,000–26,000; its gross, 27,846.00 EUR, is above the approval limit.
- **Currencies**: SecureNet invoices 4,800 in EUR; FitOut (UK) invoices in EUR; only VUS business is in USD (Shopsys, Harbor Freight). Invoice amounts are stored and shown in their own currency and never converted, except for the CHF limit checks (section 8).
- **Clean records** are created by `finance.de`, `finance.fr` or `finance.us` (one per entity) and carry the note "Validated through the vendor request workflow; linked to its supplier."

## 4. Dirty world (scenario A, as-is) — rules D1–D6

`seed.derive_dirty_accounts` applies the rules to the clean records in the order **D1, D2, D3, D5, D4**. D4 runs last so it also covers records created by D1, D2 and D5. D6 applies to purchase orders (`seed.rule_d6_po_discipline`). Every changed or created record lists its rules in `vendor_account.corruption_rules`.

### D1 — spelling duplicates (`seed.D1_DUPLICATES`)

Suppliers 1, 2, 4, 5, 7 and 8 get 1–2 extra records in the same legal entity, with a different spelling, a different IBAN, different terms and no link to the supplier. They copy the supplier's tax ID (D4 later empties some identifiers). None of the spellings equals a name printed on a case document except the two traps of section 10 (V-000117, V-000119).

| New record | Copy of | Entity | Display name | Terms (agreed) | Created by | Created on | Note |
|---|---|---|---|---|---|---|---|
| V-000117 | V-000101 Nordwind Logistics GmbH | VDE | NORDWIND LOGISTICS | 14 (30) | ap.temp | 2023-11-06 | Created to pay an urgent reminder |
| V-000118 | V-000101 Nordwind Logistics GmbH | VDE | Nordwind Logistik GmbH | 60 (30) | store.berlin01 | 2024-08-19 | Created by the Berlin store for a delivery invoice |
| V-000119 | V-000102 Bright Agency SARL | VFR | Bright Agency | 60 (45) | finance.fr | 2024-02-27 | Created from a credit note email |
| V-000120 | V-000104 Cleanspace Facilities BV | VDE | Clean Space Facilities B.V. | 14 (30) | store.berlin01 | 2022-05-09 | Created by a store for monthly cleaning |
| V-000121 | V-000113 Cleanspace Facilities BV | VFR | CLEANSPACE FACILITIES | 45 (30) | store.paris02 | 2025-10-14 | Created by a Paris store |
| V-000122 | V-000107 Metro Media GmbH | VDE | Metro Media | 45 (30) | ap.temp | 2023-03-01 | Temporary account for a campaign invoice |
| V-000123 | V-000107 Metro Media GmbH | VDE | Metro-Media GmbH | 60 (30) | store.berlin01 | 2024-11-25 | Created by the Berlin store |
| V-000129 | V-000105 Shopsys Software Inc. | VUS | SHOPSYS SOFTWARE | 45 (30) | finance.us | 2024-06-03 | Created for a renewal invoice |
| V-000130 | V-000108 QuickPrint SAS | VFR | QuickPrint S.A.S. | 60 (30) | store.paris02 | 2025-03-17 | Created by a Paris store for leaflets |

### D2 — cross-entity spread (`seed.D2_CROSS_ENTITY`)

| New record | Copy of | Entity | Display name | Terms (agreed) | Created by | Created on | Note |
|---|---|---|---|---|---|---|---|
| V-000124 | V-000101 Nordwind Logistics GmbH | VFR | Nordwind Logistics GmbH | 45 (30) | store.paris02 | 2025-02-11 | Created by a Paris store to pay a delivery |

Nordwind serves VDE only; a store user opened a record in VFR with the same name and IBAN and different terms.

### D3 — terms drift (`seed.D3_TERMS_DRIFT`)

A **fixed list** spread across entities, so the result is reproducible.

| Record | Supplier | Entity | Agreed terms | Record terms |
|---|---|---|---|---|
| V-000103 | FitOut Partners Ltd | VDE | 30 | 60 |
| V-000105 | Shopsys Software Inc. | VUS | 30 | 45 |
| V-000106 | Atlas Displays SL | VDE | 60 | 30 |
| V-000110 | SecureNet AG | VDE | 30 | 14 |
| V-000113 | Cleanspace Facilities BV | VFR | 30 (contract CT-2025-003) | 60 |

### D5 — inactive leftovers (`seed.D5_INACTIVE`)

Status `inactive`, no link to the supplier, old names and terms equal to the agreed terms. They carry the supplier's tax ID and an old bank account (D4 later empties the IBAN of V-000125 and the tax ID of V-000127).

| Record | Old name | Entity | Belonged to | Created by | Created on | Note |
|---|---|---|---|---|---|---|
| V-000125 | Nordwind Spedition GmbH | VDE | Nordwind Logistics GmbH | finance.de | 2016-05-02 | Old name before the 2019 rebrand |
| V-000126 | Atlas Display Systems SL | VDE | Atlas Displays SL | finance.de | 2018-03-12 | Old company name |
| V-000127 | Shopsys Inc | VUS | Shopsys Software Inc. | finance.us | 2017-10-01 | Replaced by a newer account |
| V-000128 | Lumen Lighting UK Ltd | VDE | Lumen Store Lighting Ltd | finance.de | 2019-07-15 | Old trading name |

### D4 — missing identifiers (`seed.D4_MISSING`)

8 of 28 records (29%; the brief says 30%) have an empty tax ID or IBAN: 6 without tax ID, 2 without IBAN.

| Record | Display name | Entity | Emptied field | Record created by rule |
|---|---|---|---|---|
| V-000109 | Kaffee & Co OHG | VDE | tax ID | clean record |
| V-000117 | NORDWIND LOGISTICS | VDE | tax ID | D1 |
| V-000119 | Bright Agency | VFR | tax ID | D1 |
| V-000120 | Clean Space Facilities B.V. | VDE | IBAN | D1 |
| V-000122 | Metro Media | VDE | tax ID | D1 |
| V-000124 | Nordwind Logistics GmbH | VFR | tax ID | D2 |
| V-000125 | Nordwind Spedition GmbH | VDE | IBAN | D5 |
| V-000127 | Shopsys Inc | VUS | tax ID | D5 |

### D6 — weak PO discipline (`world.POSpec.in_asis`, `seed.rule_d6_po_discipline`)

The requester gave the supplier a PO number, but for many POs the order was never keyed and approved in the ERP. Only these POs exist in as-is: 4500101, 4500105, 4500114, 4500117, 4500126 and 4500131, that is **6 of 12 (50%)**. Receipts follow their POs: **7 of 11 receipt lines** (6 of 9 receipt documents). The POs of documents 5 (4500123), 6 (4500109) and 11 (4500128) do not exist in as-is.

### Resulting numbers

| Measure | To-be | As-is | Case |
|---|---|---|---|
| Vendor records | 14 | 28 | 2,800 |
| Suppliers (unique by tax ID) | 12 | 12 | about 1,200 |
| **Accounts per supplier** | **1.17** | **2.33** | 2,800 / 1,200 ≈ 2.33; target ≤ 1.2 |
| Records linked to their supplier | 14 | 14 (14 unlinked) | — |
| Records missing tax ID or IBAN | 0 | 8 (29%) | — |
| Linked records with terms different from the agreed terms | 0 | 5 (D3) | "terms on vendor records differ from contracts" |
| Unlinked D1 / D2 records with non-agreed terms | — | 10 of 10 | — |
| Inactive records | 0 | 4 | — |
| Purchase orders | 12 | 6 (50%) | — |
| Receipt lines | 11 | 7 | — |
| Contract rows / catalogues | 4 / 1 | 4 / 0 | — |

`created_by` in as-is: finance.de 11, finance.fr 5, finance.us 4, store.berlin01 3, store.paris02 3, ap.temp 2. Store users and a temporary AP login created 8 of the 28 records: anyone could create a vendor. In to-be only finance users appear (finance.de 8, finance.fr 4, finance.us 2).

## 5. Intake and registration

- **As-is**: two mailboxes (`world.AP_MAILBOX`, `world.STORE_MAILBOX`): **ap@velox.com** (documents 1, 3–8 and 10–12) and **store.berlin01@velox.com** (documents 2 and 9). A document is registered when AP opens ap@ and keys it, **+1 business day**, or after the store forwards it, **+7 business days** (`sim.registration_delay_days`, values from `sim.DURATIONS`). Until then the app shows "Not registered yet" with the expected date (`sim.registration_date`).
- **To-be**: **one intake address**, ap@velox.com. Every document is registered on arrival (`registered_on = received_on`): from that minute it has an ID and the clock runs. Suppliers are asked to use the one address, so every to-be document arrives there.

| No | As-is mailbox | Received | Registered as-is | Business days |
|---|---|---|---|---|
| 1 | ap@velox.com | Thu 01 Oct 08:42 | Fri 02 Oct | 1 |
| 2 | store.berlin01@velox.com | Fri 02 Oct 08:15 | Tue 13 Oct | 7 |
| 3 | ap@velox.com | Thu 01 Oct 09:15 | Fri 02 Oct | 1 |
| 4 | ap@velox.com | Fri 02 Oct 11:30 | Mon 05 Oct | 1 |
| 5 | ap@velox.com | Fri 02 Oct 14:02 | Mon 05 Oct | 1 |
| 6 | ap@velox.com | Mon 28 Sep 10:21 | Tue 29 Sep | 1 |
| 7 | ap@velox.com | Thu 01 Oct 07:55 | Fri 02 Oct | 1 |
| 8 | ap@velox.com | Thu 01 Oct 10:33 | Fri 02 Oct | 1 |
| 9 | store.berlin01@velox.com | Thu 01 Oct 12:10 | Mon 12 Oct | 7 |
| 10 | ap@velox.com | Fri 02 Oct 09:05 | Mon 05 Oct | 1 |
| 11 | ap@velox.com | Fri 02 Oct 14:20 | Mon 05 Oct | 1 |
| 12 | ap@velox.com | Thu 01 Oct 16:40 | Fri 02 Oct | 1 |

Registered the same day: as-is 0 of 12; to-be 12 of 12. Average registration lag as-is (10 × 1 + 2 × 7) / 12 = 2.0 business days; to-be 0.

## 6. Simulated durations

Lookup table `sim.DURATIONS` (business days). The as-is values are calibrated so that a non-PO invoice sent to a store matches the case's 26 business days.

| Scenario | Activity | Business days | Key in `sim.DURATIONS` | Reasoning |
|---|---|---|---|---|
| As-is | Store mailbox forwarding to AP | 7 | `store_forwarding` | Store staff forward supplier invoices to AP when they get to them, not on a schedule |
| As-is | AP opens ap@ and keys the invoice | 1 | `ap_open_and_key` | AP picks up the mailbox the next business day and types the invoice |
| As-is | Email loop (untracked follow-up) | 8–16, mean 12 | `email_loop_min`, `email_loop_max` | About 45% of invoices in the case need email back-and-forth to find a PO, a receipt or an approver, with no owner and no SLA |
| As-is | Email approval | 4 | `email_approval` | Approval by email to the cost-centre owner |
| As-is | Posting | 1 | `posting` | Keyed into the ERP the next business day |
| To-be | Registration | 0 | `registration` | Registered on arrival |
| To-be | Reading and rules | 0 | `extraction_and_gate` | Minutes, not days |
| To-be | Workflow approval | 1 | `workflow_approval` | Approval task in the workflow |
| To-be | Posting | 0 | `posting` | Posted by the gate |
| To-be | Exception or human review | the SLA of the type | `taxonomy.EXCEPTION_TYPES` | The owner resolves within the SLA, except one document resolved past it (below) |

- **As-is derivation**: a non-PO invoice sent to a store takes store forwarding 7 + AP keying 1 + email loop 12 + email approval 4 + posting 1 = **25 business days** (21–29 with the loop's 8–16), close to the case's 26.
- **Email loop**: `sim.email_loop_days(key)` draws a whole number of days uniformly from 8 to 16 with `random.Random("2026:<key>")`, seed `sim.EMAIL_LOOP_SEED = 2026`. The key is the document's number before the renumbering of 25 Sep 2026 (`DocumentSpec.loop_key`), so each document kept its as-is loop length. Over 500 keys the mean is about 12 (checked in `tests/test_sim.py`).
- **Cycle per document** (`sim.cycle_breakdown`, `sim.cycle_days`, stored in `gate_decision.simulated_days`): as-is "matched" = (store forwarding 7 if the store mailbox) + AP keying 1 + posting 1; as-is "email loop" = the same + the seeded loop + email approval 4; to-be with no human step = 0; to-be exception or human review = its SLA; a Block is never posted.
- **One exception past its SLA**: B-06 (price mismatch, buyer Sofia Brandt) is resolved 3 business days after its SLA (`DocumentSpec.sla_overrun_days`), so its simulated cycle is 2 + 3 = 5 business days and the cockpit shows it past SLA.
- **Exception cockpit snapshot** (`sim.cockpit_as_of`): the queue is shown as it stands at the close (17:00) of the business day on which its last open item was registered. Days open = business days from registration to that moment; **past SLA** = still open with more days open than its SLA. To-be, after the next email is received: close of Fri 2 Oct 2026; B-06 (registered Mon 28 Sep) has 4 days open against an SLA of 2 (past SLA), B-01 has 1 and B-05 0 (within SLA). Before it is received the snapshot is the close of Thu 1 Oct, and B-06 is already past SLA (3 days). The as-is email-loop bucket lists its eight documents with their days in the loop; none is posted by the snapshot.

## 7. Reading with Gemini

- **Model**: a Gemini model through the Google GenAI SDK (`google-genai`). `GEMINI_MODEL` defaults to `gemini-2.5-flash` (`config.GEMINI_MODEL`); if the API rejects it as unavailable, extraction falls back automatically to the newest generally available Flash model the key can use (found with the SDK's model list, never hard-coded). The cached extractions of the case documents were read by `gemini-3.8-flash`.
- **Call**: one call per PDF, structured output with the JSON schema `extract.InvoiceExtraction`: 19 fields, each with a value (nullable) and a confidence from 0 to 1, including the **document type** (invoice, credit note, payment reminder, statement, other). Gemini reads and classifies; it never decides.
- **Cache**: `data/cache/<SHA-256 of the PDF>.json`. The demo never calls the API: *Reset demo*, *Run scenario* and *Run the control gate* read the cache (or the fixtures) only. The invoice page shows one provenance line, for example "Read by gemini-3.8-flash (cached)".
- **Screening** (simulated): before the model, each PDF or XML is checked for an accepted file type and for hidden instructions. In the demo the check is simulated (it logs `Screened → pass`); in the deck's architecture this is where Model Armor sits.
- **Critical fields** (`extract.CRITICAL_FIELDS`): supplier identity (tax ID, IBAN or name), invoice number, gross total, bill-to name. **Confidence threshold 0.80** (`config.CONFIDENCE_THRESHOLD`): in to-be, a critical field below 0.80 sends the document to Human review (Low extraction confidence, AP review, SLA 1 day). As-is has no model and no threshold: AP types the fields (the simulation uses the same reading).
- **EXTRACTOR=fixture**: reads ground-truth JSON from `tests/fixtures/` (built from `world.DOCUMENTS` by `tests/make_fixtures.py`, confidence 0.99 for printed fields and 0 for absent ones). It never calls the API and the UI labels it "FIXTURE — ground-truth test data, not a Gemini output". The tests always run in this mode.

## 8. Control-gate rules (`app/gate.py`)

The to-be gate runs these rules in order; each writes one line of the rule log (`[B-05] Rule → result — reason`) and the last line is the outcome.

1. **Registered on arrival** (ID, timestamp, channel; the clock starts) → 2. **Screened** → 3. **Read with Gemini** → 4. **Confidence** → 5. **Document type** → 6. **Supplier (tax ID)** → 7. **Legal entity** → 8. **Duplicate** → 9. **Credit note** → 10. **Commitment** → 11. **Terms** → 12. **Tolerances** → 13. **Approval limit** → **Outcome**: exactly one of **Post · Exception · Block · Human review**.

The as-is has no gate: AP keys the document, looks the supplier up by name, checks for a duplicate on that one record, looks for the PO and posts, or starts an untracked email loop. Its log is called "Processing log"; the to-be-only rules are not part of it.

| Rule | Value | Defined in | Reasoning |
|---|---|---|---|
| Supplier (to-be) | At supplier level: tax ID exact, then IBAN exact. A name match alone does **not** resolve: the document goes to Exception *Unknown vendor* | `gate._resolve_vendor`, `gate._resolve_party` | Identifiers first; a name is not proof of identity |
| Supplier (as-is, naive) | First active record (lowest ID) whose display name equals the printed supplier name (case-insensitive, trimmed); else the first with name similarity ≥ 90; else AP opens a new record. Tax ID and IBAN are not used | brief section 8; `tests/test_seed.py` | "AP picks one of several accounts" (deck slide 4). It lands document 2 on V-000117, document 4 on V-000119 and document 10 on V-000104 (VDE) |
| Name similarity | rapidfuzz `token_set_ratio` ≥ 90 on normalised names | `normalize.NAME_SIMILARITY_THRESHOLD` | Used by the as-is lookup and to flag duplicate records; all D1 spellings score 91 or more against their supplier, names of different suppliers 44 or less |
| Name normalisation | Lowercase, punctuation removed, legal suffixes stripped: AG, BV, GmbH, Inc, Limited, LLC, Ltd, OHG, SA, SARL, SAS, SL | `normalize.LEGAL_SUFFIXES` | Suffixes that appear in the seed or are common |
| Legal entity | Tax ID of the bill-to block, else the bill-to name with the legal suffix kept; the supplier must hold a record in that entity | `gate._legal_entity` | Stripping the suffix would make the three Velox entities identical |
| Duplicate | Same supplier AND (same normalised invoice number OR same gross amount and same invoice date), on any of the supplier's records. The rule line names the leg that matched | `gate._duplicate_check`, `normalize.normalise_invoice_number` | Normalisation removes spaces, punctuation, leading zeros, a leading label ("Invoice no.", "Rechnung Nr.") and words such as COPY, REMINDER, KOPIE, DUPLICATA |
| Document type | Invoice and credit note continue. A payment reminder or statement that reproduces a known invoice goes on to the duplicate rule; one with no invoice behind it is Exception *Supplier payment-status query*. Anything else is Human review ("not an invoice or credit note") | `gate._document_type` | Document 2 is a reminder that reproduces document 1: it is blocked as a duplicate |
| Credit note | Linked to the invoice it credits, same supplier; none found → Exception *Credit note without invoice* | `gate._credit_note` | Document 4 is linked to document 3 and posted |
| Commitment | By spend category: goods → PO + receipt; services → PO + service confirmation; recurring → contract schedule (no PO, contract of the billed entity, net inside the monthly range, period not invoiced yet); small store purchase → card / catalogue (supplier on a catalogue of the billed entity, gross within its CHF limit); none → Exception *No PO* | `gate._commitment_match`, `world.CONTRACTS`, `world.CATALOGUES` | Deck slide 9. Card / catalogue counts as a commitment (brief v2 section 1) |
| Terms | Always from the master, never from the invoice; a different term on the invoice is only named in the rule line | `gate._terms` | Documents 1, 2 and 6 print terms different from the agreed ones |
| Tolerances | Price and quantity: 2% or 50 in the invoice currency, whichever is larger, on net amounts; outside → Exception *Price or quantity mismatch* to the buyer of the PO | `gate.price_tolerance` | Absorbs rounding and small freight; document 6 (+300.00 on 6,300.00, 4.8%) is outside |
| Approval limit | Gross converted to CHF at the simulated rate; above CHF 25,000 → Human review *Amount above approval limit* by the next approver of the entity, SLA 2 days | `world.APPROVAL_MATRIX`, `gate._approval_limit` | Fires only on document 1: 27,846.00 EUR ≈ CHF 26,175. Document 12 is 17,250.00 USD ≈ CHF 13,800 |
| Simulated FX rates | EUR → CHF 0.94, USD → CHF 0.80, GBP → CHF 1.10; used for the approval and catalogue limits only, and named in the rule line | `world.CHF_RATES` | Fixed rates keep the demo deterministic; they are not market rates |
| Non-PO requester | Kaffee & Co (VDE) → Paul Neumann, manager of store Berlin 01 | `world.NON_PO_REQUESTERS` | The store that orders the coffee owns the spend |
| Processing order | By registration time, then document ID; duplicates, credit notes and contract periods are checked against documents processed earlier | `gate.order_key`, `gate.run_scenario` | Mirrors arrival: document 1 before its reminder (document 2), document 3 before its credit note (document 4) |
| Several POs on one invoice | Every quoted PO that exists and belongs to the supplier and the billed entity is used; each line is matched against the lines of all of them | `gate._match_po` | Test set v2 document 16 |

### Robustness to real extractions

The fixtures are perfect ground truth; a real Gemini extraction can differ. These rules keep a plausible reading error from flipping an outcome silently (`app/gate.py`, tested in `tests/test_gate.py`):

| Situation | Rule |
|---|---|
| PO number printed as "PO 4500117", "P.O. #4500117" or with several numbers | `normalize.normalise_po_number` strips labels and separators; every quoted PO is tried |
| Invoice lines in another order, split or repeated | Lines are mapped to PO lines by description first (position only as a tie-break); quantities and amounts are summed per PO line before the checks |
| Lines that do not add up to the net total | To-be: Human review; as-is: email loop. Never posted with no human step |
| No lines read | Header-level check: net total against the PO total within tolerance, and every PO line received or confirmed; the line checks show "Net total" and "PO line n" |
| Net total missing | Derived as gross − tax, else the sum of the lines; if still unknown, no contract match is attempted (Human review) |
| Currency written as "eur", "€", "$" or "£" | Normalised to the ISO code before comparing and posting |
| Dates printed day-first (30.09.2026, 30/09/2026) | Accepted; an unreadable invoice date never matches a contract period (Human review) |
| Bill-to name with the store appended ("Velox Retail GmbH Store Berlin 01") | Mapped by the longest whole-word prefix that names exactly one Velox entity |
| An invoice with a negative total | To-be: Human review ("negative total, probable credit note") |
| Values read back from the database | Every number stored in a decision is a float, so a re-run gives identical results |

### Exception types (deck A3; single source of truth `app/taxonomy.py`)

| Type | Owner | SLA (business days) | Outcome | Standard resolution |
|---|---|---|---|---|
| No PO | Requester, then budget owner | 2 | Exception | Confirm the purchase and raise the commitment; the budget owner approves |
| PO exists, no receipt or confirmation | Receiver / requester | 2 | Exception | Confirm receipt or service delivery |
| Price or quantity mismatch | Buyer | 2 | Exception | Agree a correction with the supplier or approve the variance |
| PO not found or wrong vendor | Requester | 2 | Exception | Provide the correct PO |
| Duplicate vendor record (info) | Master data owner | 5 | info task, the document is posted | Merge or deactivate the duplicate record |
| Unknown vendor | Master data owner | 2 | Exception | Onboard the supplier through the vendor request workflow |
| Wrong legal entity | AP | 1 | Exception | Ask the supplier to re-issue the invoice, or re-assign it |
| Duplicate invoice | Blocked; AP replies with status | — | Block | Blocked before posting; AP replies to the supplier with the status |
| Credit note without invoice | AP | 2 | Exception | Identify the original invoice |
| Low extraction confidence | AP review | 1 | Human review | Verify the fields against the document |
| Supplier payment-status query | Agent drafts, AP approves | — | Exception | Reply with the payment status: the agent drafts, AP approves before anything is sent |
| Amount above approval limit | Next approver in the matrix | 2 | Human review | The next approver decides in the approval workflow |

- A PO not found goes to the requester of the supplier's latest PO in the billed entity; if there is none, to AP, and the reason says so.
- An invoice that arrives only in the body of an email (no attachment) has nothing to read: it goes to Human review under its own name, "invoice only in the email body", and AP keys it from the email text.
- **Routing of the case documents (to-be)**: B-01 → Stefan Keller, next approver (amount above the approval limit); B-02 → Block (duplicate of B-01; AP, Marco Ruiz, replies to the supplier); B-05 → Jonas Weber, receiver / requester (service not confirmed); B-06 → Sofia Brandt, buyer (price).
- **Owner messages**: for an Exception or a Human review, "Draft message to owner" asks Gemini for two sentences built from the facts of the decision only ("Draft by Gemini — reviewed by AP"). Nothing is sent. Drafts are cached in `data/cache/drafts/`.
- **As-is**: there are no types, owners or SLAs; every document that needs follow-up goes into the same "Email loop — untracked".

## 9. Metrics (`app/metrics.py`, deck A6)

The four metrics of the deck (slide 11), with the deck's definitions, plus one small indicator. Every duration is in **simulated** business days. The Compare page and the Metrics page show the same values; each tile shows its definition, with the numbers of A and B, on hover and keyboard focus.

| Metric | Tag | Definition | A (as-is) | B (to-be) |
|---|---|---|---|---|
| First-pass match rate | Upstream · process health | Invoices matched at the first pass to a commitment (PO + receipt or confirmation, contract schedule, or card / catalogue) and within tolerance, with no follow-up ÷ invoices received (credit notes excluded; the reminder copy included) | 27.3% (3 of 11: documents 3, 7, 8) | 72.7% (8 of 11: documents 1, 3, 7–12) |
| Accounts per supplier | Upstream · process health | All vendor records, inactive included (as the case's 2,800 ÷ 1,200) ÷ unique suppliers by tax ID | 2.33 (28 ÷ 12) | 1.17 (14 ÷ 12) |
| Touchless rate | Downstream · automation efficiency | Invoices posted with no human step ÷ invoices posted. An Exception or Human review counts as posted after its resolution; a Block is excluded from both | 0.0% (0 of 12: AP keys every invoice) | 72.7% (8 of 11) |
| Invoice cycle time (business days) | Downstream · automation efficiency | Median simulated business days from arrival (to-be: registration, the same minute) to approved and ready to pay, over the invoices posted; P90 in the tooltip | 15 (P90 21) | 0, same day (P90 2) |
| Registered same day | Indicator | Documents registered on the day they arrive ÷ documents | 0.0% (0 of 12) | 100.0% (12 of 12) |

- Document 1 counts as a first-pass match in B: its contract matches within the range; the human review it needs is for the approval limit, not for the match.
- **Before the next email is received**, B has 11 documents: first-pass match 80.0% (8 of 10), touchless 80.0% (8 of 10), registered same day 11 of 11. After *Receive next email* and *Run the control gate*, B has 12 and the values above.
- No other metric is shown. Invoice amounts appear as document data (invoice, PO and contract amounts), never as totals or value figures.

## 10. The 12 case documents

Defined in `world.DOCUMENTS`, rendered as PDFs by `app/invoices_gen.py`. "ap@" = ap@velox.com, "store" = store.berlin01@velox.com (as-is mailboxes; in to-be every document arrives on ap@).

| No | Supplier (printed name) | As-is mailbox | Bill-to | Net | Gross | Commitment | A (as-is) | B (to-be) |
|---|---|---|---|---|---|---|---|---|
| 1 | Nordwind Logistics GmbH | ap@ | VDE | 23,400.00 | 27,846.00 EUR | Contract CT-2025-001 | Email loop — untracked | Human review: amount above approval limit |
| 2 | NORDWIND LOGISTICS (payment reminder) | store | VDE | 23,400.00 | 27,846.00 EUR | Contract CT-2025-001 | Email loop, posted a second time | Block: duplicate of B-01 |
| 3 | Bright Agency SARL | ap@ | VFR | 12,000.00 | 14,400.00 EUR | PO 4500117 | Posted by AP | Post |
| 4 | Bright Agency (credit note) | ap@ | VFR | -1,500.00 | -1,800.00 EUR | Credit for INV-2026-0457 | Posted by AP, unapplied | Post, credit note linked to document 3 |
| 5 | FitOut Partners Ltd | ap@ | VDE | 48,000.00 | 48,000.00 EUR | PO 4500123 | Email loop — untracked | Exception: PO exists, no receipt or confirmation |
| 6 | Atlas Displays SL | ap@ | VDE | 6,600.00 | 6,600.00 EUR | PO 4500109 | Email loop — untracked | Exception: price or quantity mismatch (past SLA) |
| 7 | Shopsys Software Inc. | ap@ | VUS | 9,600.00 | 9,600.00 USD | PO 4500131 | Posted by AP | Post |
| 8 | QuickPrint SAS | ap@ | VFR | 2,400.00 | 2,880.00 EUR | PO 4500114 | Posted by AP | Post |
| 9 | Kaffee & Co OHG | store | VDE | 151.26 | 180.00 EUR | Card / catalogue CAT-2026-001 (to-be) | Email loop — untracked | Post |
| 10 | Cleanspace Facilities BV | ap@ | VFR | 3,200.00 | 3,200.00 EUR | Contract CT-2025-003 | Email loop, posted to the wrong entity | Post |
| 11 | SecureNet AG | ap@ | VDE | 4,800.00 | 4,800.00 EUR | PO 4500128 | Email loop — untracked | Post |
| 12 | Harbor Freight Forwarders Inc. | ap@ | VUS | 17,250.00 | 17,250.00 USD | Contract CT-2025-004 | Email loop — untracked | Post |

A: 4 posted by AP, 8 in the email loop, nothing touchless. B: 8 Post, 2 Exception, 1 Block, 1 Human review.

### Deliberate traps

- **Document 2** reproduces document 1 (NWL-2026-00913, same amounts): a payment reminder sent to the store mailbox with a COPY watermark, printed as **"NORDWIND LOGISTICS"**, exactly the display name of the D1 duplicate record V-000117. Gemini classifies it as a payment reminder; the duplicate rule blocks it in to-be; the as-is lookup posts it a second time on V-000117.
- **Document 4** (credit note CN-2026-0031) is printed as **"Bright Agency"** from a different template, exactly the display name of the D1 duplicate V-000119 in VFR: the as-is posts the credit on that record, where it stays unapplied.
- **Payment terms**: documents 1 and 2 print 14 days (contract: 30); document 6 prints 30 days (master: 60). The others print the agreed terms; the credit note prints none.
- **Document 10** is billed to VFR. Cleanspace has one record in each entity with the same display name, and the naive as-is lookup takes the first one, V-000104 in VDE: a wrong-entity posting.
- **Document 9** is billed to Velox Retail GmbH "Attn: Store Berlin 01" at the store's address, without the Velox tax ID in the bill-to block.
- **Document 6** invoices 150 × 44.00 against a PO at 150 × 42.00; **document 5** has no service confirmation.

### VAT treatment per document

Illustrative, chosen to vary the tax blocks the model has to read; not tax advice.

| No | Invoice number | Invoice date | Tax | Tax amount | Legal mention printed |
|---|---|---|---|---|---|
| 1 | NWL-2026-00913 | 30 Sep | VAT 19% | 4,446.00 | Domestic German supply |
| 2 | NWL-2026-00913 | 30 Sep | VAT 19% | 4,446.00 | Same as document 1 |
| 3 | INV-2026-0457 | 29 Sep | VAT 20% | 2,400.00 | Domestic French supply |
| 4 | CN-2026-0031 | 02 Oct | VAT 20% | -300.00 | Credit note on document 3 |
| 5 | 2026-091 | 30 Sep | VAT 0% | 0.00 | Reverse charge (Article 196, Directive 2006/112/EC) |
| 6 | AD-2026/0788 | 28 Sep | VAT 0% | 0.00 | Intra-Community supply, exempt (Article 138, Directive 2006/112/EC) |
| 7 | SS-100482 | 01 Oct | Sales tax | 0.00 | Sales tax not applicable to this service |
| 8 | QP-26-1043 | 29 Sep | VAT 20% | 480.00 | Domestic French supply |
| 9 | 2026/117 | 30 Sep | VAT 19% | 28.74 | Domestic German supply |
| 10 | CSF-26-10355 | 30 Sep | VAT 0% | 0.00 | Reverse charge (Article 196, Directive 2006/112/EC) |
| 11 | SN-2026-3307 | 01 Oct | VAT 0% | 0.00 | Reverse charge, services supplied from Switzerland |
| 12 | HFF-2026-0930 | 30 Sep | Sales tax | 0.00 | Freight and customs services, no sales tax |

## 11. Decisions

- **24 Sep 2026**: two clean invoices were added to the brief's twelve (SecureNet, Harbor Freight); both stay.
- **25–26 Sep 2026, alignment with the final deck** (brief v2):
  - **Twelve documents**, as in the deck: the Metro Media (wrong legal entity) and Lumen (partial delivery) invoices were dropped; their suppliers, records and POs stay in the master. The other documents were renumbered 1–12; their PDFs did not change, so the cached Gemini readings still apply.
  - **Exception types, owners and SLAs from deck A3**; **four outcomes** (Post, Exception, Block, Human review); **four metrics from deck A6** plus registered same day.
  - **Approval limit** (Human review by the next approver) and the **card / catalogue commitment** replace the earlier automatic approval of small non-PO invoices.
  - **To-be master trimmed to 1.17 records per supplier**: the second records of Shopsys (VDE) and QuickPrint (VDE) and their two background POs were removed; two spelling duplicates were added to the as-is so it keeps 28 records.
  - **One intake address** in to-be; **Reset demo** and **Receive next email** for the four-minute demo.
  - **Group currency CHF** for the limits, with simulated rates.
  - **Scenario A is AP keying**: the AI quick-fix tool of the case read PDFs from one mailbox and posted them, and was halted after three weeks (deck slide 7). The simulation shows today's process after that: AP types every invoice.
- Phase 3 (Google Cloud, real mailbox) follows after the upload; see [PHASE3.md](PHASE3.md).
