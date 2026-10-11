import copy
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from waitlist_service import HoldUpdate, InventoryUpdate, JoinRequest, WaitlistService


class WaitlistTests(unittest.TestCase):
    def setUp(self):
        self.mail_patch = patch('mailer.send_email', return_value=True)
        self.mail_patch.start()
        self.addCleanup(self.mail_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + self.directory.name.replace('\\', '/') + '/test.db',
            connect_args={'check_same_thread': False, 'timeout': 30})
        base = declarative_base()

        class User(base):
            __tablename__ = 'users'
            id = Column(Integer, primary_key=True)
            email = Column(String, nullable=False)

        self.User = User
        self.sessions = sessionmaker(bind=self.engine)
        def subject(code):
            return {'code':code, 'name':code, 'schedule':{'lecture_sections':[{
                'section':code + '.L', 'quota':1, 'count':1, 'reserved':0,
                'meetings':[{'day_of_week':1,'start_time':'09:00','end_time':'09:50'}],
                'practice_sections':[{'section':code + '.P', 'quota':1, 'count':1,'reserved':0,
                    'meetings':[{'day_of_week':2,'start_time':'10:00','end_time':'10:50'}]}]}]}}
        self.service = WaitlistService(base, self.sessions, User,
            {'categories':[{'required':[subject('A'),subject('B')]}]}, academic=True)
        with self.sessions.begin() as db:
            db.add_all([User(id=i, email=f'student{i}@example.test') for i in range(1,41)])
        self.pair = JoinRequest(lecture='A.L', practice='A.P')

    def tearDown(self):
        self.service.stop.set()
        self.service.wake.set()
        if self.service.worker:
            self.service.worker.join(timeout=5)
        self.engine.dispose()
        self.directory.cleanup()

    def test_alternating_weeks_and_schedule_refresh_preserve_queue_and_seats(self):
        odd = {'day_of_week':1, 'start_time':'17:30', 'end_time':'18:20', 'week_pattern':'odd_weeks'}
        even = {**odd, 'week_pattern':'even_weeks'}
        self.assertFalse(self.service.conflicts([odd], [even]))
        self.assertTrue(self.service.conflicts([odd], [{**even, 'week_pattern':'every_week'}]))
        self.service.join(1, self.pair)
        self.service.join(2, self.pair)
        self.service.sync_curriculum({'categories':[{'required':[{'code':'A', 'name':'Updated A',
            'schedule':{'lecture_sections':[{'section':'A.L', 'quota':1, 'meetings':[odd],
                'practice_sections':[{'section':'A.P', 'quota':1, 'meetings':[even]}]}]}}]}]})
        snapshot = self.service.snapshot(2)
        self.assertEqual(snapshot['waitlists'][0]['position'], 2)
        section = next(row for row in snapshot['sections'] if row['section'] == 'A.L')
        self.assertEqual(section['available'], 0)
        self.assertEqual(section['meetings'], [odd])
        self.service.demo_seat(1, self.pair)
        with self.sessions() as db:
            enrollment = db.query(self.service.Enrollment).filter_by(user_id=1, status='enrolled').one()
            self.assertEqual(enrollment.meetings, [odd, even])

    def test_drop_promotes_position_one_and_records_email_and_billing(self):
        for user in (1,2,3):
            self.service.join(user, self.pair)
        self.assertEqual(self.service.snapshot(2)['waitlists'][0]['position'], 2)
        self.service.demo_seat(1, self.pair)
        first = self.service.snapshot(1)['enrollments'][0]
        self.service.drop(1, first['id'])
        self.assertEqual(len(self.service.snapshot(2)['enrollments']), 1)
        self.assertEqual(self.service.snapshot(3)['waitlists'][0]['position'], 1)
        self.assertEqual(self.service.snapshot(1)['enrollments'], [])
        self.service.dispatch()
        messages = self.service.snapshot(2)['notifications']
        self.assertEqual({m['channel'] for m in messages}, {'email','billing'})
        self.assertTrue(all(m['status'] == ('delivered' if m['channel']=='email' else 'simulated') for m in messages))
        # Duplicate drops and joins cannot consume another seat or notify twice.
        self.service.drop(1, first['id'])
        self.service.join(2, self.pair)
        self.assertEqual(len(self.service.snapshot(2)['notifications']), 2)

    def test_hold_preserves_fifo_and_clearing_it_promotes(self):
        self.service.demo_hold(1, True)
        self.service.join(1, self.pair)
        self.service.join(2, self.pair)
        self.service.demo_seat(2, self.pair)
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['status'], 'blocked')
        self.assertEqual(self.service.snapshot(2)['enrollments'], [])
        self.assertTrue(self.service.snapshot(1)['notifications'])
        self.service.demo_hold(1, False)
        self.assertEqual(len(self.service.snapshot(1)['enrollments']), 1)
        self.assertEqual(self.service.snapshot(2)['enrollments'], [])

    def test_conflict_warns_without_skipping_student(self):
        self.service.join(1, self.pair)
        self.service.demo_seat(1, self.pair)
        other = JoinRequest(lecture='B.L',practice='B.P')
        self.service.join(1, other)
        self.service.join(2, other)
        self.service.demo_seat(2, other)
        blocked = self.service.snapshot(1)['waitlists'][1]
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIn('Schedule conflict', blocked['reason'])
        self.assertEqual(self.service.snapshot(2)['enrollments'], [])
        self.assertTrue(any('Schedule conflict' in n['message'] for n in self.service.snapshot(1)['notifications']))
        self.service.drop(1, self.service.snapshot(1)['enrollments'][0]['id'])
        self.assertEqual(self.service.snapshot(1)['enrollments'][0]['course'], 'B')

    def test_parallel_joins_and_drops_do_not_overbook_or_reorder(self):
        # Distinct process-style locks ensure SQLite, not just a Python mutex,
        # protects seat allocation and queue order across workers.
        other_worker = copy.copy(self.service)
        other_worker.lock = threading.RLock()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda user: (self.service if user % 2 else other_worker).join(user,self.pair), range(1,25)))
        with self.service.transaction() as db:
            order = [e.user_id for e in db.query(self.service.Entry).order_by(self.service.Entry.queue_order)]
        self.service.demo_seat(order[0], self.pair)
        enrolled = []
        for user in order:
            state = self.service.snapshot(user)
            self.assertEqual(len(state['enrollments']), 1)
            enrolled.append(user)
            enrollment_id = state['enrollments'][0]['id']
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda index: (self.service if index % 2 else other_worker).drop(user,enrollment_id), range(4)))
        self.assertEqual(enrolled, order)
        with self.service.transaction() as db:
            self.assertEqual(len(self.service.active(db)), 0)
        self.assertEqual(self.service.snapshot(1)['sections'][0]['available'], 1)

    def test_inventory_replay_revision_and_atomic_pair(self):
        self.service.join(1, self.pair)
        update = InventoryUpdate(event_id='seat-1',section='A.L',revision=1,quota=1,occupied=0,reserved=0)
        self.service.source_event(update, 'inventory')
        self.assertEqual(self.service.snapshot(1)['enrollments'], [], 'Practice is still full')
        self.service.source_event(update, 'inventory')
        with self.assertRaises(HTTPException):
            self.service.source_event(update.model_copy(update={'event_id':'stale'}), 'inventory')
        self.service.source_event(update.model_copy(update={'event_id':'seat-2','section':'A.P'}), 'inventory')
        self.assertEqual(len(self.service.snapshot(1)['enrollments']), 1)
        with self.assertRaises(HTTPException):
            self.service.source_event(update.model_copy(update={'event_id':'overbook','revision':2,'occupied':1}), 'inventory')

    def test_warning_deduplication_cancellation_and_rejoining(self):
        self.service.demo_hold(1, True)
        self.service.join(1,self.pair)
        for _ in range(3):
            with self.service.transaction() as db:
                self.service.promote(db)
        self.assertEqual(len(self.service.snapshot(1)['notifications']), 1)
        self.service.join(2,self.pair)
        self.service.demo_seat(2,self.pair)
        self.service.cancel(1,self.service.snapshot(1)['waitlists'][0]['id'])
        self.assertEqual(len(self.service.snapshot(2)['enrollments']), 1)
        self.service.join(1,self.pair)
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['position'], 1)
        self.assertEqual(len(self.service.snapshot(1)['notifications']), 2)

    def test_failed_deliveries_are_retried_not_marked_sent(self):
        self.service.join(1,self.pair)
        self.service.demo_seat(1,self.pair)
        with patch.object(self.service, 'deliver', side_effect=RuntimeError('provider down')):
            self.service.dispatch()
        self.assertTrue(all(n['status']=='pending' for n in self.service.snapshot(1)['notifications']))
        with self.service.transaction() as db:
            for row in db.query(self.service.Outbox):
                row.next_attempt = 0
        self.service.dispatch()
        self.assertTrue(all(n['status']==('delivered' if n['channel']=='email' else 'simulated') for n in self.service.snapshot(1)['notifications']))

    def test_background_worker_dispatches_and_recovers_queue(self):
        self.service.join(1,self.pair)
        self.service.demo_seat(1,self.pair)
        self.service.worker = threading.Thread(target=self.service.run, daemon=True)
        self.service.worker.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            if all(n['status']==('delivered' if n['channel']=='email' else 'simulated') for n in self.service.snapshot(1)['notifications']):
                break
            time.sleep(.02)
        self.assertTrue(all(n['status']==('delivered' if n['channel']=='email' else 'simulated') for n in self.service.snapshot(1)['notifications']))

    def test_unverified_holds_fail_closed_outside_academic_mode(self):
        self.service.academic = False
        self.service.join(1,self.pair)
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['status'], 'blocked')
        self.assertIn('verified', self.service.snapshot(1)['waitlists'][0]['reason'])
        self.service.source_event(HoldUpdate(event_id='hold-1',user_id=1,revision=1,on_hold=False), 'hold')
        self.assertEqual(self.service.snapshot(1)['waitlists'][0]['status'], 'waiting')


if __name__ == '__main__':
    unittest.main()
