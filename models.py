import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Role(str, enum.Enum):
    ADMIN = "admin"
    PARTICIPANT = "participant"


class TestStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    TIMED_OUT = "timed_out"  # auto-submitted when the deadline passed


def _enum(e):
    # Store the readable values ("admin"), not the member names ("ADMIN")
    return Enum(e, native_enum=False, length=20, values_callable=lambda x: [m.value for m in x])


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="course")
    tests: Mapped[list["Test"]] = relationship(back_populates="course")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Participants must belong to a course; admins don't need one.
        CheckConstraint("role = 'admin' OR course_id IS NOT NULL", name="ck_participant_has_course"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.PARTICIPANT)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    course: Mapped["Course | None"] = relationship(back_populates="users")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="user")


class Test(Base):
    __tablename__ = "tests"
    __test__ = False  # stop pytest mistaking this model for a test class

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int]
    pass_mark: Mapped[int] = mapped_column(default=50)  # percentage
    num_questions: Mapped[int]  # how many to draw from the bank per participant
    show_review: Mapped[bool] = mapped_column(default=False, server_default=false())  # show right answers after submitting
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[TestStatus] = mapped_column(_enum(TestStatus), default=TestStatus.DRAFT)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    course: Mapped["Course"] = relationship(back_populates="tests")
    questions: Mapped[list["Question"]] = relationship(back_populates="test")
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="test")


class Question(Base):
    """Edit rule (service layer): if nobody has attempted this question yet, edit in place.
    Otherwise create a new row with supersedes_id = old.id and set the old row's
    is_active = False. Past attempts keep pointing at the old row, so their scores
    never change; new attempts only draw from active questions."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    marks: Mapped[int] = mapped_column(default=1)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    test: Mapped["Test"] = relationship(back_populates="questions")
    options: Mapped[list["Option"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="Option.id"
    )


class Option(Base):
    __tablename__ = "options"
    __table_args__ = (
        # Exactly one correct option per question can exist (single-answer MCQ)
        Index(
            "uq_one_correct_option_per_question",
            "question_id",
            unique=True,
            postgresql_where=text("is_correct"),
            sqlite_where=text("is_correct"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(default=False, server_default=false())

    question: Mapped["Question"] = relationship(back_populates="options")


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        # One attempt per participant per test
        UniqueConstraint("user_id", "test_id", name="uq_one_attempt_per_user_test"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # enforced server-side
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[int | None]
    total_marks: Mapped[int | None]
    status: Mapped[AttemptStatus] = mapped_column(_enum(AttemptStatus), default=AttemptStatus.IN_PROGRESS)

    user: Mapped["User"] = relationship(back_populates="attempts")
    test: Mapped["Test"] = relationship(back_populates="attempts")
    questions: Mapped[list["AttemptQuestion"]] = relationship(
        back_populates="attempt", order_by="AttemptQuestion.position", cascade="all, delete-orphan"
    )
    answers: Mapped[list["Answer"]] = relationship(back_populates="attempt", cascade="all, delete-orphan")


class AttemptQuestion(Base):
    """The random subset and shuffled order this participant was given."""

    __tablename__ = "attempt_questions"

    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)
    position: Mapped[int]
    option_order: Mapped[list[int]] = mapped_column(JSON)  # option ids in shuffled order

    attempt: Mapped["Attempt"] = relationship(back_populates="questions")


class Answer(Base):
    __tablename__ = "answers"

    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)
    selected_option_id: Mapped[int | None] = mapped_column(ForeignKey("options.id"))
    flagged_for_review: Mapped[bool] = mapped_column(default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    attempt: Mapped["Attempt"] = relationship(back_populates="answers")


class QuestionReport(Base):
    """A participant reporting a question as faulty during the test."""

    __tablename__ = "question_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
