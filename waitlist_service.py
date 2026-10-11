"""Transactional local enrollment, FIFO waitlists, and retryable integration events."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Integer, JSON, String, UniqueConstraint, text
import mailer

log = logging.getLogger(__name__)


class JoinRequest(BaseModel):
    lecture: str = Field(min_length=1, max_length=100)
    practice: str = Field(min_length=1, max_length=100)


class InventoryUpdate(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    section: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    quota: int = Field(ge=0, le=100000)
    occupied: int = Field(ge=0, le=100000)
    reserved: int = Field(ge=0, le=100000)


class HoldUpdate(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    user_id: int = Field(ge=1)
    revision: int = Field(ge=1)
    on_hold: bool
    reason: str = Field(default="", max_length=500)


class DemoHold(BaseModel):
    on_hold: bool


class WaitlistService:
    def __init__(self, base, sessions, user_model, curriculum, term=None, academic=None):
        self.sessions = sessions
        self.user_model = user_model
        self.term = term or os.getenv("UNIVERSITY_TERM", "local-preview")
        self.academic = academic if academic is not None else os.getenv("WAITLIST_MODE", "academic") == "academic"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.worker = None

        class Section(base):
            __tablename__ = "enrollment_sections"
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            section = Column(String(100), nullable=False)
            course = Column(String(40), nullable=False)
            name = Column(String(200), nullable=False)
            parent = Column(String(100), nullable=True)
            meetings = Column(JSON, nullable=False)
            quota = Column(Integer, nullable=False)
            occupied = Column(Integer, nullable=False, default=0)
            reserved = Column(Integer, nullable=False, default=0)
            revision = Column(Integer, nullable=False, default=0)
            __table_args__ = (UniqueConstraint("term", "section"),)

        class Entry(base):
            __tablename__ = "waitlist_entries"
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
            course = Column(String(40), nullable=False)
            lecture = Column(String(100), nullable=False)
            practice = Column(String(100), nullable=False)
            queue_order = Column(Integer, nullable=False, index=True)
            status = Column(String(20), nullable=False, default="waiting")
            reason = Column(String(500), nullable=False, default="")
            created_at = Column(Integer, nullable=False)
            __table_args__ = (UniqueConstraint("term", "user_id", "course"),)

        class Enrollment(base):
            __tablename__ = "enrollments"
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
            course = Column(String(40), nullable=False)
            lecture = Column(String(100), nullable=False)
            practice = Column(String(100), nullable=False)
            meetings = Column(JSON, nullable=False)
            status = Column(String(20), nullable=False)
            enrolled_at = Column(Integer, nullable=False)
            __table_args__ = (UniqueConstraint("term", "user_id", "course"),)

        class Hold(base):
            __tablename__ = "enrollment_holds"
            user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
            on_hold = Column(Integer, nullable=False)
            reason = Column(String(500), nullable=False)
            revision = Column(Integer, nullable=False)
            verified_at = Column(Integer, nullable=False)

        class Outbox(base):
            __tablename__ = "enrollment_outbox"
            id = Column(Integer, primary_key=True)
            key = Column(String(200), nullable=False, unique=True)
            user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
            channel = Column(String(20), nullable=False)
            payload = Column(JSON, nullable=False)
            status = Column(String(20), nullable=False, default="pending")
            attempts = Column(Integer, nullable=False, default=0)
            next_attempt = Column(Integer, nullable=False, default=0)
            lease_until = Column(Integer, nullable=False, default=0)
            last_error = Column(String(500), nullable=False, default="")

        class Event(base):
            __tablename__ = "enrollment_source_events"
            id = Column(String(100), primary_key=True)
            digest = Column(String(64), nullable=False)

        self.Section, self.Entry, self.Enrollment = Section, Entry, Enrollment
        self.Hold, self.Outbox, self.Event = Hold, Outbox, Event
        base.metadata.create_all(bind=sessions.kw["bind"])
        self.sync_curriculum(curriculum)

    def sync_curriculum(self, curriculum):
        """Refresh fixed meeting times without resetting seat inventory or queue order."""
        with self.transaction() as db:
            existing = {row.section: row for row in db.query(self.Section).filter_by(term=self.term)}
            for category in curriculum["categories"]:
                for course in category["required"]:
                    for lecture in (course.get("schedule") or {}).get("lecture_sections", []):
                        for section, parent in [(lecture, None)] + [(p, lecture["section"]) for p in lecture.get("practice_sections", [])]:
                            if section["section"] in existing:
                                saved = existing[section["section"]]
                                saved.meetings = section["meetings"]
                                saved.name, saved.parent = course["name"], parent
                                continue
                            db.add(self.Section(term=self.term, section=section["section"], course=course["code"],
                                name=course["name"], parent=parent, meetings=section["meetings"],
                                quota=section["quota"], occupied=section["quota"] - section.get("reserved", 0)
                                    if self.academic else section.get("count", 0), reserved=section.get("reserved", 0)))
                            existing[section["section"]] = None
            db.flush()
            sections = self.sections(db)
            for enrollment in self.active(db):
                if enrollment.lecture in sections and enrollment.practice in sections:
                    enrollment.meetings = sections[enrollment.lecture].meetings + sections[enrollment.practice].meetings

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def locked():
            with self.lock:
                db = self.sessions()
                try:
                    dialect = db.bind.dialect.name
                    if dialect == "sqlite":
                        db.execute(text("BEGIN IMMEDIATE"))
                    elif dialect == "postgresql":
                        db.execute(text("SELECT pg_advisory_xact_lock(73420911)"))
                    else:
                        raise RuntimeError("Waitlist transactions require SQLite or PostgreSQL")
                    yield db
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
        return locked()

    def sections(self, db):
        return {row.section: row for row in db.query(self.Section).filter_by(term=self.term)}

    def active(self, db):
        return db.query(self.Enrollment).filter_by(term=self.term, status="enrolled").all()

    def free(self, section, active):
        local = sum(section.section in (row.lecture, row.practice) for row in active)
        return max(0, section.quota - section.occupied - section.reserved - local)

    def enqueue(self, db, entry, kind, message, enrollment=None):
        payload = {"event":kind, "term":self.term, "course":entry.course,
            "lecture":entry.lecture, "practice":entry.practice, "message":message,
            "enrollment_id":enrollment.id if enrollment else None}
        # A new queue order distinguishes rejoining after a cancellation/drop.
        suffix = hashlib.sha256(message.encode()).hexdigest()[:16]
        key = f"{entry.id}:{entry.queue_order}:{kind}:{suffix}"
        for channel in (["email", "billing"] if enrollment else ["email"]):
            if not db.query(self.Outbox).filter_by(key=key + ":" + channel).first():
                db.add(self.Outbox(key=key + ":" + channel, user_id=entry.user_id, channel=channel, payload=payload))
        db.flush()

    def promote(self, db):
        sections, active = self.sections(db), self.active(db)
        blocked_sections = set()
        queue = db.query(self.Entry).filter(self.Entry.term == self.term,
            self.Entry.status.in_(["waiting", "blocked"])).order_by(self.Entry.queue_order).all()
        for entry in queue:
            lecture, practice = sections[entry.lecture], sections[entry.practice]
            requested = {entry.lecture, entry.practice}
            hold = db.get(self.Hold, entry.user_id)
            reason = ""
            student = db.get(self.user_model, entry.user_id)
            if not getattr(student, "email_verified", True):
                reason = "Verify your email address before enrollment."
            elif not hold or (not self.academic and time.time() - hold.verified_at > int(os.getenv("HOLD_VERIFICATION_TTL_SECONDS", "900"))):
                reason = "Account hold status has not been verified recently."
            elif hold.on_hold:
                reason = ("Account hold: " + (hold.reason or "Contact the university."))[:500]
            else:
                meetings = lecture.meetings + practice.meetings
                if any(self.conflicts(meetings, row.meetings) for row in active if row.user_id == entry.user_id):
                    reason = "Schedule conflict with an existing enrollment. Your queue position is retained."
            if reason:
                entry.status, entry.reason = "blocked", reason
                self.enqueue(db, entry, "enrollment_blocked", reason)
                blocked_sections.update(requested)
                continue
            entry.status, entry.reason = "waiting", ""
            if requested & blocked_sections or not self.free(lecture, active) or not self.free(practice, active):
                blocked_sections.update(requested)
                continue
            enrollment = db.query(self.Enrollment).filter_by(term=self.term, user_id=entry.user_id, course=entry.course).first()
            if not enrollment:
                enrollment = self.Enrollment(term=self.term, user_id=entry.user_id, course=entry.course)
                db.add(enrollment)
            enrollment.lecture, enrollment.practice = entry.lecture, entry.practice
            enrollment.meetings, enrollment.status = lecture.meetings + practice.meetings, "enrolled"
            enrollment.enrolled_at = int(time.time())
            entry.status, entry.reason = "enrolled", ""
            db.flush()
            active.append(enrollment)
            self.enqueue(db, entry, "auto_enrolled", f"You are enrolled in {entry.course}: {entry.lecture} / {entry.practice}.", enrollment)

    @staticmethod
    def conflicts(left, right):
        return any(a["day_of_week"] == b["day_of_week"] and
            a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"] and
            (a.get("week_pattern", "every_week") == "every_week" or
             b.get("week_pattern", "every_week") == "every_week" or
             a.get("week_pattern") == b.get("week_pattern")) for a in left for b in right)

    def join(self, user_id, payload):
        with self.transaction() as db:
            if self.academic and not db.get(self.Hold, user_id):
                db.add(self.Hold(user_id=user_id, on_hold=0, reason="", revision=0, verified_at=int(time.time())))
                db.flush()
            sections = self.sections(db)
            lecture, practice = sections.get(payload.lecture), sections.get(payload.practice)
            if not lecture or not practice or lecture.parent or practice.parent != lecture.section:
                raise HTTPException(422, "Select a lecture and its linked practice.")
            if self.conflicts(lecture.meetings, practice.meetings):
                raise HTTPException(422, "These lecture and practice meetings overlap.")
            entry = db.query(self.Entry).filter_by(term=self.term, user_id=user_id, course=lecture.course).first()
            if entry and entry.status != "cancelled":
                if (entry.lecture, entry.practice) != (payload.lecture, payload.practice):
                    raise HTTPException(409, "Cancel or drop your existing registration before changing sections.")
                return
            last = db.query(self.Entry).order_by(self.Entry.queue_order.desc()).first()
            order = (last.queue_order if last else 0) + 1
            if not entry:
                entry = self.Entry(term=self.term, user_id=user_id, course=lecture.course)
                db.add(entry)
            entry.lecture, entry.practice = payload.lecture, payload.practice
            entry.queue_order, entry.status = order, "waiting"
            entry.reason, entry.created_at = "", int(time.time())
            db.flush()
            self.promote(db)
        self.wake.set()

    def cancel(self, user_id, entry_id):
        with self.transaction() as db:
            entry = db.query(self.Entry).filter_by(id=entry_id, user_id=user_id, term=self.term).first()
            if not entry:
                raise HTTPException(404, "Waitlist entry not found.")
            if entry.status == "enrolled":
                raise HTTPException(409, "Drop the enrollment to release its seats.")
            entry.status, entry.reason = "cancelled", ""
            db.flush()
            self.promote(db)
        self.wake.set()

    def drop(self, user_id, enrollment_id):
        with self.transaction() as db:
            enrollment = db.query(self.Enrollment).filter_by(id=enrollment_id, user_id=user_id, term=self.term).first()
            if not enrollment:
                raise HTTPException(404, "Enrollment not found.")
            if enrollment.status == "dropped":
                return
            enrollment.status = "dropped"
            entry = db.query(self.Entry).filter_by(term=self.term, user_id=user_id, course=enrollment.course).one()
            entry.status = "cancelled"
            self.enqueue(db, entry, "enrollment_dropped", f"You dropped {entry.course}.", enrollment)
            db.flush()
            self.promote(db)
        self.wake.set()

    def source_event(self, payload, kind):
        digest = hashlib.sha256((kind + json.dumps(payload.model_dump(), sort_keys=True)).encode()).hexdigest()
        with self.transaction() as db:
            existing = db.get(self.Event, payload.event_id)
            if existing:
                if existing.digest != digest:
                    raise HTTPException(409, "Event ID was reused with different data.")
                return
            if kind == "inventory":
                row = db.query(self.Section).filter_by(term=self.term, section=payload.section).first()
                if not row:
                    raise HTTPException(404, "Section not found.")
                if payload.revision <= row.revision:
                    raise HTTPException(409, "Stale inventory revision.")
                if payload.occupied + payload.reserved + sum(row.section in (e.lecture,e.practice) for e in self.active(db)) > payload.quota:
                    raise HTTPException(409, "Inventory update would exceed the section quota.")
                row.quota, row.occupied, row.reserved, row.revision = payload.quota, payload.occupied, payload.reserved, payload.revision
            else:
                if not db.get(self.user_model, payload.user_id):
                    raise HTTPException(404, "Student not found.")
                row = db.get(self.Hold, payload.user_id)
                if row and payload.revision <= row.revision:
                    raise HTTPException(409, "Stale hold revision.")
                if not row:
                    row = self.Hold(user_id=payload.user_id)
                    db.add(row)
                row.on_hold, row.reason, row.revision, row.verified_at = int(payload.on_hold), payload.reason, payload.revision, int(time.time())
            db.add(self.Event(id=payload.event_id, digest=digest))
            db.flush()
            self.promote(db)
        self.wake.set()

    def demo_seat(self, user_id, payload):
        if not self.academic:
            raise HTTPException(404, "Simulation is disabled.")
        with self.transaction() as db:
            entry = db.query(self.Entry).filter_by(term=self.term, user_id=user_id,
                lecture=payload.lecture, practice=payload.practice).first()
            if not entry or entry.status not in ("waiting", "blocked"):
                raise HTTPException(409, "Join this waitlist before simulating a seat opening.")
            sections, active = self.sections(db), self.active(db)
            for section_id in (entry.lecture, entry.practice):
                section = sections[section_id]
                # Release an occupied demo seat; never change a course quota or timing.
                if not self.free(section, active) and section.occupied > 0:
                    section.occupied -= 1
                    section.revision += 1
            db.flush()
            self.promote(db)
        self.wake.set()

    def demo_hold(self, user_id, on_hold):
        if not self.academic:
            raise HTTPException(404, "Simulation is disabled.")
        with self.transaction() as db:
            hold = db.get(self.Hold, user_id)
            if not hold:
                hold = self.Hold(user_id=user_id, revision=0)
                db.add(hold)
            hold.on_hold, hold.reason = int(on_hold), "Simulated account hold" if on_hold else ""
            hold.revision, hold.verified_at = hold.revision + 1, int(time.time())
            db.flush()
            self.promote(db)
        self.wake.set()

    def snapshot(self, user_id):
        with self.transaction() as db:
            sections, active = self.sections(db), self.active(db)
            queue = db.query(self.Entry).filter(self.Entry.term == self.term,
                self.Entry.status.in_(["waiting", "blocked"])).order_by(self.Entry.queue_order).all()
            entries = db.query(self.Entry).filter_by(term=self.term, user_id=user_id).order_by(self.Entry.queue_order).all()
            hold = db.get(self.Hold, user_id)
            return {"term":self.term, "mode":"academic" if self.academic else "local enrollment",
                "on_hold":bool(hold and hold.on_hold),
                "email_configured":bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME"))),
                "billing_configured":bool(os.getenv("UNIVERSITY_BILLING_URL")),
                "sections":[{"section":s.section, "course":s.course, "name":s.name, "parent":s.parent,
                    "meetings":s.meetings, "quota":s.quota, "available":self.free(s,active)} for s in sections.values()],
                "waitlists":[{"id":e.id, "course":e.course, "lecture":e.lecture, "practice":e.practice,
                    "status":e.status, "reason":e.reason, "position":1 + sum(q.queue_order < e.queue_order and
                        bool({q.lecture,q.practice} & {e.lecture,e.practice}) for q in queue)
                        if e.status in ("waiting","blocked") else None} for e in entries if e.status != "cancelled"],
                "enrollments":[{"id":e.id, "course":e.course, "lecture":e.lecture, "practice":e.practice} for e in active if e.user_id == user_id],
                "notifications":[{"id":o.id, "channel":o.channel, "status":o.status, "message":o.payload["message"]}
                    for o in db.query(self.Outbox).filter_by(user_id=user_id).order_by(self.Outbox.id.desc()).limit(30)]}

    def deliver(self, channel, payload, email, key):
        if channel == "billing":
            if self.academic:
                return  # Billing remains simulated; email is always real SMTP delivery.
            url = os.getenv("UNIVERSITY_BILLING_URL", "").strip()
            if not url:
                raise RuntimeError("Billing API is not configured")
            if not url.startswith("https://") and not (os.getenv("APP_ENV", "development") != "production" and url.startswith("http://127.0.0.1:")):
                raise RuntimeError("Billing API must use HTTPS")
            headers = {"Content-Type":"application/json", "Idempotency-Key":key}
            token = os.getenv("UNIVERSITY_BILLING_TOKEN", "")
            if token:
                headers["Authorization"] = "Bearer " + token
            request = UrlRequest(url, data=json.dumps({**payload, "student_email":email}).encode(), headers=headers, method="POST")
            with urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("Billing API did not accept the adjustment event")
            return
        mailer.send_email(email, "Course enrollment update", payload["message"]
            + "\nTerm: " + payload["term"] + ("\nAcademic course enrollment demonstration." if self.academic else ""), key)

    def dispatch(self, limit=25):
        for _ in range(limit):
            if self.stop.is_set():
                return
            now = int(time.time())
            with self.transaction() as db:
                row = db.query(self.Outbox).filter(self.Outbox.status.notin_(["delivered", "simulated"]),
                    self.Outbox.next_attempt <= now, self.Outbox.lease_until <= now).order_by(self.Outbox.id).first()
                if not row:
                    return
                row.status, row.lease_until = "sending", now + 120
                row.attempts += 1
                row_id, channel, payload, key = row.id, row.channel, row.payload, row.key
                student = db.get(self.user_model, row.user_id)
                email = student.email
                verified = getattr(student, "email_verified", True)
            error = ""
            try:
                if channel == "email" and not verified:
                    raise RuntimeError("Verify the recipient before enrollment email delivery")
                self.deliver(channel, payload, email, key)
            except Exception as exc:
                # Store only exception type: provider errors can contain credentials/PII.
                error = type(exc).__name__
            with self.transaction() as db:
                row = db.get(self.Outbox, row_id)
                row.status = "pending" if error else ("simulated" if self.academic and channel == "billing" else "delivered")
                row.last_error, row.lease_until = error, 0
                row.next_attempt = int(time.time()) + min(300, 2 ** min(row.attempts, 8)) if error else 0

    def run(self):
        while not self.stop.is_set():
            try:
                with self.transaction() as db:
                    self.promote(db)
                self.dispatch()
            except Exception:
                log.exception("Waitlist worker cycle failed")
            self.wake.wait(1)
            self.wake.clear()


def install_waitlists(app, base, sessions, user_model, get_user, curriculum):
    service = WaitlistService(base, sessions, user_model, curriculum)

    def integration_auth(request: Request):
        secret = os.getenv("UNIVERSITY_WEBHOOK_TOKEN", "")
        if not secret:
            raise HTTPException(503, "University update integration is not configured.")
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + secret):
            raise HTTPException(401, "Invalid integration credentials.")

    @app.get("/api/waitlists")
    def get_waitlists(user=Depends(get_user)):
        return service.snapshot(user.id)

    @app.post("/api/waitlists")
    def join_waitlist(payload: JoinRequest, user=Depends(get_user)):
        service.join(user.id, payload)
        return service.snapshot(user.id)

    @app.delete("/api/waitlists/{entry_id}")
    def cancel_waitlist(entry_id: int, user=Depends(get_user)):
        service.cancel(user.id, entry_id)
        return service.snapshot(user.id)

    @app.delete("/api/enrollments/{enrollment_id}")
    def drop_enrollment(enrollment_id: int, user=Depends(get_user)):
        service.drop(user.id, enrollment_id)
        return service.snapshot(user.id)

    @app.post("/api/waitlists/demo/seat")
    def simulate_seat(payload: JoinRequest, user=Depends(get_user)):
        service.demo_seat(user.id, payload)
        return service.snapshot(user.id)

    @app.post("/api/waitlists/demo/hold")
    def simulate_hold(payload: DemoHold, user=Depends(get_user)):
        service.demo_hold(user.id, payload.on_hold)
        return service.snapshot(user.id)

    @app.post("/api/integrations/university/inventory", dependencies=[Depends(integration_auth)])
    def update_inventory(payload: InventoryUpdate):
        service.source_event(payload, "inventory")
        return {"accepted":True}

    @app.post("/api/integrations/university/holds", dependencies=[Depends(integration_auth)])
    def update_hold(payload: HoldUpdate):
        service.source_event(payload, "hold")
        return {"accepted":True}

    @asynccontextmanager
    async def lifespan(_app):
        service.stop.clear()
        service.worker = threading.Thread(target=service.run, name="waitlist-worker", daemon=True)
        service.worker.start()
        try:
            yield
        finally:
            service.stop.set()
            service.wake.set()
            service.worker.join(timeout=25)

    app.router.lifespan_context = lifespan
    app.state.waitlists = service
    return service
