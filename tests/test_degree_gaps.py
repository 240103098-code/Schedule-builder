"""US8 — Degree Audit Gap Visualizer (US8QATest scenarios and test scripts)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from conflict_resolver import install_conflict_resolver
from degree_gaps import CROSS_DEPARTMENT, build_audit, install_degree_gaps
from registration_service import install_registration
from waitlist_service import WaitlistService


def course(code, credits, status=None, kind='required', **extra):
    return {'code':code, 'name':'Course ' + code, 'credits':credits, 'kind':kind, 'completion_status':status,
            'completed':status == 'completed', 'in_progress':status == 'in_progress', **extra}


def offered(code, quota=30, count=0):
    return {'lecture_sections':[{'section':code + '.01', 'quota':quota, 'count':count, 'reserved':0,
        'availability':'Available', 'meetings':[{'day_of_week':1, 'start_time':'09:00', 'end_time':'09:50'}],
        'practice_sections':[{'section':code + '.02', 'quota':quota, 'count':count, 'reserved':0,
            'availability':'Available', 'meetings':[{'day_of_week':2, 'start_time':'09:00', 'end_time':'09:50'}]}]}]}


def elective(code, slot, status=None, elective_type='AE', **extra):
    return course(code, 5, status, kind='elective', eligible_slots=[slot], elective_type=elective_type, **extra)


# 90 of 240 ECTS completed (US8QATest scenario 1).
STUDENT_CURRICULUM = {'name':'Information Systems 2026', 'categories':[
    {'category':'Семестр 1', 'required':[
        course('CSS 101', 30, 'completed', semester=1), course('CSS 102', 30, 'completed', semester=2),
        course('MDE 101', 20, 'completed', semester=1), course('MDE 102', 10, 'completed', semester=2)]},
    {'category':'Семестр 3', 'required':[
        course('CSS 201', 10, 'in_progress', semester=3),
        course('CSS 301', 40, semester=4, prerequisites=[{'code':'CSS 102'}], schedule=offered('CSS 301')),
        course('CSS 302', 20, semester=4, prerequisites=[{'code':'CSS 401'}], schedule=offered('CSS 302')),
        course('CSS 303', 25, semester=5, prerequisites=[{'code':'CSS 201'}], schedule=offered('CSS 303')),
        course('CSS 401', 25, semester=6),
        course('MDE 201', 20, semester=4, schedule=offered('MDE 201', quota=1, count=1))]},
    {'category':'Элективы (AE)', 'required':[
        elective('INF 201', 'Сем. 3: Elective 1', 'completed', grade='F', schedule=offered('INF 201')),
        elective('INF 202', 'Сем. 3: Elective 1'),
        elective('INF 203', 'Сем. 4: Elective 2', schedule=offered('INF 203'))]},
    {'category':'Подготовительные курсы (RC)', 'required':[course('MDE 001', 6, kind='requisite')]},
]}

OTHER_CURRICULUM = {'name':'Business', 'categories':[{'category':'Core', 'required':[course('BUS 101', 5, 'completed')]}]}


class AuditMappingTests(unittest.TestCase):
    def test_breakdown_by_category_matches_the_transcript(self):
        audit = build_audit(STUDENT_CURRICULUM)
        totals = audit['totals']
        self.assertEqual((totals['total'], totals['completed'], totals['in_progress']), (240, 90, 10))
        self.assertEqual(totals['missing'], 140)
        self.assertEqual(totals['excluded_requisite_courses'], 1)
        groups = {g['key']:g for g in audit['groups']}
        self.assertEqual(list(groups), ['Core Major', 'General Education', 'Electives'])
        self.assertEqual((groups['Core Major']['completed'], groups['Core Major']['in_progress'],
                          groups['Core Major']['missing']), (60, 10, 110))
        self.assertEqual((groups['General Education']['completed'], groups['General Education']['missing']), (30, 20))
        self.assertEqual(groups['Electives']['missing'], 10)  # a failing grade does not complete an elective slot
        self.assertEqual(audit['term'], {'current_semester':3, 'upcoming_semester':4})

    def test_completed_units_are_never_reported_as_missing(self):
        curriculum = {'name':'Equivalents', 'categories':[{'category':'Семестр 1', 'required':[
            course('CSS 105', 5, semester=1, equivalents=[{'code':'CSS 100'}]),
            course('CSS 100', 5, 'completed', kind='requisite'),
            course('CSS 106', 5, 'completed', semester=1, grade='A'),
            course('CSS 107', 5, 'completed', semester=1, grade='P')]}]}
        audit = build_audit(curriculum)
        self.assertEqual(audit['totals']['missing'], 0)
        satisfied = next(r for r in audit['requirements'] if r['code'] == 'CSS 105')
        self.assertEqual((satisfied['status'], satisfied['satisfied_by']), ('completed', 'CSS 100'))

    def test_bundled_curriculum_maps_to_its_own_progress_summary(self):
        directory = tempfile.TemporaryDirectory()
        environment = {'DATABASE_URL':'sqlite:///' + directory.name.replace('\\', '/') + '/bundled.db'}
        with patch.dict(os.environ, environment):
            sys.modules.pop('main', None)
            import main
            try:
                curriculum = main.default_curriculum()
            finally:
                main.engine.dispose()
                directory.cleanup()
        audit = build_audit(curriculum)
        progress = curriculum['progress']
        self.assertEqual(audit['totals']['total'], progress['total_ects'])
        self.assertEqual(audit['totals']['completed'], progress['completed_ects'])
        self.assertEqual(audit['totals']['in_progress'], progress['in_progress_ects'])
        self.assertEqual(sum(audit['totals']['requirements'].values()), progress['total_positions'])
        codes = {c['code']:c for cat in curriculum['categories'] for c in cat['required']}
        for requirement in audit['requirements']:
            for code in requirement['candidates']:
                self.assertNotEqual(codes[code].get('completion_status'), 'completed')


class GapVisualizerApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.directory.name.replace('\\', '/') + '/gaps.db',
                                    connect_args={'check_same_thread':False})
        base = declarative_base()

        class User(base):
            __tablename__ = 'users'
            id = Column(Integer, primary_key=True)
            name = Column(String)
            email = Column(String)
            email_verified = Column(Integer, default=1)

        sessions = sessionmaker(bind=self.engine)
        self.waitlists = WaitlistService(base, sessions, User, STUDENT_CURRICULUM, academic=True)
        app = FastAPI()

        def get_user(request: Request):
            if not request.headers.get('X-Test-User'):
                raise HTTPException(401, 'Login required')
            with sessions() as db:
                return db.get(User, int(request.headers['X-Test-User']))

        registration = install_registration(app, base, self.waitlists, get_user, STUDENT_CURRICULUM)
        resolver = install_conflict_resolver(app, base, self.waitlists, registration, get_user, STUDENT_CURRICULUM)
        curricula = {1:STUDENT_CURRICULUM, 5:OTHER_CURRICULUM}
        install_degree_gaps(app, sessions, registration, resolver, User, get_user,
                            lambda db, user_id: (curricula.get(user_id, STUDENT_CURRICULUM), 'uploaded'))
        with sessions.begin() as db:
            db.add_all([User(id=i, name=name, email=f'user{i}@example.test') for i, name in
                        [(1, 'Aru Student'), (2, 'CS Advisor'), (3, 'Business Advisor'), (4, 'Bek Student'), (5, 'Business Student')]])
            db.add_all([registration.Profile(user_id=i, role=role, department=dept) for i, role, dept in
                        [(1, 'student', 'CS'), (2, 'advisor', 'CS'), (3, 'advisor', 'BUS'),
                         (4, 'student', 'CS'), (5, 'student', 'BUS')]])
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.directory.cleanup()

    def get(self, path, user):
        return self.client.get('/api/advisor' + path, headers={'X-Test-User':str(user)})

    def test_scenario_1_visual_breakdown_for_a_department_student(self):
        roster = self.get('/students', 2)
        self.assertEqual(roster.status_code, 200, roster.text)
        self.assertEqual([s['name'] for s in roster.json()['students']], ['Aru Student', 'Bek Student'])
        data = self.get('/students/1/gaps', 2).json()
        self.assertEqual((data['totals']['completed'], data['totals']['total']), (90, 240))
        self.assertEqual({g['key'] for g in data['groups']}, {'Core Major', 'General Education', 'Electives'})
        for group in data['groups']:
            self.assertEqual(group['completed'] + group['in_progress'] + group['missing'], group['total'])

    def test_scenario_2_missing_core_filter_and_upcoming_term_availability(self):
        data = self.get('/students/1/gaps', 2).json()
        missing_core = [r for r in data['requirements'] if r['group'] == 'Core Major' and r['status'] == 'missing']
        self.assertEqual(sorted(r['code'] for r in missing_core), ['CSS 301', 'CSS 302', 'CSS 303', 'CSS 401'])
        options = {code:data['candidates'][code] for r in missing_core for code in r['candidates']}
        self.assertTrue(options['CSS 301']['available_next_term'])           # open sections, prerequisite met
        self.assertTrue(options['CSS 303']['available_next_term'])           # prerequisite still in progress
        self.assertEqual(options['CSS 303']['prerequisites'][0]['status'], 'in_progress')
        self.assertFalse(options['CSS 302']['available_next_term'])          # missing prerequisite
        self.assertFalse(options['CSS 302']['prerequisites_met'])
        self.assertFalse(options['CSS 401']['offered'])                      # no sections published
        self.assertFalse(data['candidates']['MDE 201']['available_next_term'])  # section is full
        self.assertEqual(data['candidates']['MDE 201']['open_pairs'], 0)
        elective_slot = next(r for r in data['requirements'] if r['id'] == 'slot:Сем. 3: Elective 1')
        self.assertEqual(elective_slot['status'], 'missing')
        self.assertEqual(elective_slot['candidates'], ['INF 201', 'INF 202'])
        self.assertEqual(data['totals']['available_next_term'], 4)  # CSS 301, CSS 303, INF 201, INF 203

    def test_scenario_3_access_is_limited_to_the_advisors_department(self):
        denied = self.get('/students/5/gaps', 2)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['detail'], CROSS_DEPARTMENT)
        self.assertEqual(self.get('/students/1/gaps', 3).json()['detail'], CROSS_DEPARTMENT)
        self.assertEqual([s['id'] for s in self.get('/students', 3).json()['students']], [5])
        self.assertEqual(self.get('/students/1/gaps', 1).status_code, 403)   # students cannot use the advisor view
        self.assertEqual(self.get('/students', 4).status_code, 403)
        self.assertEqual(self.get('/students/2/gaps', 2).status_code, 404)   # advisors are not students
        self.assertEqual(self.get('/students/99/gaps', 2).status_code, 404)
        self.assertEqual(self.client.get('/api/advisor/students').status_code, 401)


if __name__ == '__main__':
    unittest.main()
