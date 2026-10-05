// Run with node tests/test_review_ui.cjs; no browser or extra dependency needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
let options;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } },
});
const app = { ...options.data(), ...options.methods };
const tasks = Array.from({ length: 12 }, (_, i) => ({ task_id: `T${i}`, title: `任务名称${i}` }));
const item = { reason_code: '候选0和候选1，以及候选4/6、候选10、候选99。', candidate: { candidate_tasks: tasks } };
const parts = app.reviewReasonParts(item);
assert.equal(parts.filter(p => p.taskId).map(p => p.taskId).join(','), 'T0,T1,T4,T6,T10');
assert(!parts.map(p => p.text).join('').includes('候选'));
assert(parts.map(p => p.text).join('').includes('未能定位'));
assert(app.reviewGuidance(item).includes('每个子目标'));
assert(app.reviewGuidance({ candidate: {} }).includes('未检索到'));
assert(app.reviewReasonParts({ reason_code: 'BUSINESS_REVIEW: 部门无法唯一映射到 Department Master：None', candidate: {} })[0].text.includes('未明确责任部门'));
app.reviewCandidates = [{ candidate_id: 'R1', candidate: { title: '此前任务名' } }];
assert(app.reviewReasonParts({ reason_code: 'PENDING_REVIEW_DUPLICATE: R1', candidate: {} })[0].text.includes('此前任务名'));
assert(app.reviewReasonParts({ reason_code: 'MULTI 子决策触发复核：PENDING_REVIEW_DUPLICATE: R1', candidate: {} })[0].text.includes('此前任务名'));
(async () => {
  app.api = async endpoint => { assert.equal(endpoint, '/api/tasks/T0'); return { task: { title: '任务名称0', description: '原始要求\n新进展' }, events: [], meeting: null }; };
  await app.openTask('T0');
  assert.equal(app.selectedTask.taskDescription, '原始要求\n新进展');
  app.closeTask();
  assert.equal(app.selectedTask, null);
  app.activeTab = 'review'; app.api = async () => { throw new Error('任务不存在'); };
  await app.openTask('missing');
  assert(app.reviewMessageIsError && app.reviewMessage.includes('任务不存在'));
  console.log('review UI regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
