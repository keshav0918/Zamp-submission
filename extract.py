"""Extract a small invoice schema from readable and scanned PDFs."""
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO
import re

import pymupdf
import pytesseract
from PIL import Image


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


def _ocr(page):
    pixmap = page.get_pixmap(dpi=300)
    with Image.open(BytesIO(pixmap.tobytes("png"))) as image:
        result = pytesseract.image_to_data(
            image, config="--psm 6", output_type=pytesseract.Output.DICT, timeout=20
        )
    scale = 72 / 300
    words = []
    uncertain_numbers = False
    for word, x, y, height, confidence in zip(
        result["text"], result["left"], result["top"], result["height"], result["conf"]
    ):
        if word.strip():
            words.append((x * scale, (y + height / 2) * scale, word.strip()))
            if re.search(r"[0-9]", word) and float(confidence) < 60:
                uncertain_numbers = True
    return words, uncertain_numbers


def _rows(words):
    rows = defaultdict(list)
    current = None
    for x, y, word in sorted(words, key=lambda value: value[1]):
        if current is None or y - current > 5:
            current = y
        rows[current].append((x, word))
    return [sorted(row) for row in rows.values()]


def _read_rows(page):
    native = [
        (x, (top + bottom) / 2, word)
        for x, top, _, bottom, word, *_ in page.get_text("words")
    ]
    if sum(len(word) for _, _, word in native) >= 35:
        return _rows(native), "Embedded PDF text", False
    words, uncertain_numbers = _ocr(page)
    return _rows(words), "Tesseract OCR", uncertain_numbers


def extract_invoice(content):
    invoice = Invoice()
    description_x = None
    in_table = False
    with pymupdf.open(stream=content, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("This PDF is password-protected.")
        if not 1 <= len(document) <= 10:
            raise ValueError("Upload an invoice between 1 and 10 pages.")
        for page in document:
            rows, source, uncertain_numbers = _read_rows(page)
            invoice.sources.append(f"Page {page.number + 1}: {source}")
            if uncertain_numbers:
                invoice.issues.append(f"Low OCR confidence on a numeric field on page {page.number + 1}. Verify the source PDF.")
            for row in rows:
                text = " ".join(word for _, word in row)
                if HEADER.search(text):
                    in_table = True
                    description_x = next(
                        (x for x, word in row if word.casefold() == "description"), None
                    )
                    continue
                for name, label in FIELDS.items():
                    match = re.fullmatch(rf"{label}\s*(?::\s*|\s+)(.+)", text, re.I)
                    if match and not getattr(invoice, name):
                        setattr(invoice, name, match[1].strip())
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
                    invoice.issues.append(f"Could not parse a line item on page {page.number + 1}: {text[:100]}")
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
    return invoice
