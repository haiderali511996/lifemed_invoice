"""Draw a payslip PDF.

Unlike invoices, there is no pre-printed form to fill in, so the whole page is
drawn here: LifeMed Pharma's logo, the employee's details, earnings and
deductions, and the net pay.
"""

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

import io
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(
    BASE_DIR, "invoices", "static", "invoices", "lifemed-logo.png"
)

PAGE_WIDTH = 595.0          # A4 portrait, points
PAGE_HEIGHT = 842.0

MARGIN = 45.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

BRAND = (0.0, 0.396, 0.678)         # #0065ad, sampled from the logo
DARK = (0.078, 0.188, 0.302)        # #14304d
MUTED = (0.522, 0.584, 0.659)
LINE = (0.85, 0.88, 0.92)

COMPANY_NAME = "LifeMed Pharma"


def _text(page, x, y, value, size=9, color=(0, 0, 0), bold=False):
    page.insert_text(
        (x, y), str(value), fontsize=size, color=color,
        fontname="hebo" if bold else "helv",
    )


def _right(page, x_right, y, value, size=9, color=(0, 0, 0), bold=False):
    value = str(value)
    font = "hebo" if bold else "helv"
    width = fitz.get_text_length(value, fontsize=size, fontname=font)

    page.insert_text(
        (x_right - width, y), value, fontsize=size, color=color, fontname=font
    )


def _rule(page, y, color=LINE, width=0.7, x0=MARGIN, x1=PAGE_WIDTH - MARGIN):
    page.draw_line(fitz.Point(x0, y), fitz.Point(x1, y), color=color, width=width)


def _header(page, payslip):
    """Logo on the left, company and period on the right."""
    y = MARGIN

    if os.path.exists(LOGO_PATH):
        # Keep the logo's aspect ratio; it is roughly 3.4:1.
        rect = fitz.Rect(MARGIN, y, MARGIN + 150, y + 44)
        page.insert_image(rect, filename=LOGO_PATH, keep_proportion=True)
    else:
        _text(page, MARGIN, y + 26, COMPANY_NAME, 17, BRAND, bold=True)

    _right(page, PAGE_WIDTH - MARGIN, y + 16, "SALARY SLIP", 15, DARK, bold=True)
    _right(
        page, PAGE_WIDTH - MARGIN, y + 32,
        payslip.run.month.strftime("%B %Y"), 10, MUTED,
    )

    _rule(page, y + 56, BRAND, 1.4)

    return y + 78


def _employee_block(page, payslip, y):
    employee = payslip.employee

    rows = [
        ("Employee", employee.full_name),
        ("Employee Code", employee.employee_code),
        ("Designation", employee.get_designation_display()),
        ("Territory", employee.territory.name if employee.territory else "—"),
    ]

    right_rows = [
        ("Pay Period", payslip.run.month.strftime("%B %Y")),
        ("Joined", employee.joined_on.strftime("%d %b %Y") if employee.joined_on else "—"),
        ("Phone", employee.phone or "—"),
        ("Status", "Active" if employee.is_active else "Inactive"),
    ]

    for index, ((left_label, left_value), (right_label, right_value)) in enumerate(
        zip(rows, right_rows)
    ):
        line_y = y + index * 15

        _text(page, MARGIN, line_y, left_label.upper(), 7, MUTED)
        _text(page, MARGIN + 78, line_y, left_value, 9, DARK)

        _text(page, MARGIN + 285, line_y, right_label.upper(), 7, MUTED)
        _text(page, MARGIN + 360, line_y, right_value, 9, DARK)

    return y + len(rows) * 15 + 12


def _amount_table(page, y, title, rows, total_label, total):
    """One column of the earnings / deductions pair."""
    _text(page, MARGIN, y, title.upper(), 8, BRAND, bold=True)
    _rule(page, y + 4)

    line_y = y + 18

    for label, amount in rows:
        _text(page, MARGIN + 4, line_y, label, 9)
        _right(page, PAGE_WIDTH - MARGIN, line_y, f"{amount:,.2f}", 9)

        line_y += 16

    _rule(page, line_y - 6)

    _text(page, MARGIN + 4, line_y + 6, total_label, 9, DARK, bold=True)
    _right(page, PAGE_WIDTH - MARGIN, line_y + 6, f"{total:,.2f}", 9, DARK, bold=True)

    return line_y + 26


def render_payslip(payslip):
    """Return the payslip as PDF bytes."""
    document = fitz.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)

    y = _header(page, payslip)
    y = _employee_block(page, payslip, y)

    earnings = [("Basic Salary", payslip.basic_salary)]

    if payslip.fuel_allowance:
        earnings.append(("Fuel Allowance", payslip.fuel_allowance))

    if payslip.mobile_allowance:
        earnings.append(("Mobile Allowance", payslip.mobile_allowance))

    if payslip.other_allowance:
        earnings.append(("Other Allowance", payslip.other_allowance))

    if payslip.expense_reimbursement:
        earnings.append(
            ("Expense Reimbursement", payslip.expense_reimbursement)
        )

    y = _amount_table(page, y, "Earnings", earnings, "Gross Pay", payslip.gross_pay)

    deductions = []

    if payslip.tax_deduction:
        deductions.append(("Income Tax", payslip.tax_deduction))

    if payslip.advance_deduction:
        deductions.append(("Advance Recovery", payslip.advance_deduction))

    if payslip.other_deduction:
        deductions.append(("Other Deductions", payslip.other_deduction))

    if not deductions:
        deductions = [("No deductions", 0)]

    y = _amount_table(
        page, y, "Deductions", deductions,
        "Total Deductions", payslip.total_deductions,
    )

    # Net pay, boxed so it is the first thing anyone looks at.
    box = fitz.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 42)
    page.draw_rect(box, color=BRAND, fill=(0.937, 0.965, 0.98), width=1.0)

    _text(page, MARGIN + 14, y + 26, "NET PAY", 11, DARK, bold=True)
    _right(page, PAGE_WIDTH - MARGIN - 14, y + 27, f"{payslip.net_pay:,.2f}", 16,
           BRAND, bold=True)

    y += 66

    if payslip.note:
        _text(page, MARGIN, y, "Note", 7, MUTED)
        _text(page, MARGIN, y + 13, payslip.note, 9)
        y += 34

    # Signature lines
    signature_y = max(y + 40, PAGE_HEIGHT - 150)

    _rule(page, signature_y, LINE, 0.7, MARGIN, MARGIN + 160)
    _text(page, MARGIN, signature_y + 12, "Employee Signature", 8, MUTED)

    _rule(page, signature_y, LINE, 0.7, PAGE_WIDTH - MARGIN - 160,
          PAGE_WIDTH - MARGIN)
    _right(page, PAGE_WIDTH - MARGIN, signature_y + 12,
           "Authorised Signature", 8, MUTED)

    footer_y = PAGE_HEIGHT - 55

    _rule(page, footer_y - 14)
    _text(
        page, MARGIN, footer_y,
        f"{COMPANY_NAME} · computer-generated payslip · "
        f"{payslip.run.month.strftime('%B %Y')}",
        7.5, MUTED,
    )

    buffer = io.BytesIO()
    document.save(buffer)
    document.close()

    return buffer.getvalue()
