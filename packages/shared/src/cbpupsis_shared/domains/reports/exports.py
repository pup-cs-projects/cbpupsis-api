"""In-memory export renderers for the reports domain (CSV and PDF)."""

from __future__ import annotations

import csv
import io
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from cbpupsis_shared.domains.reports.schemas import EnrollmentReportRead


def render_csv(report: EnrollmentReportRead) -> bytes:
    """Render enrollment report numbers to CSV format."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["group", "count"])
    for row in report.rows:
        writer.writerow([row.group, row.count])
    writer.writerow(["Total", report.total])
    return output.getvalue().encode("utf-8")


def render_pdf(report: EnrollmentReportRead, term_label: str = "") -> bytes:
    """Render enrollment report numbers to a clean, printable PDF table."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#4A5568"),
    )
    cell_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
    )
    cell_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
    )

    elements = [
        Paragraph("Polytechnic University of the Philippines", title_style),
        Spacer(1, 4),
        Paragraph("Enrollment Statistics Analysis Report", subtitle_style),
    ]

    if term_label:
        elements.append(Paragraph(f"Academic Term: {term_label}", subtitle_style))

    elements.append(Spacer(1, 14))

    table_data = [[Paragraph("Group", cell_bold), Paragraph("Count", cell_bold)]]
    for row in report.rows:
        table_data.append(
            [
                Paragraph(str(row.group), cell_style),
                Paragraph(str(row.count), cell_style),
            ]
        )

    table_data.append(
        [
            Paragraph("Total", cell_bold),
            Paragraph(str(report.total), cell_bold),
        ]
    )

    table = Table(table_data, colWidths=[350, 150])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF2F7")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1A202C")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#CBD5E0")),
                ("LINEBELOW", (0, -1), (-1, -1), 1, colors.HexColor("#CBD5E0")),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#CBD5E0")),
            ]
        )
    )
    elements.append(table)
    doc.build(elements)
    return buffer.getvalue()
