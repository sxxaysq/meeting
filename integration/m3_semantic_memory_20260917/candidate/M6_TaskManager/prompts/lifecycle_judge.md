# M6 任务生命周期判断

根据当前 M1 原文和 M3 检索的历史任务，判断持续业务目标。M1 条目未做语义归并。只输出 JSON，不输出推理过程。
任务 ID、版本、来源和路由由程序管理，你只选择候选序号及需要修改的字段名。

- 同项目不等于同任务。不同独立目标可以 CREATE；不要因项目已有其他待复核事项就拒绝新工作。
- 描述或汇报部门变化不等于任务身份变化。目标明确的跨部门协作可以 PROGRESS_UPDATE，保留原责任部门。
- 不确定的项目已由程序保持独立，不强行合并别名。候选为空时，明确的新业务目标可 CREATE。
- identity_uncertain=true 的候选仅提示重复风险，不能自动关联；相同目标的身份未确认时复核该次操作。source_project保留原项目/子系统范围，父项目相同不能越过该范围。
- 目标不变的进度、后续要求和子步骤优先 PROGRESS_UPDATE；MODIFY 只用于明确的稳定字段变化。
- 新要求选 description，程序保留旧描述并追加，不会用当前片段覆盖完整历史描述。
- 多子系统属于同父项目卡，但状态相互独立。子步骤完成、部分完成、计划完成不是整体 COMPLETE。
- COMPLETE 必须有原文明确已经完成整个目标的逐字 evidence，scope 必须 same_task。
- CANCEL/REOPEN/TRANSFER 同样必须有明确原文 evidence。TRANSFER 额外选择原文明示的 transfer_to 部门名称。
- 已完成任务的后续记录不自动重开；若只是重复事实，选 SKIP，并选择对应目标。真正重新启动才 REOPEN。
- 一条原文包含多个相互独立的目标时：若每个目标都能唯一确定归属（各自对应一个候选，或明确是需要 CREATE 的新目标），选 MULTI，按原文顺序为每个目标给一条子决策（2–3 条）；任一目标归属不确定、或多个目标只能对应同一候选，仍选 REVIEW 并具体说明。
- sub_decisions 的子决策规则与单决策完全相同，每条独立选择自己的 target_index、scope 与 evidence；不允许 REVIEW/EXPAND，不允许嵌套 MULTI。
- 候选无法唯一确认等真实业务歧义，选 REVIEW 并具体说明。
- 候选不足以判断时选 EXPAND，每条最多一次。已经扩展后必须做业务判断，不再 EXPAND。
- 不根据置信度、相似度或常识补造人员、部门、时间、状态、范围或交付要求。

输出：
{"decision":"PROGRESS_UPDATE","target_index":0,"fields":[],"scope":"same_task","reason":"同一交付目标的后续进度","evidence":null}

多目标输出：
{"decision":"MULTI","target_index":null,"fields":[],"scope":"same_task","reason":"两个独立目标分别归属两个候选","evidence":null,"sub_decisions":[{"decision":"PROGRESS_UPDATE","target_index":0,"fields":[],"scope":"same_task","reason":"合同审核签订是候选0的后续进度","evidence":null},{"decision":"PROGRESS_UPDATE","target_index":1,"fields":[],"scope":"same_task","reason":"方案沟通汇报是候选1的后续进度","evidence":null}]}

decision: CREATE/PROGRESS_UPDATE/MODIFY/COMPLETE/CANCEL/REOPEN/TRANSFER/REVIEW/SKIP/EXPAND/MULTI。
target_index 为 historical_candidates 中的 index，CREATE/REVIEW/EXPAND/MULTI 时为 null。
fields 仅 MODIFY 时非空，可选 title/description/work_section/delivery_group/assignees，禁止返回字段值。
scope: same_task（同一目标）/subtask（目标的一部分）/uncertain（无法确认归属）。
evidence 必须逐字取自 current_item.content 或 evidence.text，普通进度可以为 null。
sub_decisions 仅 MULTI 时使用，2–3 条；子决策不允许 REVIEW/EXPAND/MULTI，CREATE 子决策的 target_index 为 null。
