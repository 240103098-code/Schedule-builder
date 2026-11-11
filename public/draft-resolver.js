/* US7 — Class Time Conflict Resolver (student draft schedule). */
(function () {
  'use strict';
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var PALETTE = ['#F2C14E', '#5FB3B3', '#E8734A', '#8BD3C7', '#C792EA', '#7FB2E5', '#F28FAD', '#A9DC76', '#F0A6CA', '#87C4F0'];
  var LOCKED_COLOR = '#CFC7B4';
  var POLL_MS = 3000, CATALOG_MS = 20000;

  var el = function (id) { return document.getElementById(id); };
  var catalog = null, draft = null, preview = null, busy = false, flash = null;
  var draftRevision = '', pollTimer = null, catalogLoadedAt = 0, generation = 0;

  function active() { return el('paneDraft').classList.contains('active') && !document.hidden; }

  async function api(url, method, payload) {
    var response = await fetch(url, {method: method || 'GET', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: payload ? JSON.stringify(payload) : undefined});
    var body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    if (!response.ok) {
      var error = new Error(body && typeof body.detail === 'string' ? body.detail : 'Request failed. Try again.');
      error.status = response.status; throw error;
    }
    return body;
  }

  function node(tag, className, text) {
    var item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = text;
    return item;
  }

  function chip(text, tone, plain) {
    return node('span', 'chip' + (tone ? ' is-' + tone : '') + (plain ? ' is-plain' : ''), text);
  }

  function seatChip(section) {
    if (section.status === 'open') return chip(section.free + '/' + section.quota + ' seats', 'ok');
    if (section.status === 'full') return chip('Full', 'bad');
    if (section.status === 'waitlisted') return chip('Waitlist · ' + section.waiting, 'warn');
    if (section.status === 'unpublished') return chip('Not published', 'bad');
    return chip('Closed', 'bad');
  }

  function seatText(section) {
    if (section.status === 'open') return section.free + '/' + section.quota + ' seats';
    if (section.status === 'waitlisted') return 'WAITLIST (' + section.waiting + ')';
    return section.status.toUpperCase();
  }

  function setStatus(message, error) {
    el('draftStatus').textContent = message || '';
    el('draftStatus').className = 'status-line' + (error ? ' error' : '');
  }

  // ---------------------------------------------------------------- overlaps
  function minutes(value) { var p = String(value).split(':'); return Number(p[0]) * 60 + Number(p[1]); }
  function weeksOverlap(a, b) {
    var x = a.week_pattern || 'every_week', y = b.week_pattern || 'every_week';
    return x === 'every_week' || y === 'every_week' || x === y;
  }
  function overlap(a, b) {
    if (Number(a.day_of_week) !== Number(b.day_of_week) || !weeksOverlap(a, b)) return null;
    var start = Math.max(minutes(a.start_time), minutes(b.start_time));
    var end = Math.min(minutes(a.end_time), minutes(b.end_time));
    return start < end ? [start, end] : null;
  }
  function hhmm(total) { return String(Math.floor(total / 60)).padStart(2, '0') + ':' + String(total % 60).padStart(2, '0'); }

  // ----------------------------------------------------------------- catalog
  function findCourse(code) {
    return catalog && catalog.courses.find(function (course) { return course.code === code; });
  }

  function inDraft(code) {
    return draft && draft.items.find(function (item) { return item.course === code; });
  }

  function fillCourses() {
    if (!catalog) return;
    var select = el('draftCourse'), previous = select.value;
    var semester = el('draftSemester').value;
    var query = el('draftSearch').value.trim().toLowerCase();
    var courses = catalog.courses.filter(function (course) {
      return (!semester || course.semesters.indexOf(Number(semester)) !== -1)
        && (course.code + ' ' + course.name).toLowerCase().indexOf(query) !== -1;
    });
    select.replaceChildren();
    if (!courses.length) {
      var none = node('option', '', 'No matching courses'); none.value = ''; select.appendChild(none);
    }
    courses.forEach(function (course) {
      var option = node('option', '', course.code + ' · ' + course.name + ' (' + course.credits + ' ECTS)'
        + (course.kind === 'elective' ? ' · elective' : '') + (inDraft(course.code) ? ' · in draft' : ''));
      option.value = course.code; select.appendChild(option);
    });
    if (courses.some(function (course) { return course.code === previous; })) select.value = previous;
    fillLectures();
  }

  function fillLectures(keep) {
    var course = findCourse(el('draftCourse').value), select = el('draftLecture');
    var previous = keep && keep.lecture || select.value;
    var current = course && inDraft(course.code);
    select.replaceChildren();
    (course ? course.lectures : []).forEach(function (lecture) {
      var option = node('option', '', lecture.section + ' · ' + lecture.time + ' · ' + seatText(lecture));
      option.value = lecture.section; select.appendChild(option);
    });
    var wanted = [previous, current && current.lecture];
    var openFirst = course && course.lectures.find(function (l) { return l.status === 'open'; });
    var choice = wanted.find(function (code) { return code && course && course.lectures.some(function (l) { return l.section === code; }); });
    if (choice) select.value = choice; else if (openFirst) select.value = openFirst.section;
    fillPractices(keep);
  }

  function selectedLecture() {
    var course = findCourse(el('draftCourse').value);
    return course && course.lectures.find(function (lecture) { return lecture.section === el('draftLecture').value; });
  }

  function fillPractices(keep) {
    var lecture = selectedLecture(), select = el('draftPractice');
    var previous = keep && keep.practice || select.value;
    var current = inDraft(el('draftCourse').value);
    select.replaceChildren();
    var practices = lecture ? lecture.practices : [];
    if (!practices.length) {
      var none = node('option', '', lecture ? 'No practice for this lecture' : '—'); none.value = ''; select.appendChild(none);
    }
    practices.forEach(function (practice) {
      var option = node('option', '', practice.section + ' · ' + practice.time + ' · ' + seatText(practice));
      option.value = practice.section; select.appendChild(option);
    });
    select.disabled = !practices.length;
    var choice = [previous, current && current.practice].find(function (code) {
      return code && practices.some(function (p) { return p.section === code; });
    });
    var openFirst = practices.find(function (p) { return p.status === 'open'; });
    if (choice) select.value = choice; else if (openFirst) select.value = openFirst.section;
    precheck();
  }

  function selectedPractice() {
    var lecture = selectedLecture();
    return lecture && lecture.practices.find(function (practice) { return practice.section === el('draftPractice').value; });
  }

  /** Real-time overlap detection while the student chooses sections. */
  function precheck() {
    var box = el('draftPrecheck'), button = el('draftAddBtn');
    var code = el('draftCourse').value, lecture = selectedLecture(), practice = selectedPractice();
    box.className = 'draft-precheck'; box.replaceChildren();
    var existing = inDraft(code);
    button.textContent = existing ? 'Update sections in draft' : 'Add to draft';
    if (!catalog || !lecture || (lecture.practices.length && !practice)) { button.disabled = true; return; }
    var chosen = [lecture].concat(practice ? [practice] : []);
    var blocked = chosen.find(function (section) { return section.status !== 'open'; });
    var same = existing && existing.lecture === lecture.section && (existing.practice || '') === (practice ? practice.section : '');
    button.disabled = busy || Boolean(blocked) || Boolean(same);
    if (blocked) {
      box.classList.add('is-bad');
      box.append(node('strong', '', blocked.section + ' is ' + (blocked.status === 'waitlisted' ? 'waitlisted' : blocked.status) + '.'),
        document.createTextNode('Choose an open section, or join its queue on the Waitlists tab.'));
      return;
    }
    if (same) { box.append(document.createTextNode('These sections are already in your draft.')); return; }
    var hits = [];
    var others = draft ? draft.items.concat(draft.enrolled) : [];
    others.forEach(function (item) {
      if (item.course === code) return;
      item.sections.forEach(function (section) {
        chosen.forEach(function (mine) {
          mine.meetings.forEach(function (a) {
            section.meetings.forEach(function (b) {
              var span = overlap(a, b);
              if (span) hits.push(item.course + ' (' + section.section + ') · ' + DAYS[a.day_of_week - 1] + ' ' + hhmm(span[0]) + '–' + hhmm(span[1]));
            });
          });
        });
      });
    });
    if (hits.length) {
      box.classList.add('is-warn');
      box.append(node('strong', '', '⚠ Overlaps ' + Array.from(new Set(hits)).join('; ')),
        document.createTextNode('You can still add it — the resolver will suggest open sections that fit.'));
    } else {
      box.classList.add('is-ok');
      box.append(node('strong', '', '✓ Fits your draft'), document.createTextNode('No time overlaps with the courses already planned.'));
    }
  }

  async function loadCatalog() {
    try {
      catalog = await api('/api/draft/catalog');
      catalogLoadedAt = Date.now();
      var semester = el('draftSemester'), previous = semester.value;
      if (!semester.options.length) {
        semester.appendChild(Object.assign(node('option', '', 'All semesters'), {value: ''}));
        catalog.semesters.forEach(function (value) {
          semester.appendChild(Object.assign(node('option', '', 'Semester ' + value), {value: String(value)}));
        });
        semester.value = previous;
      }
      fillCourses();
    } catch (error) {
      if (error.status !== 403 && error.status !== 401) setStatus(error.message, true);
    }
  }

  // ------------------------------------------------------------- rendering
  function colors() {
    var map = {};
    (draft ? draft.items : []).forEach(function (item, index) { map[item.course] = PALETTE[index % PALETTE.length]; });
    (draft ? draft.enrolled : []).forEach(function (item) { map[item.course] = LOCKED_COLOR; });
    return map;
  }

  function renderList() {
    var list = el('draftList'); list.replaceChildren();
    var palette = colors();
    var entries = draft ? draft.items.concat(draft.enrolled) : [];
    el('draftClearBtn').disabled = busy || !draft || !draft.items.length;
    if (!entries.length) {
      list.appendChild(node('div', 'draft-empty', 'Your draft is empty. Add a course above, or press “Open in Draft →” on a generated schedule in Schedule Builder.'));
      return;
    }
    entries.forEach(function (item) {
      var card = node('div', 'draft-item' + (item.locked ? ' is-locked' : item.status === 'conflict' ? ' is-conflict' : item.status === 'seats' ? ' is-seats' : ''));
      card.style.setProperty('--swatch', palette[item.course]);
      var head = node('div', 'draft-item-head');
      if (item.status === 'conflict') {
        var dot = node('span', 'conflict-dot'); dot.title = 'Time conflict'; dot.setAttribute('aria-label', 'Time conflict'); head.appendChild(dot);
      }
      var title = node('div', 'draft-item-title');
      title.append(node('b', '', item.course), node('span', '', item.name + ' · ' + item.credits + ' ECTS'));
      head.appendChild(title);
      if (item.locked) head.appendChild(chip('Enrolled', 'info'));
      else {
        if (item.status === 'conflict') head.appendChild(chip('Conflict', 'bad'));
        else if (item.status === 'seats') head.appendChild(chip('Seat issue', 'warn'));
        else head.appendChild(chip('OK', 'ok'));
        var remove = node('button', 'draft-remove', '×'); remove.type = 'button'; remove.disabled = busy;
        remove.setAttribute('aria-label', 'Remove ' + item.course + ' from draft');
        remove.addEventListener('click', function () { mutate('/api/draft/courses/' + encodeURIComponent(item.course), 'DELETE'); });
        head.appendChild(remove);
      }
      card.appendChild(head);
      var sections = node('div', 'draft-sections');
      item.sections.forEach(function (section) {
        var line = node('div', 'draft-section');
        line.append(node('code', '', (section.type === 'practice' ? 'P ' : 'L ') + section.section), document.createTextNode(section.time || 'no meetings'));
        if (!item.locked) line.appendChild(seatChip(section));
        sections.appendChild(line);
      });
      card.appendChild(sections);
      if (!item.locked) {
        var edit = node('button', 'planner-link', 'Change sections'); edit.type = 'button';
        edit.addEventListener('click', function () { editItem(item); });
        card.appendChild(edit);
      }
      list.appendChild(card);
    });
  }

  function editItem(item) {
    el('draftSemester').value = ''; el('draftSearch').value = item.course;
    fillCourses();
    el('draftCourse').value = item.course;
    fillLectures({lecture: item.lecture, practice: item.practice});
    el('draftLecture').focus();
  }

  function renderHeader() {
    var summary = draft ? draft.summary : null, health = el('draftHealth');
    if (!summary) { el('draftCounter').textContent = '—'; health.textContent = ''; health.className = 'draft-health'; return; }
    el('draftCounter').innerHTML = '';
    el('draftCounter').append(document.createTextNode(summary.courses + (summary.courses === 1 ? ' course' : ' courses')),
      node('span', 'of', ' · ' + summary.credits + ' ECTS'));
    el('draftNote').textContent = 'Analysed in ' + draft.elapsed_ms + ' ms · term ' + draft.term + ' · '
      + (draft.mode === 'academic' ? 'catalog seats minus live enrollments' : 'live university seat inventory')
      + (draft.enrolled.length ? ' · ' + draft.enrolled.length + ' confirmed enrollment(s) included' : '');
    if (!summary.courses) { health.className = 'draft-health'; health.textContent = 'Empty draft'; }
    else if (summary.conflicts) { health.className = 'draft-health is-bad'; health.textContent = '⚠ ' + summary.conflicts + (summary.conflicts === 1 ? ' time conflict' : ' time conflicts'); }
    else if (summary.problems) { health.className = 'draft-health is-warn'; health.textContent = '⚠ ' + summary.problems + ' seat issue' + (summary.problems === 1 ? '' : 's'); }
    else { health.className = 'draft-health is-ok'; health.textContent = '✓ No conflicts'; }
  }

  function renderBanner() {
    var banner = el('draftBanner'); banner.replaceChildren();
    if (flash) {
      var message = node('div', 'draft-banner is-flash ' + (flash.error ? '' : 'is-ok'), flash.text);
      if (flash.error) { message.style.borderColor = 'var(--danger)'; message.style.color = 'var(--danger)'; }
      banner.appendChild(message);
    } else if (draft && draft.summary.courses && draft.summary.clean) {
      banner.appendChild(node('div', 'draft-banner is-ok', '✓ Conflict-free: all ' + draft.summary.courses
        + ' courses fit together and every section is open for enrollment.'));
    } else if (draft && !draft.summary.courses) {
      banner.appendChild(node('div', 'draft-banner', 'Add courses to start. When two lessons overlap, they are outlined in red and alternative open sections appear here.'));
    }
  }

  function fixButton(option, label) {
    var button = node('button', 'fix-btn'); button.type = 'button'; button.disabled = busy;
    var title = node('b', '', 'Replace with ' + option.lecture + (option.practice ? ' + ' + option.practice : ''));
    var seats = node('span', 'fix-seats', option.free_seats + ' seats');
    var keep = option.changes === 1 ? (option.lecture === label.lecture ? ' · keeps your lecture' : ' · keeps your practice') : '';
    var time = node('span', 'fix-time', 'Lecture ' + option.lecture_time + (option.practice ? ' · Practice ' + option.practice_time : '') + keep);
    button.append(title, seats, time);
    button.setAttribute('aria-label', 'Replace ' + option.course + ' with ' + option.lecture + (option.practice ? ' and ' + option.practice : '') + ', ' + option.time);
    function show() { if (busy) return; preview = option; renderGrid(); }
    function hide() { if (preview === option) { preview = null; renderGrid(); } }
    button.addEventListener('mouseenter', show); button.addEventListener('focus', show);
    button.addEventListener('mouseleave', hide); button.addEventListener('blur', hide);
    button.addEventListener('click', function () {
      preview = null;
      mutate('/api/draft/resolve', 'POST', {course: option.course, lecture: option.lecture, practice: option.practice});
    });
    return button;
  }

  function optionsColumn(side, title) {
    var column = node('div', 'resolver-column');
    var item = draft.items.find(function (entry) { return entry.course === side.course; });
    column.appendChild(node('h4', '', title));
    var list = node('div', 'fix-list');
    if (side.locked) {
      list.appendChild(node('div', 'fix-none', side.course + ' is a confirmed enrollment and stays fixed. Change the other course, or drop the enrollment on the Waitlists tab.'));
    } else {
      var options = draft.suggestions[side.course] || [];
      if (!options.length) list.appendChild(node('div', 'fix-none', 'No open section of ' + side.course + ' fits around the rest of your draft right now.'));
      options.forEach(function (option) { list.appendChild(fixButton(option, item || {})); });
    }
    column.appendChild(list);
    return column;
  }

  function courseName(code) {
    var item = draft.items.concat(draft.enrolled).find(function (entry) { return entry.course === code; });
    return item ? item.name : code;
  }

  function renderResolver() {
    var box = el('draftResolver'); box.replaceChildren();
    if (!draft) return;
    draft.conflicts.forEach(function (conflict) {
      var card = node('section', 'resolver-card');
      var head = node('div', 'resolver-head');
      head.append(chip('Time conflict', 'bad'), node('h3', '', conflict.windows.map(function (w) {
        return w.day + ' ' + w.start + '–' + w.end + (w.week_pattern && w.week_pattern !== 'every_week'
          ? ' (' + (w.week_pattern === 'odd_weeks' ? 'odd' : 'even') + ' weeks)' : '');
      }).join(' · ')));
      if (!conflict.resolvable) head.appendChild(chip('No single-section fix', 'warn'));
      card.appendChild(head);
      var pair = node('p', 'resolver-pair');
      pair.append(node('code', '', conflict.a.section), document.createTextNode(' ' + courseName(conflict.a.course) + (conflict.a.locked ? ' (enrolled)' : '') + ' overlaps '),
        node('code', '', conflict.b.section), document.createTextNode(' ' + courseName(conflict.b.course) + (conflict.b.locked ? ' (enrolled)' : '') + '.'));
      card.appendChild(pair);
      var columns = node('div', 'resolver-columns');
      columns.append(optionsColumn(conflict.a, 'MOVE ' + conflict.a.course), optionsColumn(conflict.b, 'OR MOVE ' + conflict.b.course));
      card.appendChild(columns);
      if (!conflict.resolvable) card.appendChild(node('p', 'fix-none', 'Tip: remove one of these courses, or let Schedule Builder search every combination for you.'));
      box.appendChild(card);
    });
    draft.items.filter(function (item) { return item.status === 'seats'; }).forEach(function (item) {
      var card = node('section', 'resolver-card is-seats');
      var head = node('div', 'resolver-head');
      head.append(chip('Seat availability', 'warn'), node('h3', '', item.course + ' · ' + item.name));
      card.append(head, node('p', 'resolver-pair', item.seat_issues.join(', ') + (item.seat_issues.length === 1 ? ' is' : ' are')
        + ' no longer open (full, waitlisted or closed). Switch to an open section that still fits your week:'));
      var columns = node('div', 'resolver-columns');
      columns.appendChild(optionsColumn({course: item.course, locked: false}, 'OPEN ALTERNATIVES'));
      card.appendChild(columns); box.appendChild(card);
    });
  }

  function renderGrid() {
    var grid = el('draftGrid'); grid.replaceChildren();
    if (!draft || !(draft.items.length + draft.enrolled.length)) {
      grid.appendChild(node('div', 'gap-empty', 'Your weekly grid appears here.'));
      return;
    }
    var palette = colors(), events = [];
    draft.items.concat(draft.enrolled).forEach(function (item) {
      var faded = preview && preview.course === item.course;
      item.sections.forEach(function (section) {
        section.meetings.forEach(function (meeting) {
          events.push({course: item.course, raw: meeting, day: DAYS[meeting.day_of_week - 1], start: meeting.start_time, end: meeting.end_time,
            week_pattern: meeting.week_pattern, name: item.course + ' · ' + item.name, section: section.section + (item.locked ? ' · enrolled' : ''),
            type: section.type, color: palette[item.course], faded: faded, locked: item.locked,
            quota: item.locked ? undefined : section.quota, available: section.free});
        });
      });
    });
    if (preview) {
      var catalogCourse = findCourse(preview.course);
      var lecture = catalogCourse && catalogCourse.lectures.find(function (l) { return l.section === preview.lecture; });
      var practice = lecture && lecture.practices.find(function (p) { return p.section === preview.practice; });
      [lecture, practice].filter(Boolean).forEach(function (section) {
        section.meetings.forEach(function (meeting) {
          events.push({course: preview.course, raw: meeting, day: DAYS[meeting.day_of_week - 1], start: meeting.start_time, end: meeting.end_time,
            week_pattern: meeting.week_pattern, name: preview.course + ' · ' + courseName(preview.course), section: section.section,
            type: section === practice ? 'practice' : 'lecture', color: palette[preview.course], preview: true, flag: 'PREVIEW',
            quota: section.quota, available: section.free});
        });
      });
    }
    var live = events.filter(function (event) { return !event.faded; });
    live.forEach(function (a, i) {
      live.slice(i + 1).forEach(function (b) {
        if (a.course !== b.course && overlap(a.raw, b.raw)) { a.conflict = b.conflict = true; }
      });
    });
    live.forEach(function (event) { if (event.conflict && !event.flag) event.flag = 'CONFLICT'; });
    grid.appendChild(window.ScheduleTimetable.render(events, preview
      ? 'Preview: ' + preview.course + ' moved to ' + preview.lecture + (preview.practice ? ' / ' + preview.practice : '')
      : 'Draft schedule by lesson time'));
  }

  function render(data, message, error) {
    if (data) draft = data;
    if (message !== undefined) {
      flash = message ? {text: message, error: Boolean(error)} : null;
      var shown = flash;
      if (shown) setTimeout(function () { if (flash === shown) { flash = null; renderBanner(); } }, 8000);
    }
    renderHeader(); renderBanner(); renderList(); renderResolver(); renderGrid();
    fillCourses();
    el('draftLive').textContent = 'Live seats · updated ' + new Date().toLocaleTimeString() + ' · refreshes every ' + (POLL_MS / 1000) + ' s';
  }

  async function refresh(force) {
    var version = generation;
    try {
      var data = await api('/api/draft');
      if (version !== generation) return;
      var revision = JSON.stringify(data.items) + JSON.stringify(data.enrolled) + JSON.stringify(data.conflicts) + JSON.stringify(data.suggestions);
      if (!force && revision === draftRevision && preview) return; // keep the hovered preview steady
      var changed = revision !== draftRevision;
      draftRevision = revision;
      if (changed || force) render(data);
      else { draft = data; el('draftLive').textContent = 'Live seats · updated ' + new Date().toLocaleTimeString() + ' · refreshes every ' + (POLL_MS / 1000) + ' s'; }
    } catch (error) {
      if (error.status === 403 || error.status === 401) return;
      el('draftLive').textContent = 'Live updates paused: ' + error.message;
    }
    if (active() && Date.now() - catalogLoadedAt > CATALOG_MS) loadCatalog();
  }

  async function mutate(url, method, payload) {
    if (busy) return;
    busy = true; render(); setStatus('Saving…');
    try {
      var data = await api(url, method, payload);
      draftRevision = '';
      busy = false; preview = null;
      setStatus('');
      render(data, data.message || '');
      loadCatalog();
    } catch (error) {
      busy = false; preview = null;
      setStatus(error.message, true);
      render(null, error.message, true);
      refresh(true);
      loadCatalog();
    }
  }

  el('draftSemester').addEventListener('change', fillCourses);
  el('draftSearch').addEventListener('input', fillCourses);
  el('draftCourse').addEventListener('change', function () { fillLectures(); });
  el('draftLecture').addEventListener('change', function () { fillPractices(); });
  el('draftPractice').addEventListener('change', precheck);
  el('draftAddBtn').addEventListener('click', function () {
    var code = el('draftCourse').value;
    if (!code) return;
    mutate('/api/draft/courses/' + encodeURIComponent(code), 'PUT',
      {lecture: el('draftLecture').value, practice: el('draftPractice').disabled ? '' : el('draftPractice').value});
  });
  el('draftClearBtn').addEventListener('click', function () {
    if (window.confirm('Remove every course from your draft? Confirmed enrollments are not affected.')) mutate('/api/draft', 'DELETE');
  });
  el('tabDraftBtn').addEventListener('click', function () {
    if (!catalog) loadCatalog();
    refresh(true);
  });
  window.addEventListener('draft:updated', function (event) {
    draftRevision = '';
    render(event.detail, event.detail && event.detail.message || '');
    if (!catalog) loadCatalog();
  });

  function start() {
    generation++; catalog = null; draft = null; preview = null; flash = null; draftRevision = '';
    el('draftSemester').replaceChildren(); el('draftSearch').value = '';
    setStatus(''); render();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () { if (active() && !busy) refresh(); }, POLL_MS);
  }
  // Data loads once the account is known to be a student (advisors do not have drafts).
  window.addEventListener('registration:role', function (event) {
    if (event.detail.role === 'student' && !catalog) { loadCatalog(); refresh(true); }
  });
  window.addEventListener('auth:ready', start);
  if (window.scheduleCurrentUser) start();
})();
