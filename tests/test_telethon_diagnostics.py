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

    def test_actual_installed_forum_request(self):
        from history_sync import forum_request
        from telethon.tl.types import InputPeerChannel
        peer=InputPeerChannel(123,456)
        self.assertIsNotNone(forum_request('GetForumTopicsRequest',peer,q='',offset_date=0,offset_id=0,offset_topic=0,limit=10))
        self.assertIsNotNone(forum_request('GetForumTopicsByIDRequest',peer,topics=[2]))
    def test_legacy_channel_parameter(self):
        from unittest.mock import patch,Mock
        from history_sync import forum_request,tl_messages,tl_channels
        legacy=Mock(return_value='legacy')
        with patch.object(tl_messages,'GetForumTopicsRequest',None,create=True),patch.object(tl_channels,'GetForumTopicsRequest',legacy,create=True):
            self.assertEqual(forum_request('GetForumTopicsRequest','entity',q=''),'legacy')
        legacy.assert_called_once_with(channel='entity',q='')
