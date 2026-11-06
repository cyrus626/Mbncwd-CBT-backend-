import random
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from auth import require_participant
from database import get_db
from models import (
    Answer,
    Attempt,
    AttemptQuestion,
    AttemptStatus,
    Option,
    Question,
    QuestionReport,
    Test,
    TestStatus,
    User,
)
from schemas import (
    AnswerIn,
    AnswerSaved,
    AttemptOut,
    AttemptQuestionOut,
    ParticipantOptionOut,
    ParticipantTestOut,
    ReportIn,
    ResultOut,
    ReviewItemOut,
    ReviewOptionOut,
)

router = APIRouter(tags=["participant"])

DB = Annotated[Session, Depends(get_db)]
Participant = Annotated[User, Depends(require_participant)]

# Extra seconds allowed after the deadline, so a last answer or the submit click
# that was already on its way over the network isn't thrown away.
GRACE_SECONDS = 5


# ---------- Time helpers ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything in the database is UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _seconds_remaining(attempt: Attempt) -> int:
    return max(0, int((_utc(attempt.deadline) - _now()).total_seconds()))


def _availability(test: Test, now: datetime) -> str:
    opens, closes = _utc(test.opens_at), _utc(test.closes_at)
    if opens and now < opens:
        return "upcoming"
    if closes and now > closes:
        return "closed"
    return "open"


def _percentage(score: int, total: int) -> float:
    return round(score * 100 / total, 1) if total else 0.0


# ---------- Attempt helpers ----------

def _ended_error(attempt: Attempt, message: str) -> HTTPException:
    """409 that tells the React app which attempt to show the result of."""
    return HTTPException(status.HTTP_409_CONFLICT, {"message": message, "attempt_id": attempt.id})


def _finalize(db: Session, attempt: Attempt, *, timed_out: bool) -> None:
    """Grade the attempt on the server. The caller commits."""
    question_ids = [aq.question_id for aq in attempt.questions]
    correct_option = dict(
        db.execute(
            select(Option.question_id, Option.id).where(Option.question_id.in_(question_ids), Option.is_correct)
        ).all()
    )
    marks = dict(db.execute(select(Question.id, Question.marks).where(Question.id.in_(question_ids))).all())
    chosen = {a.question_id: a.selected_option_id for a in attempt.answers}

    attempt.score = sum(
        marks[qid]
        for qid in question_ids
        if chosen.get(qid) is not None and chosen[qid] == correct_option.get(qid)
    )
    deadline = _utc(attempt.deadline)
    attempt.submitted_at = deadline if timed_out else min(_now(), deadline)
    attempt.status = AttemptStatus.TIMED_OUT if timed_out else AttemptStatus.SUBMITTED


def _settle_if_expired(db: Session, attempt: Attempt) -> None:
    """If time ran out, grade whatever was auto-saved. Safe to call repeatedly."""
    if attempt.status == AttemptStatus.IN_PROGRESS and _now() > _utc(attempt.deadline) + timedelta(seconds=GRACE_SECONDS):
        _finalize(db, attempt, timed_out=True)
        db.commit()


def _load_own_attempt(db: Session, attempt_id: int, user: User) -> Attempt:
    attempt = db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:  # same error either way: don't reveal other people's ids
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attempt not found")
    _settle_if_expired(db, attempt)
    return attempt


def _attempt_payload(db: Session, attempt: Attempt, test: Test) -> AttemptOut:
    aqs = attempt.questions  # already ordered by position
    question_ids = [aq.question_id for aq in aqs]
    questions = {
        q.id: q
        for q in db.scalars(
            select(Question).where(Question.id.in_(question_ids)).options(selectinload(Question.options))
        )
    }
    answers = {a.question_id: a for a in attempt.answers}

    items = []
    for aq in aqs:
        q = questions[aq.question_id]
        options = {o.id: o for o in q.options}
        answer = answers.get(q.id)
        items.append(
            AttemptQuestionOut(
                question_id=q.id,
                position=aq.position,
                text=q.text,
                marks=q.marks,
                options=[ParticipantOptionOut(id=oid, text=options[oid].text) for oid in aq.option_order if oid in options],
                selected_option_id=answer.selected_option_id if answer else None,
                flagged_for_review=answer.flagged_for_review if answer else False,
            )
        )

    return AttemptOut(
        attempt_id=attempt.id,
        test_id=test.id,
        test_title=test.title,
        status=attempt.status,
        started_at=_utc(attempt.started_at),
        deadline=_utc(attempt.deadline),
        server_time=_now(),
        seconds_remaining=_seconds_remaining(attempt),
        questions=items,
    )


def _result_payload(db: Session, attempt: Attempt) -> ResultOut:
    test = db.get(Test, attempt.test_id)
    percentage = _percentage(attempt.score, attempt.total_marks)

    review = None
    if test.show_review:
        aqs = attempt.questions
        questions = {
            q.id: q
            for q in db.scalars(
                select(Question)
                .where(Question.id.in_([aq.question_id for aq in aqs]))
                .options(selectinload(Question.options))
            )
        }
        chosen = {a.question_id: a.selected_option_id for a in attempt.answers}
        review = []
        for aq in aqs:
            q = questions[aq.question_id]
            options = {o.id: o for o in q.options}
            selected_id = chosen.get(q.id)
            review.append(
                ReviewItemOut(
                    question_id=q.id,
                    position=aq.position,
                    text=q.text,
                    marks=q.marks,
                    options=[
                        ReviewOptionOut(
                            id=oid,
                            text=options[oid].text,
                            is_correct=options[oid].is_correct,
                            selected=(oid == selected_id),
                        )
                        for oid in aq.option_order
                        if oid in options
                    ],
                    answered=selected_id is not None,
                    is_correct=selected_id is not None and options.get(selected_id) is not None and options[selected_id].is_correct,
                )
            )

    return ResultOut(
        attempt_id=attempt.id,
        test_title=test.title,
        status=attempt.status,
        score=attempt.score,
        total_marks=attempt.total_marks,
        percentage=percentage,
        pass_mark=test.pass_mark,
        passed=percentage >= test.pass_mark,
        submitted_at=_utc(attempt.submitted_at),
        review=review,
    )


# ---------- Routes ----------

@router.get("/tests/available", response_model=list[ParticipantTestOut])
def available_tests(user: Participant, db: DB):
    """Published tests for the participant's course, with their own attempt status."""
    tests = db.scalars(
        select(Test)
        .where(Test.course_id == user.course_id, Test.status == TestStatus.PUBLISHED)
        .order_by(Test.created_at.desc())
    ).all()
    attempts = {
        a.test_id: a
        for a in db.scalars(
            select(Attempt).where(Attempt.user_id == user.id, Attempt.test_id.in_([t.id for t in tests]))
        )
    }

    now = _now()
    out = []
    for test in tests:
        attempt = attempts.get(test.id)
        if attempt:
            _settle_if_expired(db, attempt)

        availability = _availability(test, now)
        if availability == "closed" and attempt is None:
            continue  # nothing the participant can do with it

        item = ParticipantTestOut(
            id=test.id,
            title=test.title,
            description=test.description,
            duration_minutes=test.duration_minutes,
            pass_mark=test.pass_mark,
            num_questions=test.num_questions,
            opens_at=test.opens_at,
            closes_at=test.closes_at,
            availability=availability,
            attempt_id=attempt.id if attempt else None,
            attempt_status=attempt.status if attempt else None,
        )
        if attempt and attempt.status != AttemptStatus.IN_PROGRESS:
            item.score = attempt.score
            item.total_marks = attempt.total_marks
            item.percentage = _percentage(attempt.score, attempt.total_marks)
            item.passed = item.percentage >= test.pass_mark
        out.append(item)
    return out


@router.post("/tests/{test_id}/start", response_model=AttemptOut)
def start_test(test_id: int, user: Participant, db: DB):
    """Starts the test, or resumes it if the participant already started and time remains.
    Only one attempt per participant per test is ever created."""
    test = db.get(Test, test_id)
    if test is None or test.status != TestStatus.PUBLISHED or test.course_id != user.course_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")

    existing = db.scalar(select(Attempt).where(Attempt.user_id == user.id, Attempt.test_id == test.id))
    if existing:
        return _resume(db, existing, test)

    now = _now()
    availability = _availability(test, now)
    if availability == "upcoming":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This test has not opened yet")
    if availability == "closed":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This test is closed")

    bank = db.scalars(
        select(Question).where(Question.test_id == test.id, Question.is_active).options(selectinload(Question.options))
    ).all()
    if len(bank) < test.num_questions:
        raise HTTPException(status.HTTP_409_CONFLICT, "This test isn't ready yet. Please contact your administrator.")

    chosen = random.sample(list(bank), test.num_questions)  # random subset, in random order

    attempt = Attempt(
        user_id=user.id,
        test_id=test.id,
        started_at=now,
        deadline=now + timedelta(minutes=test.duration_minutes),
        total_marks=sum(q.marks for q in chosen),
        status=AttemptStatus.IN_PROGRESS,
    )
    attempt.questions = [
        AttemptQuestion(
            question_id=q.id,
            position=position,
            option_order=random.sample([o.id for o in q.options], len(q.options)),
        )
        for position, q in enumerate(chosen, start=1)
    ]
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:  # a double-click created the attempt a moment earlier
        db.rollback()
        existing = db.scalar(select(Attempt).where(Attempt.user_id == user.id, Attempt.test_id == test.id))
        if existing is None:
            raise
        return _resume(db, existing, test)

    db.refresh(attempt)
    return _attempt_payload(db, attempt, test)


def _resume(db: Session, attempt: Attempt, test: Test) -> AttemptOut:
    _settle_if_expired(db, attempt)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise _ended_error(attempt, "You have already taken this test.")
    return _attempt_payload(db, attempt, test)


@router.get("/attempts/{attempt_id}", response_model=AttemptOut)
def get_attempt(attempt_id: int, user: Participant, db: DB):
    """Reload the running test (after a refresh): same questions, saved answers, time left."""
    attempt = _load_own_attempt(db, attempt_id, user)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise _ended_error(attempt, "This test has ended.")
    return _attempt_payload(db, attempt, db.get(Test, attempt.test_id))


@router.put("/attempts/{attempt_id}/answers", response_model=AnswerSaved)
def save_answer(attempt_id: int, data: AnswerIn, user: Participant, db: DB):
    """Auto-save. Call it every time the participant picks an option or flags a question."""
    attempt = _load_own_attempt(db, attempt_id, user)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise _ended_error(attempt, "Time is up. Your saved answers have been submitted.")

    aq = db.get(AttemptQuestion, (attempt.id, data.question_id))
    if aq is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That question isn't part of your test")

    sent = data.model_fields_set
    if "selected_option_id" in sent and data.selected_option_id is not None:
        if data.selected_option_id not in aq.option_order:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That option doesn't belong to this question")

    answer = db.get(Answer, (attempt.id, data.question_id))
    if answer is None:
        answer = Answer(attempt_id=attempt.id, question_id=data.question_id)
        db.add(answer)
    if "selected_option_id" in sent:
        answer.selected_option_id = data.selected_option_id
    if "flagged_for_review" in sent and data.flagged_for_review is not None:
        answer.flagged_for_review = data.flagged_for_review

    db.commit()
    return AnswerSaved(seconds_remaining=_seconds_remaining(attempt))


@router.post("/attempts/{attempt_id}/submit", response_model=ResultOut)
def submit_attempt(attempt_id: int, user: Participant, db: DB):
    """Grades on the server and returns the score. Safe to call twice (double-click)."""
    attempt = _load_own_attempt(db, attempt_id, user)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        _finalize(db, attempt, timed_out=False)
        db.commit()
    return _result_payload(db, attempt)


@router.get("/attempts/{attempt_id}/result", response_model=ResultOut)
def get_result(attempt_id: int, user: Participant, db: DB):
    attempt = _load_own_attempt(db, attempt_id, user)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        raise _ended_error(attempt, "This test is still in progress.")
    return _result_payload(db, attempt)


@router.post("/attempts/{attempt_id}/report", status_code=status.HTTP_201_CREATED)
def report_question(attempt_id: int, data: ReportIn, user: Participant, db: DB) -> dict[str, bool]:
    """Report a question as faulty. Allowed during the test and afterwards."""
    attempt = _load_own_attempt(db, attempt_id, user)
    if db.get(AttemptQuestion, (attempt.id, data.question_id)) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That question isn't part of your test")

    existing = db.scalar(
        select(QuestionReport).where(
            QuestionReport.attempt_id == attempt.id,
            QuestionReport.question_id == data.question_id,
            QuestionReport.resolved.is_(False),
        )
    )
    if existing:
        existing.reason = data.reason.strip()  # reporting again just updates the note
    else:
        db.add(
            QuestionReport(
                question_id=data.question_id,
                attempt_id=attempt.id,
                user_id=user.id,
                reason=data.reason.strip(),
            )
        )
    db.commit()
    return {"reported": True}
