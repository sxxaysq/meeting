const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let options;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), {
  Vue: { createApp(value) { options = value; return { mount() {} }; } },
});
const app = { ...options.data(), ...options.methods };
const task = { task_id: 'T1', title: '最新任务名', description: '最新说明', status: 'open', work_items: ['环节一'] };
(async () => {
  const calls = [];
  app.api = async (url, request) => { calls.push({ url, request }); return request ? task : { task }; };
  app.loadTasks = async () => {}; app.loadEvents = async () => {};
  let openedDetails = 0; app.openTask = async () => { openedDetails++; };
  await app.editTask('T1');
  assert.equal(app.taskDraft.title, '最新任务名');
  assert.notEqual(app.taskDraft.work_items, task.work_items);
  assert.equal(app.editingTask, true);
  app.taskDraft.title = '修改后的名称';
  await app.saveTask();
  const save = calls.find(call => call.request?.method === 'PATCH');
  assert.equal(save.url, '/api/tasks/T1');
  assert.equal(JSON.parse(save.request.body).title, '修改后的名称');
  assert(!('source_meeting_id' in JSON.parse(save.request.body)));
  assert.equal(app.editingTask, false);
  assert.equal(openedDetails, 0); // Editing from the list stays in the list.
  let before = calls.length;
  await app.deleteTask(task);
  assert.equal(calls.length, before); // Cancelling never sends DELETE.
  assert.equal(app.deletionTarget.title, '最新任务名');
  app.cancelTaskDeletion();
  assert.equal(app.deletionTarget, null);
  app.deleteTask(task);
  await app.confirmTaskDeletion();
  assert.equal(calls.at(-1).request.method, 'DELETE');
  assert(app.taskActionMessage.includes('已删除'));
  before = calls.length;
  await app.deleteTask({ ...task, is_deleted: 1 });
  assert.equal(calls.length, before);
  app.api = async () => { throw new Error('网络错误'); };
  app.deleteTask(task);
  await app.confirmTaskDeletion();
  assert.equal(app.taskActionMessage, '网络错误');
  assert.equal(app.deletionTarget.task_id, 'T1');
  assert.equal(app.taskMutationBusy, false);
  app.taskDraft = { ...task }; app.editingTask = true;
  await app.saveTask();
  assert.equal(app.editingTask, true); // Failed saves keep editable input.
  assert.equal(app.taskEditorMessage, '网络错误');
  assert.equal(app.taskMutationBusy, false);
  console.log('task actions UI regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
