const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options, lastPath;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } },
  fetch: async url => { lastPath = url; return { ok: true, json: async () => ({}) }; },
});
const app = { ...options.data(), ...options.methods, isDepartmentView: false };
(async () => {
  app.basePath = '/meeting';
  await app.api('/api/tasks'); assert.equal(lastPath, '/meeting/api/tasks');
  await app.api('/health'); assert.equal(lastPath, '/meeting/health');
  app.isDepartmentView = true; app.departmentId = 'DEPT';
  await app.api('/api/tasks/T1/history');
  assert.equal(lastPath, '/meeting/api/departments/DEPT/tasks/T1/history');
  app.basePath = ''; await app.api('/api/tasks');
  assert.equal(lastPath, '/api/departments/DEPT/tasks');
  console.log('LAN prefix UI regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
