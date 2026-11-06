from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    model_validator,
)

from models import AttemptStatus, Role, TestStatus


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops time zones, so everything is stored as UTC and always
    sent back to the browser marked as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


# ---------- Courses and auth ----------

class CourseIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    course_id: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: Role
    course: CourseOut | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- Tests ----------

class TestIn(BaseModel):
    __test__ = False  # not a pytest class

    course_id: int
    title: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    duration_minutes: int = Field(ge=1, le=600)
    pass_mark: int = Field(default=50, ge=0, le=100)  # percentage
    num_questions: int = Field(ge=1, le=500)
    opens_at: UTCDateTime | None = None
    closes_at: UTCDateTime | None = None
    show_review: bool = False

    @model_validator(mode="after")
    def check_dates(self):
        if self.opens_at and self.closes_at and self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        return self


class TestOut(BaseModel):
    __test__ = False

    model_config = ConfigDict(from_attributes=True)

    id: int
    course_id: int
    title: str
    description: str | None
    duration_minutes: int
    pass_mark: int
    num_questions: int
    opens_at: UTCDateTime | None
    closes_at: UTCDateTime | None
    show_review: bool
    status: TestStatus
    created_at: UTCDateTime
    updated_at: UTCDateTime
    question_count: int = 0  # active questions in the bank
    attempt_count: int = 0


class StatusIn(BaseModel):
    status: TestStatus


# ---------- Questions ----------

class OptionIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    is_correct: bool = False


class QuestionIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    marks: int = Field(default=1, ge=1, le=100)
    options: list[OptionIn] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def check_question(self):
        if not self.text.strip():
            raise ValueError("Question text cannot be blank")
        texts = [o.text.strip().lower() for o in self.options]
        if any(not t for t in texts):
            raise ValueError("Options cannot be blank")
        if len(set(texts)) != len(texts):
            raise ValueError("Options must be different from each other")
        if sum(o.is_correct for o in self.options) != 1:
            raise ValueError("Exactly one option must be marked correct")
        return self


class OptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    text: str
    is_correct: bool  # admin-only view; participants never get this field


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    test_id: int
    text: str
    marks: int
    is_active: bool
    options: list[OptionOut]


# ---------- Reports and import ----------

class ReportOut(BaseModel):
    id: int
    question_id: int
    question_text: str
    question_active: bool  # False means it has already been replaced or removed
    reporter_name: str
    reason: str
    resolved: bool
    created_at: UTCDateTime


class ImportRowError(BaseModel):
    row: int
    message: str


class ImportResult(BaseModel):
    dry_run: bool
    valid_count: int
    imported: int
    errors: list[ImportRowError]


# ---------- Participant side ----------

class ParticipantTestOut(BaseModel):
    id: int
    title: str
    description: str | None
    duration_minutes: int
    pass_mark: int
    num_questions: int
    opens_at: UTCDateTime | None
    closes_at: UTCDateTime | None
    availability: Literal["open", "upcoming", "closed"]
    attempt_id: int | None = None
    attempt_status: AttemptStatus | None = None
    # Filled in once the test has been submitted
    score: int | None = None
    total_marks: int | None = None
    percentage: float | None = None
    passed: bool | None = None


class ParticipantOptionOut(BaseModel):
    """Deliberately has no is_correct field: answers never reach the browser mid-test."""

    id: int
    text: str


class AttemptQuestionOut(BaseModel):
    question_id: int
    position: int
    text: str
    marks: int
    options: list[ParticipantOptionOut]
    selected_option_id: int | None = None
    flagged_for_review: bool = False


class AttemptOut(BaseModel):
    attempt_id: int
    test_id: int
    test_title: str
    status: AttemptStatus
    started_at: UTCDateTime
    deadline: UTCDateTime
    server_time: UTCDateTime  # lets the browser correct for a wrong device clock
    seconds_remaining: int
    questions: list[AttemptQuestionOut]


class AnswerIn(BaseModel):
    """Send only what changed: selected_option_id (null clears the answer) and/or flagged_for_review."""

    question_id: int
    selected_option_id: int | None = None
    flagged_for_review: bool | None = None


class AnswerSaved(BaseModel):
    saved: bool = True
    seconds_remaining: int


class ReportIn(BaseModel):
    question_id: int
    reason: str = Field(min_length=3, max_length=1000)


class ReviewOptionOut(BaseModel):
    id: int
    text: str
    is_correct: bool
    selected: bool


class ReviewItemOut(BaseModel):
    question_id: int
    position: int
    text: str
    marks: int
    options: list[ReviewOptionOut]
    answered: bool
    is_correct: bool


class ResultOut(BaseModel):
    attempt_id: int
    test_title: str
    status: AttemptStatus
    score: int
    total_marks: int
    percentage: float
    pass_mark: int
    passed: bool
    submitted_at: UTCDateTime
    review: list[ReviewItemOut] | None = None  # only when the admin turned review on


# ---------- Admin results report ----------

class ResultRowOut(BaseModel):
    attempt_id: int
    participant_id: int
    name: str
    email: str
    status: AttemptStatus
    score: int | None  # null while the participant is still taking the test
    total_marks: int | None
    percentage: float | None
    passed: bool | None
    started_at: UTCDateTime
    submitted_at: UTCDateTime | None
    time_taken_minutes: float | None


class NotTakenOut(BaseModel):
    participant_id: int
    name: str
    email: str


class ResultsSummary(BaseModel):
    course_participants: int  # everyone registered in the test's course
    taken: int  # finished (submitted or timed out)
    in_progress: int
    not_started: int
    average_percentage: float | None
    highest_percentage: float | None
    lowest_percentage: float | None
    passed: int
    failed: int
    pass_rate: float | None  # % of finished participants who passed


class TestResultsOut(BaseModel):
    __test__ = False

    test_id: int
    test_title: str
    course_name: str
    pass_mark: int
    num_questions: int
    generated_at: UTCDateTime
    summary: ResultsSummary
    results: list[ResultRowOut]
    not_taken: list[NotTakenOut]
