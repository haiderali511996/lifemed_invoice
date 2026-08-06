"""Work out where to print on an unseen invoice template.

Every distributor has their own pre-printed form. Rather than measuring
coordinates by hand for each one, this locates the labels the form already
prints - "Customer Name", "Invoice Number", the item table's column headings,
"Net Payable" - and positions each value relative to its label.

That makes the layout survive a template being redesigned, and lets a new
distributor be added by uploading their PDF.

The result is a plain dict, stored on the distributor, so a human can correct
anything the detector gets wrong without touching code.
"""

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

# Label spellings seen on Pakistani pharma invoices. Matched case-insensitively
# against runs of adjacent words, longest first so "Invoice Date" wins over
# "Date".
HEADER_ANCHORS = {
    "customer_name": [
        "customer name", "customer", "party name", "client name", "name of firm",
    ],
    "address": ["address", "customer address"],
    "invoice_no": [
        "invoice number", "invoice no", "invoice #", "bill no", "inv no",
    ],
    "date": ["invoice date", "bill date", "date"],
    "license_no": [
        "license", "licence", "drug license", "d.l. no", "lic no", "lic",
    ],
    "ntn": ["cnic / ntn", "cnic/ntn", "ntn", "cnic", "n.t.n"],
    "sales_tax": [
        "sales tax reg", "sales tax", "s.tax", "strn", "sale tax reg",
    ],
    "area": ["area", "city", "region"],
}

COLUMN_ANCHORS = {
    "sr": ["sr#", "sr.", "sr", "s.no", "s#", "no."],
    "name": [
        "item name", "product name", "description", "particulars", "item",
        "product",
    ],
    "qty": ["qty", "quantity", "qnty"],
    "bonus": ["bonus", "bns", "b.qty"],
    "batch": ["batch no", "batch#", "batch", "lot no", "lot"],
    "expiry": ["expiry", "exp date", "exp.", "exp"],
    "price": ["t.price", "trade price", "unit price", "price", "rate"],
    "discount": ["disc%", "disc %", "discount%", "disc", "discount"],
    "gst": ["gst%", "gst"],
    "ft": ["ft%", "f.t%", "ft"],
    "amount": ["value", "amount", "net value", "total"],
}

TOTAL_ANCHORS = {
    "gross": ["gross value", "gross amount", "gross total", "gross"],
    "discount": ["discount value", "less discount", "discount amount"],
    "gst": ["+ gst value", "gst value", "sales tax value"],
    "further_tax": ["+ further tax value", "further tax value", "further tax"],
    "advance_tax": ["adv.tax under section", "advance tax", "adv tax"],
    "net": ["net payable", "net amount", "net total", "grand total"],
    "company_total": ["company total", "total amount"],
}

# Columns whose values read better right-aligned under the heading.
NUMERIC_COLUMNS = {"qty", "bonus", "price", "discount", "gst", "ft", "amount"}

DEFAULT_ROW_HEIGHT = 9.5
DEFAULT_FONT_SIZE = 8
MIN_TABLE_COLUMNS = 3

# A value is written this far right of its label, when nothing else anchors it.
LABEL_GAP = 6.0

# Words closer together than this belong to the same sample value.
VALUE_RUN_GAP = 18.0

# Minimum room to leave for a value, so a short sample does not cramp it.
MIN_VALUE_WIDTH = 90.0

# Text further right than this from its label belongs to another column, not
# to this field - the form simply leaves this field blank.
MAX_VALUE_GAP = 120.0


class LayoutError(Exception):
    """The PDF does not look like an invoice template."""


# ------------------------------------------------------------------ primitives

def _words(page):
    """Words as (x0, y0, x1, y1, text), reading order."""
    return [
        (w[0], w[1], w[2], w[3], w[4])
        for w in page.get_text("words")
    ]


def _lines(words, tolerance=2.5):
    """Group words into visual lines, keyed by their shared baseline."""
    lines = []

    for word in sorted(words, key=lambda w: (w[1], w[0])):
        for line in lines:
            if abs(line["y0"] - word[1]) <= tolerance:
                line["words"].append(word)
                line["y1"] = max(line["y1"], word[3])
                break
        else:
            lines.append({"y0": word[1], "y1": word[3], "words": [word]})

    for line in lines:
        line["words"].sort(key=lambda w: w[0])

    return lines


def _find_phrase(line, phrase):
    """Locate `phrase` as consecutive words in `line`.

    Returns (x0, y0, x1, y1) of the matched run, or None. Punctuation is
    ignored so "Customer Name :" matches "customer name".
    """
    target = phrase.lower().split()
    words = line["words"]

    for start in range(len(words) - len(target) + 1):
        run = words[start:start + len(target)]

        cleaned = [
            w[4].lower().strip(":.#-,")
            for w in run
        ]
        wanted = [t.strip(":.#-,") for t in target]

        if cleaned == wanted:
            return (
                min(w[0] for w in run),
                min(w[1] for w in run),
                max(w[2] for w in run),
                max(w[3] for w in run),
            )

    return None


def _search(lines, phrases):
    """First match for any of `phrases`, longest phrase first."""
    for phrase in sorted(phrases, key=lambda p: -len(p)):
        for line in lines:
            box = _find_phrase(line, phrase)

            if box is not None:
                return box, line

    return None, None


def _horizontal_rules(page):
    """y positions of full-width horizontal lines, which bound the table."""
    rules = []

    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]

                if abs(a.y - b.y) < 0.8 and abs(a.x - b.x) > 150:
                    rules.append(round(a.y, 2))

            elif item[0] == "re":
                rect = item[1]

                if rect.width > 150 and rect.height < 2.5:
                    rules.append(round(rect.y0, 2))

    return sorted(set(rules))


# ------------------------------------------------------------------ detection

def _detect_header_fields(lines, page_width):
    """Place each header value just right of its printed label."""
    fields = {}

    for field, phrases in HEADER_ANCHORS.items():
        box, line = _search(lines, phrases)

        if box is None:
            continue

        label_right = box[2]

        following = [w for w in line["words"] if w[0] > label_right + 1]

        # A colon after the caption is its own word; it is punctuation, not a
        # value.
        while following and following[0][4].strip() in {":", "-", "=", "."}:
            following.pop(0)

        x = label_right + LABEL_GAP
        right = page_width - 20

        if not following:
            right = min(x + MIN_VALUE_WIDTH, page_width - 20)

        else:
            gap = following[0][0] - label_right

            run = [following[0]]

            for previous, nxt in zip(following, following[1:]):
                if nxt[0] - previous[2] <= VALUE_RUN_GAP:
                    run.append(nxt)
                else:
                    break

            # A run containing a colon is the next column's caption.
            is_caption = any(w[4].rstrip().endswith(":") for w in run)

            if gap > MAX_VALUE_GAP or is_caption:
                # This field is blank on the template, and what follows belongs
                # to another column. Sit just right of the label instead.
                right = min(x + MIN_VALUE_WIDTH + 50, following[0][0] - 4)
            else:
                # Templates usually ship filled in, and the sample sits exactly
                # where the real value belongs.
                x = run[0][0]
                right = run[-1][2]

                after = [w[0] for w in following if w[0] > right + 1]
                limit = min(after) - 2 if after else page_width - 20

                right = max(right, min(x + MIN_VALUE_WIDTH, limit))

        if right <= x + 4:
            right = x + MIN_VALUE_WIDTH

        fields[field] = {
            "x": round(x, 2),
            "y": round(box[3], 2),
            "right": round(right, 2),
            "size": 9,
        }

    return fields


def _detect_table(lines, rules, page_height):
    """Find the item table: its column x positions and its vertical band."""
    best = None

    for line in lines:
        matched = {}

        for column, phrases in COLUMN_ANCHORS.items():
            for phrase in sorted(phrases, key=lambda p: -len(p)):
                box = _find_phrase(line, phrase)

                if box is not None:
                    matched[column] = box
                    break

        if len(matched) >= MIN_TABLE_COLUMNS:
            if best is None or len(matched) > len(best[1]):
                best = (line, matched)

    if best is None:
        raise LayoutError(
            "No item table found - expected a row of column headings such as "
            "'Item Name', 'Qty', 'Batch', 'Value'."
        )

    header_line, matched = best

    header_bottom = header_line["y1"]

    # The table ends at the first rule below the headings.
    below = [y for y in rules if y > header_bottom + 4]
    table_bottom = below[0] if below else header_bottom + 60

    columns = {}

    for column, box in matched.items():
        if column in NUMERIC_COLUMNS:
            # Right-align numbers on the heading's right edge.
            columns[column] = {"x": round(box[2], 2), "align": "right"}
        else:
            columns[column] = {"x": round(box[0], 2), "align": "left"}

    return {
        "columns": columns,
        "header_bottom": round(header_bottom, 2),
        "bottom": round(table_bottom, 2),
        "row_height": DEFAULT_ROW_HEIGHT,
        "font_size": DEFAULT_FONT_SIZE,
    }


def _detect_totals(lines, page_width):
    """Right-align each total against the page's right margin."""
    totals = {}

    for field, phrases in TOTAL_ANCHORS.items():
        box, line = _search(lines, phrases)

        if box is None:
            continue

        # If the template prints a sample figure on this line, align to it.
        sample = [
            w for w in line["words"]
            if w[0] > box[2] and any(ch.isdigit() for ch in w[4])
        ]

        right = max(w[2] for w in sample) if sample else page_width - 22

        totals[field] = {
            "right": round(right, 2),
            "y": round(box[3], 2),
            "size": 9,
        }

    return totals


def _free_band(lines, table_bottom, page_height):
    """The largest empty vertical gap below the table.

    Used for the outstanding-balance breakdown, which has no label to anchor
    to because the form was never designed to carry one.
    """
    ys = sorted({round(line["y0"], 1) for line in lines if line["y0"] > table_bottom})

    best = (0.0, None)

    for a, b in zip(ys, ys[1:]):
        if b - a > best[0]:
            best = (b - a, (a, b))

    if best[1] is None:
        return None

    top, bottom = best[1]

    return {"top": round(top + 22, 2), "bottom": round(bottom - 12, 2)}


# ------------------------------------------------------------------ public API

def detect_layout(pdf_path):
    """Return a coordinate map for a blank (or sample-filled) template.

    Raises LayoutError when the file does not look like an invoice.
    """
    document = fitz.open(pdf_path)

    try:
        if document.page_count == 0:
            raise LayoutError("The PDF has no pages.")

        page = document[0]
        words = _words(page)

        if not words:
            raise LayoutError(
                "No text found. The template looks like a scanned image; a "
                "text-based PDF is required."
            )

        lines = _lines(words)
        rules = _horizontal_rules(page)

        table = _detect_table(lines, rules, page.rect.height)

        layout = {
            "page": {
                "width": round(page.rect.width, 2),
                "height": round(page.rect.height, 2),
            },
            "fields": _detect_header_fields(lines, page.rect.width),
            "table": table,
            "totals": _detect_totals(lines, page.rect.width),
            "previous_balance": _free_band(
                lines, table["bottom"], page.rect.height
            ),
        }

    finally:
        document.close()

    return layout


def describe(layout):
    """Human-readable summary, for the confirmation screen after upload."""
    fields = sorted(layout.get("fields", {}))
    columns = sorted(layout.get("table", {}).get("columns", {}))
    totals = sorted(layout.get("totals", {}))

    return {
        "fields": fields,
        "columns": columns,
        "totals": totals,
        "rows_per_page": rows_per_page(layout),
        "missing": [f for f in HEADER_ANCHORS if f not in fields],
    }


def rows_per_page(layout):
    """How many item rows fit between the column headings and the closing rule."""
    table = layout["table"]

    band = table["bottom"] - table["header_bottom"]
    height = table.get("row_height") or DEFAULT_ROW_HEIGHT

    # One row height of clearance keeps glyph descenders off the rule.
    return max(1, int(band // height) - 1 + 1) if band >= height else 1
