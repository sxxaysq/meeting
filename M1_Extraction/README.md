# M1 Meeting Item Extraction

> 接手这个模块请先读 **[HANDOFF.md](HANDOFF.md)**：环境、端点、必踩的坑、
> 不该退回去的设计约束、以及按优先级排好的待办。

从一份**完整会议 PDF/DOCX** 中，高召回、忠实地抽取结构化业务事项。

M1 的唯一职责就是抽取。它**不做**：访问历史任务数据库、判断 CREATE/UPDATE、
跨会议任务匹配、项目实体永久归一（不会把"红沙泉项目"改成"红沙泉二矿项目"）、
跨 Item 的语义合并、知识图谱、部门分发。

原则：**宁可把不确定是否属于同一业务事项的内容拆成多条，也不要把不同业务事项错误合并。**
下游 M3 负责项目解析，M6 负责历史任务关联与生命周期；M1 保留独立事项及其原文证据。

## 架构

```text
PDF/DOCX
   ↓  document_reader      逐页读取，记录每页在原文中的字符区间
   ↓  text_normalizer      去页码、全角空格、按编号合并断行 → 条目行坐标系
   ↓  structure_segmenter  默认：还原 部门/工作板块/交付组 层级 → Block（确定性）
   ↓  item_extractor       逐 Block 调 LLM，只产出 evidence_text（LLM）
   ↓  evidence_aligner     evidence_text 回原文定位 → start/end/page/exact_match（确定性）
   ↓  quality_gate         8 项基础检查，不静默修正
   ↓  schema_validator     严格 Schema，不合规就失败
   ↓  {"items": [...]}
```

另有 `generic` 泛用高召回模式：规范化后把整篇会议文本一次喂给
`prompts/generic_item_extractor.md`，输出 `tasks[]` 后映射为同一份
`{"items":[...]}` schema。这个模式不依赖固定标题、编号、换行或表格，
更适合 ASR/OCR/格式混乱文本，以及经营工作、售前跟进、部门协同等弱动作短句召回。
它会保留更多候选，项目称谓解析交给 M3，任务关联与严格业务判定交给 M6。

### 为什么结构切分是确定性的，而不是 LLM

需求推荐用 LLM 做 Structure Segmenter。实测这批会议纪要的编号层级
（`一、` / `（一）` / `1.` / `（1）` / `1）` / `①`）本身就是业务层级，
正则可以 100% 复现，而且——**Block 需要精确的 start_char/end_char，
而需求第十一节明确禁止 LLM 猜字符位置**。让 LLM 输出 raw_text 再回找，
等于把一个已经精确的东西先弄模糊再猜回来。

所以：结构切分走 `structure_segmenter.py`（确定性、可复现、位置精确），
`prompts/structure_segmenter.md` 保留为**兜底路径**，供没有编号层级的自由排版文档使用。
LLM 的算力全部投到真正需要语义判断的 Item 抽取上。

验证：`src/text_normalizer.py` 的输出与人工标注所用的 `.body.txt`
**10/10 份文档逐字节一致**，PDF → PyMuPDF → 规范化同样 10/10 一致。
也就是说本模块的 `start_char/end_char` 与现有人工标注在同一坐标系，可直接对比。

## 输出格式

只输出 `{"items": [...]}`，每个 Item 固定 9 个字段，禁止 additionalProperties：

```json
{
  "items": [
    {
      "department": "智能矿山事业部",
      "work_section": "项目推进",
      "delivery_group": "新疆交付组",
      "project": "红沙泉二矿项目",
      "item_type": "PROJECT_TASK",
      "assignee": [],
      "title": "集控中心（四合院）建设",
      "content": "完成顶面线管、桥架安装100%，吊顶、墙面工程90%；确保空调设备100%到货。",
      "evidence": {
        "text": "①集控中心（四合院）：完成顶面线管、桥架安装100%，吊顶、墙面工程90%；确保空调设备100%到货。",
        "page_start": 2,
        "page_end": 2,
        "start_char": 380,
        "end_char": 431,
        "exact_match": true
      }
    }
  ]
}
```

禁止出现 `confidence` / `priority` / `project_id` / `task_id` /
`CREATE` / `UPDATE` / `reasoning` / `merge_group` 等任何其他字段。

`item_type` 四选一：`PROJECT_TASK` / `RESEARCH_TASK` / `NON_PROJECT_WORK` / `NON_TASK_ITEM`。
`project` 与 `item_type` 是**两个独立维度**——
`{"project":"武家塔项目","item_type":"NON_TASK_ITEM","content":"暂无。"}` 是合法结果。
`assignee` 恒为数组，无人时是 `[]` 而不是 `null`。

### 与人工标注的一处刻意差异

人工标注里 `evidence.text` 恒等于 `content`（去掉编号后的清洗文本）。
本模块的 `evidence.text` 是**逐字原文**（保留 `①`、`（2）` 等编号），
因为需求第十一节要求 evidence 可回原文定位、且 `exact_match` 不得伪造。
逐字原文才能让"能否完整定位"这件事有真实意义。
`start_char/end_char` 的口径与标注一致（覆盖完整条目行）。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r M1_Extraction/requirements.txt
```

## 配置

复制 `.env.example` 为 `.env`。模型名不写死在业务逻辑里：

```text
LLM_BASE_URL   OpenAI Chat Completions 兼容地址
LLM_API_KEY
LLM_MODEL
LLM_TIMEOUT
```

默认指向 192.168.30.215:8000 的 vLLM（`Qwen/Qwen3.6-35B-A3B`，无需 Key）。

两个必须知道的坑：

**思考型模型要关思考。** Qwen3.x 默认把推理写进独立的 `reasoning` 字段，
`content` 直接是 `null`，不关的话 JSON 解析必然失败。
`LLM_ENABLE_THINKING=false`（默认）会发送 `chat_template_kwargs.enable_thinking=false`；
服务端不认识该参数时留空即可完全不发送。客户端仍保留从 `reasoning` 兜底取 JSON 的路径。

**Ollama 的 OpenAI 兼容层把上下文硬截断到 4096 token**
（实测 9000 token 输入的 `prompt_tokens` 仍是 4096），长 Block 会被悄悄截断。
所以改用 Ollama 时必须 `LLM_TRANSPORT=ollama` 走原生 `/api/chat` + `LLM_NUM_CTX`。
vLLM / 云端模型保持默认 `openai` 即可，提示词与业务逻辑完全不变。

## CLI

```bash
python M1_Extraction/src/cli.py text   会议.pdf          # 只看规范化文本与坐标系
```

```bash
python M1_Extraction/src/cli.py blocks 会议.pdf          # 只看结构切分，不调模型
```

```bash
python M1_Extraction/src/cli.py extract 会议.pdf -o out/items.json --workers 3 -v
```

格式差或想用泛用兜底召回时：

```bash
python M1_Extraction/src/cli.py extract 会议.pdf -o out/items.json --mode generic
```

`extract` 会写出两个文件：`items.json`（只有 `{"items":[...]}`）和
`items.report.json`（Block 数、exact_match 比例、item_type 分布、
质量门问题、被丢弃候选、模型调用统计）。业务输出不被诊断信息污染。

Schema 不通过时默认**退出码 2 并打印错误**，不会静默写出看起来正常的结果；
需要强行落盘调试时加 `--allow-invalid`。

## Dify Workflow

见 [`dify/README.md`](dify/README.md)。DSL 由 `dify/build_workflow.py` 生成，
Code 节点的 Python 从 `dify/code_nodes/*.py` 内联，
这些文件由 `tests/test_dify_nodes.py` 直接测试，并断言与 `src/` 主实现产出一致——
不会出现"YAML 里的代码没人测过"。

## 测试

```bash
cd M1_Extraction && python -m pytest tests -q
```

覆盖需求第十八节的 7 个用例（普通项目 / 同段多项目 / 项目下多动作 / 暂无 /
科研 / 非项目工作 / 相似项目名不归一）、Schema 严格性、Evidence 定位与降级、
以及 Dify Code 节点与主实现的一致性。Item 抽取用假模型替身，测试不依赖在线模型。

## 回归评测

全量 10 份会议一次跑完并汇总（约 4.5 分钟）：

```bash
python M1_Extraction/eval/batch_regression.py --pdf-dir /home/yty/数据集/原始数据 --annotation /home/yty/标注/items.annotation_v3.merge_group.json --out-dir out_v2 --workers 6
```

泛用兜底模式批量回归：

```bash
python M1_Extraction/eval/batch_regression.py --pdf-dir /home/yty/数据集/原始数据 --annotation /home/yty/标注/items.annotation_v3.merge_group.json --out-dir out_generic --mode generic --workers 1
```

单份对比：

```bash
python M1_Extraction/eval/compare_annotation.py --annotation /home/yty/标注/items.annotation_v3.merge_group.json --doc-index 0 --predicted out_v2/2026-04-07.items.json
```

标注文件是 10 份会议拼接的一个 items 数组，脚本按 `start_char` 回退切分文档边界。
**不修改标注数据来迎合模型输出。**

全量实测（Qwen3.6-35B-A3B，1367 条标注）：标注条目完整覆盖率 **0.950**，
evidence `exact_match` **0.997**，质量门 error **0**，Schema 违规 **0**。
逐字段一致率与已知分歧见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

## 目录

```text
M1_Extraction/
├── README.md              本文
├── .env.example
├── requirements.txt
├── dify/
│   ├── README.md          导入与运行步骤
│   ├── build_workflow.py  DSL 生成器
│   ├── m1_workflow.yml    可直接导入 Dify 的 DSL
│   └── code_nodes/        Code 节点 Python（被 pytest 直接测试）
├── prompts/
│   ├── item_extractor.md     主提示词
│   ├── generic_item_extractor.md 整篇泛用高召回提示词
│   ├── structure_segmenter.md 兜底切分提示词
│   └── quality_check.md       可选语义质检提示词
├── schemas/
│   ├── blocks.schema.json
│   └── m1_items.schema.json
├── src/
│   ├── document_reader.py     PDF/DOCX/TXT → 原文 + 页字符区间
│   ├── text_normalizer.py     规范化 + 字符级偏移映射
│   ├── structure_segmenter.py 业务结构 Block（确定性）
│   ├── llm_client.py          OpenAI 兼容客户端，最多一次 repair
│   ├── item_extractor.py      Block → Item
│   ├── generic_extractor.py   整篇泛用 tasks → M1 Item 兜底路径
│   ├── evidence_aligner.py    Evidence 定位（确定性）
│   ├── quality_gate.py        基础质量门
│   ├── schema_validator.py    严格 Schema
│   ├── pipeline.py            主链路
│   └── cli.py
├── eval/compare_annotation.py
└── tests/
```
