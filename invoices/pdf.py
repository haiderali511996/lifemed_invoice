"""Renders an invoice onto the pre-printed template.

The template is a fixed A4 form: a header block, an item table bounded by a
rule at y=240.7, and a totals block immediately beneath that rule. Text is
written at absolute coordinates because the form's own lines and labels are
part of the artwork.

Only four rows fit between the table header and the closing rule, so invoices
with more items are split across repeated copies of the template rather than
overflowing into the totals block - which used to erase the form's own labels.
"""

try:
    # PyMuPDF renamed its module to `pymupdf`; `fitz` is the pre-1.24.3 name.
    import pymupdf as fitz
except ImportError:
    import fitz

import io
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(BASE_DIR, "template.pdf")

# ----------------------------------------------------------------- GEOMETRY

HEADER_COORDS = {
    "customer_name": (125.84, 110.15, 272.87, 122.43),
    "address": (125.84, 124.65, 347.06, 134.70),
    "invoice_no": (482.60, 110.13, 524.41, 120.18),
    "date": (479.85, 120.98, 523.99, 131.03),
    "license_no": (75.06, 181.90, 173.89, 191.95),
}

NTN_VALUE = (95, 158, 200, 168)
SALES_TAX_VALUE = (110, 170, 220, 180)

# x positions of the template's own column headers, so values land under the
# right heading. Batch used to print under "Bonus" and the discount under
# "FT%" (Further Tax) - misleading on a tax document.
TABLE_COLS = {
    "sr": 54.7,          # Sr#       header 49.8
    "name": 72.1,        # Item Name header 71.8
    "qty": 208.5,        # Qty       header 204.4
    "batch": 271.0,      # Batch No  header 269.9
    "expiry": 336.0,     # Expiry    header 340.3
    "price": 393.0,      # T.Price   header 396.0
    "discount": 436.0,   # Disc%     header 434.6
    "amount": 546.0,     # Value     header 546.5
}

# Measured from the template: the column header row ends at y=206.15 and the
# rule closing the table is at y=240.7, leaving a 34.5pt band. At 8pt, a row's
# glyphs run from baseline-7.6 to baseline+2.4, so three 9.5pt rows fit inside
# that band and a fourth would cross the rule into the totals block - the exact
# overflow this pagination exists to prevent.
TABLE_HEADER_BOTTOM = 206.15
TABLE_RULE_Y = 240.7

ROW_START_Y = 215.0
ROW_HEIGHT = 9.5
ROWS_PER_PAGE = 3

# Clears the template's own sample rows without touching the column headers.
TABLE_WIPE = (44, TABLE_HEADER_BOTTOM + 0.4, 580, TABLE_RULE_Y - 0.7)

GROSS_VALUE_RECT = (535, 260, 590, 280)
DISCOUNT_VALUE_RECT = (535, 277.58, 590, 287.63)
NET_PAYABLE_RECT = (535, 320, 590, 345)
COMPANY_TOTAL_RECT = (535, 240.9, 590, 260)

TOTALS_RECTS = (
    GROSS_VALUE_RECT,
    DISCOUNT_VALUE_RECT,
    NET_PAYABLE_RECT,
    COMPANY_TOTAL_RECT,
)

PAGE_LABEL_POS = (470, 762)
CONTINUED_POS = (44, 250)

# The template is empty between Net Payable (y=327) and the warranty text
# (y=628), so the outstanding-balance breakdown goes there on the last page.
PREV_X = 44.0
PREV_TITLE_Y = 358.0
PREV_HEADER_Y = 374.0
PREV_ROW_START_Y = 387.0
PREV_ROW_HEIGHT = 10.5

PREV_COL_INVOICE = 48.0
PREV_COL_DATE = 140.0
PREV_COL_AMOUNT_RIGHT = 300.0
PREV_LABEL_RIGHT = 235.0

# Keeps the block clear of the warranty block at y=628.
PREV_MAX_ROWS = 16
PREV_BLOCK_RIGHT = 305.0


# ----------------------------------------------------------------- HELPERS

def wipe_rect(page, rect):
    page.add_redact_annot(fitz.Rect(rect), fill=(1, 1, 1))


def write_in_rect(page, rect, text, fontsize=9):
    r = fitz.Rect(rect)
    page.insert_text((r.x0 + 2, r.y1 - 2), str(text), fontsize=fontsize)


def write_in_rect_right(page, rect, text, fontsize=9):
    r = fitz.Rect(rect)
    text = str(text)

    x = r.x1 - fitz.get_text_length(text, fontsize=fontsize) - 2

    page.insert_text((x, r.y1 - 3), text, fontsize=fontsize)


def write_right(page, x_right, y, text, fontsize=8):
    text = str(text)
    x = x_right - fitz.get_text_length(text, fontsize=fontsize)

    page.insert_text((x, y), text, fontsize=fontsize)


def draw_previous_balance(page, previous):
    """List what the customer owed before this invoice, invoice by invoice.

    `previous` carries `rows` (invoice_no, date, balance), an optional
    unallocated `credit`, the `total` brought forward and the `grand_total`
    including this invoice.
    """
    y = PREV_TITLE_Y

    page.insert_text(
        (PREV_X, y), "PREVIOUS OUTSTANDING BALANCE", fontsize=8.5
    )

    page.draw_line(
        fitz.Point(PREV_X, y + 3.5),
        fitz.Point(PREV_BLOCK_RIGHT, y + 3.5),
        width=0.6,
    )

    page.insert_text((PREV_COL_INVOICE, PREV_HEADER_Y), "Invoice #", fontsize=7.5)
    page.insert_text((PREV_COL_DATE, PREV_HEADER_Y), "Date", fontsize=7.5)
    write_right(page, PREV_COL_AMOUNT_RIGHT, PREV_HEADER_Y, "Balance", 7.5)

    page.draw_line(
        fitz.Point(PREV_X, PREV_HEADER_Y + 3),
        fitz.Point(PREV_BLOCK_RIGHT, PREV_HEADER_Y + 3),
        width=0.4,
    )

    rows = previous["rows"]
    shown = rows[:PREV_MAX_ROWS]

    y = PREV_ROW_START_Y

    for row in shown:
        page.insert_text((PREV_COL_INVOICE, y), row["invoice_no"], fontsize=8)
        page.insert_text(
            (PREV_COL_DATE, y), row["date"].strftime("%d/%m/%Y"), fontsize=8
        )
        write_right(page, PREV_COL_AMOUNT_RIGHT, y, f"{row['balance']:.2f}", 8)

        y += PREV_ROW_HEIGHT

    if len(rows) > PREV_MAX_ROWS:
        page.insert_text(
            (PREV_COL_INVOICE, y),
            f"... and {len(rows) - PREV_MAX_ROWS} older invoice(s)",
            fontsize=7.5,
        )
        y += PREV_ROW_HEIGHT

    credit = previous.get("credit")

    if credit:
        page.insert_text(
            (PREV_COL_INVOICE, y), "Less: payments on account", fontsize=8
        )
        write_right(page, PREV_COL_AMOUNT_RIGHT, y, f"-{abs(credit):.2f}", 8)
        y += PREV_ROW_HEIGHT

    page.draw_line(
        fitz.Point(PREV_X, y - 6),
        fitz.Point(PREV_BLOCK_RIGHT, y - 6),
        width=0.4,
    )

    y += 2

    write_right(page, PREV_LABEL_RIGHT, y, "Total Previous Balance:", 8.5)
    write_right(page, PREV_COL_AMOUNT_RIGHT, y, f"{previous['total']:.2f}", 8.5)

    y += PREV_ROW_HEIGHT + 2

    write_right(page, PREV_LABEL_RIGHT, y, "Grand Total Payable:", 9)
    write_right(
        page, PREV_COL_AMOUNT_RIGHT, y, f"{previous['grand_total']:.2f}", 9
    )

    page.draw_line(
        fitz.Point(PREV_X, y + 3),
        fitz.Point(PREV_BLOCK_RIGHT, y + 3),
        width=0.8,
    )


def chunk_rows(rows, size=ROWS_PER_PAGE):
    """Split items into per-page groups, always yielding at least one page."""
    if not rows:
        return [[]]

    return [rows[i:i + size] for i in range(0, len(rows), size)]


# ----------------------------------------------------------------- RENDERING

def _fill_page(page, header, rows, totals, first_row_number, page_no, page_count,
               previous=None):
    """Draw one page: header on every page, totals only on the last."""

    # Clear everything the template pre-prints before writing anything, so a
    # later redaction cannot erase text written earlier in this pass.
    for rect in HEADER_COORDS.values():
        wipe_rect(page, rect)

    wipe_rect(page, NTN_VALUE)
    wipe_rect(page, SALES_TAX_VALUE)
    wipe_rect(page, TABLE_WIPE)

    for rect in TOTALS_RECTS:
        wipe_rect(page, rect)

    page.apply_redactions()

    for key, rect in HEADER_COORDS.items():
        write_in_rect(page, rect, header.get(key, ""), 9)

    write_in_rect(page, NTN_VALUE, header.get("ntn", ""), 9)
    write_in_rect(page, SALES_TAX_VALUE, header.get("sales_tax", ""), 9)

    for offset, row in enumerate(rows):
        y = ROW_START_Y + offset * ROW_HEIGHT

        page.insert_text(
            (TABLE_COLS["sr"], y), str(first_row_number + offset), fontsize=8
        )
        page.insert_text((TABLE_COLS["name"], y), row["name"], fontsize=8)
        page.insert_text((TABLE_COLS["qty"], y), str(row["qty"]), fontsize=8)
        page.insert_text((TABLE_COLS["batch"], y), row["batch"], fontsize=8)
        page.insert_text((TABLE_COLS["expiry"], y), row["expiry"], fontsize=8)
        page.insert_text(
            (TABLE_COLS["price"], y), f"{row['price']:.2f}", fontsize=8
        )
        page.insert_text(
            (TABLE_COLS["discount"], y), f"{row['discount']}%", fontsize=8
        )
        page.insert_text(
            (TABLE_COLS["amount"], y), f"{row['amount']:.2f}", fontsize=8
        )

    if totals is None:
        # Totals belong on the final page only; say so rather than leaving the
        # boxes blank and looking like a zero invoice.
        page.insert_text(
            CONTINUED_POS,
            f"Continued on page {page_no + 1} ...",
            fontsize=8,
        )
    else:
        write_in_rect_right(page, GROSS_VALUE_RECT, f"{totals['gross']:.2f}", 9)
        write_in_rect_right(
            page, DISCOUNT_VALUE_RECT, f"-{abs(totals['discount']):.2f}", 9
        )
        write_in_rect_right(page, NET_PAYABLE_RECT, f"{totals['net']:.2f}", 9)
        write_in_rect_right(page, COMPANY_TOTAL_RECT, f"{totals['net']:.2f}", 9)

        if previous:
            draw_previous_balance(page, previous)

    if page_count > 1:
        page.insert_text(
            PAGE_LABEL_POS, f"Page {page_no} of {page_count}", fontsize=8
        )


def render_invoice(header, rows, totals, previous=None):
    """Return the finished PDF as bytes.

    `rows` is a list of dicts (name, qty, batch, expiry, price, discount,
    amount); `totals` has gross, discount and net. `previous`, when given,
    adds the outstanding-balance breakdown to the final page.
    """
    template = fitz.open(PDF_PATH)
    doc = fitz.open()

    pages = chunk_rows(rows)

    for _ in pages:
        doc.insert_pdf(template, from_page=0, to_page=0)

    for index, page_rows in enumerate(pages):
        is_last = index == len(pages) - 1

        _fill_page(
            page=doc[index],
            header=header,
            rows=page_rows,
            totals=totals if is_last else None,
            first_row_number=index * ROWS_PER_PAGE + 1,
            page_no=index + 1,
            page_count=len(pages),
            previous=previous if is_last else None,
        )

    buffer = io.BytesIO()
    doc.save(buffer)
    doc.close()
    template.close()

    return buffer.getvalue()
