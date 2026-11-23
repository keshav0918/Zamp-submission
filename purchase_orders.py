"""Validate uploaded purchase-order data and identify its ledger."""

from collections import defaultdict
import csv
from decimal import Decimal
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re


PO_CSV = Path(__file__).resolve().parent / "data" / "purchase_orders.csv"
COLUMNS = ("po", "vendor", "currency", "sku", "description", "quantity", "unit_price")
MAX_CSV_SIZE = 128 * 1024
MAX_ROWS = 100


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def reference(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _amount(value, column, row):
    if not re.fullmatch(r"\d{1,7}(?:\.\d{1,6})?", value):
        raise ValueError(f"Row {row}: {column} must be a non-negative number with up to six decimal places.")
    amount = Decimal(value)
    if amount > 1_000_000 or (column == "quantity" and amount == 0):
        raise ValueError(f"Row {row}: {column} is outside the supported range.")
    return format(amount.normalize(), "f")


def load_catalog(content=None):
    """Return the canonical catalog ID and a mapping from PO to ordered lines."""
    demo = content is None
    if demo:
        content = PO_CSV.read_text(encoding="utf-8-sig")
    if len(content.encode("utf-8")) > MAX_CSV_SIZE:
        raise ValueError("The PO CSV must be 128 KB or smaller.")

    orders = defaultdict(list)
    identifiers = {}
    seen_skus = set()
    try:
        reader = csv.reader(StringIO(content.removeprefix("\ufeff"), newline=""), strict=True)
        header = [column.strip().lower() for column in next(reader, [])]
        missing = [column for column in COLUMNS if column not in header]
        if missing:
            raise ValueError(f"Missing CSV columns: {', '.join(missing)}.")
        if len(set(header)) != len(header):
            raise ValueError("CSV column names must be unique.")
        positions = {key: header.index(key) for key in COLUMNS}
        for values in reader:
            row = reader.line_num
            if not any(value.strip() for value in values):
                continue
            if len(seen_skus) >= MAX_ROWS:
                raise ValueError("A PO CSV can have up to 100 line items.")
            if len(values) != len(header):
                raise ValueError(f"Row {row}: expected {len(header)} columns, found {len(values)}.")
            line = {key: values[index].strip() for key, index in positions.items()}
            for key, value in line.items():
                if not value or any(ord(character) < 32 for character in value):
                    raise ValueError(f"Row {row}: {key} must have a value on one line.")
                if len(value) > (250 if key == "description" else 150):
                    raise ValueError(f"Row {row}: {key} exceeds the supported length.")
            po = line["po"] = line["po"].upper()
            line["vendor"] = " ".join(line["vendor"].split())
            line["sku"] = line["sku"].upper()
            line["currency"] = line["currency"].upper()
            po_key = reference(po)
            sku_key = reference(line["sku"])
            if not po_key or not sku_key or not normalize(line["vendor"]):
                raise ValueError(f"Row {row}: invalid PO, vendor or SKU.")
            if not re.fullmatch(r"[A-Z]{3}", line["currency"]):
                raise ValueError(f"Row {row}: currency must be a three-letter code such as USD.")
            if identifiers.setdefault(po_key, po) != po:
                raise ValueError(f"Row {row}: PO {po} conflicts with {identifiers[po_key]}.")
            if orders[po] and (
                normalize(orders[po][0]["vendor"]) != normalize(line["vendor"])
                or orders[po][0]["currency"] != line["currency"]
            ):
                raise ValueError(f"Row {row}: all lines on {po} must use the same vendor and currency.")
            if (po_key, sku_key) in seen_skus:
                raise ValueError(f"Row {row}: SKU {line['sku']} occurs more than once on {po}.")
            for column in ("quantity", "unit_price"):
                line[column] = _amount(line[column], column, row)
            seen_skus.add((po_key, sku_key))
            orders[po].append(line)
    except csv.Error as error:
        raise ValueError(f"Invalid CSV near row {reader.line_num}: {error}.") from error

    if not orders:
        raise ValueError("The PO CSV must contain at least one line item.")
    orders = {po: sorted(lines, key=lambda item: item["sku"]) for po, lines in sorted(orders.items())}
    identity = "demo" if demo else sha256(json.dumps(orders, sort_keys=True).encode()).hexdigest()
    return identity, orders
