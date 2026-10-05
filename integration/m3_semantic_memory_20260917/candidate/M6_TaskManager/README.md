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


## 2026-09-15 当前 M6 修订：代码、全量复测及 Demo 已部署

是否仍在使用此版本，以 integration/m6_revision_20260915/deployment.json 的 status 为准。
M6 原目录源码与 HTTP 接入层已更新；前端功能没有修改。没有提交、推送或重置 Git。

- 固定代码版本摘要：88bd939ab7e531cd0f45faf85b6068dc5d907c3a59aaeea437cbfb75c33db474。
- 全量输入：15 场、2168 条冻结 M2 子事项；baseline 与 candidate_verified 独立代码和空数据库，按日期顺序执行。
- 基线：CREATE 928 / PROGRESS_UPDATE 550 / MODIFY 102 / COMPLETE 13 / REVIEW 572 / SKIP 3。
- 修改版：CREATE 1136 / PROGRESS_UPDATE 732 / MODIFY 0 / COMPLETE 0 / REVIEW 116 / SKIP 184。
- 两边模型调用失败 0；修改版拒绝 0、技术失败 0；幂等重放通过；所有队列仅 test://，未实际派发。
- 复核涉及子事项均值 38.13→7.73，仍未达到每场3–5条理想值；剩余36条缺责任部门、8条M2别名冲突、72条多目标/目标未唯一。
- 60 项核心回归、2 项 HTTP 回归通过。候选来自当时已建立任务和历史，未使用未来会议任务、旧试验库或生产任务。
- M3仅有实体图查询/HashEmbedder占位，当前 /v1/embeddings 返回404；未接真实语义向量索引，采用词面、明确层级和历史扩展检索。
- 模型输出改为动作、target_index、fields、scope、reason、evidence。ID/版本/路由和字段值由程序获取；CLI 请显式使用 --model qwen3.8-27b。
- 删除项目级复核扩散；协作进展保留原责任部门。字段、来源、目标范围、状态机、事务、版本与幂等检查保留。
- 技术失败可重试并返回HTTP503；非法操作HTTP422；技术失败结果文件不再永久缓存为完成。

当前 Demo： http://127.0.0.1:18098/ ，数据库 M6_TaskManager_Demo/data/m6_revision_20260915.sqlite。
原 Demo 数据库及原代码备份保留在 deployment.json / deployment_backup 中，不删除历史结果。
回滚入口：/home/yty-s/venvs/m6-service/bin/python integration/m6_revision_20260915/deploy_verified.py --rollback。
回滚检查后续用户修改，恢复旧代码和旧数据指向，保留本次全部结果。

完整报告：integration/m6_revision_20260915/M6_REVISION_REPORT.md；规则审查：RULE_AUDIT.md。
数据：runs/baseline、runs/candidate_verified；源码冻结哈希：frozen_manifest.json、candidate_verified_manifest.json。
试跑 candidate/candidate_v2/candidate_final/candidate_release 已停止且保留，不用于最终比较。
所有全量和自动发布任务均已结束；本修订没有遗留会覆盖 Demo 的后台发布进程。

验证边界：程序不变量不等于业务语义准确率，没有独立人工标注；不称重复新建已完全消除。
年度报告编制与报送等新建样例仍待业务核验；MODIFY/COMPLETE/TRANSFER 在本批修改版未执行，只有回归边界验证。
