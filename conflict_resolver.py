"""US7 — Class Time Conflict Resolver.

A student builds a *draft* schedule section by section. Every change is analysed
immediately: overlapping meetings are flagged, and for each affected course the
service lists alternative lecture/practice pairs that are open for enrollment and
fit the rest of the draft (including the student's confirmed enrollments).
A one-click substitution re-validates seats and overlaps inside one serialized
transaction, so a section that filled up in the meantime is never applied.
"""
import re
import time
from collections import Counter

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Integer, JSON, String, UniqueConstraint

DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
SUGGESTION_LIMIT = 6
DRAFT_LIMIT = 15
OPEN_STATUSES = {'open'}


class DraftCourseInput(BaseModel):
    lecture: str = Field(min_length=1, max_length=100)
    practice: str = Field(default='', max_length=100)


class DraftItemInput(DraftCourseInput):
    course: str = Field(min_length=1, max_length=40)


class ImportInput(BaseModel):
    items: list[DraftItemInput] = Field(default_factory=list, max_length=DRAFT_LIMIT)


def minutes(value):
    hours, mins = str(value).split(':')[:2]
    return int(hours) * 60 + int(mins)


def weeks_overlap(left, right):
    a = left.get('week_pattern') or 'every_week'
    b = right.get('week_pattern') or 'every_week'
    return a == 'every_week' or b == 'every_week' or a == b


def meeting_overlap(a, b):
    """Return the overlapping (start, end) window in minutes, or None."""
    if int(a['day_of_week']) != int(b['day_of_week']) or not weeks_overlap(a, b):
        return None
    start = max(minutes(a['start_time']), minutes(b['start_time']))
    end = min(minutes(a['end_time']), minutes(b['end_time']))
    return (start, end) if start < end else None


def any_overlap(left, right):
    return any(meeting_overlap(a, b) for a in left for b in right)


def internal_overlap(meetings):
    return any(meeting_overlap(meetings[i], meetings[j])
               for i in range(len(meetings)) for j in range(i + 1, len(meetings)))


def hhmm(total):
    return f'{total // 60:02d}:{total % 60:02d}'


def meeting_text(meetings):
    return ', '.join(f"{DAYS[int(m['day_of_week']) - 1]} {m['start_time']}–{m['end_time']}"
                     + ('' if (m.get('week_pattern') or 'every_week') == 'every_week'
                        else ' (' + ('odd' if m['week_pattern'] == 'odd_weeks' else 'even') + ' weeks)')
                     for m in sorted(meetings, key=lambda m: (int(m['day_of_week']), m['start_time'])))


def first_meeting_key(meetings):
    return min(((int(m['day_of_week']), minutes(m['start_time'])) for m in meetings), default=(9, 0))


class ConflictResolver:
    def __init__(self, base, waitlists, registration, curriculum):
        self.waitlists = waitlists
        self.registration = registration

        class Draft(base):
            __tablename__ = 'schedule_drafts'
            id = Column(Integer, primary_key=True)
            term = Column(String(80), nullable=False)
            user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
            items = Column(JSON, nullable=False)
            updated_at = Column(Integer, nullable=False)
            __table_args__ = (UniqueConstraint('term', 'user_id'),)

        self.Draft = Draft
        base.metadata.create_all(bind=waitlists.sessions.kw['bind'])
        self.load_catalog(curriculum)

    # ------------------------------------------------------------------ catalog
    def load_catalog(self, curriculum):
        """Index the official course catalog's published sections."""
        self.courses, self.sections = {}, {}
        for category in curriculum['categories']:
            for course in category['required']:
                lectures = (course.get('schedule') or {}).get('lecture_sections') or []
                if not lectures:
                    continue
                semesters = {course.get('semester')} if course.get('semester') else set()
                for slot in course.get('eligible_slots') or []:
                    match = re.search(r'\d+', str(slot))
                    if match:
                        semesters.add(int(match.group()))
                entry = {'code':course['code'], 'name':course['name'], 'credits':course.get('credits', 0),
                         'kind':course.get('kind', 'required'), 'elective_type':course.get('elective_type'),
                         'semesters':sorted(s for s in semesters if s), 'lectures':[]}
                for lecture in lectures:
                    practices = [p['section'] for p in lecture.get('practice_sections') or []]
                    entry['lectures'].append({'section':lecture['section'], 'practices':practices})
                    for raw, parent in [(lecture, None)] + [(p, lecture['section']) for p in lecture.get('practice_sections') or []]:
                        self.sections[raw['section']] = {
                            'section':raw['section'], 'course':course['code'], 'parent':parent,
                            'meetings':raw.get('meetings') or [], 'quota':int(raw.get('quota') or 0),
                            'count':int(raw.get('count') or 0), 'reserved':int(raw.get('reserved') or 0),
                            'available_places':raw.get('available_places'),
                            'availability':raw.get('availability') or 'Available'}
                self.courses[course['code']] = entry

    def seat_board(self, db):
        """Real-time seat status for every published section.

        Academic mode: catalog capacity minus seats taken by this app's live enrollments.
        Live mode: the inventory pushed by the university integration webhook.
        A section with students in its waitlist queue is never offered as open.
        """
        active = self.waitlists.active(db)
        taken = Counter()
        for enrollment in active:
            taken[enrollment.lecture] += 1
            taken[enrollment.practice] += 1
        queued = Counter()
        entries = db.query(self.waitlists.Entry).filter(self.waitlists.Entry.term == self.waitlists.term,
            self.waitlists.Entry.status.in_(['waiting', 'blocked'])).all()
        for entry in entries:
            queued[entry.lecture] += 1
            queued[entry.practice] += 1
        inventory = {} if self.waitlists.academic else self.waitlists.sections(db)
        board = {}
        for code, section in self.sections.items():
            quota = section['quota']
            if code in inventory:
                free, quota = self.waitlists.free(inventory[code], active), inventory[code].quota
            else:
                capacity = quota - section['count'] - section['reserved']
                if section['available_places'] is not None:
                    capacity = min(capacity, int(section['available_places']))
                free = max(0, capacity - taken[code])
            closed = str(section['availability']).strip().lower() != 'available'
            status = ('closed' if closed else 'waitlisted' if queued[code]
                      else 'full' if free <= 0 else 'open')
            board[code] = {'free':free, 'quota':quota, 'waiting':queued[code], 'status':status}
        return board

    def offering(self, board, code):
        """Whether a course has at least one open lecture/practice pair right now."""
        course = self.courses.get(code)
        if not course:
            return {'offered':False, 'open_pairs':0, 'sections':0}
        pairs = sum(1 for lecture in course['lectures'] if board[lecture['section']]['status'] in OPEN_STATUSES
                    for practice in (lecture['practices'] or [None])
                    if practice is None or board[practice]['status'] in OPEN_STATUSES)
        sections = sum(1 + len(lecture['practices']) for lecture in course['lectures'])
        return {'offered':True, 'open_pairs':pairs, 'sections':sections}

    def section_view(self, code, board):
        section = self.sections[code]
        return {'section':code, 'meetings':section['meetings'], 'time':meeting_text(section['meetings']),
                **board[code]}

    def catalog(self, user_id):
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            board = self.seat_board(db)
        courses = []
        for course in sorted(self.courses.values(), key=lambda c: (c['semesters'][:1] or [99], c['code'])):
            courses.append({**{k: course[k] for k in ('code', 'name', 'credits', 'kind', 'elective_type', 'semesters')},
                'lectures':[{**self.section_view(lecture['section'], board),
                             'practices':[self.section_view(p, board) for p in lecture['practices']]}
                            for lecture in course['lectures']]})
        semesters = sorted({s for course in self.courses.values() for s in course['semesters']})
        return {'term':self.waitlists.term, 'semesters':semesters, 'courses':courses}

    # ------------------------------------------------------------- validation
    def validate_pair(self, code, lecture, practice):
        course = self.courses.get(code)
        if not course:
            raise HTTPException(404, f'{code} has no published sections in the course catalog.')
        option = next((item for item in course['lectures'] if item['section'] == lecture), None)
        if not option:
            raise HTTPException(422, f'{lecture} is not a lecture section of {code}.')
        if option['practices'] and practice not in option['practices']:
            raise HTTPException(422, f'Choose a practice linked to lecture {lecture}.')
        if not option['practices'] and practice:
            raise HTTPException(422, f'Lecture {lecture} has no linked practice sections.')
        meetings = self.meetings(lecture, practice)
        if internal_overlap(meetings):
            raise HTTPException(422, 'These lecture and practice meetings overlap.')
        return meetings

    def meetings(self, lecture, practice):
        return list(self.sections[lecture]['meetings']) + (list(self.sections[practice]['meetings']) if practice else [])

    @staticmethod
    def seat_problem(board, sections):
        for code in sections:
            status = board[code]['status']
            if status == 'full':
                return f'Target section {code} is now full.'
            if status == 'waitlisted':
                return f'Section {code} has a waitlist queue ({board[code]["waiting"]} waiting); it cannot be offered as open.'
            if status == 'closed':
                return f'Section {code} is closed for enrollment.'
        return ''

    # ------------------------------------------------------------------ state
    def draft_row(self, db, user_id, create=False):
        row = db.query(self.Draft).filter_by(term=self.waitlists.term, user_id=user_id).first()
        if not row and create:
            row = self.Draft(term=self.waitlists.term, user_id=user_id, items=[], updated_at=int(time.time()))
            db.add(row)
        return row

    def enrolled(self, db, user_id):
        return [e for e in self.waitlists.active(db) if e.user_id == user_id]

    def blocks(self, db, user_id, items):
        """Draft items and confirmed enrollments as comparable meeting blocks."""
        blocks = []
        for index, item in enumerate(items):
            sections = [s for s in (item['lecture'], item.get('practice') or '') if s]
            blocks.append({'course':item['course'], 'locked':False, 'index':index,
                'sections':[(s, self.sections[s]['meetings'] if s in self.sections else []) for s in sections]})
        for enrollment in self.enrolled(db, user_id):
            if any(b['course'] == enrollment.course for b in blocks):
                continue
            # Confirmed enrollment meetings are stored together; split them by section when possible.
            sections = [(code, self.sections[code]['meetings'] if code in self.sections else [])
                        for code in (enrollment.lecture, enrollment.practice)]
            if not any(m for _, m in sections):
                sections = [(enrollment.lecture, enrollment.meetings or [])]
            blocks.append({'course':enrollment.course, 'locked':True, 'index':None, 'sections':sections})
        return blocks

    def conflicts(self, blocks):
        """One conflict per overlapping section pair, listing every overlapping time window."""
        grouped = {}
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                for left_code, left in blocks[i]['sections']:
                    for right_code, right in blocks[j]['sections']:
                        for a in left:
                            for b in right:
                                window = meeting_overlap(a, b)
                                if not window:
                                    continue
                                pattern = a.get('week_pattern') if (a.get('week_pattern') or 'every_week') != 'every_week' \
                                    else (b.get('week_pattern') or 'every_week')
                                key = f'{left_code}|{right_code}'
                                conflict = grouped.setdefault(key, {'id':key,
                                    'a':{'course':blocks[i]['course'], 'section':left_code, 'locked':blocks[i]['locked']},
                                    'b':{'course':blocks[j]['course'], 'section':right_code, 'locked':blocks[j]['locked']},
                                    'windows':[]})
                                entry = {'day':DAYS[int(a['day_of_week']) - 1], 'start':hhmm(window[0]),
                                         'end':hhmm(window[1]), 'week_pattern':pattern}
                                if entry not in conflict['windows']:
                                    conflict['windows'].append(entry)
        result = []
        for conflict in grouped.values():
            conflict['windows'].sort(key=lambda w: (DAYS.index(w['day']), w['start']))
            result.append({**conflict, **conflict['windows'][0]})  # first window kept flat for convenience
        return result

    def suggestions(self, code, current, others, board):
        """Open lecture/practice pairs of `code` that fit every other block."""
        course = self.courses.get(code)
        if not course:
            return []
        rest = [m for block in others for _, meetings in block['sections'] for m in meetings]
        options = []
        for lecture in course['lectures']:
            if board[lecture['section']]['status'] not in OPEN_STATUSES:
                continue
            for practice in lecture['practices'] or ['']:
                if practice and board[practice]['status'] not in OPEN_STATUSES:
                    continue
                if (lecture['section'], practice) == (current.get('lecture'), current.get('practice') or ''):
                    continue
                meetings = self.meetings(lecture['section'], practice)
                if internal_overlap(meetings) or any_overlap(meetings, rest):
                    continue
                changes = int(lecture['section'] != current.get('lecture')) + int(practice != (current.get('practice') or ''))
                seats = min(board[lecture['section']]['free'], board[practice]['free'] if practice else 10 ** 6)
                options.append({'course':code, 'lecture':lecture['section'], 'practice':practice, 'changes':changes,
                    'lecture_time':meeting_text(self.sections[lecture['section']]['meetings']),
                    'practice_time':meeting_text(self.sections[practice]['meetings']) if practice else '',
                    'meetings':meetings, 'time':meeting_text(meetings), 'free_seats':seats,
                    '_order':(changes, first_meeting_key(meetings), lecture['section'], practice)})
        options.sort(key=lambda option: option['_order'])
        for option in options:
            option.pop('_order')
        return options[:SUGGESTION_LIMIT]

    def analyze(self, db, user_id, items):
        started = time.perf_counter()
        board = self.seat_board(db)
        blocks = self.blocks(db, user_id, items)
        conflicts = self.conflicts(blocks)
        conflicted = {}
        for conflict in conflicts:
            for side in ('a', 'b'):
                conflicted.setdefault(conflict[side]['course'], []).append(conflict['id'])
        views, suggestions, credits = [], {}, 0
        for block in blocks:
            code = block['course']
            course = self.courses.get(code, {})
            sections = []
            for section_code, meetings in block['sections']:
                known = section_code in self.sections
                sections.append({'section':section_code,
                    'type':'practice' if known and self.sections[section_code]['parent'] else 'lecture',
                    'meetings':meetings, 'time':meeting_text(meetings),
                    **(board[section_code] if known else {'status':'unpublished', 'free':0, 'quota':0, 'waiting':0})})
            seat_issues = [] if block['locked'] else [
                s['section'] for s in sections if s['status'] not in OPEN_STATUSES]
            status = 'conflict' if code in conflicted else 'seats' if seat_issues else 'ok'
            if not block['locked']:
                credits += float(course.get('credits') or 0)
            view = {'course':code, 'name':course.get('name', code), 'credits':course.get('credits', 0),
                    'locked':block['locked'], 'sections':sections, 'status':status,
                    'conflict_ids':conflicted.get(code, []), 'seat_issues':seat_issues}
            if not block['locked']:
                view['lecture'] = items[block['index']]['lecture']
                view['practice'] = items[block['index']].get('practice') or ''
            views.append(view)
            if status != 'ok' and not block['locked']:
                current = items[block['index']]
                others = [b for b in blocks if b is not block]
                suggestions[code] = self.suggestions(code, current, others, board)
        for conflict in conflicts:
            conflict['resolvable'] = any(suggestions.get(conflict[side]['course']) for side in ('a', 'b')
                                         if not conflict[side]['locked'])
        draft_items = [v for v in views if not v['locked']]
        problems = sum(1 for v in draft_items if v['status'] != 'ok')
        return {'term':self.waitlists.term, 'mode':'academic' if self.waitlists.academic else 'live',
                'items':draft_items, 'enrolled':[v for v in views if v['locked']],
                'conflicts':conflicts, 'suggestions':suggestions,
                'summary':{'courses':len(draft_items), 'credits':int(credits) if float(credits).is_integer() else credits,
                           'conflicts':len(conflicts), 'problems':problems, 'clean':not conflicts and not problems},
                'elapsed_ms':round((time.perf_counter() - started) * 1000, 2)}

    def snapshot(self, user_id, message=''):
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            row = self.draft_row(db, user_id)
            items = list(row.items) if row else []
            result = self.analyze(db, user_id, items)
            result['updated_at'] = row.updated_at if row else None
        if message:
            result['message'] = message
        return result

    def save(self, db, row, items):
        row.items = [dict(item) for item in items]
        row.updated_at = int(time.time())

    # -------------------------------------------------------------- mutations
    def put_course(self, user_id, code, payload):
        code = code.strip().upper()
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            self.validate_pair(code, payload.lecture, payload.practice)
            if any(e.course == code for e in self.enrolled(db, user_id)):
                raise HTTPException(409, f'You are already enrolled in {code}; it is shown in the draft as a confirmed course.')
            row = self.draft_row(db, user_id, create=True)
            items = [item for item in row.items if item['course'] != code]
            existing = len(items) != len(row.items)
            if not existing and len(items) >= DRAFT_LIMIT:
                raise HTTPException(409, f'A draft can contain at most {DRAFT_LIMIT} courses.')
            problem = self.seat_problem(self.seat_board(db), [s for s in (payload.lecture, payload.practice) if s])
            if problem:
                raise HTTPException(409, problem + ' Choose an open section or join the waitlist instead.')
            item = {'course':code, 'lecture':payload.lecture, 'practice':payload.practice}
            if existing:
                items = [item if old['course'] == code else old for old in row.items]
            else:
                items.append(item)
            self.save(db, row, items)
        return self.snapshot(user_id, f'{code} {"updated" if existing else "added to your draft"}.')

    def remove_course(self, user_id, code):
        code = code.strip().upper()
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            row = self.draft_row(db, user_id)
            if not row or not any(item['course'] == code for item in row.items):
                raise HTTPException(404, f'{code} is not in your draft.')
            self.save(db, row, [item for item in row.items if item['course'] != code])
        return self.snapshot(user_id, f'{code} removed from your draft.')

    def clear(self, user_id):
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            row = self.draft_row(db, user_id)
            if row:
                self.save(db, row, [])
        return self.snapshot(user_id, 'Draft cleared.')

    def import_items(self, user_id, payload):
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            seen, items = set(), []
            enrolled = {e.course for e in self.enrolled(db, user_id)}
            board = self.seat_board(db)
            for raw in payload.items:
                code = raw.course.strip().upper()
                if code in seen:
                    raise HTTPException(422, f'{code} appears more than once.')
                seen.add(code)
                if code in enrolled:
                    continue
                self.validate_pair(code, raw.lecture, raw.practice)
                problem = self.seat_problem(board, [s for s in (raw.lecture, raw.practice) if s])
                if problem:
                    raise HTTPException(409, problem + ' Generate the schedule again to use current seats.')
                items.append({'course':code, 'lecture':raw.lecture, 'practice':raw.practice})
            row = self.draft_row(db, user_id, create=True)
            self.save(db, row, items)
        return self.snapshot(user_id, f'{len(items)} courses copied from Schedule Builder into your draft.')

    def resolve(self, user_id, payload):
        """One-click substitution, re-validated against live seats and the rest of the draft."""
        code = payload.course.strip().upper()
        with self.waitlists.transaction() as db:
            self.registration.require_student(db, user_id)
            row = self.draft_row(db, user_id)
            current = next((item for item in (row.items if row else []) if item['course'] == code), None)
            if not current:
                raise HTTPException(404, f'{code} is not in your draft.')
            meetings = self.validate_pair(code, payload.lecture, payload.practice)
            problem = self.seat_problem(self.seat_board(db), [s for s in (payload.lecture, payload.practice) if s])
            if problem:
                raise HTTPException(409, problem + ' Your draft was not changed.')
            others = [b for b in self.blocks(db, user_id, row.items) if b['course'] != code]
            for block in others:
                for section_code, other in block['sections']:
                    for a in meetings:
                        for b in other:
                            window = meeting_overlap(a, b)
                            if window:
                                raise HTTPException(409, f'This option now overlaps {block["course"]} ({section_code}) on '
                                    f'{DAYS[int(a["day_of_week"]) - 1]} {hhmm(window[0])}–{hhmm(window[1])}. Your draft was not changed.')
            replacement = {'course':code, 'lecture':payload.lecture, 'practice':payload.practice}
            self.save(db, row, [replacement if item['course'] == code else item for item in row.items])
        label = payload.lecture + (' / ' + payload.practice if payload.practice else '')
        return self.snapshot(user_id, f'{code} replaced with {label}. The draft was recalculated.')


def install_conflict_resolver(app, base, waitlists, registration, get_user, curriculum):
    service = ConflictResolver(base, waitlists, registration, curriculum)
    app.state.conflict_resolver = service

    @app.get('/api/draft')
    def draft(user=Depends(get_user)):
        return service.snapshot(user.id)

    @app.get('/api/draft/catalog')
    def catalog(user=Depends(get_user)):
        return service.catalog(user.id)

    @app.put('/api/draft/courses/{code}')
    def put_course(code: str, payload: DraftCourseInput, user=Depends(get_user)):
        return service.put_course(user.id, code, payload)

    @app.delete('/api/draft/courses/{code}')
    def remove_course(code: str, user=Depends(get_user)):
        return service.remove_course(user.id, code)

    @app.post('/api/draft/resolve')
    def resolve(payload: DraftItemInput, user=Depends(get_user)):
        return service.resolve(user.id, payload)

    @app.post('/api/draft/import')
    def import_items(payload: ImportInput, user=Depends(get_user)):
        return service.import_items(user.id, payload)

    @app.delete('/api/draft')
    def clear(user=Depends(get_user)):
        return service.clear(user.id)

    return service
