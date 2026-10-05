# 旧 M6 实际调用链与处置

## 实际主链路

旧 Web 上传入口并没有使用 `TaskAutomationService` 的 LLM 命令生成路径作为正式主链路。
`M6_TaskManager_Demo/app/orchestrator.py` 实际执行：

```text
PDF / DOCX
  ↓ M1_Preprocess/src/main.py
旧 tasks.json
  ↓ M2_TaskClassifier/src/m1_task_gate.py
m2.task_gate.v2
  ↓ M1_5_ProjectNormalizer/src/main.py
m1_5_projected_tasks.json
  ↓ scripts/import_m2_task_candidates.py
ProjectTaskMatcher（Dice + 固定阈值）
  ↓
CREATE / UPDATE / REVIEW
  ↓ CommandValidator
CommandExecutor
  ↓
SQLite tasks + task_events
```

这条链路与新 M1/M2 不兼容：旧 M2 gate 依赖 `confidence/status`，新 M1 九字段没有这些
字段；旧 importer 只接受 `m2.task_gate.v2`，不接受新 M2 顶层
`items/project_entities/merge_trace/validation` 契约。

## 保留并升级的实现思想

- SQLite 参数化 SQL 与外键；
- `BEGIN IMMEDIATE` 事务边界；
- 乐观版本检查；
- 来源幂等键；
- before/after 审计；
- Repository / Validator / Executor 分层；
- LLM 不接触 SQL。

## 明确废弃为最终决策器的部分

- `project_task_matcher.py` 的 Dice 阈值 CREATE/UPDATE；
- 只搜索 active task 的召回；
- `CREATE/UPDATE/NOOP` 三类粗动作；
- 用新的 content 覆盖历史 description；
- 旧 M1 → 旧 M2 gate → M1.5 编排。

旧 `M6_TaskManager_Demo/` 未删除、未修改，继续作为 baseline 和回滚参考。新主线位于
`M6_TaskManager/`。
