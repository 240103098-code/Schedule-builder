(function () {
  'use strict';
  var busy = false, timer = null, current = null, comments = new Map(), requestCourse = '', catalogRevision = '';
  var el = function (id) { return document.getElementById(id); };
  function status(message, error) {
    el('registrationStatus').textContent = message;
    el('registrationStatus').className = 'status-line' + (error ? ' error' : '');
  }
  function requestStatus(message, error) {
    el('overrideFormStatus').textContent = message;
    el('overrideFormStatus').className = 'status-line' + (error ? ' error' : '');
  }
  async function api(url, method, payload) {
    var response = await fetch(url, {method: method || 'GET', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: payload ? JSON.stringify(payload) : undefined});
    var body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Check the form fields.');
    return body;
  }
  function card(container, title, copy, state) {
    var block = document.createElement('div'); block.className = 'waitlist-row registration-card';
    var header = document.createElement('div'); header.className = 'registration-card-heading';
    var heading = document.createElement('strong'); heading.textContent = title;
    header.appendChild(heading);
    if (state) {
      var badge = document.createElement('span'); badge.className = 'registration-badge';
      badge.dataset.status = state; badge.textContent = state; header.appendChild(badge);
    }
    var text = document.createElement('p'); text.textContent = copy;
    block.append(header, text); container.appendChild(block); return block;
  }
  function empty(container, title, copy) { card(container, title, copy).classList.add('is-empty'); }
  function button(container, label, handler, variant) {
    var actions = container.querySelector('.registration-actions');
    if (!actions) { actions = document.createElement('div'); actions.className = 'registration-actions'; container.appendChild(actions); }
    var item = document.createElement('button'); item.type = 'button'; item.className = 'registration-action' + (variant ? ' is-' + variant : '');
    item.textContent = label; item.disabled = busy; item.addEventListener('click', handler);
    actions.appendChild(item);
  }
  function selection() {
    var lecture = el('waitlistLecture'), practice = el('waitlistPractice');
    var selected = el('alertSection').value;
    el('alertSection').replaceChildren();
    [lecture, practice].forEach(function (source) {
      if (!source.value) return;
      var option = document.createElement('option'); option.value = source.value;
      option.textContent = source.selectedOptions[0].textContent; el('alertSection').appendChild(option);
    });
    if ([lecture.value, practice.value].indexOf(selected) >= 0) el('alertSection').value = selected;
    updateRequestButton();
  }
  function courseOptions() {
    var select = el('requestCourse'), previous = select.value;
    var query = el('requestCourseSearch').value.trim().toLowerCase();
    var courses = (current && current.courses || []).filter(function (course) {
      return (course.code + ' ' + course.name).toLowerCase().includes(query);
    });
    select.replaceChildren();
    var placeholder = document.createElement('option'); placeholder.value = '';
    placeholder.textContent = courses.length ? 'Choose a course…' : 'No matching courses';
    select.appendChild(placeholder);
    courses.forEach(function (course) {
      var option = document.createElement('option'); option.value = course.code;
      option.textContent = course.code + ' · ' + course.name; option.title = option.textContent;
      select.appendChild(option);
    });
    if (courses.some(function (course) { return course.code === previous; })) select.value = previous;
    updateRequestButton();
  }
  function updateRequestButton() {
    if (requestCourse !== el('requestCourse').value) {
      requestCourse = el('requestCourse').value; requestStatus('');
    }
    var existing = current && current.requests.find(function (request) {
      return request.course === el('requestCourse').value;
    });
    var ready = current && current.department && el('requestCourse').value;
    el('submitOverrideBtn').disabled = busy || !ready || Boolean(existing);
    el('submitOverrideBtn').textContent = existing ? 'Request ' + existing.status : 'Request extra seat';
    if (existing) requestStatus(existing.status === 'pending' ? 'Request submitted. Awaiting advisor review.' :
      'Request ' + existing.status + '. See its details on the right.');
    else if (current && !current.department) requestStatus('Ask an administrator to assign your department before submitting a request.', true);
  }
  function render(data) {
    current = data;
    var revision = JSON.stringify(data.courses || []);
    if (revision !== catalogRevision) { catalogRevision = revision; courseOptions(); }
    selection();
    el('registrationIdentity').textContent = (data.role === 'advisor' ? 'Academic advisor' : 'Student') +
      ' · ' + (data.department || 'Department has not been assigned');
    el('overrideForm').hidden = data.role === 'advisor';
    el('seatAlertForm').hidden = data.role === 'advisor';
    el('studentSeatPanel').hidden = data.role === 'advisor';
    el('gatewayStatus').hidden = data.role === 'advisor';
    el('overrideHeading').textContent = data.role === 'advisor' ? 'Department review dashboard' : 'Your course requests';
    el('gatewayStatus').textContent = data.gateway_configured ? 'Seat alerts use the University Notification Gateway.' :
      'University Notification Gateway is not configured. Alerts stay pending until it is connected.';
    var requests = el('overrideRequests'); requests.replaceChildren();
    data.requests.forEach(function (request) {
      var block = card(requests, request.course + (request.course_name ? ' · ' + request.course_name : ''),
        request.student + ' · ' + request.department + ' · ' + request.flags.map(function (flag) {
          return flag === 'additional_seat' ? 'Additional seat request' : flag;
        }).join(', ') + ' · ' + request.credit_total + ' credits' +
        (request.lecture ? '\n' + request.lecture + ' / ' + request.practice : '') +
        '\n' + request.reason + (request.comment ? '\nAdvisor: ' + request.comment : ''), request.status);
      if (data.role === 'advisor' && request.status === 'pending') {
        var comment = document.createElement('textarea'); comment.className = 'waitlist-select';
        comment.maxLength = 1000; comment.placeholder = 'Decision comment'; comment.setAttribute('aria-label', 'Decision comment for ' + request.course);
        comment.value = comments.get(request.id) || '';
        comment.addEventListener('input', function () { comments.set(request.id, comment.value); });
        block.appendChild(comment);
        ['approved', 'rejected'].forEach(function (decision) {
          button(block, decision === 'approved' ? 'Approve' : 'Reject', function () {
            mutate('/api/registration/overrides/' + request.id + '/decision', 'POST', {decision: decision, comment: comment.value});
          }, decision === 'approved' ? 'primary' : 'reject');
        });
      }
    });
    if (!data.requests.length) empty(requests, 'No requests yet', 'Submitted requests appear here automatically.');
    var subscriptions = el('seatSubscriptions'); subscriptions.replaceChildren();
    data.subscriptions.forEach(function (sub) {
      var block = card(subscriptions, sub.section, sub.channels.join(' · '));
      button(block, 'Unsubscribe', function () { mutate('/api/registration/subscriptions/' + sub.id, 'DELETE'); });
    });
    if (!data.subscriptions.length) empty(subscriptions, 'No subscriptions yet', 'Subscribe to a full section to receive seat alerts.');
    var notices = el('seatNotifications'); notices.replaceChildren();
    data.notifications.forEach(function (notice) { card(notices, notice.channel.toUpperCase(), notice.message, notice.status); });
    if (!data.notifications.length) empty(notices, 'No seat alerts yet', 'Your notifications will appear here when a seat opens in a subscribed section.');
  }
  async function refresh() {
    if (busy || !window.scheduleCurrentUser) return;
    try {
      var data = await api('/api/registration');
      // Preserve an advisor's unfinished comment during polling.
      if (!el('overrideRequests').contains(document.activeElement)) render(data);
    } catch (error) { status(error.message, true); }
  }
  async function mutate(url, method, payload) {
    if (busy) return;
    busy = true;
    el('paneRegistration').querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    var isOverride = url === '/api/registration/seat-requests';
    if (isOverride) requestStatus('Submitting request…');
    try {
      current = await api(url, method, payload); status('Saved.');
      if (isOverride) { el('overrideReason').value = ''; requestStatus('Request submitted. Awaiting advisor review.'); }
    }
    catch (error) { status(error.message, true); if (isOverride) requestStatus(error.message, true); }
    finally {
      busy = false;
      if (current) render(current);
      el('paneRegistration').querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      updateRequestButton();
    }
  }
  el('overrideForm').addEventListener('submit', function (event) {
    event.preventDefault();
    if (el('overrideReason').value.trim().length < 5) { requestStatus('Enter a reason of at least 5 characters.', true); return; }
    if (!el('requestCourse').value) { requestStatus('Choose a course first.', true); return; }
    mutate('/api/registration/seat-requests', 'POST', {course: el('requestCourse').value, reason: el('overrideReason').value});
  });
  el('requestCourseSearch').addEventListener('input', courseOptions);
  el('requestCourse').addEventListener('change', updateRequestButton);
  el('seatAlertForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var channels = Array.from(document.querySelectorAll('input[name="alertChannel"]:checked')).map(function (c) { return c.value; });
    if (!channels.length) { status('Select at least one notification channel.', true); return; }
    mutate('/api/registration/subscriptions', 'PUT', {section: el('alertSection').value, channels: channels, phone: el('alertPhone').value});
  });
  el('tabRegistrationBtn').addEventListener('click', function () { selection(); refresh(); });
  function start() {
    current = null; comments.clear(); requestCourse = ''; catalogRevision = ''; requestStatus(''); refresh(); if (timer) clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden && el('paneRegistration').classList.contains('active')) refresh(); }, 2000);
  }
  window.addEventListener('auth:ready', start);
  if (window.scheduleCurrentUser) start();
})();
