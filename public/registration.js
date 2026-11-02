(function () {
  'use strict';
  var busy = false, timer = null, current = null, comments = new Map(), requestCourse = '', catalogRevision = '',
    refreshVersion = 0, sectionChoices = new Map(), inboxRevision = '', telegramWasAvailable = null, emailWasAvailable = null;
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
    return item;
  }
  function seatPicker(block, request, data) {
    var saved = sectionChoices.get(request.id) || {};
    var lecture = document.createElement('select'), practice = document.createElement('select');
    lecture.className = practice.className = 'waitlist-select';
    lecture.setAttribute('aria-label', 'Lecture for ' + request.course);
    practice.setAttribute('aria-label', 'Practice for ' + request.course);
    function label(copy, field) {
      var node = document.createElement('label'); node.className = 'section-label';
      node.textContent = copy; var id = 'grant-' + request.id + '-' + copy.toLowerCase();
      field.id = id; node.htmlFor = id; block.append(node, field);
    }
    function fill(select, sections, previous) {
      select.replaceChildren();
      sections.forEach(function (section) {
        var option = document.createElement('option'); option.value = section.section;
        option.textContent = section.section + ' · ' + section.meetings.map(function (meeting) {
          return ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][meeting.day_of_week - 1] + ' ' + meeting.start_time + '–' + meeting.end_time;
        }).join(', '); select.appendChild(option);
      });
      if (sections.some(function (s) { return s.section === previous; })) select.value = previous;
    }
    function practices() {
      fill(practice, data.sections.filter(function (s) { return s.parent === lecture.value; }),
        saved.practice || request.queued_practice);
      sectionChoices.set(request.id, {lecture:lecture.value, practice:practice.value});
    }
    fill(lecture, data.sections.filter(function (s) { return s.course === request.course && !s.parent; }),
      saved.lecture || request.queued_lecture);
    practices(); label('LECTURE', lecture); label('PRACTICE', practice);
    lecture.addEventListener('change', function () { saved = {}; practices(); });
    practice.addEventListener('change', function () { sectionChoices.set(request.id, {lecture:lecture.value, practice:practice.value}); });
    if (!lecture.options.length || !practice.options.length) {
      var note = document.createElement('p'); note.textContent = 'No scheduled lecture/practice pair is available. You can reject the request or review it after sections are published.'; block.appendChild(note);
    }
    return function () { return {lecture:lecture.value, practice:practice.value}; };
  }
  function selection(force) {
    if (!current) return;
    var focused = document.activeElement;
    if (!force && (focused === el('alertCourse') || focused === el('alertSection'))) { updateRequestButton(); return; }
    var previousCourse = el('alertCourse').value, selected = el('alertSection').value;
    var courses = current.courses.filter(function (course) { return current.sections.some(function (s) { return s.course === course.code; }); });
    el('alertCourse').replaceChildren();
    courses.forEach(function (course) {
      var option = document.createElement('option'); option.value = course.code;
      option.textContent = course.code + ' · ' + course.name; el('alertCourse').appendChild(option);
    });
    if (courses.some(function (course) { return course.code === previousCourse; })) el('alertCourse').value = previousCourse;
    var sections = current.sections.filter(function (s) { return s.course === el('alertCourse').value; });
    el('alertSection').replaceChildren();
    sections.forEach(function (section) {
      var option = document.createElement('option'); option.value = section.section;
      option.textContent = (section.parent ? 'Practice · ' : 'Lecture · ') + section.section + ' · ' + section.available + '/' + section.quota + ' seats';
      el('alertSection').appendChild(option);
    });
    if (sections.some(function (section) { return section.section === selected; })) el('alertSection').value = selected;
    updateRequestButton();
  }
  function renderInbox(data) {
    el('readCourseNoticesBtn').disabled = !data || !data.unread;
    var revision = JSON.stringify(data);
    if (revision === inboxRevision) return;
    inboxRevision = revision;
    var notices = el('seatNotifications'); notices.replaceChildren();
    if (!data || !data.items.length) empty(notices, 'No seat alerts yet', 'Notifications appear here automatically, on any tab and after you return to the site.');
    else data.items.forEach(function (notice) {
      var block = card(notices, notice.course + (notice.section ? ' · ' + notice.section : ''),
        notice.message + '\n' + new Date(notice.created_at * 1000).toLocaleString('ru-RU'), notice.read ? 'Read' : 'New');
      if (!notice.read) block.classList.add('is-unread');
    });
    el('readCourseNoticesBtn').disabled = !data || !data.unread;
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
    var isAdvisor = data.role === 'advisor';
    el('accountAccessRole').textContent = (isAdvisor ? 'Advisor' : 'Student') + (data.department ? ' · ' + data.department : '');
    el('registrationTitle').textContent = isAdvisor ? 'Advisor workspace' : 'Requests & Alerts';
    el('registrationIntro').textContent = isAdvisor
      ? 'Review requests from students in your department. Allocate one personal extra seat in a lecture and practice, or reject the request with a comment.'
      : 'Choose any course and explain why you need an extra seat. After approval, use the reserved seat from your request card.';
    ['Schedule','Degree','Waitlist'].forEach(function (name) { el('tab' + name + 'Btn').hidden = isAdvisor; });
    el('tabRegistrationBtn').textContent = isAdvisor ? 'Advisor workspace' : '04 · Requests & Alerts';
    if (isAdvisor && !el('paneRegistration').classList.contains('active')) {
      ['Schedule','Degree','Waitlist'].forEach(function (name) {
        el('pane' + name).classList.remove('active'); el('tab' + name + 'Btn').classList.remove('active');
      });
      el('paneRegistration').classList.add('active'); el('tabRegistrationBtn').classList.add('active');
    }
    el('overrideForm').hidden = data.role === 'advisor';
    el('seatAlertForm').hidden = data.role === 'advisor';
    el('studentSeatPanel').hidden = data.role === 'advisor';
    el('gatewayStatus').hidden = data.role === 'advisor';
    el('overrideHeading').textContent = data.role === 'advisor' ? 'Department review dashboard' : 'Your course requests';
    el('gatewayStatus').textContent = 'Уведомления внутри сайта работают всегда. ' +
      (data.telegram_linked ? 'Telegram подключён: сохраните подписку, чтобы получать уведомления.' :
        data.telegram_configured ? 'Подключите Telegram, чтобы получать сообщения на телефоне.' :
        'Для Telegram запустите start-with-telegram.ps1 и введите токен бота.');
    el('gatewayStatus').textContent += data.email_configured ? ' Почтовая отправка подключена.' : ' Email ожидает настройки SMTP.';
    el('alertTelegramChannel').disabled = !data.telegram_linked || !data.telegram_configured;
    var telegramAvailable = !el('alertTelegramChannel').disabled;
    if (!telegramAvailable) el('alertTelegramChannel').checked = false;
    else if (telegramWasAvailable !== true) el('alertTelegramChannel').checked = true;
    telegramWasAvailable = telegramAvailable;
    var emailReady = data.email_configured && data.email_verified;
    el('alertEmailChannel').disabled = !emailReady;
    if (!emailReady) el('alertEmailChannel').checked = false;
    else if (emailWasAvailable !== true) el('alertEmailChannel').checked = true;
    emailWasAvailable = emailReady;
    var address = window.scheduleCurrentUser && window.scheduleCurrentUser.email || '';
    el('emailLinkStatus').textContent = data.email_verified ?
      (data.email_configured ? 'Email подтверждён: ' + address + '. Сохраните подписку для получения писем.' : 'Для писем настройте Gmail при запуске сервера.') :
      (data.email_sender_configured ? 'Подтвердите адрес аккаунта: ' + address + '.' : 'Для писем настройте Gmail при запуске сервера, затем подтвердите адрес аккаунта.');
    el('verifyAccountEmailBtn').hidden = !!data.email_verified;
    el('verifyAccountEmailBtn').disabled = !data.email_sender_configured;
    el('telegramLinkBtn').disabled = busy || !data.telegram_configured;
    el('telegramUnlinkBtn').hidden = !data.telegram_linked;
    el('telegramConfirmBtn').hidden = data.telegram_linked || !el('telegramBotLink').getAttribute('href');
    el('telegramBotLink').hidden = data.telegram_linked || !el('telegramBotLink').getAttribute('href');
    el('telegramManual').hidden = data.telegram_linked || !el('telegramCommand').value;
    el('telegramQrPanel').hidden = data.telegram_linked || !el('telegramQr').getAttribute('src');
    if (data.telegram_linked) { stopTelegramPolling(); el('telegramCommand').value = ''; }
    el('telegramLinkStatus').textContent = data.telegram_linked ? 'Telegram подключён.' :
      'Откройте бота по ссылке или QR-коду и нажмите Start. Подключение произойдёт автоматически.';
    var requests = el('overrideRequests'); requests.replaceChildren();
    data.requests.forEach(function (request) {
      var block = card(requests, request.course + (request.course_name ? ' · ' + request.course_name : ''),
        request.student + ' · ' + request.department + ' · ' + request.flags.map(function (flag) {
          return flag === 'additional_seat' ? 'Additional seat request' : flag;
        }).join(', ') + ' · ' + request.credit_total + ' credits' +
        (request.lecture ? '\n' + request.lecture + ' / ' + request.practice : '') +
        '\n' + request.reason + (request.comment ? '\nAdvisor: ' + request.comment : '') +
        (request.seat_grant ? '\n' + (request.seat_grant === 'used' ? 'Extra seat used · Enrollment: ' + request.enrollment_status :
          '1 extra seat reserved for this student') + (request.enrollment_reason ? '\n' + request.enrollment_reason : '') : ''), request.status);
      if (data.role === 'advisor' && !request.own_request && (request.status === 'pending' ||
          (request.status === 'approved' && !request.seat_grant && request.flags.indexOf('additional_seat') >= 0))) {
        var extraSeat = request.flags.indexOf('additional_seat') >= 0;
        var selectedPair = extraSeat ? seatPicker(block, request, data) : null;
        var comment = document.createElement('textarea'); comment.className = 'waitlist-select';
        comment.maxLength = 1000; comment.placeholder = 'Decision comment'; comment.setAttribute('aria-label', 'Decision comment for ' + request.course);
        comment.value = comments.get(request.id) || '';
        comment.addEventListener('input', function () { comments.set(request.id, comment.value); });
        block.appendChild(comment);
        (request.status === 'pending' ? ['approved', 'rejected'] : ['approved']).forEach(function (decision) {
          var action = button(block, decision === 'approved' ? (extraSeat ? 'Approve & grant 1 seat' : 'Approve') : 'Reject', function () {
            mutate('/api/registration/overrides/' + request.id + '/decision', 'POST', Object.assign(
              {decision: decision, comment: comment.value}, decision === 'approved' && selectedPair ? selectedPair() : {}));
          }, decision === 'approved' ? 'primary' : 'reject');
          if (decision === 'approved' && selectedPair && (!selectedPair().lecture || !selectedPair().practice)) action.disabled = true;
        });
      } else if (data.role === 'advisor' && request.status === 'pending' && request.own_request) {
        var selfNote = document.createElement('p'); selfNote.textContent = 'Your own request must be reviewed by another advisor.'; block.appendChild(selfNote);
      } else if (data.role === 'student' && request.seat_grant === 'reserved') {
        button(block, request.enrollment_status === 'blocked' ? 'Retry enrollment with reserved seat' : 'Enroll using reserved seat', function () {
          mutate('/api/registration/seat-requests/' + request.id + '/enroll', 'POST', {});
        }, 'primary');
      } else if (data.role === 'student' && request.status === 'pending' &&
          request.flags.indexOf('additional_seat') >= 0) {
        button(block, 'Cancel request', function () {
          mutate('/api/registration/seat-requests/' + request.id, 'DELETE');
        }, 'reject');
      }
    });
    if (!data.requests.length) empty(requests, 'No requests yet', 'Submitted requests appear here automatically.');
    var subscriptions = el('seatSubscriptions'); subscriptions.replaceChildren();
    data.subscriptions.forEach(function (sub) {
      var block = card(subscriptions, sub.section, sub.channels.join(' · '));
      button(block, 'Unsubscribe', function () { mutate('/api/registration/subscriptions/' + sub.id, 'DELETE'); });
      if (data.academic_mode) {
        var section = data.sections.find(function (s) { return s.section === sub.section; });
        var opened = section && section.available > 0;
        var demoButton = button(block, opened ? 'Место уже свободно' : 'Учебный пример: освободить место', function () {
          mutate('/api/registration/subscriptions/' + sub.id + '/demo-seat', 'POST', {});
        });
        demoButton.disabled = busy || opened;
      }
    });
    if (!data.subscriptions.length) empty(subscriptions, 'No subscriptions yet', 'Subscribe to a full section to receive seat alerts.');
    renderInbox(window.courseInbox);
    var deliveries = el('seatDeliveryLog'); deliveries.replaceChildren();
    var deliveryLabels = {pending:'Ожидает отправки', sending:'Отправляется', 
      failed:'Не доставлено', cancelled:'Отменено'};
    data.notifications.forEach(function (notice) {
      var label = notice.status === 'delivered' ? (notice.provider === 'telegram' ? 'Отправлено в Telegram' : notice.provider === 'smtp' ? 'Принято почтовым сервером' : 'Передано шлюзу') :
        deliveryLabels[notice.status] || notice.status;
      card(deliveries, notice.channel.toUpperCase() + ' · ' + label,
        notice.message + (notice.last_error ? '\nОшибка отправки: ' + notice.last_error : ''));
    });
    if (!data.notifications.length) empty(deliveries, 'Нет внешних отправок', 'Внутренние уведомления отображаются выше. Telegram доступен после привязки аккаунта.');
  }
  async function refresh() {
    if (busy || !window.scheduleCurrentUser) return;
    var version = ++refreshVersion;
    try {
      var data = await api('/api/registration');
      if (version !== refreshVersion) return;
      // Preserve an advisor's unfinished comment during polling.
      if (!el('overrideRequests').contains(document.activeElement)) render(data);
    } catch (error) {
      if (version === refreshVersion) status(error.message, true);
    }
  }
  async function mutate(url, method, payload) {
    if (busy) return;
    refreshVersion++;
    busy = true;
    el('paneRegistration').querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    var isOverride = url === '/api/registration/seat-requests';
    var isCancellation = method === 'DELETE' && url.indexOf('/api/registration/seat-requests/') === 0;
    if (isOverride) requestStatus('Submitting request…');
    if (isCancellation) requestStatus('Cancelling request…');
    try {
      current = await api(url, method, payload); status('Saved.');
      if (isOverride) { el('overrideReason').value = ''; requestStatus('Request submitted. Awaiting advisor review.'); }
      if (isCancellation) requestStatus('Request cancelled. You can submit another request for this course.');
    }
    catch (error) {
      status(error.message, true);
      if (isOverride || isCancellation) requestStatus(error.message, true);
    }
    finally {
      busy = false;
      if (current) render(current);
      el('overrideForm').querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      el('seatAlertForm').querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      el('enableCourseBrowserNoticesBtn').disabled = false;
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
  el('alertCourse').addEventListener('change', function () { selection(true); });
  window.addEventListener('course:notifications', function (event) { renderInbox(event.detail); });
  var telegramPollTimer = null, telegramPollGeneration = 0;
  function stopTelegramPolling() {
    telegramPollGeneration++;
    if (telegramPollTimer) clearTimeout(telegramPollTimer);
    telegramPollTimer = null;
  }
  function startTelegramPolling() {
    stopTelegramPolling();
    var generation = telegramPollGeneration, deadline = Date.now() + 10 * 60 * 1000;
    async function check() {
      if (generation !== telegramPollGeneration) return;
      if (Date.now() >= deadline) {
        status('Ссылка истекла. Нажмите «Подключить Telegram» ещё раз.', true);
        return;
      }
      try {
        var response = await fetch('/api/registration/telegram/confirm', {
          method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:'{}'
        });
        if (generation !== telegramPollGeneration) return;
        if (response.ok) {
          current = await response.json();
          stopTelegramPolling();
          render(current);
          status('Telegram подключён. Выберите его и сохраните подписку.');
          return;
        }
        if (response.status !== 409) {
          var error = await response.json().catch(function () { return {}; });
          status(error.detail || 'Не удалось проверить Telegram. Повторяем попытку.', true);
        }
      } catch (error) {
        if (generation !== telegramPollGeneration) return;
        status('Не удалось проверить Telegram. Повторяем попытку.', true);
      }
      if (generation === telegramPollGeneration) telegramPollTimer = setTimeout(check, 3000);
    }
    check();
  }
  el('verifyAccountEmailBtn').addEventListener('click', async function () {
    var button = this;
    if (!window.scheduleCurrentUser || !window.scheduleCurrentUser.email) return;
    button.disabled = true;
    try {
      var result = await api('/api/auth/resend-verification', 'POST', {email:window.scheduleCurrentUser.email});
      status(result.message + ' Откройте письмо на этом компьютере и подтвердите адрес.');
    } catch (error) { status(error.message, true); }
    finally { button.disabled = false; }
  });
  el('telegramLinkBtn').addEventListener('click', async function () {
    this.disabled = true;
    try {
      var result = await api('/api/registration/telegram/link', 'POST', {});
      el('telegramBotLink').href = result.url;
      el('telegramQr').src = result.qr_svg;
      el('telegramQrPanel').hidden = false;
      el('telegramCommand').value = '/start ' + new URL(result.url).searchParams.get('start');
      el('telegramManual').hidden = false;
      el('telegramBotLink').hidden = false;
      el('telegramConfirmBtn').hidden = false;
      status('Откройте бота по ссылке или QR-коду и нажмите Start. Сайт подключится автоматически.');
      startTelegramPolling();
    } catch (error) { status(error.message, true); }
    finally { this.disabled = false; }
  });
  el('telegramCopyCommand').addEventListener('click', async function () {
    var command = el('telegramCommand');
    try {
      await navigator.clipboard.writeText(command.value);
      status('Команда скопирована. Отправьте её боту в личном чате Telegram.');
    } catch (error) {
      command.focus(); command.select();
      status('Выделите команду и скопируйте её вручную.');
    }
  });
  el('telegramConfirmBtn').addEventListener('click', function () {
    mutate('/api/registration/telegram/confirm', 'POST', {});
  });
  el('telegramUnlinkBtn').addEventListener('click', function () {
    stopTelegramPolling();
    el('telegramQr').removeAttribute('src');
    el('telegramQrPanel').hidden = true;
    el('telegramBotLink').removeAttribute('href');
    el('telegramCommand').value = '';
    el('telegramManual').hidden = true;
    mutate('/api/registration/telegram/link', 'DELETE');
  });
  el('seatAlertForm').addEventListener('submit' , function (event) {
    event.preventDefault();
    var channels = Array.from(document.querySelectorAll('input[name="alertChannel"]:checked')).map(function (c) { return c.value; });
    if (!channels.length) { status('Select at least one notification channel.', true); return; }
    mutate('/api/registration/subscriptions', 'PUT', {section: el('alertSection').value, channels: channels});
  });
  el('tabRegistrationBtn').addEventListener('click', function () { selection(); refresh(); });
  function start() {
    refreshVersion++; telegramWasAvailable = null; emailWasAvailable = null; current = null; comments.clear(); sectionChoices.clear(); requestCourse = ''; catalogRevision = ''; inboxRevision = ''; requestStatus(''); refresh(); if (timer) clearInterval(timer);
    timer = setInterval(function () { if (!document.hidden && el('paneRegistration').classList.contains('active')) refresh(); }, 2000);
  }
  window.addEventListener('auth:ready', start);
  if (window.scheduleCurrentUser) start();
})();
