import unittest
from telethon_diagnostics import classify,result,normalize,DiagnosticError
class DiagnosticTests(unittest.TestCase):
    def test_duplicate(self):
        self.assertEqual(classify('authorization',type('AuthKeyDuplicatedError',(Exception,),{})('secret')),'session_duplicate')
    def test_no_secret(self):
        data=normalize({'ok':False,'stage':'session','code':'session_invalid','message':'secret','checks':[{'stage':'settings','status':'ok'}]})
        self.assertNotIn('secret',str(data))
        self.assertEqual(data['checks'][0]['status'],'ok')
        self.assertEqual(data['checks'][1]['status'],'error')
    def test_unknown(self):
        self.assertEqual(normalize({'ok':False,'code':'secret'})['code'],'unknown')
    def test_topic_method(self):
        self.assertEqual(classify('topic',AttributeError('secret')),'topic_api')
    def test_missing(self):
        self.assertEqual(classify('session',DiagnosticError('session_missing')),'session_missing')
