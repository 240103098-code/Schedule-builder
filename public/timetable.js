(function(global){
  'use strict';
  var DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  function weekLabel(pattern){
    return pattern === 'odd_weeks' ? 'Нечётные недели' : pattern === 'even_weeks' ? 'Чётные недели' : 'Каждую неделю';
  }
  function rowsForEvents(events){
    var rows = new Map();
    events.forEach(function(event){
      var key = event.start + '–' + event.end;
      if(!rows.has(key)) rows.set(key, {start:event.start, end:event.end, events:[]});
      rows.get(key).events.push(event);
    });
    return Array.from(rows.values()).sort(function(a,b){ return a.start.localeCompare(b.start) || a.end.localeCompare(b.end); });
  }
  function render(events, caption){
    var wrap = document.createElement('div'); wrap.className = 'board-scroll lesson-table-wrap';
    if(!events.length){ var empty = document.createElement('p'); empty.textContent = 'Нет выбранных занятий'; wrap.appendChild(empty); return wrap; }
    var table = document.createElement('table'); table.className = 'lesson-table';
    var title = document.createElement('caption'); title.textContent = caption; table.appendChild(title);
    var days = DAYS.filter(function(day,index){ return index < 5 || events.some(function(event){ return event.day === day; }); });
    var head = document.createElement('thead'), header = document.createElement('tr');
    ['Время'].concat(days).forEach(function(label){ var cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.appendChild(cell); });
    head.appendChild(header); table.appendChild(head);
    var body = document.createElement('tbody');
    rowsForEvents(events).forEach(function(row){
      var tr = document.createElement('tr'), time = document.createElement('th'); time.scope = 'row'; time.textContent = row.start + '–' + row.end; tr.appendChild(time);
      days.forEach(function(day){
        var td = document.createElement('td'), content = document.createElement('div'); content.className = 'lesson-cell-content';
        row.events.filter(function(event){ return event.day === day; }).forEach(function(event){
          var card = document.createElement('div'); card.className = 'lesson-card' + (event.type === 'practice' ? ' is-practice' : ''); card.style.background = event.color || '#f6c344';
          [[event.type === 'practice' ? 'Практика' : 'Лекция','lesson-type'],[event.name,'lesson-name'],[event.section,'lesson-meta'],[weekLabel(event.week_pattern),'lesson-meta']].forEach(function(part){ var span = document.createElement('span'); span.className = part[1]; span.textContent = part[0]; card.appendChild(span); });
          if(event.quota !== undefined){ var seats = document.createElement('span'); seats.className = 'lesson-meta'; seats.textContent = event.available + '/' + event.quota + ' мест'; card.appendChild(seats); }
          content.appendChild(card);
        });
        td.appendChild(content); tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body); wrap.appendChild(table); return wrap;
  }
  global.ScheduleTimetable = {render:render, rowsForEvents:rowsForEvents, weekLabel:weekLabel};
})(typeof window === 'undefined' ? globalThis : window);
