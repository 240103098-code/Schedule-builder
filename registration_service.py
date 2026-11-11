"""Advisor routing and opt-in seat alerts sharing enrollment transactions."""
import os
import re
import time
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Integer, Float, String, JSON, UniqueConstraint
import telegram_delivery
import hashlib
import secrets
import hmac
import threading
import io
import base64
import qrcode
import qrcode.image.svg
import mailer


CROSS_DEPARTMENT = 'Unauthorized: Student belongs to another department'
SEAT_ALERT_LIMIT = 5


def registration_url(section):
    """Direct link that opens the registration (Waitlists) page with the section preselected."""
    from urllib.parse import quote
    base = os.getenv('APP_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
    return f'{base}/?tab=waitlists&section={quote(section)}'


class OverrideInput(BaseModel):
    lecture: str = Field(min_length=1, max_length=100)
    practice: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=5, max_length=1000)


class DecisionInput(BaseModel):
    decision: Literal['approved', 'rejected']
    comment: str = Field(default='', max_length=1000)
    lecture: str = Field(default='', max_length=100)
    practice: str = Field(default='', max_length=100)


class SeatRequestInput(BaseModel):
    course: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=5, max_length=1000)


class SubscriptionInput(BaseModel):
    section: str = Field(min_length=1, max_length=100)
    channels: list[Literal['in_app', 'push', 'telegram', 'email']] = Field(min_length=1, max_length=4)


class ReadNoticesInput(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=100)
    through_id: int | None = Field(default=None, ge=1)


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
            legacy_contact = Column('phone', String(20), nullable=False, default='')
            active = Column(Integer, nullable=False, default=1)
            generation = Column(Integer, nullable=False, default=0)
            __table_args__ = (UniqueConstraint('term', 'user_id', 'section'),)

        class SeatGrant(base):
            __tablename__ = 'advisor_seat_grants'
            id = Column(Integer, primary_key=True)
            request_id = Column(Integer, ForeignKey('registration_overrides.id'), nullable=False, unique=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
            course = Column(String(40), nullable=False)
            lecture = Column(String(100), nullable=False)
            practice = Column(String(100), nullable=False)
            status = Column(String(20), nullable=False, default='reserved')
            created_at = Column(Integer, nullable=False)

        self.Profile, self.Policy, self.Override, self.Subscription = Profile, Policy, Override, Subscription
        self.SeatGrant = SeatGrant
        class Notice(base):
            __tablename__ = 'course_notifications'
            id = Column(Integer, primary_key=True)
            key = Column(String(150), nullable=False, unique=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
            course = Column(String(40), nullable=False)
            section = Column(String(100), nullable=False, default='')
            kind = Column(String(40), nullable=False)
            message = Column(String(1500), nullable=False)
            created_at = Column(Integer, nullable=False)
            read_at = Column(Integer)
        class TelegramLink(base):
            __tablename__ = 'telegram_links'
            user_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
            chat_id = Column(String(30), nullable=False, default='')
            token_hash = Column(String(64), nullable=False, default='')
            expires_at = Column(Integer, nullable=False, default=0)
        class TelegramCursor(base):
            __tablename__ = 'telegram_update_cursor'
            id = Column(Integer, primary_key=True)
            offset = Column(Integer, nullable=False, default=0)
        self.TelegramLink = TelegramLink
        self.TelegramCursor = TelegramCursor
        self.telegram_lock = threading.Lock()
        self.Notice = Notice
        base.metadata.create_all(bind=waitlists.sessions.kw['bind'])
        waitlists.registration = self
        self.retire_legacy_channel()

    def retire_legacy_channel(self):
        # Retire old channel data without silently opting users into a new service.
        with self.waitlists.transaction() as db:
            db.query(self.waitlists.Outbox).filter_by(channel='sms').delete(synchronize_session=False)
            for sub in db.query(self.Subscription):
                sub.channels = [c for c in sub.channels if c != 'sms'] or ['in_app']
                sub.legacy_contact = ''
            for event in db.query(self.waitlists.Outbox):
                if 'phone' in event.payload:
                    event.payload = {k:v for k,v in event.payload.items() if k != 'phone'}

    def telegram_start(self, user_id):
        try:
            bot = telegram_delivery.call('getMe')
        except telegram_delivery.TelegramError as exc:
            raise HTTPException(503, str(exc)) from None
        username = bot.get('username', '') if isinstance(bot, dict) else ''
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise HTTPException(503, 'Telegram вернул некорректное имя бота.')
        token = secrets.token_urlsafe(24)
        with self.waitlists.transaction() as db:
            self.require_student(db, user_id)
            row = db.get(self.TelegramLink, user_id)
            if not row:
                row = self.TelegramLink(user_id=user_id)
                db.add(row)
            row.token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.expires_at = int(time.time()) + 600
        url = 'https://t.me/' + username + '?start=' + token
        image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=4)
        buffer = io.BytesIO()
        image.save(buffer)
        return {'url':url, 'qr_svg':'data:image/svg+xml;base64,' + base64.b64encode(buffer.getvalue()).decode()}

    def process_telegram_updates(self):
        # Serialize getUpdates calls in this process. Process every user's update
        # before acknowledging it, so concurrent sign-ins cannot steal each other's codes.
        with self.telegram_lock:
            with self.waitlists.transaction() as db:
                cursor = db.get(self.TelegramCursor, 1)
                offset = cursor.offset if cursor else 0
            try:
                updates = telegram_delivery.call('getUpdates', {'offset':offset, 'limit':100, 'timeout':0})
            except telegram_delivery.TelegramError as exc:
                raise HTTPException(503, str(exc)) from None
            if not isinstance(updates, list):
                raise HTTPException(503, 'Telegram вернул некорректный ответ.')
            with self.waitlists.transaction() as db:
                cursor = db.get(self.TelegramCursor, 1)
                if not cursor:
                    cursor = self.TelegramCursor(id=1, offset=offset)
                    db.add(cursor)
                for update in updates:
                    update_id = update.get('update_id')
                    if not isinstance(update_id, int) or update_id < offset:
                        continue
                    message = update.get('message') or {}
                    chat = message.get('chat') or {}
                    text = message.get('text') or ''
                    if (chat.get('type') == 'private' and chat.get('id') == (message.get('from') or {}).get('id')
                            and isinstance(text, str) and text.startswith('/start ')):
                        token = text.split(' ', 1)[1].strip()
                        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', token):
                            digest = hashlib.sha256(token.encode()).hexdigest()
                            row = db.query(self.TelegramLink).filter_by(token_hash=digest).first()
                            if (row and row.expires_at >= int(time.time()) and
                                    message.get('date', 0) >= row.expires_at - 600):
                                row.chat_id, row.token_hash, row.expires_at = str(chat['id']), '', 0
                    cursor.offset = max(cursor.offset or 0, update_id + 1)

    def telegram_confirm(self, user_id):
        with self.waitlists.transaction() as db:
            row = db.get(self.TelegramLink, user_id)
            if not row:
                raise HTTPException(409, 'Сначала нажмите «Подключить Telegram».')
            if row.chat_id and not row.token_hash:
                return {'linked':True}
            if not row.token_hash or row.expires_at < int(time.time()):
                raise HTTPException(409, 'Ссылка истекла. Нажмите «Подключить Telegram» ещё раз.')
        self.process_telegram_updates()
        with self.waitlists.transaction() as db:
            row = db.get(self.TelegramLink, user_id)
            if row and row.chat_id and not row.token_hash:
                return {'linked':True}
        raise HTTPException(409, 'Откройте ссылку в Telegram и нажмите Start.')

    def telegram_disconnect(self, user_id):
        with self.waitlists.transaction() as db:
            row = db.get(self.TelegramLink, user_id)
            if row:
                db.delete(row)
            for sub in db.query(self.Subscription).filter_by(user_id=user_id):
                sub.channels = [c for c in sub.channels if c != 'telegram'] or ['in_app']
            for event in db.query(self.waitlists.Outbox).filter_by(user_id=user_id, channel='telegram'):
                if event.status not in ('delivered', 'failed', 'cancelled'):
                    event.status = 'cancelled'

    def require_student(self, db, user_id):
        profile = self.student_profile(db, user_id)
        if profile and profile.role != 'student':
            raise HTTPException(403, 'This action is available only to students.')
        return profile

    def grant_for(self, db, entry):
        return db.query(self.SeatGrant).filter_by(term=self.waitlists.term, user_id=entry.user_id,
            course=entry.course, lecture=entry.lecture, practice=entry.practice, status='reserved').first()

    def reserved_count(self, db, section):
        return db.query(self.SeatGrant).filter(self.SeatGrant.term == self.waitlists.term,
            self.SeatGrant.status == 'reserved',
            (self.SeatGrant.lecture == section) | (self.SeatGrant.practice == section)).count()

    def claim_grant(self, db, grant, lecture, practice):
        if lecture.reserved < 1 or practice.reserved < 1:
            raise HTTPException(409, 'The reserved seat must be reconciled with the section inventory.')
        lecture.reserved -= 1
        practice.reserved -= 1
        grant.status = 'used'
        db.flush()

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
        if total > int(os.getenv('REGISTRATION_CREDIT_LIMIT', '40')):
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
            profile = self.require_student(db, user_id)
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
            profile = self.require_student(db, user_id)
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

    def cancel_seat_request(self, user_id, request_id):
        with self.waitlists.transaction() as db:
            row = db.query(self.Override).filter_by(id=request_id, term=self.waitlists.term,
                user_id=user_id).first()
            if not row or 'additional_seat' not in row.flags:
                raise HTTPException(404, 'Seat request not found.')
            if row.status != 'pending':
                raise HTTPException(409, 'Only pending seat requests can be cancelled.')
            db.delete(row)

    def use_seat(self, user_id, request_id):
        with self.waitlists.transaction() as db:
            self.require_student(db, user_id)
            row = db.query(self.SeatGrant).filter_by(request_id=request_id,
                term=self.waitlists.term, user_id=user_id).first()
            if not row:
                raise HTTPException(404, 'Approved extra seat not found.')
            if row.status == 'used':
                return
            from waitlist_service import JoinRequest
            pair = JoinRequest(lecture=row.lecture, practice=row.practice)
        self.waitlists.join(user_id, pair)

    def decide(self, advisor_id, request_id, payload):
        with self.waitlists.transaction() as db:
            profile = db.get(self.Profile, advisor_id)
            if not profile or profile.role != 'advisor':
                raise HTTPException(403, 'Advisor role required.')
            row = db.query(self.Override).filter_by(id=request_id, term=self.waitlists.term).first()
            if not row:
                raise HTTPException(404, 'Request not found.')
            student = db.get(self.Profile, row.user_id)
            if (row.department != profile.department or not student or student.department != profile.department
                    or row.user_id == advisor_id):
                # Nothing is modified: the advisor may only act within the assigned department.
                raise HTTPException(403, CROSS_DEPARTMENT)
            if payload.decision == 'rejected' and not payload.comment.strip():
                raise HTTPException(422, 'Feedback comments are required to reject a request.')
            legacy_allocation = (row.status == 'approved' and payload.decision == 'approved' and
                'additional_seat' in row.flags and not db.query(self.SeatGrant).filter_by(request_id=row.id).first())
            if row.status != 'pending' and not legacy_allocation:
                raise HTTPException(409, 'This request has already been decided.')
            if payload.decision == 'approved' and 'additional_seat' in row.flags:
                if not payload.lecture or not payload.practice:
                    raise HTTPException(422, 'Choose a lecture and linked practice to allocate the extra seat.')
                lecture, practice = self.pair(db, payload)
                if lecture.course != row.course:
                    raise HTTPException(422, 'Selected sections must belong to the requested course.')
                queued = db.query(self.waitlists.Entry).filter_by(term=self.waitlists.term,
                    user_id=row.user_id, course=row.course).first()
                if queued and queued.status in ('waiting', 'blocked') and (
                        queued.lecture != lecture.section or queued.practice != practice.section):
                    raise HTTPException(409, 'Choose the student\'s waitlisted sections, or ask the student to leave that queue first.')
                if db.query(self.waitlists.Enrollment).filter_by(term=self.waitlists.term,
                        user_id=row.user_id, course=row.course, status='enrolled').first():
                    raise HTTPException(409, 'The student is already enrolled in this course.')
                flags, total, policy = self.flags(db, row.user_id, lecture, self.waitlists.active(db))
                if policy and policy.department != profile.department and flags:
                    raise HTTPException(403, 'Only the course department may approve its academic restrictions.')
                row.lecture, row.practice = lecture.section, practice.section
                row.flags = sorted(set(row.flags + flags))
                row.credit_total = total
                # Increase physical capacity and reserve it for this student, atomically.
                # Other waitlisted students cannot consume this personal extra place.
                for section in (lecture, practice):
                    section.quota += 1
                    section.reserved += 1
                db.add(self.SeatGrant(request_id=row.id, term=self.waitlists.term, user_id=row.user_id,
                    course=row.course, lecture=lecture.section, practice=practice.section,
                    created_at=int(time.time())))
            row.status, row.comment = payload.decision, payload.comment.strip()
            row.reviewer_id, row.decided_at = advisor_id, int(time.time())
            self.notice(db, f'decision:{row.id}:{row.status}', row.user_id, row.course, row.lecture,
                'seat_granted' if payload.decision == 'approved' else 'request_rejected',
                ('Дополнительное место выделено для ' if payload.decision == 'approved' and 'additional_seat' in row.flags else
                 'Заявка одобрена: ' if payload.decision == 'approved' else 'Заявка отклонена: ') +
                row.course + ' · ' + self.catalog.get(row.course, row.course) + ('. ' + row.comment if row.comment else ''))
            self.decision_email(db, row)
            db.flush()
            self.waitlists.promote(db)
        self.waitlists.wake.set()

    def decision_email(self, db, row):
        """Email the student the advisor's decision and feedback (transactional outbox)."""
        course = row.course + ' · ' + self.catalog.get(row.course, row.course)
        if row.status == 'approved':
            message = (f'Your advisor approved your request for {course}. Registration for this course is now unlocked.'
                       + ('\nAdvisor feedback: ' + row.comment if row.comment else ''))
        else:
            message = (f'Your advisor rejected your request for {course}. The course stays locked for registration.'
                       '\nAdvisor feedback: ' + row.comment)
        key = f'decision:{row.id}:{row.status}:email'
        if not db.query(self.waitlists.Outbox).filter_by(key=key).first():
            db.add(self.waitlists.Outbox(key=key, user_id=row.user_id, channel='email',
                payload={'event':'advisor_decision', 'term':self.waitlists.term, 'course':row.course,
                         'subject':'Advisor decision: ' + row.course + ' — ' + row.status,
                         'decision':row.status, 'comment':row.comment, 'message':message}))

    def subscribe(self, user_id, payload):
        with self.waitlists.transaction() as db:
            self.require_student(db, user_id)
            if 'telegram' in payload.channels:
                link = db.get(self.TelegramLink, user_id)
                if not telegram_delivery.configured() or not link or not link.chat_id:
                    raise HTTPException(409, 'Сначала подключите Telegram к своему аккаунту.')
            section = self.waitlists.sections(db).get(payload.section)
            if not section:
                raise HTTPException(404, 'Section not found.')
            if self.waitlists.free(section, self.waitlists.active(db)):
                raise HTTPException(409, 'Seats are available already. Subscribe when the section is full.')
            row = db.query(self.Subscription).filter_by(term=self.waitlists.term, user_id=user_id, section=payload.section).first()
            if not (row and row.active):
                active = db.query(self.Subscription).filter_by(term=self.waitlists.term, user_id=user_id, active=1).count()
                if active >= SEAT_ALERT_LIMIT:
                    raise HTTPException(409, f'Maximum seat alert limit of {SEAT_ALERT_LIMIT} courses reached. '
                        'Remove an existing alert to add a new one.')
            if not row:
                row = self.Subscription(term=self.waitlists.term, user_id=user_id, section=payload.section)
                db.add(row)
            row.channels, row.active = sorted(set(payload.channels)), 1

    def unsubscribe(self, user_id, subscription_id):
        with self.waitlists.transaction() as db:
            row = db.query(self.Subscription).filter_by(id=subscription_id, user_id=user_id, term=self.waitlists.term).first()
            if not row:
                raise HTTPException(404, 'Subscription not found.')
            row.active = 0
            for event in db.query(self.waitlists.Outbox).filter_by(user_id=user_id, status='pending'):
                if event.payload.get('subscription_id') == row.id:
                    event.status = 'cancelled'

    def demo_open_seat(self, user_id, subscription_id):
        if not self.waitlists.academic:
            raise HTTPException(404, 'Academic seat simulation is disabled.')
        with self.waitlists.transaction() as db:
            self.require_student(db, user_id)
            subscription = db.query(self.Subscription).filter_by(id=subscription_id, user_id=user_id,
                term=self.waitlists.term, active=1).first()
            if not subscription:
                raise HTTPException(404, 'Subscription not found.')
            section = self.waitlists.sections(db).get(subscription.section)
            if not section:
                raise HTTPException(404, 'Section not found.')
            if self.waitlists.free(section, self.waitlists.active(db)):
                raise HTTPException(409, 'This section already has free seats.')
            if section.occupied < 1:
                raise HTTPException(409, 'No simulated occupied seat can be released. Drop an existing enrollment instead.')
            before = self.waitlists.seat_counts(db)
            section.occupied -= 1
            section.revision += 1
            db.flush()
            self.waitlists.notify_seats(db, before)
            self.waitlists.promote(db)
        self.waitlists.wake.set()

    def seat_opened(self, db, before):
        active = self.waitlists.active(db)
        for section in self.waitlists.sections(db).values():
            if before.get(section.section) != 0 or self.waitlists.free(section, active) == 0:
                continue
            for sub in db.query(self.Subscription).filter_by(term=self.waitlists.term, section=section.section, active=1):
                sub.generation += 1
                link = registration_url(section.section)
                self.notice(db, f'seat:{sub.id}:{sub.generation}', sub.user_id, section.course, section.section,
                    'seat_available', f'Освободилось место: {section.course} · {section.name}, секция {section.section}. Доступность может измениться; очередь записи сохраняется. Записаться: {link}')
                for channel in sub.channels:
                    if channel == 'in_app':
                        continue
                    db.add(self.waitlists.Outbox(key=f'seat:{sub.id}:{sub.generation}:{channel}', user_id=sub.user_id,
                        channel=channel, payload={'event':'seat_available', 'term':self.waitlists.term,
                            'section':section.section, 'subscription_id':sub.id, 'url':link,
                            'message':f'A seat opened in {section.course}, section {section.section}. Availability may change; waitlist order still applies.'
                                      f'\nOpen course registration: {link}'}))
        db.flush()

    def notice(self, db, key, user_id, course, section, kind, message):
        if not db.query(self.Notice).filter_by(key=key).first():
            db.add(self.Notice(key=key, term=self.waitlists.term, user_id=user_id, course=course,
                section=section, kind=kind, message=message, created_at=int(time.time())))

    def inbox(self, user_id):
        with self.waitlists.transaction() as db:
            profile = self.student_profile(db, user_id)
            query = db.query(self.Notice).filter_by(term=self.waitlists.term, user_id=user_id)
            items = query.order_by(self.Notice.id.desc()).limit(50).all()
            return {'role':profile.role if profile else 'student', 'unread':query.filter(self.Notice.read_at.is_(None)).count(),
                'items':[{'id':n.id, 'course':n.course, 'section':n.section, 'kind':n.kind,
                    'message':n.message, 'created_at':n.created_at, 'read':n.read_at is not None} for n in items]}

    def read_notices(self, user_id, ids, through_id=None):
        with self.waitlists.transaction() as db:
            query = db.query(self.Notice).filter(self.Notice.user_id == user_id, self.Notice.term == self.waitlists.term,
                self.Notice.read_at.is_(None))
            query = query.filter(self.Notice.id <= through_id) if through_id else query.filter(self.Notice.id.in_(ids))
            query.update({'read_at':int(time.time())}, synchronize_session=False)

    def snapshot(self, user_id):
        with self.waitlists.transaction() as db:
            active = self.waitlists.active(db)
            profile = self.student_profile(db, user_id)
            role = profile.role if profile else 'student'
            query = db.query(self.Override).filter_by(term=self.waitlists.term)
            query = query.filter_by(department=profile.department) if role == 'advisor' else query.filter_by(user_id=user_id)
            requests = []
            for row in query.order_by(self.Override.created_at.desc(), self.Override.id.desc()):
                student = db.get(self.waitlists.user_model, row.user_id)
                grant = db.query(self.SeatGrant).filter_by(request_id=row.id).first()
                entry = db.query(self.waitlists.Entry).filter_by(term=self.waitlists.term,
                    user_id=row.user_id, course=row.course).first()
                requests.append({k: getattr(row, k) for k in ('id', 'course', 'lecture', 'practice', 'department',
                    'flags', 'credit_total', 'reason', 'status', 'comment', 'created_at', 'decided_at')} |
                    {'student':student.name, 'course_name':self.catalog.get(row.course, row.course),
                     'own_request':row.user_id == user_id,
                     'seat_grant':grant.status if grant else None,
                     'queued_lecture':entry.lecture if entry and entry.status in ('waiting', 'blocked') else '',
                     'queued_practice':entry.practice if entry and entry.status in ('waiting', 'blocked') else '',
                     'enrollment_status':entry.status if entry else None,
                     'enrollment_reason':entry.reason if entry else ''})
            return {'role':role, 'department':profile.department if profile else None, 'academic_mode':self.waitlists.academic,
                'sections':[{'section':s.section, 'course':s.course, 'parent':s.parent,
                    'meetings':s.meetings, 'quota':s.quota, 'reserved':s.reserved,
                    'available':self.waitlists.free(s, active)}
                    for s in self.waitlists.sections(db).values()],
                'courses':[{'code':code, 'name':name} for code, name in sorted(self.catalog.items())],
                'gateway_configured':bool(os.getenv('UNIVERSITY_NOTIFICATION_URL')),
                'email_configured':mailer.configured() or bool(os.getenv('UNIVERSITY_NOTIFICATION_URL')),
                'email_sender_configured':mailer.configured(),
                'email_verified':bool(getattr(db.get(self.waitlists.user_model, user_id), 'email_verified', True)),
                'telegram_configured':telegram_delivery.configured(),
                'telegram_linked':bool(db.get(self.TelegramLink, user_id) and db.get(self.TelegramLink, user_id).chat_id),
                'requests':requests,
                'subscriptions':[{'id':s.id, 'section':s.section, 'channels':s.channels} for s in
                    db.query(self.Subscription).filter_by(term=self.waitlists.term, user_id=user_id, active=1)],
                'notifications':[{'id':e.id, 'channel':e.channel, 'status':e.status, 'message':e.payload['message'],
                    'last_error':e.last_error,
                    'provider':e.payload.get('delivery_provider') or ('telegram' if e.channel == 'telegram' else 'gateway')}
                    for e in db.query(self.waitlists.Outbox).filter_by(user_id=user_id).filter(
                        self.waitlists.Outbox.payload['event'].as_string() == 'seat_available').order_by(self.waitlists.Outbox.id.desc()).limit(50)]}


def install_registration(app, base, waitlists, get_user, curriculum):
    service = RegistrationService(base, waitlists, curriculum)
    app.state.registration = service

    @app.get('/api/registration')
    def snapshot(user=Depends(get_user)):
        return service.snapshot(user.id)

    @app.get('/api/registration/notifications')
    def inbox(user=Depends(get_user)):
        return service.inbox(user.id)

    @app.post('/api/registration/notifications/read')
    def read_notices(payload: ReadNoticesInput, user=Depends(get_user)):
        service.read_notices(user.id, payload.ids, payload.through_id)
        return service.inbox(user.id)

    @app.post('/api/registration/overrides')
    def request(payload: OverrideInput, user=Depends(get_user)):
        service.request_override(user.id, payload)
        return service.snapshot(user.id)

    @app.post('/api/registration/seat-requests')
    def request_seat(payload: SeatRequestInput, user=Depends(get_user)):
        service.request_seat(user.id, payload)
        return service.snapshot(user.id)

    @app.delete('/api/registration/seat-requests/{request_id}')
    def cancel_seat_request(request_id: int, user=Depends(get_user)):
        service.cancel_seat_request(user.id, request_id)
        return service.snapshot(user.id)

    @app.post('/api/registration/seat-requests/{request_id}/enroll')
    def use_seat(request_id: int, user=Depends(get_user)):
        service.use_seat(user.id, request_id)
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

    @app.post('/api/registration/subscriptions/{subscription_id}/demo-seat')
    def demo_open_seat(subscription_id: int, user=Depends(get_user)):
        service.demo_open_seat(user.id, subscription_id)
        return service.snapshot(user.id)

    @app.post('/api/registration/telegram/link')
    def telegram_link(user=Depends(get_user)):
        return service.telegram_start(user.id)

    @app.post('/api/registration/telegram/confirm')
    def telegram_confirm(user=Depends(get_user)):
        service.telegram_confirm(user.id)
        return service.snapshot(user.id)

    @app.delete('/api/registration/telegram/link')
    def telegram_disconnect(user=Depends(get_user)):
        service.telegram_disconnect(user.id)
        return service.snapshot(user.id)

    return service
