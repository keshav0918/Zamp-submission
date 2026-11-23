"""Resolve a purchase order and record the decision in one transaction."""

from collections import defaultdict
from decimal import Decimal
import json

from rapidfuzz import fuzz

from ledger import open_ledger
from purchase_orders import load_catalog, normalize, reference


CENT = Decimal("0.01")
UNIT_RATE = Decimal("0.01")
UNIT_CAP = Decimal("0.50")
PO_CAP = Decimal("1.00")


def same_supplier(invoice, order):
    return normalize(order["vendor"]) == normalize(invoice.vendor) and order["currency"] == invoice.currency


def suggest_orders(invoice, orders, used_by_po):
    skus = {reference(line.sku) for line in invoice.lines if line.sku}
    descriptions = {normalize(line.description) for line in invoice.lines if not line.sku and line.description}
    candidates = []
    for po, lines in orders.items():
        if not same_supplier(invoice, lines[0]):
            continue
        if all(used_by_po[po][line["sku"]] >= Decimal(line["quantity"]) for line in lines):
            continue
        overlap = len({reference(line["sku"]) for line in lines} & skus)
        description_matches = len({normalize(line["description"]) for line in lines} & descriptions)
        similarity = fuzz.ratio(reference(invoice.po), reference(po)) if invoice.po else 0
        if overlap or description_matches or similarity >= 75:
            candidates.append({
                "PO": po,
                "Reference similarity": round(similarity) if invoice.po else None,
                "Matching SKUs": overlap,
                "Matching descriptions": description_matches,
            })
    return sorted(candidates, key=_candidate_score, reverse=True)


def _candidate_score(candidate):
    return (candidate["Matching SKUs"], candidate["Matching descriptions"], candidate["Reference similarity"] or 0)


def compare_lines(invoice, order_lines, used):
    results = []
    consumed = defaultdict(Decimal)
    variance = Decimal("0")
    for line in invoice.lines:
        matches = [
            po_line for po_line in order_lines
            if (reference(po_line["sku"]) == reference(line.sku) if line.sku
                else normalize(po_line["description"]) == normalize(line.description))
        ]
        issues = []
        sku = ""
        before = after = po_price = difference = None
        if abs(line.quantity * line.unit_price - line.total) > CENT:
            issues.append("Quantity × price differs from line total")
        if line.quantity <= 0 or line.unit_price < 0 or line.total < 0:
            issues.append("Invalid quantity or amount")
        if len(matches) != 1:
            issues.append("No unique PO line")
        else:
            po_line = matches[0]
            sku = po_line["sku"]
            before = Decimal(po_line["quantity"]) - used[sku] - consumed[sku]
            after = before - line.quantity
            po_price = Decimal(po_line["unit_price"])
            difference = abs(line.quantity * (line.unit_price - po_price))
            variance += difference
            if after < 0:
                issues.append("Quantity exceeds remaining PO quantity")
            if abs(line.unit_price - po_price) > min(po_price * UNIT_RATE, UNIT_CAP):
                issues.append("Unit price outside tolerance")
            consumed[sku] += line.quantity
        results.append({
            "Invoice item": line.description,
            "Invoice SKU": line.sku,
            "PO SKU": sku,
            "Invoice qty": str(line.quantity),
            "Previously approved": str(used[sku]) if sku else "",
            "PO qty remaining": str(before) if before is not None else "",
            "If approved": str(after) if after is not None else "",
            "Invoice unit price": str(line.unit_price),
            "PO unit price": str(po_price) if po_price is not None else "",
            "Price variance": str(difference) if difference is not None else "",
            "Invoice line total": str(line.total),
            "Check": "; ".join(issues) if issues else "Within price tolerance" if difference else "Matched",
            "passed": not issues,
        })
    return results, consumed, variance


def history(*, scope="", db_path=None, database_url=None):
    with open_ledger(db_path=db_path, database_url=database_url, scope=scope) as ledger:
        return [
            {"run_id": run_id, "created": str(created), "filename": filename, "outcome": json.loads(outcome)}
            for run_id, created, filename, outcome in ledger.execute("""
                SELECT id, created, filename, outcome FROM ap_runs WHERE scope = ?
                ORDER BY id DESC LIMIT 100
            """, (scope,))
        ]


def save_review_advice(outcome, advice, *, scope="", db_path=None, database_url=None):
    annotated = {**outcome, "review_advice": advice}
    with open_ledger(db_path=db_path, database_url=database_url, scope=scope) as ledger:
        ledger.execute("""
            UPDATE ap_runs SET outcome = ? WHERE scope = ? AND id = ? AND status = 'Needs review'
        """, (json.dumps(annotated), scope, outcome["run_id"]))
    return annotated


def validate(invoice):
    reasons = list(invoice.issues)
    if len(invoice.number.strip().upper()) > 150:
        reasons.append("Invoice number exceeds the supported length.")
    line_sum = sum((line.total for line in invoice.lines), Decimal("0"))
    calculated_sum = sum((line.quantity * line.unit_price for line in invoice.lines), Decimal("0"))
    if abs(calculated_sum - line_sum) > CENT:
        reasons.append("The combined line amounts differ from quantity multiplied by unit price.")
    if invoice.subtotal is not None and abs(invoice.subtotal - line_sum) > CENT:
        reasons.append("The line items do not reconcile to the invoice subtotal.")
    if invoice.total is not None and (invoice.total < 0 or abs(line_sum + invoice.tax - invoice.total) > CENT):
        reasons.append("The line items and tax do not reconcile to a valid invoice total.")
    if invoice.tax < 0:
        reasons.append("Negative tax requires manual review.")
    return reasons


def _match(invoice, orders, used_by_po):
    exact = [po for po in orders if reference(po) == reference(invoice.po)]
    if len(exact) == 1:
        po = exact[0]
        if same_supplier(invoice, orders[po][0]):
            return po, "Exact reference", [], None
        return "", "Vendor or currency conflict", [], "The referenced PO belongs to a different vendor or currency."

    candidates = suggest_orders(invoice, orders, used_by_po)
    if candidates:
        tied = len(candidates) > 1 and _candidate_score(candidates[0]) == _candidate_score(candidates[1])
        method = "Fuzzy reference and item evidence" if invoice.po else "Item evidence"
        reason = ("Multiple open POs have equally strong evidence. Confirm which PO applies." if tied else
                  "Confirm the suggested PO. The invoice did not identify a unique exact match.")
        return "" if tied else candidates[0]["PO"], method, candidates, reason
    return "", "No candidate", [], "No matching open PO was found in the approved dataset."


def reconcile(invoice, filename, stage=lambda *_: None, *, scope="", db_path=None, database_url=None, approvals=None, run_id=None, orders=None):
    stage("validate", "Checking required fields and invoice arithmetic")
    reasons = validate(invoice)
    if orders is None:
        _, orders = load_catalog()
    if not any(normalize(lines[0]["vendor"]) == normalize(invoice.vendor) for lines in orders.values()):
        reasons.append("Vendor is outside the approved PO dataset.")

    if approvals is not None:
        db_path = ":memory:"
    with open_ledger(db_path=db_path, database_url=database_url, scope=scope, write=True) as ledger:
        if approvals:
            ledger.executemany("""
                INSERT INTO ap_runs (id, scope, filename, vendor, invoice, po, status, outcome, quantities, variance)
                VALUES (?, ?, '', ?, ?, ?, 'Approved', '{}', ?, ?)
            """, [
                (prior["run_id"], scope, prior["vendor"], prior["invoice"], prior["po"],
                 json.dumps(prior["quantities"]), prior["variance"])
                for prior in approvals
            ])
        stage("match", "Trying the exact reference, then comparing open purchase orders")
        used_by_po = defaultdict(lambda: defaultdict(Decimal))
        po_variances = defaultdict(Decimal)
        for po, quantities, variance in ledger.execute(
            "SELECT po, quantities, variance FROM ap_runs WHERE scope = ? AND status = 'Approved'", (scope,)
        ):
            for sku, quantity in json.loads(quantities).items():
                used_by_po[po][sku] += Decimal(quantity)
            po_variances[po] += Decimal(variance)
        order_po, match_method, candidates, match_issue = _match(invoice, orders, used_by_po)
        if match_issue:
            reasons.append(match_issue)

        stage("reconcile", "Checking prior approvals, quantities, prices and the PO tolerance budget")
        invoice_key = invoice.number.strip().upper()
        vendor_key = normalize(invoice.vendor)
        duplicate = ledger.execute("""
            SELECT id FROM ap_runs WHERE scope = ? AND vendor = ? AND invoice = ? AND status = 'Approved'
        """, (scope, vendor_key, invoice_key)).fetchone()
        lines, consumed, variance = [], {}, Decimal("0")
        if duplicate:
            reasons = [f"Invoice already approved in run #{duplicate[0]}. No PO quantities were consumed."]
        else:
            lines, consumed, variance = compare_lines(invoice, orders.get(order_po, []), used_by_po[order_po])
            for candidate in candidates[:5]:
                candidate_po = candidate["PO"]
                candidate_lines = lines if candidate_po == order_po else compare_lines(
                    invoice, orders[candidate_po], used_by_po[candidate_po]
                )[0]
                candidate["Lines passing every check"] = sum(line["passed"] for line in candidate_lines)
            if any(not row["passed"] for row in lines):
                reasons.append("One or more line items failed reconciliation. See the line-item checks.")
            if order_po and variance + po_variances[order_po] > PO_CAP:
                reasons.append("The cumulative absolute price variance would exceed the PO's 1.00 allowance.")

        status = "Duplicate" if duplicate else "Needs review" if reasons else "Approved"
        if status == "Approved":
            reasons = ["Exact PO and vendor match. Required data, invoice arithmetic and quantities passed."]
            if variance:
                reasons.append(f"Small price variance ({variance:.2f} {invoice.currency}) is within the unit and cumulative PO limits.")
            if any(used_by_po[order_po].values()):
                reasons.append("Prior approved invoices were deducted before checking this split invoice.")

        outcome = {
            "status": status, "vendor": invoice.vendor, "invoice": invoice.number, "po": order_po,
            "total": str(invoice.total) if invoice.total is not None else None, "date": invoice.date,
            "currency": invoice.currency, "reference": invoice.po, "sources": invoice.sources, "lines": lines,
            "match_method": match_method, "candidates": candidates[:5], "reasons": reasons,
            "variance": {"current": str(variance), "used": str(po_variances[order_po]), "cap": str(PO_CAP)} if order_po else None,
        }
        stage("decision", "Recording the decision and any approved quantity changes")
        quantities = {sku: str(quantity) for sku, quantity in consumed.items()} if status == "Approved" else {}
        columns = "scope, filename, vendor, invoice, po, status, outcome, quantities, variance"
        values = (scope, filename, vendor_key, invoice_key, order_po, status, json.dumps(outcome),
                  json.dumps(quantities), str(variance) if status == "Approved" else "0")
        if run_id is not None:
            columns = f"id, {columns}"
            values = (run_id, *values)
        placeholders = ", ".join("?" for _ in values)
        outcome["run_id"] = ledger.execute(
            f"INSERT INTO ap_runs ({columns}) VALUES ({placeholders}) RETURNING id", values
        ).fetchone()[0]
        if approvals is not None and status == "Approved":
            outcome["approval"] = {
                "run_id": outcome["run_id"], "vendor": vendor_key, "invoice": invoice_key,
                "po": order_po, "quantities": quantities, "variance": str(variance),
            }
    return outcome
