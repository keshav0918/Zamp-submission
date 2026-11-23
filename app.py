"""Streamlit interface for invoice-to-PO review."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import pytesseract
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from demo import SAMPLES, make_invoice
from extract import extract_invoice
from inference import InferenceUnavailable, infer_review
from reconcile import history, reconcile, record_extraction_failure, save_review_advice


st.set_page_config(page_title="Invoice Review", layout="wide")

STAGES = [
    "Read and extract invoice",
    "Match purchase order",
    "Check duplicates and reconcile line items",
    "Record decision",
]


def configured_value(name):
    if value := os.environ.get(name, "").strip():
        return value
    try:
        return str(st.secrets.get(name, "")).strip()
    except StreamlitSecretNotFoundError:
        return ""


def show_outcome(outcome):
    status = outcome["status"]
    feedback = {"Approved": st.success, "Needs review": st.warning, "Duplicate": st.info}
    feedback[status](status)
    columns = st.columns(4)
    columns[0].metric("Vendor", outcome["vendor"] or "—")
    columns[1].metric("Invoice", outcome["invoice"] or "—")
    columns[2].metric("Purchase order", outcome["po"] or "—")
    total = f"{outcome['currency']} {outcome['total']}" if outcome["total"] is not None else "—"
    columns[3].metric("Invoice total", total)
    for reason in outcome["reasons"]:
        st.write(f"• {reason}")
    if outcome["candidates"]:
        st.subheader("PO candidates")
        st.dataframe(outcome["candidates"], hide_index=True, width="stretch")
    if outcome["lines"]:
        st.subheader("Line-item evidence")
        st.dataframe(outcome["lines"], hide_index=True, width="stretch")
    if advice := outcome.get("review_advice"):
        st.subheader("AI review guidance")
        st.caption("For manual verification. The recorded decision is Needs review.")
        if advice["suggested_po"]:
            st.text(f"Suggested PO: {advice['suggested_po']}")
        st.text(advice["summary"])
        for number, step in enumerate(advice["next_steps"], start=1):
            st.text(f"{number}. {step}")
    if outcome["sources"]:
        with st.expander("Extraction details"):
            st.write(f"Invoice date: {outcome['date'] or 'Unavailable'}")
            st.write(f"PO reference on invoice: {outcome.get('reference') or 'Unavailable'}")
            for source in outcome["sources"]:
                st.write(source)


st.title("Invoice Review")
run_tab, history_tab = st.tabs(["Live run", "Run history"])
db_path = None
if os.environ.get("INVOICE_SHARED_HISTORY") != "1":
    if "history_directory" not in st.session_state:
        st.session_state.history_directory = TemporaryDirectory(prefix="invoice-review-")
    db_path = Path(st.session_state.history_directory.name) / "runs.sqlite3"

with run_tab:
    upload = st.file_uploader("Upload an invoice PDF", type=["pdf"])
    inference_config = {
        "api_key": configured_value("LLM_API_KEY"),
        "base_url": configured_value("LLM_BASE_URL"),
        "model": configured_value("LLM_MODEL"),
    }
    inference_ready = all(inference_config.values())
    use_inference = st.checkbox(
        "Ask AI for help on exceptions",
        disabled=not inference_ready,
        help=("Sends a limited set of reconciliation evidence to the configured API for Needs review runs."
              if inference_ready else "Configure LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL to enable this option."),
    )
    with st.expander("Download demo invoices"):
        st.caption("Run the readable match first, then the scanned PO typo and quantity mismatch.")
        for filename, attributes in SAMPLES.items():
            st.download_button(filename, make_invoice(*attributes), filename, "application/pdf")
    if st.button("Run invoice checks", type="primary", disabled=upload is None):
        live = st.empty()

        def show_stage(current):
            active = STAGES.index(current)
            with live.container():
                for i, label in enumerate(STAGES):
                    if i == active:
                        st.info(f"Running · {label}")
                    else:
                        st.caption(f"{'Complete' if i < active else 'Waiting'} · {label}")

        show_stage(STAGES[0])
        try:
            if upload.size > 15 * 1024 * 1024:
                raise ValueError("Upload a PDF smaller than 15 MB.")
            invoice = extract_invoice(upload.getvalue())
        except (ValueError, pymupdf.FileDataError, pytesseract.TesseractError, RuntimeError, OSError) as error:
            live.warning("Extraction stopped · Review required")
            reason = str(error) if isinstance(error, ValueError) else "The PDF could not be read. Check the file or OCR installation."
            st.session_state.outcome = record_extraction_failure(upload.name, reason, db_path=db_path)
        else:
            outcome = reconcile(invoice, upload.name, show_stage, db_path=db_path)
            if outcome["status"] == "Needs review" and inference_ready and use_inference:
                live.info("Decision recorded · Generating AI review guidance")
                try:
                    advice = infer_review(outcome, **inference_config)
                except InferenceUnavailable:
                    live.warning("Decision recorded · AI guidance unavailable. You can review the evidence below.")
                else:
                    outcome = save_review_advice(outcome, advice, db_path=db_path)
                    live.success("Run complete · AI review guidance added")
            else:
                live.success("Run complete")
            st.session_state.outcome = outcome
    if "outcome" in st.session_state:
        show_outcome(st.session_state.outcome)

with history_tab:
    runs = history(db_path=db_path)
    if runs:
        st.dataframe(
            [{"Run": run_id, "UTC": created, "File": filename, "Decision": status,
              "PO": json.loads(outcome)["po"]} for run_id, created, filename, status, outcome in runs],
            hide_index=True, width="stretch",
        )
        by_id = {run[0]: run for run in runs}
        selected = st.selectbox("Inspect run", by_id, format_func=lambda key: f"#{key} · {by_id[key][2]}")
        show_outcome(json.loads(by_id[selected][4]))
    else:
        st.info("Upload an invoice to record your first run.")
