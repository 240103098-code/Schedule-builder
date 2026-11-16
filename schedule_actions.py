"""US9 atomic section swaps and US10 confirmed enrollment calendar export."""
from datetime import date, datetime, time as clock_time, timedelta, timezone
from types import SimpleNamespace
import hashlib
import os
import time

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, model_validator

from waitlist_service import JoinRequest


class CalendarRange(BaseModel):
    start: date
    end: date

    @model_validator(mode='after')
    def valid_range(self):
        if not 0 <= (self.end - self.start).days <= 366:
            raise ValueError('Choose a semester date range of at most 367 days.')
        return self


def swap_section(service, user_id, enrollment_id, payload):
    # The same database lock protects swaps, seat updates, drops and promotions.
    with service.transaction() as db:
        service.require_student(db, user_id)
        enrollment = db.query(service.Enrollment).filter_by(
            id=enrollment_id, term=service.term, user_id=user_id, status='enrolled').first()
        if not enrollment:
            raise HTTPException(404, 'Active enrollment not found.')
        sections = service.sections(db)
        lecture, practice = sections.get(payload.lecture), sections.get(payload.practice)
        if (not lecture or not practice or lecture.parent or practice.parent != lecture.section
                or lecture.course != enrollment.course or practice.course != enrollment.course):
            raise HTTPException(422, 'Select a lecture and linked practice of the same course.')
        if (enrollment.lecture, enrollment.practice) == (payload.lecture, payload.practice):
            raise HTTPException(409, 'You are already enrolled in these sections. Select a different lecture or practice to swap. Your original enrollment is unchanged.')
        hold = db.get(service.Hold, user_id)
        if (not hold or hold.on_hold or (not service.academic and
                time.time() - hold.verified_at > int(os.getenv('HOLD_VERIFICATION_TTL_SECONDS', '900')))):
            raise HTTPException(409, 'Resolve or verify your account hold before swapping.')
        active = service.active(db)
        others = [e for e in active if e.id != enrollment.id]
        meetings = lecture.meetings + practice.meetings
        if service.conflicts(lecture.meetings, practice.meetings) or any(
                service.conflicts(meetings, e.meetings) for e in others if e.user_id == user_id):
            raise HTTPException(409, 'Target section conflicts with your enrolled schedule.')
        candidate = SimpleNamespace(user_id=user_id, course=enrollment.course,
                                    lecture=payload.lecture, practice=payload.practice)
        if service.registration:
            reason = service.registration.block_reason(db, candidate, lecture, others)
            if reason:
                raise HTTPException(409, reason)
        # Shared sections already contain this student's seat; only new seats are acquired.
        old = {enrollment.lecture, enrollment.practice}
        for section in (lecture, practice):
            if section.section not in old and service.free(section, active) <= 0:
                raise HTTPException(409, 'Target section is now full')
        before = service.seat_counts(db)
        entry = db.query(service.Entry).filter_by(
            term=service.term, user_id=user_id, course=enrollment.course).one()
        enrollment.lecture, enrollment.practice = payload.lecture, payload.practice
        enrollment.meetings = meetings
        entry.lecture, entry.practice = payload.lecture, payload.practice
        entry.status, entry.reason = 'enrolled', ''
        db.flush()
        service.notify_seats(db, before)
        service.promote(db)
    service.wake.set()


def escape(value):
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace(
        '\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def fold(line):
    """RFC 5545 folds at 75 octets without splitting a UTF-8 character."""
    result, current = [], ''
    for character in line:
        if len((current + character).encode('utf-8')) > 75:
            result.append(current)
            current = ' '
        current += character
    return '\r\n'.join(result + [current])


def export_calendar(service, user_id, dates):
    with service.transaction() as db:
        service.require_student(db, user_id)
        enrolled = [e for e in service.active(db) if e.user_id == user_id]
        pending = db.query(service.Entry).filter(service.Entry.term == service.term,
            service.Entry.user_id == user_id, service.Entry.status.in_(['waiting', 'blocked'])).count()
        if not enrolled or pending:
            raise HTTPException(409, 'Schedule must be fully confirmed before export')
        sections = service.sections(db)
        lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Schedule Builder//EN',
                 'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Confirmed class schedule']
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        # The catalog uses Asia/Qyzylorda (UTC+05:00). Emit UTC for calendar portability.
        local = timezone(timedelta(hours=5))
        week_one = dates.start - timedelta(days=dates.start.weekday())
        count = 0
        for enrollment in enrolled:
            for section_id in (enrollment.lecture, enrollment.practice):
                section = sections.get(section_id)
                if not section or not section.meetings:
                    raise HTTPException(409, 'Confirmed section has no published meeting times.')
                for index, meeting in enumerate(section.meetings):
                    day = dates.start
                    while day <= dates.end:
                        week = (day - week_one).days // 7 + 1
                        pattern = meeting.get('week_pattern', 'every_week')
                        if day.isoweekday() == int(meeting['day_of_week']) and (
                            pattern == 'every_week' or pattern == ('odd_weeks' if week % 2 else 'even_weeks')):
                            start = datetime.combine(day, clock_time.fromisoformat(meeting['start_time']), local)
                            end = datetime.combine(day, clock_time.fromisoformat(meeting['end_time']), local)
                            if end <= start:
                                raise HTTPException(409, 'Invalid published meeting time.')
                            uid = hashlib.sha256(f'{service.term}:{user_id}:{section_id}:{index}:{day}'.encode()).hexdigest()
                            lines += ['BEGIN:VEVENT', f'UID:{uid}@schedule-builder', f'DTSTAMP:{stamp}',
                                'DTSTART:' + start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                                'DTEND:' + end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                                'SUMMARY:' + escape(f'{enrollment.course} · {section.name}'),
                                'LOCATION:' + escape(meeting.get('location') or meeting.get('room') or 'Not specified'),
                                'DESCRIPTION:' + escape(f'Section: {section_id}; Term: {service.term}'),
                                'STATUS:CONFIRMED', 'END:VEVENT']
                            count += 1
                        day += timedelta(days=1)
        if not count:
            raise HTTPException(409, 'No classes occur in the selected date range.')
        lines.append('END:VCALENDAR')
        return ('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8')


def install_schedule_actions(app, service, get_user):
    @app.post('/api/enrollments/{enrollment_id}/swap')
    def swap(enrollment_id: int, payload: JoinRequest, user=Depends(get_user)):
        swap_section(service, user.id, enrollment_id, payload)
        return {**service.snapshot(user.id), 'message':'Section swap completed successfully.'}

    @app.post('/api/enrollments/calendar')
    def calendar(payload: CalendarRange, user=Depends(get_user)):
        return Response(export_calendar(service, user.id, payload), media_type='text/calendar',
            headers={'Content-Disposition':'attachment; filename="confirmed-schedule.ics"',
                     'Cache-Control':'no-store'})
