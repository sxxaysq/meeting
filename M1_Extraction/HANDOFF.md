# M1 交接文档

给下一个接手 M1 的 agent。读完这一份就能上手，不用重新摸索环境。
配套细节在 [README.md](README.md)（架构与用法）和 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)（实测数据与已知分歧）。

---

## 0. 一句话现状

M1 已重写完成并跑通全量回归：10 份周例会 PDF、1367 条人工标注，
**标注条目完整覆盖率 0.950，evidence exact_match 0.997，质量门 error 0，Schema 违规 0**，
10 份共 262 秒。139 项单测全绿。Dify DSL 已生成但**尚未在 Dify 里实跑过**。

---

## 1. 先读这一节：环境硬事实

这些是踩出来的，不要重新试错。

### 服务器与路径

| 项 | 值 |
|---|---|
| 工作机 | `192.168.30.214`（用户 `yty`，SSH 免密已配好） |
| 代码 | `/home/yty/m1x/M1_Extraction/` |
| Python | `/home/yty/m1x_venv/bin/python`（3.11，已装 pymupdf/python-docx/jsonschema/pytest/pyyaml） |
| 会议 PDF | `/home/yty/数据集/原始数据/*.pdf`（10 份，2026.4.7–7.13） |
| 人工标注 | `/home/yty/标注/items.annotation_v3.merge_group.json` |
| 规范化文本参照 | `/home/yty/m1_annotation/text/*.body.txt` |
| git 历史包 | `/home/yty/m1x/meeting-m1-extraction.bundle` |

**服务器访问不了 GitHub**（PyPI 可以）。仓库是在别处克隆后 scp 上来的。

### 模型端点

| 端点 | 模型 | 说明 |
|---|---|---|
| `http://192.168.30.215:8000/v1` | `Qwen/Qwen3.6-35B-A3B` | **默认**。vLLM，`max_model_len=262144`，无需 Key。单份约 26 秒 |
| `http://192.168.30.214:7060/v1` | `qwen3:30b` / `qwen2.5:32b` | Ollama。单份约 10 分钟，慢 23 倍 |

⚠️ **模型 id 不是 `Qwen/Qwen3.5-36B-A3B`**（那个 404）。先 `curl .../v1/models` 确认。

### 三个必然踩的坑

**① 思考型模型不关思考 = 解析必然失败。**
Qwen3.x 默认把推理写进独立的 `reasoning` 字段，`content` 直接是 `null`。
已由 `LLM_ENABLE_THINKING=false`（默认）处理，会发送
`chat_template_kwargs.enable_thinking=false`。服务端不认这个参数时，
把该变量设成空字符串即可完全不发送。客户端还保留了从 `reasoning` 兜底取 JSON 的路径并计数。

**② Ollama 的 OpenAI 兼容层把上下文硬截断到 4096 token。**
实测 9000 token 输入的 `prompt_tokens` 仍然是 4096，长 Block 被悄悄截断、无任何报错。
用 Ollama 必须 `LLM_TRANSPORT=ollama` 走原生 `/api/chat` + `LLM_NUM_CTX=8192`。
215 的 vLLM 没这个问题。

**③ 输出 token 不够会整块作废。**
一个 Block 可能要产出 26 个 Item。`max_tokens=4096` 会截断 JSON，
repair 也救不回来（04-13 曾因此 coverage 只有 0.774）。默认已提到 8192。
`llm_stats.truncations` 会计数，别让它无声发生。

### 跑起来

```bash
ssh 192.168.30.214
cd ~/m1x/M1_Extraction
export LLM_BASE_URL=http://192.168.30.215:8000/v1 LLM_MODEL="Qwen/Qwen3.6-35B-A3B" LLM_TRANSPORT=openai LLM_ENABLE_THINKING=false LLM_TIMEOUT=900
~/m1x_venv/bin/python -m pytest tests -q
```

```bash
~/m1x_venv/bin/python src/cli.py extract "/home/yty/数据集/原始数据/2026.4.7信息公司周例会工作安排备忘录.pdf" -o out/items.json --workers 6 -v
```

```bash
~/m1x_venv/bin/python eval/batch_regression.py --pdf-dir /home/yty/数据集/原始数据 --annotation /home/yty/标注/items.annotation_v3.merge_group.json --out-dir out_v2 --workers 6
```

---

## 2. M1 的职责边界（不要越界）

M1 **只做**：从一份完整会议 PDF/DOCX 中高召回、忠实地抽取结构化业务事项。

**明令禁止**（属于 M2/M3/M6）：访问历史任务库、判断 CREATE/UPDATE、跨会议匹配、
项目实体永久归一（**不许**把"红沙泉项目"改成"红沙泉二矿项目"）、跨 Item 语义合并、
知识图谱、部门分发。

输出**只能**是 `{"items":[...]}`，每个 Item 固定 9 个字段
（`department` / `work_section` / `delivery_group` / `project` / `item_type` /
`assignee` / `title` / `content` / `evidence`），禁止 additionalProperties。
禁止出现 `confidence` / `priority` / `project_id` / `task_id` / `CREATE` / `UPDATE` /
`reasoning` / `merge_group`。

`item_type` 四选一：`PROJECT_TASK` / `RESEARCH_TASK` / `NON_PROJECT_WORK` / `NON_TASK_ITEM`。
`project` 与 `item_type` 是**两个独立维度**——
`{"project":"武家塔项目","item_type":"NON_TASK_ITEM","content":"暂无。"}` 合法。
`assignee` 恒为数组，无人时 `[]` 不是 `null`。

粒度取向：**宁可拆多，不要错合**。M2 负责归并，错误合并无法挽回。

---

## 3. 两个必须理解的设计决策

### ① 结构切分是确定性的，不是 LLM

需求原本推荐 LLM Structure Segmenter，实现时改成了正则，理由有两条：

1. 这批会议纪要的编号层级（`一、`/`（一）`/`1.`/`（1）`/`1）`/`①`）**本身就是业务层级**，正则可 100% 复现；
2. Block 需要精确 `start_char/end_char`，而需求第十一节明确禁止 LLM 猜字符位置。
   让 LLM 输出 raw_text 再回找，等于把已经精确的东西先弄模糊再猜回来。

**这个决策有实测支持**：后来试过 LLM 切分（`out_llmseg_2026-07-13/`），
`department` 一致率从 0.982 掉到 **0.094**，`delivery_group` 从 0.881 掉到 **0.076**。
结构字段基本被毁掉。除非有新证据，不要推翻这个决策。

`prompts/structure_segmenter.md` 保留为无编号层级文档的兜底提示词。

### ② 坐标系与人工标注逐字节对齐

`text_normalizer.py` 的输出与标注所用的 `.body.txt` **10/10 份逐字节一致**，
PDF → PyMuPDF → 规范化同样 10/10 一致。所以 `start_char/end_char` 可以和标注直接比对。

规范化步骤顺序**固定不可改**，改了历史标注偏移量全部失效：
① 删 `— N —` 页码 → ② 全角空格 U+3000 转半角 → ③ 按编号合并被 PDF 拆断的行 → ④ 逐行 strip、丢空行、`\n` 连接。

规范化后**每一行 = 原文一个最内层编号条目**，这是全流程的坐标系。

---

## 4. 两种模式

### block 模式（默认，结构化路径）

```text
PDF/DOCX → document_reader（逐页+页字符区间）→ text_normalizer（条目行坐标系）
→ structure_segmenter（确定性 Block）→ item_extractor（逐 Block 调 LLM，只产 evidence_text）
→ evidence_aligner（程序定位 start/end/page/exact_match）→ quality_gate → schema_validator
→ {"items":[...]}
```

### generic 模式（泛用高召回兜底，用户后加）

规范化后把**整篇**文本一次喂给 `prompts/generic_item_extractor.md`，
模型返回 `tasks[]`，再映射回同一份 items schema
（`GENERAL_WORK` → `NON_PROJECT_WORK`，`project_group` → `delivery_group`）。
不依赖标题/编号/换行/表格，面向 ASR、OCR、断句错误、排版混乱文本。
evidence 仍走同一个 `evidence_aligner` 程序定位。

```bash
python src/cli.py extract 会议.pdf -o out/items.json --mode generic
```

### 实测对比（2026-04-07，单次运行，仅供参考）

| | coverage | exact_match | department | delivery_group | work_section | project | item_type |
|---|---|---|---|---|---|---|---|
| block | 0.954 | 1.000 | 1.000 | 0.947 | 0.189 | 0.803 | 0.652 |
| generic | 0.947 | 1.000 | 1.000 | 1.000 | **0.000** | 0.527 | 0.641 |

**结论：召回相当，结构字段 generic 更弱。**
`work_section` 恒为 0 是因为 `generic_extractor.normalize_task` 里硬编码 `"work_section": None`。
`project` 从 0.803 掉到 0.527，因为丢掉了确定性层级上下文。

**所以：格式规整的正式纪要用 block；ASR/OCR/格式崩坏的文本才用 generic。**
不要因为 generic "更通用"就把它设成默认。

---

## 5. 当前指标（block 模式，全量 10 份 / 1367 条标注）

| 指标 | 值 |
|---|---|
| 标注 / 预测条目 | 1367 / 1444（比值 1.056，严格配对 1142 对） |
| 标注条目完整覆盖率 | **0.950** |
| evidence `exact_match` | **0.997** |
| 质量门 error / Schema 违规 | **0 / 0** |
| 模型失败 / repair / 截断 | 0 / 0 / 0 |
| 耗时 | 262 秒 / 10 份 |

字段一致率：`delivery_group` 0.969、`assignee` 0.968、`department` 0.949、
`item_type` 0.760、`project` 0.659、`work_section` 0.287（真实板块名子集 0.591）。

⚠️ **单次跑分方差很大**。`temperature=0` 但 vLLM 并发下不确定，
同一份文档两次 coverage 能在 0.85–0.99 之间跳（04-20 实测 0.988 / 0.856）。
**评估提示词改动时必须多跑几次或 `--workers 1`**，否则你会把噪声当成改进。

---

## 6. 代码地图

```text
src/
  document_reader.py     PDF/DOCX/TXT → 原文 + 每页字符区间
  text_normalizer.py     规范化 + 字符级偏移映射（坐标系的定义者）
  structure_segmenter.py 确定性业务结构 Block
  item_extractor.py      block 模式：Block → Item；含 _check_project 护栏
  generic_extractor.py   generic 模式：整篇 tasks → Item
  evidence_aligner.py    三级降级定位，exact_match 不伪造
  quality_gate.py        8 项基础检查，只报不改
  schema_validator.py    严格 Schema（内置校验 + jsonschema 双路）
  llm_client.py          OpenAI 兼容，最多一次 repair，不无限重试
  pipeline.py / cli.py
prompts/   item_extractor.md（主）/ generic_item_extractor.md / structure_segmenter.md（兜底）/ quality_check.md
schemas/   m1_items.schema.json / blocks.schema.json
eval/      batch_regression.py（10 份批量）/ compare_annotation.py（单份）
dify/      build_workflow.py 生成 m1_workflow.yml；code_nodes/*.py 被 pytest 直接测
tests/     139 项
```

**Dify 的 Code 节点 Python 不是手写进 YAML 的**：写在 `dify/code_nodes/*.py`，
由 `build_workflow.py` 内联进 DSL，并由 `tests/test_dify_nodes.py` 断言与 `src/` 主实现产出一致。
改了 code_nodes 或提示词后**必须重新生成** `m1_workflow.yml`。

---

## 7. 几条不该退回去的设计约束

改代码前先读这几条，都是有实测代价换来的：

1. **evidence 位置一律由程序算**，LLM 只给 `evidence_text`。
   `exact_match=true` 仅当 evidence 的每个有效字符在原文中连续命中。不许伪造。
2. **校验失败不静默修正**。CLI 退出码 2 且不写 items.json（`--allow-invalid` 可强制），
   Dify 校验节点直接抛错。但 **report 永远落盘**，否则没法排查。
3. **整条 Item 只在不可用时才丢**。单个附属字段（如 `assignee`）形状不对时清空该字段并
   写 `normalize_warnings`，不要赔上整条业务事项——M1 的目标是高召回。
4. **`project` 必须是原文连续片段**。`item_extractor._check_project` 是确定性护栏，
   拼接/加后缀的名字会被置空并告警。全量只触发 8 次，全部命中真实拼接，无误伤。
5. **不要改标注去迎合模型输出**。差异如实报告在 KNOWN_ISSUES.md。

---

## 8. 待办（按优先级）

### P0 — 需要人拍板，不要自己决定

**招采台账的 `item_type` 口径。** 这是 `item_type` 0.760 的最大来源：
163 条"采购管理/招采清单"，人工标注判 `NON_TASK_ITEM` + `project=null`，
M1 判 `PROJECT_TASK` + 项目名。原文确实写了项目名和采购动作，**不是模型臆造**。
业务上确认后，正确做法是在 `prompts/item_extractor.md` 的 `NON_TASK_ITEM`
定义里补一条"招采清单/台账式列举不算任务"，**不是改标注**。
这一条定了，`item_type` 和 `project` 两个指标会同时明显上移。

### P1 — 可以直接做

* **Dify 实跑**。DSL 已生成并通过 20 项接线校验，但从没在 Dify 里跑过。
  Dify 1.3.1 在 `192.168.30.214:80`，Docker 可通过 `DOCKER_HOST=tcp://127.0.0.1:2375` 管理（无认证）。
  控制台需登录，账号 `admin@dify.ai`，**密码没人知道**。
  重置命令（属于凭据变更，需用户明确同意后再执行）：
  ```bash
  DOCKER_HOST=tcp://127.0.0.1:2375 docker exec dify-api-1 flask reset-password --email admin@dify.ai --new-password '<新密码>' --password-confirm '<新密码>'
  ```
  ⚠️ 现有 Dify 里已有一个 2026-08-13 建的同名应用，**不要重装、不要覆盖**。
  数据库备份在 `/home/yty/m1x/_backup_dify_20260817/dify_db_full.sql`。
  导入新 workflow 时建成**新应用**。
* **多次运行取中位数的评测脚本**。当前单次跑分噪声太大，无法可靠比较提示词改动。
* **`work_section` 评测口径**。现在靠"标注值长度 ≤ 20"这个粗启发式区分真实板块名和标注产物，不够严谨。

### P2 — 有价值但不急

* generic 模式补上 `work_section`（现在硬编码 `None`）。
* generic 模式跑一次全量回归（当前只有单份对比）。
* `RESEARCH_TASK` 漏判 35 条、`PROJECT_TASK` 被降级成 `NON_PROJECT_WORK` 67 条，
  提示词已补判据但只从 0.750 提到 0.760，需要更系统的办法。
* 接 M2 时注意：`M2_TaskClassifier/src/m1_task_gate.py` 按 `confidence/status` 做准入，
  而 M1 新输出**不再有这两个字段**，必须改造。

---

## 9. 仓库与同步状态（重要）

* 分支 `feat/m1-extraction`，6 个 commit，**未推送 GitHub**（推送需用户确认）。
* git 工作副本在**一台笔记本的临时目录**里，随时可能被清理。
  服务器上的 `/home/yty/m1x/meeting-m1-extraction.bundle` 是完整历史包：
  ```bash
  git clone /home/yty/m1x/meeting-m1-extraction.bundle meeting
  ```
* **服务器上的 `/home/yty/m1x/M1_Extraction/` 不是 git 工作区**，只是同步过去的文件副本。
  在服务器上改了代码，记得同步回仓库再 commit，否则会丢。

### 只动过一个旧文件

`.gitignore` 加了一行 `!.env.example`（原来的 `.env.*` 会把配置模板也忽略掉）。
`M1_Preprocess/`、`M2_TaskClassifier/`、`M1_5_ProjectNormalizer/` **全部原样保留未改**，可回滚。

### 服务器上的杂物（不在仓库里，可忽略或清理）

`backups/`、`dify/n*.py`（与 `dify/code_nodes/` 重复的副本）、
根目录的 `test_llm_client.py`、`tests/item_extractor.md`、
以及多个实验输出目录 `out*`（`out_v2` 是 block 模式最新全量结果，
`out_generic_*`、`out_llmseg_*` 是对照实验）。
`input_manual/2026-08-10_items/` 是一份**不在标注集内**的新会议 PDF。

---

## 10. 交接时最容易搞错的三件事

1. **模型 id 写错**（`Qwen3.5-36B` vs 实际 `Qwen3.6-35B-A3B`）→ 直接 404。
2. **忘了关思考** → `content` 是 `null`，看起来像模型不听话，实际是解析路径不对。
3. **拿单次跑分当结论** → 方差 0.85–0.99，你会反复"优化"出不存在的提升。
