"""US8 — Degree Audit Gap Visualizer (advisor view).

Maps a student's curriculum into degree requirements, then into credit totals
per requirement category (completed / in progress / missing). Each missing
requirement lists the catalog courses that satisfy it, flagged when they can be
taken in the upcoming term (sections open now and prerequisites met).
Access is limited to advisors of the student's department (RBAC).
"""
import re
import time
from collections import Counter

from fastapi import Depends, HTTPException

GENERAL_ED_PREFIXES = {'MDE'}
FAILING_GRADES = {'F', 'FX', 'FAIL', 'NP'}
SEMESTER_CATEGORY = re.compile(r'^(семестр|semester|сем\.?)\s*\d+', re.IGNORECASE)
GENERAL_CATEGORY = re.compile(r'general|gen\.?\s*ed|общеобраз|общие', re.IGNORECASE)

CORE = 'Core Major'
GENERAL = 'General Education'
ELECTIVES = 'Electives'
PRACTICE = 'Practice & Final Attestation'
GROUP_ORDER = [CORE, GENERAL, ELECTIVES, PRACTICE]
CROSS_DEPARTMENT = 'Unauthorized: Student belongs to another department'


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def tidy(value):
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def completed(course):
    if not course:
        return False
    if str(course.get('grade') or '').strip().upper() in FAILING_GRADES:
        return False
    return course.get('completed') is True or str(course.get('completion_status') or '').lower() == 'completed'


def in_progress(course):
    return bool(course) and not completed(course) and (
        course.get('in_progress') is True or str(course.get('completion_status') or '').lower() == 'in_progress')


def slot_semester(slot):
    match = re.search(r'\d+', str(slot))
    return int(match.group()) if match else None


def slot_label(slot):
    text = str(slot)
    return text.split(':', 1)[1].strip() if ':' in text else text


def group_for_required(course, category):
    prefix = re.split(r'[\s\d]', course['code'].strip(), maxsplit=1)[0].upper()
    if prefix in GENERAL_ED_PREFIXES or GENERAL_CATEGORY.search(category):
        return GENERAL
    if not category or SEMESTER_CATEGORY.match(category.strip()):
        return CORE
    return category


PRACTICE_SLOT = re.compile(r'practice|diploma|thesis|exam|практик|диплом', re.IGNORECASE)


def group_for_slot(slot, options):
    types = {str(option.get('elective_type') or '').upper() for option in options}
    if 'NTE' in types or PRACTICE_SLOT.search(slot_label(slot)):
        return PRACTICE
    if types == {'NAE'}:
        return GENERAL
    return ELECTIVES


def slot_order(slot):
    numbers = [int(n) for n in re.findall(r'\d+', slot_label(slot))]
    return (slot_semester(slot) or 99, numbers[:1] or [0], slot)


def build_audit(curriculum):
    """Turn a curriculum into requirements and per-category credit totals."""
    courses, category_of = {}, {}
    for category in curriculum.get('categories') or []:
        for course in category.get('required') or []:
            code = str(course.get('code') or '').strip().upper()
            if code:
                courses[code] = course
                category_of[code] = str(category.get('category') or '')

    def satisfied(course, check):
        return check(course) or any(check(courses.get(str(e.get('code') or '').upper()))
                                    for e in course.get('equivalents') or [])

    requirements, slots, excluded = [], {}, 0
    for code, course in courses.items():
        kind = str(course.get('kind') or 'required').lower()
        if kind == 'requisite':
            excluded += 1
            continue
        if kind == 'elective' and course.get('eligible_slots'):
            for slot in course['eligible_slots']:
                slots.setdefault(slot, []).append(code)
            continue
        if kind == 'elective' and not (course.get('selected_in_curriculum') or completed(course) or in_progress(course)):
            continue  # an unselected elective option is not itself a requirement
        status = ('completed' if satisfied(course, completed) else
                  'in_progress' if satisfied(course, in_progress) else 'missing')
        via = ''
        if status != 'missing' and not (completed(course) or in_progress(course)):
            via = next(str(e.get('code')).upper() for e in course.get('equivalents') or []
                       if completed(courses.get(str(e.get('code') or '').upper()))
                       or in_progress(courses.get(str(e.get('code') or '').upper())))
        requirements.append({'id':'course:' + code, 'kind':'course', 'code':code, 'name':course.get('name') or code,
            'label':code + ' · ' + (course.get('name') or code), 'credits':tidy(number(course.get('credits'))),
            'status':status, 'semester':course.get('semester') or course.get('selected_semester'),
            'grade':course.get('grade'), 'satisfied_by':via,
            'group':ELECTIVES if kind == 'elective' else group_for_required(course, category_of[code]),
            'candidates':[] if status != 'missing' else [code]})

    # Each taken elective fills exactly one slot: first the slot matching its selected
    # semester, then any remaining eligible slot.
    ordered_slots = sorted(slots, key=slot_order)
    taken = {code for codes in slots.values() for code in codes
             if completed(courses[code]) or in_progress(courses[code])}
    chosen, used = {}, set()
    for exact in (True, False):
        for slot in ordered_slots:
            if slot in chosen:
                continue
            options = sorted((c for c in slots[slot] if c in taken and c not in used),
                             key=lambda c: (not completed(courses[c]), c))
            if exact:
                options = [c for c in options if courses[c].get('selected_semester') == slot_semester(slot)]
            if options:
                chosen[slot] = options[0]
                used.add(options[0])
    for slot in ordered_slots:
        options = [courses[code] for code in slots[slot]]
        semester = slot_semester(slot)
        pick = courses[chosen[slot]] if slot in chosen else None
        credits = number(pick.get('credits')) if pick else Counter(number(o.get('credits')) for o in options).most_common(1)[0][0]
        status = 'completed' if pick and completed(pick) else 'in_progress' if pick else 'missing'
        requirements.append({'id':'slot:' + slot, 'kind':'slot', 'code':pick['code'] if pick else '',
            'name':slot_label(slot), 'label':slot_label(slot) + (f' · semester {semester}' if semester else ''),
            'credits':tidy(credits), 'status':status, 'semester':semester,
            'grade':pick.get('grade') if pick else None, 'satisfied_by':pick['code'] if pick else '',
            'group':group_for_slot(slot, options),
            # Options already completed or counted in another slot cannot fill this one.
            'candidates':[] if pick else sorted(o['code'] for o in options if o['code'] not in taken)})

    groups = {}
    for requirement in requirements:
        group = groups.setdefault(requirement['group'], {'key':requirement['group'], 'label':requirement['group'],
            'total':0.0, 'completed':0.0, 'in_progress':0.0, 'missing':0.0,
            'counts':{'completed':0, 'in_progress':0, 'missing':0}})
        group['total'] += requirement['credits']
        group[requirement['status']] += requirement['credits']
        group['counts'][requirement['status']] += 1
    ordered = sorted(groups.values(), key=lambda g: (GROUP_ORDER.index(g['key']) if g['key'] in GROUP_ORDER else len(GROUP_ORDER), g['key']))
    for group in ordered:
        for field in ('total', 'completed', 'in_progress', 'missing'):
            group[field] = tidy(group[field])

    totals = {field: tidy(sum(number(g[field]) for g in ordered)) for field in ('total', 'completed', 'in_progress', 'missing')}
    totals['percent_completed'] = round(100 * number(totals['completed']) / number(totals['total']), 1) if number(totals['total']) else 0
    totals['requirements'] = Counter(r['status'] for r in requirements)
    totals['excluded_requisite_courses'] = excluded

    semesters_in_progress = [r['semester'] for r in requirements if r['status'] == 'in_progress' and r['semester']]
    semesters_completed = [r['semester'] for r in requirements if r['status'] == 'completed' and r['semester']]
    all_semesters = [r['semester'] for r in requirements if r['semester']]
    current = max(semesters_in_progress or semesters_completed or [0])
    upcoming = min(current + 1, max(all_semesters or [current + 1])) if current else (min(all_semesters) if all_semesters else 1)
    return {'requirements':requirements, 'groups':ordered, 'totals':totals,
            'term':{'current_semester':current or None, 'upcoming_semester':upcoming}, 'courses':courses}


class DegreeGapService:
    def __init__(self, sessions, registration, resolver, user_model, load_curriculum):
        self.sessions = sessions
        self.registration = registration
        self.resolver = resolver
        self.user_model = user_model
        self.load_curriculum = load_curriculum

    def advisor(self, db, user_id):
        profile = db.get(self.registration.Profile, user_id)
        if not profile or profile.role != 'advisor':
            raise HTTPException(403, 'Advisor role required.')
        return profile

    def student(self, db, advisor, student_id):
        user = db.get(self.user_model, student_id)
        profile = db.get(self.registration.Profile, student_id)
        if not user or not profile or profile.role != 'student':
            raise HTTPException(404, 'Student not found.')
        if profile.department != advisor.department:
            raise HTTPException(403, CROSS_DEPARTMENT)
        return user, profile

    def students(self, advisor_id):
        with self.sessions() as db:
            advisor = self.advisor(db, advisor_id)
            rows = (db.query(self.registration.Profile, self.user_model)
                    .join(self.user_model, self.user_model.id == self.registration.Profile.user_id)
                    .filter(self.registration.Profile.role == 'student',
                            self.registration.Profile.department == advisor.department)
                    .order_by(self.user_model.name).all())
            students = []
            for profile, user in rows:
                curriculum, source = self.load_curriculum(db, user.id)
                totals = build_audit(curriculum)['totals']
                students.append({'id':user.id, 'name':user.name, 'email':user.email, 'department':profile.department,
                    'curriculum_source':source, 'completed':totals['completed'], 'in_progress':totals['in_progress'],
                    'missing':totals['missing'], 'total':totals['total'], 'percent_completed':totals['percent_completed']})
            return {'department':advisor.department, 'students':students}

    def gaps(self, advisor_id, student_id):
        with self.sessions() as db:
            advisor = self.advisor(db, advisor_id)
            user, profile = self.student(db, advisor, student_id)
            curriculum, source = self.load_curriculum(db, user.id)
            audit = build_audit(curriculum)
            board = self.resolver.seat_board(db)
        courses = audit.pop('courses')
        candidates = {}
        for requirement in audit['requirements']:
            for code in requirement['candidates']:
                if code in candidates:
                    candidates[code]['requirements'].append(requirement['id'])
                    continue
                course = courses.get(code, {})
                prerequisites = []
                for prerequisite in course.get('prerequisites') or []:
                    target = courses.get(str(prerequisite.get('code') or '').upper())
                    state = 'met' if completed(target) else 'in_progress' if in_progress(target) else 'missing'
                    prerequisites.append({'code':prerequisite.get('code') or '', 'name':prerequisite.get('name') or '',
                        'status':state, 'official':prerequisite.get('official')})
                offering = self.resolver.offering(board, code)
                ready = all(p['status'] != 'missing' for p in prerequisites)
                candidates[code] = {'code':code, 'name':course.get('name') or code,
                    'credits':tidy(number(course.get('credits'))), 'kind':course.get('kind') or 'required',
                    'elective_type':course.get('elective_type'),
                    'semester':course.get('semester') or course.get('selected_semester'),
                    'status':'in_progress' if in_progress(course) else 'not_taken',
                    'prerequisites':prerequisites, 'prerequisites_met':ready,
                    **offering, 'available_next_term':offering['open_pairs'] > 0 and ready,
                    'requirements':[requirement['id']]}
        audit['totals']['requirements'] = dict(audit['totals']['requirements'])
        audit['totals']['available_next_term'] = sum(1 for c in candidates.values() if c['available_next_term'])
        return {'student':{'id':user.id, 'name':user.name, 'email':user.email, 'department':profile.department},
                'curriculum':{'name':curriculum.get('name') or 'Curriculum', 'source':source,
                              'updated_at':curriculum.get('updated_at')},
                **audit, 'candidates':candidates, 'generated_at':int(time.time())}


def install_degree_gaps(app, sessions, registration, resolver, user_model, get_user, load_curriculum):
    service = DegreeGapService(sessions, registration, resolver, user_model, load_curriculum)
    app.state.degree_gaps = service

    @app.get('/api/advisor/students')
    def students(user=Depends(get_user)):
        return service.students(user.id)

    @app.get('/api/advisor/students/{student_id}/gaps')
    def gaps(student_id: int, user=Depends(get_user)):
        return service.gaps(user.id, student_id)

    return service
