"""Apply purchase order and invoice rules and retain an audit trail."""
from collections import defaultdict
from contextlib import closing
import csv
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
PO_CSV = ROOT / "data" / "purchase_orders.csv"
HISTORY = ROOT / "data" / "runs.sqlite3"
CENT = Decimal("0.01")


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def reference(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def load_orders():
    orders = defaultdict(list)
    with PO_CSV.open(newline="") as source:
        for row in csv.DictReader(source):
            orders[row["po"]].append(row)
    return orders


def suggest_orders(invoice, orders, used_by_po):
    invoice_skus = {reference(line.sku) for line in invoice.lines if line.sku}
    candidates = []
    for po, lines in orders.items():
        if normalize(lines[0]["vendor"]) != normalize(invoice.vendor) or lines[0]["currency"] != invoice.currency:
            continue
        if all(used_by_po[po][line["sku"]] >= Decimal(line["quantity"]) for line in lines):
            continue
        po_skus = {reference(line["sku"]) for line in lines}
        overlap = len(po_skus & invoice_skus)
        similarity = fuzz.ratio(reference(invoice.po), reference(po)) if invoice.po else 0
        if overlap or similarity >= 75:
            candidates.append({
                "PO": po,
                "Reference similarity": round(similarity) if invoice.po else None,
                "Matching SKUs": overlap,
            })
    candidates.sort(key=lambda row: (row["Matching SKUs"], row["Reference similarity"] or 0), reverse=True)
    return candidates


def compare_lines(invoice, order_lines, used):
    results = []
    consumed = defaultdict(Decimal)
    for line in invoice.lines:
        matches = [
            po_line for po_line in order_lines
            if (reference(po_line["sku"]) == reference(line.sku) if line.sku
                else normalize(po_line["description"]) == normalize(line.description))
        ]
        issues = []
        sku = ""
        remaining = None
        po_price = None
        if abs(line.quantity * line.unit_price - line.total) > CENT:
            issues.append("Quantity × price differs from line total")
        if line.quantity <= 0 or line.unit_price < 0 or line.total < 0:
            issues.append("Invalid quantity or amount")
        if len(matches) != 1:
            issues.append("No unique PO line")
        else:
            po_line = matches[0]
            sku = po_line["sku"]
            remaining = Decimal(po_line["quantity"]) - used[sku] - consumed[sku]
            po_price = Decimal(po_line["unit_price"])
            if line.quantity > remaining:
                issues.append("Quantity exceeds remaining PO quantity")
            if line.unit_price != po_price:
                issues.append("Unit price differs from PO")
            consumed[sku] += line.quantity
        results.append({
            "Invoice item": line.description,
            "Invoice SKU": line.sku,
            "PO SKU": sku,
            "Invoice qty": str(line.quantity),
            "PO qty remaining": str(remaining) if remaining is not None else "",
            "Invoice unit price": str(line.unit_price),
            "PO unit price": str(po_price) if po_price is not None else "",
            "Invoice line total": str(line.total),
            "Check": "; ".join(issues) if issues else "Matched",
        })
    return results, consumed


def _connect():
    connection = sqlite3.connect(HISTORY)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP,
            filename TEXT NOT NULL, vendor TEXT NOT NULL, invoice TEXT NOT NULL,
            po TEXT, status TEXT NOT NULL, outcome TEXT NOT NULL, quantities TEXT NOT NULL
        )
    """)
    return connection


def history():
    with closing(_connect()) as connection:
        return connection.execute("""
            SELECT id, created, filename, status, outcome FROM runs ORDER BY id DESC LIMIT 100
        """).fetchall()


def record_extraction_failure(filename, reason):
    outcome = {
        "status": "Needs review", "vendor": "", "invoice": "", "po": "", "currency": "",
        "total": None, "date": "", "reference": "", "reasons": [reason], "candidates": [], "lines": [], "sources": [],
    }
    with closing(_connect()) as connection, connection:
        _save(connection, filename, outcome, {})
    return outcome


def _save(connection, filename, outcome, quantities):
    cursor = connection.execute("""
        INSERT INTO runs (filename, vendor, invoice, po, status, outcome, quantities)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (filename, normalize(outcome["vendor"]), outcome["invoice"].strip().upper(),
          outcome["po"], outcome["status"], json.dumps(outcome), json.dumps(quantities)))
    return cursor.lastrowid


def save_review_advice(outcome, advice):
    annotated = {**outcome, "review_advice": advice}
    with closing(_connect()) as connection, connection:
        connection.execute(
            "UPDATE runs SET outcome = ? WHERE id = ? AND status = 'Needs review'",
            (json.dumps(annotated), outcome["run_id"]),
        )
    return annotated


def reconcile(invoice, filename, stage=lambda _: None):
    stage("Match purchase order")
    orders = load_orders()
    order_po = next((po for po in orders if reference(po) == reference(invoice.po)), "")
    exact = bool(order_po)
    reasons = list(invoice.issues)
    candidates = []
    authorized_vendor = any(normalize(lines[0]["vendor"]) == normalize(invoice.vendor) for lines in orders.values())
    if not authorized_vendor:
        reasons.append("Vendor is outside the approved PO dataset.")
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        used_by_po = defaultdict(lambda: defaultdict(Decimal))
        for po, quantities in connection.execute(
            "SELECT po, quantities FROM runs WHERE status = 'Approved'"
        ):
            for sku, quantity in json.loads(quantities).items():
                used_by_po[po][sku] += Decimal(quantity)
        if exact:
            order = orders[order_po][0]
            if normalize(order["vendor"]) != normalize(invoice.vendor) or order["currency"] != invoice.currency:
                reasons.append("The referenced PO belongs to a different vendor or currency.")
                order_po = ""
        else:
            candidates = suggest_orders(invoice, orders, used_by_po)
            if candidates:
                scores = ("Matching SKUs", "Reference similarity")
                top = candidates[0]
                tied = len(candidates) > 1 and all(top[key] == candidates[1][key] for key in scores)
                if not tied:
                    order_po = top["PO"]
                reasons.append("Confirm the intended PO. The invoice reference is missing or did not match exactly.")
            else:
                reasons.append("No matching open PO was found in the demo dataset.")

        stage("Check duplicates and reconcile line items")
        duplicate = connection.execute("""
            SELECT id FROM runs WHERE vendor = ? AND invoice = ? AND status = 'Approved'
        """, (normalize(invoice.vendor), invoice.number.strip().upper())).fetchone()
        lines, consumed = compare_lines(invoice, orders.get(order_po, []), used_by_po[order_po])
        for candidate in candidates[:5]:
            candidate_po = candidate["PO"]
            candidate_lines = lines if candidate_po == order_po else compare_lines(
                invoice, orders[candidate_po], used_by_po[candidate_po]
            )[0]
            candidate["Lines passing every check"] = sum(line["Check"] == "Matched" for line in candidate_lines)
        if any(row["Check"] != "Matched" for row in lines):
            reasons.append("Review the line-item differences below.")
        line_sum = sum((line.total for line in invoice.lines), Decimal("0"))
        if invoice.subtotal is not None and abs(invoice.subtotal - line_sum) > CENT:
            reasons.append("The line items do not reconcile to the invoice subtotal.")
        if invoice.total is not None and abs(line_sum + invoice.tax - invoice.total) > CENT:
            reasons.append("The line items and tax do not reconcile to the invoice total.")
        if duplicate:
            reasons.insert(0, f"Invoice already approved in run #{duplicate[0]}.")
        status = "Duplicate" if duplicate else "Needs review" if reasons else "Approved"
        outcome = {
            "status": status, "vendor": invoice.vendor, "invoice": invoice.number, "po": order_po,
            "total": str(invoice.total) if invoice.total is not None else None, "date": invoice.date,
            "currency": invoice.currency, "reference": invoice.po, "sources": invoice.sources, "lines": lines,
            "candidates": candidates, "reasons": reasons or [
                "Exact PO, approved vendor and currency, available quantities, and matching prices and totals."
            ],
        }
        stage("Record decision")
        quantities = {sku: str(quantity) for sku, quantity in consumed.items()} if status == "Approved" else {}
        outcome["run_id"] = _save(connection, filename, outcome, quantities)
    return outcome
