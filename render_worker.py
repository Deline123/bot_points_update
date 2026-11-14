"""Run as ONE independent worker, not inside Gunicorn workers.

Do not enable on a sleeping/free web instance. Use one always-on worker to
avoid duplicate Telegram polling and Google Sheets write loops.
"""
import os
import threading
import time
from app import init_db, poll, auto_write_loop, TOKEN

if __name__ == '__main__':
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    if os.getenv('BOT_POINTS_WORKER_ENABLED') != '1':
        raise RuntimeError('Explicitly set BOT_POINTS_WORKER_ENABLED=1')
    init_db()
    if TOKEN:
        threading.Thread(target=poll, daemon=True, name='telegram-poll').start()
    auto_write_loop()
