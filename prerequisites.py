"""US3 — Instant Prerequisite Check.

Cross-checks the student's transcript (the saved curriculum statuses and grades)
against each course's prerequisite and corequisite rules:

* a prerequisite is met only by a completed course with a passing grade of C- or
  higher (or P), directly or through an equivalent course;
* a corequisite is also met when it is completed, currently in progress, or in the
  same term's cart (selected together with the course).
"""
from fastapi import Depends
from pydantic import BaseModel, Field

GRADE_SCALE = ['F', 'FX', 'D', 'D+', 'C-', 'C', 'C+', 'B-', 'B', 'B+', 'A-', 'A']
MINIMUM_GRADE = 'C-'
PASS_GRADES = {'P', 'PASS', 'S'}
COREQUISITE_TYPES = {'corequisite', 'co-requisite', 'coreq', 'co'}


class PrerequisiteCheckInput(BaseModel):
    courses: list[str] = Field(default_factory=list, max_length=300)
    cart: list[str] = Field(default_factory=list, max_length=300)


def code_of(value):
    return ' '.join(str(value or '').strip().upper().split())


def is_corequisite(rule):
    return rule.get('corequisite') is True or str(rule.get('type') or '').strip().lower() in COREQUISITE_TYPES


def grade_ok(grade):
    """True when a recorded grade satisfies a prerequisite (no grade recorded counts as passed)."""
    grade = str(grade or '').strip().upper()
    if not grade or grade in PASS_GRADES:
        return True
    if grade not in GRADE_SCALE:
        return True  # unknown local grade notation: trust the completion status
    return GRADE_SCALE.index(grade) >= GRADE_SCALE.index(MINIMUM_GRADE)


def index_courses(curriculum):
    return {code_of(course.get('code')): course for category in curriculum.get('categories') or []
            for course in category.get('required') or [] if code_of(course.get('code'))}


def completed(course):
    return bool(course) and (course.get('completed') is True or
                             str(course.get('completion_status') or '').lower() == 'completed')


def in_progress(course):
    return bool(course) and not completed(course) and (
        course.get('in_progress') is True or str(course.get('completion_status') or '').lower() == 'in_progress')


def rule_state(rule, courses, cart):
    """Return (satisfied, state) for one prerequisite/corequisite rule."""
    code = code_of(rule.get('code'))
    target = courses.get(code)
    candidates = [target] + [courses.get(code_of(e.get('code'))) for e in (target or {}).get('equivalents') or []]
    if any(completed(c) and grade_ok(c.get('grade')) for c in candidates if c):
        return True, 'completed'
    if is_corequisite(rule):
        if any(in_progress(c) for c in candidates if c):
            return True, 'in_progress'
        if code in cart:
            return True, 'in_cart'
    if any(completed(c) for c in candidates if c):
        grade = next(str(c.get('grade')).upper() for c in candidates if c and completed(c))
        return False, 'grade_below_minimum:' + grade
    if any(in_progress(c) for c in candidates if c):
        return False, 'in_progress'
    if target is None:
        return False, 'not_in_curriculum'
    return False, 'not_completed'


STATE_LABELS = {
    'in_progress':'currently in progress, not yet completed',
    'not_completed':'not completed',
    'not_in_curriculum':'not found in your academic record',
}


def describe(state):
    if state.startswith('grade_below_minimum:'):
        return 'grade ' + state.split(':', 1)[1] + ' is below the required ' + MINIMUM_GRADE
    return STATE_LABELS.get(state, state)


def evaluate(curriculum, codes, cart=()):
    courses = index_courses(curriculum)
    cart = {code_of(code) for code in cart}
    results = {}
    for raw in codes:
        code = code_of(raw)
        course = courses.get(code)
        rules = (course or {}).get('prerequisites') or []
        checks, missing = [], []
        for rule in rules:
            ok, state = rule_state(rule, courses, cart)
            entry = {'code':code_of(rule.get('code')), 'name':rule.get('name') or (courses.get(code_of(rule.get('code'))) or {}).get('name', ''),
                     'kind':'corequisite' if is_corequisite(rule) else 'prerequisite', 'satisfied':ok, 'state':state,
                     'detail':describe(state), 'official':rule.get('official')}
            checks.append(entry)
            if not ok:
                missing.append(entry)
        status = 'unknown' if course is None else 'none' if not rules else 'missing' if missing else 'met'
        message = ''
        if status == 'missing':
            message = 'Missing Prerequisite: ' + ', '.join(m['code'] for m in missing)
        elif status == 'met':
            message = 'All prerequisites are met' + (' (corequisite in your cart)' if any(c['state'] == 'in_cart' for c in checks) else '')
        results[code] = {'code':code, 'status':status, 'can_add':status != 'missing', 'message':message,
                         'checks':checks, 'missing':missing}
    return results


def install_prerequisite_check(app, get_user, load_curriculum, sessions):
    @app.post('/api/prerequisites/check')
    def check(payload: PrerequisiteCheckInput, user=Depends(get_user)):
        with sessions() as db:
            curriculum, _ = load_curriculum(db, user.id)
        return {'minimum_grade':MINIMUM_GRADE, 'results':evaluate(curriculum, payload.courses, payload.cart)}
