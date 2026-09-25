# Test set v2 — 26 documents (phase 3)

A wider, harder set than the 14 case documents (brief section 17): suppliers from DE, FR, ES, NL, UK, CH and US; invoices in German, French, Spanish and English; native PDFs, scanned-image PDFs, a COPY watermark, a Peppol BIS Billing 3.0 UBL e-invoice, an invoice pasted in an email body, and a supplier statement; channels ap@, the Berlin store mailbox and a store manager forwarding a supplier invoice. It reuses the seed of the 14-document demo (same suppliers, accounts, POs, receipts and contracts); only document 26 comes from a supplier that is not in the vendor master.

Expected results per scenario: `tests/golden_v2.yaml`. Source of truth: `app/world_v2.py`. Files: `data/invoices_v2/`. Load it with **Load sample documents → Test set v2**; the 14-document demo is unchanged.

All companies, people and identifiers are fictional. Invoice dates are October–November 2026, arrival in the mailboxes Mon 2 – Tue 10 Nov 2026.

## The documents

"Net / gross" in the document currency. VAT: DE 19%, FR 20%, flowers 7%; cross-border services and intra-EU goods at 0% with the legal mention; US documents without tax.

| No | Supplier | Lang. | Format | Channel (sender) | Received | Number / date / terms | Bill-to | Lines | Net / VAT / gross | PO / contract / ref. | Designed to show |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Nordwind Logistics GmbH | DE | native | ap@ (billing@nordwind-logistics.de) | Mon 02 Nov 08:15 | NWL-2026-01027 · 31 Oct · 30 | VDE | "Lager und Fulfilment, Berlin DC — Oktober 2026" 1 × 14,600.00; "Filialbelieferung Deutschland — Oktober 2026" 1 × 9,500.00 | 24,100.00 / 4,579.00 / 28,679.00 EUR | contract CT-2025-001; VAT printed "USt-IdNr. DE281947305" | German invoice, contract match |
| 2 | Nordwind Logistics GmbH | DE | native, **KOPIE** watermark | store (ar@nordwind-logistics.de) | Tue 10 Nov 15:20 | same as 1 | VDE | same as 1 | same as 1 | same as 1; heading "RECHNUNG — KOPIE" | Duplicate on the **same** account: blocked in both scenarios (the naive per-account check works when the account is the same) |
| 3 | Nordwind Logistics GmbH | DE | native, **statement** (doc_type other) | ap@ (ar@nordwind-logistics.de) | Thu 05 Nov 10:00 | KA-2026-11 · 05 Nov · — | VDE | "Rechnung NWL-2026-00913 vom 30.09.2026" 27,846.00; "Rechnung NWL-2026-01027 vom 31.10.2026" 28,679.00 | balance 56,525.00 / — / 56,525.00 EUR | heading "KONTOAUSZUG" | Not an invoice: to-be files it; the as-is tool posts it as an invoice |
| 4 | Cleanspace Facilities BV | EN | native | ap@ (invoicing@cleanspace.nl) | Mon 02 Nov 09:40 | CSF-26-11412 · 31 Oct · 30 | VDE | "Store cleaning services Germany — October 2026 (9 stores)" 1 × 3,150.00 | 3,150.00 / 0 / 3,150.00 EUR (reverse charge) | contract CT-2025-002 | Supplier with two legitimate accounts: VDE account |
| 5 | Cleanspace Facilities BV | FR | native | ap@ (invoicing@cleanspace.nl) | Mon 02 Nov 09:41 | CSF-26-11413 · 31 Oct · 30 | VFR | "Nettoyage des magasins France — octobre 2026 (6 magasins)" 1 × 3,300.00 | 3,300.00 / 0 / 3,300.00 EUR (autoliquidation) | contract CT-2025-003 | Two legitimate accounts: to-be picks VFR; as-is posts to the VDE account |
| 6 | Bright Agency SARL | FR | native | ap@ (billing@bright-agency.fr) | Tue 03 Nov 11:05 | INV-2026-0512 · 30 Oct · 45 | VFR | "Campagne réseaux sociaux T4 — contenu et animation (octobre)" 1 × 6,000.00 | 6,000.00 / 1,200.00 / 7,200.00 EUR | PO 4500121 (no service confirmation) | French invoice, service not confirmed |
| 7 | QuickPrint SAS | FR | native, **credit note** | ap@ (accounts@quickprint.fr) | Wed 04 Nov 14:30 | AV-26-0088 · 04 Nov · — | VFR | "Remise commerciale — volume 2026" 1 × -200.00 | -200.00 / -40.00 / -240.00 EUR | **no reference** | Credit note without reference |
| 8 | QuickPrint SAS | FR | native, **forwarded** | ap@ (luc.bernard@velox.com, "TR: Facture QP-26-1107", comment in the body) | Fri 06 Nov 16:45 | QP-26-1107 · 28 Oct · 30 | VFR | "Affiches A2 en magasin, quadrichromie" 400 × 3.50; "Flyers A5 recto-verso" 10,000 × 0.10 | 2,400.00 / 480.00 / 2,880.00 EUR | PO 4500114 | Forwarded by a store manager; French line texts against English PO lines |
| 9 | Atlas Displays SL | ES | native | ap@ (invoices@atlasdisplays.es) | Tue 03 Nov 09:12 | AD-2026/0861 · 27 Oct · 60 | VFR | "Expositor mural WD-120, acabado roble" 80 × 42.00 | 3,360.00 / 0 / 3,360.00 EUR (intracomunitaria) | PO 4500105; VAT printed "ES-B86419273" | Two legitimate accounts: as-is takes the VDE account, the PO does not match, wrong-entity posting |
| 10 | Atlas Displays SL | ES | native | ap@ (invoices@atlasdisplays.es) | Tue 03 Nov 09:13 | AD-2026/0862 · 27 Oct · 60 | VDE | "Expositor mural WD-120, acabado roble" 150 × 42.00 | 6,300.00 / 0 / 6,300.00 EUR | PO 4500109 | Spanish invoice, clean 3-way match |
| 11 | Metro Media GmbH | DE | **Peppol UBL XML** | ap@ (einvoice@metromedia.de) | Mon 02 Nov 07:30 | MM-2026-248 · 30 Oct · 30 | VDE | "Berlin Außenwerbung September 2026 — 40 Plakatflächen" 1 × 7,500.00 | 7,500.00 / 1,425.00 / 8,925.00 EUR | PO 4500126 | Structured e-invoice parsed directly, no model call |
| 12 | FitOut Partners Ltd | EN | **scanned** (300 dpi, 1.5° skew, clean) | ap@ (accounts@fitoutpartners.co.uk) | Wed 04 Nov 10:20 | 2026-104 · 30 Oct · 30 | VDE | "Store fit-out Hamburg — milestone 1: demolition and electrical works" 1 × 40,000.00 | 40,000.00 / 0 / 40,000.00 EUR (reverse charge) | PO 4500101; VAT printed "GB293 8475 61" | Clean scan read with high confidence |
| 13 | Kaffee & Co OHG | DE | **scanned** (300 dpi, 3° skew, noise, low quality) | store (info@kaffee-und-co.de) | Mon 02 Nov 12:00 | 2026/139 · 30 Oct · 14 | VDE, Attn Store Berlin 01 | "Kaffeebohnen Espresso 1 kg" 3 × 18.50; "Hafermilch 1 l, Karton à 12" 1 × 14.98; "Lieferung" 1 × 10.30 | 80.78 / 15.35 / 96.13 EUR | none | Poor scan: low confidence on the gross total and the number -> human review (to-be) |
| 14 | Kaffee & Co OHG | DE | **email body, no attachment** | store (info@kaffee-und-co.de, "Rechnung 2026/140") | Fri 06 Nov 09:30 | 2026/140 · 06 Nov · 14 (in the text) | VDE | coffee delivery week 45 | 49.00 / 9.31 / 58.31 EUR (in the text) | none | Registered as "unknown", routed to human review |
| 15 | Lumen Store Lighting Ltd | EN | native | ap@ (accounts@lumenlighting.co.uk) | Wed 04 Nov 08:05 | LSL-INV-5602 · 29 Oct · 30 | VDE | "LED track spotlight 30W" 80 × 40.00; "LED panel 600x600 40W — partial delivery, 20 of 40" 20 × 50.00 | 4,200.00 / 0 / 4,200.00 EUR (zero-rated export) | PO 4500112 | Partial delivery invoiced correctly: touchless |
| 16 | SecureNet AG | EN | native | ap@ (billing@securenet.ch) | Thu 05 Nov 13:10 | SN-2026-3391 · 31 Oct · 30 | VDE | "Managed firewall service Q3 2026 (PO 4500128)" 1 × 4,800.00; "Penetration test — e-commerce platform (PO 4500130)" 1 × 3,500.00 | 8,300.00 / 0 / 8,300.00 EUR (reverse charge) | **POs 4500128 and 4500130** | Multi-PO invoice: the gate pinpoints the unconfirmed line |
| 17 | Shopsys Software Inc. | EN | native | ap@ (billing@shopsys.io) | Mon 02 Nov 07:50 | SS-100517 · 01 Nov · 30 | VUS | "Shopsys Commerce Cloud — annual subscription (Oct 2026 – Sep 2027)" 1 × 9,600.00 | 9,600.00 / 0 / 9,600.00 **EUR** | PO 4500131 (USD) | Currency different from the PO |
| 18 | Harbor Freight Forwarders Inc. | EN | native | ap@ (billing@harborff.com) | Mon 02 Nov 16:30 | HFF-2026-1031 · 31 Oct · 30 | VUS | "Ocean freight forwarding — October 2026" 1 × 11,100.00; "Customs brokerage and drayage — October 2026" 1 × 5,700.00 | 16,800.00 / 0 / 16,800.00 USD | contract CT-2025-004 | US contract invoice |
| 19 | Harbor Freight Forwarders Inc. | EN | native, **credit note** | ap@ (billing@harborff.com) | Fri 06 Nov 17:00 | HFF-CN-2026-017 · 06 Nov · — | VUS | "Credit: demurrage charge waived — October 2026" 1 × -450.00 | -450.00 / 0 / -450.00 USD | refers to HFF-2026-1031 | Credit note on the same account: applied in both scenarios |
| 20 | Shopsys Software Inc. | EN | native | ap@ (billing@shopsys.io) | Tue 03 Nov 08:40 | SS-100522 · 02 Nov · 30 | VDE | "POS integration add-on — annual licence (Germany)" 1 × 2,400.00 | 2,400.00 / 0 / 2,400.00 EUR (reverse charge) | PO 4500107 (no service confirmation) | Two legitimate accounts (VUS, VDE): as-is posts to VUS |
| 21 | Bright Agency SARL | FR | native | ap@ (billing@bright-agency.fr) | Tue 03 Nov 11:00 | INV-2026-0530 · 03 Nov · 45 | VFR | "Campagne d'automne 2026 — conception, production et achat média" 1 × 12,000.00 | 12,000.00 / 2,400.00 / 14,400.00 EUR | PO 4500117; VAT printed "FR 62 512 345 678" | French invoice, touchless |
| 22 | Kaffee & Co OHG | DE | native | store (info@kaffee-und-co.de) | Mon 02 Nov 12:05 | 2026/136 · 30 Oct · 14 | VDE, Attn Store Berlin 01 | "Kaffeebohnen Espresso 1 kg" 8 × 18.50; "Hafermilch 1 l, Karton à 12" 2 × 14.98; "Lieferung" 1 × 10.30 | 188.26 / 35.77 / 224.03 EUR | none | Low-value non-PO: DoA auto-approval |
| 23 | Nordwind Logistics GmbH | DE | native | ap@ (billing@nordwind-logistics.de) | Mon 09 Nov 08:30 | NWL-2026-01064 · 06 Nov · 30 | VDE | "Sondertransporte Weihnachtsgeschäft — Vorlauf" 1 × 7,400.00 | 7,400.00 / 1,406.00 / 8,806.00 EUR | contract CT-2025-001 printed, but outside the monthly range | Extra services beyond the contract: no_po to the contract owner |
| 24 | Lumen Store Lighting Ltd | EN | native | ap@ (accounts@lumenlighting.co.uk) | Thu 05 Nov 09:00 | LSL-INV-5611 · 02 Nov · 30 | VDE | "LED strip 5m, warm white" 30 × 22.00 | 660.00 / 0 / 660.00 EUR | **PO 4500999 (does not exist)** | PO not found |
| 25 | QuickPrint SAS | EN | native | ap@ (accounts@quickprint.fr) | Thu 05 Nov 11:15 | QP-26-1119 · 04 Nov · 30 | VDE | "Window stickers, die-cut" 500 × 2.20 | 1,100.00 / 0 / 1,100.00 EUR (intra-EU) | PO 4500119 (goods, no receipt) | Two legitimate accounts; goods not received |
| 26 | **Berliner Blumen GmbH** (not in the master) | DE | native | store (rechnung@berliner-blumen.de) | Thu 05 Nov 15:40 | BB-26-318 · 05 Nov · 14 | VDE, Attn Store Berlin 01 | "Blumendekoration Schaufenster — November" 1 × 268.91 | 268.91 / 18.82 (7%) / 287.73 EUR | none; VAT printed "DE 305 118 442" | Unknown supplier: onboarding (to-be) vs an account created on the fly (as-is) |

Document 14 is the email itself; its body reads (German): "Guten Tag, anbei unsere Rechnung 2026/140 vom 06.11.2026 über 58,31 EUR (netto 49,00 EUR zzgl. 19 % MwSt. 9,31 EUR) für die Kaffeelieferung KW 45 an die Filiale Berlin 01. Zahlbar innerhalb von 14 Tagen auf IBAN DE.. (Kaffee & Co). Mit freundlichen Grüßen, Kaffee & Co OHG".

Document 8 is forwarded with the comment: "Bonjour, facture reçue au magasin la semaine dernière (affiches et flyers de la campagne d'automne). Merci de la régler. Luc".

## Expected results (summary; details in `tests/golden_v2.yaml`)

| No | B — To-be | A — As-is |
|---|---|---|
| 1 | posted, contract CT-2025-001 | email loop, posted on V-000101 |
| 2 | blocked duplicate of 1 | blocked duplicate (same account) |
| 3 | exception `not_an_invoice` (AP specialist) | email loop, **statement posted as an invoice** |
| 4 | posted, contract CT-2025-002, V-000104 | email loop, V-000104 |
| 5 | posted, contract CT-2025-003, V-000113 | email loop, V-000104 → **wrong entity** |
| 6 | `po_no_receipt` → Camille Martin | email loop (PO not in the ERP) |
| 7 | `credit_note_without_invoice` → Marco Ruiz | posted, unapplied credit |
| 8 | posted, 3-way match | posted |
| 9 | posted, V-000115 | email loop (PO of another account), V-000106 → **wrong entity** |
| 10 | posted, 3-way match | email loop (PO not in the ERP) |
| 11 | posted (UBL, no model call) | posted |
| 12 | posted (scan) | posted |
| 13 | human review (low confidence) | email loop, posted |
| 14 | human review (no document attached) | email loop, not posted (keyed by hand) |
| 15 | posted (partial delivery) | email loop |
| 16 | `po_no_receipt` on PO 4500130 → Felix Braun | email loop |
| 17 | `price_qty_mismatch` (currency) → Daniel Price | email loop |
| 18 | posted, contract CT-2025-004 | email loop |
| 19 | credit applied to 18 | posted, credit applied (same account) |
| 20 | `po_no_receipt` → Felix Braun, V-000114 | email loop, V-000105 → **wrong entity** |
| 21 | posted | posted |
| 22 | posted, DoA auto-approved (Paul Neumann) | email loop |
| 23 | `no_po` → contract owner Nina Hoffmann (outside the contract range) | email loop |
| 24 | `po_not_found` → Marco Ruiz | email loop |
| 25 | `po_no_receipt` → Anna Schulz, V-000116 | email loop, V-000108 → **wrong entity** |
| 26 | `unknown_vendor` → Lena Fischer | account V-000129 created on the fly, email loop, posted |

To-be: 14 of 26 touchless (53.8%), 12 exceptions or human reviews, each with an owner and an SLA. As-is: 6 of 26 touchless (23.1%), 19 in the untracked email loop, 4 wrong-entity postings, 1 unapplied credit note, 1 statement posted as an invoice (56,525.00 EUR). v2 is a stress set, not a showcase: most documents are designed to hit a rule.

## Honesty notes

- Without a Gemini key the set runs with ground-truth fixtures (`tests/fixtures_v2/`). The two scans carry **simulated** confidences (document 12: 0.93; document 13: 0.62 on the gross total, 0.71 on the invoice number) to exercise the human-review path; real values come from Gemini once a key is set.
- The UBL e-invoice (document 11) never needs a model: `app/ubl.py` reads it; the UI labels it "UBL e-invoice (parsed, no model call)".
