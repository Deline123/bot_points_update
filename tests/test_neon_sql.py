"""Regression checks for SQLite-to-Neon SQL compatibility, no external services."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neon_store import translate, Connection

class Cursor:
    def __init__(self):
        self.calls = []
    def execute(self, sql, params):
        self.calls.append((sql, params))
        return self

class NeonSQLTests(unittest.TestCase):
    def test_qmark_parameters(self):
        self.assertEqual(translate('SELECT * FROM meta WHERE key=?'), 'SELECT * FROM meta WHERE key=%s')
    def test_ignore_insert(self):
        raw = Cursor()
        Connection(raw).execute('INSERT OR IGNORE INTO sheet_message_applied (part) VALUES (?)', ('A1',))
        self.assertEqual(raw.calls[0], ('INSERT INTO sheet_message_applied (part) VALUES (%s) ON CONFLICT DO NOTHING', ('A1',)))
    def test_reject_pragma(self):
        with self.assertRaises(ValueError):
            translate('PRAGMA table_info(messages)')
    def test_history_order(self):
        self.assertIn('operation_id DESC', translate('ORDER BY created_utc DESC, rowid DESC LIMIT ?'))

if __name__ == '__main__':
    unittest.main()
