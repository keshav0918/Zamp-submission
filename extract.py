"""Extract a small invoice schema from readable and scanned PDFs."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import re


@dataclass
class InvoiceLine:
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    total: Decimal


@dataclass
class Invoice:
    vendor: str = ""
    number: str = ""
    date: str = ""
    currency: str = ""
    po: str = ""
    total: Decimal | None = None
    tax: Decimal = Decimal("0")
    subtotal: Decimal | None = None
    lines: list[InvoiceLine] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
AMOUNT = rf"(?:USD|EUR|GBP|INR|[$€£₹])?\s*({NUMBER})"
LINE = re.compile(rf"^(.*?)\s+({NUMBER})\s+{AMOUNT}\s+{AMOUNT}$", re.I)
HEADER = re.compile(r"\b(?:description|item)\b.*\b(?:qty|quantity)\b.*\b(?:unit price|rate)\b.*\b(?:amount|total)\b", re.I)
SUMMARY = re.compile(r"^(?:sub\s*total|tax|vat|gst|grand total|invoice total|total(?: due)?|amount due|shipping|discount)\b", re.I)
FIELDS = {
    "vendor": r"(?:vendor(?: name)?|supplier(?: name)?|seller)",
    "number": r"invoice\s*(?:no\.?|number|#)",
    "date": r"(?:invoice\s+date|date)",
    "currency": r"currency",
    "po": r"(?:purchase order|p\.?\s*o\.?)\s*(?:no\.?|number|#|ref(?:erence)?)?",
}
TOTAL = r"(?:grand total|invoice total|amount due|total due|total)"


def decimal(value):
    return Decimal(value.replace(",", ""))


def _rows(words):
    rows = defaultdict(list)
    current = None
    for x, y, word in sorted(words, key=lambda value: value[1]):
        if current is None or y - current > 5:
            current = y
        rows[current].append((x, word))
    return [sorted(row) for row in rows.values()]


def _field_value(text, label):
    if ":" in text:
        key, _, value = text.partition(":")
        return value.strip() if re.fullmatch(label, key.strip(), re.I) else ""
    if re.fullmatch(label, text, re.I):
        return ""
    match = re.fullmatch(rf"{label}\s+(.+)", text, re.I)
    return match[1].strip() if match else ""


def extract_pages(pages):
    """Parse coordinate-bearing page words supplied by the PDF reader."""
    invoice = Invoice()
    description_x = None
    in_table = False
    for number, page in enumerate(pages, start=1):
        rows = _rows(page["words"])
        source = "Browser OCR" if page["ocr"] else "Embedded PDF text"
        invoice.sources.append(f"Page {number}: {source}")
        if page["ocr"] and page["uncertain"]:
            invoice.issues.append(f"Low OCR confidence on a numeric field on page {number}. Verify the source PDF.")
        for row in rows:
            text = " ".join(word for _, word in row)
            if HEADER.search(text):
                in_table = True
                description_x = next(
                    (x for x, word in row if word.casefold() == "description"), None
                )
                continue
            for name, label in FIELDS.items():
                value = _field_value(text, label)
                if value:
                    original = getattr(invoice, name)
                    if original and original.casefold() != value.casefold():
                        issue = f"Conflicting invoice {name} values. Verify the source PDF."
                        if issue not in invoice.issues:
                            invoice.issues.append(issue)
                    elif not original:
                        setattr(invoice, name, value)
                    break
            if SUMMARY.match(text):
                in_table = False
                for name, label in (("total", TOTAL), ("subtotal", r"sub\s*total"), ("tax", "tax|vat|gst")):
                    match = re.fullmatch(rf"(?:{label})\s*:?\s*{AMOUNT}", text, re.I)
                    if match:
                        setattr(invoice, name, decimal(match[1]))
                        break
                continue
            if not in_table:
                continue
            match = LINE.fullmatch(text)
            if not match:
                invoice.issues.append(f"Could not parse a line item on page {number}: {text[:100]}")
                continue
            label, quantity, price, total = match.groups()
            sku = ""
            if description_x is not None and row[0][0] < description_x - 12:
                sku = row[0][1]
                label = label.removeprefix(sku).strip()
            invoice.lines.append(InvoiceLine(sku, label, decimal(quantity), decimal(price), decimal(total)))
    invoice.currency = invoice.currency.upper()
    invoice.vendor = invoice.vendor.strip()
    for label, value in (("vendor", invoice.vendor), ("invoice number", invoice.number),
                         ("invoice date", invoice.date), ("currency", invoice.currency),
                         ("invoice total", invoice.total), ("line items", invoice.lines)):
        if value is None or value == "" or value == []:
            invoice.issues.append(f"Missing {label}.")
    if invoice.date:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", invoice.date):
                raise ValueError
            date.fromisoformat(invoice.date)
        except ValueError:
            invoice.issues.append("Invoice date is invalid or does not use YYYY-MM-DD.")
    if invoice.currency and not re.fullmatch(r"[A-Z]{3}", invoice.currency):
        invoice.issues.append("Currency must be a three-letter code.")
    return invoice
