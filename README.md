# Invoice Review · PS-1

A Vite browser app and Python API for matching vendor invoices to purchase orders. Upload a readable or scanned PDF, follow the live pipeline, and open the decision with its line-item evidence and audit history.

## Deploy on Vercel

1. Import [keshav0918/Zamp-submission](https://github.com/keshav0918/Zamp-submission) from GitHub in the [Vercel dashboard](https://vercel.com/new). Select branch `main` and keep the Vite framework and repository root.
2. Connect a PostgreSQL database, such as Neon from the Vercel Marketplace. Set `DATABASE_URL` (or `POSTGRES_URL`) to its pooled connection string in the Vercel project's production environment. The API initializes the ledger on the first request. A database is required on Vercel because multiple serverless invocations must share approvals and balances.
3. Deploy after the variable is set. Open the production URL in a private browser window, wait for **Ledger connected**, and run the four built-in samples in order. Ensure deployment protection allows external reviewers to view the production URL.

The app uses a browser workspace cookie to isolate each visitor's run history, duplicate checks and PO balances. The server receives extracted words and coordinates. The original PDF stays in the browser.

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

Open the URL printed by Vite. Local runs use SQLite at `data/web-runs.sqlite3` by default. The browser OCR worker and English language model are copied from locked npm packages and served with the app.

## Live demo

| Step | Expected result |
|---|---|
| **01 · Readable invoice** | Approves the exact PO match and consumes five paper cases and four toner cartridges. |
| **02 · Scanned PO typo** | Browser OCR reads `PO-1032`. The system suggests `PO-1023` with two matching SKUs and routes it to review. |
| **03 · Split invoice + variance** | Approves another invoice against `PO-1023`. It uses the remaining quantities and consumes 0.40 of the PO's 1.00 price-variance allowance. |
| **04 · Quantity overage** | Routes the invoice to review when four paper cases are requested with only three remaining. |
| **Run 01 again** | Detects a duplicate using vendor and invoice number. The prior approval stays in effect without consuming quantities again. |

## Decision policy

1. PDF.js reads embedded text. Tesseract OCR handles scanned pages. The parser extracts labeled header fields and simple line-item tables. Missing fields, unsupported layouts and uncertain numeric OCR evidence go to review.
2. Required invoice fields, ISO dates, currency codes, line arithmetic, subtotal, tax and total are validated. Arithmetic allows one cent for rounding.
3. Exact PO references require the matching vendor and currency. For missing or mistyped references, open POs from that vendor and currency are ranked by SKU overlap, description matches and reference similarity. Fuzzy suggestions and tied candidates need human confirmation.
4. Each line matches by SKU, with exact normalized description as a fallback for missing SKUs. The allowed quantity is the ordered quantity minus prior approvals and quantities already assigned on this invoice.
5. Unit prices may differ by up to 1% of the PO unit price or 0.50 in the invoice currency, whichever is smaller. Absolute variances across all approvals on the PO are capped at 1.00. Overages and discrepancies go to review. Only approvals consume quantity and variance. The database transaction and duplicate constraint protect those balances when requests arrive together.

## Optional LLM inference

Set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` as server-side environment variables. The API must support OpenAI-compatible Chat Completions. Enable **Generate AI review guidance** before running an exception. The deterministic decision is saved first and remains available if the provider fails.

Use the model to explain the most likely cause of an exception, distinguish plausible PO candidates from the shortlist, and suggest what the analyst should verify. The model sees a bounded set of reconciliation evidence, with no raw PDF, invoice number, or vendor name. It can only propose a shortlisted PO. Extraction, vendor approval, duplicate detection, quantities, arithmetic and tolerance are deterministic.
