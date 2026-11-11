/* US8 — Degree Audit Gap Visualizer (academic advisor view). */
(function () {
  'use strict';
  var STATUSES = [
    {key: 'completed', label: 'Completed', color: '--gap-completed'},
    {key: 'in_progress', label: 'In progress', color: '--gap-progress'},
    {key: 'missing', label: 'Missing', color: '--gap-missing'}
  ];
  var el = function (id) { return document.getElementById(id); };
  var roster = null, selected = null, data = null, chart = null, showTable = false, version = 0;
  var filter = {group: null, status: 'missing'};

  async function api(url) {
    var response = await fetch(url, {credentials: 'same-origin'});
    var body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    if (!response.ok) {
      var error = new Error(body && typeof body.detail === 'string' ? body.detail : 'Request failed.');
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
  function chip(text, tone, plain) { return node('span', 'chip' + (tone ? ' is-' + tone : '') + (plain ? ' is-plain' : ''), text); }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function fmt(value) { return Number(value).toLocaleString('en-US', {maximumFractionDigits: 1}); }
  function status(message, error) { el('gapStatus').textContent = message || ''; el('gapStatus').className = 'status-line' + (error ? ' error' : ''); }
  function statusLabel(key) { return STATUSES.find(function (s) { return s.key === key; }).label; }

  function segBar(parts, total, tall) {
    var bar = node('div', 'seg-bar' + (tall ? ' is-tall' : ''));
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', parts.map(function (p) { return p.label + ' ' + fmt(p.value) + ' of ' + fmt(total) + ' ECTS'; }).join(', '));
    parts.forEach(function (part) {
      if (!part.value) return;
      var span = node('span', part.className); span.style.width = (100 * part.value / (total || 1)) + '%';
      bar.appendChild(span);
    });
    return bar;
  }
  function parts(source) {
    return [{label: 'Completed', value: source.completed, className: 'seg-completed'},
      {label: 'In progress', value: source.in_progress, className: 'seg-progress'},
      {label: 'Missing', value: source.missing, className: 'seg-missing'}];
  }

  // ------------------------------------------------------------------ roster
  function renderRoster() {
    var list = el('gapStudents'); list.replaceChildren();
    if (!roster) return;
    var query = el('gapSearch').value.trim().toLowerCase();
    var students = roster.students.filter(function (s) { return (s.name + ' ' + s.email).toLowerCase().indexOf(query) !== -1; });
    if (!roster.students.length) {
      list.appendChild(node('div', 'draft-empty', 'No students are assigned to department ' + roster.department
        + ' yet. A student appears here after an administrator assigns the department (or after their first sign-in in academic mode).'));
      return;
    }
    if (!students.length) list.appendChild(node('div', 'draft-empty', 'No students match “' + query + '”.'));
    students.forEach(function (student) {
      var button = node('button', 'gap-student'); button.type = 'button';
      button.setAttribute('aria-pressed', String(student.id === selected));
      button.append(node('b', '', student.name),
        node('small', '', student.email + ' · ' + student.percent_completed + '% · ' + fmt(student.completed) + '/' + fmt(student.total) + ' ECTS'),
        segBar(parts(student), student.total));
      button.addEventListener('click', function () { select(student.id); });
      list.appendChild(button);
    });
  }

  async function loadRoster() {
    var current = version;
    try {
      roster = await api('/api/advisor/students');
      if (current !== version) return;
      status(roster.students.length + ' student' + (roster.students.length === 1 ? '' : 's') + ' in department ' + roster.department + '.');
      renderRoster();
      if (!selected && roster.students.length) select(roster.students[0].id);
      if (!roster.students.length) renderEmpty('No students yet', 'Students of your department will be listed on the left.');
    } catch (error) {
      if (error.status === 401 || error.status === 403) return;
      status(error.message, true);
    }
  }

  async function select(id) {
    selected = id; renderRoster();
    var current = ++version;
    el('gapStudentName').textContent = 'Loading…';
    try {
      var result = await api('/api/advisor/students/' + id + '/gaps');
      if (current !== version) return;
      data = result; filter = {group: null, status: 'missing'};
      renderBody();
    } catch (error) {
      if (current !== version) return;
      data = null;
      el('gapStudentName').textContent = 'Access denied';
      el('gapStudentMeta').textContent = '';
      renderEmpty(error.status === 403 ? 'Unauthorized' : 'Could not load the audit', error.message);
    }
  }

  function renderEmpty(title, copy) {
    destroyChart();
    var body = el('gapBody'); body.replaceChildren();
    var empty = node('div', 'gap-empty'); empty.append(node('b', '', title), document.createTextNode(copy));
    body.appendChild(empty);
  }

  // -------------------------------------------------------------------- body
  function renderBody() {
    var body = el('gapBody'); body.replaceChildren();
    var totals = data.totals;
    el('gapStudentName').textContent = data.student.name;
    el('gapStudentMeta').textContent = data.student.email + ' · ' + data.student.department + ' · ' + data.curriculum.name
      + (data.curriculum.source === 'default' ? ' (standard curriculum)' : ' (uploaded by student)')
      + (data.term.current_semester ? ' · now in semester ' + data.term.current_semester : '')
      + ' · upcoming: semester ' + data.term.upcoming_semester;

    var hero = node('div', 'gap-hero');
    var overall = node('section', 'gap-card');
    overall.append(node('h3', '', 'Overall progress'), node('p', 'gap-card-sub', 'Credits toward the degree'));
    var big = node('p', 'gap-big', fmt(totals.completed));
    big.appendChild(node('small', '', ' / ' + fmt(totals.total) + ' ECTS'));
    overall.append(big, node('p', 'gap-big-caption', totals.percent_completed + '% completed · ' + fmt(totals.in_progress) + ' in progress · ' + fmt(totals.missing) + ' still missing'),
      segBar(parts(totals), totals.total, true));
    overall.appendChild(legend());
    var breakdown = node('section', 'gap-card');
    breakdown.append(node('h3', '', 'Requirements by category'),
      node('p', 'gap-card-sub', 'ECTS credits completed, in progress and missing. Click a bar segment to list the matching requirements below.'),
      legend());
    var wrap = node('div', 'gap-chart-wrap'); wrap.style.height = (data.groups.length * 46 + 54) + 'px';
    var canvas = node('canvas'); canvas.id = 'gapChart'; canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Stacked bar chart of credits per category: ' + data.groups.map(function (g) {
      return g.label + ' ' + fmt(g.completed) + ' completed, ' + fmt(g.in_progress) + ' in progress, ' + fmt(g.missing) + ' missing';
    }).join('; '));
    wrap.appendChild(canvas); breakdown.appendChild(wrap);
    var toggle = node('button', 'planner-link gap-table-toggle', showTable ? 'Hide data table' : 'Show as table'); toggle.type = 'button';
    toggle.addEventListener('click', function () { showTable = !showTable; renderBody(); });
    breakdown.appendChild(toggle);
    if (showTable) breakdown.appendChild(table());
    hero.append(overall, breakdown); body.appendChild(hero);
    body.appendChild(tiles());

    var head = node('div', 'gap-filter-title');
    head.append(node('h2', '', 'Course planning'), node('span', '', 'Highlighted = can be taken next term'));
    body.append(head, filters(), node('p', 'search-note', ''));
    body.lastChild.id = 'gapFilterNote';
    var list = node('div', 'req-list'); list.id = 'gapRequirements'; body.appendChild(list);
    renderRequirements();
    drawChart(canvas);
  }

  function legend() {
    var box = node('div', 'gap-legend');
    STATUSES.forEach(function (s) {
      var item = node('span'); var swatch = node('i'); swatch.style.background = 'var(' + s.color + ')';
      item.append(swatch, document.createTextNode(s.label)); box.appendChild(item);
    });
    return box;
  }

  function tiles() {
    var counts = data.totals.requirements, all = (counts.completed || 0) + (counts.in_progress || 0) + (counts.missing || 0);
    var box = node('div', 'gap-tiles');
    [['Requirements met', counts.completed || 0, ' of ' + all, '--gap-completed'],
      ['In progress', counts.in_progress || 0, ' requirements', '--gap-progress'],
      ['Still missing', counts.missing || 0, ' requirements', '--gap-missing'],
      ['Ready next term', data.totals.available_next_term, ' courses', '--ok']].forEach(function (tile) {
      var card = node('div', 'gap-tile'), k = node('p', 'k'), swatch = node('i');
      swatch.style.background = 'var(' + tile[3] + ')'; k.append(swatch, document.createTextNode(tile[0]));
      var v = node('p', 'v', String(tile[1])); v.appendChild(node('small', '', tile[2]));
      card.append(k, v); box.appendChild(card);
    });
    return box;
  }

  function table() {
    var t = node('table', 'gap-table'), head = node('thead'), row = node('tr');
    ['Category', 'Completed', 'In progress', 'Missing', 'Total'].forEach(function (label) { row.appendChild(node('th', '', label)); });
    head.appendChild(row); t.appendChild(head);
    var body = node('tbody');
    data.groups.concat([Object.assign({label: 'All'}, data.totals)]).forEach(function (group) {
      var tr = node('tr');
      [group.label, fmt(group.completed), fmt(group.in_progress), fmt(group.missing), fmt(group.total)].forEach(function (value) { tr.appendChild(node('td', '', value)); });
      body.appendChild(tr);
    });
    t.appendChild(body); return t;
  }

  function filters() {
    var box = node('div', 'gap-filters'); box.id = 'gapFilters';
    function button(label, pressed, handler) {
      var b = node('button', 'gap-filter', label); b.type = 'button'; b.setAttribute('aria-pressed', String(pressed));
      b.addEventListener('click', handler); box.appendChild(b);
    }
    button('All categories', !filter.group, function () { setFilter(null, filter.status); });
    data.groups.forEach(function (group) {
      button(group.label + ' · ' + fmt(group[filter.status]), filter.group === group.key, function () { setFilter(group.key, filter.status); });
    });
    box.appendChild(node('span', 'chip is-plain is-mute', 'show'));
    STATUSES.slice().reverse().forEach(function (s) {
      button(s.label, filter.status === s.key, function () { setFilter(filter.group, s.key); });
    });
    return box;
  }

  function setFilter(group, state) {
    filter = {group: group, status: state};
    var old = el('gapFilters'); old.replaceWith(filters());
    renderRequirements();
    if (chart) chart.update('none');
  }

  function rank(candidate) {
    return candidate.available_next_term ? 0 : candidate.open_pairs ? 1 : candidate.offered ? 2 : 3;
  }

  function optionCard(code) {
    var candidate = data.candidates[code];
    var card = node('div', 'req-option' + (candidate.available_next_term ? ' is-ready' : (!candidate.offered ? ' is-muted' : '')));
    var head = node('div', 'planner-row');
    head.append(node('b', '', candidate.code), node('span', 'chip is-plain is-mute', candidate.credits + ' ECTS'));
    card.append(head, node('p', '', candidate.name));
    var chips = node('div', 'planner-row'); chips.style.justifyContent = 'flex-start'; chips.style.flexWrap = 'wrap';
    if (candidate.available_next_term) chips.appendChild(chip('★ Available next term', 'ok'));
    else if (!candidate.offered) chips.appendChild(chip('Not offered next term', 'mute'));
    else if (!candidate.prerequisites_met) chips.appendChild(chip('Prerequisites missing', 'bad'));
    else if (!candidate.open_pairs) chips.appendChild(chip('Sections full', 'warn'));
    if (candidate.offered && candidate.open_pairs) chips.appendChild(chip(candidate.open_pairs + ' open section pair' + (candidate.open_pairs === 1 ? '' : 's'), 'info', true));
    if (candidate.status === 'in_progress') chips.appendChild(chip('In progress', 'info'));
    card.appendChild(chips);
    if (candidate.prerequisites.length) {
      card.appendChild(node('p', 'req-note', 'Prerequisites: ' + candidate.prerequisites.map(function (p) {
        return p.code + (p.status === 'met' ? ' ✓' : p.status === 'in_progress' ? ' (in progress)' : ' ✗ missing');
      }).join(', ')));
    }
    if (candidate.semester) card.appendChild(node('p', 'req-note', 'Recommended semester ' + candidate.semester));
    return card;
  }

  function renderRequirements() {
    var list = el('gapRequirements'); list.replaceChildren();
    var requirements = data.requirements.filter(function (r) {
      return r.status === filter.status && (!filter.group || r.group === filter.group);
    }).sort(function (a, b) { return (a.semester || 99) - (b.semester || 99) || a.label.localeCompare(b.label); });
    var groupLabel = filter.group || 'all categories';
    var ready = new Set();
    requirements.forEach(function (r) { r.candidates.forEach(function (c) { if (data.candidates[c].available_next_term) ready.add(c); }); });
    var credits = requirements.reduce(function (sum, r) { return sum + Number(r.credits); }, 0);
    el('gapFilterNote').textContent = requirements.length + ' ' + statusLabel(filter.status).toLowerCase() + ' requirement'
      + (requirements.length === 1 ? '' : 's') + ' in ' + groupLabel + ' · ' + fmt(credits) + ' ECTS'
      + (filter.status === 'missing' ? ' · ' + ready.size + ' satisfying course' + (ready.size === 1 ? '' : 's') + ' available in semester ' + data.term.upcoming_semester : '');
    if (!requirements.length) {
      var empty = node('div', 'gap-empty');
      empty.append(node('b', '', filter.status === 'missing' ? 'Nothing missing here 🎉' : 'Nothing to show'),
        document.createTextNode('No ' + statusLabel(filter.status).toLowerCase() + ' requirements in ' + groupLabel + '.'));
      list.appendChild(empty); return;
    }
    requirements.forEach(function (requirement) {
      var card = node('article', 'req-card');
      var head = node('div', 'req-card-head');
      var tone = requirement.status === 'completed' ? 'ok' : requirement.status === 'in_progress' ? 'info' : 'bad';
      head.append(node('h3', '', requirement.label), chip(statusLabel(requirement.status), tone),
        node('span', 'req-meta', requirement.credits + ' ECTS · ' + requirement.group + (requirement.semester ? ' · semester ' + requirement.semester : '')
          + (requirement.kind === 'slot' ? ' · elective slot' : '')));
      card.appendChild(head);
      if (requirement.status !== 'missing') {
        var detail = requirement.satisfied_by && requirement.satisfied_by !== requirement.code ? 'Satisfied by ' + requirement.satisfied_by
          : requirement.kind === 'slot' && requirement.satisfied_by ? 'Filled by ' + requirement.satisfied_by : '';
        if (requirement.grade) detail += (detail ? ' · ' : '') + 'grade ' + requirement.grade;
        if (detail) card.appendChild(node('p', 'req-note', detail));
      } else {
        var options = node('div', 'req-options');
        requirement.candidates.slice().sort(function (a, b) { return rank(data.candidates[a]) - rank(data.candidates[b]) || a.localeCompare(b); })
          .forEach(function (code) { options.appendChild(optionCard(code)); });
        if (!requirement.candidates.length) options.appendChild(node('div', 'fix-none', 'No catalog course is listed for this requirement.'));
        card.appendChild(options);
      }
      list.appendChild(card);
    });
  }

  // ------------------------------------------------------------------- chart
  function destroyChart() { if (chart) { chart.destroy(); chart = null; } }

  function drawChart(canvas) {
    destroyChart();
    if (!window.Chart) { canvas.replaceWith(table()); return; }
    var surface = cssVar('--board-bg-2') || '#fff', ink = cssVar('--board-text'), soft = cssVar('--board-text-soft'), line = cssVar('--board-line');
    var groups = data.groups;
    chart = new window.Chart(canvas, {
      type: 'bar',
      data: {
        labels: groups.map(function (g) { return g.label; }),
        datasets: STATUSES.map(function (s) {
          return {key: s.key, label: s.label, data: groups.map(function (g) { return g[s.key]; }),
            backgroundColor: function (context) {
              var base = cssVar(s.color);
              var group = groups[context.dataIndex];
              var dim = filter.group && group && filter.group !== group.key || (filter.group && filter.status !== s.key);
              return dim ? base + '55' : base;
            },
            borderColor: surface, borderWidth: 2, borderSkipped: 'start', barThickness: 22,
            borderRadius: function (context) {
              var index = context.dataIndex, later = STATUSES.slice(context.datasetIndex + 1);
              return later.some(function (next) { return groups[index] && groups[index][next.key] > 0; }) ? 0 : 4;
            }};
        })
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false, animation: {duration: 350},
        layout: {padding: {right: 8}},
        plugins: {
          legend: {display: false},
          tooltip: {
            backgroundColor: cssVar('--surface') || '#fff', titleColor: ink, bodyColor: ink, footerColor: soft,
            borderColor: line, borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true,
            callbacks: {
              label: function (context) {
                var group = groups[context.dataIndex], key = STATUSES[context.datasetIndex].key;
                return ' ' + context.dataset.label + ': ' + fmt(context.raw) + ' ECTS (' + group.counts[key] + ' requirement' + (group.counts[key] === 1 ? '' : 's') + ')';
              },
              footer: function () { return 'Click to list these requirements'; }
            }
          }
        },
        scales: {
          x: {stacked: true, beginAtZero: true, grid: {color: line}, border: {display: false},
            ticks: {color: soft, font: {family: 'IBM Plex Mono', size: 11}},
            title: {display: true, text: 'ECTS credits', color: soft, font: {size: 11}}},
          y: {stacked: true, grid: {display: false}, border: {color: line},
            ticks: {color: ink, font: {family: 'Inter', size: 12, weight: '600'},
              callback: function (value) {
                var label = this.getLabelForValue(value);
                return this.chart.width < 420 && label.length > 16 ? label.slice(0, 15) + '…' : label;
              }}}
        },
        onHover: function (event, elements) { event.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
        onClick: function (event, elements) {
          if (elements.length) {
            var hit = elements[0];
            setFilter(groups[hit.index].key, STATUSES[hit.datasetIndex].key);
          } else {
            var row = chart.getElementsAtEventForMode(event, 'y', {intersect: false}, false)[0];
            if (row) setFilter(groups[row.index].key, 'missing');
          }
          var target = el('gapFilters'); if (target) target.scrollIntoView({behavior: 'smooth', block: 'nearest'});
        }
      }
    });
  }

  new MutationObserver(function () {
    if (data && el('paneGaps').classList.contains('active')) renderBody();
  }).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});

  el('gapSearch').addEventListener('input', renderRoster);
  el('tabGapsBtn').addEventListener('click', function () {
    loadRoster();
    if (data) setTimeout(renderBody, 0); // redraw once the pane is visible so the chart gets its real size
  });

  function start() {
    version++; roster = null; selected = null; data = null; showTable = false; destroyChart();
    el('gapStudents').replaceChildren(); el('gapBody').replaceChildren(); status('');
    el('gapStudentName').textContent = '—'; el('gapStudentMeta').textContent = '';
  }
  window.addEventListener('registration:role', function (event) {
    if (event.detail.role === 'advisor' && !roster) loadRoster();
  });
  window.addEventListener('auth:ready', start);
  if (window.scheduleCurrentUser) start();
})();
