"""Legible, paginated unofficial transcripts, generated without stored files."""

from html import escape
from io import BytesIO
from pathlib import Path

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from cbpupsis_shared.domains.grading.schemas import TranscriptRead

_FONT = "TranscriptVera"
pdfmetrics.registerFont(
    TTFont(_FONT, str(Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"))
)


def transcript_pdf(record: TranscriptRead) -> bytes:
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=76,
        bottomMargin=40,
        title="Unofficial Student Transcript",
        author="PUP",
    )
    body = ParagraphStyle("RecordBody", fontName=_FONT, fontSize=8, leading=11)
    title = ParagraphStyle(
        "RecordTitle", parent=body, fontSize=13, leading=17, alignment=TA_CENTER
    )
    heading = ParagraphStyle(
        "RecordTerm",
        parent=body,
        fontSize=10,
        leading=14,
        spaceBefore=12,
    )

    def paragraph(value):
        return Paragraph(escape(str(value)), body)

    def displayed_average(value):
        return str(value) if value is not None else "Not available"

    def page_frame(canvas, document):
        canvas.saveState()
        canvas.setFont(_FONT, 11)
        canvas.setFillColor(colors.HexColor("#800000"))
        canvas.drawCentredString(
            A4[0] / 2, A4[1] - 30, "Polytechnic University of the Philippines"
        )
        canvas.setFillColor(colors.black)
        canvas.setFont(_FONT, 8)
        canvas.drawCentredString(
            A4[0] / 2, A4[1] - 46, "Unofficial Transcript - Student Copy"
        )
        canvas.drawString(36, A4[1] - 61, record.student.student_number)
        canvas.drawString(36, 23, "Not valid as an official Transcript of Records.")
        canvas.drawRightString(A4[0] - 36, 23, f"Page {document.page}")
        canvas.restoreState()

    story = [
        Paragraph("Cumulative Academic Record", title),
        Spacer(1, 12),
        paragraph(f"Student: {record.student.full_name}"),
        paragraph(f"Student number: {record.student.student_number}"),
        Spacer(1, 8),
    ]
    if not record.terms:
        story.append(paragraph("No finalized marks are available."))
    for block in record.terms:
        label = f"{block.term.academic_year} / {block.term.semester}"
        if not block.completed:
            label += " (ongoing; excluded from cumulative GWA)"
        headers = [
            paragraph(v)
            for v in ["Code", "Course title", "Units", "Mark", "Status / deadline"]
        ]
        # Repeat term labels and column headers together. The table
        # can still split naturally on its first page.
        data = [[Paragraph(escape(label), heading), "", "", "", ""], headers]
        for course in block.courses:
            status = course.status
            if course.completion_deadline:
                status += f"; due {course.completion_deadline.date().isoformat()}"
            data.append(
                [
                    paragraph(v)
                    for v in [
                        course.code,
                        course.title,
                        course.units,
                        course.final_grade,
                        status,
                    ]
                ]
            )
        table = Table(
            data,
            colWidths=[65, doc.width - 262, 35, 40, 110],
            repeatRows=2,
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#eeeeee")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 1), (-1, -1), 0.3, colors.grey),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([table, Spacer(1, 5)])
        summary = (
            f"Term GWA: {displayed_average(block.gwa)}; "
            f"term units earned: {block.units_earned}; "
            f"running cumulative GWA: {displayed_average(block.cumulative_gwa)}; "
            f"running units earned: {block.total_units_earned}"
        )
        story.append(KeepTogether([paragraph(summary), Spacer(1, 6)]))
    story.extend(
        [
            Spacer(1, 12),
            paragraph(
                "Cumulative GWA (completed terms): "
                f"{displayed_average(record.cumulative_gwa)}"
            ),
            paragraph(
                "Total units earned (unique passed courses): "
                f"{record.total_units_earned}"
            ),
        ]
    )
    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return stream.getvalue()
