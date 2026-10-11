import importlib
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class EmailAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {'DATABASE_URL':'sqlite:///'
            + cls.directory.name.replace('\\','/') + '/auth.db', 'WAITLIST_MODE':'academic'})
        cls.environment.start()
        sys.modules.pop('main', None)
        cls.main = importlib.import_module('main')
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        cls.main.engine.dispose()
        cls.environment.stop()
        cls.directory.cleanup()

    def setUp(self):
        type(self).counter += 1
        self.email = f'auth{self.counter}@example.test'
        self.client = TestClient(self.main.app)
        self.sender_patch = patch('mailer.send_email', return_value=True)
        self.sender = self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)

    def register(self):
        return self.client.post('/api/auth/register',json={'name':'Test Student',
            'email':self.email,'password':'Original123!'})

    def token(self, purpose):
        return re.search('#' + purpose + r'-token=([^\s]+)', self.sender.call_args.args[2]).group(1)

    def verify(self):
        self.assertEqual(self.register().status_code,201)
        token = self.token('verify')
        self.assertEqual(self.client.post('/api/auth/verify-email',json={'token':token}).status_code,200)
        return token

    def login(self, password='Original123!'):
        return self.client.post('/api/auth/login',json={'email':self.email,'password':password})

    def test_registration_requires_mailbox_confirmation_and_single_use(self):
        response = self.register()
        self.assertEqual(response.status_code,201,response.text)
        self.assertTrue(response.json()['verification_required'])
        self.assertNotIn('token',response.text)
        self.assertFalse(response.cookies)
        self.assertEqual(self.login().status_code,403)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        token = self.token('verify')
        with self.main.SessionLocal() as db:
            record = db.query(self.main.EmailVerificationModel).filter_by(token_hash=self.main.token_digest(token)).one()
            self.assertNotEqual(record.token_hash, token)
        self.assertEqual(self.client.post('/api/auth/verify-email',json={'token':token}).status_code,200)
        self.assertEqual(self.client.post('/api/auth/verify-email',json={'token':token}).status_code,400)
        self.assertEqual(self.login().status_code,200)
        self.assertEqual(self.client.get('/api/auth/me').status_code,200)

    def test_expired_verification_resend_and_cooldown(self):
        self.assertEqual(self.register().status_code,201)
        old = self.token('verify')
        self.assertEqual(self.client.post('/api/auth/resend-verification',json={'email':self.email}).status_code,429)
        with self.main.SessionLocal.begin() as db:
            db.query(self.main.EmailVerificationModel).filter_by(token_hash=self.main.token_digest(old)).update({'expires_at':int(time.time())-1})
        self.assertEqual(self.client.post('/api/auth/verify-email',json={'token':old}).status_code,400)
        self.assertEqual(self.client.post('/api/auth/resend-verification',json={'email':self.email}).status_code,200)
        new = self.token('verify')
        self.assertNotEqual(old,new)
        self.assertEqual(self.client.post('/api/auth/verify-email',json={'token':new}).status_code,200)

    def test_reset_requires_emailed_token_and_revokes_sessions(self):
        self.verify()
        self.assertEqual(self.login().status_code,200)
        second = TestClient(self.main.app)
        self.assertEqual(second.post('/api/auth/login',json={'email':self.email,'password':'Original123!'}).status_code,200)
        response = self.client.post('/api/auth/forgot-password',json={'email':self.email})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('token',response.text)
        token = self.token('reset')
        self.assertEqual(self.client.post('/api/auth/reset-password',json={'token':'forged','password':'NewPassword123!'}).status_code,400)
        self.assertEqual(self.client.post('/api/auth/reset-password',json={'token':token,'password':'NewPassword123!'}).status_code,200)
        self.assertEqual(self.client.post('/api/auth/reset-password',json={'token':token,'password':'Another123!'}).status_code,400)
        self.assertEqual(second.get('/api/auth/me').status_code,401)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        self.assertEqual(self.login().status_code,401)
        self.assertEqual(self.login('NewPassword123!').status_code,200)

    def test_missing_smtp_does_not_register_or_expose_reset_token(self):
        self.sender.side_effect = RuntimeError('SMTP missing')
        self.assertEqual(self.register().status_code,503)
        with self.main.SessionLocal() as db:
            self.assertIsNone(db.query(self.main.UserModel).filter_by(email=self.email).first())
        self.sender.side_effect = None
        self.verify()
        self.sender.side_effect = RuntimeError('SMTP unavailable')
        response = self.client.post('/api/auth/forgot-password',json={'email':self.email})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('token',response.text)
        with self.main.SessionLocal() as db:
            user = db.query(self.main.UserModel).filter_by(email=self.email).one()
            self.assertEqual(db.query(self.main.PasswordResetModel).filter_by(user_id=user.id).count(),0)

    def test_expired_reset_and_unknown_address(self):
        self.verify()
        self.assertEqual(self.client.post('/api/auth/forgot-password',json={'email':self.email}).status_code,200)
        token = self.token('reset')
        with self.main.SessionLocal.begin() as db:
            db.query(self.main.PasswordResetModel).filter_by(token_hash=self.main.token_digest(token)).update({'expires_at':int(time.time())-1})
        self.assertEqual(self.client.post('/api/auth/reset-password',json={'token':token,'password':'NewPassword123!'}).status_code,400)
        before = self.sender.call_count
        unknown = self.client.post('/api/auth/forgot-password',json={'email':'unknown@example.test'})
        self.assertEqual(unknown.status_code,200)
        self.assertEqual(before,self.sender.call_count)


if __name__ == '__main__':
    unittest.main()
