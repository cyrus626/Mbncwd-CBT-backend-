"""Loads settings from a .env file sitting next to this code.
Real environment variables (for example on your hosting) always win over the file."""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
