import os
import unittest
from unittest.mock import patch

@unittest.skipUnless(os.getenv('POINTS_TEST_POSTGRES'), 'requires isolated PostgreSQL service')
class PostgresRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'DATABASE_URL':os.environ['POINTS_TEST_POSTGRES']})
        self.env.start()
        self.addCleanup(self.env.stop)
        from app import connect, init_db
        self.connect=connect
        init_db()
    def test_cross_connection_lock(self):
        from runtime_control import SharedLock
        first=SharedLock(self.connect,742910)
        second=SharedLock(self.connect,742910)
        self.assertTrue(first.acquire(False))
        try:
            self.assertFalse(second.acquire(False))
        finally:
            first.release()
        self.assertTrue(second.acquire(False))
        second.release()
    def test_event_version_prevents_old_history_overwrite(self):
        from app import save_report
        from runtime_control import record_status, read_status
        with self.connect() as c:
            # Empty content is sufficient to test event ordering independently of report grammar.
            save_report(c,42420,101,5,100,'',event_stamp=200)
            stale=save_report(c,42420,101,5,100,'',event_stamp=100)
            self.assertTrue(stale['stale'])
            row=c.execute('SELECT event_utc FROM report_ingest_versions WHERE chat_id=? AND message_id=?',(42420,101)).fetchone()
            self.assertEqual(row['event_utc'],200)
        record_status(self.connect,'worker','ok',now=300)
        self.assertEqual(read_status(self.connect,'worker')['last_success'],300)

if __name__=='__main__': unittest.main()
