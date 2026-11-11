"""US3 — Instant Prerequisite Check (US3QATest scenarios and test scripts)."""
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from prerequisites import evaluate, install_prerequisite_check


def course(code, status=None, grade=None, prerequisites=(), **extra):
    return {'code':code, 'name':'Course ' + code, 'credits':5, 'completion_status':status,
            'completed':status == 'completed', 'in_progress':status == 'in_progress', 'grade':grade,
            'prerequisites':list(prerequisites), **extra}


def curriculum(*courses):
    return {'name':'Test', 'categories':[{'category':'Core', 'required':list(courses)}]}


class PrerequisiteRuleTests(unittest.TestCase):
    def test_scenario_1_missing_prerequisite_blocks_the_course(self):
        result = evaluate(curriculum(course('CS101'), course('CS201', prerequisites=[{'code':'CS101'}])), ['CS201'])['CS201']
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['message'], 'Missing Prerequisite: CS101')
        self.assertFalse(result['can_add'])

    def test_scenario_2_passed_with_c_or_higher_allows_the_course(self):
        for grade in ('C', 'B+', 'A', 'P', None):
            result = evaluate(curriculum(course('CS101', 'completed', grade), course('CS201', prerequisites=[{'code':'CS101'}])),
                              ['CS201'])['CS201']
            self.assertEqual((result['status'], result['can_add']), ('met', True), grade)

    def test_failing_or_low_grade_and_in_progress_do_not_satisfy_a_prerequisite(self):
        for grade in ('D', 'D+', 'F', 'FX'):
            result = evaluate(curriculum(course('CS101', 'completed', grade), course('CS201', prerequisites=[{'code':'CS101'}])),
                              ['CS201'])['CS201']
            self.assertEqual(result['status'], 'missing', grade)
            self.assertIn('below the required C-', result['missing'][0]['detail'])
        result = evaluate(curriculum(course('CS101', 'in_progress'), course('CS201', prerequisites=[{'code':'CS101'}])), ['CS201'])
        self.assertEqual(result['CS201']['missing'][0]['state'], 'in_progress')

    def test_scenario_3_corequisite_in_the_same_cart_is_satisfied(self):
        data = curriculum(course('MATH101'), course('PHYS101', prerequisites=[{'code':'MATH101', 'type':'corequisite'}]))
        self.assertEqual(evaluate(data, ['PHYS101'])['PHYS101']['status'], 'missing')
        both = evaluate(data, ['PHYS101', 'MATH101'], cart=['MATH101', 'PHYS101'])
        self.assertEqual(both['PHYS101']['status'], 'met')
        self.assertEqual(both['MATH101']['status'], 'none')
        self.assertEqual(both['PHYS101']['checks'][0]['state'], 'in_cart')
        enrolled = curriculum(course('MATH101', 'in_progress'), course('PHYS101', prerequisites=[{'code':'MATH101', 'corequisite':True}]))
        self.assertEqual(evaluate(enrolled, ['PHYS101'])['PHYS101']['status'], 'met')

    def test_completed_course_is_never_flagged_and_equivalents_count(self):
        data = curriculum(course('CS100', 'completed', 'B'), course('CS101', equivalents=[{'code':'CS100'}]),
                          course('CS201', prerequisites=[{'code':'CS101'}]))
        self.assertEqual(evaluate(data, ['CS201'])['CS201']['status'], 'met')
        self.assertEqual(evaluate(data, ['CS100'])['CS100']['status'], 'none')


class PrerequisiteApiTests(unittest.TestCase):
    def test_endpoint_uses_the_signed_in_students_record(self):
        app = FastAPI()
        records = {1:curriculum(course('CS101', 'completed', 'A'), course('CS201', prerequisites=[{'code':'CS101'}])),
                   2:curriculum(course('CS101'), course('CS201', prerequisites=[{'code':'CS101'}]))}

        class User:
            def __init__(self, user_id):
                self.id = user_id

        def get_user(request: Request):
            if not request.headers.get('X-Test-User'):
                raise HTTPException(401, 'Login required')
            return User(int(request.headers['X-Test-User']))

        class Session:
            def __enter__(self):
                return None

            def __exit__(self, *args):
                return False

        install_prerequisite_check(app, get_user, lambda db, user_id: (records[user_id], 'uploaded'), Session)
        client = TestClient(app)
        payload = {'courses':['CS201'], 'cart':['CS201']}
        self.assertEqual(client.post('/api/prerequisites/check', json=payload).status_code, 401)
        passed = client.post('/api/prerequisites/check', json=payload, headers={'X-Test-User':'1'}).json()
        self.assertEqual(passed['results']['CS201']['status'], 'met')
        blocked = client.post('/api/prerequisites/check', json=payload, headers={'X-Test-User':'2'}).json()
        self.assertEqual(blocked['results']['CS201']['message'], 'Missing Prerequisite: CS101')

    def test_uploaded_corequisite_type_survives_curriculum_save(self):
        directory = tempfile.TemporaryDirectory()
        environment = {'DATABASE_URL':'sqlite:///' + directory.name.replace('\\', '/') + '/prereq.db'}
        with patch.dict(os.environ, environment), patch('mailer.send_email', return_value=True) as mail:
            sys.modules.pop('main', None)
            import main
            try:
                with TestClient(main.app) as client:
                    client.post('/api/auth/register', json={'name':'Student', 'email':'p@example.test', 'password':'Testing123!'})
                    token = re.search(r'#verify-token=([^\s]+)', mail.call_args.args[2]).group(1)
                    client.post('/api/auth/verify-email', json={'token':token})
                    client.post('/api/auth/login', json={'email':'p@example.test', 'password':'Testing123!'})
                    saved = client.put('/api/curriculum', json={'name':'Physics', 'categories':[{'category':'Core', 'required':[
                        {'code':'MATH101', 'name':'Calculus I', 'credits':5},
                        {'code':'PHYS101', 'name':'Physics I', 'credits':5,
                         'prerequisites':[{'code':'MATH101', 'name':'Calculus I', 'type':'corequisite'}]}]}]})
                    self.assertEqual(saved.status_code, 200, saved.text)
                    result = client.post('/api/prerequisites/check', json={'courses':['PHYS101'], 'cart':['PHYS101', 'MATH101']}).json()
                    self.assertEqual(result['results']['PHYS101']['status'], 'met')
                    alone = client.post('/api/prerequisites/check', json={'courses':['PHYS101'], 'cart':['PHYS101']}).json()
                    self.assertEqual(alone['results']['PHYS101']['missing'][0]['kind'], 'corequisite')
            finally:
                main.engine.dispose()
                directory.cleanup()


if __name__ == '__main__':
    unittest.main()
