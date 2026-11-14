"""Gunicorn entrypoint. No polling or spreadsheet writing in web workers."""
import os
if not os.getenv('DATABASE_URL'):
    raise RuntimeError('DATABASE_URL is required')
from app import app, init_db
init_db()
