"""Vercel Python entry point for the invoice review application."""

import json
import logging
import os
from queue import Queue
from threading import Thread
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from demo import SAMPLES, make_invoice
from extract import extract_pages
from inference import InferenceUnavailable, infer_review
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


class RunRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=150, pattern=r"^[^\x00]+$")
    pages: list[Page] = Field(min_length=1, max_length=10)
    use_ai: bool = False


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
    response.set_cookie(COOKIE, scope, max_age=30 * 24 * 3600, httponly=True, samesite="lax",
                        secure=bool(os.environ.get("VERCEL")) or request.url.scheme == "https")
    return response


def database_settings(scope):
    url = next((value for name in ("DATABASE_URL", "POSTGRES_URL") if (value := os.environ.get(name))), None)
    if os.environ.get("VERCEL") and not url:
        raise HTTPException(503, "Connect a PostgreSQL database and set DATABASE_URL in Vercel.")
    return {"scope": scope, "database_url": url, "db_path": os.environ.get("INVOICE_DB_PATH")}


def inference_settings():
    return {"api_key": os.environ.get("LLM_API_KEY"), "base_url": os.environ.get("LLM_BASE_URL"),
            "model": os.environ.get("LLM_MODEL")}


@app.get("/")
@app.get("/api/invoice")
def get_workspace(request: Request, demo: str | None = None):
    if demo:
        if demo not in SAMPLES:
            raise HTTPException(404, "Demo invoice does not exist.")
        return Response(make_invoice(*SAMPLES[demo]), media_type="application/pdf",
                        headers={"Cache-Control": "public, max-age=3600"})
    scope = workspace(request)
    settings = database_settings(scope)
    try:
        runs = history(**settings)
    except Exception:
        log.exception("Could not load invoice history")
        raise HTTPException(503, "The invoice ledger is unavailable.") from None
    return private_response(JSONResponse({
        "runs": runs, "ai_enabled": all(inference_settings().values()),
        "demos": list(SAMPLES), "storage": "PostgreSQL" if settings["database_url"] else "SQLite",
    }), request, scope)


@app.post("/")
@app.post("/api/invoice")
def run_invoice(payload: RunRequest, request: Request):
    scope = workspace(request)
    settings = database_settings(scope)
    ai = inference_settings()

    def events():
        output = Queue()

        def emit(stage, detail):
            output.put({"stage": stage, "detail": detail})

        def run():
            outcome = None
            try:
                emit("extract", "Reading vendor, invoice number, totals and line items from the PDF")
                invoice = extract_pages([page.model_dump() for page in payload.pages])
                outcome = reconcile(invoice, payload.filename, emit, **settings)
                if outcome["status"] == "Needs review" and payload.use_ai and all(ai.values()):
                    emit("advice", "Generating optional guidance from the reconciliation evidence")
                    try:
                        advice = infer_review(outcome, **ai)
                        outcome = save_review_advice(outcome, advice, **settings)
                    except InferenceUnavailable:
                        outcome["advice_unavailable"] = True
                    except Exception:
                        log.exception("Could not save AI guidance")
                        outcome["advice_unavailable"] = True
                output.put({"outcome": outcome})
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
