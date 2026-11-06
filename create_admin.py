"""Run once: python create_admin.py
Creates the tables (if missing) and an admin account. Admins are never created
through the public registration endpoint."""

import getpass
import sys

import models  # noqa: F401  (registers all tables on Base)
from auth import hash_password
from database import Base, SessionLocal, engine
from models import Role, User


def main():
    Base.metadata.create_all(engine)

    name = input("Admin name: ").strip()
    email = input("Admin email: ").strip().lower()
    password = getpass.getpass("Password: ")

    with SessionLocal() as db:
        if db.query(User).filter_by(email=email).first():
            sys.exit("A user with that email already exists.")
        db.add(User(name=name, email=email, password_hash=hash_password(password), role=Role.ADMIN))
        db.commit()
    print("Admin created.")


if __name__ == "__main__":
    main()
