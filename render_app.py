"""Web initialization; Gunicorn lifecycle hook starts optional embedded runtime."""
import os
if not os.getenv('DATABASE_URL'):
    raise RuntimeError('DATABASE_URL is required')
from app import app, init_db
init_db()
