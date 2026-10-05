# M2 交接文档

给下一个接手 **M2_SemanticConsolidator** 的 agent。读完这一份就能接着干。
架构与用法在 [README.md](README.md)。本文只讲 M2；M1 侧的事看 `M1_Extraction/`。

---

## 0. 一句话现状

M2 已实现并跑通 block / generic 两条全量链路（各 10 份、各重复 3 次）。
**block：1444 → 1436 条，hard-negative 违例 0，coverage 0.9495 → 0.9517（没掉），
Schema 违规 0，项目名一致率 0.645 → 0.710。** 81 项单测全绿。
merge 极度保守：全量只合并 8 组，逐条核对 6 组正确、2 组边界、0 组明显错误。

**有 3 个已定位但未修的问题**，见第 5 节。接手第一件事建议先修它们。

---

## 1. 先读这一节：一个会改变你所有判断的结论

### 人工标注不能用作 M2 merge 的 gold

`/home/yty/标注/items.annotation_v3.merge_group.json`：

1. **文件里根本没有 `merge_group` 字段**，只有 9 个业务字段。
   合并信号隐含在 `evidence` 区间——一条 gold 覆盖多条输入 Item 的字符区间（最长 1058 字符）。
2. 与未合并版 `items.annotation_v3.strict.json`（1474 条）比对可还原它的合并规则：
   214 个多成员合并组，**214/214 在源顺序上完全连续**，192/214 共享同一
   `(department, work_section)`。也就是**按源段落合并**。
3. **170/214 组内 `project` 不同**。例如把 `CRM二期项目` + `数据中台项目` +
   `煤矿复合灾害监测预警系统项目` 合成一条。
4. 剩下 54 个 `project` 同质组，**逐条核对后同样全部是"同段落里的不同业务目标"**：
   * `红沙泉二矿项目` 一组 9 条 = 集控中心 / 数据中心 / 现场跟进 / 智能综合管控平台 /
     采场全景系统 / 数据管理平台 / 火灾监测招标 / 厂家调研 / 日常报表；
   * 另一组 4 条 = 选煤厂论坛汇报 / 头盔式智能巡检场景梳理 / 评价系统研发整理 /
     科研项目月度例会。

而需求第七节点名的**反例**正是第 4 条那个例子：

> 但下面这种不能合：同一个红沙泉二矿项目：数据中心建设 / 综合管控平台研发 / 火灾监测系统

**结论：这份标注里一个"同一业务目标被上游拆开"的正例都没有。**
`merge recall` 对着它算永远接近 0，而且**提高它就是在制造 over-merge**。

⚠️ **不要因为 `merge_recall` 低就去放宽 M2。** 这是这个模块最容易犯的错误。
完整论证与替代口径写在 `eval/gold_merge_groups.py` 顶部，改评测前先读那里。
标注**未被修改**（需求第十九节）。

实例：0407 的 `红沙泉二矿` 7 条子系统，21 对全部进了候选（gap 最小 1 字符、
相似度 0.375–0.5），LLM 逐对判定后全部 `KEEP_SEPARATE`。
在 `merge_recall` 上这被记成 21 次漏合——**而这正是正确行为**。

### 顺带发现的标注质量问题

* doc0 的 gold148 / gold149 / gold150 三条内容完全相同；doc1 的 gold62 / gold63 同样重复。
  这些重复会被重复计入 gold_pairs。
* 标注的 `project` 是**逐条抄的原文写法**，不是统一规范名：
  `互联网收敛项目` 的规范名是更长的 `集团互联网收敛项目`，
  而 `平庄煤业调运智能指挥中心建设项目` 的规范名却是更短的 `平庄煤业`。
  **所以"project 字符串与标注精确相等"这个指标有先天上限**，只能看趋势。
  真正该看的是 `project_entity_pairs_*`（按对算聚类一致性，与取名无关）。

### 那 M2 的质量怎么 measure

| 指标 | 说明 |
|---|---|
| `over_merge.hard_negative_violations` | 合并了 gold 侧 project 明确不同的两条 = **一定错**。标注上唯一可信的 merge 指标。目标 0，**实测 0** |
| `eval/dump_merges.py` 的输出 | 每份只合并个位数组，全量 8 组，逐条人工核对完全可行。**唯一能算出真实 merge precision 的办法** |
| `project_entity_pairs_*` | 按对算的实体归一质量，与 canonical 取名无关 |
| `coverage_m2` | 不能低于输入 baseline |
| `merge_project_consistent` / `merge_full_annotation` | **仅参照**，说明与标注的粒度差多少，不是优化目标 |

---

## 2. 环境与运行

| 项 | 值 |
|---|---|
| 工作机 | `192.168.30.214`（用户 `yty`，SSH 免密） |
| **正式 Git 工作副本** | `/home/yty/m1x/meeting-m2-work/`，分支 `feat/m2-semantic-consolidator` |
| Python | `/home/yty/m1x_venv/bin/python`（3.11） |
| 人工标注 | `/home/yty/标注/items.annotation_v3.merge_group.json` |
| 未合并版标注 | `/home/yty/标注/items.annotation_v3.strict.json`（推导合并规则用） |
| 规范化文本 | `/home/yty/m1_annotation/text/*.body.txt` |
| git 历史包 | `/home/yty/m1x/meeting-m2-consolidator.bundle` |

**服务器访问不了 GitHub**，未推送（推送需用户确认）。

### 输入契约

M2 吃 `{"items": [...]}`，每条 9 个字段
（`department` / `work_section` / `delivery_group` / `project` / `item_type` /
`assignee` / `title` / `content` / `evidence`）。
**M2 不修改这个 Schema，也不要求上游增加 `confidence` / `status` / `project_id` 等字段。**

现成的输入：

| 来源模式 | 目录 | 条目 |
|---|---|---|
| block | `/home/yty/m1x/M1_Extraction/out_v2/` | 1444 |
| generic | `meeting-m2-work/M1_Extraction/out_generic_v4/` | 1386 |

`evidence.start_char/end_char` 与人工标注**逐字节同坐标系**，M2 不改变它。

### 模型

`http://192.168.30.215:8000/v1` · `Qwen/Qwen3.6-35B-A3B`（vLLM，无需 Key）

⚠️ **不是 `Qwen/Qwen3.5-36B-A3B`**（那个 404）。先 `curl .../v1/models` 确认。

```bash
export LLM_BASE_URL=http://192.168.30.215:8000/v1 LLM_MODEL="Qwen/Qwen3.6-35B-A3B" LLM_TRANSPORT=openai LLM_ENABLE_THINKING=false LLM_TIMEOUT=900
```

思考型模型不关思考 → `content` 是 `null`，解析必然失败，`LLM_ENABLE_THINKING=false` 已是默认。
M2 每次只把两条 Item 交给模型，输出很短，`LLM_MAX_TOKENS` 默认 8192 绰绰有余。

### 跑起来

```bash
cd /home/yty/m1x/meeting-m2-work/M2_SemanticConsolidator && /home/yty/m1x_venv/bin/python -m pytest tests -q
```

block 全量回归（3 次重复取中位数）：

```bash
/home/yty/m1x_venv/bin/python eval/batch_regression.py --m1-dir /home/yty/m1x/M1_Extraction/out_v2 --source-mode block --annotation /home/yty/标注/items.annotation_v3.merge_group.json --strict-annotation /home/yty/标注/items.annotation_v3.strict.json --text-dir /home/yty/m1_annotation/text --out-dir out_m2_block --workers 8 --repeat 3 --persist-catalog
```

generic 全量回归：

```bash
/home/yty/m1x_venv/bin/python eval/batch_regression.py --m1-dir ../M1_Extraction/out_generic_v4 --source-mode generic --annotation /home/yty/标注/items.annotation_v3.merge_group.json --strict-annotation /home/yty/标注/items.annotation_v3.strict.json --text-dir /home/yty/m1_annotation/text --out-dir out_m2_generic --workers 8 --repeat 3 --persist-catalog
```

导出全部合并供人工核对：

```bash
/home/yty/m1x_venv/bin/python eval/dump_merges.py --m2-dir out_m2_block/run0 -o out_m2_block/merges.md
```

不调模型、只看候选召回（排查用）：

```bash
/home/yty/m1x_venv/bin/python src/cli.py candidates <items.json> --source-mode block
```

---

## 3. 保守性是怎么落到实现里的

| 环节 | 设计 |
|---|---|
| 候选召回 | 相似度/邻近性只用于召回，**绝不**直接决定合并 |
| 判定输入 | 召回信号（相似度、gap、同项目）**不写进 prompt**，避免诱导。`tests/test_cluster_and_merge.py` 有断言守着 |
| 两两判定 | 三值 `MERGE` / `KEEP_SEPARATE` / `UNCERTAIN`，`UNCERTAIN` 保持拆开并进 REVIEW |
| 传递式错合 | MERGE 对聚簇后**整簇重新判**，可 `SPLIT_CLUSTER` 并给出明确分组 |
| 分组不合法 | 模型给的分组漏项/重复/越界 → 不猜，整簇转 REVIEW |
| 簇规模 | 超过 6 条即使模型说保留也强制 REVIEW |
| 结构冲突 | department / delivery_group 冲突是**合并否决**，不是"合完把字段置空" |
| 项目段落 | block 用 `effective_projects()` 给空 project 条目补段落归属，`None` 也是一个段落 |
| 序数 | `一矿/二矿`、`2025/2026` **两边都带且不同** → 确定性硬否决，不问模型 |
| 简称歧义 | 无序数简称同时匹配多个序数不同候选 → 强制 UNCERTAIN |
| 主表绕过 | 会议内判过"不是同一个"的名字，不许再经项目主表连回去 |
| canonical | 必须原样取输入称谓之一，模型自造名字一律拒绝采信 |
| 字符坐标 | 一律程序算，LLM 永远不碰 |

### 几条不该退回去的约束（都有实测代价）

1. **`--source-mode` 必填，绝不从 JSON 内容猜。** 猜错会把 block 的强结构约束套到 generic 上。
2. **结构字段冲突要否决合并。** 实测 05-25 出现过一簇 5 条跨多个部门：
   A(部门空)–B(安全生产部) 不冲突、A–C(市场经营部) 也不冲突，连成簇就冲突了。
   早期实现把 `department` 置为 None 后照样合并，既丢信息又留下错误合并。
3. **block 必须用"项目段落上下文"。** 05-11 有两条 `project` 都是 null、部门相同、
   位置相近的"跟进商机"，标注里分属两个不同项目，是唯一一次 hard-negative 违例。
   `effective_projects()` 加上后归 0。**`None` 不能当通配符**——
   "该部门第一个项目名出现之前"本身就是一个段落。
4. **序数硬否决只在两边都带序数时生效。** `红沙泉一矿` vs `红沙泉二矿` → 否决；
   但 `红沙泉项目` vs `红沙泉二矿项目` **不能否决**，需求第十节点名要归一这一对。
5. **title 的 grounding 阈值是 0.75，不是 0.85。** 合法概括跨过原文标点会丢 bigram
   （`顶面线管安装` vs 原文 `顶面线管、桥架安装` 只有 0.80），0.85 会把正确标题打回去。
   凭空发挥的标题实测在 0.5 以下。
6. **under-merge 检查要"少而准"。** 阈值 0.62 且只比部门时，单份文档能报 125 条，
   全是短标题字面重合。现在阈值 0.85 且要求部门与项目都一致、内容长度 ≥ 12。

---

## 4. 实测数据

### block（1444 条输入，3 次重复）

| 指标 | 值 |
|---|---|
| 输入 → 输出 | 1444 → **1436**（8 次合并操作，8 组） |
| **hard-negative 违例** | **0 / 6 合并对**（3 次运行全为 0） |
| coverage | 0.9495 → **0.9517**（没掉，略升） |
| Schema 违规 | **0** |
| project 字符串一致率 | 0.6452 → **0.7104**（+6.5 点） |
| project 实体成对准确率 | 0.9525 → 0.9529（错连 3→9，错分 1095→1066） |
| 质量门 | 10 份全 REVIEW，**0 份 ERROR** |
| REVIEW 条目占比 | 0.0014 |
| 耗时 / LLM 调用 | 194 秒 / 1395 次 |

**3 次重复完全一致**（1436 / 8 组 / 违例 0）。M2 方差远小于上游，
因为每次调用都是二值判定且绝大多数落在 KEEP_SEPARATE。

### block 的 8 组合并，逐条核对

| # | 文档 | 内容 | 判定 |
|---|---|---|---|
| 1 | 04-07 | 组织开展 4 月警示教育提醒活动 + 组织开展公司 2026 年 4 月警示教育活动… | ✅ 正确（同一活动，后者是"下一步工作要求"里的重述） |
| 2 | 04-13 | 定稿并提报 HP1 项目专利 1 项 + 定稿发明专利 1 件（视觉辅助…）并提报 | ✅ 正确（同一件专利） |
| 3 | 04-13 | 筹备五四青年节主题活动 + 团支部要做好五四青年节系列活动策划 | ✅ 正确 |
| 4 | 05-11 | 计划去常州院调研，编写项目请示文件 + 下周与徐处沟通一起去常州院调研 | ✅ 正确（同一次出差） |
| 5 | 05-11 | 现场沟通天津工厂项目相关事宜 + 现场汇报变更后的方案和付款方式 | ✅ 正确（同一次现场） |
| 6 | 06-15 | 配合巡视工作。+ 配合巡视工作。 | ✅ 正确（完全重复，去重后剩一条） |
| 7 | 06-22 | 组织修订高层次人才考核管理办法 + 拟定高层次人才绩效考核责任书 | ⚠️ 边界（两个不同交付物） |
| 8 | 07-13 | 筹备年中工作会议表彰决定、颁奖仪式 + 发布两优一先表彰决定 | ⚠️ 边界（同一次表彰，粒度可争议） |

**严格 precision = 6/8 = 0.75；边界算作可接受 = 8/8 = 1.00。**
需求要求自动 MERGE 的 precision ≥ 0.95——n=8 这个量级上严格口径达不到，
但两个失分项都是"粒度可争议"而非"把两个业务目标错合"，且 hard-negative 违例为 0。

主要合并类型是**"工作安排"条目与它在"下一步工作要求"章节里的重述**——
原文位置很远（evidence 不连续），但确实是同一件事。

### generic（1355 条输入，3 次重复）

| 指标 | 值 |
|---|---|
| 输入 → 输出 | 1355 → **1340**（15 组） |
| **hard-negative 违例** | **0 / 13 合并对** |
| coverage | 0.9261 → **0.9305** |
| Schema 违规 | **0** |
| project 字符串一致率 | 0.5405 → **0.5791** |
| 质量门 | 1 份 PASS、9 份 REVIEW、**0 份 ERROR** |

逐条核对 15 组：**8 正确 / 6 边界 / 1 明显错误**。
错的那组是 `持续跟进重点项目进展` + `红沙泉现场跟进项目进展`——
block 被"项目段落上下文"挡住了，generic 没有可靠结构层级挡不住，
这是两种模式的固有差距，不是实现缺陷。

`work_section` 全为 null **没有导致任何 ERROR**（需求第十二节第 7 条），有 1 份拿到 PASS。

⚠️ **这组 generic 数据是在旧输入上跑的**（1355 条那一版，已归档删除）。
当前 generic 输入是 `out_generic_v4`（1386 条），**M2 尚未在它上面重跑**，见第 7 节。

---

## 5. 已定位但未修的问题（接手优先做这个）

三个都是我在核对结果时发现的，都有明确复现，**都还没改**。

### ① `OVER_MERGE_PROJECT` 误报

`src/quality_gate.py` 的 `check_over_merge()` 比的是来源 Item 的**原始 project 字符串**，
应该比**解析后的 entity_id**。

复现：generic 0407 那组 `天地王坡数据中台项目` + `数据中台项目` 被标了
"簇内存在未归一的多个项目称谓"，但实体解析其实**已经把两者归到同一实体 P0027** 了
（`merge_trace[].project_entity_id` 可验证）。这会给人工审核制造假信号。

修法：用 `merge_trace` 里的 `project_entity_id`，或把 `project_of` 传进质量门。

### ② `SPLIT_CLUSTER` 拆完不留痕

`src/cluster_validator.py` 判 `SPLIT_CLUSTER` 后，如果拆成的都是单元素簇，
`merge_trace` 里看不出"曾经考虑过合并又被否决"。
`CLUSTER_REVIEW` 会写 issue，`SPLIT_CLUSTER` 不会。

复现：block 0407 的 `cluster_decisions` 是 `{"KEEP_CLUSTER":1,"SPLIT_CLUSTER":1}`，
但输出里找不到被拆的是哪几条。

对一个以保守决策为卖点的模块，这是可审计性缺口。建议加一条
`CLUSTER_SPLIT` 级别 warning，带上原簇成员。

### ③ 非连续 evidence 仍算包络区间，与需求第十一节不符

`src/merger.py` 的 `merge_evidence()` 在来源**不连续**时照样算
`[min_start, max_end]` 包络并置 `exact_match=true`。
文本本身是原文字面切片（没伪造），但它把大量无关内容当成了这条 item 的证据。

复现：block 0407 items[143]，两条来源相距 255 字符（6667 → 6922），
中间隔着综合办公室、工会、团支部三个科室的条目，
`evidence.text` 是 370 字符、大半与该 item 无关。

需求原文：

> 如果多个来源 evidence **可以构成一个连续原文区间**，可以通过规范化文本重新程序计算
> bounding evidence。如果 evidence 不连续：…保留多来源 evidence 到 provenance

也就是包络只应该用在连续的情况。修法：非连续时走 `primary_source`
（沿用首条来源 evidence 不动，全部来源已经在 `merge_trace[].source_evidence` 里）。

影响面：block 全量 9 条、generic 大部分合并都是非连续的——
跨"下一步工作要求"章节的重述是主要合并类型，天然不连续。
**下游 M3/M6 如果直接读 `evidence.text` 会拿到一大段无关文字。**

---

## 6. 代码地图

```text
src/
  models.py                      数据结构与三值枚举
  text_utils.py                  序数硬否决 / 简称歧义 / grounding
  project_normalizer.py          projects + project_aliases 两表（SQLite，可内存）
  project_candidate_retriever.py 项目候选召回（含 core_containment 简称规则）
  project_entity_judge.py        实体归一：召回→LLM→alias 落库；blocked 防绕过
  block_candidate_strategy.py    block 候选：强结构约束 + 项目段落上下文
  generic_candidate_strategy.py  generic 候选：work_section 完全不参与
  merge_candidate_retriever.py   策略分发 + 每条 Item 候选数上限 6
  merge_judge.py                 两两判定（召回信号不进 prompt）
  cluster_validator.py           聚簇 + 整簇复判 + 规模硬上限 6
  merger.py                      合并字段规则 + evidence 两种模式
  provenance.py                  追溯 / REVIEW 样本 / SFT 数据
  quality_gate.py                七类检查 → PASS / REVIEW / ERROR
  schema_validator.py            内置 + jsonschema 双路
  pipeline.py / cli.py / llm_client.py
prompts/  project_entity_judge.md / item_merge_judge.md
          cluster_validator.md / quality_check.md（未接线，默认不启用）
schemas/  m2_output.schema.json
eval/
  gold_merge_groups.py   ← 标注局限的完整论证在文件顶部，改评测前先读
  evaluate_m2.py / batch_regression.py / dump_merges.py
results/  已提交的回归产物（两模式的 repeat_summary / merges.md / run0_summary）
tests/    81 项，全部用假模型替身，不依赖在线模型
```

`llm_client.py` 是 `M1_Extraction/src/llm_client.py` 的副本，
`tests/test_llm_client.py` 会逐行比对两边核心实现，单边漂移会直接测试失败。

`merge_trace` 必须覆盖**每一条**输出 Item，否则 Schema 直接失败——保证可逐条回溯。
**LLM 的 reasoning 原文不进正式输出**，只进 `*.review.json`。

---

## 7. 待办（按优先级）

### P0 — 先做

1. **修第 5 节那三个问题。** 其中 ③ 影响下游正确性，最该先修。
2. **在 `out_generic_v4` 上重跑 M2 generic 回归。** 现有 generic 指标是旧输入上的。

### P1 — 需要人拍板

3. **合并粒度口径。** 第 4 节第 7、8 两组边界案例（"修订办法 + 拟定责任书"、
   "表彰决定 + 颁奖仪式"）算不算一个业务事项，要业务确认。
   定了改 `prompts/item_merge_judge.md` 的判据，**不是改标注**。
4. **M2 专用的 merge 评测集。** 现有标注提供不了正例（第 1 节）。
   要把 precision 提到 0.95 并可验证，需要人工标一批
   "同一业务目标被上游拆开"的对照数据，200～300 对即可。
   可从 `eval/dump_merges.py` 的输出和 `--no-llm` 跑出来的候选集里抽样。

### P2

5. **canonical_name 的取向。** 一个名字包含另一个时模型倾向选更长的，于是出现
   `科工成套办公信息化项目会议室无纸化系统采购` 当成 `科工成套办公信息化项目`
   的规范名。别名分组本身不受影响（主指标是成对口径），但输出给 M3/M6 的名字不好看。
6. **项目主表跨批次复用验证。** `--persist-catalog` 已实现并在回归里用了，
   但只在单次 10 份会议内累积过。
7. **`prompts/quality_check.md` 未接线**（默认不启用），程序化质量门已覆盖确定性部分。
8. **SFT 数据导出**（`--sft-out`）只在 CLI 单份路径上，批量回归没有汇总导出。

### 明确不要做

接 M3 / 接知识图谱 / 接 M6 / 判断 CREATE·UPDATE / 访问历史任务库 / 部门分发 /
修改上游 9 字段 Schema / 改变 evidence 坐标系 / 修改人工标注 / Dify。

---

## 8. 仓库状态

* 分支 `feat/m2-semantic-consolidator`，从 `feat/m1-extraction` 切出。
* **未推送 GitHub**（服务器访问不了，且推送需用户确认）。
  历史包：`/home/yty/m1x/meeting-m2-consolidator.bundle`。
* `M1_Preprocess/`、`M2_TaskClassifier/`、`M1_5_ProjectNormalizer/`、`M6_TaskManager_Demo/`
  **全部原样保留未改**。
* `M2_TaskClassifier/src/m1_task_gate.py` 按 `confidence`/`status` 做准入，
  而上游新输出不再有这两个字段——**不能作为新 M2 主流程**，也不要为了兼容它
  往 Schema 里加回这些字段。
* `M1_5_ProjectNormalizer/` 的 projects/aliases 主表思路被 `project_normalizer.py`
  继承并重新适配（去掉浮点 `confidence`，加了序数硬否决与主表绕过防护）。
* **本轮没有碰 `dify/`**（需求第二节明确暂停 Dify）。
* `out_*/` 已加进 `.gitignore`，回归产物用 `eval/batch_regression.py` 重跑即可；
  关键汇总已提交在 `results/`。

---

## 9. 交接时最容易搞错的四件事

1. **拿 `merge_recall` 当优化目标** → 标注的合并语义是段落粒度，
   提 recall 就是在制造 over-merge。先读 `eval/gold_merge_groups.py` 顶部。
2. **拿单份文档判断改动好坏** → 逐份指标噪声很大。
   有过实例：某次改动在 0407 上看 project 是掉的，全量却是涨 8.8 点，结论完全相反。
   语料级聚合数才有参考价值，关键实验 `--workers 1` 或 `--repeat N` 看中位数。
3. **看到质量门是 REVIEW 就以为出错了** → block 10 份全是 REVIEW，
   全部来自项目实体 UNCERTAIN（保守设计的预期行为），ERROR 是 0。
4. **以为 `evidence.text` 可以直接给下游用** → 非连续合并的 evidence 目前是包络区间，
   含大量无关内容，见第 5 节 ③。修好之前下游应该读 `merge_trace[].source_evidence`。


## 2026-09-14 最新：M2 项目卡与减少复核已完成

详见 integration/M2_PROJECT_POLICY_FIX_20260914.md（仓库根目录相对路径）。
M2 已改为不确定保持独立并记录 warning；实际别名冲突继续 REVIEW，Schema/来源硬错误拒绝。
report.project_cards 提供父项目卡和子事项引用，正式五字段 payload / 九字段 Item 保持兼容。
0407 原始条目 5–13 已聚成同一父项目卡，九项来源完整，无 M2 复核。
15 场真实 qwen3.8-27b 复测：M2 复核涉及子事项 304→8，平均 0.53/场，14 PASS / 1 REVIEW。
余下 0420 是一个实际旧别名冲突。M2 核心 92、接入 66、M6 兼容 36 项测试通过；
15 份新结果通过 M6 输入契约及来源校验，未执行新的 M6 生命周期全量推理。
结果位于 integration/m2_service/data/project_policy_20260914/。
原 M6 全量 V2 结果与 867 条复核保持原样，不可冒充本轮 M2 结果。
18093 M2 已重载并通过健康检查。中台代码未改动，未派发任务，未提交或推送 Git。
备份在 integration/backups/m2-project-policy-20260914-1720/。
本地查看页 http://127.0.0.1:18797/，本地目录 C:/Users/Lenovo/Documents/会议/m2-project-policy。
