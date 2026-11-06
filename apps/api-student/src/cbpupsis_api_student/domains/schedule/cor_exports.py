"""Fully buffered COR rendering from the same immutable DTO used for JSON."""

from html import escape
from io import BytesIO

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from cbpupsis_shared.domains.enrollment.client import CertificateRead


def render_pdf(certificate: CertificateRead) -> bytes:
    stream = BytesIO()
    styles = getSampleStyleSheet()
    body = ParagraphStyle("CORBody", parent=styles["BodyText"], fontSize=8, leading=10)

    def p(text):
        return Paragraph(escape(str(text), quote=False), body)

    doc = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
        title="Certificate of Registration",
        author="CBPUPSIS",
    )
    term = certificate.term
    story = [
        Paragraph("Certificate of Registration", styles["Title"]),
        p(f"{certificate.name} | {certificate.studentNumber}"),
        p(f"Program: {certificate.program}"),
        p(f"Term: {term.year} / {term.semester}"),
        p(f"Certificate: {certificate.certificateId}"),
        p(f"Issued: {certificate.issuedAt.isoformat()} | {certificate.status}"),
        Spacer(1, 12),
    ]
    rows = [[p(h) for h in ("Course / Section", "Schedule / Room", "Faculty", "Units")]]
    for course in certificate.courses:
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        meetings = "; ".join(
            f"{days[m.dayOfWeek]} {m.startTime:%H:%M}-{m.endTime:%H:%M} "
            f"{m.type} / {m.room or 'TBA'}"
            for m in course.schedule
        )
        rows.append(
            [
                p(f"{course.code}: {course.title} / {course.section}"),
                p(meetings or f"TBA / {course.room or 'TBA'}"),
                p(course.faculty or "TBA"),
                p(course.units),
            ]
        )
    table = Table(rows, colWidths=[185, 180, 120, 50], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ececec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 8),
            p(f"Total units: {certificate.totalUnits}"),
            Spacer(1, 12),
            Paragraph("Assessment (PHP)", styles["Heading2"]),
        ]
    )
    assessment = certificate.assessment
    story.extend(p(f"{line.code}: {line.amount}") for line in assessment.lines)
    story.extend(
        [
            p(f"Amount assessed: {assessment.amountAssessed}"),
            p(f"FHE subsidy: {assessment.subsidyApplied}"),
            p(f"Net payable: {assessment.netPayable}"),
            p(
                f"FHE qualified: {assessment.fheQualified} | "
                f"Forfeited: {assessment.fheForfeited}"
            ),
            p(f"Forfeiture reason: {assessment.fheForfeitureReason or 'None'}"),
            Spacer(1, 10),
        ]
    )
    qr = QrCodeWidget(certificate.verificationUrl)
    x1, y1, x2, y2 = qr.getBounds()
    drawing = Drawing(85, 85, transform=[85 / (x2 - x1), 0, 0, 85 / (y2 - y1), 0, 0])
    drawing.add(qr)
    story.extend([drawing, p(certificate.verificationUrl)])
    doc.build(story)
    return stream.getvalue()
