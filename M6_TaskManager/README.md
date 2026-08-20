# M6 Task Lifecycle Manager

把一份 **M2 `validation.status=PASS` 输出**中的 Meeting Item 安全映射到历史 Task 生命周期。

```text
M2 PASS Item
  ↓ Stable source_document_id + source_item_id
  ↓ Candidate Retriever（active + 少量 closed，Top 3–5）
  ↓ LLM Lifecycle Judge（只看候选）
  ↓ Deterministic Command Validator
  ↓ State Machine
  ↓ Transactional Executor
  ↓ tasks / task_events / source_links / audit / dispatch_queue
```

M6 不解析 PDF、不做项目 alias 归一、不合并同会议 Item、不接 M3、不使用 Dify。任务身份由
持续业务目标决定；项目名、负责人或相似度只用于候选召回，不能直接决定 UPDATE。

## 动作

支持 `CREATE`、`PROGRESS_UPDATE`、`MODIFY`、`COMPLETE`、`CANCEL`、`REOPEN`、
`TRANSFER`、`REVIEW`。`NON_TASK_ITEM` 由程序直接 `SKIP`，即使 `project` 非空也不会调用
生命周期模型。

`PROGRESS_UPDATE` 只追加 `task_events`，不覆盖 Task 的稳定描述。其余生命周期变化在同一
事务内完成 Task 更新、Event、Source Link、Audit 和 Department Dispatch。

## 安全边界

- LLM 只能返回提供给它的候选 task_id；
- UPDATE 类动作必须通过候选成员、目标存在、项目一致、版本和状态迁移校验；
- 普通部门冲突强制 REVIEW；TRANSFER 必须有逐字原文和明确移交措辞；
- MODIFY 值只能逐字来自当前 M2 Item；
- LLM 没有 SQL 或数据库执行权；
- `(source_document_id, source_item_id)` 是唯一幂等键，重复运行不再调用 LLM；
- 任一数据库步骤失败会回滚整笔事务；
- M2 `REVIEW/ERROR` 默认拒绝进入 M6。

人工已明确确认当前 M2 `REVIEW` 结果可作为预览基线时，可显式传入
`--accept-review-input`。该开关只接受 `REVIEW`，仍拒绝 `ERROR`，并在运行结果及完整
provenance 中保留原始 validation、issues 和“人工接受”标记，不能用于无人值守生产写库。

## 安装与运行

```bash
cd M6_TaskManager
python -m pip install -r requirements.txt
python -m src.cli init-db \
  --database data/m6.db \
  --departments db/departments.example.json
python -m src.cli import-history examples/history.sample.json \
  --database data/m6.db
```

模型配置：

```bash
export LLM_BASE_URL=http://192.168.30.215:8000/v1
export LLM_MODEL=Qwen/Qwen3.6-35B-A3B
export LLM_ENABLE_THINKING=false
export LLM_TIMEOUT=300
```

处理 M2 PASS 输出：

```bash
python -m src.cli process examples/m2_pass.sample.json \
  --database data/m6.db \
  --output out/m6_result.json \
  --document-id MEETING-2026-04-14
```

人工接受 M2 REVIEW 后的预览运行：

```bash
python -m src.cli process current.m2.json \
  --database data/m6-preview.db \
  --output out/m6_preview_result.json \
  --document-id MEETING-2026-04-07 \
  --accept-review-input
```

再次执行相同命令会返回 `DUPLICATE`，不再做候选检索或模型调用。

## 数据库

`db/schema.sql` 包含：

- `departments`：Department Master 与路由；
- `tasks`：稳定业务身份和当前状态；
- `task_events`：每次会议变化；
- `task_source_links`：Evidence 与完整 M2 provenance；
- `task_audit`：before/after、理由与来源；
- `dispatch_queue`：按部门分发的事件；
- `lifecycle_reviews`：需人工判断的 Item 与候选；
- `processing_records`：幂等执行结果。

## Python service

核心组合方式见 `src/cli.py`：`TaskLifecycleService` 依赖 Repository、Retriever、Judge、
Validator 和 Executor，方便在 API 或批处理服务中复用。LLM 的隐藏 reasoning 不写入业务
结果或审计；只保留模型名、token 使用量和简短业务 reason。

## 测试与评估

```bash
python -m pytest tests -q
python eval/evaluate_lifecycle.py --mode old \
  --output results/old_baseline.json
python eval/evaluate_lifecycle.py --mode pipeline \
  --output results/qwen_pipeline.json
```

当前目标服务器测试：32 passed。28 条架构验证 Gold 上，旧 matcher 的 decision accuracy
为 0.1786，新完整链路为 0.9643；新链路 wrong-target、false UPDATE、false CREATE、幂等
违规和事务失败均为 0。详见 `results/EVALUATION.md`。

Gold 只有 28 条，足够验证架构但不足以宣称生产准确率。下一阶段应从 2026-04-07 到
2026-07-13 连续会议中人工标注 100–300 条真实跨周 Task identity，再重复评估。

## 目录

```text
src/       M2 入口、召回、Judge、Validator、状态机、Repository、Executor、CLI
prompts/   生命周期判定硬边界
schemas/   Lifecycle Decision / TaskCommand JSON Schema
db/        SQLite Schema、migration 标记、Department Master 示例
eval/      Gold 构建、旧 baseline、新链路评估、重复回归
gold/      28 条跨周架构验证集
tests/     单元、事务、幂等、Prompt/Schema 与风险指标测试
results/   可复现评估结果
docs/      旧 M6 实际调用链与处置
```
