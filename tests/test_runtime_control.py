import sqlite3
import unittest
from unittest.mock import patch
from runtime_control import SharedLock, init_runtime, record_status, read_status, dashboard

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE messages(date_utc INTEGER,chat_id INTEGER,thread_id INTEGER);
          CREATE TABLE review_messages(chat_id INTEGER,thread_id INTEGER);
          CREATE TABLE sheet_point_reviews(status TEXT,spreadsheet_id TEXT,gid TEXT);
          CREATE TABLE sheet_applied(updated_utc INTEGER,spreadsheet_id TEXT,gid TEXT);
        """)
        init_runtime(self.db)
        self.connect=lambda:self.db
    def tearDown(self): self.db.close()
    def test_persistent_status_and_error(self):
        record_status(self.connect,'worker','ok',now=100)
        record_status(self.connect,'worker','error',error='safe',now=101)
        r=read_status(self.connect,'worker')
        self.assertEqual(r['last_success'],100)
        self.assertEqual(r['error'],'safe')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM runtime_events').fetchone()[0],2)
    def test_scope_and_truthful_status(self):
        now=1795262400
        self.db.execute('INSERT INTO messages VALUES(?,?,?)',(now,1,2))
        self.db.execute('INSERT INTO messages VALUES(?,?,?)',(now,1,3))
        record_status(self.connect,'worker','ok',now=now-400)
        r=dashboard(self.connect,'1','2',{'DATABASE_URL':'test','BOT_TOKEN':'x'}, {},False,{},now=now)
        self.assertEqual(r['counts']['processed'],1)
        self.assertEqual(r['services']['worker'],'stale')
        self.assertEqual(r['services']['telegram'],'waiting')
        self.assertFalse(r['writes_enabled'])
    def test_explicit_cloud_write_gate(self):
        env={'DATABASE_URL':'x','BOT_POINTS_WRITES_ENABLED':'1'}
        self.assertFalse(dashboard(self.connect,'','',env,{},True,{})['writes_enabled'])
        env['BOT_POINTS_CLOUD_WRITES_CONFIRMED']='1'
        self.assertTrue(dashboard(self.connect,'','',env,{},True,{})['writes_enabled'])
    def test_lock_local_excludes_overlap(self):
        with patch.dict('os.environ',{'DATABASE_URL':''}):
            l=SharedLock(self.connect,123)
            self.assertTrue(l.acquire(False))
            self.assertFalse(l.acquire(False))
            l.release()
            with l: self.assertFalse(l.acquire(False))
            self.assertTrue(l.acquire(False))
            l.release()

if __name__=='__main__': unittest.main()
