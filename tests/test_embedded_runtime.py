import threading
import unittest
from unittest.mock import patch, Mock
from embedded_runtime import runtime_mode, start_embedded, stop_embedded

class EmbeddedRuntimeTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(runtime_mode({'RENDER':'true'}),'embedded')
        self.assertEqual(runtime_mode({}),'disabled')
        self.assertEqual(runtime_mode({'RENDER':'true','BOT_POINTS_RUNTIME_MODE':'external'}),'external')
    def test_external_disabled_or_missing_db_do_not_start(self):
        runner=Mock()
        for env in ({'RENDER':'true'}, {'DATABASE_URL':'test'}, {'RENDER':'true','DATABASE_URL':'test','BOT_POINTS_RUNTIME_MODE':'external'}):
            self.assertFalse(start_embedded(env=env,runner=runner))
        runner.assert_not_called()
    def test_single_start_and_shutdown(self):
        entered=threading.Event()
        ended=threading.Event()
        def runner(stop, *, embedded):
            self.assertTrue(embedded)
            entered.set()
            stop.wait(3)
            ended.set()
        env={'RENDER':'true','DATABASE_URL':'test'}
        self.assertTrue(start_embedded(env=env,runner=runner))
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(start_embedded(env=env,runner=runner))
        finally:
            stop_embedded()
        self.assertTrue(ended.wait(2))
    def test_gunicorn_hooks(self):
        import runpy
        hooks=runpy.run_path('gunicorn.conf.py')
        with patch('embedded_runtime.start_embedded') as start, patch('embedded_runtime.stop_embedded') as stop:
            start.assert_not_called()
            hooks['post_worker_init'](None)
            hooks['worker_exit'](None,None)
            start.assert_called_once_with()
            stop.assert_called_once_with()

if __name__=='__main__': unittest.main()
