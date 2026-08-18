"""Render an invoice onto a distributor's pre-printed template.

Coordinates come from the layout map detected for that distributor (see
invoices.layout), so a new distributor is added by uploading their form rather
than by editing this file.

Templates are fixed forms: the item table is a band bounded by the column
headings and the rule beneath them. Invoices with more rows than fit are split
across repeated copies of the template, because overflowing the band writes
over the form's own totals labels.
"""

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

import io
import os

from .layout import DEFAULT_FONT_SIZE, DEFAULT_ROW_HEIGHT, detect_layout

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FALLBACK_TEMPLATE = os.path.join(BASE_DIR, "template.pdf")

# Clearance kept below the last row so glyph descenders stay off the rule.
ROW_CLEARANCE = 2.5

PAGE_LABEL_OFFSET = 30.0
CONTINUED_GAP = 10.0

# Layout of the outstanding-balance block, relative to its free band.
PREV_ROW_HEIGHT = 10.5
PREV_MAX_ROWS = 16
PREV_LEFT = 44.0
PREV_WIDTH = 262.0


class TemplateError(Exception):
    """The template cannot be rendered onto."""


# ------------------------------------------------------------------ drawing

def _wipe(page, rect):
    page.add_redact_annot(fitz.Rect(rect), fill=(1, 1, 1))


def _text(page, x, y, value, size):
    page.insert_text((x, y), str(value), fontsize=size)


def _text_right(page, x_right, y, value, size):
    value = str(value)
    width = fitz.get_text_length(value, fontsize=size)

    page.insert_text((x_right - width, y), value, fontsize=size)


def _fit(value, width, size):
    """Trim a value to the width its box allows."""
    value = str(value)

    if fitz.get_text_length(value, fontsize=size) <= width:
        return value

    while value and fitz.get_text_length(value + "…", fontsize=size) > width:
        value = value[:-1]

    return value + "…" if value else ""


# ------------------------------------------------------------------ geometry

def rows_per_page(layout):
    table = layout["table"]

    height = table.get("row_height") or DEFAULT_ROW_HEIGHT
    band = table["bottom"] - table["header_bottom"] - ROW_CLEARANCE

    return max(1, int(band // height))


def row_start_y(layout):
    """Baseline of the first row.

    Sits one row height below the headings, so ascenders clear them.
    """
    table = layout["table"]
    height = table.get("row_height") or DEFAULT_ROW_HEIGHT

    return table["header_bottom"] + height


def chunk_rows(rows, size):
    if not rows:
        return [[]]

    return [rows[i:i + size] for i in range(0, len(rows), size)]


# ------------------------------------------------------------------ page fill

def _fill_page(page, layout, header, rows, totals, first_row_number,
               page_no, page_count, previous):
    table = layout["table"]
    columns = table["columns"]
    font = table.get("font_size") or DEFAULT_FONT_SIZE
    height = table.get("row_height") or DEFAULT_ROW_HEIGHT

    fields = layout.get("fields", {})
    totals_map = layout.get("totals", {})

    # Clear every pre-printed sample before writing, so a later redaction
    # cannot erase what an earlier step wrote.
    for spec in fields.values():
        _wipe(page, (spec["x"] - 1, spec["y"] - spec["size"] - 1,
                     spec["right"], spec["y"] + 2.5))

    _wipe(page, (
        table.get("wipe_left", 40),
        table["header_bottom"] + 0.4,
        table.get("wipe_right", layout["page"]["width"] - 20),
        table["bottom"] - 0.7,
    ))

    for spec in totals_map.values():
        _wipe(page, (spec["right"] - 70, spec["y"] - spec["size"] - 1,
                     spec["right"] + 2, spec["y"] + 2.5))

    page.apply_redactions()

    # ---- header, repeated on every page so a loose sheet is identifiable
    for name, spec in fields.items():
        value = header.get(name)

        if not value:
            continue

        _text(
            page, spec["x"], spec["y"],
            _fit(value, spec["right"] - spec["x"], spec["size"]),
            spec["size"],
        )

    # ---- item rows
    start_y = row_start_y(layout)

    for offset, row in enumerate(rows):
        y = start_y + offset * height

        _column(page, columns, "sr", y, first_row_number + offset, font)
        _column(page, columns, "name", y, row["name"], font, limit=110)
        _column(page, columns, "qty", y, row["qty"], font)

        # Only printed when there is one: a column of noughts down a form
        # that mostly sells without bonus is just noise.
        if row.get("bonus"):
            _column(page, columns, "bonus", y, row["bonus"], font)

        _column(page, columns, "batch", y, row["batch"], font, limit=60)
        _column(page, columns, "expiry", y, row["expiry"], font, limit=50)
        _column(page, columns, "price", y, f"{row['price']:.2f}", font)
        _column(page, columns, "discount", y, f"{row['discount']}%", font)
        _column(page, columns, "amount", y, f"{row['amount']:.2f}", font)

    # ---- totals, last page only
    if totals is None:
        _text(
            page, PREV_LEFT, table["bottom"] + CONTINUED_GAP,
            f"Continued on page {page_no + 1} ...", 8,
        )
    else:
        for name, value in (
            ("gross", totals["gross"]),
            ("discount", -abs(totals["discount"])),
            ("net", totals["net"]),
            ("company_total", totals["net"]),
        ):
            spec = totals_map.get(name)

            if spec:
                _text_right(page, spec["right"], spec["y"], f"{value:.2f}",
                            spec["size"])

        if previous:
            _draw_previous(page, layout, previous)

    if page_count > 1:
        _text(
            page,
            layout["page"]["width"] - 140,
            layout["page"]["height"] - PAGE_LABEL_OFFSET,
            f"Page {page_no} of {page_count}",
            8,
        )


def _column(page, columns, key, y, value, size, limit=None):
    spec = columns.get(key)

    if spec is None:
        return

    value = str(value)

    if limit:
        value = _fit(value, limit, size)

    if spec.get("align") == "right":
        _text_right(page, spec["x"], y, value, size)
    else:
        _text(page, spec["x"], y, value, size)


def _draw_previous(page, layout, previous):
    """Outstanding balance, itemised by invoice, in the form's free space."""
    band = layout.get("previous_balance")

    if not band:
        return

    right = PREV_LEFT + PREV_WIDTH
    y = band["top"]

    _text(page, PREV_LEFT, y, "PREVIOUS OUTSTANDING BALANCE", 8.5)
    page.draw_line(fitz.Point(PREV_LEFT, y + 3.5), fitz.Point(right, y + 3.5),
                   width=0.6)

    y += 16

    _text(page, PREV_LEFT + 4, y, "Invoice #", 7.5)
    _text(page, PREV_LEFT + 96, y, "Date", 7.5)
    _text_right(page, right, y, "Balance", 7.5)

    page.draw_line(fitz.Point(PREV_LEFT, y + 3), fitz.Point(right, y + 3),
                   width=0.4)

    y += 13

    rows = previous["rows"]
    room = max(1, int((band["bottom"] - y - 40) // PREV_ROW_HEIGHT))
    shown = rows[:min(PREV_MAX_ROWS, room)]

    for row in shown:
        _text(page, PREV_LEFT + 4, y, row["invoice_no"], 8)
        _text(page, PREV_LEFT + 96, y, row["date"].strftime("%d/%m/%Y"), 8)
        _text_right(page, right, y, f"{row['balance']:.2f}", 8)

        y += PREV_ROW_HEIGHT

    if len(rows) > len(shown):
        _text(page, PREV_LEFT + 4, y,
              f"... and {len(rows) - len(shown)} older invoice(s)", 7.5)
        y += PREV_ROW_HEIGHT

    if previous.get("credit"):
        _text(page, PREV_LEFT + 4, y, "Less: payments on account", 8)
        _text_right(page, right, y, f"-{abs(previous['credit']):.2f}", 8)
        y += PREV_ROW_HEIGHT

    page.draw_line(fitz.Point(PREV_LEFT, y - 6), fitz.Point(right, y - 6),
                   width=0.4)

    y += 2
    _text_right(page, right - 66, y, "Total Previous Balance:", 8.5)
    _text_right(page, right, y, f"{previous['total']:.2f}", 8.5)

    y += PREV_ROW_HEIGHT + 2
    _text_right(page, right - 66, y, "Grand Total Payable:", 9)
    _text_right(page, right, y, f"{previous['grand_total']:.2f}", 9)

    page.draw_line(fitz.Point(PREV_LEFT, y + 3), fitz.Point(right, y + 3),
                   width=0.8)


# ------------------------------------------------------------------ public API

def resolve_template(distributor):
    """The template path and layout to render with.

    Falls back to the bundled template so invoicing keeps working before any
    distributor has been configured.
    """
    if distributor is not None and distributor.template_path:
        path = distributor.template_path

        if not os.path.exists(path):
            raise TemplateError(
                f"{distributor.name}'s template file is missing from the server."
            )

        layout = distributor.layout

        if not layout or not layout.get("table"):
            # Never configured, or the upload predates layout detection.
            layout = detect_layout(path)

        return path, layout

    if not os.path.exists(FALLBACK_TEMPLATE):
        raise TemplateError(
            "No distributor template configured and template.pdf is missing."
        )

    return FALLBACK_TEMPLATE, detect_layout(FALLBACK_TEMPLATE)


def render_invoice(header, rows, totals, previous=None, distributor=None):
    """Return the finished PDF as bytes."""
    path, layout = resolve_template(distributor)

    template = fitz.open(path)
    document = fitz.open()

    try:
        pages = chunk_rows(rows, rows_per_page(layout))

        for _ in pages:
            document.insert_pdf(template, from_page=0, to_page=0)

        per_page = rows_per_page(layout)

        for index, page_rows in enumerate(pages):
            is_last = index == len(pages) - 1

            _fill_page(
                page=document[index],
                layout=layout,
                header=header,
                rows=page_rows,
                totals=totals if is_last else None,
                first_row_number=index * per_page + 1,
                page_no=index + 1,
                page_count=len(pages),
                previous=previous if is_last else None,
            )

        buffer = io.BytesIO()
        document.save(buffer)

        return buffer.getvalue()

    finally:
        document.close()
        template.close()
