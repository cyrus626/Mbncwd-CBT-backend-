from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from database import get_db
from models import Course, Role, User
from schemas import CourseIn, CourseOut, RegisterIn, TokenOut, UserOut

router = APIRouter()

DB = Annotated[Session, Depends(get_db)]

# Used so a login for an unknown email takes as long as one for a real email
_DUMMY_HASH = hash_password("not-a-real-password")


# ---------- Courses ----------

@router.get("/courses", response_model=list[CourseOut], tags=["courses"])
def list_courses(db: DB):
    """Public: the registration form needs this before anyone has an account."""
    return db.scalars(select(Course).order_by(Course.name)).all()


@router.post("/courses", response_model=CourseOut, status_code=status.HTTP_201_CREATED, tags=["courses"])
def create_course(data: CourseIn, _admin: Annotated[User, Depends(require_admin)], db: DB):
    name = data.name.strip()
    if db.scalar(select(Course).where(func.lower(Course.name) == name.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "A course with this name already exists")
    course = Course(name=name)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


# ---------- Auth ----------

@router.post("/auth/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED, tags=["auth"])
def register(data: RegisterIn, db: DB):
    """Participant self-registration. The role is always 'participant'."""
    email = data.email.lower()

    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    if db.get(Course, data.course_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The selected course does not exist")

    user = User(
        name=data.name.strip(),
        email=email,
        password_hash=hash_password(data.password),
        role=Role.PARTICIPANT,
        course_id=data.course_id,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # two sign-ups with the same email at the same moment
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    db.refresh(user)

    return TokenOut(access_token=create_access_token(user), user=UserOut.model_validate(user))


@router.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DB):
    """Form fields: username (the email) and password."""
    user = db.scalar(select(User).where(User.email == form.username.strip().lower()))

    password_ok = verify_password(form.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not password_ok or not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenOut(access_token=create_access_token(user), user=UserOut.model_validate(user))


@router.get("/auth/me", response_model=UserOut, tags=["auth"])
def me(user: Annotated[User, Depends(get_current_user)]):
    return user
