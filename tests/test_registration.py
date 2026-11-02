import os
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from registration_service import install_registration, OverrideInput, DecisionInput, SubscriptionInput
from waitlist_service import WaitlistService, JoinRequest, InventoryUpdate


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.directory.name.replace('\\', '/') + '/test.db',
                                    connect_args={'check_same_thread': False})
        base = declarative_base()

        class User(base):
            __tablename__ = 'users'
            id = Column(Integer, primary_key=True)
            name = Column(String)
            email = Column(String)
            email_verified = Column(Integer, default=1)

        self.sessions = sessionmaker(bind=self.engine)
        curriculum = {'categories':[{'required':[{'code':'A', 'name':'Course A', 'credits':6,
            'schedule':{'lecture_sections':[{'section':'A.L', 'quota':1, 'meetings':[],
                'practice_sections':[{'section':'A.P', 'quota':1, 'meetings':[]}]}]}},
                {'code':'B', 'name':'Course without scheduled sections', 'credits':3}]}]}
        self.waitlists = WaitlistService(base, self.sessions, User, curriculum, academic=True)
        self.app = FastAPI()

        def get_user(request: Request):
            if not request.headers.get('X-Test-User'):
                raise HTTPException(401, 'Login required')
            with self.sessions() as db:
                return db.get(User, int(request.headers['X-Test-User']))

        self.service = install_registration(self.app, base, self.waitlists, get_user, curriculum)
        with self.sessions.begin() as db:
            db.add_all([User(id=i, name=f'User {i}', email=f'user{i}@example.test') for i in range(1, 6)])
            db.add_all([self.service.Profile(user_id=i, role=role, department=dept)
                        for i, role, dept in [(1,'student','CS'), (2,'advisor','CS'), (3,'advisor','MATH'),
                                              (4,'student','CS'), (5,'student','MATH')]])
            db.add(self.service.Policy(course='A', department='CS', restricted=1))
        self.client = TestClient(self.app)
        self.pair = JoinRequest(lecture='A.L', practice='A.P')
        self.override = OverrideInput(**self.pair.model_dump(), reason='Required course this semester')

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.directory.cleanup()

    def call(self, method, path, user=1, data=None):
        return self.client.request(method, '/api/registration' + path, json=data, headers={'X-Test-User':str(user)})

    def request_override(self):
        result = self.call('POST', '/overrides', data=self.override.model_dump())
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['requests'][0]['id']

    def test_department_rbac_and_approval_unlocks_waiting_enrollment(self):
        self.assertEqual(self.client.get('/api/registration').status_code, 401)
        self.waitlists.join(1, self.pair)
        self.waitlists.demo_seat(1, self.pair)
        self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
        request_id = self.request_override()
        self.assertEqual(self.call('GET', '', user=3).json()['requests'], [])
        self.assertEqual(self.call('GET', '', user=4).json()['requests'], [])
        decision = {'decision':'approved', 'comment':'Approved by department'}
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=1, data=decision).status_code, 403)
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=3, data=decision).status_code, 404)
        approved = self.call('POST', f'/overrides/{request_id}/decision', user=2, data=decision)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(len(self.waitlists.snapshot(1)['enrollments']), 1)
        self.assertEqual(approved.json()['requests'][0]['comment'], decision['comment'])
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=2, data=decision).status_code, 409)

    def test_rejection_blocks_direct_join_and_worker_promotion(self):
        request_id = self.request_override()
        result = self.call('POST', f'/overrides/{request_id}/decision', user=2, data={'decision':'rejected'})
        self.assertEqual(result.status_code, 200)
        self.waitlists.join(1, self.pair)
        self.waitlists.demo_seat(1, self.pair)
        with self.waitlists.transaction() as db:
            self.waitlists.promote(db)
        self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
        self.assertIn('rejected', self.waitlists.snapshot(1)['waitlists'][0]['reason'])
        self.assertEqual(self.call('POST', '/overrides', data=self.override.model_dump()).status_code, 409)

    def test_overload_approval_does_not_override_account_hold(self):
        with self.sessions.begin() as db:
            db.get(self.service.Policy, 'A').restricted = 0
        with patch.dict(os.environ, {'REGISTRATION_CREDIT_LIMIT':'5'}):
            request_id = self.request_override()
            self.assertEqual(self.call('GET', '').json()['requests'][0]['flags'], ['overload'])
            self.waitlists.join(1, self.pair)
            self.waitlists.demo_hold(1, True)
            self.waitlists.demo_seat(1, self.pair)
            self.service.decide(2, request_id, DecisionInput(decision='approved'))
            self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
            self.waitlists.demo_hold(1, False)
            self.assertEqual(len(self.waitlists.snapshot(1)['enrollments']), 1)

    def inventory(self, event, revision, occupied):
        self.waitlists.source_event(InventoryUpdate(event_id=event, section='A.L', revision=revision,
            quota=1, occupied=occupied, reserved=0), 'inventory')

    def test_seat_transition_channels_deduplication_and_unsubscribe(self):
        subscription = {'section':'A.L', 'channels':['push','email']}
        self.assertEqual(self.call('PUT', '/subscriptions', data={**subscription, 'channels':['invalid']}).status_code, 422)
        result = self.call('PUT', '/subscriptions', data=subscription)
        self.assertEqual(result.status_code, 200, result.text)
        sub_id = result.json()['subscriptions'][0]['id']
        started = time.monotonic()
        self.inventory('open', 1, 0)
        notices = self.call('GET', '').json()['notifications']
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual({n['channel'] for n in notices}, {'push','email'})
        self.inventory('open', 1, 0)
        self.inventory('still-open', 2, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 2)
        self.inventory('full', 3, 1)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 2)
        self.inventory('open-again', 4, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 4)
        self.assertEqual(self.call('DELETE', f'/subscriptions/{sub_id}', user=4).status_code, 404)
        self.assertEqual(self.call('DELETE', f'/subscriptions/{sub_id}').status_code, 200)
        self.inventory('full-again', 5, 1)
        self.inventory('no-subscriber', 6, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 4)
        self.assertTrue(all(n['status'] == 'cancelled' for n in self.call('GET', '').json()['notifications']))

    def test_seat_email_uses_smtp_without_gateway_and_retries(self):
        self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['email']))
        self.inventory('smtp-open', 1, 0)
        with patch.dict(os.environ, {'SMTP_HOST':'smtp.example.test', 'SMTP_FROM':'sender@example.test',
                                     'UNIVERSITY_NOTIFICATION_URL':''}):
            self.assertTrue(self.call('GET', '').json()['email_configured'])
            with patch('mailer.send_email', side_effect=OSError('offline')):
                self.waitlists.dispatch()
            with self.sessions.begin() as db:
                event = db.query(self.waitlists.Outbox).one()
                self.assertEqual(event.status, 'pending')
                event.next_attempt = 0
            with patch('mailer.send_email', return_value=True) as sender, patch('waitlist_service.urlopen') as gateway:
                self.waitlists.dispatch()
                self.waitlists.dispatch()
                sender.assert_called_once()
                self.assertEqual(sender.call_args.args[0], 'user1@example.test')
                self.assertTrue(sender.call_args.args[3])
                gateway.assert_not_called()
            notice = self.call('GET', '').json()['notifications'][0]
            self.assertEqual(notice['status'], 'delivered')
            self.assertEqual(notice['provider'], 'smtp')

    def test_unverified_email_waits_until_verification_then_sends(self):
        with self.sessions.begin() as db:
            user = db.get(self.waitlists.user_model, 1)
            user.email_verified = 0
        self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['email']))
        self.inventory('verify-email-before-send', 1, 0)
        with patch.dict(os.environ, {'SMTP_HOST':'smtp.example.test', 'SMTP_FROM':'sender@example.test'}):
            with patch('mailer.send_email') as sender:
                self.waitlists.dispatch()
                sender.assert_not_called()
                self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'pending')
                with self.sessions.begin() as db:
                    db.get(self.waitlists.user_model, 1).email_verified = 1
                    db.query(self.waitlists.Outbox).one().next_attempt = 0
                self.waitlists.dispatch()
                sender.assert_called_once()
                self.assertEqual(sender.call_args.args[0], 'user1@example.test')
                self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'delivered')

    def test_unconfigured_seat_email_stays_pending(self):
        self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['email']))
        self.inventory('unconfigured-email', 1, 0)
        with patch.dict(os.environ, {'SMTP_HOST':'', 'SMTP_FROM':'', 'SMTP_USERNAME':'',
                                     'UNIVERSITY_NOTIFICATION_URL':''}):
            self.assertFalse(self.call('GET', '').json()['email_configured'])
            with patch('mailer.send_email') as sender:
                self.waitlists.dispatch()
                sender.assert_not_called()
            self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'pending')

    def test_gateway_payload_and_failure_retry(self):
        self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['push']))
        self.inventory('gateway-open', 1, 0)
        with patch.dict(os.environ, {'UNIVERSITY_NOTIFICATION_URL':'https://university.example.test/notify'}):
            with patch('waitlist_service.urlopen', side_effect=OSError('offline')):
                self.waitlists.dispatch()
            with self.sessions.begin() as db:
                event = db.query(self.waitlists.Outbox).one()
                self.assertEqual(event.status, 'pending')
                self.assertEqual(event.last_error, 'OSError')
                event.next_attempt = 0
            with patch('waitlist_service.urlopen') as gateway:
                gateway.return_value.__enter__.return_value.status = 202
                self.waitlists.dispatch()
                request = gateway.call_args.args[0]
                import json
                body = json.loads(request.data)
                self.assertEqual(body['channel'], 'push')
                self.assertEqual(body['section'], 'A.L')
                self.assertEqual(body['student_email'], 'user1@example.test')
                self.assertIn('Idempotency-key', request.headers)
            self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'delivered')

    def test_existing_account_without_department_can_submit_in_academic_mode(self):
        with self.sessions.begin() as db:
            db.query(self.service.Profile).filter_by(user_id=1).delete()
        initial = self.call('GET', '')
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()['department'], 'CS')
        self.assertEqual(initial.json()['role'], 'student')
        request_id = self.request_override()
        self.assertEqual(len(self.call('GET', '', user=2).json()['requests']), 1)
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=1,
                                  data={'decision':'approved'}).status_code, 403)

    def test_unrestricted_course_accepts_manual_review_and_waits_for_approval(self):
        with self.sessions.begin() as db:
            db.query(self.service.Profile).filter_by(user_id=1).delete()
            db.query(self.service.Policy).delete()
        request_id = self.request_override()
        self.assertEqual(self.call('GET', '').json()['requests'][0]['flags'], ['manual_review'])
        self.waitlists.join(1, self.pair)
        self.waitlists.demo_seat(1, self.pair)
        self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=2,
                                  data={'decision':'approved'}).status_code, 200)
        self.assertEqual(len(self.waitlists.snapshot(1)['enrollments']), 1)

    def test_manual_review_rejection_remains_blocked(self):
        with self.sessions.begin() as db:
            db.query(self.service.Policy).delete()
        request_id = self.request_override()
        self.service.decide(2, request_id, DecisionInput(decision='rejected'))
        self.waitlists.join(1, self.pair)
        self.waitlists.demo_seat(1, self.pair)
        self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
        self.assertIn('rejected', self.waitlists.snapshot(1)['waitlists'][0]['reason'])

    def test_no_automatic_department_assignment_outside_simulation(self):
        with self.sessions.begin() as db:
            db.query(self.service.Profile).filter_by(user_id=1).delete()
        self.waitlists.academic = False
        self.assertIsNone(self.call('GET', '').json()['department'])
        self.assertEqual(self.call('POST', '/overrides', data=self.override.model_dump()).status_code, 409)

    def test_blank_reason_and_cross_department_request_are_rejected(self):
        self.assertEqual(self.call('POST', '/overrides', data={**self.override.model_dump(), 'reason':'     '}).status_code, 422)
        self.assertEqual(self.call('POST', '/overrides', user=5, data=self.override.model_dump()).status_code, 403)

    def test_course_catalog_includes_subjects_without_sections(self):
        response = self.call('GET', '')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({c['code'] for c in response.json()['courses']}, {'A', 'B'})
        self.assertEqual(next(c for c in response.json()['courses'] if c['code'] == 'B')['name'],
                         'Course without scheduled sections')

    def test_request_extra_seat_for_any_catalog_course_without_waitlist_selection(self):
        reason = 'I need this course to graduate this semester.'
        for course in ['B', 'A']:
            response = self.call('POST', '/seat-requests', data={'course':course, 'reason':reason})
            self.assertEqual(response.status_code, 200, response.text)
            request = next(r for r in response.json()['requests'] if r['course'] == course)
            self.assertEqual(request['reason'], reason)
            self.assertEqual(request['flags'], ['additional_seat'])
            self.assertEqual(request['status'], 'pending')
            self.assertEqual(request['lecture'], '')
            self.assertEqual(request['practice'], '')
            self.assertTrue(request['course_name'])
        self.assertEqual(len(self.call('GET', '', user=2).json()['requests']), 2)
        self.assertEqual(self.waitlists.snapshot(1)['waitlists'], [])

    def test_extra_seat_request_validation_duplicates_and_advisor_isolation(self):
        self.assertEqual(self.call('POST', '/seat-requests', data={'course':'UNKNOWN', 'reason':'Need another seat'}).status_code, 422)
        self.assertEqual(self.call('POST', '/seat-requests', data={'course':'B', 'reason':'     '}).status_code, 422)
        payload = {'course':'B', 'reason':'All sections are full and this course is required.'}
        result = self.call('POST', '/seat-requests', data=payload)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.call('POST', '/seat-requests', data=payload).status_code, 409)
        request_id = result.json()['requests'][0]['id']
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=3,
                                  data={'decision':'approved'}).status_code, 404)
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=2,
                                  data={'decision':'approved', 'comment':'Seat request reviewed'}).status_code, 422)
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=2,
                                  data={'decision':'rejected', 'comment':'Sections not published yet'}).status_code, 200)
        self.assertEqual(self.call('GET', '').json()['requests'][0]['status'], 'rejected')
        self.assertEqual(self.call('DELETE', f'/seat-requests/{request_id}').status_code, 409)

    def test_student_can_cancel_only_own_pending_extra_seat_request(self):
        result = self.call('POST', '/seat-requests', data={
            'course':'B', 'reason':'I need another seat for my study plan.'})
        self.assertEqual(result.status_code, 200, result.text)
        request_id = result.json()['requests'][0]['id']

        self.assertEqual(self.call('DELETE', f'/seat-requests/{request_id}', user=4).status_code, 404)
        self.assertEqual(self.call('DELETE', f'/seat-requests/{request_id}', user=2).status_code, 404)
        cancelled = self.call('DELETE', f'/seat-requests/{request_id}')
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()['requests'], [])
        self.assertEqual(self.call('GET', '', user=2).json()['requests'], [])

        resubmitted = self.call('POST', '/seat-requests', data={
            'course':'B', 'reason':'I still need another seat for my study plan.'})
        self.assertEqual(resubmitted.status_code, 200, resubmitted.text)
        self.assertEqual(len(resubmitted.json()['requests']), 1)

    def seat_request(self):
        response = self.call('POST', '/seat-requests', data={'course':'A', 'reason':'I need an extra seat for graduation.'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['requests'][0]['id']

    def approve_seat(self, request_id):
        return self.call('POST', f'/overrides/{request_id}/decision', user=2,
                         data={'decision':'approved', 'lecture':'A.L', 'practice':'A.P', 'comment':'One personal extra seat granted.'})

    def test_granted_seat_is_reserved_for_recipient_and_cannot_be_taken_by_queue(self):
        with self.sessions.begin() as db:
            db.get(self.service.Policy, 'A').restricted = 0
        self.waitlists.join(4, self.pair)
        request_id = self.seat_request()
        result = self.approve_seat(request_id)
        self.assertEqual(result.status_code, 200, result.text)
        request = self.call('GET', '').json()['requests'][0]
        self.assertEqual(request['seat_grant'], 'reserved')
        with self.sessions() as db:
            for section in self.waitlists.sections(db).values():
                self.assertEqual(section.quota, 2)
                self.assertEqual(section.reserved, 1)
        self.assertEqual(self.waitlists.snapshot(4)['enrollments'], [])
        self.assertEqual(self.call('POST', f'/seat-requests/{request_id}/enroll', user=4, data={}).status_code, 404)
        enrolled = self.call('POST', f'/seat-requests/{request_id}/enroll', data={})
        self.assertEqual(enrolled.status_code, 200, enrolled.text)
        self.assertEqual(enrolled.json()['requests'][0]['seat_grant'], 'used')
        self.assertEqual(len(self.waitlists.snapshot(1)['enrollments']), 1)
        self.assertEqual(self.waitlists.snapshot(4)['enrollments'], [])
        self.assertEqual(self.approve_seat(request_id).status_code, 409)
        self.call('POST', f'/seat-requests/{request_id}/enroll', data={})
        with self.sessions() as db:
            self.assertEqual(db.query(self.service.SeatGrant).count(), 1)
            for section in self.waitlists.sections(db).values():
                self.assertEqual(section.quota, 2)
                self.assertEqual(section.reserved, 0)

    def test_reject_does_not_increase_quotas_or_create_grant(self):
        request_id = self.seat_request()
        result = self.call('POST', f'/overrides/{request_id}/decision', user=2,
                           data={'decision':'rejected', 'comment':'No additional capacity available.'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.call('POST', f'/seat-requests/{request_id}/enroll', data={}).status_code, 404)
        with self.sessions() as db:
            self.assertEqual(db.query(self.service.SeatGrant).count(), 0)
            self.assertTrue(all(s.quota == 1 and s.reserved == 0 for s in self.waitlists.sections(db).values()))

    def test_role_boundaries_for_student_and_advisor(self):
        request_id = self.seat_request()
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision',
            data={'decision':'approved','lecture':'A.L','practice':'A.P'}).status_code, 403)
        self.assertEqual(self.call('POST', '/seat-requests', user=2, data={'course':'B','reason':'Not a student account'}).status_code, 403)
        with self.assertRaises(HTTPException) as denied:
            self.waitlists.join(2, self.pair)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(self.call('POST', f'/seat-requests/{request_id}/enroll', user=2, data={}).status_code, 403)
        with self.sessions.begin() as db:
            db.get(self.service.Profile, 1).role = 'advisor'
        self.assertEqual(self.call('POST', f'/overrides/{request_id}/decision', user=1,
            data={'decision':'approved','lecture':'A.L','practice':'A.P'}).status_code, 403)

    def test_hold_keeps_grant_reserved_until_student_becomes_eligible(self):
        request_id = self.seat_request()
        self.waitlists.demo_hold(1, True)
        self.assertEqual(self.approve_seat(request_id).status_code, 200)
        self.call('POST', f'/seat-requests/{request_id}/enroll', data={})
        self.assertEqual(self.waitlists.snapshot(1)['enrollments'], [])
        self.assertEqual(self.call('GET', '').json()['requests'][0]['seat_grant'], 'reserved')
        self.waitlists.demo_hold(1, False)
        self.assertEqual(len(self.waitlists.snapshot(1)['enrollments']), 1)
        self.assertEqual(self.call('GET', '').json()['requests'][0]['seat_grant'], 'used')

    def test_inventory_updates_preserve_personal_reservations(self):
        request_id = self.seat_request()
        self.assertEqual(self.approve_seat(request_id).status_code, 200)
        self.waitlists.source_event(InventoryUpdate(event_id='reserved-inventory', section='A.L',
            revision=1, quota=2, occupied=1, reserved=0), 'inventory')
        with self.sessions() as db:
            self.assertEqual(self.waitlists.sections(db)['A.L'].reserved, 1)
        with self.assertRaises(HTTPException):
            self.waitlists.source_event(InventoryUpdate(event_id='insufficient-capacity', section='A.L',
                revision=2, quota=1, occupied=1, reserved=0), 'inventory')

    def test_legacy_approval_can_receive_one_actual_seat(self):
        request_id = self.seat_request()
        with self.sessions.begin() as db:
            db.get(self.service.Override, request_id).status = 'approved'
        self.assertEqual(self.approve_seat(request_id).status_code, 200)
        self.assertEqual(self.approve_seat(request_id).status_code, 409)

    def test_in_app_alert_is_immediate_persisted_and_deduplicated_without_gateway(self):
        result = self.call('PUT', '/subscriptions', data={'section':'A.L','channels':['in_app']})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.call('GET', '/notifications').json()['unread'], 0)
        started = time.monotonic()
        self.inventory('inbox-open', 1, 0)
        inbox = self.call('GET', '/notifications').json()
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(inbox['unread'], 1)
        self.assertEqual(inbox['items'][0]['course'], 'A')
        self.assertEqual(inbox['items'][0]['section'], 'A.L')
        self.assertEqual(inbox['items'][0]['kind'], 'seat_available')
        self.inventory('inbox-open', 1, 0)
        self.inventory('inbox-still-open', 2, 0)
        self.assertEqual(len(self.call('GET', '/notifications').json()['items']), 1)
        with self.sessions() as db:
            self.assertEqual(db.query(self.service.Notice).count(), 1)
            self.assertEqual(db.query(self.waitlists.Outbox).count(), 0)
        other_client = TestClient(self.app)
        try:
            persisted = other_client.get('/api/registration/notifications', headers={'X-Test-User':'1'}).json()
            self.assertEqual(persisted['items'][0]['id'], inbox['items'][0]['id'])
        finally:
            other_client.close()

    def test_read_status_is_private_and_new_notices_remain_unread(self):
        for user in (1,4):
            self.assertEqual(self.call('PUT', '/subscriptions', user=user,
                data={'section':'A.L','channels':['in_app']}).status_code, 200)
        self.inventory('both-open', 1, 0)
        first = self.call('GET', '/notifications').json()['items'][0]['id']
        self.assertEqual(self.call('POST', '/notifications/read', user=4, data={'ids':[first]}).json()['unread'], 1)
        read = self.call('POST', '/notifications/read', data={'through_id':first})
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()['unread'], 0)
        self.assertTrue(read.json()['items'][0]['read'])
        self.assertEqual(self.call('GET', '/notifications', user=4).json()['unread'], 1)
        self.inventory('both-full', 2, 1)
        self.inventory('both-reopen', 3, 0)
        late_read = self.call('POST', '/notifications/read', data={'through_id':first}).json()
        self.assertEqual(late_read['unread'], 1)
        self.assertFalse(late_read['items'][0]['read'])

    def test_unsubscribe_stops_new_in_app_alerts_and_keeps_history(self):
        subscription = self.call('PUT', '/subscriptions', data={'section':'A.L','channels':['in_app']}).json()['subscriptions'][0]['id']
        self.inventory('unsub-open', 1, 0)
        self.call('DELETE', f'/subscriptions/{subscription}')
        self.inventory('unsub-full', 2, 1)
        self.inventory('unsub-open-again', 3, 0)
        self.assertEqual(len(self.call('GET', '/notifications').json()['items']), 1)

    def test_advisor_decision_notifies_only_requesting_student(self):
        request_id = self.seat_request()
        self.assertEqual(self.approve_seat(request_id).status_code, 200)
        notices = self.call('GET', '/notifications').json()
        self.assertEqual(notices['unread'], 1)
        self.assertEqual(notices['items'][0]['kind'], 'seat_granted')
        self.assertEqual(self.call('GET', '/notifications', user=4).json()['items'], [])
        self.assertEqual(self.approve_seat(request_id).status_code, 409)
        self.assertEqual(self.call('GET', '/notifications').json()['unread'], 1)

    def test_academic_demo_opens_subscribed_seat_without_joining_waitlist(self):
        sub = self.call('PUT', '/subscriptions', data={'section':'A.L','channels':['in_app']}).json()['subscriptions'][0]['id']
        self.assertEqual(self.call('POST', f'/subscriptions/{sub}/demo-seat', user=4, data={}).status_code, 404)
        self.assertEqual(self.call('POST', f'/subscriptions/{sub}/demo-seat', data={}).status_code, 200)
        self.assertEqual(self.call('GET', '/notifications').json()['unread'], 1)
        self.assertEqual(self.call('POST', f'/subscriptions/{sub}/demo-seat', data={}).status_code, 409)
        self.assertEqual(self.call('GET', '/notifications').json()['unread'], 1)
        self.assertEqual(self.waitlists.snapshot(1)['waitlists'], [])
        self.waitlists.academic = False
        self.assertEqual(self.call('POST', f'/subscriptions/{sub}/demo-seat', data={}).status_code, 404)


    def link_telegram(self, user=1):
        with self.sessions.begin() as db:
            db.add(self.service.TelegramLink(user_id=user, chat_id='123456'))

    def test_telegram_subscription_requires_link(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'123:fake'}):
            result = self.call('PUT', '/subscriptions', data={'section':'A.L','channels':['telegram']})
            self.assertEqual(result.status_code, 409)
            self.link_telegram()
            self.assertEqual(self.call('PUT', '/subscriptions', data={'section':'A.L','channels':['telegram']}).status_code, 200)
            self.inventory('telegram-open', 1, 0)
            with patch('telegram_delivery.deliver', return_value={'status':'delivered','provider_message_id':'42'}) as send:
                self.waitlists.dispatch()
                self.waitlists.dispatch()
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0], '123456')
                self.assertIn('A.L', send.call_args.args[1])
            notice = self.call('GET', '').json()['notifications'][0]
            self.assertEqual(notice['status'], 'delivered')
            self.assertEqual(notice['provider'], 'telegram')

    def test_pairing_is_private_expiring_and_single_use(self):
        from urllib.parse import urlparse, parse_qs
        with patch('telegram_delivery.call', return_value={'username':'TestNoticeBot'}):
            started = self.call('POST', '/telegram/link', data={})
        self.assertEqual(started.status_code, 200)
        token = parse_qs(urlparse(started.json()['url']).query)['start'][0]
        update = {'update_id':1,'message':{'date':int(time.time()),'text':'/start ' + token,
            'chat':{'id':123456,'type':'group'},'from':{'id':123456}}}
        with patch('telegram_delivery.call', return_value=[update]):
            self.assertEqual(self.call('POST', '/telegram/confirm', user=4, data={}).status_code, 409)
            self.assertEqual(self.call('POST', '/telegram/confirm', data={}).status_code, 409)
            update['message']['chat']['type'] = 'private'
            update['update_id'] = 2
            self.assertEqual(self.call('POST', '/telegram/confirm', data={}).status_code, 200)
            self.assertEqual(self.call('POST', '/telegram/confirm', data={}).status_code, 200)
        self.assertTrue(self.call('GET', '').json()['telegram_linked'])
        self.assertFalse(self.call('GET', '', user=4).json()['telegram_linked'])
        with patch('telegram_delivery.call', return_value={'username':'TestNoticeBot'}):
            self.call('POST', '/telegram/link', data={})
        with self.sessions.begin() as db:
            db.get(self.service.TelegramLink, 1).expires_at = 1
        self.assertEqual(self.call('POST', '/telegram/confirm', data={}).status_code, 409)

    def test_pairing_updates_for_multiple_users_are_processed_together(self):
        from urllib.parse import urlparse, parse_qs
        with patch('telegram_delivery.call', return_value={'username':'TestNoticeBot'}):
            first = self.call('POST', '/telegram/link', user=1, data={}).json()
            second = self.call('POST', '/telegram/link', user=4, data={}).json()
        self.assertTrue(first['qr_svg'].startswith('data:image/svg+xml;base64,'))
        first_token = parse_qs(urlparse(first['url']).query)['start'][0]
        second_token = parse_qs(urlparse(second['url']).query)['start'][0]
        def event(uid, chat, token):
            return {'update_id':uid, 'message':{'date':int(time.time()), 'text':'/start ' + token,
                'chat':{'id':chat,'type':'private'}, 'from':{'id':chat}}}
        events = [event(20, 111, first_token), event(21, 444, second_token)]
        with patch('telegram_delivery.call', return_value=events) as call:
            self.assertEqual(self.call('POST', '/telegram/confirm', user=1, data={}).status_code, 200)
            self.assertEqual(call.call_args.args[1]['offset'], 0)
        self.assertTrue(self.call('GET', '', user=4).json()['telegram_linked'])
        with self.sessions() as db:
            self.assertEqual(db.get(self.service.TelegramLink, 1).chat_id, '111')
            self.assertEqual(db.get(self.service.TelegramLink, 4).chat_id, '444')
            self.assertEqual(db.get(self.service.TelegramCursor, 1).offset, 22)
        with patch('telegram_delivery.call', return_value=[]) as call:
            self.service.process_telegram_updates()
            self.assertEqual(call.call_args.args[1]['offset'], 22)

    def test_telegram_failure_retry_and_disconnect(self):
        import telegram_delivery
        self.link_telegram()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'123:fake'}):
            self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['telegram']))
        self.inventory('telegram-fail', 1, 0)
        with patch('telegram_delivery.deliver', side_effect=telegram_delivery.TelegramError(429, retry_after=40)):
            self.waitlists.dispatch()
        event = self.call('GET', '').json()['notifications'][0]
        self.assertEqual(event['status'], 'pending')
        self.assertEqual(event['last_error'], 'Telegram 429')
        self.assertEqual(self.call('DELETE', '/telegram/link').status_code, 200)
        self.assertFalse(self.call('GET', '').json()['telegram_linked'])
        self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'cancelled')
        with patch('telegram_delivery.deliver') as send:
            self.waitlists.dispatch()
            send.assert_not_called()

    def test_telegram_permanent_failure_stops_retrying(self):
        import telegram_delivery
        self.link_telegram()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'123:fake'}):
            self.service.subscribe(1, SubscriptionInput(section='A.L', channels=['telegram']))
        self.inventory('telegram-blocked', 1, 0)
        with patch('telegram_delivery.deliver', side_effect=telegram_delivery.TelegramError(403, permanent=True)) as send:
            self.waitlists.dispatch()
            self.waitlists.dispatch()
            send.assert_called_once()
        self.assertEqual(self.call('GET', '').json()['notifications'][0]['status'], 'failed')

    def test_retired_channel_cleanup_preserves_other_notifications(self):
        with self.sessions.begin() as db:
            db.add(self.service.Subscription(user_id=1, term=self.waitlists.term, section='A.L',
                channels=['sms','email'], legacy_contact='+12345678901'))
            db.add(self.waitlists.Outbox(key='old-channel', user_id=1, channel='sms', payload={'message':'old'}))
        self.service.retire_legacy_channel()
        with self.sessions() as db:
            sub = db.query(self.service.Subscription).one()
            self.assertEqual(sub.channels, ['email'])
            self.assertEqual(sub.legacy_contact, '')
            self.assertEqual(db.query(self.waitlists.Outbox).count(), 0)


if __name__ == '__main__':
    unittest.main()
