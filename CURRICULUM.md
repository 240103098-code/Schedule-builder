# Curriculum schedules and themes

The bundled curriculum is in `data/curriculum_subjects_and_electives_with_schedule.json`.
Schedule Builder loads its semester, lecture, practice, and seat-quota data automatically.
Choose a semester, select the required courses and electives, then click
**Generate schedules**. Section names, meeting times, and quotas are fixed by the curriculum
and cannot be edited or added in Schedule Builder. Each practice is paired only with its parent lecture;
full sections (including occupied and reserved seats) are excluded.

Optionally select a **Day off**: all results must keep that day free. Valid schedules
are ranked to favor short gaps and fewer days with only one or two lessons. Ranking
counts each meeting as a lesson, penalizes total idle minutes, adds extra weight to
gaps over an hour, and penalizes days with fewer than three lessons. These are
preferences, so less compact schedules remain available when unavoidable.
The search retains the best 500 schedules found within its 400,000-node limit;
the results indicate when that limit is reached.

The supplied timetable is generated example data, not an official university timetable.
Times use `Asia/Qyzylorda`. Updated JSON imports in Degree Progress retain their section data;
the upload limit is 5 MB. Existing imports recover missing section data by course code
from the bundled curriculum.

The timetable uses one row for each exact lesson interval (for example, 13:30–14:20).
Schedule Builder and the selected-quota preview in Waitlists share this table.
Odd/even-week lessons are labeled and only conflict when their weeks overlap;
gap and lesson-count preferences are evaluated separately for each week pattern.
Replacing the bundled JSON and restarting the app updates existing saved curriculum
schedules by course code and refreshes waitlist meeting times while retaining queue
positions and seat inventory. Imports saved against the current bundle retain their
own section schedules until a new bundled curriculum is installed.

The light/dark toggle applies to sign-in, Schedule Builder, and Degree Progress.
It initially follows the system theme and remembers an explicit choice in the browser.
