# M6 V1 评估结果

评估日期：2026-08-18。目标端点：`http://192.168.30.215:8000/v1`，模型：
`Qwen/Qwen3.6-35B-A3B`，thinking 关闭。

## 数据口径

`gold/lifecycle_gold_v1.json` 有 28 条人工构造的跨周架构验证样本，覆盖 CREATE 8、
PROGRESS_UPDATE 7、MODIFY 2、COMPLETE 2、CANCEL 2、REOPEN 2、TRANSFER 1、
REVIEW 4。包含同项目不同任务、同负责人不同任务、同任务不同表述、子步骤完成、关闭后
重启、部门冲突、多目标 Item 等 hard cases。

这不是从单次会议标注自动推导的，也不是生产集准确率。现有
`items.annotation_v3.merge_group.json` 没有跨会议 Task identity 标签，不能冒充 M6 Gold。

## 对照结果

| 指标 | 旧 Dice matcher | 新 M6 完整链路 |
|---|---:|---:|
| decision accuracy | 0.1786 | **0.9643** |
| target_task_id accuracy | 0.1875 | **0.9375** |
| wrong-target rate | 0 | **0** |
| false-update rate | 0.0357 | **0** |
| false-create rate | 0.5714 | **0** |
| REVIEW rate | 0.1071 | 0.1786 |
| department routing accuracy | 0 | **1.0** |
| idempotency violations | 未测 | **0** |
| transaction failures | 未测 | **0** |

旧 baseline 的 false UPDATE 是 `CREATE_HIGH_SIMILARITY_DIFFERENT_PHASE`：一期与二期字面
高度相似，但属于可独立验收的两个业务目标。新 M6 没有 wrong-target、false UPDATE 或
false CREATE；唯一非 Gold 动作是把 `PROGRESS_RECENT_EVENT_LINK` 保守送 REVIEW。

原始结果：`old_baseline.json`、`qwen_pipeline_run2.json`。完整链路评估每条样本都实际执行
Candidate Retrieval → LLM Judge → Validator → SQLite Executor，并重复处理同一来源验证
幂等。

## Embedding baseline

实测 `/v1/models` 只返回 `Qwen/Qwen3.6-35B-A3B` 生成模型，没有 embedding 模型。
因此本轮未临时制造 embedding 基础设施，也未伪造纯 Embedding Top-1 结果。
