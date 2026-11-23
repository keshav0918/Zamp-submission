# Invoice Review · PS-1

Upload an invoice PDF and see how it was read, which purchase order it matches, and why it was approved, routed to review, or flagged as a duplicate. All extraction and decisions run locally in the app.

## Run locally

Use Python 3.10+ and install the Tesseract binary and English language data for scanned PDFs. On Debian or Ubuntu, install `tesseract-ocr` and `tesseract-ocr-eng`. On macOS, use `brew install tesseract`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy publicly

1. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/) with access to the GitHub repository.
2. Select **Create app** and choose repository `keshav0918/Zamp-submission`, branch `main`, and main file path `app.py`. The repository already has the Python and Tesseract dependency files.
3. Deploy and check the app's sharing settings so anyone with the link can view it. Open the resulting `.streamlit.app` URL in a private browser window and run the readable and scanned demo invoices.

By default, each visitor uses a separate SQLite history for their browser session. Starting a new session resets the demo history and PO balances. For a trusted local installation, set `INVOICE_SHARED_HISTORY=1` to retain the shared history in `data/runs.sqlite3`. Leave that setting unset on a public deployment.

## Optional AI review guidance

Set the following values as environment variables or in Streamlit secrets. For local Streamlit secrets, use the ignored `.streamlit/secrets.toml` file. For a hosted app, enter the values in its secrets settings.

```toml
LLM_API_KEY = "paste your key in your private secrets settings"
LLM_BASE_URL = "https://api.openai.com/v1"
LLM_MODEL = "gpt-4o-mini"
```

The URL and model above are examples for OpenAI. For another OpenAI-compatible provider, use its HTTPS API base URL and model ID. The app appends `/chat/completions`. Use a key issued for the configured provider and keep it server-side.

Enable **Ask AI for help on exceptions** before running an invoice. When reconciliation returns **Needs review**, the provider receives a limited set of review reasons, PO candidate evidence, and line-item differences. The request excludes the PDF, raw OCR text, vendor name, and invoice number. Guidance can suggest a PO from the existing shortlist and give the analyst concrete verification steps. It is saved separately from the decision in run history. The app still records and displays the deterministic outcome if the provider is unavailable. Approvals, duplicates, and PDF read failures skip the API call.

## Demo

Download the sample PDFs from the Live run tab and run them in order in a fresh instance.

| PDF | Expected decision |
|---|---|
| 01-readable-match.pdf | Approved. The $650 invoice matches both PO lines. |
| 02-scanned-mistyped-po.pdf | Needs review. OCR reads the scan and PO-1023 is suggested based on the reference and two matching SKUs. |
| 03-line-quantity-mismatch.pdf | Needs review. The invoice also totals $650, but requests more paper than the PO allows. |
| Re-upload the first PDF | Duplicate. It points to the original approval and consumes no additional quantity. |

## Decision rules and scope

- Uses embedded text when available and Tesseract for scanned pages. The extractor supports labeled invoice fields and a simple line-item table with description, quantity, unit price, and amount columns. Unparsed rows and missing fields need review. Other invoice layouts may require extending the parser.
- An exact PO reference, vendor, and currency are required for approval. Fuzzy suggestions are limited to open POs for the same vendor and currency and ranked by matching SKUs, then reference similarity. Ties remain unresolved. The PO CSV is the demo's approved PO list.
- Uses SKU first, with exact normalized description as a fallback for lines without a SKU. Checks unit price, remaining quantity, line arithmetic, subtotal when present, and the invoice total including tax. Prices must match exactly. Arithmetic allows a one-cent rounding difference.
- Prevents duplicate approvals by vendor and invoice number. Only approved runs consume PO quantities. The demo records a decision and supports human review of exceptions.
- All inputs are fictional. Replace the bundled PO data in `data/purchase_orders.csv` to try other vendors and orders.
