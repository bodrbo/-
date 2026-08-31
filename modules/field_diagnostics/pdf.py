"""PDF rendering for completed field diagnostic sheets."""

import html
from io import BytesIO
import os


_FONTS_REGISTERED = False


def _register_fonts(fonts_dir):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(
        TTFont("FieldOpenSans", os.path.join(fonts_dir, "open-sans-400.ttf"))
    )
    pdfmetrics.registerFont(
        TTFont("FieldOpenSans-Bold", os.path.join(fonts_dir, "open-sans-700.ttf"))
    )
    _FONTS_REGISTERED = True


def _value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        value = default
    return default if value is None else value


def build_diagnostic_pdf(sheet, answers, inspection_label, fonts_dir):
    """Return a print-ready A4 diagnostic report as bytes."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
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

    _register_fonts(fonts_dir)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="Диагностический лист",
        author="Бодрый Боцман",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    navy = colors.HexColor("#173847")
    brass = colors.HexColor("#B68B4C")
    green = colors.HexColor("#2F6B4F")
    red = colors.HexColor("#A43D2C")
    pale_green = colors.HexColor("#EAF3EE")
    pale_red = colors.HexColor("#F7EAE6")
    hairline = colors.HexColor("#D7D0C4")
    muted = colors.HexColor("#667780")

    title_style = ParagraphStyle(
        "FieldTitle",
        fontName="FieldOpenSans-Bold",
        fontSize=22,
        leading=27,
        textColor=navy,
        alignment=TA_CENTER,
        spaceAfter=5 * mm,
    )
    sheet_number_style = ParagraphStyle(
        "FieldNumber",
        fontName="FieldOpenSans",
        fontSize=8,
        leading=11,
        textColor=muted,
        alignment=TA_RIGHT,
    )
    label_style = ParagraphStyle(
        "FieldLabel", fontName="FieldOpenSans", fontSize=8, leading=11, textColor=muted
    )
    value_style = ParagraphStyle(
        "FieldValue", fontName="FieldOpenSans-Bold", fontSize=10, leading=14, textColor=navy
    )
    section_style = ParagraphStyle(
        "FieldSection",
        fontName="FieldOpenSans-Bold",
        fontSize=13,
        leading=17,
        textColor=navy,
        spaceBefore=5 * mm,
        spaceAfter=2.5 * mm,
    )
    item_title_style = ParagraphStyle(
        "FieldItemTitle",
        fontName="FieldOpenSans-Bold",
        fontSize=9.5,
        leading=13,
        textColor=navy,
    )
    item_text_style = ParagraphStyle(
        "FieldItemText",
        fontName="FieldOpenSans",
        fontSize=8.5,
        leading=12,
        textColor=navy,
    )
    status_ok_style = ParagraphStyle(
        "FieldOk",
        fontName="FieldOpenSans-Bold",
        fontSize=8.5,
        leading=12,
        textColor=green,
    )
    status_problem_style = ParagraphStyle(
        "FieldProblem",
        fontName="FieldOpenSans-Bold",
        fontSize=8.5,
        leading=12,
        textColor=red,
    )

    safe = lambda value: html.escape(str(value or ""))
    completed_at = _value(sheet, "completed_at") or _value(sheet, "started_at")
    display_date = str(completed_at)[:16]
    ok_answers = [answer for answer in answers if _value(answer, "status") == "ok"]
    problems = [answer for answer in answers if _value(answer, "status") == "problem"]

    story = [
        Paragraph("ДИАГНОСТИЧЕСКИЙ ЛИСТ", title_style),
        Paragraph("Лист № %s" % safe(_value(sheet, "id")), sheet_number_style),
    ]
    metadata = [
        [Paragraph("Модель лодки", label_style), Paragraph("Судовладелец", label_style)],
        [Paragraph(safe(_value(sheet, "boat_model")), value_style), Paragraph(safe(_value(sheet, "owner_name")), value_style)],
        [Paragraph("Тип осмотра", label_style), Paragraph("Телефон", label_style)],
        [Paragraph(safe(inspection_label), value_style), Paragraph(safe(_value(sheet, "owner_phone")), value_style)],
        [Paragraph("Дата завершения", label_style), Paragraph("Результат", label_style)],
        [
            Paragraph(safe(display_date), value_style),
            Paragraph("Исправно: %d · Неисправности: %d" % (len(ok_answers), len(problems)), value_style),
        ],
    ]
    metadata_table = Table(metadata, colWidths=[86 * mm, 86 * mm])
    metadata_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F6F3ED")),
                ("BOX", (0, 0), (-1, -1), 0.6, hairline),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, hairline),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.extend([metadata_table, Spacer(1, 3 * mm)])

    story.append(Paragraph("Исправные узлы и системы", section_style))
    if ok_answers:
        ok_rows = []
        for index, answer in enumerate(ok_answers, start=1):
            title = safe(_value(answer, "question_title"))
            text = safe(_value(answer, "question_text"))
            ok_rows.append(
                [
                    Paragraph(str(index), status_ok_style),
                    Paragraph("<b>%s</b><br/>%s" % (title, text), item_text_style),
                    Paragraph("ИСПРАВНО", status_ok_style),
                ]
            )
        ok_table = Table(ok_rows, colWidths=[9 * mm, 130 * mm, 33 * mm], repeatRows=0)
        ok_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), pale_green),
                    ("BOX", (0, 0), (-1, -1), 0.5, hairline),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, hairline),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(ok_table)
    else:
        story.append(Paragraph("Исправные пункты не отмечены.", item_text_style))

    if problems:
        problem_blocks = []
        for index, answer in enumerate(problems, start=1):
            problem_table = Table(
                [
                    [
                        Paragraph("НЕИСПРАВНОСТЬ %d" % index, status_problem_style),
                        Paragraph(safe(_value(answer, "section_name")), label_style),
                    ],
                    [
                        Paragraph(safe(_value(answer, "question_title")), item_title_style),
                        Paragraph(safe(_value(answer, "comment") or "Описание не указано"), item_text_style),
                    ],
                ],
                colWidths=[58 * mm, 114 * mm],
            )
            problem_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), pale_red),
                        ("BOX", (0, 0), (-1, -1), 0.6, red),
                        ("INNERGRID", (0, 0), (-1, -1), 0.35, hairline),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                )
            )
            problem_blocks.append(problem_table)
        # Keep the section heading with the first defect. Otherwise a nearly
        # full first page can leave a lonely heading at its bottom while all
        # useful content starts on the next page.
        story.append(
            KeepTogether(
                [
                    Paragraph("Обнаруженные неисправности", section_style),
                    problem_blocks[0],
                    Spacer(1, 2.5 * mm),
                ]
            )
        )
        for problem_table in problem_blocks[1:]:
            story.extend([KeepTogether(problem_table), Spacer(1, 2.5 * mm)])
    else:
        story.append(Paragraph("Обнаруженные неисправности", section_style))
        story.append(Paragraph("По результатам осмотра неисправности не обнаружены.", item_text_style))

    story.extend(
        [
            Spacer(1, 9 * mm),
            Table(
                [["Специалист ____________________", "Судовладелец ____________________"]],
                colWidths=[86 * mm, 86 * mm],
                style=TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, -1), "FieldOpenSans"),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("TEXTCOLOR", (0, 0), (-1, -1), navy),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ]
                ),
            ),
        ]
    )

    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(brass)
        canvas.setLineWidth(0.7)
        canvas.line(18 * mm, 12 * mm, A4[0] - 18 * mm, 12 * mm)
        canvas.setFont("FieldOpenSans", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm, 8 * mm, "Бодрый Боцман · выездная диагностика")
        canvas.drawRightString(A4[0] - 18 * mm, 8 * mm, "Страница %d" % doc.page)
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
