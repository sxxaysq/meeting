# M6 Task Lifecycle Manager 交接文档

## 0. 当前结论

新 M6 已作为独立 `M6_TaskManager/` 实现，基于 `feat/m2-semantic-consolidator`，旧
`M6_TaskManager_Demo/` 原样保留。目标服务器 Python 3.11 测试 **32 passed**。

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
10 份都是文档级 REVIEW（项目实体不确定），所以不能拿这些产物绕过门控做生产写库。
先在 M2 消除或人工确认 REVIEW，或生成明确 PASS 的结果，再进入 M6。

来源 ID：显式 `--document-id` 优先；否则对 M2 `source_mode/items/merge_trace` 稳定哈希。
`source_item_id` 使用 document_id、source_indexes、evidence 坐标、content、item_type 生成。

M2 的 `project_entities`、`merge_trace`、`validation`、`source_mode` 和 Item 原文全部保存到
`task_source_links.provenance_json` 与 Audit。M2 当前非连续 evidence 的已知问题不会被 M6
重新包络；全部 `source_evidence` 仍可从 provenance 回溯。

## 3. 旧 M6 处置

旧 Web 主链路实际是旧 M1 → 旧 M2 gate → M1.5 → Dice matcher importer，并没有走新
M2。详情见 `docs/OLD_M6_CALL_CHAIN.md`。

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
3. 当前 CLI 是批处理入口，尚未把旧 FastAPI 前端迁到新主链路。
4. Department Master 示例只有 3 个部门，生产部署前必须导入完整权威部门主表。
5. 当前真实 M2 block 结果是 REVIEW，必须先处理 M2 复核门控，不应在 M6 加绕过开关。
6. 数据库是新 Schema；如需迁移旧 Demo 数据，应写显式迁移和核对报告，不能直接复用旧库。
