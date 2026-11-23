# Invoice Review · PS-1

A Vite browser app and Python API for matching vendor invoices to purchase orders with SQLite. Upload a readable or scanned PDF, follow the live pipeline, and open the decision with its line-item evidence and audit history.

## Deploy on Vercel

1. Import [keshav0918/Zamp-submission](https://github.com/keshav0918/Zamp-submission) from GitHub in the [Vercel dashboard](https://vercel.com/new). Select branch `main` and keep the Vite framework and repository root.
2. Click **Deploy**. The core app requires no environment variables or database integration.
3. Open the production URL in a private browser window, wait for **Ledger connected**, and run the four built-in samples in order. Ensure deployment protection allows external reviewers to view the production URL.

Vercel runs each reconciliation in an in-memory SQLite transaction. The browser stores its approvals and latest 100 decisions and supplies the approvals to the API on each run. This keeps split-invoice balances and duplicate checks working across reloads and tabs, without relying on Vercel's temporary filesystem. The ledger is a per-browser demo workspace. A new browser or cleared site data starts fresh. The server receives extracted words, coordinates and prior approvals. The original PDF stays in the browser.

## Run locally

Use Node.js 22+ and Python 3.12. Start these commands from the repo root in separate terminals.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn api.invoice:app --port 8001
```

```bash
npm ci
npm run dev
```

Open the URL printed by Vite. The app uses the same per-browser ledger as Vercel by default. To keep a local SQLite file, start Uvicorn with `INVOICE_DB_PATH=data/web-runs.sqlite3`. Vite bundles the browser OCR worker and English model from locked npm packages. The browser loads them the first time it reads a scan.

## Live demo

| Step | Expected result |
|---|---|
| **01 · Readable invoice** | Approves the exact PO match and consumes five paper cases and four toner cartridges. |
| **02 · Scanned PO typo** | Browser OCR reads `PO-1032`. The system suggests `PO-1023` with two matching SKUs and routes it to review. |
| **03 · Split invoice + variance** | Approves another invoice against `PO-1023`. It uses the remaining quantities and consumes 0.40 of the PO's 1.00 price-variance allowance. |
| **04 · Quantity overage** | Routes the invoice to review when four paper cases are requested with only three remaining. |
| **Run 01 again** | Detects a duplicate using vendor and invoice number. The prior approval stays in effect without consuming quantities again. |

## Upload your own purchase orders

Under **Purchase orders**, select **Upload PO CSV** before reviewing the invoice PDF. Use a UTF-8 CSV with up to 100 line items (128 KB maximum). A PO can span multiple rows. Each row must have a SKU, and each PO must use one vendor and currency with unique SKUs.

```csv
po,vendor,currency,sku,description,quantity,unit_price
PO-1023,Northstar Supply,USD,PPR-01,Copy paper case,10,50.00
PO-1023,Northstar Supply,USD,TNR-02,Black toner cartridge,5,100.00
```

All seven columns are required. Column order can vary, and extra columns are ignored. Quantity must be positive and unit price must be non-negative. Do not include currency symbols in numeric fields. The app checks the CSV before activating it. Each distinct catalog keeps separate approval balances and run history. Select **Use built-in POs** to return to the demo catalog.

The three downloads include the demo purchase orders, so you can upload any of them and run the built-in PDF examples.

- [Office purchase orders, 12 lines](public/samples/office-purchase-orders.csv)
- [Operations purchase orders, 48 lines](public/samples/operations-purchase-orders.csv)
- [Multi-vendor purchase orders, 100 lines](public/samples/multi-vendor-purchase-orders.csv)

## Decision policy

1. PDF.js reads embedded text. Tesseract OCR handles scanned pages. The parser extracts labeled header fields and simple line-item tables. Missing fields, unsupported layouts and uncertain numeric OCR evidence go to review.
2. Required invoice fields, ISO dates, currency codes, line arithmetic, subtotal, tax and total are validated. Arithmetic allows one cent for rounding.
3. Exact PO references require the matching vendor and currency. For missing or mistyped references, open POs from that vendor and currency are ranked by SKU overlap, description matches and reference similarity. Fuzzy suggestions and tied candidates need human confirmation.
4. Each line matches by SKU, with exact normalized description as a fallback for missing SKUs. The allowed quantity is the ordered quantity minus prior approvals and quantities already assigned on this invoice.
5. Unit prices may differ by up to 1% of the PO unit price or 0.50 in the invoice currency, whichever is smaller. Absolute variances across all approvals on the PO are capped at 1.00. Overages and discrepancies go to review. Only approvals consume quantity and variance. Browser tabs serialize runs before reading and saving the ledger. Local file mode uses a SQLite write transaction and duplicate constraint.

## Optional LLM inference

Set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` as server-side environment variables. The API must support OpenAI-compatible Chat Completions. Enable **Generate AI review guidance** before running an exception. The deterministic decision remains available if the provider fails.

On Vercel, add these under **Project Settings → Environment Variables** for **Production**, then redeploy. Keep the API key out of the repository.

Use the model to explain the most likely cause of an exception, distinguish plausible PO candidates from the shortlist, and suggest what the analyst should verify. The model sees a bounded set of reconciliation evidence, with no raw PDF, invoice number, or vendor name. It can only propose a shortlisted PO. Extraction, vendor approval, duplicate detection, quantities, arithmetic and tolerance are deterministic.
