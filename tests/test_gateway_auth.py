import hashlib
import hmac
import os
import re
import secrets
import time
import unittest
from unittest.mock import patch
from flask import Flask
from gateway_auth import install_gateway_auth, sign_gateway_request

class FakeResult:
    def __init__(self,row): self.row=row
    def fetchone(self): return self.row

class FakeDB:
    def __init__(self): self.nonces=set();self.attempts=0
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def execute(self,sql,args):
        if 'INSERT INTO gateway_nonces' in sql:
            if args[0] in self.nonces:return FakeResult(None)
            self.nonces.add(args[0]);return FakeResult({'nonce':args[0]})
        if 'INSERT INTO points_login_attempts' in sql:
            self.attempts+=1;return FakeResult({'attempts':self.attempts})
        return FakeResult(None)

class GatewayAuthTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__)
        self.app.secret_key='test-secret-key-not-production'
        install_gateway_auth(self.app)
        self.app.add_url_rule('/api/points','points',lambda:'ok',methods=['GET','POST'])
        self.app.add_url_rule('/','home',lambda:'home')
        self.client=self.app.test_client()
        self.secret='S'*48
        self.db=FakeDB()
        env=patch.dict(os.environ,{'BOT_POINTS_GATEWAY_SECRET':self.secret,
            'STAFFFLOW_POINTS_GATEWAY_SECRET':self.secret,'DATABASE_URL':'mock',
            'FLASK_SECRET_KEY':'test-only','BOT_POINTS_ADMIN_PASSWORD':'',
            'BOT_POINTS_ADMIN_PASSWORD_HASH':''},clear=True)
        env.start();self.addCleanup(env.stop)
        db=patch('neon_store.connect',return_value=self.db)
        db.start();self.addCleanup(db.stop)

    def ticket(self,expiry=None):
        expiry=expiry or int(time.time())+60
        n=secrets.token_hex(32)
        sig=hmac.new(self.secret.encode(),f'bot-point-sso:v1:{expiry}:{n}'.encode(),hashlib.sha256).hexdigest()
        return f'v1.{expiry}.{n}.{sig}'

    def sso(self,ticket):
        return self.client.post('/sso',json={'ticket':ticket},headers={'X-Bot-Points-Request':'1'})

    def test_no_secret_fail_closed(self):
        with patch.dict(os.environ,{'BOT_POINTS_GATEWAY_SECRET':'','STAFFFLOW_POINTS_GATEWAY_SECRET':''}):
            self.assertEqual(self.client.get('/api/points').status_code,503)

    def test_unsigned_request_denied(self):
        self.assertEqual(self.client.get('/api/points').status_code,401)

    def test_signed_request_and_replay(self):
        headers=sign_gateway_request(self.secret,'POST','/api/points','',b'{}',
            nonce=secrets.token_hex(16),timestamp=int(time.time()))
        self.assertEqual(self.client.post('/api/points',data=b'{}',headers=headers).status_code,200)
        self.assertEqual(self.client.post('/api/points',data=b'{}',headers=headers).status_code,401)

    def test_body_tamper_denied(self):
        headers=sign_gateway_request(self.secret,'POST','/api/points','',b'{}',
            nonce=secrets.token_hex(16),timestamp=int(time.time()))
        self.assertEqual(self.client.post('/api/points',data=b'{ }',headers=headers).status_code,401)

    def test_sso_one_time_and_separate_session(self):
        token=self.ticket()
        self.assertEqual(self.client.get('/sso').status_code,200)
        self.assertEqual(self.sso(token).status_code,200)
        self.assertEqual(self.client.get('/api/points').status_code,200)
        self.assertEqual(self.client.post('/api/points').status_code,403)
        self.assertEqual(self.client.post('/api/points',headers={'X-Bot-Points-Request':'1'}).status_code,200)
        self.assertEqual(self.sso(token).status_code,401)
        fresh=self.app.test_client()
        self.assertEqual(fresh.get('/api/points').status_code,401)

    def test_sso_tamper_expiry_and_unsigned_posts(self):
        token=self.ticket()
        wrong=token[:-1]+('0' if token[-1]!='0' else '1')
        self.assertEqual(self.sso(wrong).status_code,401)
        self.assertEqual(self.sso(self.ticket(int(time.time())-1)).status_code,401)
        self.assertEqual(self.sso(self.ticket(int(time.time())+600)).status_code,401)
        self.assertEqual(self.client.post('/sso',json={'ticket':token}).status_code,403)
        self.assertEqual(self.client.get('/api/points').status_code,401)

    def test_standalone_login_without_staffflow_and_logout(self):
        with patch.dict(os.environ,{'BOT_POINTS_ADMIN_PASSWORD':'local-admin-password-123',
                'STAFFFLOW_POINTS_GATEWAY_SECRET':'','BOT_POINTS_GATEWAY_SECRET':''}):
            page=self.client.get('/login')
            self.assertEqual(page.status_code,200)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.get_data(as_text=True)).group(1)
            self.assertEqual(self.client.post('/login',data={'password':'local-admin-password-123','csrf':'wrong'}).status_code,403)
            self.assertEqual(self.client.post('/login',data={'password':'wrong','csrf':csrf}).status_code,401)
            self.assertEqual(self.client.post('/login',data={'password':'local-admin-password-123','csrf':csrf}).status_code,302)
            self.assertEqual(self.client.get('/api/points').status_code,200)
            self.assertEqual(self.client.post('/logout').status_code,403)
            self.assertEqual(self.client.post('/logout',headers={'X-Bot-Points-Request':'1'}).status_code,200)
            self.assertNotEqual(self.client.get('/api/points').status_code,200)

    def test_rotating_gateway_revokes_sso_session(self):
        self.assertEqual(self.sso(self.ticket()).status_code,200)
        with patch.dict(os.environ,{'STAFFFLOW_POINTS_GATEWAY_SECRET':'different-key-'*5}):
            self.assertEqual(self.client.get('/api/points').status_code,401)

if __name__=='__main__':
    unittest.main()
