import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models  # noqa: F401  (registers all tables on Base)
from database import Base, engine
from routes_admin import router as admin_router
from routes_auth import router as auth_router
from routes_participant import router as participant_router
from routes_reports import router as reports_router

Base.metadata.create_all(engine)

app = FastAPI(title="CBT API")

# Your React dev server (Vite is 5173, Create React App is 3000). In production,
# set CORS_ORIGINS to your real site address, e.g. https://tests.example.com
origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # lets the browser read the export file name
)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(participant_router)
app.include_router(reports_router)
