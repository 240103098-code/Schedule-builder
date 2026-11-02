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

    def test_verification_errors_report_safe_diagnostic(self):
        import smtplib
        client = self.local_client()
        with patch.dict(os.environ, {'APP_ENV':'development', 'WAITLIST_MODE':'academic',
                                     'SMTP_HOST':'smtp.gmail.com', 'SMTP_FROM':'sender@example.test'}):
            self.assertEqual(client.post('/api/auth/register', json={'name':'Local Student', 'email':self.email,
                'password':'Original123!', 'local_demo':True}).status_code, 201)
            self.sender.side_effect = smtplib.SMTPAuthenticationError(535, b'secret password or recipient')
            response = client.post('/api/auth/resend-verification', json={'email':self.email})
            self.assertEqual(response.status_code, 503)
            self.assertIn('Gmail', response.json()['detail'])
            self.assertNotIn('secret', response.text)
            self.sender.side_effect = TimeoutError('private data')
            response = client.post('/api/auth/resend-verification', json={'email':self.email})
            self.assertIn('подключиться', response.json()['detail'])
            self.assertNotIn('private data', response.text)
        with patch.dict(os.environ, {'SMTP_HOST':'', 'SMTP_FROM':'', 'SMTP_USERNAME':''}):
            self.sender.side_effect = RuntimeError('private data')
            response = client.post('/api/auth/resend-verification', json={'email':self.email})
            self.assertIn('не настроена', response.json()['detail'])

    def test_smtp_errors_are_not_misreported_as_network_errors(self):
        import smtplib
        import socket
        import mailer
        with patch.dict(os.environ, {'SMTP_HOST':'smtp.gmail.com', 'SMTP_FROM':'sender@example.test'}):
            self.assertIn('SMTP 550', mailer.diagnostic(smtplib.SMTPDataError(550, b'private')))
            self.assertIn('получателя', mailer.diagnostic(smtplib.SMTPRecipientsRefused({'private':(550, b'private')})))
            result = mailer.diagnostic(socket.gaierror(11001, 'private'))
            self.assertIn('gaierror', result)
            self.assertIn('11001', result)
            self.assertNotIn('private', result)

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

    def local_client(self):
        client = TestClient(self.main.app, base_url='http://127.0.0.1', client=('127.0.0.1', 55000))
        self.addCleanup(client.close)
        return client

    def test_local_student_can_register_and_log_in_without_smtp(self):
        client = self.local_client()
        self.sender.side_effect = RuntimeError('SMTP missing')
        with patch.dict(os.environ, {'APP_ENV':'development', 'WAITLIST_MODE':'academic'}):
            self.assertTrue(client.get('/api/auth/config').json()['local_registration_available'])
            result = client.post('/api/auth/register', json={'name':'Local Student', 'email':self.email,
                'password':'Original123!', 'local_demo':True, 'role':'advisor'})
            self.assertEqual(result.status_code, 201, result.text)
            self.assertTrue(result.json()['local_account'])
            self.assertFalse(result.json()['verification_required'])
            self.assertEqual(self.sender.call_count, 0)
            self.assertEqual(client.get('/api/auth/me').status_code, 200)
            self.assertEqual(client.get('/api/registration').json()['role'], 'student')
            sections = client.get('/api/waitlists').json()['sections']
            lecture = next(s for s in sections if not s['parent'])
            practice = next(s for s in sections if s['parent'] == lecture['section'])
            pair = {'lecture':lecture['section'], 'practice':practice['section']}
            joined = client.post('/api/waitlists', json=pair)
            self.assertEqual(joined.status_code, 200, joined.text)
            opened = client.post('/api/waitlists/demo/seat', json=pair)
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertEqual(len(opened.json()['enrollments']), 1)
            with self.main.SessionLocal() as db:
                user = db.query(self.main.UserModel).filter_by(email=self.email).one()
                self.assertEqual(user.email_verified, 0)
                self.assertEqual(user.local_account, 1)
                self.assertNotEqual(user.password_hash, 'Original123!')
            client.post('/api/auth/logout')
            self.assertEqual(client.post('/api/auth/login', json={'email':self.email,'password':'Original123!'}).status_code, 200)
            self.assertEqual(client.post('/api/auth/register', json={'name':'Duplicate Student','email':self.email,
                'password':'Original123!','local_demo':True}).status_code, 409)

    def test_local_registration_cannot_be_used_remotely_or_in_production(self):
        payload = {'name':'Local Student','email':self.email,'password':'Original123!','local_demo':True}
        client = self.local_client()
        with patch.dict(os.environ, {'APP_ENV':'production'}):
            self.assertFalse(client.get('/api/auth/config').json()['local_registration_available'])
            self.assertEqual(client.post('/api/auth/register',json=payload).status_code, 403)
        with patch.dict(os.environ, {'APP_ENV':'development','WAITLIST_MODE':'academic'}):
            self.assertFalse(self.client.get('/api/auth/config').json()['local_registration_available'])
            self.assertEqual(self.client.post('/api/auth/register',json=payload).status_code, 403)
        with patch.dict(os.environ, {'APP_ENV':'development','WAITLIST_MODE':'live'}):
            self.assertEqual(client.post('/api/auth/register',json=payload).status_code, 403)

    def test_local_account_sessions_do_not_work_in_production(self):
        client = self.local_client()
        with patch.dict(os.environ, {'APP_ENV':'development','WAITLIST_MODE':'academic'}):
            self.assertEqual(client.post('/api/auth/register',json={'name':'Local Student','email':self.email,
                'password':'Original123!', 'local_demo':True}).status_code, 201)
        with patch.dict(os.environ, {'APP_ENV':'production'}):
            self.assertEqual(client.get('/api/auth/me').status_code, 403)
            self.assertEqual(client.post('/api/auth/login',json={'email':self.email,'password':'Original123!'}).status_code, 403)


    def test_local_account_can_confirm_email_for_notifications(self):
        client = self.local_client()
        with patch.dict(os.environ, {'APP_ENV':'development', 'WAITLIST_MODE':'academic',
                                     'SMTP_HOST':'smtp.example.test', 'SMTP_FROM':'sender@example.test'}):
            registered = client.post('/api/auth/register', json={'name':'Local Student', 'email':self.email,
                'password':'Original123!', 'local_demo':True})
            self.assertEqual(registered.status_code, 201)
            state = client.get('/api/registration').json()
            self.assertTrue(state['email_sender_configured'])
            self.assertFalse(state['email_verified'])
            self.assertEqual(client.post('/api/auth/resend-verification', json={'email':self.email}).status_code, 200)
            self.assertEqual(self.sender.call_args.args[0], self.email)
            token = self.token('verify')
            self.assertEqual(client.post('/api/auth/verify-email', json={'token':token}).status_code, 200)
            self.assertTrue(client.get('/api/registration').json()['email_verified'])
            self.assertEqual(client.post('/api/auth/verify-email', json={'token':token}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
