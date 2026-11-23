"""Vercel Python entry point for the invoice review application."""

from decimal import Decimal
import json
import logging
import os
from queue import Queue
from threading import Thread
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator

from demo import SAMPLES, make_invoice
from extract import extract_pages
from inference import InferenceUnavailable, infer_review
from purchase_orders import MAX_CSV_SIZE, load_catalog
from reconcile import history, reconcile, save_review_advice


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger(__name__)
COOKIE = "invoice_workspace"
Word = tuple[
    Annotated[float, Field(ge=-2000, le=20000)],
    Annotated[float, Field(ge=-2000, le=20000)],
    Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"^[^\x00]+$")],
]


class Page(BaseModel):
    words: list[Word] = Field(max_length=2000)
    ocr: bool
    uncertain: bool = False


LedgerText = Annotated[str, StringConstraints(min_length=1, max_length=150, pattern=r"^[^\x00]+$")]
Quantity = Annotated[Decimal, Field(gt=0, le=1_000_000, allow_inf_nan=False)]


class Approval(BaseModel):
    run_id: int = Field(ge=1, le=1_000_000_000)
    vendor: LedgerText
    invoice: LedgerText
    po: LedgerText
    quantities: dict[LedgerText, Quantity] = Field(min_length=1, max_length=200)
    variance: Decimal = Field(ge=0, le=1, allow_inf_nan=False)


class BrowserLedger(BaseModel):
    last_run_id: int = Field(ge=0, lt=1_000_000_000)
    approvals: list[Approval] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_approvals(self):
        ids = {row.run_id for row in self.approvals}
        invoices = {(row.vendor, row.invoice) for row in self.approvals}
        if len(ids) != len(self.approvals) or len(invoices) != len(self.approvals) or max(ids, default=0) > self.last_run_id:
            raise ValueError("Approval history is inconsistent.")
        return self


class RunRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=150, pattern=r"^[^\x00]+$")
    pages: list[Page] = Field(min_length=1, max_length=10)
    use_ai: bool = False
    ledger: BrowserLedger | None = None
    catalog_id: str = Field(default="demo", pattern=r"^(demo|[a-f0-9]{64})$")
    catalog_csv: str | None = Field(default=None, max_length=MAX_CSV_SIZE)


class CatalogRequest(BaseModel):
    action: Literal["validate_catalog"]
    csv: str = Field(max_length=MAX_CSV_SIZE)


def workspace(request):
    value = request.cookies.get(COOKIE, "")
    try:
        key = UUID(value, version=None)
        if key.version == 4 and str(key) == value:
            return value
    except ValueError:
        pass
    return str(uuid4())


def private_response(response, request, scope):
    response.headers["Cache-Control"] = "private, no-store"
    if scope:
        response.set_cookie(COOKIE, scope, max_age=30 * 24 * 3600, httponly=True, samesite="lax",
                            secure=bool(os.environ.get("VERCEL")) or request.url.scheme == "https")
    return response


def database_settings():
    if database_url := os.environ.get("DATABASE_URL"):
        return {"database_url": database_url}
    if not os.environ.get("VERCEL") and (db_path := os.environ.get("INVOICE_DB_PATH")):
        return {"db_path": db_path}
    return {}


def ledger_scope(workspace_id, catalog_id):
    return workspace_id if catalog_id == "demo" else f"{workspace_id}:{catalog_id}"


def catalog_details(catalog_id, orders):
    return {"id": catalog_id, "count": len(orders), "rows": sum(map(len, orders.values()))}


def uploaded_catalog(text):
    try:
        return load_catalog(text)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def inference_settings():
    return {"api_key": os.environ.get("LLM_API_KEY"), "base_url": os.environ.get("LLM_BASE_URL"),
            "model": os.environ.get("LLM_MODEL")}


@app.get("/")
@app.get("/api/invoice")
def get_workspace(request: Request, demo: str | None = None, catalog_id: str = "demo"):
    if demo:
        if demo not in SAMPLES:
            raise HTTPException(404, "Demo invoice does not exist.")
        return Response(make_invoice(*SAMPLES[demo]), media_type="application/pdf",
                        headers={"Cache-Control": "public, max-age=3600"})
    database = database_settings()
    if catalog_id != "demo" and (len(catalog_id) != 64 or any(char not in "0123456789abcdef" for char in catalog_id)):
        raise HTTPException(422, "Invalid PO catalog ID.")
    scope = workspace(request) if database else None
    try:
        runs = history(scope=ledger_scope(scope, catalog_id), **database) if database else []
        default_id, orders = load_catalog()
    except Exception:
        log.exception("Could not load invoice history")
        raise HTTPException(503, "The invoice ledger is unavailable.") from None
    return private_response(JSONResponse({
        "runs": runs, "ai_enabled": all(inference_settings().values()),
        "demos": list(SAMPLES), "storage": "PostgreSQL" if "database_url" in database else "SQLite",
        "browser_ledger": not database,
        "default_catalog": catalog_details(default_id, orders),
    }), request, scope)


@app.post("/")
@app.post("/api/invoice")
def run_invoice(payload: RunRequest | CatalogRequest, request: Request):
    if isinstance(payload, CatalogRequest):
        catalog_id, orders = uploaded_catalog(payload.csv)
        return JSONResponse(catalog_details(catalog_id, orders), headers={"Cache-Control": "no-store"})
    catalog_id, orders = uploaded_catalog(payload.catalog_csv)
    if catalog_id != payload.catalog_id:
        raise HTTPException(422, "The PO CSV changed. Upload it again before running the invoice.")
    database = database_settings()
    scope = workspace(request) if database else None
    storage_scope = ledger_scope(scope, catalog_id) if database else None
    if not database and payload.ledger is None:
        raise HTTPException(422, "Browser history is missing. Reload the app and try again.")
    ai = inference_settings()

    def events():
        output = Queue()

        def emit(stage, detail):
            output.put({"stage": stage, "detail": detail})

        def run():
            try:
                emit("extract", "Reading vendor, invoice number, totals and line items from the PDF")
                invoice = extract_pages([page.model_dump() for page in payload.pages])
                if database:
                    outcome = reconcile(invoice, payload.filename, emit, scope=storage_scope, orders=orders, **database)
                else:
                    outcome = reconcile(
                        invoice, payload.filename, emit,
                        approvals=[row.model_dump(mode="json") for row in payload.ledger.approvals],
                        run_id=payload.ledger.last_run_id + 1,
                        orders=orders,
                    )
                approval = outcome.pop("approval", None)
                if outcome["status"] == "Needs review" and payload.use_ai and all(ai.values()):
                    emit("advice", "Generating optional guidance from the reconciliation evidence")
                    try:
                        advice = infer_review(outcome, **ai)
                        if database:
                            outcome = save_review_advice(outcome, advice, scope=storage_scope, **database)
                        else:
                            outcome["review_advice"] = advice
                    except InferenceUnavailable:
                        outcome["advice_unavailable"] = True
                    except Exception:
                        log.exception("Could not save AI guidance")
                        outcome["advice_unavailable"] = True
                output.put({"outcome": outcome, "approval": approval})
            except Exception:
                log.exception("Invoice processing failed")
                output.put({"error": "The invoice run could not be completed. Refresh history before retrying."})
            finally:
                output.put(None)

        Thread(target=run, daemon=True).start()
        while (event := output.get()) is not None:
            yield json.dumps(event, ensure_ascii=False) + "\n"

    response = StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no"})
    return private_response(response, request, scope)
