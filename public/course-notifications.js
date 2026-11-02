(function () {
  'use strict';
  var timer, userId = null, epoch = 0, busy = false, cursor = 0, toastTimer;
  var el = function (id) { return document.getElementById(id); };
  function read(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (_) { return fallback; } }
  function save(key, value) { try { localStorage.setItem(key, String(value)); } catch (_) {} }
  function openInbox() {
    el('tabRegistrationBtn').click();
    el('seatNotifications').scrollIntoView({behavior:'smooth', block:'center'});
  }
  function consume(data) {
    window.courseInbox = data;
    var button = el('courseNotificationsBtn'); button.hidden = data.role !== 'student';
    button.textContent = 'Уведомления · ' + data.unread;
    button.setAttribute('aria-label', 'Уведомления: непрочитанных ' + data.unread);
    window.dispatchEvent(new CustomEvent('course:notifications', {detail:data}));
    if (data.role !== 'student') return;
    var fresh = data.items.filter(function (item) { return item.id > cursor && !item.read; });
    var newest = fresh[0];
    if (newest) {
      el('courseNoticeToastTitle').textContent = newest.course;
      el('courseNoticeToastText').textContent = newest.message;
      el('courseNoticeToast').hidden = false;
      clearTimeout(toastTimer); toastTimer = setTimeout(function () { el('courseNoticeToast').hidden = true; }, 12000);
      if (document.hidden && 'Notification' in window && Notification.permission === 'granted' &&
          read('course-browser:' + userId, '') === 'enabled') {
        try {
          var notification = new Notification(newest.course, {body:newest.message, tag:'course-notice-' + newest.id});
          notification.onclick = function () { window.focus(); openInbox(); notification.close(); };
        } catch (_) { /* Persistent inbox and toast remain available. */ }
      }
    }
    if (data.items.length) { cursor = Math.max(cursor, data.items[0].id); save('course-notice:' + userId, cursor); }
  }
  async function poll() {
    if (!window.scheduleCurrentUser || busy) return;
    var currentEpoch = epoch; busy = true;
    try {
      var response = await fetch('/api/registration/notifications', {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) return;
      var data = await response.json();
      if (currentEpoch === epoch) consume(data);
    } catch (_) { /* Retry next cycle; notices are persisted by the server. */ }
    finally { if (currentEpoch === epoch) busy = false; }
  }
  function start() {
    epoch++; busy = false; clearInterval(timer); clearTimeout(toastTimer);
    userId = window.scheduleCurrentUser.id;
    window.courseInbox = null;
    cursor = Number(read('course-notice:' + userId, '0')) || 0;
    el('courseNoticeToast').hidden = true;
    poll(); timer = setInterval(poll, 2000);
  }
  el('courseNotificationsBtn').addEventListener('click', openInbox);
  el('openCourseNoticeBtn').addEventListener('click', function () { el('courseNoticeToast').hidden = true; openInbox(); });
  el('closeCourseNoticeBtn').addEventListener('click', function () { el('courseNoticeToast').hidden = true; });
  el('readCourseNoticesBtn').addEventListener('click', async function () {
    var data = window.courseInbox;
    if (!data || !data.items.length) return;
    this.disabled = true;
    try {
      var response = await fetch('/api/registration/notifications/read', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({through_id:data.items[0].id})});
      if (response.ok) consume(await response.json());
    } catch (_) { el('courseBrowserNoticeStatus').textContent = 'Не удалось отметить уведомления. Попробуйте ещё раз.'; }
    finally { this.disabled = !window.courseInbox || !window.courseInbox.unread; }
  });
  el('enableCourseBrowserNoticesBtn').addEventListener('click', async function () {
    var note = el('courseBrowserNoticeStatus');
    if (!('Notification' in window)) { note.textContent = 'Этот браузер не поддерживает системные уведомления. Уведомления внутри сайта работают.'; return; }
    try {
      var permission = await Notification.requestPermission();
      if (permission === 'granted') { save('course-browser:' + userId, 'enabled'); note.textContent = 'Всплывающие уведомления включены, пока сайт открыт в браузере.'; }
      else note.textContent = 'Разрешение не получено. Уведомления внутри сайта продолжают работать.';
    } catch (_) { note.textContent = 'Браузер не разрешил системные уведомления. Используйте уведомления внутри сайта.'; }
  });
  window.addEventListener('auth:ready', start);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  if (window.scheduleCurrentUser) start();
})();
