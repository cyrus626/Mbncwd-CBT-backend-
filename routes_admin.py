from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from auth import require_admin
from database import get_db
from importer import TEMPLATE_CSV, ImportFileError, parse_import_file
from models import (
    Attempt,
    AttemptQuestion,
    Course,
    Option,
    Question,
    QuestionReport,
    Test,
    TestStatus,
    User,
)
from schemas import (
    ImportResult,
    QuestionIn,
    QuestionOut,
    ReportOut,
    StatusIn,
    TestIn,
    TestOut,
)

# Every route in this file needs an admin account.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

DB = Annotated[Session, Depends(get_db)]
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB


# ---------- Helpers ----------

def _get_test(db: Session, test_id: int) -> Test:
    test = db.get(Test, test_id)
    if test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
    return test


def _get_editable_test(db: Session, test_id: int) -> Test:
    test = _get_test(db, test_id)
    if test.status == TestStatus.ARCHIVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "This test is archived. Move it back to draft to change it.")
    return test


def _require_course(db: Session, course_id: int) -> None:
    if db.get(Course, course_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The selected course does not exist")


def _tests_out(db: Session, tests: list[Test]) -> list[TestOut]:
    if not tests:
        return []
    ids = [t.id for t in tests]
    questions = dict(
        db.execute(
            select(Question.test_id, func.count())
            .where(Question.test_id.in_(ids), Question.is_active)
            .group_by(Question.test_id)
        ).all()
    )
    attempts = dict(
        db.execute(
            select(Attempt.test_id, func.count()).where(Attempt.test_id.in_(ids)).group_by(Attempt.test_id)
        ).all()
    )
    return [
        TestOut.model_validate(t).model_copy(
            update={"question_count": questions.get(t.id, 0), "attempt_count": attempts.get(t.id, 0)}
        )
        for t in tests
    ]


def _has_attempts(db: Session, test_id: int) -> bool:
    return db.scalar(select(func.count()).select_from(Attempt).where(Attempt.test_id == test_id)) > 0


def _question_was_used(db: Session, question_id: int) -> bool:
    return (
        db.scalar(
            select(func.count()).select_from(AttemptQuestion).where(AttemptQuestion.question_id == question_id)
        )
        > 0
    )


def _active_question_count(db: Session, test_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(Question).where(Question.test_id == test_id, Question.is_active)
    )


def _publish_problems(db: Session, test: Test) -> list[str]:
    """Reasons this test can't be given to participants yet (empty list = ready)."""
    questions = db.scalars(
        select(Question)
        .where(Question.test_id == test.id, Question.is_active)
        .options(selectinload(Question.options))
    ).all()

    problems = []
    if len(questions) < test.num_questions:
        problems.append(
            f"The test draws {test.num_questions} questions per participant, "
            f"but the bank only has {len(questions)} active questions."
        )
    for q in questions:
        if len(q.options) < 2 or sum(o.is_correct for o in q.options) != 1:
            problems.append(f"Question {q.id} needs at least 2 options and exactly 1 correct answer.")
    return problems


def _build_question(test_id: int, data: QuestionIn) -> Question:
    return Question(
        test_id=test_id,
        text=data.text.strip(),
        marks=data.marks,
        options=[Option(text=o.text.strip(), is_correct=o.is_correct) for o in data.options],
    )


# ---------- Tests ----------

@router.post("/tests", response_model=TestOut, status_code=status.HTTP_201_CREATED)
def create_test(data: TestIn, admin: Annotated[User, Depends(require_admin)], db: DB):
    _require_course(db, data.course_id)
    test = Test(**data.model_dump(), created_by=admin.id)  # starts as a draft
    db.add(test)
    db.commit()
    db.refresh(test)
    return _tests_out(db, [test])[0]


@router.get("/tests", response_model=list[TestOut])
def list_tests(
    db: DB,
    course_id: int | None = None,
    test_status: Annotated[TestStatus | None, Query(alias="status")] = None,
):
    stmt = select(Test).order_by(Test.created_at.desc())
    if course_id is not None:
        stmt = stmt.where(Test.course_id == course_id)
    if test_status is not None:
        stmt = stmt.where(Test.status == test_status)
    return _tests_out(db, list(db.scalars(stmt).all()))


@router.get("/tests/{test_id}", response_model=TestOut)
def get_test(test_id: int, db: DB):
    return _tests_out(db, [_get_test(db, test_id)])[0]


@router.put("/tests/{test_id}", response_model=TestOut)
def update_test(test_id: int, data: TestIn, db: DB):
    test = _get_editable_test(db, test_id)

    if data.course_id != test.course_id:
        if _has_attempts(db, test.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "The course can't be changed once participants have taken the test")
        _require_course(db, data.course_id)

    for field, value in data.model_dump().items():
        setattr(test, field, value)

    # A live test must stay valid (e.g. num_questions can't exceed the bank size)
    if test.status == TestStatus.PUBLISHED:
        problems = _publish_problems(db, test)
        if problems:
            db.rollback()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"message": "These changes would break a published test", "problems": problems})

    db.commit()
    db.refresh(test)
    return _tests_out(db, [test])[0]


@router.patch("/tests/{test_id}/status", response_model=TestOut)
def set_test_status(test_id: int, data: StatusIn, db: DB):
    """draft -> published (makes it visible to the course), back to draft, or archived."""
    test = _get_test(db, test_id)

    if data.status == TestStatus.PUBLISHED:
        problems = _publish_problems(db, test)
        if problems:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"message": "This test isn't ready to publish", "problems": problems},
            )

    test.status = data.status
    db.commit()
    db.refresh(test)
    return _tests_out(db, [test])[0]


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(test_id: int, db: DB):
    _get_test(db, test_id)
    if _has_attempts(db, test_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Participants have taken this test, so it can't be deleted. Archive it instead.")
    # Bulk delete: the database cascades to its questions and options
    db.execute(delete(Test).where(Test.id == test_id))
    db.commit()


# ---------- Questions ----------

@router.get("/tests/{test_id}/questions", response_model=list[QuestionOut])
def list_questions(test_id: int, db: DB, include_inactive: bool = False):
    _get_test(db, test_id)
    stmt = (
        select(Question)
        .where(Question.test_id == test_id)
        .options(selectinload(Question.options))
        .order_by(Question.id)
    )
    if not include_inactive:
        stmt = stmt.where(Question.is_active)
    return db.scalars(stmt).all()


@router.post("/tests/{test_id}/questions", response_model=QuestionOut, status_code=status.HTTP_201_CREATED)
def add_question(test_id: int, data: QuestionIn, db: DB):
    _get_editable_test(db, test_id)
    question = _build_question(test_id, data)
    db.add(question)
    db.commit()
    db.refresh(question)
    return question


@router.put("/questions/{question_id}", response_model=QuestionOut)
def update_question(question_id: int, data: QuestionIn, db: DB):
    """If nobody has seen this question yet, it's edited in place. Otherwise a NEW
    version is created and the old one is retired, so scores already given never change
    and new participants get the corrected question."""
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found")
    if not question.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "This question was already replaced or removed. Edit its current version instead.")
    _get_editable_test(db, question.test_id)

    if _question_was_used(db, question.id):
        new_version = _build_question(question.test_id, data)
        new_version.supersedes_id = question.id
        question.is_active = False
        db.add(new_version)
        result = new_version
    else:
        question.text = data.text.strip()
        question.marks = data.marks
        # Remove the old options first: inserting the new correct option while the old
        # one still exists would break the "one correct option" rule.
        question.options.clear()
        db.flush()
        question.options.extend(
            Option(text=o.text.strip(), is_correct=o.is_correct) for o in data.options
        )
        result = question

    db.commit()
    db.refresh(result)
    return result


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: int, db: DB):
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found")
    test = _get_editable_test(db, question.test_id)

    if question.is_active and test.status == TestStatus.PUBLISHED:
        if _active_question_count(db, test.id) - 1 < test.num_questions:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Removing this question would leave too few questions for the published test. Add another one first.",
            )

    if _question_was_used(db, question.id):
        question.is_active = False  # keep it so old results still make sense
    else:
        db.delete(question)
    db.commit()


# ---------- Import ----------

@router.get("/import-template")
def import_template():
    return Response(
        TEMPLATE_CSV,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="questions_template.csv"'},
    )


@router.post("/tests/{test_id}/import", response_model=ImportResult)
def import_questions(
    test_id: int,
    db: DB,
    file: Annotated[UploadFile, File()],
    dry_run: bool = False,
):
    """Upload a .csv or .xlsx. Use dry_run=true first to check the file without saving.
    A real import is all-or-nothing: one bad row means nothing is saved."""
    _get_editable_test(db, test_id)

    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than 2 MB")

    try:
        valid, errors = parse_import_file(file.filename or "", content)
    except ImportFileError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    if dry_run:
        return ImportResult(dry_run=True, valid_count=len(valid), imported=0, errors=errors)

    if errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": "Nothing was imported. Fix the rows below and upload again.", "errors": errors},
        )

    for item in valid:
        db.add(
            Question(
                test_id=test_id,
                text=item.text,
                marks=item.marks,
                options=[Option(text=text, is_correct=is_correct) for text, is_correct in item.options],
            )
        )
    db.commit()
    return ImportResult(dry_run=False, valid_count=len(valid), imported=len(valid), errors=[])


# ---------- Questions reported as faulty ----------

@router.get("/tests/{test_id}/reports", response_model=list[ReportOut])
def list_reports(test_id: int, db: DB, resolved: bool | None = False):
    """Faulty-question reports from participants. By default only unresolved ones."""
    _get_test(db, test_id)
    stmt = (
        select(QuestionReport, Question.text, Question.is_active, User.name)
        .join(Question, QuestionReport.question_id == Question.id)
        .join(User, QuestionReport.user_id == User.id)
        .where(Question.test_id == test_id)
        .order_by(QuestionReport.created_at.desc())
    )
    if resolved is not None:
        stmt = stmt.where(QuestionReport.resolved == resolved)

    return [
        ReportOut(
            id=report.id,
            question_id=report.question_id,
            question_text=question_text,
            question_active=question_active,
            reporter_name=reporter_name,
            reason=report.reason,
            resolved=report.resolved,
            created_at=report.created_at,
        )
        for report, question_text, question_active, reporter_name in db.execute(stmt).all()
    ]


@router.post("/reports/{report_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
def resolve_report(report_id: int, db: DB):
    report = db.get(QuestionReport, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    report.resolved = True
    db.commit()
