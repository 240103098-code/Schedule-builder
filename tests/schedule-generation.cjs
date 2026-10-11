// Run with: node tests/schedule-generation.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'public/schedule-builder.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]);
scripts.forEach(script => new vm.Script(script));
const app = scripts.find(script => script.includes('var NODE_BUDGET'));
const source = app.slice(app.indexOf('  var DAYS'), app.indexOf('  var state'))
  + app.slice(app.indexOf('  function toMinutes'), app.indexOf('  function fmtTime'))
  + app.slice(app.indexOf('  function importedSlot'), app.indexOf("  semesterSelect.addEventListener"))
  + app.slice(app.indexOf('  var NODE_BUDGET'), app.indexOf('  // GENERATE BUTTON'));
const generator = {};
vm.createContext(generator);
vm.runInContext(source, generator);
const meeting = (day, start, end) => generator.mkMeeting([day], start, end);
const lecture = (label, meetings) => generator.mkSlot(label, meetings);
const course = lectures => generator.mkCourse('Test course', '#fff', lectures, []);
const compact = () => [meeting('Mon','09:00','09:50'), meeting('Mon','10:00','10:50'), meeting('Mon','11:00','11:50')];
const gappy = () => [meeting('Mon','09:00','09:50'), meeting('Mon','13:00','13:50'), meeting('Mon','17:00','17:50')];

let result = generator.generateSchedules([course([
  lecture('Long gaps', gappy()), lecture('Compact', compact())
])]);
assert.equal(result.results[0][0].lecture.label, 'Compact');
assert.equal(generator.scheduleQuality(result.results[0]).gaps, 20);

result = generator.generateSchedules([course([
  lecture('Scattered', [meeting('Mon','09:00','09:50'), meeting('Tue','09:00','09:50'), meeting('Wed','09:00','09:50')]),
  lecture('Compact', compact())
])]);
assert.equal(result.results[0][0].lecture.label, 'Compact');
assert.equal(generator.scheduleQuality(result.results[0]).sparseDays, 0);
assert.equal(result.results.length, 2, 'Less compact results remain available');

const flexible = course([lecture('Monday', compact()), lecture('Tuesday', [meeting('Tue','09:00','09:50')])]);
result = generator.generateSchedules([flexible], 'Mon');
assert.equal(result.results.length, 1);
assert.equal(result.results[0][0].lecture.label, 'Tuesday');
assert.equal(generator.generateSchedules([course([lecture('Monday', compact())])], 'Mon').results.length, 0);
assert.equal(generator.generateSchedules([]).results.length, 0);

const manyLectures = Array.from({length:599}, (_, index) => lecture('Long gaps ' + index, gappy()));
manyLectures.push(lecture('Best late result', compact()));
result = generator.generateSchedules([course(manyLectures)]);
assert.equal(result.results.length, 500);
assert.equal(result.totalFound, 600, 'Search continues beyond the display cap');
assert.equal(result.results[0][0].lecture.label, 'Best late result');
assert.equal(result.truncated, false);

const raw = JSON.parse(fs.readFileSync(path.join(root, 'data/curriculum_subjects_and_electives_with_schedule.json'), 'utf8'));
const alternating = generator.importScheduledCourse(raw.subjects.find(item => item.code === 'MDE 153'), '#fff');
assert.equal(generator.buildCourseOptions(alternating).length, 6, 'Alternating lecture/practice pairs are compatible');
assert.equal(generator.generateSchedules([alternating]).results.length, 6);
const oddMeeting = {day:'Mon', start:1050, end:1100, week_pattern:'odd_weeks'};
assert(!generator.hasInternalOverlap([oddMeeting, {...oddMeeting, week_pattern:'even_weeks'}]));
assert(generator.hasInternalOverlap([oddMeeting, {...oddMeeting, week_pattern:'every_week'}]));
const timetable = {};
vm.createContext(timetable);
vm.runInContext(fs.readFileSync(path.join(root, 'public/timetable.js'), 'utf8'), timetable);
const rows = timetable.ScheduleTimetable.rowsForEvents([
  {day:'Tue',start:'14:30',end:'15:20'}, {day:'Wed',start:'09:30',end:'10:20'},
  {day:'Mon',start:'14:30',end:'15:20'}]);
assert.equal(rows.length, 2);
assert.equal(rows[0].start, '09:30');
assert.equal(rows[1].events.length, 2, 'Equal lesson times share a table row');
class Element {
  constructor(tag){ this.tag = tag; this.children = []; this.style = {}; this.textContent = ''; }
  appendChild(child){ this.children.push(child); return child; }
}
timetable.document = {createElement:tag => new Element(tag)};
const preview = timetable.ScheduleTimetable.render([
  {day:'Mon',start:'17:30',end:'18:20',name:'Module',section:'MDE 153.05',type:'lecture',week_pattern:'odd_weeks',quota:60,available:0},
  {day:'Mon',start:'17:30',end:'18:20',name:'Module',section:'MDE 153.06',type:'practice',week_pattern:'even_weeks',quota:20,available:0}
], 'Selected quota');
const table = preview.children[0];
const body = table.children.find(element => element.tag === 'tbody');
assert.equal(body.children.length, 1);
assert.equal(body.children[0].children[0].textContent, '17:30–18:20');
const cards = body.children[0].children[1].children[0].children;
assert.equal(cards.length, 2);
assert.equal(cards[0].children[3].textContent, timetable.ScheduleTimetable.weekLabel('odd_weeks'));
assert.equal(cards[1].children[4].textContent, '0/20 мест');
const semester = raw.subjects.filter(item => item.semester === 5).map(item => generator.importScheduledCourse(item, '#fff'));
for(const dayOff of ['', ...generator.DAYS]){
  result = generator.generateSchedules(semester, dayOff);
  for(let index=0; index<result.results.length; index++){
    const picks = result.results[index];
    const meetings = picks.flatMap(pick => {
      assert.equal(pick.practice.lecture_section, pick.lecture.section);
      assert(generator.sectionAvailable(pick.lecture) && generator.sectionAvailable(pick.practice));
      return generator.meetingsInMinutes(pick.lecture).concat(generator.meetingsInMinutes(pick.practice));
    });
    assert(!generator.hasInternalOverlap(meetings));
    if(dayOff) assert(meetings.every(item => item.day !== dayOff));
    if(index) assert(generator.compareQuality(generator.scheduleQuality(result.results[index-1]), generator.scheduleQuality(picks)) <= 0);
  }
}
module.exports = 'Passed: script syntax, gap ranking, sparse-day ranking, strict day off, impossible preferences, best results beyond 500, curriculum conflicts and quotas.';
if(require.main === module) console.log(module.exports);
