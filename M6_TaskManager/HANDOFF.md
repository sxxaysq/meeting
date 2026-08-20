# M6 Task Lifecycle Manager 交接文档

## 0. 当前结论

新 M6 已作为独立 `M6_TaskManager/` 实现，并于 2026-08-19 接入现有
`M6_TaskManager_Demo/` 的上传编排；生命周期库通过兼容仓储投影到未改版的原前端 API，
旧 importer 代码与投影前 `demo.db` 备份仍保留作回滚参考。目标服务器 Python 3.11 测试
**34 passed**，演示服务回归 **40 passed**。

28 条跨周架构验证 Gold 的最新完整链路实测：decision accuracy 0.9643，
target_task_id accuracy 0.9375，wrong-target / false-update / false-create 均为 0，
department routing accuracy 1.0，幂等违规 0，事务失败 0。唯一保守误差是 1 条真实进展
进入 REVIEW。

## 1. 环境

| 项 | 值 |
|---|---|
| 工作机 | `yty@192.168.30.214` |
| Python | `/home/yty/m1x_venv/bin/python` |
| LLM | `http://192.168.30.215:8000/v1` |
| Model | `Qwen/Qwen3.6-35B-A3B` |
| thinking | `LLM_ENABLE_THINKING=false` |
| Git 基线 | `36a19e2 feat/m2-semantic-consolidator` |
| M6 分支 | `feat/m6-task-lifecycle` |

模型 ID 不是 `Qwen/Qwen3.5-36B-A3B`。`/v1/models` 已实测只有生成模型，没有
embedding 模型，因此本轮没有接纯 Embedding baseline。

## 2. 输入硬边界

只接受新 M2 Schema，且默认必须 `validation.status=PASS`。当前已提交的 block 回归产物
10 份都是文档级 REVIEW（项目实体不确定），所以不能静默绕过门控做生产写库。人工已明确
确认当前 M2 REVIEW 可作为预览基线时，CLI 可显式传入 `--accept-review-input`；该开关仍拒绝
ERROR，并保留原始 validation/issues 与人工接受标记，不能用于无人值守生产写库。

来源 ID：显式 `--document-id` 优先；否则对 M2 `source_mode/items/merge_trace` 稳定哈希。
`source_item_id` 使用 document_id、source_indexes、evidence 坐标、content、item_type 生成。

M2 的 `project_entities`、`merge_trace`、`validation`、`source_mode` 和 Item 原文全部保存到
`task_source_links.provenance_json` 与 Audit。M2 当前非连续 evidence 的已知问题不会被 M6
重新包络；全部 `source_evidence` 仍可从 provenance 回溯。

## 3. 旧 M6 处置

历史 Web 主链路是旧 M1 → 旧 M2 gate → M1.5 → Dice matcher importer；当前上传入口已切换
为新 M1 Generic → 新 M2 Generic → 新 M6。历史详情见 `docs/OLD_M6_CALL_CHAIN.md`。

复用：Repository 分层、参数化 SQL、事务、幂等、版本、审计思想。

重写：输入契约、历史候选召回、生命周期动作、Prompt、Validator、状态机、数据表、部门
路由与编排。旧 Dice matcher 只保留为评估 baseline。

## 4. 代码地图

```text
src/m2_input.py             M2 PASS Schema 门控与稳定来源 ID
src/candidate_retriever.py  项目优先的 lexical Top-K；active + closed
src/lifecycle_judge.py      只对候选做语义判断；一次格式修复
src/command_validator.py    目标/项目/部门/状态/版本/grounding 安全边界
src/state_machine.py        生命周期迁移
src/repository.py           SQLite 参数化访问
src/executor.py             单事务写 Task/Event/Link/Audit/Dispatch/Idempotency
src/department_router.py    Department Master 精确/别名路由
src/service.py              SKIP → Recall → Judge → Validate → Execute
src/cli.py                  init-db / import-history / process / inspect
```

## 5. 运行与复现

```bash
cd M6_TaskManager
/home/yty/m1x_venv/bin/python -m pytest tests -q
```

```bash
/home/yty/m1x_venv/bin/python eval/evaluate_lifecycle.py \
  --mode old --output results/old_baseline.json
```

```bash
export LLM_BASE_URL=http://192.168.30.215:8000/v1
export LLM_MODEL=Qwen/Qwen3.6-35B-A3B
export LLM_ENABLE_THINKING=false
/home/yty/m1x_venv/bin/python eval/evaluate_lifecycle.py \
  --mode pipeline --output results/qwen_pipeline.json
```

## 6. 评估口径与误差样本

- 旧 false UPDATE：`CREATE_HIGH_SIMILARITY_DIFFERENT_PHASE`；一期与二期高度相似但独立验收。
- 新 false UPDATE：0。
- 新 false CREATE：0。
- 新 wrong-target：0。
- 新 REVIEW：Gold 的 4 条高风险歧义全部 REVIEW，另把
  `PROGRESS_RECENT_EVENT_LINK` 保守 REVIEW。

原始预测和简短业务 reason 在 `results/*.json`。未保存 chain-of-thought。

## 7. 已知缺口与下一步

1. 28 条 Gold 是架构验证集，不是生产精度证据。必须人工建立 100–300 条真实跨周标注。
2. Candidate Retrieval 当前为字符 Dice + 字段/近期事件混合召回；没有可用 embedding 服务。
3. Web 当前固定使用 Generic 模式；是否开放 block/generic 前端选择需等待真实使用需求。
4. 前端预览主表覆盖本轮 17 个部门，但仍是非生产路由；生产部署前必须导入完整权威部门主表。
5. M2 REVIEW 只有在人工确认后由显式开关准入，ERROR 始终拒绝；生产无人值守仍应只接 PASS。
6. 新生命周期数据库与旧 Demo 数据库隔离；如需迁移旧数据，应写显式迁移和核对报告，不能直接复用旧库。
