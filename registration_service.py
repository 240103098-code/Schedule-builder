"""Advisor routing and opt-in seat alerts sharing enrollment transactions."""
import os
import re
import time
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Integer, Float, String, JSON, UniqueConstraint


class OverrideInput(BaseModel):
    lecture: str = Field(min_length=1, max_length=100)
    practice: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=5, max_length=1000)


class DecisionInput(BaseModel):
    decision: Literal['approved', 'rejected']
    comment: str = Field(default='', max_length=1000)


class SeatRequestInput(BaseModel):
    course: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=5, max_length=1000)


class SubscriptionInput(BaseModel):
    section: str = Field(min_length=1, max_length=100)
    channels: list[Literal['push', 'sms', 'email']] = Field(min_length=1, max_length=3)
    phone: str = Field(default='', max_length=20)


class RegistrationService:
    def __init__(self, base, waitlists, curriculum):
        self.waitlists = waitlists
        self.credits = {c['code']: float(c.get('credits', 0))
                        for cat in curriculum['categories'] for c in cat['required']}
        self.catalog = {c['code']: c['name'] for cat in curriculum['categories'] for c in cat['required']}

        class Profile(base):
            __tablename__ = 'registration_profiles'
            user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
            role = Column(String(20), nullable=False, default='student')
            department = Column(String(40), nullable=False)

        class Policy(base):
            __tablename__ = 'registration_policies'
            course = Column(String(40), primary_key=True)
            department = Column(String(40), nullable=False)
            restricted = Column(Integer, nullable=False, default=0)

        class Override(base):
            __tablename__ = 'registration_overrides'
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
            course = Column(String(40), nullable=False)
            lecture = Column(String(100), nullable=False)
            practice = Column(String(100), nullable=False)
            department = Column(String(40), nullable=False, index=True)
            flags = Column(JSON, nullable=False)
            credit_total = Column(Float, nullable=False)
            reason = Column(String(1000), nullable=False)
            status = Column(String(20), nullable=False, default='pending')
            reviewer_id = Column(Integer, ForeignKey('users.id'))
            comment = Column(String(1000), nullable=False, default='')
            created_at = Column(Integer, nullable=False)
            decided_at = Column(Integer)
            __table_args__ = (UniqueConstraint('term', 'user_id', 'course'),)

        class Subscription(base):
            __tablename__ = 'seat_subscriptions'
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
            section = Column(String(100), nullable=False)
            channels = Column(JSON, nullable=False)
            phone = Column(String(20), nullable=False, default='')
            active = Column(Integer, nullable=False, default=1)
            generation = Column(Integer, nullable=False, default=0)
            __table_args__ = (UniqueConstraint('term', 'user_id', 'section'),)

        self.Profile, self.Policy, self.Override, self.Subscription = Profile, Policy, Override, Subscription
        base.metadata.create_all(bind=waitlists.sessions.kw['bind'])
        waitlists.registration = self

    def student_profile(self, db, user_id):
        """Local academic simulations need usable student defaults, never advisor privileges."""
        profile = db.get(self.Profile, user_id)
        if not profile and self.waitlists.academic:
            if not db.get(self.waitlists.user_model, user_id):
                raise HTTPException(404, 'Student not found.')
            department = os.getenv('REGISTRATION_DEFAULT_DEPARTMENT', 'CS').strip().upper()
            if not department or len(department) > 40:
                raise HTTPException(503, 'Configure REGISTRATION_DEFAULT_DEPARTMENT (1–40 characters).')
            profile = self.Profile(user_id=user_id, role='student', department=department)
            db.add(profile)
            db.flush()
        return profile

    def flags(self, db, user_id, lecture, active):
        policy = db.get(self.Policy, lecture.course)
        flags = ['restricted'] if policy and policy.restricted else []
        courses = {e.course for e in active if e.user_id == user_id} | {lecture.course}
        total = sum(self.credits.get(code, 0) for code in courses)
        if total > int(os.getenv('REGISTRATION_CREDIT_LIMIT', '30')):
            flags.append('overload')
        return flags, total, policy

    def block_reason(self, db, entry, lecture, active):
        flags, total, policy = self.flags(db, entry.user_id, lecture, active)
        row = db.query(self.Override).filter_by(term=self.waitlists.term,
            user_id=entry.user_id, course=entry.course).first()
        if row and row.status != 'approved':
            return 'Advisor review ' + row.status + '. Approval is required before enrollment.'
        if not flags:
            return ''
        profile = db.get(self.Profile, entry.user_id)
        if (row and row.status == 'approved' and profile and profile.department == row.department
                and (not policy or policy.department == row.department)
                and row.lecture == entry.lecture and row.practice == entry.practice
                and set(flags) <= set(row.flags) and ('overload' not in flags or total <= row.credit_total)):
            return ''
        return 'Advisor approval required: ' + ', '.join(flags) + ('. Request ' + row.status if row else '')

    def pair(self, db, payload):
        sections = self.waitlists.sections(db)
        lecture, practice = sections.get(payload.lecture), sections.get(payload.practice)
        if not lecture or not practice or lecture.parent or practice.parent != lecture.section:
            raise HTTPException(422, 'Select a lecture and its linked practice.')
        if self.waitlists.conflicts(lecture.meetings, practice.meetings):
            raise HTTPException(422, 'These lecture and practice meetings overlap.')
        return lecture, practice

    def request_override(self, user_id, payload):
        if len(payload.reason.strip()) < 5:
            raise HTTPException(422, 'Explain why an override is needed (at least 5 characters).')
        with self.waitlists.transaction() as db:
            profile = self.student_profile(db, user_id)
            if not profile or profile.role != 'student':
                raise HTTPException(409, 'A student department must be assigned by an administrator.')
            lecture, _ = self.pair(db, payload)
            flags, total, policy = self.flags(db, user_id, lecture, self.waitlists.active(db))
            if not flags:
                flags = ['manual_review']
            department = policy.department if policy else profile.department
            if department != profile.department:
                raise HTTPException(403, 'Course and student departments must match for advisor routing.')
            row = db.query(self.Override).filter_by(term=self.waitlists.term, user_id=user_id, course=lecture.course).first()
            if row:
                raise HTTPException(409, 'A request already exists for this course.')
            enrollment = db.query(self.waitlists.Enrollment).filter_by(term=self.waitlists.term,
                user_id=user_id, course=lecture.course, status='enrolled').first()
            if enrollment:
                raise HTTPException(409, 'You are already enrolled in this course.')
            db.add(self.Override(term=self.waitlists.term, user_id=user_id, course=lecture.course,
                lecture=payload.lecture, practice=payload.practice, department=department, flags=flags,
                credit_total=total, reason=payload.reason.strip(), created_at=int(time.time())))

    def request_seat(self, user_id, payload):
        course = payload.course.strip().upper()
        reason = payload.reason.strip()
        if course not in self.catalog:
            raise HTTPException(422, 'Choose a course from the course catalog.')
        if len(reason) < 5:
            raise HTTPException(422, 'Explain why you need an extra seat (at least 5 characters).')
        with self.waitlists.transaction() as db:
            profile = self.student_profile(db, user_id)
            if not profile or profile.role != 'student':
                raise HTTPException(409, 'A student department must be assigned by an administrator.')
            if db.query(self.Override).filter_by(term=self.waitlists.term, user_id=user_id, course=course).first():
                raise HTTPException(409, 'A request already exists for this course.')
            if db.query(self.waitlists.Enrollment).filter_by(term=self.waitlists.term,
                    user_id=user_id, course=course, status='enrolled').first():
                raise HTTPException(409, 'You are already enrolled in this course.')
            # This is a course-level seat request, not a selection of waitlist sections.
            # Route to the student's assigned advisor; no course-wide quota is changed.
            db.add(self.Override(term=self.waitlists.term, user_id=user_id, course=course,
                lecture='', practice='', department=profile.department, flags=['additional_seat'],
                credit_total=self.credits[course], reason=reason, created_at=int(time.time())))

    def decide(self, advisor_id, request_id, payload):
        with self.waitlists.transaction() as db:
            profile = db.get(self.Profile, advisor_id)
            if not profile or profile.role != 'advisor':
                raise HTTPException(403, 'Advisor role required.')
            row = db.query(self.Override).filter_by(id=request_id, term=self.waitlists.term,
                department=profile.department).first()
            if not row:
                raise HTTPException(404, 'Request not found in your department.')
            student = db.get(self.Profile, row.user_id)
            if not student or student.department != profile.department or row.user_id == advisor_id:
                raise HTTPException(403, 'Student is outside your assigned department.')
            if row.status != 'pending':
                raise HTTPException(409, 'This request has already been decided.')
            row.status, row.comment = payload.decision, payload.comment.strip()
            row.reviewer_id, row.decided_at = advisor_id, int(time.time())
            db.flush()
            self.waitlists.promote(db)
        self.waitlists.wake.set()

    def subscribe(self, user_id, payload):
        phone = payload.phone.strip()
        if 'sms' in payload.channels and not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
            raise HTTPException(422, 'SMS requires an international phone number, e.g. +77001234567.')
        with self.waitlists.transaction() as db:
            section = self.waitlists.sections(db).get(payload.section)
            if not section:
                raise HTTPException(404, 'Section not found.')
            if self.waitlists.free(section, self.waitlists.active(db)):
                raise HTTPException(409, 'Seats are available already. Subscribe when the section is full.')
            row = db.query(self.Subscription).filter_by(term=self.waitlists.term, user_id=user_id, section=payload.section).first()
            if not row:
                row = self.Subscription(term=self.waitlists.term, user_id=user_id, section=payload.section)
                db.add(row)
            row.channels, row.phone, row.active = sorted(set(payload.channels)), phone if 'sms' in payload.channels else '', 1

    def unsubscribe(self, user_id, subscription_id):
        with self.waitlists.transaction() as db:
            row = db.query(self.Subscription).filter_by(id=subscription_id, user_id=user_id, term=self.waitlists.term).first()
            if not row:
                raise HTTPException(404, 'Subscription not found.')
            row.active = 0
            for event in db.query(self.waitlists.Outbox).filter_by(user_id=user_id, status='pending'):
                if event.payload.get('subscription_id') == row.id:
                    event.status = 'cancelled'

    def seat_opened(self, db, before):
        active = self.waitlists.active(db)
        for section in self.waitlists.sections(db).values():
            if before.get(section.section) != 0 or self.waitlists.free(section, active) == 0:
                continue
            for sub in db.query(self.Subscription).filter_by(term=self.waitlists.term, section=section.section, active=1):
                sub.generation += 1
                for channel in sub.channels:
                    db.add(self.waitlists.Outbox(key=f'seat:{sub.id}:{sub.generation}:{channel}', user_id=sub.user_id,
                        channel=channel, payload={'event':'seat_available', 'term':self.waitlists.term,
                            'section':section.section, 'subscription_id':sub.id, 'phone':sub.phone,
                            'message':f'A seat opened in {section.course}, section {section.section}. Availability may change; waitlist order still applies.'}))
        db.flush()

    def snapshot(self, user_id):
        with self.waitlists.transaction() as db:
            profile = self.student_profile(db, user_id)
            role = profile.role if profile else 'student'
            query = db.query(self.Override).filter_by(term=self.waitlists.term)
            query = query.filter_by(department=profile.department) if role == 'advisor' else query.filter_by(user_id=user_id)
            requests = []
            for row in query.order_by(self.Override.created_at.desc(), self.Override.id.desc()):
                student = db.get(self.waitlists.user_model, row.user_id)
                requests.append({k: getattr(row, k) for k in ('id', 'course', 'lecture', 'practice', 'department',
                    'flags', 'credit_total', 'reason', 'status', 'comment', 'created_at', 'decided_at')} |
                    {'student':student.name, 'course_name':self.catalog.get(row.course, row.course)})
            return {'role':role, 'department':profile.department if profile else None,
                'courses':[{'code':code, 'name':name} for code, name in sorted(self.catalog.items())],
                'gateway_configured':bool(os.getenv('UNIVERSITY_NOTIFICATION_URL')), 'requests':requests,
                'subscriptions':[{'id':s.id, 'section':s.section, 'channels':s.channels} for s in
                    db.query(self.Subscription).filter_by(term=self.waitlists.term, user_id=user_id, active=1)],
                'notifications':[{'id':e.id, 'channel':e.channel, 'status':e.status, 'message':e.payload['message']}
                    for e in db.query(self.waitlists.Outbox).filter_by(user_id=user_id).order_by(self.waitlists.Outbox.id.desc()).limit(50)
                    if e.payload.get('event') == 'seat_available']}


def install_registration(app, base, waitlists, get_user, curriculum):
    service = RegistrationService(base, waitlists, curriculum)
    app.state.registration = service

    @app.get('/api/registration')
    def snapshot(user=Depends(get_user)):
        return service.snapshot(user.id)

    @app.post('/api/registration/overrides')
    def request(payload: OverrideInput, user=Depends(get_user)):
        service.request_override(user.id, payload)
        return service.snapshot(user.id)

    @app.post('/api/registration/seat-requests')
    def request_seat(payload: SeatRequestInput, user=Depends(get_user)):
        service.request_seat(user.id, payload)
        return service.snapshot(user.id)

    @app.post('/api/registration/overrides/{request_id}/decision')
    def decide(request_id: int, payload: DecisionInput, user=Depends(get_user)):
        service.decide(user.id, request_id, payload)
        return service.snapshot(user.id)

    @app.put('/api/registration/subscriptions')
    def subscribe(payload: SubscriptionInput, user=Depends(get_user)):
        service.subscribe(user.id, payload)
        return service.snapshot(user.id)

    @app.delete('/api/registration/subscriptions/{subscription_id}')
    def unsubscribe(subscription_id: int, user=Depends(get_user)):
        service.unsubscribe(user.id, subscription_id)
        return service.snapshot(user.id)

    return service
