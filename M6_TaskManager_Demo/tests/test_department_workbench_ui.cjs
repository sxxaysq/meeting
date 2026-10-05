const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options, requestPath, requestOptions;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } },
  fetch: async (url, opts) => { requestPath = url; requestOptions = opts; return { ok: true, json: async () => [] }; },
});
const app = { ...options.data(), ...options.methods, isDepartmentView: true, departmentId: 'DEPT-A' };
(async () => {
  await app.api('/api/tasks?status=open');
  assert.equal(requestPath, '/api/departments/DEPT-A/tasks?status=open');
  await app.api('/api/tasks/T1/history');
  assert.equal(requestPath, '/api/departments/DEPT-A/tasks/T1/history');
  await app.api('/api/meetings');
  assert.equal(requestPath, '/api/departments/DEPT-A/meetings');
  await assert.rejects(app.api('/api/review-candidates'));
  await assert.rejects(app.api('/api/tasks-other'));
  await assert.rejects(app.api('/api/departments/DEPT-B'));
  app.events = [{ task_id: 'FOREIGN' }]; await app.loadEvents(); assert.equal(app.events.length, 0);
  app.processingTask = { task_id: 'T1', version: 7 };
  app.progressDraft = { status: 'completed', progress_note: '完成检查' };
  app.loadAll = async () => {}; await app.saveDepartmentProgress();
  assert.equal(requestPath, '/api/departments/DEPT-A/tasks/T1');
  assert.equal(JSON.parse(requestOptions.body).expected_version, 7);
  assert.equal(app.processingTask, null);
  assert(app.taskActionMessage.includes('移出'));
  console.log('department workbench UI regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
