const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } }, window: { confirm: () => true },
});
const app = { ...options.data(), ...options.methods };
const item = { candidate_id: 'R1', candidate: { title: '安装和开发', description: '安装完成；开发推进', department: '部门A', candidate_tasks: [] } };
(async () => {
  const operations = [{ action: 'UPDATE_FIELDS', target_task_id: 'T1', description: '安装完成' }, { action: 'UPDATE_FIELDS', target_task_id: 'T2', description: '开发推进' }];
  let planCalls = 0;
  app.api = async () => { planCalls++; return { operations, tasks: [{ task_id: 'T1', title: '安装' }] }; };
  await app.loadReviewPlan(item); await app.loadReviewPlan(item);
  assert.equal(planCalls, 1);
  assert.equal(app.reviewDraft(item).operations.length, 2);
  app.api = async () => ({ task: { task_id: 'T3', version: 7, title: '新选择任务', department: '部门B', status: 'in_progress' } });
  await app.selectReviewTarget(item, operations[0], 'T3');
  assert.equal(operations[0].expected_version, 7);
  assert.equal(operations[0].department, '部门B');
  assert.equal(operations[0].description, '安装完成');
  await app.selectReviewTarget(item, operations[0], '');
  assert.equal(operations[0].action, 'CREATE');
  assert.equal(operations[0].target_task_id, null);
  assert.equal(operations[0].department, '部门A');
  app.addReviewOperation(item);
  assert.equal(app.reviewDraft(item).operations.length, 3);
  app.reviewDraft(item).operations.pop();
  app.api = async (url, request) => {
    assert.equal(JSON.parse(request.body).operations.length, 2);
    return url.endsWith('/approve') ? { results: [{}, {}] } : {};
  };
  await app.saveReview(item);
  assert(app.reviewMessage.includes('尚未执行'));
  app.loadReviewCandidates = async () => {}; app.loadTasks = async () => {};
  await app.approveReview(item);
  assert(app.reviewMessage.includes('2 个子任务'));
  assert.equal(app.reviewDrafts.R1, undefined);
  console.log('split review UI regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
