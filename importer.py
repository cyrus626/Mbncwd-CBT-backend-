"""Parses an uploaded CSV or Excel (.xlsx) file of questions.

Expected columns (header names are not case-sensitive):
    question, option_a ... option_f, correct, marks
`correct` is the letter of the right option (A to F). `marks` is optional (default 1).
Every row is checked and ALL problems are reported at once, so the admin can fix the
file in one go.
"""

import csv
import io
from dataclasses import dataclass

MAX_ROWS = 1000
OPTION_LETTERS = "ABCDEF"

TEMPLATE_CSV = (
    "question,option_a,option_b,option_c,option_d,correct,marks\n"
    'What does print("Hi") do?,Shows Hi on the screen,Deletes Hi,Saves Hi,Nothing,A,1\n'
    "Which symbol starts a comment in Python?,//,#,--,/*,B,1\n"
)


class ImportFileError(Exception):
    """The file itself is unusable (wrong type, missing columns, empty...)."""


@dataclass
class ParsedQuestion:
    text: str
    marks: int
    options: list[tuple[str, bool]]  # (option text, is_correct)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header(value) -> str:
    return _cell(value).lower().replace(" ", "_")


def _read_rows(filename: str, content: bytes) -> list[tuple[int, dict[str, str]]]:
    """Returns (spreadsheet row number, {column: value}) for every non-empty row."""
    name = filename.lower()
    raw_rows: list[list] = []

    if name.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:  # CSVs saved by older Excel versions
            text = content.decode("cp1252", errors="replace")
        raw_rows = list(csv.reader(io.StringIO(text)))
    elif name.endswith(".xlsx"):
        from openpyxl import load_workbook  # only needed for Excel files

        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise ImportFileError("This Excel file could not be read. Is it a valid .xlsx file?")
        raw_rows = [list(r) for r in workbook.active.iter_rows(values_only=True)]
    else:
        raise ImportFileError("Upload a .csv or .xlsx file.")

    if not raw_rows:
        raise ImportFileError("The file is empty.")

    headers = [_header(h) for h in raw_rows[0]]
    if "question" not in headers or "correct" not in headers:
        raise ImportFileError(
            "The first row must contain the columns 'question' and 'correct' "
            "(plus option_a, option_b ...). Download the template to see the format."
        )

    rows = []
    for index, raw in enumerate(raw_rows[1:], start=2):  # row 1 is the header
        values = {headers[i]: _cell(v) for i, v in enumerate(raw) if i < len(headers) and headers[i]}
        if any(values.values()):  # skip blank lines
            rows.append((index, values))
    return rows


def parse_import_file(filename: str, content: bytes):
    """Returns (valid_questions, errors). errors is a list of {"row": n, "message": str}."""
    rows = _read_rows(filename, content)
    if not rows:
        raise ImportFileError("The file has no questions in it.")
    if len(rows) > MAX_ROWS:
        raise ImportFileError(f"Too many rows ({len(rows)}). The limit is {MAX_ROWS} per file.")

    valid: list[ParsedQuestion] = []
    errors: list[dict] = []

    for row_number, row in rows:
        problems = []

        text = row.get("question", "")
        if not text:
            problems.append("the question is empty")

        filled = [
            (letter, row.get(f"option_{letter.lower()}", ""))
            for letter in OPTION_LETTERS
            if row.get(f"option_{letter.lower()}", "")
        ]
        if len(filled) < 2:
            problems.append("at least 2 options are needed")
        if len({t.lower() for _, t in filled}) != len(filled):
            problems.append("two options are identical")

        correct = row.get("correct", "").upper()
        if len(correct) != 1 or correct not in OPTION_LETTERS:
            problems.append("'correct' must be a single letter from A to F")
        elif correct not in dict(filled):
            problems.append(f"'correct' is {correct} but option_{correct.lower()} is empty")

        marks = 1
        marks_raw = row.get("marks", "")
        if marks_raw:
            try:
                marks = int(float(marks_raw))
                if marks < 1:
                    raise ValueError
            except ValueError:
                problems.append("'marks' must be a whole number of 1 or more")

        if problems:
            errors.append({"row": row_number, "message": "; ".join(problems)})
        else:
            valid.append(
                ParsedQuestion(
                    text=text,
                    marks=marks,
                    options=[(t, letter == correct) for letter, t in filled],
                )
            )

    return valid, errors
