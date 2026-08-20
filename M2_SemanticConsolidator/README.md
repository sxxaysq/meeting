# M2 Semantic Consolidation & Validation

> 接手请先读 **[HANDOFF.md](HANDOFF.md)**：环境、必踩的坑、标注的重大限制、
> 以及按优先级排好的待办。

把 M1 的高召回抽取结果做**保守的语义归一与归并**，并对结果做质量校验。

```text
M1 Extraction
    ↓
M2 Semantic Consolidation & Validation   ← 本模块
    ├────────→ M3 Knowledge Graph（以后）
    ↓
M6 Task Lifecycle Manager（以后）
```

M2 只做三件事：

1. **项目实体语义归一**——`红沙泉项目` / `红二矿项目` 是不是同一个真实项目；
2. **同一会议内部的 Item 语义归并**——修复 M1"拆多了"；
3. **质量校验**——Schema、可追溯性、over/under-merge、结构冲突 → `PASS/REVIEW/ERROR`。

M2 **不做**：访问历史任务库、判断 CREATE/UPDATE、跨会议任务生命周期
（那是 M6）、知识图谱（M3）、部门分发。

最高原则：**M1 负责高召回地"看见"，M2 负责保守地"归一和合并"。
宁可 M2 少合，也不能把两个不同业务目标错合成一个。**

---

## ⚠️ 关于人工标注的一个必须知道的结论

`items.annotation_v3.merge_group.json` **不能用作 M2 merge 的 gold。**

实测（`eval/gold_merge_groups.py` 顶部有完整数据）：

* 该文件里**没有 `merge_group` 字段**，合并信号隐含在 evidence 区间；
* 与未合并版 `items.annotation_v3.strict.json` 比对，214 个多成员合并组
  **214/214 在源顺序上连续**，说明合并规则是**源段落粒度**；
* **170/214 组内 `project` 不同**（把 `CRM二期项目`+`数据中台项目`+
  `煤矿复合灾害监测预警系统项目` 合成一条）；
* 逐条核对剩下 54 个 `project` 同质组，**同样全部是"同段落里的不同业务目标"**——
  比如 `红沙泉二矿项目` 那组 9 条 = 集控中心 / 数据中心 / 智能综合管控平台 /
  采场全景系统 / 火灾监测招标 / …

而需求第七节点名的反例正是这一条：

> 但下面这种不能合：同一个红沙泉二矿项目：数据中心建设 / 综合管控平台研发 / 火灾监测系统

**也就是说，这份标注里一个"同一业务目标被 M1 拆开"的正例都没有。**
照它优化 merge recall，等于直接把 M2 训成 over-merge。

所以评测口径是：

| 指标 | 用途 |
|---|---|
| `over_merge.hard_negative_violations` | **主指标**。合并了 gold 侧 project 明确不同的两条 = 一定错。目标 0 |
| `project_entity_pairs_m2` | **主指标**。按对算的实体归一质量，与 canonical 取名无关 |
| `coverage_m2` | **主指标**。不能低于 M1 baseline |
| `eval/dump_merges.py` 导出的合并清单 | **主指标**。逐条人工核对，这是唯一能算出真实 merge precision 的办法 |
| `merge_project_consistent` / `merge_full_annotation` | 仅参照，说明与标注的粒度差多少 |

标注**未被修改**。差异如实记录在这里和 HANDOFF.md。

---

## 架构

```text
                 M2 Common Core
                       │
         ┌─────────────┴─────────────┐
         │                           │
 block candidate strategy    generic candidate strategy
         │                           │
         └─────────────┬─────────────┘
                       ↓
             Project Entity Resolver
                       ↓
               Item Merge Judge          两两语义判定
                       ↓
              Cluster Validation         整簇复判，切断传递式错合
                       ↓
                Quality Gate             PASS / REVIEW / ERROR
```

**只有候选生成这一层按模式分叉**，后面全部共用。不维护两套 M2。

### 保守是怎么落到实现里的

| 环节 | 保守设计 |
|---|---|
| 候选召回 | 相似度/邻近性只用于召回，**绝不**直接决定合并 |
| 判定输入 | 召回信号（相似度、gap、同项目）**不写进给模型的 prompt**，避免诱导 |
| 两两判定 | 三值：`MERGE` / `KEEP_SEPARATE` / `UNCERTAIN`，`UNCERTAIN` 保持拆开 |
| 传递式错合 | MERGE 对聚簇后**整簇重新判**，可 `SPLIT_CLUSTER` 并给出明确分组 |
| 分组不合法 | 模型给的分组漏项/重复/越界 → 不猜，整簇转 REVIEW |
| 簇规模 | 超过 6 条即使模型说保留也强制 REVIEW |
| 项目序数 | `一矿`/`二矿`、`二期`/`三期`、`2025`/`2026` 两边都带且不同 → 确定性硬否决，不问模型 |
| 简称歧义 | 无序数简称同时匹配多个序数不同候选 → 强制 UNCERTAIN |
| 主表绕过 | 会议内判过"不是同一个"的名字，不许再经项目主表连到一起 |
| canonical | 必须原样取输入称谓之一，模型自造名字一律拒绝采信 |

### 合并后的字段规则（禁止凭空增加事实）

| 字段 | 规则 |
|---|---|
| `content` | 只由来源内容按原文顺序拼接、去完全重复。不改写、不概括 |
| `title` | 允许 LLM 生成，但要过 grounding 校验（2-gram ≥ 0.75），不过就退回来源标题 |
| `assignee` | 来源去重并集，不新增 |
| `department` / `delivery_group` | 一致取该值；明确冲突 → REVIEW |
| `work_section` | block 同上；generic 允许 null 与非 null 合并取非 null |
| `project` | 取 Project Entity Resolver 的 canonical |
| `item_type` | 多数决，平局按"像任务"优先级；冲突必写告警 |
| `evidence` | **程序计算**，LLM 永远不碰字符坐标 |

### evidence 的两种模式

```text
bounding_span    给了规范化文本时，取 [min_start, max_end] 的**字面切片**。
                 text 就是原文该区间，exact_match 由切片一致性得出，不是拼接产物。
                 来源不连续时照样落这个区间，但 evidence_contiguous=false 并告警。
primary_source   没给规范化文本时，原样沿用首条来源的 evidence，不做任何拼接。
```

两种模式下**全部来源 evidence 都保留进 `merge_trace[].source_evidence`**。
M1 的坐标系与人工标注逐字节对齐，M2 不改变它。

---

## 输出格式

`items[]` 与 M1 的九字段**完全兼容**（同一份定义，禁止 additionalProperties），
M2 自己的信息全部放顶层：

```json
{
  "source_mode": "block",
  "items": [ { "department": "...", "...": "...", "evidence": { } } ],
  "project_entities": [
    {
      "entity_id": "P0007",
      "canonical_name": "集团互联网收敛项目",
      "aliases": ["互联网收敛项目", "集团互联网收敛项目"],
      "source_names": ["互联网收敛项目"],
      "decisions": [ { "type": "intra_meeting", "decision": "SAME_ENTITY" } ]
    }
  ],
  "merge_trace": [
    {
      "item_index": 3,
      "source_indexes": [5, 6],
      "merged": true,
      "evidence_mode": "bounding_span",
      "evidence_contiguous": true,
      "source_evidence": [ ],
      "project_entity_id": "P0007",
      "title_source": "llm"
    }
  ],
  "validation": { "status": "REVIEW", "issues": [ ], "issue_counts": { } }
}
```

`items[]` 里禁止出现 `confidence` / `status` / `project_id` / `task_id` /
`merge_group` / `reasoning` / `CREATE` / `UPDATE`——与 M1 同一套禁令。
**LLM 的 reasoning 原文不进正式输出**，只进 `*.review.json`。

Schema：[`schemas/m2_output.schema.json`](schemas/m2_output.schema.json)。
`merge_trace` 必须覆盖**每一条**输出 Item，否则 Schema 直接失败——保证可逐条回溯。

---

## CLI

`--source-mode` **必填，不从内容猜**。猜错会让两种模式的假设互相污染。

```bash
python src/cli.py consolidate out_v2/2026-04-07.items.json --source-mode block --normalized-text /home/yty/m1_annotation/text/2026-04-07.body.txt -o out_m2/2026-04-07.m2.json --workers 8
```

只看候选召回、不调模型（排查用）：

```bash
python src/cli.py candidates out_v2/2026-04-07.items.json --source-mode block
```

只跑项目实体归一：

```bash
python src/cli.py projects out_v2/2026-04-07.items.json --source-mode block --catalog projects.sqlite -o entities.json
```

`consolidate` 会写出 `*.m2.json`（业务输出）、`*.report.json`（统计与诊断）、
`*.review.json`（需人工判断的条目及其来源）。质量门 `ERROR` 时默认**退出码 2
且不写业务结果**（`--allow-invalid` 可强制），但 report 永远落盘。

---

## 配置

复制 `.env.example` 为 `.env`。默认指向 `192.168.30.215:8000` 的 vLLM
（`Qwen/Qwen3.6-35B-A3B`，无需 Key）。

两个坑与 M1 相同：**思考型模型必须关思考**（`LLM_ENABLE_THINKING=false`）、
**Ollama 的 OpenAI 兼容层把上下文硬截断到 4096**（换 `LLM_TRANSPORT=ollama`）。

⚠️ 模型 id 是 `Qwen/Qwen3.6-35B-A3B`，**不是** `Qwen/Qwen3.5-36B-A3B`（那个 404）。

---

## 测试

```bash
cd M2_SemanticConsolidator && python -m pytest tests -q
```

74 项，全部用假模型替身，不依赖在线模型。覆盖：

* 序数硬否决与简称歧义守卫（`红沙泉一矿` vs `红沙泉二矿` vs `红沙泉`）；
* block/generic 候选策略的差异（generic 的 `work_section=null` 不得算错误）；
* 传递式错合的切断、非法分组降级、簇规模硬上限；
* 合并字段规则、grounding 退回、evidence 不伪造 `exact_match`；
* **需求第七节点名的反例**：同项目不同子系统必须保持分开；
* Schema 严格性与 `merge_trace` 全覆盖；
* `llm_client.py` 与 M1 副本不漂移。

---

## 评测

```bash
python eval/batch_regression.py --m1-dir /home/yty/m1x/M1_Extraction/out_v2 --source-mode block --annotation /home/yty/标注/items.annotation_v3.merge_group.json --strict-annotation /home/yty/标注/items.annotation_v3.strict.json --text-dir /home/yty/m1_annotation/text --out-dir out_m2_block --workers 8 --persist-catalog
```

导出全部合并供人工核对：

```bash
python eval/dump_merges.py --m2-dir out_m2_block -o out_m2_block/merges.md
```

**方差提醒**：Qwen/vLLM 即使 `temperature=0`，并发下仍有明显方差
（M1 实测同一份文档两次 coverage 能在 0.85–0.99 之间跳）。
比较提示词改动时用 `--workers 1` 或 `--repeat N` 看中位数，
不要拿单次结果当结论。每次的原始结果都会落盘。

### 实测结果（10 份全量，各跑 3 次，中位数）

| | M1 条目 | M2 条目 | 合并组 | **hard-neg 违例** | coverage M1→M2 | project 一致率 M1→M2 | Schema |
|---|---|---|---|---|---|---|---|
| block | 1444 | 1436 | 8 | **0** | 0.9495 → **0.9517** | 0.645 → **0.710** | 0 |
| generic | 1355 | 1340 | 15 | **0** | 0.9261 → **0.9305** | 0.541 → **0.579** | 0 |

两种模式**都没有损失召回**（coverage 反而略升，因为合并把碎片拼回完整区间）。

逐条人工核对实际合并：block 8 组 = 6 正确 / 2 边界 / **0 明显错误**；
generic 15 组 = 8 正确 / 6 边界 / **1 明显错误**。
完整案例表在 [HANDOFF.md](HANDOFF.md) 第 4 节。

---

## 目录

```text
M2_SemanticConsolidator/
├── README.md / HANDOFF.md / .env.example / requirements.txt
├── src/
│   ├── models.py                      数据结构与三值枚举
│   ├── text_utils.py                  确定性文本工具（序数硬否决在这里）
│   ├── project_normalizer.py          projects / project_aliases 两表
│   ├── project_candidate_retriever.py 项目候选召回
│   ├── project_entity_judge.py        实体归一：召回→LLM→alias 落库
│   ├── block_candidate_strategy.py    block 候选（强结构约束）
│   ├── generic_candidate_strategy.py  generic 候选（弱结构，靠语义）
│   ├── merge_candidate_retriever.py   策略分发 + 候选数上限
│   ├── merge_judge.py                 两两语义判定
│   ├── cluster_validator.py           聚簇 + 整簇复判
│   ├── merger.py                      合并字段生成 + evidence 规则
│   ├── provenance.py                  追溯、REVIEW 样本、SFT 数据
│   ├── quality_gate.py                七类检查
│   ├── schema_validator.py            严格 Schema（内置 + jsonschema 双路）
│   ├── pipeline.py / cli.py / llm_client.py
├── prompts/  project_entity_judge.md / item_merge_judge.md
│             cluster_validator.md / quality_check.md（可选）
├── schemas/  m2_output.schema.json
├── eval/     gold_merge_groups.py / evaluate_m2.py
│             batch_regression.py / dump_merges.py
└── tests/    74 项
```

## 与旧模块的关系

| 路径 | 处置 |
|---|---|
| `M2_TaskClassifier/` | **保留未改**。`src/m1_task_gate.py` 按 `confidence`/`status` 做准入，而 M1 新输出不再有这两个字段，不能作为新 M2 主流程。本模块是新建目录，不为兼容它而污染 M1 Schema |
| `M1_5_ProjectNormalizer/` | **保留未改**。它的 projects/aliases 主表思路被本模块的 `project_normalizer.py` 继承并重新适配（去掉了浮点 `confidence`，加了序数硬否决与主表绕过防护） |
| `M1_Preprocess/` | 保留未改，已废弃 |

本模块没有删除或覆盖任何既有文件。**本轮不涉及 Dify**，`dify/` 未改动。
