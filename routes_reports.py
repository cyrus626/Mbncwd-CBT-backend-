import csv
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from auth import require_admin
from database import get_db
from models import Attempt, AttemptStatus, Course, Role, Test, User
from routes_participant import _now, _percentage, _settle_if_expired, _utc
from schemas import (
    NotTakenOut,
    ResultRowOut,
    ResultsSummary,
    TestResultsOut,
)

router = APIRouter(prefix="/admin", tags=["admin reports"], dependencies=[Depends(require_admin)])

DB = Annotated[Session, Depends(get_db)]

STATUS_LABELS = {
    AttemptStatus.SUBMITTED: "Submitted",
    AttemptStatus.TIMED_OUT: "Timed out",
    AttemptStatus.IN_PROGRESS: "In progress",
}

HEADERS = [
    "Name",
    "Email",
    "Status",
    "Score",
    "Total marks",
    "Percentage",
    "Result",
    "Started (UTC)",
    "Submitted (UTC)",
    "Time taken (min)",
]


# ---------- Building the report ----------

def _build_results(db: Session, test: Test) -> TestResultsOut:
    attempts = db.scalars(
        select(Attempt).where(Attempt.test_id == test.id).options(selectinload(Attempt.user))
    ).all()

    # Anyone whose time already ran out gets graded now, so the report is up to date
    for attempt in attempts:
        _settle_if_expired(db, attempt)

    rows: list[ResultRowOut] = []
    for a in attempts:
        finished = a.status != AttemptStatus.IN_PROGRESS
        percentage = _percentage(a.score, a.total_marks) if finished else None
        rows.append(
            ResultRowOut(
                attempt_id=a.id,
                participant_id=a.user_id,
                name=a.user.name,
                email=a.user.email,
                status=a.status,
                score=a.score if finished else None,
                total_marks=a.total_marks,
                percentage=percentage,
                passed=(percentage >= test.pass_mark) if finished else None,
                started_at=_utc(a.started_at),
                submitted_at=_utc(a.submitted_at),
                time_taken_minutes=(
                    round((_utc(a.submitted_at) - _utc(a.started_at)).total_seconds() / 60, 1) if finished else None
                ),
            )
        )
    # Finished first, best score at the top; people still writing go last
    rows.sort(key=lambda r: (r.percentage is None, -(r.percentage or 0), r.name.lower()))

    course_users = db.scalars(
        select(User).where(User.course_id == test.course_id, User.role == Role.PARTICIPANT, User.is_active)
    ).all()
    attempted_ids = {a.user_id for a in attempts}
    not_taken = sorted(
        (NotTakenOut(participant_id=u.id, name=u.name, email=u.email) for u in course_users if u.id not in attempted_ids),
        key=lambda n: n.name.lower(),
    )

    finished_rows = [r for r in rows if r.percentage is not None]
    percentages = [r.percentage for r in finished_rows]
    passed = sum(1 for r in finished_rows if r.passed)

    summary = ResultsSummary(
        course_participants=len(course_users),
        taken=len(finished_rows),
        in_progress=len(rows) - len(finished_rows),
        not_started=len(not_taken),
        average_percentage=round(sum(percentages) / len(percentages), 1) if percentages else None,
        highest_percentage=max(percentages) if percentages else None,
        lowest_percentage=min(percentages) if percentages else None,
        passed=passed,
        failed=len(finished_rows) - passed,
        pass_rate=round(passed * 100 / len(finished_rows), 1) if finished_rows else None,
    )

    return TestResultsOut(
        test_id=test.id,
        test_title=test.title,
        course_name=db.get(Course, test.course_id).name,
        pass_mark=test.pass_mark,
        num_questions=test.num_questions,
        generated_at=_now(),
        summary=summary,
        results=rows,
        not_taken=not_taken,
    )


# ---------- Export helpers ----------

def _safe(value):
    """Names are typed by participants. A name like =HYPERLINK(...) would run as a
    formula when the admin opens the file, so text starting with those characters
    gets a leading apostrophe."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if dt else None  # Excel can't store time zones


def _row_values(r) -> list:
    if r.passed is None:
        result = ""
    else:
        result = "Pass" if r.passed else "Fail"
    return [
        _safe(r.name),
        _safe(r.email),
        STATUS_LABELS[r.status],
        r.score,
        r.total_marks,
        r.percentage,
        result,
        _naive(r.started_at),
        _naive(r.submitted_at),
        r.time_taken_minutes,
    ]


def _csv_bytes(report) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    for r in report.results:
        writer.writerow(
            [v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else ("" if v is None else v) for v in _row_values(r)]
        )
    return buffer.getvalue().encode("utf-8-sig")  # the BOM makes Excel read accents correctly


def _xlsx_bytes(report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="305496")

    def style_header(ws, widths):
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
        for column, width in zip("ABCDEFGHIJ", widths):
            ws.column_dimensions[column].width = width
        ws.freeze_panes = "A2"

    wb = Workbook()

    # Sheet 1: one row per participant who started the test
    ws = wb.active
    ws.title = "Results"
    ws.append(HEADERS)
    for r in report.results:
        ws.append(_row_values(r))
    for row in ws.iter_rows(min_row=2, min_col=8, max_col=9):
        for cell in row:
            cell.number_format = "yyyy-mm-dd hh:mm"
    style_header(ws, [26, 32, 13, 8, 12, 12, 8, 18, 18, 16])
    ws.auto_filter.ref = ws.dimensions

    # Sheet 2: the headline numbers
    s = report.summary
    summary_ws = wb.create_sheet("Summary")
    summary_ws.append(["Item", "Value"])
    for label, value in [
        ("Test", _safe(report.test_title)),
        ("Course", _safe(report.course_name)),
        ("Pass mark (%)", report.pass_mark),
        ("Questions per participant", report.num_questions),
        ("Participants in course", s.course_participants),
        ("Finished the test", s.taken),
        ("Still in progress", s.in_progress),
        ("Not started", s.not_started),
        ("Average score (%)", s.average_percentage),
        ("Highest score (%)", s.highest_percentage),
        ("Lowest score (%)", s.lowest_percentage),
        ("Passed", s.passed),
        ("Failed", s.failed),
        ("Pass rate (%)", s.pass_rate),
        ("Report generated (UTC)", _naive(report.generated_at)),
    ]:
        summary_ws.append([label, value])
    style_header(summary_ws, [28, 34])

    # Sheet 3: registered in the course but never started
    missing_ws = wb.create_sheet("Not taken")
    missing_ws.append(["Name", "Email"])
    for n in report.not_taken:
        missing_ws.append([_safe(n.name), _safe(n.email)])
    style_header(missing_ws, [26, 32])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


FONT_DIR = Path(__file__).parent / "fonts"


def _pdf_fonts() -> tuple[str, str]:
    """Names with dots or marks (Yoruba, Igbo, French...) need a Unicode font. The standard
    PDF fonts show boxes for them, so we use DejaVu Sans from the fonts/ folder."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular, bold = FONT_DIR / "DejaVuSans.ttf", FONT_DIR / "DejaVuSans-Bold.ttf"
    if regular.exists() and bold.exists():
        if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
        return "DejaVuSans", "DejaVuSans-Bold"
    return "Helvetica", "Helvetica-Bold"


def _pdf_bytes(report) -> bytes:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font, bold = _pdf_fonts()
    page_width, _ = landscape(A4)
    margin = 15 * mm

    title_style = ParagraphStyle("title", fontName=bold, fontSize=16, leading=20)
    meta_style = ParagraphStyle("meta", fontName=font, fontSize=9, leading=12, textColor=colors.HexColor("#555555"))
    heading_style = ParagraphStyle("heading", fontName=bold, fontSize=11, leading=14, spaceBefore=10, spaceAfter=4)
    cell_style = ParagraphStyle("cell", fontName=font, fontSize=8.5, leading=10.5)
    head_cell_style = ParagraphStyle("headcell", parent=cell_style, fontName=bold, textColor=colors.white)

    def P(value, style=cell_style):
        return Paragraph(escape("" if value is None else str(value)), style)  # escape: names can contain < or &

    def fmt(value, suffix=""):
        return "-" if value is None else f"{value}{suffix}"

    header_fill = colors.HexColor("#305496")
    stripe = colors.HexColor("#F2F5FA")
    grid = colors.HexColor("#C9D1E0")

    def base_style(extra=()):
        return TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), header_fill),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, stripe]),
                ("GRID", (0, 0), (-1, -1), 0.25, grid),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                *extra,
            ]
        )

    s = report.summary
    elements = [
        Paragraph(escape(report.test_title), title_style),
        Spacer(1, 2 * mm),
        P(
            f"Course: {report.course_name}   |   Pass mark: {report.pass_mark}%   |   "
            f"Questions per participant: {report.num_questions}   |   "
            f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M')} UTC",
            meta_style,
        ),
        Spacer(1, 5 * mm),
    ]

    # Key figures
    labels = ["In course", "Finished", "In progress", "Not started", "Average", "Highest", "Lowest", "Passed", "Failed", "Pass rate"]
    values = [
        s.course_participants, s.taken, s.in_progress, s.not_started,
        fmt(s.average_percentage, "%"), fmt(s.highest_percentage, "%"), fmt(s.lowest_percentage, "%"),
        s.passed, s.failed, fmt(s.pass_rate, "%"),
    ]
    figures = Table(
        [[P(l, head_cell_style) for l in labels], [P(v) for v in values]],
        colWidths=[(page_width - 2 * margin) / len(labels)] * len(labels),
    )
    figures.setStyle(base_style())
    elements += [figures, Spacer(1, 4 * mm), Paragraph("Participants", heading_style)]

    # Results table
    if report.results:
        rows = [[P(h, head_cell_style) for h in ["#", "Name", "Email", "Status", "Score", "Percentage", "Result", "Time (min)"]]]
        pass_style = ParagraphStyle("pass", parent=cell_style, fontName=bold, textColor=colors.HexColor("#1E7B34"))
        fail_style = ParagraphStyle("fail", parent=cell_style, fontName=bold, textColor=colors.HexColor("#B3261E"))
        for i, r in enumerate(report.results, start=1):
            if r.passed is None:
                result_cell = P("-")
            else:
                result_cell = P("Pass", pass_style) if r.passed else P("Fail", fail_style)
            rows.append(
                [
                    P(i),
                    P(r.name),
                    P(r.email),
                    P(STATUS_LABELS[r.status]),
                    P(fmt(r.score) if r.score is None else f"{r.score}/{r.total_marks}"),
                    P(fmt(r.percentage, "%")),
                    result_cell,
                    P(fmt(r.time_taken_minutes)),
                ]
            )
        table = Table(rows, colWidths=[10 * mm, 64 * mm, 76 * mm, 26 * mm, 26 * mm, 26 * mm, 18 * mm, 21 * mm], repeatRows=1)
        table.setStyle(base_style())
        elements.append(table)
    else:
        elements.append(P("No participant has started this test yet."))

    # Registered but never started
    if report.not_taken:
        elements.append(Paragraph(f"Not taken ({len(report.not_taken)})", heading_style))
        rows = [[P(h, head_cell_style) for h in ["#", "Name", "Email"]]]
        rows += [[P(i), P(n.name), P(n.email)] for i, n in enumerate(report.not_taken, start=1)]
        missing = Table(rows, colWidths=[10 * mm, 80 * mm, 90 * mm], repeatRows=1, hAlign="LEFT")
        missing.setStyle(base_style())
        elements.append(missing)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(margin, 8 * mm, f"{report.test_title[:90]} - results")
        canvas.drawRightString(page_width - margin, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=landscape(A4),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=15 * mm,
        title=f"{report.test_title} - results",
    )
    doc.build(elements, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


# ---------- Routes ----------

def _get_test(db: Session, test_id: int) -> Test:
    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
    return test


@router.get("/tests/{test_id}/results", response_model=TestResultsOut)
def test_results(test_id: int, db: DB):
    """Who took the test and what they scored, plus summary figures and who hasn't taken it."""
    return _build_results(db, _get_test(db, test_id))


@router.get("/tests/{test_id}/results/export")
def export_results(
    test_id: int,
    db: DB,
    file_format: Annotated[Literal["xlsx", "csv", "pdf"], Query(alias="format")] = "xlsx",
):
    test = _get_test(db, test_id)
    report = _build_results(db, test)

    slug = re.sub(r"[^a-z0-9]+", "-", test.title.lower()).strip("-")[:40] or "test"
    filename = f"results-{test.id}-{slug}.{file_format}"

    if file_format == "csv":
        content, media_type = _csv_bytes(report), "text/csv"
    elif file_format == "pdf":
        content, media_type = _pdf_bytes(report), "application/pdf"
    else:
        content = _xlsx_bytes(report)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
