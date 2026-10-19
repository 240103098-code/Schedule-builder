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
        subscription = {'section':'A.L', 'channels':['push','sms','email'], 'phone':'+77001234567'}
        self.assertEqual(self.call('PUT', '/subscriptions', data={**subscription, 'phone':'bad'}).status_code, 422)
        result = self.call('PUT', '/subscriptions', data=subscription)
        self.assertEqual(result.status_code, 200, result.text)
        sub_id = result.json()['subscriptions'][0]['id']
        started = time.monotonic()
        self.inventory('open', 1, 0)
        notices = self.call('GET', '').json()['notifications']
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual({n['channel'] for n in notices}, {'push','sms','email'})
        self.inventory('open', 1, 0)
        self.inventory('still-open', 2, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 3)
        self.inventory('full', 3, 1)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 3)
        self.inventory('open-again', 4, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 6)
        self.assertEqual(self.call('DELETE', f'/subscriptions/{sub_id}', user=4).status_code, 404)
        self.assertEqual(self.call('DELETE', f'/subscriptions/{sub_id}').status_code, 200)
        self.inventory('full-again', 5, 1)
        self.inventory('no-subscriber', 6, 0)
        self.assertEqual(len(self.call('GET', '').json()['notifications']), 6)
        self.assertTrue(all(n['status'] == 'cancelled' for n in self.call('GET', '').json()['notifications']))

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
                                  data={'decision':'approved', 'comment':'Seat request reviewed'}).status_code, 200)
        self.assertEqual(self.call('GET', '').json()['requests'][0]['status'], 'approved')


if __name__ == '__main__':
    unittest.main()
