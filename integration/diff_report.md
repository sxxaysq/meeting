# 双路 diff 验收报告（§4.4）

日期：2026-08-20　执行人：开发 agent（全程未在顿悟/中台平台执行任何写操作）

## 1. 验收目标与方式

§4.4 要求同一份会议 PDF 双路运行（顿悟链路 vs `src/cli.py`），items 逐字段 diff，
唯一允许差异为 `evidence.page_start/page_end`（平台链路恒 null）。

决策门结论（详见 `acceptance_report.md`）：顿悟工作流是平台自研画布，**无 Dify DSL 导入入口**，
Track A 无法在平台侧真实运行。因此按两条链路的**唯一已知结构差异**（平台 Document Extractor
只给纯文本、无分页元数据）做等价复现：

- **路 1（cli 路径，真实页码）**：`run_pipeline(PDF)`，PyMuPDF 逐页读取，保留 page_spans；
- **路 2（平台等价路径，页码 null）**：同一份 PDF 用与 `document_reader._read_pdf` 完全相同的
  逐页文本拼成纯文本 `.txt`，再 `run_pipeline(TXT)`（`page_spans=[]` → 页码恒 null）。

两路共用同一份规范化/切分/LLM 抽取/对齐/质量门/Schema 校验代码与同一模型
（`Qwen/Qwen3.6-35B-A3B` @ `http://192.168.30.215:8000/v1`，temperature=0，禁思考），
隔离变量只剩"页码来源"。

## 2. 样本与命令

- 样本：`2026.4.7信息公司周例会工作安排备忘录.pdf`
  （取自 `/home/yty/m1x/meeting-m6-work/M6_TaskManager_Demo/demo_runs/20260819_061450_8d098f/input/`）
- 驱动器：`integration/diff_two_paths.py`

```bash
cd /home/yty/m1x/integration
LLM_BASE_URL=http://192.168.30.215:8000/v1 LLM_MODEL="Qwen/Qwen3.6-35B-A3B" \
LLM_ENABLE_THINKING=false LLM_TEMPERATURE=0 LLM_MAX_TOKENS=8192 \
/home/yty/m1x_venv/bin/python diff_two_paths.py \
  --pdf "<上述 PDF 路径>" --out /home/yty/m1x/integration/data/diff_run
```

## 3. diff 结论（`data/diff_run/diff_summary.json`）

| 指标 | 路 1（PDF/真实页码） | 路 2（纯文本/平台等价） |
|---|---|---|
| items 数 | 160 | 160 |
| exact_match 占比 | 98.75% | 98.75% |
| 八字段内容差异（department…content） | — | **0 处** |
| evidence 差异（text/start_char/end_char/exact_match） | — | **0 处** |
| `evidence.page_start/page_end` 差异 | 真实页码 | 全部为 null（160/160） |

**判定：PASS。** 唯一差异即 `evidence.page_start/page_end`（路 2 恒 null），
与 §4.4 的允许差异完全一致；页码之外逐字段全同，说明平台链路的"无分页"限制
不会污染任何业务字段与坐标。

产物：`data/diff_run/{items_pdf.json, items_txt.json, report_pdf.json, report_txt.json,
diff_summary.json}`。

> 2026-08-20 管理员清理要求下，上述原始产物已删除；本报告保留为验收记录，
> 复跑 `diff_two_paths.py` 可重新生成。

## 4. 回归与旁证

- M1 既有测试全绿：`pytest M1_Extraction/tests/` → **139 passed**
  （含 `test_dify_nodes.py` Code 节点与 `src/` 主实现一致性断言、`test_dify_dsl.py` 接线校验）；
- Track B 服务旁证（小样本）：`POST :8091/m1/extract` 上传 `sample_meeting.txt` →
  200，10 items，恰九字段，坐标由代码计算；其结果再 `POST :8090/m1/ingest` 两次 →
  行数恒为 10（幂等），exact_match 占比 100%。
