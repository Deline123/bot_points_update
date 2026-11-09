"""PostgreSQL compatibility layer for Bot Points v3.9.9.

Only database statements are translated; all writes use bound parameters.
"""
import os
import re
from contextlib import contextmanager


def translate(sql):
    if re.search(r'\bPRAGMA\b', sql, re.I):
        raise ValueError('SQLite PRAGMA is unsupported on PostgreSQL')
    sql = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', sql, flags=re.I)
    sql = re.sub(r'\browid\s+DESC\b', 'operation_id DESC', sql, flags=re.I)
    # Existing queries use qmark parameters; none of the SQL contains JSON ? operators.
    sql = sql.replace('?', '%s')
    return sql


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        ignore = bool(re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', sql, re.I))
        sql = translate(sql)
        if ignore and 'ON CONFLICT' not in sql.upper():
            sql += ' ON CONFLICT DO NOTHING'
        return self.raw.execute(sql, params)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.raw.__exit__(*args)
        finally:
            self.raw.close()

    def close(self):
        return self.raw.close()


def connect():
    import psycopg
    from psycopg.rows import dict_row
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        raise RuntimeError('DATABASE_URL is required for PostgreSQL')
    return Connection(psycopg.connect(url, row_factory=dict_row, connect_timeout=10))


def init_schema():
    """Initialize independent Bot Points tables; no StaffFlow tables touched."""
    statements = [
        '''CREATE TABLE IF NOT EXISTS messages (chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, thread_id BIGINT, date_utc BIGINT NOT NULL, part TEXT NOT NULL, points DOUBLE PRECISION NOT NULL, text TEXT NOT NULL, assembly_id TEXT, PRIMARY KEY(chat_id,message_id,part))''',
        'CREATE INDEX IF NOT EXISTS idx_part_date ON messages(part,date_utc)',
        '''CREATE TABLE IF NOT EXISTS review_messages (chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, thread_id BIGINT, date_utc BIGINT NOT NULL, part TEXT NOT NULL, status TEXT NOT NULL, calculated DOUBLE PRECISION, last_value DOUBLE PRECISION, reason TEXT NOT NULL, text TEXT NOT NULL, assembly_id TEXT, PRIMARY KEY(chat_id,message_id))''',
        'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS missing_id_notifications (chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, thread_id BIGINT, alert_message_id BIGINT, status TEXT NOT NULL, last_error TEXT, created_utc BIGINT NOT NULL, PRIMARY KEY(chat_id,message_id))''',
        '''CREATE TABLE IF NOT EXISTS sheet_applied (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, fingerprint TEXT NOT NULL, written_value TEXT NOT NULL, updated_utc BIGINT NOT NULL, PRIMARY KEY(spreadsheet_id,gid,part))''',
        '''CREATE TABLE IF NOT EXISTS approved_reports (chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, part TEXT NOT NULL, text_hash TEXT NOT NULL, approved_points DOUBLE PRECISION NOT NULL, approved_utc BIGINT NOT NULL, PRIMARY KEY(chat_id,message_id))''',
        '''CREATE TABLE IF NOT EXISTS sheet_message_applied (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, evidence_hash TEXT NOT NULL, applied_utc BIGINT NOT NULL, PRIMARY KEY(spreadsheet_id,gid,part,chat_id,message_id))''',
        '''CREATE TABLE IF NOT EXISTS sheet_pending_jobs (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, payload TEXT NOT NULL, created_utc BIGINT NOT NULL, PRIMARY KEY(spreadsheet_id,gid))''',
        '''CREATE TABLE IF NOT EXISTS manual_point_writes (operation_id TEXT PRIMARY KEY, spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, row_number INTEGER NOT NULL, action TEXT NOT NULL, old_value TEXT NOT NULL, new_value INTEGER NOT NULL, status TEXT NOT NULL, error TEXT, created_utc BIGINT NOT NULL, finished_utc BIGINT)''',
        '''CREATE TABLE IF NOT EXISTS sheet_point_reviews (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, signature TEXT NOT NULL, old_value TEXT NOT NULL, new_value DOUBLE PRECISION NOT NULL, part_col INTEGER NOT NULL, points_col INTEGER NOT NULL, first_row INTEGER NOT NULL, mode TEXT NOT NULL, start_stamp BIGINT NOT NULL, end_stamp BIGINT NOT NULL, status TEXT NOT NULL, created_utc BIGINT NOT NULL, PRIMARY KEY(spreadsheet_id,gid,part))''',
    ]
    with connect() as db:
        statements.append('CREATE TABLE IF NOT EXISTS gateway_nonces (nonce TEXT PRIMARY KEY, expires_at BIGINT NOT NULL)')
        for sql in statements:
            db.execute(sql)
