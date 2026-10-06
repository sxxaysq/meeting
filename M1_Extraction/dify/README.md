# M1 Dify Workflow

## 拓扑

```text
Start (会议文件, Block字符上限)
  ↓
Document Extractor            PDF/DOCX → 纯文本
  ↓
Code  文本规范化+结构切分       去页码、条目行合并、部门/板块/交付组层级 → blocks[]
  ↓
Iteration(blocks)  并发 3
  ├─ Code  展开 Block 字段
  ├─ LLM   Item Extractor      只产出 evidence_text，不猜字符位置
  └─ Code  解析本轮输出         非法候选进 dropped，不静默修正
  ↓
Code  Flatten
  ↓
Code  Evidence Aligner         start_char/end_char/exact_match 由代码算出
  ↓
Code  基础质量门                8 项检查，问题写入 issues_json
  ↓
Code  Final JSON Schema Validator   不合规直接抛错
  ↓
End   items
```

End 只输出 `items`，因此一次调用的结果就是 `{"items": [...]}`，可作为当前 M1→M3→M6 链路的 M1 输入。

## 生成 DSL

`m1_workflow.yml` 是生成产物，不要手改；改提示词或 Code 节点后重新生成：

```bash
python M1_Extraction/dify/build_workflow.py
```

默认指向服务器本地 Qwen3.5（`qwen35_9b_api` 暴露 OpenAI 兼容接口，
供应商 `openai_api_compatible`，模型名 `Qwen3.5-9B`）。换成 Ollama：

```bash
python M1_Extraction/dify/build_workflow.py --provider langgenius/ollama/ollama --model qwen3:30b
```

在 Dify 里配置本地 Qwen3.5 供应商时，API Base URL 填
`http://192.168.30.214:8001/v1`（容器内访问宿主机端口，不能写 127.0.0.1），
先用 `~/qwen35_9b_api/start_qwen35_9b_api.sh` 把服务拉起来。

Code 节点的 Python 来自 `dify/code_nodes/*.py`，由 `tests/test_dify_nodes.py`
直接测试，并断言与 `src/` 主实现产出一致。

## 从 Dify UI 导入

1. 左上角 **工作室 / Studio** → **导入 DSL 文件**（Import DSL）。
2. 选择 `M1_Extraction/dify/m1_workflow.yml`，确认创建。
3. 打开应用，点开 **Item Extractor** 节点，在右上角模型选择器里
   选中你已配置的模型（DSL 里默认写的是 Ollama 的 `qwen3:30b`；
   若你的工作区用的是别的供应商，这里换掉即可，提示词不用动）。
4. 右上角 **运行 / Run**，上传一份会议 PDF，`Block 字符上限` 留空即用默认 1200。
5. 运行结束后在 **结束节点** 查看 `items`；每个 Code 节点的输入输出都能在
   运行详情里逐节点展开，方便定位问题。

## 通过 API 调用

在应用右上角「访问 API」里创建 API Key，然后：

```bash
curl -X POST 'http://192.168.30.214/v1/workflows/run' -H 'Authorization: Bearer <APP_API_KEY>' -H 'Content-Type: application/json' -d '{"inputs":{"meeting_file":{"transfer_method":"local_file","upload_file_id":"<FILE_ID>","type":"document"},"max_block_chars":"1200"},"response_mode":"blocking","user":"m1"}'
```

`<FILE_ID>` 来自先调用文件上传接口：

```bash
curl -X POST 'http://192.168.30.214/v1/files/upload' -H 'Authorization: Bearer <APP_API_KEY>' -F 'file=@会议.pdf' -F 'user=m1'
```

## 已知限制

**页码为 null。** Dify 的 Document Extractor 只返回纯文本，不给分页信息，
所以这条链路上 `evidence.page_start/page_end` 恒为 `null`
（与现有人工标注一致）。需要真实页码时走 `src/cli.py`，
它用 PyMuPDF 逐页读取并保留每页的字符区间。

**沙箱字符串上限。** Dify Code 节点默认 `CODE_MAX_STRING_LENGTH=80000`。
一份周例会正文约 8000 字符、items JSON 约 60–110KB，
`items_json` 有可能触顶。触顶时在 Dify 的 `docker/.env` 里调大：

```text
CODE_MAX_STRING_LENGTH=2000000
```

改完 `docker compose up -d dify-sandbox dify-api` 生效。

**模型上下文。** Item Extractor 的输入约 900–1400 token，输出可达 1500 token。
用 Ollama 供应商时记得把模型的上下文长度设到 8192 以上，
否则长 Block 会被静默截断（Ollama 默认 num_ctx=4096）。
