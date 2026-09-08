"""Customer-facing estimate PDF branded as the tuning partner."""

import datetime as dt
import html
import os
from io import BytesIO


def _money(value):
    amount = float(value or 0)
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _safe(value, fallback="-"):
    return html.escape(str(value if value not in (None, "") else fallback), quote=False)


def _partner_logo(logo_path, max_width, max_height, fallback_letter, styles, colors):
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, Paragraph, Table, TableStyle

    if logo_path and os.path.isfile(logo_path):
        try:
            width, height = ImageReader(logo_path).getSize()
            scale = min(max_width / width, max_height / height)
            return Image(logo_path, width=width * scale, height=height * scale)
        except Exception:
            # An obsolete or damaged upload must not make every estimate for
            # this partner unavailable. The branded initial is the fallback.
            pass
    fallback = Table(
        [[Paragraph(_safe((fallback_letter or "П")[:1].upper()), styles["logo_letter"])]],
        colWidths=[max_height],
        rowHeights=[max_height],
    )
    fallback.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#3498db")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#2e86c1")),
    ]))
    return fallback


def build_partner_quote_pdf(
    order,
    items,
    goods,
    partner_name,
    equipment_label,
    logo_path=None,
    unit_labels=None,
):
    """Return a polished partner-branded estimate as PDF bytes."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    unit_labels = unit_labels or {}
    ink = colors.HexColor("#0f2a35")
    ink_soft = colors.HexColor("#294c56")
    blue = colors.HexColor("#3498db")
    blue_strong = colors.HexColor("#2e86c1")
    pale_blue = colors.HexColor("#eef4fa")
    line = colors.HexColor("#d7e3ee")
    white = colors.white

    styles = {
        "brand": ParagraphStyle(
            "partner-quote-brand", fontName="OpenSans-Bold", fontSize=18,
            leading=22, textColor=ink,
        ),
        "logo_letter": ParagraphStyle(
            "partner-quote-logo-letter", fontName="OpenSans-Bold", fontSize=23,
            leading=28, textColor=white, alignment=TA_CENTER,
        ),
        "title": ParagraphStyle(
            "partner-quote-title", fontName="OpenSans-Bold", fontSize=24,
            leading=30, textColor=ink, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "partner-quote-subtitle", fontName="OpenSans", fontSize=10,
            leading=14, textColor=ink_soft,
        ),
        "section": ParagraphStyle(
            "partner-quote-section", fontName="OpenSans-Bold", fontSize=12,
            leading=16, textColor=ink, spaceAfter=8,
        ),
        "cell": ParagraphStyle(
            "partner-quote-cell", fontName="OpenSans", fontSize=9.2,
            leading=12, textColor=ink,
        ),
        "cell_right": ParagraphStyle(
            "partner-quote-cell-right", fontName="OpenSans", fontSize=9.2,
            leading=12, textColor=ink, alignment=TA_RIGHT,
        ),
        "head": ParagraphStyle(
            "partner-quote-head", fontName="OpenSans-Bold", fontSize=8.5,
            leading=11, textColor=white,
        ),
        "head_right": ParagraphStyle(
            "partner-quote-head-right", fontName="OpenSans-Bold", fontSize=8.5,
            leading=11, textColor=white, alignment=TA_RIGHT,
        ),
        "summary_label": ParagraphStyle(
            "partner-quote-summary-label", fontName="OpenSans", fontSize=9.5,
            leading=13, textColor=ink_soft,
        ),
        "summary_value": ParagraphStyle(
            "partner-quote-summary-value", fontName="OpenSans-Bold", fontSize=10,
            leading=13, textColor=ink, alignment=TA_RIGHT,
        ),
        "total_label": ParagraphStyle(
            "partner-quote-total-label", fontName="OpenSans-Bold", fontSize=12,
            leading=16, textColor=ink,
        ),
        "total_value": ParagraphStyle(
            "partner-quote-total-value", fontName="OpenSans-Bold", fontSize=15,
            leading=18, textColor=blue_strong, alignment=TA_RIGHT,
        ),
        "note": ParagraphStyle(
            "partner-quote-note", fontName="OpenSans", fontSize=8.5,
            leading=12, textColor=ink_soft,
        ),
    }

    try:
        order_date = dt.date.fromisoformat(str(order["order_date"])[:10]).strftime(
            "%d.%m.%Y"
        )
    except (TypeError, ValueError):
        order_date = str(order.get("order_date") or "-")

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=17 * mm,
        title=f"Расчёт заказа №{order['id']}",
        author=str(partner_name),
    )

    logo = _partner_logo(
        logo_path,
        34 * mm,
        25 * mm,
        partner_name,
        styles,
        colors,
    )
    brand = Table(
        [[
            logo,
            Paragraph(_safe(partner_name), styles["brand"]),
        ]],
        colWidths=[42 * mm, 130 * mm],
    )
    brand.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 8),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    flow = [
        brand,
        Spacer(1, 17),
        Table(
            [["", ""]],
            colWidths=[34 * mm, 138 * mm],
            rowHeights=[2.2 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), blue),
                ("BACKGROUND", (1, 0), (1, 0), pale_blue),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]),
        ),
        Spacer(1, 17),
        Paragraph(f"Расчёт заказа №{order['id']}", styles["title"]),
        Paragraph(
            f"Дата: {order_date}<br/>Техника: {_safe(equipment_label)}",
            styles["subtitle"],
        ),
        Spacer(1, 19),
    ]

    table_widths = [10 * mm, 86 * mm, 18 * mm, 28 * mm, 30 * mm]
    table_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink),
        ("BOX", (0, 0), (-1, -1), 0.6, line),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, line),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, pale_blue]),
    ])

    work_total = sum(float(item["partner_price"] or 0) for item in items)
    if items:
        work_rows = [[
            Paragraph("№", styles["head"]),
            Paragraph("Работа", styles["head"]),
            Paragraph("Кол-во", styles["head_right"]),
            Paragraph("Цена", styles["head_right"]),
            Paragraph("Сумма", styles["head_right"]),
        ]]
        for index, item in enumerate(items, 1):
            open_price = float(item["partner_price"] or 0)
            work_rows.append([
                str(index),
                Paragraph(_safe(item["work_name"]), styles["cell"]),
                Paragraph("1", styles["cell_right"]),
                Paragraph(_money(open_price), styles["cell_right"]),
                Paragraph(_money(open_price), styles["cell_right"]),
            ])
        work_table = Table(work_rows, colWidths=table_widths, repeatRows=1)
        work_table.setStyle(table_style)
        flow.extend([
            KeepTogether([Paragraph("Работы", styles["section"]), work_table]),
            Spacer(1, 18),
        ])

    goods_total = sum(float(row["quantity"]) * float(row["unit_price"]) for row in goods)
    if goods:
        goods_rows = [[
            Paragraph("№", styles["head"]),
            Paragraph("Товар", styles["head"]),
            Paragraph("Кол-во", styles["head_right"]),
            Paragraph("Цена", styles["head_right"]),
            Paragraph("Сумма", styles["head_right"]),
        ]]
        for index, row in enumerate(goods, 1):
            quantity = float(row["quantity"])
            unit_price = float(row["unit_price"])
            quantity_text = f"{quantity:g} {unit_labels.get(row['unit'], row['unit'])}"
            goods_rows.append([
                str(index),
                Paragraph(_safe(row["product_name"]), styles["cell"]),
                Paragraph(_safe(quantity_text), styles["cell_right"]),
                Paragraph(_money(unit_price), styles["cell_right"]),
                Paragraph(_money(quantity * unit_price), styles["cell_right"]),
            ])
        goods_table = Table(goods_rows, colWidths=table_widths, repeatRows=1)
        goods_table.setStyle(table_style)
        flow.extend([
            KeepTogether([Paragraph("Товары", styles["section"]), goods_table]),
            Spacer(1, 18),
        ])

    total = work_total + goods_total
    summary = Table(
        [
            [Paragraph("Работы", styles["summary_label"]), Paragraph(_money(work_total), styles["summary_value"])],
            [Paragraph("Товары", styles["summary_label"]), Paragraph(_money(goods_total), styles["summary_value"])],
            [Paragraph("Итого", styles["total_label"]), Paragraph(_money(total), styles["total_value"])],
        ],
        colWidths=[112 * mm, 60 * mm],
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale_blue),
        ("BOX", (0, 0), (-1, -1), 0.8, blue),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, blue),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.extend([
        summary,
        Spacer(1, 14),
        Paragraph(
            "Расчёт подготовлен по указанному перечню работ и товаров. "
            "Итоговая стоимость может измениться после диагностики и согласования объёма работ.",
            styles["note"],
        ),
    ])

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 12 * mm, A4[0] - 18 * mm, 12 * mm)
        canvas.setFont("OpenSans", 7.5)
        canvas.setFillColor(ink_soft)
        canvas.drawString(18 * mm, 7.5 * mm, str(partner_name))
        canvas.drawRightString(
            A4[0] - 18 * mm, 7.5 * mm, f"Страница {doc.page}"
        )
        canvas.restoreState()

    document.build(flow, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
