const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } },
});
const app = { ...options.data(), ...options.methods };
app.meetings = [
  { meeting_id: 'M1', meeting_title: '第一次会议', meeting_date: '2026-06-22', meeting_time: '09:30' },
  { meeting_id: 'MEETING_SECOND', source_file: '第二次会议.pdf', meeting_date: '2026-06-29' },
];
assert.equal(app.eventMeeting({ source_meeting_id: 'M1', created_at: '2026-09-20' }).title, '第一次会议');
assert.equal(app.eventMeeting({ source_meeting_id: 'M1' }).time, '2026-06-22 09:30');
assert.equal(app.eventMeeting({ source_meeting_id: 'MEETING_SECOND' }).title, '第二次会议.pdf');
assert.equal(app.eventMeeting({ source_meeting_id: 'MEETING_SECOND' }).time, '2026-06-29 （具体时间未记录）');
assert.equal(app.eventMeeting({ source_meeting_id: 'MANUAL' }).title, '未关联会议');
assert.equal(app.eventMeeting({ source_meeting_id: 'MISSING' }).time, '未记录');
console.log('event meeting UI regression passed');
