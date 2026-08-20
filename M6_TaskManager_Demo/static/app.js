const { createApp } = Vue;

createApp({
  data() {
    return {
      activeTab: 'tasks', health: 'checking', busy: false, message: '', messageIsError: false,
      form: { meetingTitle: '', meetingDate: new Date().toISOString().slice(0, 10), meetingTime: '', meetingType: '调度会', attendees: '', leaderRequirements: '' },
      run: null, summary: null, tasks: [], events: [], selectedTask: null, taskEvents: [], taskMeeting: null, taskHistory: [], historyOpen: false, historyLoading: false, historyLoadedTaskId: '', meetings: [], selectedMeetingId: '', reviewCandidates: [], reviewDrafts: {}, reviewMessage: '', reviewMessageIsError: false, editingTask: false, taskDraft: {}, taskEditorMessage: '', taskEditorMessageIsError: false,
      taskFilter: '', includeDeleted: false, searchText: '', departmentFilter: '', projectFilter: '', pageSize: 10, currentPage: 1, pollTimer: null,
      stages: [
        { index: 1, key: 'running_m1', label: 'M1 预处理' }, { index: 2, key: 'running_m2', label: 'M2 分类' },
        { index: 3, key: 'running_m6', label: 'M6 命令' }, { index: 4, key: 'completed', label: '数据库更新' },
      ],
    };
  },
  computed: {
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
  async mounted() { await this.checkHealth(); await this.loadAll(); },
  beforeUnmount() { if (this.pollTimer) clearTimeout(this.pollTimer); },
  methods: {
    async api(path, options = {}) {
      const response = await fetch(path, options);
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
    async loadAll() { await Promise.all([this.loadTasks(), this.loadEvents(), this.loadMeetings()]); },
    async loadMeetings() { try { this.meetings = await this.api('/api/meetings'); } catch (error) { this.message = error.message; this.messageIsError = true; } },
    async loadTasks() {
      const params = new URLSearchParams(); if (this.taskFilter) params.set('status', this.taskFilter); params.set('include_deleted', String(this.includeDeleted)); if (this.selectedMeetingId) params.set('meeting_id', this.selectedMeetingId);
      try { this.tasks = await this.api(`/api/tasks?${params}`); this.resetPage(); } catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    onMeetingChange() { this.closeTask(); this.loadTasks(); this.loadEvents(); },
    resetPage() { this.currentPage = 1; },
    filterByDepartment(department) { this.departmentFilter = department; this.projectFilter = ''; this.resetPage(); },
    filterByProject(project) { this.projectFilter = project; this.departmentFilter = ''; this.resetPage(); },
    async loadEvents() {
      try { this.events = await this.api('/api/events'); }
      catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    async loadReviewCandidates() {
      try { this.reviewCandidates = await this.api('/api/review-candidates'); this.reviewMessage = ''; }
      catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    reviewDraft(item) {
      if (!this.reviewDrafts[item.candidate_id]) {
        const candidate = item.candidate || {};
        this.reviewDrafts[item.candidate_id] = {
          title: candidate.title || '', description: candidate.description || candidate.title || '',
          assignee_raw: candidate.assignee || '', deadline_raw: candidate.deadline || '', department: candidate.department || '', project: candidate.project || '', priority: candidate.priority || '', status: candidate.initial_status || 'open',
        };
      }
      return this.reviewDrafts[item.candidate_id];
    },
    confidenceLabel(score, source) { return score == null ? '需人工判断' : `置信度 ${Math.round(score * 100)}%（${source === 'model' ? '模型' : '规则'}）`; },
    reviewReason(reason) { return ({ low_model_confidence: '模型低置信度', non_active_initial_status: '初始状态异常' })[reason] || `待复核：${reason}`; },
    attendeeList(value) { return String(value || '').split(/[、，,\n]+/).map((item) => item.trim()).filter(Boolean); },
    requirementList(value) { return String(value || '').split(/\n|\d+[.、]/).map((item) => item.trim()).filter(Boolean); },
    async approveReview(item) {
      const draft = this.reviewDraft(item); this.reviewMessage = ''; this.reviewMessageIsError = false;
      try {
        await this.api(`/api/review-candidates/${item.candidate_id}/approve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) });
        this.reviewMessage = '候选已确认并写入任务库。'; await Promise.all([this.loadReviewCandidates(), this.loadTasks()]);
      } catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    async rejectReview(item) {
      try {
        await this.api(`/api/review-candidates/${item.candidate_id}/reject`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        this.reviewMessage = '候选已拒绝。'; this.reviewMessageIsError = false; await this.loadReviewCandidates();
      } catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    async saveReview(item) {
      const draft = this.reviewDraft(item); this.reviewMessage = ''; this.reviewMessageIsError = false;
      const payload = { title: draft.title, description: draft.description, assignee: draft.assignee_raw, deadline: draft.deadline_raw, department: draft.department, project: draft.project, priority: draft.priority, initial_status: draft.status };
      try { await this.api(`/api/review-candidates/${item.candidate_id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); this.reviewMessage = '复核修改已保存，原文证据未变更。'; await this.loadReviewCandidates(); }
      catch (error) { this.reviewMessage = error.message; this.reviewMessageIsError = true; }
    },
    async openTask(taskId) {
      try {
        const detail = await this.api(`/api/tasks/${taskId}`);
        this.selectedTask = this.enrichTask(detail.task); this.taskEvents = detail.events; this.taskMeeting = detail.meeting;
        this.taskHistory = []; this.historyOpen = false; this.historyLoading = false; this.historyLoadedTaskId = '';
      }
      catch (error) { this.message = error.message; this.messageIsError = true; }
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
    editTask() { if (!this.selectedTask) return; this.taskDraft = { ...this.selectedTask, work_items: [...(this.selectedTask.work_items || [])] }; this.taskEditorMessage = ''; this.taskEditorMessageIsError = false; this.editingTask = true; },
    closeTaskEditor() { this.editingTask = false; this.taskDraft = {}; },
    async saveTask() {
      const draft = this.taskDraft; this.taskEditorMessage = ''; this.taskEditorMessageIsError = false;
      const payload = { title: draft.title, description: draft.description, work_items: draft.work_items || [], assignee_raw: draft.assignee_raw, deadline_raw: draft.deadline_raw, department: draft.department, project: draft.project, priority: draft.priority, status: draft.status };
      if (!draft.task_id) payload.source_meeting_id = draft.source_meeting_id || '';
      if (!draft.task_id) payload.evidence_text = draft.evidence_text;
      try {
        const saved = await this.api(draft.task_id ? `/api/tasks/${draft.task_id}` : '/api/tasks', { method: draft.task_id ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        await this.loadTasks(); this.closeTaskEditor(); if (draft.task_id) await this.openTask(saved.task_id);
      } catch (error) { this.taskEditorMessage = error.message; this.taskEditorMessageIsError = true; }
    },
    async deleteSelectedTask() {
      if (!this.selectedTask || !window.confirm('确认软删除此任务？原文证据与审计记录会保留。')) return;
      try { await this.api(`/api/tasks/${this.selectedTask.task_id}`, { method: 'DELETE' }); this.closeTask(); await this.loadTasks(); }
      catch (error) { this.message = error.message; this.messageIsError = true; }
    },
    async exportTrainingSamples() {
      try {
        const response = await fetch('/api/training-samples/export');
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
    formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; },
  },
}).mount('#app');
