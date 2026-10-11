import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class WaitlistApiTests(unittest.TestCase):
    def test_authenticated_demo_and_protected_inventory(self):
        directory = tempfile.TemporaryDirectory()
        environment = {'DATABASE_URL':'sqlite:///' + directory.name.replace('\\','/') + '/api.db',
            'WAITLIST_MODE':'academic', 'UNIVERSITY_WEBHOOK_TOKEN':'test-only-webhook-token'}
        with patch.dict(os.environ, environment), patch('mailer.send_email', return_value=True) as mail:
            sys.modules.pop('main', None)
            import main
            try:
                current = main.default_curriculum()
                original = current['categories'][0]['required'][0]
                stale = {**original, 'schedule':{'lecture_sections':[]}}
                saved = main.CurriculumModel(name='Saved', categories=[{'required':[stale]}],
                    schedule_revision=None, progress=None, updated_at=0)
                refreshed = main.public_curriculum(saved)['categories'][0]['required'][0]
                self.assertEqual(refreshed['schedule'], original['schedule'])
                saved.schedule_revision = current['schedule_revision']
                self.assertEqual(main.public_curriculum(saved)['categories'][0]['required'][0]['schedule'], stale['schedule'])
                with TestClient(main.app) as first:
                    second = TestClient(main.app)
                    anonymous = TestClient(main.app)
                    self.assertEqual(anonymous.get('/api/waitlists').status_code, 401)
                    for client, email in ((first,'first@example.test'),(second,'second@example.test')):
                        response = client.post('/api/auth/register',json={'name':'Test Student','email':email,'password':'Testing123!'})
                        self.assertEqual(response.status_code, 201, response.text)
                        token = re.search(r'#verify-token=([^\s]+)', mail.call_args.args[2]).group(1)
                        self.assertEqual(client.post('/api/auth/verify-email',json={'token':token}).status_code,200)
                        self.assertEqual(client.post('/api/auth/login',json={'email':email,'password':'Testing123!'}).status_code,200)
                    initial = first.get('/api/waitlists').json()
                    self.assertEqual(initial['mode'], 'academic')
                    lecture = next(s for s in initial['sections'] if not s['parent'])
                    practice = next(s for s in initial['sections'] if s['parent']==lecture['section'])
                    pair = {'lecture':lecture['section'],'practice':practice['section']}
                    self.assertEqual(first.post('/api/waitlists',json={**pair,'practice':'not-a-section'}).status_code,422)
                    self.assertEqual(first.post('/api/waitlists',json=pair).status_code,200)
                    queued = second.post('/api/waitlists',json=pair).json()['waitlists'][0]
                    self.assertEqual(queued['position'], 2)
                    self.assertEqual(first.delete('/api/waitlists/' + str(queued['id'])).status_code,404)
                    response = first.post('/api/waitlists/demo/seat',json=pair)
                    self.assertEqual(response.status_code,200,response.text)
                    enrolled = response.json()['enrollments'][0]
                    self.assertEqual(second.get('/api/waitlists').json()['enrollments'],[])
                    self.assertEqual(first.delete('/api/enrollments/' + str(enrolled['id'])).status_code,200)
                    promoted = second.get('/api/waitlists').json()
                    self.assertEqual(len(promoted['enrollments']),1)
                    inventory = {'event_id':'protected','section':lecture['section'],'revision':10,
                        'quota':lecture['quota'],'occupied':0,'reserved':0}
                    self.assertEqual(second.post('/api/integrations/university/inventory',json=inventory).status_code,401)
                    accepted = anonymous.post('/api/integrations/university/inventory',json=inventory,
                        headers={'Authorization':'Bearer test-only-webhook-token'})
                    self.assertEqual(accepted.status_code,200,accepted.text)
                    deadline = time.time()+3
                    while time.time()<deadline:
                        messages = second.get('/api/waitlists').json()['notifications']
                        if messages and all(n['status']==('delivered' if n['channel']=='email' else 'simulated') for n in messages):
                            break
                        time.sleep(.02)
                    self.assertEqual({n['channel'] for n in messages},{'email','billing'})
                    self.assertTrue(all(n['status']==('delivered' if n['channel']=='email' else 'simulated') for n in messages))
            finally:
                main.engine.dispose()
                directory.cleanup()


if __name__ == '__main__':
    unittest.main()
