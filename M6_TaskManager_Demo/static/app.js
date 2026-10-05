const { createApp } = Vue;

createApp({
  data() {
    return {
      basePath: '', departmentId: '', departmentName: '', departmentWorkspaces: [], organization: { company: '', units: [] }, departmentSummary: null, workspaceReady: false, workspaceError: '', processingTask: null, progressDraft: { status: 'open', progress_note: '' },
      activeTab: 'tasks', health: 'checking', busy: false, message: '', messageIsError: false,
      form: { meetingTitle: '', meetingDate: new Date().toISOString().slice(0, 10), meetingTime: '', meetingType: '调度会', attendees: '', leaderRequirements: '' },
      run: null, summary: null, tasks: [], events: [], selectedTask: null, taskEvents: [], taskMeeting: null, taskHistory: [], historyOpen: false, historyLoading: false, historyLoadedTaskId: '', meetings: [], selectedMeetingId: '', reviewCandidates: [], reviewDrafts: {}, reviewMessage: '', reviewMessageIsError: false, editingTask: false, taskDraft: {}, taskEditorMessage: '', taskEditorMessageIsError: false,
      deletionTarget: null, taskMutationBusy: false, taskActionMessage: '', taskActionMessageIsError: false,
      taskFilter: '', includeDeleted: false, searchText: '', departmentFilter: '', projectFilter: '', pageSize: 10, currentPage: 1, pollTimer: null,
      stages: [
        { index: 1, key: 'running_m1', label: 'M1 预处理' }, { index: 2, key: 'running_m2', label: 'M2 分类' },
        { index: 3, key: 'running_m6', label: 'M6 命令' }, { index: 4, key: 'completed', label: '数据库更新' },
      ],
    };
  },
  computed: {
    isDepartmentView() { return Boolean(this.departmentId); },
    normalizedTasks() { return this.tasks.map((task) => this.enrichTask(task)); },
    departments() { return [...new Set(this.normalizedTasks.map((task) => task.department).filter(Boolean))].sort(); },
    projects() { return [...new Set(this.normalizedTasks.map((task) => task.project).filter(Boolean))].sort(); },
    taskRows() {
      const keyword = this.searchText.toLowerCase();
      return this.normalizedTasks.filter((task) => {
        const searchable = [task.title, task.department, task.project, task.assignee_raw, task.taskDescription].filter(Boolean).join(' ').toLowerCase();
        return (!keyword || searchable.includes(keyword))
          && (!this.departmentFilter || task.department === this.departmentFilter)
          && (!this.projectFilter || task.project === this.projectFilter);
      });
    },
    departmentGroups() {
      const groups = new Map();
      this.taskRows.forEach((task) => {
        const name = task.department || '未标注部门';
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push(task);
      });
      return [...groups.entries()].map(([name, tasks]) => ({ name, tasks }));
    },
    totalPages() { return Math.max(1, Math.ceil(this.taskRows.length / this.pageSize)); },
    pagedTaskRows() {
      if (this.currentPage > this.totalPages) this.currentPage = this.totalPages;
      const start = (this.currentPage - 1) * this.pageSize;
      return this.taskRows.slice(start, start + this.pageSize);
    },
    selectedMeeting() { return this.meetings.find((meeting) => meeting.meeting_id === this.selectedMeetingId) || null; },
  },
  async mounted() {
    this.basePath = window.location.pathname.startsWith('/meeting/') ? '/meeting' : '';
    const match = window.location.pathname.slice(this.basePath.length).match(/^\/departments\/([^/]+)\/?$/);
    if (match) this.departmentId = match[1];
    try {
      if (this.departmentId) {
        const workspace = await this.api(`/api/departments/${this.departmentId}`);
        this.departmentName = workspace.name; this.departmentSummary = workspace; document.title = workspace.name + '工作台';
      }
      await this.checkHealth(); await this.loadAll();
    } catch (error) { this.workspaceError = error.message; }
    finally { this.workspaceReady = true; }
  },
  beforeUnmount() { if (this.pollTimer) clearTimeout(this.pollTimer); },
  methods: {
    async api(path, options = {}) {
      if (this.isDepartmentView && path.startsWith('/api/')) {
        if (/^\/api\/tasks(?:[/?]|$)/.test(path) || path === '/api/meetings') path = `/api/departments/${this.departmentId}` + path.slice(4);
        else if (path !== `/api/departments/${this.departmentId}`) throw new Error('部门工作台不支持该操作');
      }
      const response = await fetch(this.basePath + path, options);
      if (!response.ok) { let detail = `请求失败 (${response.status})`; try { detail = (await response.json()).detail || detail; } catch (_) {} throw new Error(detail); }
      return response.json();
    },
    extractField(description, name) {
      const match = (description || '').match(new RegExp(`(?:^|\\n)${name}：\\s*([^\\n]+)`));
      return match ? match[1].trim() : '';
    },
    taskBody(description) {
      return (description || '').split('\n').filter((line) => !/^(部门|项目|优先级)：/.test(line)).join('\n').trim();
    },
    enrichTask(task) {
      const department = this.extractField(task.description, '部门');
      const project = this.extractField(task.description, '项目');
      return { ...task, department: task.department || department, project: task.project || project, priority: task.priority || this.extractField(task.description, '优先级'), taskDescription: this.taskBody(task.description), category: task.department || task.project || department || project ? '项目级任务' : '存量任务' };
    },
    priorityClass(priority) { return ({ 高: 'high', 中: 'mid', 低: 'low' })[priority] || 'none'; },
    async checkHealth() { try { this.health = (await this.api('/health')).status; } catch (_) { this.health = 'bad'; } },
    async startRun() {
      const file = this.$refs.fileInput.files[0]; if (!file) return;
      this.busy = true; this.message = ''; this.messageIsError = false;
      const body = new FormData(); body.append('file', file); body.append('meeting_date', this.form.meetingDate); body.append('meeting_type', this.form.meetingType); body.append('meeting_title', this.form.meetingTitle); body.append('meeting_time', this.form.meetingTime); body.append('attendees', this.form.attendees); body.append('leader_requirements', this.form.leaderRequirements);
      try { this.run = await this.api('/api/demo/runs', { method: 'POST', body }); this.events = []; this.summary = null; this.message = '运行已创建，正在处理会议材料。'; await this.pollRun(); }
      catch (error) { this.message = error.message; this.messageIsError = true; this.busy = false; }
    },
    async pollRun() {
      if (!this.run) return;
      try {
        this.run = await this.api(`/api/demo/runs/${this.run.run_id}`); this.summary = await this.api(`/api/demo/runs/${this.run.run_id}/summary`);
        if (['completed', 'completed_with_errors', 'failed'].includes(this.run.status)) {
          this.busy = false; this.message = this.run.status === 'failed' ? this.runFailureMessage() : '处理完成，任务清单已刷新。'; this.messageIsError = this.run.status === 'failed'; await this.loadAll(); return;
        }
        this.pollTimer = setTimeout(() => this.pollRun(), 1200);
      } catch (error) { this.message = error.message; this.messageIsError = true; this.busy = false; }
    },
    runFailureMessage() {
      const detail = this.run?.error_summary || '';
      const safePrefixes = ['模型服务暂时不可用', '模型服务连接异常', '模型服务未配置', '会议材料处理超时', '会议材料处理失败'];
      const safeDetail = safePrefixes.some(prefix => detail.startsWith(prefix))
        ? detail : '会议材料处理未完成';
      return `运行失败：${safeDetail} 可稍后重新上传重试。`;
    },
    async loadAll() { await Promise.all([this.loadTasks(), this.loadMeetings(), this.isDepartmentView ? this.loadDepartmentSummary() : this.loadEvents(), this.isDepartmentView ? Promise.resolve() : this.loadOrganization()]); },
    async loadDepartmentSummary() { this.departmentSummary = await this.api(`/api/departments/${this.departmentId}`); },
    async loadOrganization() {
      try { this.organization = await this.api('/api/organization'); }
      catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    async loadDepartmentWorkspaces() {
      try { this.departmentWorkspaces = await this.api('/api/departments'); }
      catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    async processDepartmentTask(taskId) {
      if (this.taskMutationBusy) return;
      this.taskActionMessage = ''; this.taskMutationBusy = true;
      try {
        const { task } = await this.api(`/api/tasks/${encodeURIComponent(taskId)}`);
        if (!['open', 'in_progress', 'blocked'].includes(task.status)) throw new Error('任务已结束，请刷新待办清单');
        this.processingTask = task; this.progressDraft = { status: task.status, progress_note: '' };
      } catch (error) { this.taskActionMessage = error.message; this.taskActionMessageIsError = true; }
      finally { this.taskMutationBusy = false; }
    },
    closeDepartmentProgress() { if (!this.taskMutationBusy) this.processingTask = null; },
    async saveDepartmentProgress() {
      if (!this.processingTask || this.taskMutationBusy) return;
      const task = this.processingTask; this.taskMutationBusy = true; this.taskActionMessage = '';
      try {
        await this.api(`/api/tasks/${encodeURIComponent(task.task_id)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...this.progressDraft, expected_version: task.version }) });
        this.processingTask = null; await this.loadAll();
        if (this.selectedTask?.task_id === task.task_id) await this.openTask(task.task_id);
        this.taskActionMessage = this.progressDraft.status === 'completed' ? '任务已完成，已从本部门待办清单移出。' : '任务进展已保存。'; this.taskActionMessageIsError = false;
      } catch (error) { this.taskActionMessage = error.message; this.taskActionMessageIsError = true; }
      finally { this.taskMutationBusy = false; }
    },
    async loadMeetings() { try { this.meetings = await this.api('/api/meetings'); } catch (error) { this.message = error.message; this.messageIsError = true; } },
    async loadTasks() {
      const params = new URLSearchParams(); if (this.taskFilter) params.set('status', this.taskFilter); if (!this.isDepartmentView) params.set('include_deleted', String(this.includeDeleted)); if (this.selectedMeetingId) params.set('meeting_id', this.selectedMeetingId);
      try { this.tasks = await this.api(`/api/tasks?${params}`); this.resetPage(); } catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    onMeetingChange() { this.closeTask(); this.loadTasks(); this.loadEvents(); },
    resetPage() { this.currentPage = 1; },
    filterByDepartment(department) { this.departmentFilter = department; this.projectFilter = ''; this.resetPage(); },
    filterByProject(project) { this.projectFilter = project; this.departmentFilter = ''; this.resetPage(); },
    async loadEvents() {
      if (this.isDepartmentView) { this.events = []; return; }
      try { this.events = await this.api('/api/events'); }
      catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    async loadReviewCandidates() {
      try { this.reviewCandidates = await this.api('/api/review-candidates'); this.reviewMessage = ''; }
      catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    reviewDraft(item) {
      return this.reviewDrafts[item.candidate_id] ||= { operations: [], targets: [], loading: false, loaded: false, submitting: false, targetBusy: false, error: '' };
    },
    async loadReviewPlan(item, refresh = false) {
      const draft = this.reviewDraft(item);
      if (draft.loading || (draft.loaded && !refresh)) return;
      if (refresh && draft.operations.length && !window.confirm('重新生成会替换当前未保存的表单，是否继续？')) return;
      draft.loading = true; draft.error = '';
      try {
        const result = await this.api(`/api/review-candidates/${item.candidate_id}/plan`, { method: 'POST' });
        draft.operations = result.operations; draft.targets = result.tasks; draft.loaded = true;
      } catch (error) { draft.error = error.message; }
      finally { draft.loading = false; }
    },
    addReviewOperation(item) {
      const c = item.candidate; const draft = this.reviewDraft(item);
      draft.operations.push({ action: 'CREATE', target_task_id: null, expected_version: null,
        title: c.title || '', description: c.description || c.title || '', assignee_raw: c.assignee || '',
        deadline_raw: c.deadline || '', department: c.department || '', project: c.project || '',
        priority: c.priority || '', status: 'open', source_fragments: [], reason: '手工补充，请填写该子任务对应的说明。' });
      draft.loaded = true;
    },
    reviewTargets(item) { return this.reviewDraft(item).targets.length ? this.reviewDraft(item).targets : (item.candidate.candidate_tasks || []); },
    async selectReviewTarget(item, op, taskId) {
      const draft = this.reviewDraft(item); draft.error = ''; draft.targetBusy = true;
      op.target_task_id = taskId || null; op.expected_version = null;
      try {
        const target = taskId ? (await this.api(`/api/tasks/${encodeURIComponent(taskId)}`)).task : null;
        if (target?.is_deleted) throw new Error('该任务已删除，请选择其他目标');
        op.action = target ? 'UPDATE_FIELDS' : 'CREATE'; op.expected_version = target?.version ?? null;
        const base = target || { ...item.candidate, assignee_raw: item.candidate.assignee, deadline_raw: item.candidate.deadline, status: 'open' };
        for (const field of ['title', 'assignee_raw', 'deadline_raw', 'department', 'project', 'priority', 'status']) op[field] = base[field] || '';
        op.reason = target ? `人工选择更新“${target.title}”，请核对本次说明与目标的对应关系。` : '人工选择新建独立任务，请核对是否与已有任务重复。';
      } catch (error) { draft.error = error.message; }
      finally { draft.targetBusy = false; }
    },
    confidenceLabel(score, source) { return score == null ? '需人工判断' : `置信度 ${Math.round(score * 100)}%（${source === 'model' ? '模型' : '规则'}）`; },
    reviewReasonParts(item) {
      let reason = item.reason_code || '未记录具体原因，请核对原文和关联任务。';
      const labels = { low_model_confidence: '模型对任务提取结果置信度不足，需要核对任务内容与原文是否一致。', non_active_initial_status: '新任务的初始状态异常，需要确认应新建任务还是更新已有任务。' };
      reason = labels[reason] || reason.replace(/^BUSINESS_REVIEW:\s*/, '');
      const pending = reason.match(/PENDING_REVIEW_DUPLICATE:\s*([A-Za-z0-9-]+)/);
      if (pending) {
        const previous = this.reviewCandidates.find(row => row.candidate_id === pending[1]);
        reason = `同一业务目标此前已有待复核条目${previous ? `“${previous.candidate.title}”` : ''}尚未处理，本次已暂缓新建，避免重复任务。请先核对此前条目，再决定本次内容的归属。`;
      }
      reason = reason.replace('DUPLICATE_GOAL:', '目标重复：').replace('PROJECT_IDENTITY:', '项目归属待确认：').replace('TARGET_SCOPE:', '任务范围待确认：')
        .replace('部门无法唯一映射到 Department Master：None', '原文未明确责任部门，无法匹配部门名录；请确认实际承办部门。');
      const parts = []; let offset = 0;
      for (const match of reason.matchAll(/候选(?:任务)?\s*(\d+(?:\s*[/、]\s*\d+)*)/g)) {
        parts.push({ text: reason.slice(offset, match.index) });
        match[1].split(/\s*[/、]\s*/).forEach((index, position) => {
          if (position) parts.push({ text: '、' });
          const task = (item.candidate.candidate_tasks || [])[Number(index)];
          parts.push(task ? { text: `“${task.title || '未命名任务'}”`, taskId: task.task_id } : { text: '未能定位的关联任务（请核对原始记录）' });
        });
        offset = match.index + match[0].length;
      }
      parts.push({ text: reason.slice(offset) });
      return parts;
    },
    reviewGuidance(item) {
      if ((item.reason_code || '').includes('部门无法唯一映射')) return '请核对承办部门及项目归属；填写部门名称本身不会确认任务归属。';
      return (item.candidate.candidate_tasks || []).length
        ? '请对照原文核对各关联任务的目标、项目、部门和历史进展，确认每个子目标应更新哪个任务或新建任务。展开处理表单后，可修改建议、增加或移除子任务。'
        : '未检索到可对照的已有任务。请核对原文中的目标、部门和项目，确认字段完整且确需新建任务。';
    },
    attendeeList(value) { return String(value || '').split(/[、，,\n]+/).map((item) => item.trim()).filter(Boolean); },
    requirementList(value) { return String(value || '').split(/\n|\d+[.、]/).map((item) => item.trim()).filter(Boolean); },
    async approveReview(item) {
      const draft = this.reviewDraft(item); if (draft.submitting || draft.loading || draft.targetBusy) return;
      draft.submitting = true; draft.error = '';
      try {
        const result = await this.api(`/api/review-candidates/${item.candidate_id}/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operations: draft.operations }) });
        await Promise.all([this.loadReviewCandidates(), this.loadTasks()]);
        this.reviewMessage = `已确认并执行 ${result.results.length} 个子任务。`; this.reviewMessageIsError = false;
        delete this.reviewDrafts[item.candidate_id];
      } catch (error) { draft.error = error.message; }
      finally { draft.submitting = false; }
    },
    async rejectReview(item) {
      try {
        await this.api(`/api/review-candidates/${item.candidate_id}/reject`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        this.reviewMessage = '候选已拒绝。'; this.reviewMessageIsError = false; await this.loadReviewCandidates();
      } catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    async saveReview(item) {
      const draft = this.reviewDraft(item); draft.submitting = true; draft.error = '';
      try {
        await this.api(`/api/review-candidates/${item.candidate_id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operations: draft.operations }) });
        this.reviewMessage = '全部子任务表单已保存，尚未执行。'; this.reviewMessageIsError = false;
      } catch (error) { draft.error = error.message; }
      finally { draft.submitting = false; }
    },
    async openTask(taskId) {
      try {
        const detail = await this.api(`/api/tasks/${taskId}`);
        this.selectedTask = this.enrichTask(detail.task); this.taskEvents = detail.events; this.taskMeeting = detail.meeting;
        this.taskHistory = []; this.historyOpen = false; this.historyLoading = false; this.historyLoadedTaskId = '';
      }
      catch (error) {
        if (this.activeTab === 'review') { this.reviewMessage = '无法加载任务详情：' + error.message; this.reviewMessageIsError = true; }
        else { this.message = error.message; this.messageIsError = true; }
      }
    },
    closeTask() { this.selectedTask = null; this.taskEvents = []; this.taskMeeting = null; this.taskHistory = []; this.historyOpen = false; this.historyLoading = false; this.historyLoadedTaskId = ''; },
    async toggleTaskHistory() {
      if (!this.selectedTask) return;
      if (this.historyOpen) { this.historyOpen = false; return; }
      this.historyOpen = true;
      const taskId = this.selectedTask.task_id;
      if (this.historyLoadedTaskId === taskId) return;
      this.historyLoading = true;
      try {
        const history = await this.api(`/api/tasks/${taskId}/history`);
        if (this.selectedTask?.task_id !== taskId) return;
        this.taskHistory = history; this.historyLoadedTaskId = taskId;
      } catch (error) {
        this.message = error.message; this.messageIsError = true; this.historyOpen = false;
      } finally {
        if (this.selectedTask?.task_id === taskId) this.historyLoading = false;
      }
    },
    newTask() { this.taskDraft = { title: '', description: '', work_items: [], assignee_raw: '', deadline_raw: '', department: '', project: '', priority: '', status: 'open', evidence_text: '', source_meeting_id: this.selectedMeetingId || '' }; this.taskEditorMessage = ''; this.taskEditorMessageIsError = false; this.editingTask = true; },
    async editTask(taskId = this.selectedTask?.task_id) {
      if (!taskId || this.taskMutationBusy) return;
      this.taskMutationBusy = true; this.taskActionMessage = '';
      try {
        const { task } = await this.api(`/api/tasks/${encodeURIComponent(taskId)}`);
        if (task.is_deleted) throw new Error('已删除的任务不能修改');
        this.taskDraft = { ...task, work_items: [...(task.work_items || [])] };
        this.taskEditorMessage = ''; this.taskEditorMessageIsError = false; this.editingTask = true;
      } catch (error) { this.taskActionMessage = error.message; this.taskActionMessageIsError = true; }
      finally { this.taskMutationBusy = false; }
    },
    closeTaskEditor() { if (this.taskMutationBusy) return; this.editingTask = false; this.taskDraft = {}; },
    async saveTask() {
      if (this.taskMutationBusy) return;
      const draft = this.taskDraft; this.taskEditorMessage = ''; this.taskEditorMessageIsError = false;
      this.taskMutationBusy = true;
      const payload = { title: draft.title, description: draft.description, work_items: draft.work_items || [], assignee_raw: draft.assignee_raw, deadline_raw: draft.deadline_raw, department: draft.department, project: draft.project, priority: draft.priority, status: draft.status };
      if (!draft.task_id) payload.source_meeting_id = draft.source_meeting_id || '';
      if (!draft.task_id) payload.evidence_text = draft.evidence_text;
      try {
        const saved = await this.api(draft.task_id ? `/api/tasks/${encodeURIComponent(draft.task_id)}` : '/api/tasks', { method: draft.task_id ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        this.editingTask = false; this.taskDraft = {};
        await Promise.all([this.loadTasks(), this.loadEvents()]);
        if (draft.task_id && this.selectedTask?.task_id === draft.task_id) await this.openTask(saved.task_id);
        this.taskActionMessage = draft.task_id ? '任务修改已保存。' : '任务已新增。'; this.taskActionMessageIsError = false;
      } catch (error) { this.taskEditorMessage = error.message; this.taskEditorMessageIsError = true; }
      finally { this.taskMutationBusy = false; }
    },
    deleteTask(task = this.selectedTask) {
      if (!task || task.is_deleted || this.taskMutationBusy) return;
      this.deletionTarget = { task_id: task.task_id, title: task.title };
      this.taskActionMessage = ''; this.taskActionMessageIsError = false;
    },
    cancelTaskDeletion() { if (!this.taskMutationBusy) this.deletionTarget = null; },
    async confirmTaskDeletion() {
      const task = this.deletionTarget;
      if (!task || this.taskMutationBusy) return;
      this.taskMutationBusy = true; this.taskActionMessage = '';
      try {
        await this.api(`/api/tasks/${encodeURIComponent(task.task_id)}`, { method: 'DELETE' });
        this.deletionTarget = null;
        if (this.selectedTask?.task_id === task.task_id) this.closeTask();
        await Promise.all([this.loadTasks(), this.loadEvents()]);
        this.taskActionMessage = '任务已删除。勾选“含软删除”可查看历史记录。'; this.taskActionMessageIsError = false;
      } catch (error) { this.taskActionMessage = error.message; this.taskActionMessageIsError = true; }
      finally { this.taskMutationBusy = false; }
    },
    async exportTrainingSamples() {
      try {
        const response = await fetch(this.basePath + '/api/training-samples/export');
        if (!response.ok) { const body = await response.json(); throw new Error(body.detail || '导出失败'); }
        const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = 'meeting-task-training-samples.zip'; link.click(); URL.revokeObjectURL(url);
      } catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    statusLabel(status) { return ({ queued: '排队中', running_m1: 'M1 处理中', running_m2: 'M2 分类中', running_m6: 'M6 执行中', applying_database: '数据库更新中', completed: '已完成', completed_with_errors: '完成但有失败项', failed: '运行失败' })[status] || status; },
    stageClass(key) {
      if (!this.run) return ''; const order = ['queued', 'running_m1', 'running_m2', 'running_m6', 'completed']; const current = ['completed_with_errors', 'failed'].includes(this.run.status) ? (this.run.status === 'failed' ? -1 : 4) : order.indexOf(this.run.status); const target = order.indexOf(key);
      return { active: current === target, done: current > target || ['completed', 'completed_with_errors'].includes(this.run.status) };
    },
    taskStatusLabel(task) { if (task.is_deleted) return '已软删除'; return ({ open: '待执行', in_progress: '进行中', blocked: '受阻', completed: '已完成', cancelled: '已取消' })[task.status] || task.status; },
    historyActionLabel(action) { return ({ CREATE: '首次发布', HUMAN_CREATE: '人工发布', UPDATE_FIELDS: '进度更新', UPDATE_STATUS: '状态更新' })[action] || action; },
    historyPublishedAt(item) {
      if (!item.meeting?.meeting_date) return this.formatTime(item.published_at);
      return [item.meeting.meeting_date, item.meeting.meeting_time].filter(Boolean).join(' ');
    },
    eventMeeting(event) {
      const meeting = this.meetings.find(item => item.meeting_id === event.source_meeting_id);
      if (!meeting) return { title: event.source_meeting_id && event.source_meeting_id !== 'MANUAL' ? '会议信息未找到' : '未关联会议', time: '未记录' };
      return { title: meeting.meeting_title || meeting.source_file || '未命名会议',
        time: [meeting.meeting_date || '日期未记录', meeting.meeting_time || '（具体时间未记录）'].join(' ') };
    },
    formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; },
  },
}).mount('#app');
