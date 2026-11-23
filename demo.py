"""Generate fictional invoice PDFs for the live demo."""
from decimal import Decimal

import pymupdf


SAMPLES = {
    "01-readable-match.pdf": ("NS-1001", "PO-1023", 5, 4, False),
    "02-scanned-mistyped-po.pdf": ("NS-1002", "PO-1032", 1, 1, True),
    "03-line-quantity-mismatch.pdf": ("NS-1003", "PO-1023", 11, 1, False),
}


def make_invoice(number, po, paper, toner, scanned):
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_text((48, 65), "INVOICE", fontsize=28)
        for y, text in enumerate((
            "Vendor: Northstar Supply", f"Invoice Number: {number}",
            "Invoice Date: 2026-11-23", f"PO Reference: {po}", "Currency: USD",
        ), start=5):
            page.insert_text((48, 20 * y), text, fontsize=12)
        columns = (48, 148, 337, 392, 490)
        for x, label in zip(columns, ("SKU", "Description", "Qty", "Unit Price", "Amount")):
            page.insert_text((x, 224), label, fontsize=12)
        page.draw_line((48, 230), (570, 230))
        subtotal = Decimal("0")
        for y, sku, label, quantity, price in (
            (255, "PPR-01", "Copy paper case", paper, Decimal("50.00")),
            (282, "TNR-02", "Black toner cartridge", toner, Decimal("100.00")),
        ):
            amount = quantity * price
            subtotal += amount
            for x, value in zip(columns, (sku, label, str(quantity), f"{price:.2f}", f"{amount:.2f}")):
                page.insert_text((x, y), value, fontsize=12)
        page.insert_text((392, 330), f"Subtotal: {subtotal:.2f}", fontsize=12)
        page.insert_text((392, 355), "Tax: 0.00", fontsize=12)
        page.insert_text((392, 380), f"Grand Total: {subtotal:.2f}", fontsize=12)
        if scanned:
            image = page.get_pixmap(dpi=180)
            with pymupdf.open() as scan:
                scan_page = scan.new_page(width=612, height=792)
                scan_page.insert_image(scan_page.rect, pixmap=image)
                return scan.tobytes(deflate=True)
        return document.tobytes(deflate=True)
