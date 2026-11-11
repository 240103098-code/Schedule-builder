# Sprint 4 — User stories 7–8

| # | Story | Role | Owner | Deadline |
|---|-------|------|-------|----------|
| 7 | Class Time Conflict Resolver | Student | Abdulla Bakyt | 21.10.2026 |
| 8 | Degree Audit Gap Visualizer | Academic Advisor | Adilzhan Baktybayev | 26.10.2026 |

## US7 — Class Time Conflict Resolver

> As a Student, I want the system to suggest alternative course sections when a time
> overlap occurs so that I can quickly fix schedule conflicts.

New tab **05 · Draft & Conflicts** (students only).

- **Draft builder.** Choose a semester, search the official catalog, pick a lecture and
  its linked practice, and add it to a personal draft. Drafts are saved per account and term
  (`schedule_drafts` table) and never change real enrollments.
- **Real-time detection.** While sections are being chosen, the form already shows whether
  they overlap the draft. After every change the server re-analyses the whole draft:
  conflicting courses get a red indicator in the list, overlapping lessons are outlined in red
  in the weekly grid, and a conflict card names both sections and every overlapping time window.
  Odd/even-week lessons only conflict when their weeks overlap.
- **Suggestions.** For each course in a conflict, the resolver lists up to six lecture/practice
  pairs that are (1) open for enrollment and (2) free of overlaps with the rest of the draft and
  the student's confirmed enrollments. Options that keep the current lecture or practice come
  first. Hovering an option previews it in the grid (dashed outline).
- **One-click substitution.** "Replace with …" re-checks seats and overlaps inside one
  serialized transaction, then updates the draft, clears the warning and redraws the grid.
  If the target section filled up in the meantime the draft is left unchanged and the student
  sees *"Target section … is now full"*.
- **Availability.** A section is *open* only when it has free seats and nobody is waiting in its
  waitlist queue. Academic mode: catalog capacity minus live enrollments made in this app.
  Live mode (`WAITLIST_MODE=live`): the university inventory received by the integration webhook.
  Full, waitlisted and closed sections are never suggested. The tab refreshes every 3 seconds.
- **Confirmed enrollments** (from the Waitlists tab) appear in the draft as locked courses;
  conflicts with them are detected, but only draft courses are moved.
- **Schedule Builder → Draft.** "Open in Draft →" copies the generated schedule on screen
  into the draft for manual adjustments.

API (student session required; advisors receive 403):

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/draft` | Draft with conflicts, suggestions, seat status, summary, `elapsed_ms` |
| GET | `/api/draft/catalog` | Catalog courses with lecture/practice sections and live seats |
| PUT | `/api/draft/courses/{code}` | `{"lecture":"CSS 105.01","practice":"CSS 105.02"}` add or change sections |
| DELETE | `/api/draft/courses/{code}` | Remove a course |
| POST | `/api/draft/resolve` | `{"course","lecture","practice"}` one-click substitution |
| POST | `/api/draft/import` | `{"items":[…]}` copy a generated schedule |
| DELETE | `/api/draft` | Clear the draft |

## US8 — Degree Audit Gap Visualizer

> As an Academic Advisor, I want to view a visual breakdown of a student's missing degree
> requirements so that I can quickly guide their course planning.

New tab **Degree Gap Visualizer** (advisors only, next to *Advisor workspace*).

- **Roster.** Students of the advisor's department with a mini progress bar each.
- **Requirement mapping.** The student's curriculum (uploaded in Degree Progress, otherwise
  the standard one) is mapped into requirements: every required course, plus one requirement
  per elective slot (each taken elective fills one slot). Requisite (RC) courses do not count
  toward degree credits. A course completed directly *or through an equivalent course* is never
  shown as missing; a failing grade (F/FX) does not complete a course.
- **Categories.** Core Major, General Education (MDE courses, NAE electives), Electives (AE),
  Practice & Final Attestation (NTE). Uploaded curricula with their own category names keep them.
- **Charts.** Overall credits (e.g. 90 / 240 ECTS) as a segmented bar and a Chart.js stacked bar
  chart per category: completed, in progress, missing. Tooltips, legend and a data-table view are
  included; colors were validated for color-vision deficiency in light and dark mode.
- **Interactive filtering.** Clicking a bar segment (for example *Core Major → Missing*) lists
  only those requirements together with the catalog courses that satisfy them. Courses that can
  be taken in the upcoming term — open sections now and prerequisites completed or in progress —
  are highlighted with **★ Available next term**. Others are labeled *Prerequisites missing*,
  *Sections full* or *Not offered next term*.
- **RBAC.** Only accounts with the advisor role can open the view. Opening a student from
  another department returns 403 *"Unauthorized: Student belongs to another department"*.

API:

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/advisor/students` | Department roster with credit summaries |
| GET | `/api/advisor/students/{id}/gaps` | Groups, requirements, candidates, term, totals |

Chart.js 4.4.7 (MIT) is bundled in `public/vendor/` so the page also works offline.

## Local demonstration

Start the server as usual (`python main.py`) and register accounts in the UI. Then, from the
project directory, assign roles with the existing administrator CLI:

```powershell
python manage_registration.py profile advisor@example.com --role advisor --department CS
python manage_registration.py profile student@example.com --role student --department CS
python manage_registration.py profile other@example.com --role student --department BUS
```

US7: sign in as the student → **05 · Draft & Conflicts** → add two courses whose sections
overlap (the form warns before adding) → pick a *Replace with …* option. To see the seat rule,
join a section's waitlist on **03 · Waitlists**: that section disappears from the suggestions.

US8: sign in as the advisor → **Degree Gap Visualizer** → choose a student → click the red
*Missing* part of the *Core Major* bar. Opening the BUS student's audit is refused.

## QA mapping

| QA item | Automated test |
|---|---|
| US7 Scenario 1 — red flag + open alternatives for both courses | `test_scenario_1_conflict_is_flagged_with_open_alternatives` |
| US7 Scenario 2 — one-click substitution, warning cleared, grid recalculated | `test_scenario_2_one_click_substitution_clears_the_conflict` |
| US7 Fail — full or waitlisted sections suggested | `test_full_and_waitlisted_sections_are_never_suggested` |
| US7 seat taken during substitution | `test_failed_substitution_keeps_the_draft_unchanged` |
| US7 Deferred — > 3 s on complex overlaps | `test_complex_multi_course_overlaps_are_analysed_quickly` (bundled catalog) |
| US8 Scenario 1 — 90 / 240 breakdown by category | `test_scenario_1_visual_breakdown_for_a_department_student` |
| US8 Scenario 2 — Missing Core Major filter, next-term highlight | `test_scenario_2_missing_core_filter_and_upcoming_term_availability` |
| US8 Fail — completed units shown as unfulfilled | `test_completed_units_are_never_reported_as_missing` |
| US5/US8 RBAC — cross-department access denied | `test_scenario_3_access_is_limited_to_the_advisors_department` |
| Mapping matches the curriculum's own `curriculum_progress` | `test_bundled_curriculum_maps_to_its_own_progress_summary` |

```powershell
python -m unittest discover -s tests -p "test_*.py"
node tests/schedule-generation.cjs
```
