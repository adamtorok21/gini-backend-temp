from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
import tempfile
import os
import re
from datetime import datetime, timedelta, timezone
from app.services.Invoice_bucket import s3_service

# Brand colours
ORANGE = colors.HexColor("#F97316")
DARK   = colors.HexColor("#1C1C1C")
MUTED  = colors.HexColor("#6B7280")
CREAM  = colors.HexColor("#FFF8F0")
WHITE  = colors.white

# GINI Bali logo (transparent PNG, ~3.8:1). Bundled in the backend so the
# invoice generator can read it at runtime on Render (frontend assets are a
# separate deployment and are not reachable here).
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "eb_logo.png")
# White variant of the logo for the dark footer bar (the dark logo is invisible
# on black). Falls back to styled text if the asset is missing.
_LOGO_WHITE_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "eb_logo_white.png")

async def generate_and_upload_invoice(order_data: dict, payment_data: dict) -> dict:
    try:
        paid_at = payment_data.get("paid_at", datetime.now())
        if isinstance(paid_at, str):
            try:
                paid_at = datetime.fromisoformat(paid_at.replace("Z", "+00:00"))
            except Exception:
                paid_at = datetime.now()

        # Display in Bali time (WITA, UTC+8). Xendit/DB timestamps are UTC — a
        # naive value is treated as UTC, a tz-aware value is converted. Without
        # this the receipt showed UTC (e.g. 07:59 instead of 15:59 / 3:59 PM).
        try:
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            paid_at = paid_at.astimezone(timezone(timedelta(hours=8)))
        except Exception:
            pass

        invoice_data = {
            "receipt_no":      f"INV-{order_data['order_number']}",
            "payment_date":    paid_at.strftime("%d %B %Y"),
            "name":            order_data.get("customer_name", "Guest"),
            "wa_number":       order_data.get("sender_id", "—"),
            "items": [
                {
                    "description": order_data.get("service_name", "Service"),
                    "amount":      order_data.get("price", 0),
                }
            ],
            "total":            order_data.get("price", 0),
            "payment_method":   payment_data.get("payment_method", "Bank Transfer"),
            "transfer_date":    paid_at.strftime("%d %B %Y"),
            "transfer_time":    f"{paid_at.strftime('%I:%M %p')} WITA",
        }

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            temp_path = tmp.name

        _draw_invoice(invoice_data, temp_path)

        object_key = (
            f"invoices/{order_data['order_number']}/"
            f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        )
        upload_result = await s3_service.upload_file_async(temp_path, object_key)
        os.unlink(temp_path)

        if upload_result["success"]:
            return {
                "success":      True,
                "download_url": upload_result["download_url"],
                "object_key":   upload_result["object_key"],
                "invoice_data": invoice_data,
            }
        return upload_result

    except Exception as e:
        print(f"Invoice generation error: {e}")
        return {"success": False, "error": str(e)}


def _fmt_idr(amount) -> str:
    try:
        return f"IDR {int(float(amount)):,}".replace(",", ".")
    except (ValueError, TypeError):
        try:
            cleaned = re.sub(r'[^\d]', '', str(amount))
            return f"IDR {int(cleaned):,}".replace(",", ".")
        except Exception:
            return str(amount)


def _draw_invoice(data: dict, path: str) -> None:
    width, height = A4          # 595 x 842 pt
    c = canvas.Canvas(path, pagesize=A4)

    # ── decorative orange left stripe (top) ───────────────────────────────────
    c.setFillColor(ORANGE)
    c.rect(0, height - 8, width, 8, stroke=0, fill=1)

    # ── header: logo image (top-left) ────────────────────────────────────────
    # Draw the real GINI Bali logo. Falls back to styled text if the asset is
    # missing so invoice generation never fails over a logo.
    _logo_w, _logo_h = 150, 40    # 3.75:1, close to the logo's native 3.8:1
    try:
        c.drawImage(
            _LOGO_PATH, 40, height - 62,
            width=_logo_w, height=_logo_h,
            preserveAspectRatio=True, anchor="sw", mask="auto",
        )
    except Exception:
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(40, height - 55, "GINI")
        c.setFillColor(ORANGE)
        c.drawString(100, height - 55, "Bali")

    # ── header: contact block (top-right) ────────────────────────────────────
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    contact_lines = [
        "+62 851 908 28581  |  +62 82 247 959 788",
        "www.ginibali.com  |  info@ginibali.com",
        "Jimbaran Hub JLoft 1-F, Jl. Karang Mas,",
        "Jimbaran, Kab. Badung, 80361",
    ]
    y_contact = height - 40
    for line in contact_lines:
        c.drawRightString(width - 40, y_contact, line)
        y_contact -= 12

    # ── divider ───────────────────────────────────────────────────────────────
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.setLineWidth(0.5)
    c.line(40, height - 90, width - 40, height - 90)

    # ── "PAYMENT RECEIPT" title (left) ────────────────────────────────────────
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 38)
    c.drawString(40, height - 150, "PAYMENT")
    c.drawString(40, height - 192, "RECEIPT")

    # ── receipt info block (right) ────────────────────────────────────────────
    info_x = 320
    info_y = height - 115
    label_w = 95

    def _info_row(label, value, y):
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(info_x, y, label)
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        c.drawString(info_x + label_w, y, str(value))

    _info_row("Receipt No:",    data["receipt_no"],   info_y)
    _info_row("Payment Date:",  data["payment_date"],  info_y - 16)

    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(info_x, info_y - 36, "Received From")

    _info_row("Name:",      data["name"],       info_y - 52)
    _info_row("WA Number:", data["wa_number"],  info_y - 68)

    # ── "Payment Details:" section ────────────────────────────────────────────
    table_top = height - 220
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, table_top, "Payment Details:")

    # table header
    th_y = table_top - 18
    c.setFillColor(DARK)
    c.rect(40, th_y - 4, width - 80, 18, stroke=0, fill=1)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(46, th_y + 1, "No")
    c.drawString(76, th_y + 1, "Description")
    c.drawRightString(width - 44, th_y + 1, "Amount")

    # item rows
    row_y = th_y - 20
    for idx, item in enumerate(data["items"], start=1):
        bg = CREAM if idx % 2 == 1 else WHITE
        c.setFillColor(bg)
        c.rect(40, row_y - 4, width - 80, 18, stroke=0, fill=1)
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        c.drawString(46, row_y + 1, str(idx))
        c.drawString(76, row_y + 1, str(item["description"]))
        c.drawRightString(width - 44, row_y + 1, _fmt_idr(item["amount"]))
        row_y -= 20

    # total row
    c.setFillColor(CREAM)
    c.rect(40, row_y - 4, width - 80, 18, stroke=0, fill=1)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(76, row_y + 1, "Total Payment")
    c.drawRightString(width - 44, row_y + 1, _fmt_idr(data["total"]))

    # table border
    table_bottom = row_y - 4
    c.setStrokeColor(colors.HexColor("#D1D5DB"))
    c.setLineWidth(0.5)
    c.rect(40, table_bottom, width - 80, table_top - 18 - table_bottom + 14, stroke=1, fill=0)

    # ── Payment Method (left) + Note (right) ──────────────────────────────────
    section_y = table_bottom - 30
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, section_y, "Payment Method:")

    c.setFont("Helvetica", 9)
    c.setFillColor(DARK)
    c.drawString(40, section_y - 18, data["payment_method"])

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(40, section_y - 36, f"Transfer Date:  {data['transfer_date']}")
    c.drawString(40, section_y - 50, f"Time:               {data['transfer_time']}")

    # note box (right side)
    note_x = width // 2 + 10
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(note_x, section_y, "Note:")

    note_lines = [
        "Payment has been received in full and is",
        "non-refundable. Please keep this receipt",
        "for your records.",
        "",
        "Thank you for your payment! We look",
        "forward to welcoming you again.",
        "#makeitGINI",
    ]
    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)
    ny = section_y - 16
    for line in note_lines:
        if line == "#makeitGINI":
            c.setFillColor(ORANGE)
            c.setFont("Helvetica-Bold", 8)
        c.drawString(note_x, ny, line)
        ny -= 12

    # ── footer bar ────────────────────────────────────────────────────────────
    footer_h = 48
    c.setFillColor(DARK)
    c.rect(0, 0, width, footer_h, stroke=0, fill=1)

    # orange accent stripe
    c.setFillColor(ORANGE)
    c.rect(0, footer_h, width, 4, stroke=0, fill=1)

    # footer logo (white image on the dark bar). Falls back to styled text if
    # the white asset is missing so the invoice never fails over a logo.
    try:
        c.drawImage(
            _LOGO_WHITE_PATH, 40, footer_h - 34,
            width=95, height=26,
            preserveAspectRatio=True, anchor="sw", mask="auto",
        )
    except Exception:
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(40, footer_h - 22, "GINI")
        c.setFillColor(ORANGE)
        c.drawString(88, footer_h - 22, "Bali")

    # right side tagline
    c.setFillColor(colors.HexColor("#9CA3AF"))
    c.setFont("Helvetica", 7)
    c.drawRightString(width - 40, footer_h - 22, "www.ginibali.com")
    c.drawRightString(width - 40, footer_h - 34, "info@ginibali.com")

    c.save()
