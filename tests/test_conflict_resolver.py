"""US7 — Class Time Conflict Resolver (US7QATest scenarios and test scripts)."""
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from conflict_resolver import install_conflict_resolver, meeting_overlap
from registration_service import install_registration
from waitlist_service import WaitlistService


def meeting(day, start, end, week='every_week'):
    return {'day_of_week':day, 'start_time':start, 'end_time':end, 'week_pattern':week}


def section(code, meetings, quota=30, count=0):
    return {'section':code, 'quota':quota, 'count':count, 'reserved':0, 'available_places':quota - count,
            'availability':'Available', 'meetings':meetings}


def lecture(code, meetings, practices, **seats):
    return {**section(code, meetings, **seats), 'practice_sections':practices}


CURRICULUM = {'categories':[{'category':'Семестр 1', 'required':[
    {'code':'CS101', 'name':'Intro to Computer Science', 'credits':5, 'schedule':{'lecture_sections':[
        lecture('CS101.01', [meeting(1, '09:00', '10:50')], [
            section('CS101.02', [meeting(3, '09:00', '09:50')]),
            section('CS101.03', [meeting(4, '11:00', '11:50')])]),
        lecture('CS101.04', [meeting(2, '14:00', '14:50'), meeting(4, '14:00', '14:50')], [
            section('CS101.05', [meeting(5, '15:00', '15:50')])]),
    ]}},
    {'code':'MDE102', 'name':'Academic English', 'credits':5, 'schedule':{'lecture_sections':[
        lecture('MDE102.01', [meeting(1, '10:00', '11:50')], [section('MDE102.02', [meeting(5, '09:00', '09:50')])]),
        lecture('MDE102.03', [meeting(3, '13:00', '14:50')], [section('MDE102.04', [meeting(5, '10:00', '10:50')], quota=1)]),
        lecture('MDE102.05', [meeting(2, '09:00', '10:50')], [section('MDE102.06', [meeting(5, '12:00', '12:50')])],
                quota=1, count=1),
    ]}},
    {'code':'MAT101', 'name':'Calculus I', 'credits':5, 'schedule':{'lecture_sections':[
        lecture('MAT101.01', [meeting(1, '09:00', '09:50')], []),
    ]}},
    {'code':'PHY101', 'name':'Physics I', 'credits':5, 'schedule':{'lecture_sections':[
        lecture('PHY101.01', [meeting(1, '09:00', '09:50', 'odd_weeks')], []),
        lecture('PHY101.02', [meeting(1, '09:00', '09:50', 'even_weeks')], []),
    ]}},
    {'code':'HIS100', 'name':'Course without published sections', 'credits':3},
]}]}


class ConflictResolverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.directory.name.replace('\\', '/') + '/draft.db',
                                    connect_args={'check_same_thread':False})
        base = declarative_base()

        class User(base):
            __tablename__ = 'users'
            id = Column(Integer, primary_key=True)
            name = Column(String)
            email = Column(String)
            email_verified = Column(Integer, default=1)

        self.sessions = sessionmaker(bind=self.engine)
        self.waitlists = WaitlistService(base, self.sessions, User, CURRICULUM, academic=True)
        app = FastAPI()

        def get_user(request: Request):
            if not request.headers.get('X-Test-User'):
                raise HTTPException(401, 'Login required')
            with self.sessions() as db:
                return db.get(User, int(request.headers['X-Test-User']))

        registration = install_registration(app, base, self.waitlists, get_user, CURRICULUM)
        self.service = install_conflict_resolver(app, base, self.waitlists, registration, get_user, CURRICULUM)
        with self.sessions.begin() as db:
            db.add_all([User(id=i, name=f'User {i}', email=f'user{i}@example.test') for i in range(1, 5)])
            db.add_all([registration.Profile(user_id=i, role=role, department='CS')
                        for i, role in [(1, 'student'), (2, 'advisor'), (3, 'student'), (4, 'student')]])
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.directory.cleanup()

    def call(self, method, path, user=1, data=None):
        return self.client.request(method, '/api/draft' + path, json=data, headers={'X-Test-User':str(user)})

    def put(self, course, lecture, practice='', user=1):
        return self.call('PUT', '/courses/' + course, user, {'lecture':lecture, 'practice':practice})

    def enroll(self, user_id, course, lecture, practice=''):
        meetings = self.service.meetings(lecture, practice)
        with self.sessions.begin() as db:
            db.add(self.waitlists.Enrollment(term=self.waitlists.term, user_id=user_id, course=course,
                lecture=lecture, practice=practice, meetings=meetings, status='enrolled', enrolled_at=int(time.time())))

    def assert_fits(self, option, draft):
        others = [m for item in draft['items'] + draft['enrolled'] if item['course'] != option['course']
                  for s in item['sections'] for m in s['meetings']]
        self.assertFalse(any(meeting_overlap(a, b) for a in option['meetings'] for b in others), option)

    def test_scenario_1_conflict_is_flagged_with_open_alternatives(self):
        self.assertEqual(self.put('CS101', 'CS101.01', 'CS101.02').status_code, 200)
        draft = self.put('MDE102', 'MDE102.01', 'MDE102.02').json()
        self.assertFalse(draft['summary']['clean'])
        self.assertEqual(draft['summary']['conflicts'], 1)
        conflict = draft['conflicts'][0]
        self.assertEqual({conflict['a']['section'], conflict['b']['section']}, {'CS101.01', 'MDE102.01'})
        self.assertEqual((conflict['day'], conflict['start'], conflict['end']), ('Mon', '10:00', '10:50'))
        self.assertTrue(conflict['resolvable'])
        self.assertTrue(all(item['status'] == 'conflict' for item in draft['items']))
        # Alternatives are offered for BOTH courses in the overlap and never overlap the rest of the draft.
        self.assertIn({'lecture':'CS101.04', 'practice':'CS101.05'},
                      [{'lecture':o['lecture'], 'practice':o['practice']} for o in draft['suggestions']['CS101']])
        mde = [(o['lecture'], o['practice']) for o in draft['suggestions']['MDE102']]
        self.assertEqual(mde, [('MDE102.03', 'MDE102.04')])
        for options in draft['suggestions'].values():
            for option in options:
                self.assert_fits(option, draft)
        self.assertLess(draft['elapsed_ms'], 3000)

    def test_scenario_2_one_click_substitution_clears_the_conflict(self):
        self.put('CS101', 'CS101.01', 'CS101.02')
        self.put('MDE102', 'MDE102.01', 'MDE102.02')
        result = self.call('POST', '/resolve', data={'course':'CS101', 'lecture':'CS101.04', 'practice':'CS101.05'})
        self.assertEqual(result.status_code, 200, result.text)
        draft = result.json()
        self.assertTrue(draft['summary']['clean'])
        self.assertEqual(draft['conflicts'], [])
        self.assertEqual(draft['suggestions'], {})
        cs = next(item for item in draft['items'] if item['course'] == 'CS101')
        self.assertEqual((cs['lecture'], cs['practice'], cs['status']), ('CS101.04', 'CS101.05', 'ok'))
        self.assertIn('replaced', draft['message'])
        # The saved draft (and so the weekly grid) is recalculated from the new sections.
        self.assertEqual(self.call('GET', '').json()['items'], draft['items'])

    def test_full_and_waitlisted_sections_are_never_suggested(self):
        self.put('CS101', 'CS101.01', 'CS101.02')
        self.put('MDE102', 'MDE102.01', 'MDE102.02')
        # MDE102.05 is full in the catalog; MDE102.04 fills up through another student's live enrollment.
        self.enroll(4, 'MDE102', 'MDE102.03', 'MDE102.04')
        with self.sessions.begin() as db:
            db.add(self.waitlists.Entry(term=self.waitlists.term, user_id=3, course='CS101', lecture='CS101.04',
                practice='CS101.05', queue_order=1, status='waiting', created_at=int(time.time())))
        draft = self.call('GET', '').json()
        suggested = {s for options in draft['suggestions'].values() for o in options for s in (o['lecture'], o['practice'])}
        self.assertTrue(suggested.isdisjoint({'MDE102.04', 'MDE102.05', 'MDE102.06', 'CS101.04', 'CS101.05'}), suggested)
        self.assertFalse(draft['conflicts'][0]['resolvable'])
        catalog = {s['section']:s for c in self.call('GET', '/catalog').json()['courses']
                   for lec in c['lectures'] for s in [lec] + lec['practices']}
        self.assertEqual(catalog['MDE102.04']['status'], 'full')
        self.assertEqual(catalog['MDE102.05']['status'], 'full')
        self.assertEqual(catalog['CS101.05']['status'], 'waitlisted')

    def test_failed_substitution_keeps_the_draft_unchanged(self):
        self.put('CS101', 'CS101.01', 'CS101.02')
        self.put('MDE102', 'MDE102.01', 'MDE102.02')
        before = self.call('GET', '').json()
        self.assertIn('MDE102.04', [o['practice'] for o in before['suggestions']['MDE102']])
        self.enroll(4, 'MDE102', 'MDE102.03', 'MDE102.04')  # the last seat is taken by someone else
        result = self.call('POST', '/resolve', data={'course':'MDE102', 'lecture':'MDE102.03', 'practice':'MDE102.04'})
        self.assertEqual(result.status_code, 409)
        self.assertIn('Target section MDE102.04 is now full', result.json()['detail'])
        after = self.call('GET', '').json()
        self.assertEqual([(i['course'], i['lecture'], i['practice']) for i in after['items']],
                         [(i['course'], i['lecture'], i['practice']) for i in before['items']])
        overlapping = self.call('POST', '/resolve', data={'course':'MDE102', 'lecture':'MDE102.01', 'practice':'MDE102.02'})
        self.assertEqual(overlapping.status_code, 409)
        self.assertIn('overlaps CS101', overlapping.json()['detail'])

    def test_adding_validates_sections_and_seats(self):
        self.assertEqual(self.put('MDE102', 'MDE102.05', 'MDE102.06').status_code, 409)
        self.assertEqual(self.put('CS101', 'CS101.01', 'CS101.05').status_code, 422)
        self.assertEqual(self.put('CS101', 'CS101.01').status_code, 422)
        self.assertEqual(self.put('HIS100', 'HIS100.01').status_code, 404)
        self.assertEqual(self.put('CS101', 'MDE102.01', 'MDE102.02').status_code, 422)
        self.assertEqual(self.call('DELETE', '/courses/CS101').status_code, 404)
        self.assertEqual(self.put('CS101', 'CS101.01', 'CS101.03').status_code, 200)
        changed = self.put('CS101', 'CS101.04', 'CS101.05').json()
        self.assertEqual(len(changed['items']), 1)
        self.assertEqual(changed['items'][0]['lecture'], 'CS101.04')
        self.assertEqual(self.call('DELETE', '/courses/CS101').json()['items'], [])

    def test_confirmed_enrollments_are_locked_and_respected(self):
        self.enroll(1, 'MAT101', 'MAT101.01')
        draft = self.put('CS101', 'CS101.01', 'CS101.02').json()
        self.assertEqual([e['course'] for e in draft['enrolled']], ['MAT101'])
        self.assertTrue(draft['enrolled'][0]['locked'])
        conflict = draft['conflicts'][0]
        self.assertEqual({conflict['a']['locked'], conflict['b']['locked']}, {True, False})
        self.assertNotIn('MAT101', draft['suggestions'])
        for option in draft['suggestions']['CS101']:
            self.assertNotEqual(option['lecture'], 'CS101.01')
            self.assert_fits(option, draft)
        self.assertEqual(self.put('MAT101', 'MAT101.01').status_code, 409)

    def test_alternate_week_patterns_do_not_conflict(self):
        self.put('PHY101', 'PHY101.01')
        draft = self.put('MAT101', 'MAT101.01').json()
        self.assertEqual(draft['summary']['conflicts'], 1)  # every-week lesson overlaps odd weeks
        self.call('DELETE', '/courses/MAT101')
        self.enroll(1, 'MAT101', 'MAT101.01')
        self.call('DELETE', '/courses/PHY101')
        self.assertEqual(self.put('PHY101', 'PHY101.02').json()['summary']['conflicts'], 1)
        with self.sessions.begin() as db:
            db.query(self.waitlists.Enrollment).delete()
        self.service.sections['MAT101.01']['meetings'] = [meeting(1, '09:00', '09:50', 'odd_weeks')]
        self.enroll(1, 'MAT101', 'MAT101.01')
        self.assertTrue(self.call('GET', '').json()['summary']['clean'])

    def test_drafts_are_private_and_student_only(self):
        self.put('CS101', 'CS101.01', 'CS101.02')
        self.assertEqual(self.call('GET', '', user=3).json()['items'], [])
        self.assertEqual(self.call('POST', '/resolve', user=3,
            data={'course':'CS101', 'lecture':'CS101.04', 'practice':'CS101.05'}).status_code, 404)
        self.assertEqual(self.call('GET', '', user=2).status_code, 403)
        self.assertEqual(self.client.get('/api/draft').status_code, 401)
        cleared = self.call('DELETE', '').json()
        self.assertEqual(cleared['items'], [])

    def test_import_copies_a_generated_schedule(self):
        result = self.call('POST', '/import', data={'items':[
            {'course':'CS101', 'lecture':'CS101.04', 'practice':'CS101.05'},
            {'course':'MDE102', 'lecture':'MDE102.01', 'practice':'MDE102.02'}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['summary']['clean'])
        self.assertEqual(result.json()['summary']['credits'], 10)
        rejected = self.call('POST', '/import', data={'items':[{'course':'MDE102', 'lecture':'MDE102.05', 'practice':'MDE102.06'}]})
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(len(self.call('GET', '').json()['items']), 2)


class BundledCatalogTests(unittest.TestCase):
    """Runs the real application with the bundled curriculum (performance test script)."""

    def test_complex_multi_course_overlaps_are_analysed_quickly(self):
        directory = tempfile.TemporaryDirectory()
        environment = {'DATABASE_URL':'sqlite:///' + directory.name.replace('\\', '/') + '/app.db', 'WAITLIST_MODE':'academic'}
        with patch.dict(os.environ, environment), patch('mailer.send_email', return_value=True) as mail:
            sys.modules.pop('main', None)
            import main
            try:
                with TestClient(main.app) as client:
                    client.post('/api/auth/register', json={'name':'Student', 'email':'draft@example.test', 'password':'Testing123!'})
                    token = re.search(r'#verify-token=([^\s]+)', mail.call_args.args[2]).group(1)
                    client.post('/api/auth/verify-email', json={'token':token})
                    client.post('/api/auth/login', json={'email':'draft@example.test', 'password':'Testing123!'})
                    catalog = client.get('/api/draft/catalog').json()
                    self.assertGreater(len(catalog['courses']), 100)
                    # Seven courses all placed on their first sections produce many overlaps at once.
                    for course in catalog['courses'][:7]:
                        lecture = course['lectures'][0]
                        practice = lecture['practices'][0]['section'] if lecture['practices'] else ''
                        response = client.put('/api/draft/courses/' + course['code'],
                                              json={'lecture':lecture['section'], 'practice':practice})
                        self.assertEqual(response.status_code, 200, response.text)
                    started = time.perf_counter()
                    draft = client.get('/api/draft').json()
                    self.assertLess(time.perf_counter() - started, 3)
                    self.assertEqual(draft['summary']['courses'], 7)
                    for code, options in draft['suggestions'].items():
                        for option in options:
                            others = [m for item in draft['items'] if item['course'] != code
                                      for s in item['sections'] for m in s['meetings']]
                            self.assertFalse(any(meeting_overlap(a, b) for a in option['meetings'] for b in others))
            finally:
                main.engine.dispose()
                directory.cleanup()


if __name__ == '__main__':
    unittest.main()
