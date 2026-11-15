import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import test_waitlists
from schedule_actions import CalendarRange, export_calendar, install_schedule_actions, swap_section
from waitlist_service import JoinRequest


class ScheduleActionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_waitlists.WaitlistTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.pair = self.fixture.pair
        self.service.join(1, self.pair)
        self.service.demo_seat(1, self.pair)
        self.enrollment = self.service.snapshot(1)['enrollments'][0]
        with self.service.transaction() as db:
            for section, parent, day in [('A.L2', None, 3), ('A.P2', 'A.L2', 4)]:
                db.add(self.service.Section(term=self.service.term, section=section, course='A',
                    name='Course A', parent=parent, quota=1, occupied=0, reserved=0,
                    meetings=[{'day_of_week':day, 'start_time':'09:00', 'end_time':'09:50',
                               'location':'Room 101, East; wing'}]))
        self.target = JoinRequest(lecture='A.L2', practice='A.P2')
        self.dates = CalendarRange(start='2026-11-16', end='2026-11-29')

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def swap(self, user=1):
        swap_section(self.service, user, self.enrollment['id'], self.target)

    def test_atomic_swap_and_release_promotes_waiter(self):
        self.service.join(2, self.pair)
        self.swap()
        current = self.service.snapshot(1)['enrollments'][0]
        self.assertEqual(current['id'], self.enrollment['id'])
        self.assertEqual(current['lecture'], 'A.L2')
        self.assertEqual(self.service.snapshot(2)['enrollments'][0]['lecture'], 'A.L')
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['lecture'], 'A.L2')

    def test_full_target_keeps_original(self):
        self.service.join(2, self.target)
        with self.assertRaises(HTTPException) as error:
            self.swap()
        self.assertEqual(error.exception.detail, 'Target section is now full')
        self.assertEqual(self.service.snapshot(1)['enrollments'][0], self.enrollment)

    def test_api_full_target_and_same_sections_return_visible_errors(self):
        from types import SimpleNamespace
        app = FastAPI()
        install_schedule_actions(app, self.service, lambda: SimpleNamespace(id=1))
        self.service.join(2, self.target)
        with TestClient(app) as client:
            path = f"/api/enrollments/{self.enrollment['id']}/swap"
            same = client.post(path, json=self.pair.model_dump())
            self.assertEqual(same.status_code, 409)
            self.assertIn('already enrolled', same.json()['detail'])
            full = client.post(path, json=self.target.model_dump())
            self.assertEqual(full.status_code, 409)
            self.assertEqual(full.json()['detail'], 'Target section is now full')
        self.assertEqual(self.service.snapshot(1)['enrollments'][0], self.enrollment)

    def test_failure_after_write_rolls_back_both_sections(self):
        with patch.object(self.service, 'promote', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                self.swap()
        self.assertEqual(self.service.snapshot(1)['enrollments'][0], self.enrollment)
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['lecture'], 'A.L')
        target = next(s for s in self.service.snapshot(1)['sections'] if s['section'] == 'A.L2')
        self.assertEqual(target['available'], 1)

    def test_owner_course_hold_and_conflict_validation(self):
        with self.assertRaises(HTTPException) as error:
            self.swap(user=2)
        self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            swap_section(self.service, 1, self.enrollment['id'], JoinRequest(lecture='B.L', practice='B.P'))
        self.service.demo_hold(1, True)
        with self.assertRaises(HTTPException):
            self.swap()
        self.assertEqual(self.service.snapshot(1)['enrollments'][0], self.enrollment)

    def test_concurrent_swaps_for_last_seat(self):
        with self.service.transaction() as db:
            for section in db.query(self.service.Section).filter(self.service.Section.section.in_(['A.L','A.P'])):
                section.quota += 1
        self.service.join(2, self.pair)
        second = self.service.snapshot(2)['enrollments'][0]
        def attempt(args):
            try:
                swap_section(self.service, args[0], args[1], self.target)
                return True
            except HTTPException:
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, [(1,self.enrollment['id']), (2,second['id'])]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(sum(len(self.service.snapshot(i)['enrollments']) for i in (1,2)), 2)

    def test_conflict_with_another_enrollment_prevents_swap(self):
        with self.service.transaction() as db:
            db.add(self.service.Enrollment(term=self.service.term, user_id=1, course='B',
                lecture='B.L', practice='B.P', status='enrolled', enrolled_at=1,
                meetings=[{'day_of_week':3, 'start_time':'09:20', 'end_time':'10:00'}]))
        with self.assertRaises(HTTPException) as error:
            self.swap()
        self.assertIn('conflicts', error.exception.detail)
        current = next(e for e in self.service.snapshot(1)['enrollments'] if e['course'] == 'A')
        self.assertEqual(current, self.enrollment)

    def test_changing_only_practice_reuses_existing_lecture_seat(self):
        with self.service.transaction() as db:
            section = db.query(self.service.Section).filter_by(section='A.P2').one()
            section.parent = 'A.L'
        swap_section(self.service, 1, self.enrollment['id'], JoinRequest(lecture='A.L', practice='A.P2'))
        current = self.service.snapshot(1)['enrollments'][0]
        self.assertEqual((current['lecture'], current['practice']), ('A.L','A.P2'))

    def test_calendar_current_confirmed_schedule_and_escaping(self):
        self.swap()
        text = export_calendar(self.service, 1, self.dates).decode()
        self.assertEqual(text.count('BEGIN:VEVENT'), 4)
        self.assertIn('DTSTART:20261118T040000Z', text)
        self.assertIn('LOCATION:Room 101\\, East\\; wing', text)
        self.assertIn('SUMMARY:A · Course A', text)
        self.assertNotIn('Section: A.L;', text)
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split('\r\n')))

    def test_calendar_empty_and_pending_blocked_and_week_parity(self):
        with self.assertRaises(HTTPException):
            export_calendar(self.service, 2, self.dates)
        self.service.join(1, JoinRequest(lecture='B.L', practice='B.P'))
        with self.assertRaises(HTTPException) as error:
            export_calendar(self.service, 1, self.dates)
        self.assertEqual(error.exception.detail, 'Schedule must be fully confirmed before export')
        entry = next(e for e in self.service.snapshot(1)['waitlists'] if e['course'] == 'B')
        self.service.cancel(1, entry['id'])
        with self.service.transaction() as db:
            section = db.query(self.service.Section).filter_by(section='A.L').one()
            section.meetings = [{**section.meetings[0], 'week_pattern':'even_weeks'}]
        text = export_calendar(self.service, 1, self.dates).decode()
        self.assertEqual(text.count('BEGIN:VEVENT'), 3)
        self.assertNotIn('DTSTART:20261116T040000Z', text)
        self.assertIn('DTSTART:20261123T040000Z', text)

    def test_api_returns_confirmation_and_calendar_download(self):
        from types import SimpleNamespace
        app = FastAPI()
        install_schedule_actions(app, self.service, lambda: SimpleNamespace(id=1))
        with TestClient(app) as client:
            response = client.post(f"/api/enrollments/{self.enrollment['id']}/swap", json=self.target.model_dump())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('successfully', response.json()['message'])
            result = client.post('/api/enrollments/calendar', json={'start':'2026-11-16','end':'2026-11-29'})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn('text/calendar', result.headers['content-type'])
            self.assertIn('.ics', result.headers['content-disposition'])
            self.assertEqual(client.post('/api/enrollments/calendar', json={'start':'2026-11-29','end':'2026-11-16'}).status_code, 422)
