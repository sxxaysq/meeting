# 顿悟智体平台「HTTP 请求」节点缺陷单（阻断 M1 Track B 工作流）

**提交对象**：顿悟智体平台管理员
**提交方**：M1 集成开发侧（全程只读排查，未修改平台任何配置与代码）
**日期**：2026-09-03
**严重级别**：**阻断（Blocker）** —— 当前版本下，任何需要向外部服务提交请求体的工作流均无法跑通

---

## 1. 环境信息

| 项 | 值 |
|---|---|
| 平台后端容器 | `sxzk-api`（镜像 `yuxi-api:latest`，构建于 2026-08-29） |
| 代码版本 | `yuxi-workspace` **0.7.0.beta2**（`/app/pyproject.toml`） |
| 关键文件 | `/app/package/yuxi/workflow/node_executors.py`、`/app/server/routers/workflow_router.py`、`/app/package/yuxi/workflow/models.py` |
| 前端容器 | `frontend-main`（`:80`，静态资源 `/usr/share/nginx/html/yuxi/`） |
| 受影响工作流 | ID=3「M1 会议任务抽取入库（Track B）」，5 节点 4 连接 |
| 被调用方 | M1 抽取 `http://192.168.30.216:18091/m1/extract`（FastAPI, multipart）<br>M1 入库 `http://192.168.30.216:18090/m1/ingest`（FastAPI, JSON） |
| 运行日志 | `docker logs sxzk-api`，runId 示例 `wf_run_7c09a6fc36bf`（2026-09-02 15:49:42） |

**先说结论：被调用方无故障。** 开发侧直连实测同一份会议 PDF（297 KB）：

```
POST http://192.168.30.216:18091/m1/extract   (multipart, mode=generic)
→ HTTP 200, 76489 bytes, 81.58 s, 138 条事项, evidence exact_match 100%
```

---

## 2. 缺陷清单

### 缺陷 1（阻断）：`bodyType` 字段被完全忽略，HTTP 节点无法发送 multipart / form-data / x-www-form-urlencoded

**现象**
在节点面板把 Body 类型选成 `form-data` 并填入附件变量后，请求仍然以 `Content-Type: application/json` 发出，被调用方收不到任何 multipart 字段，直接返回 `422 {"detail":[{"type":"missing","loc":["body","file"],"msg":"Field required"}]}`。

**根因（代码位置）**
`node_executors.py:325-331`，`HttpNodeExecutor.execute()` 中发请求的语句写死只有 `json=`：

```python
response = await client.request(
    method=method,
    url=url,
    params=params if params else None,
    headers=headers or None,
    json=body if method in ("POST", "PUT", "PATCH") else None,
)
```

`bodyType` 在 `models.py:93` 有定义、在 `workflow_router.py:133` 被读取入库，但**执行器全文从未引用过 `cfg.bodyType`**。全仓库检索 `files=` / `multipart` 在执行器内零命中；`NodeExecutorFactory` 中 `http` 类型只有这一个实现（`node_executors.py:1214-1250`），无别名覆盖。

**影响面**
所有需要上传文件的编排（文档、图片、附件处理类服务）在本平台上**全部不可实现**。这也是本工作流第一次失败的最直接原因。

**期望行为**
`bodyType` 生效，至少支持 `none | json | form-data | x-www-form-urlencoded`；`form-data` 需支持把「附件类型变量」还原为真实文件分片（平台开始节点已把附件解码为纯 base64 字符串，见缺陷 4 的日志证据，具备还原条件）。

---

### 缺陷 2（阻断）：请求体中的 `{{变量}}` 占位符不做任何替换

**现象**
被调用方实际收到的请求体里，占位符是**原样字面量**，例如 body 为字符串 `"file={{meeting_file}}\nmode={{mode}}"` 时，服务端收到的就是这个带 `{{}}` 的原文。

**根因（代码位置）**
两处叠加：

1. `workflow_router.py:121-124`（`_map_node_config`）把非 dict 的 body 判空：
   ```python
   raw_body = cfg.get("body")
   if not isinstance(raw_body, dict):
       raw_body = None
   ```
   而前端对 `form-data` 和 `json` 两种 Body 类型**都存成字符串**（见 §3 的 DB 原文），于是 `cfg.body` 恒为 `None`。

2. `node_executors.py:301-303` 又通过 `extra=cfg`（`workflow_router.py:177`）把原始字符串捞回来，但插值只在 dict 分支执行：
   ```python
   body = cfg.body or cfg.extra.get("body")
   if isinstance(body, dict):
       body = _resolve_dict(body, context)   # 字符串走不到这里
   ```
   结果 `json=<字符串>` 被 httpx 序列化为一个 **JSON 字符串字面量**（整体带引号）发出，既不是对象也不是表单。

**对照**：同一执行器里 **headers 是走插值的**（`node_executors.py:287` `headers = _resolve_dict(headers, context)`），而 `queryParams`（`:289-297`）和 `body` 都不走。行为不一致。

**影响面**
即使不涉及文件，只要 body 需要携带上游节点产出的动态数据，本节点也无法使用。

**期望行为**
字符串形态的 body 应先做模板解析；若解析结果是合法 JSON 文本，则以 JSON 对象发送（而非字符串字面量）；`form-data` / `urlencoded` 形态按行解析 `k=v` 后再插值。

---

### 缺陷 3（高）：变量引用只认 nodeId，UI 展示的「显示名」引用无法解析，且无提示

**现象**
body 里写 `{{开始.会议文件}}`、`{{代码执行.result}}` 时，即便插值逻辑正常也不会命中 —— 执行引擎的 context 以 nodeId 为键（`node-1787743092871` 这种）。同一工作流的输出节点存的却是正确的 nodeId 形态：`node-1787743293361.response`。

**根因（代码位置）**
`node_executors.py:64-90`（`_resolve_template`）：
```python
node_output = context.get(ref_node, {})   # ref_node = "开始" → 取不到
```
取不到时 `value = node_output.get(ref_key, m.group(0))` 会**原样返回占位符**，不报错、不告警，静默把 `{{...}}` 发给下游。

**期望行为**
二选一即可：① 引擎支持按节点 label/displayName 解析（`_resolve_variable_value` 已具备按 key 名跨节点查找的能力，`:534-546`，可对齐）；② 保存/运行时对无法解析的引用给出显式校验错误。

---

### 缺陷 4（高）：节点失败被下游静默吞掉，调试面板显示 `{"result": ""}`，看起来像"跑通了但结果为空"

**现象**
5 节点链路里 3 个 failed，但右侧「输出结果」只有一对空引号，界面上没有任何醒目报错。运行日志实际是：

```
09-02 15:49:42 WARNING service.py:174: [Workflow] 节点 'HTTP 请求' 失败: Client error '422 ...' for url '.../m1/extract'
09-02 15:49:42 ERROR   node_executors.py:457: [Workflow] 代码节点 '代码执行' 失败: 'NoneType' object has no attribute 'get'
09-02 15:49:42 WARNING service.py:174: [Workflow] 节点 'HTTP 请求' 失败: Client error '422 ...' for url '.../m1/ingest'
09-02 15:49:42 INFO    node_executors.py:518: [Workflow] 输出节点 '输出' 完成, output={'result': ''}
09-02 15:49:42 INFO    service.py:203:  [Workflow] 工作流执行完成 runId=wf_run_7c09a6fc36bf, totalLatency=80ms, success=2/5
```

**根因（代码位置）**
`models.py:207-211`（`get_node_output`）在节点未写入 context 时返回 `default=""`，`OutputNodeExecutor`（`node_executors.py:487-497`）把它当成正常输出返回 `status="success"`。失败信息没有向下游传播，也没有汇总到 `finalOutput`。

**附带问题（同一处代码）**
`OutputNodeExecutor` 对 dict 值会摊平成 `{name}_{k}` 形式的键：
```python
if isinstance(val, dict):
    for k, v in val.items():
        output[f"{name}_{k}"] = v
```
所以**即使整条链路全部成功**，绑定 `result` 的输出节点也不会返回 `{"result": {...}}`，而是 `{"result_status":"ok","result_item_count":138,...}`。若这是设计意图，建议在面板上说明；否则建议保留嵌套结构。

**期望行为**
上游节点 failed 时，输出节点应标记为 failed（或至少在 `finalOutput` 里带上失败节点清单与错误原文），不要返回空字符串。

---

### 缺陷 5（中，附带发现）：`inputMappings.sourceVariable` 按 key 名跨节点匹配，多节点同名输出时结果不确定

`node_executors.py:534-546`（`_resolve_variable_value`）遍历所有节点输出，返回**第一个** key 名等于 `sourceVariable` 的值。本工作流两个 HTTP 节点都输出 `response`，代码节点映射 `http_result ← response` —— 当前因为缺陷 1/2 走不到这一步，一旦前两个缺陷修好，这里取到哪个节点的 `response` 取决于 dict 迭代顺序。建议引用格式统一为 `nodeId.key`。

---

## 3. 证据附录

### 3.1 平台库中该工作流的节点配置原文（只读查询 `workflow_configs.nodes_json`）

HTTP #1（抽取）—— 注意 `body` 是**字符串**、`bodyType` 是 `form-data`：

```json
{
  "id": "node-1787743092871",
  "key": "http",
  "config": {
    "url": "http://192.168.30.216:18091/m1/extract",
    "method": "POST",
    "bodyType": "form-data",
    "body": "file={{meeting_file}}\nmode={{mode}}",
    "headers": [],
    "queryParams": [],
    "timeout": 600000,
    "retryCount": 0,
    "retryInterval": 1000
  }
}
```

HTTP #2（入库）—— `bodyType=json` 但 body 同样是**字符串**，且引用用了显示名：

```json
{
  "id": "node-1787743293361",
  "key": "http",
  "config": {
    "url": "http://192.168.30.216:18090/m1/ingest",
    "method": "POST",
    "bodyType": "json",
    "body": "{\n  \"source_document_id\": \"{{开始.会议文件}}\",\n  \"file_name\": \"{{开始.会议文件}}\",\n  \"mode\": \"{{开始.mode}}\",\n  \"items\": {{代码执行.result}}\n}",
    "headers": [{ "key": "", "value": "application/json", "valueType": "fixed" }],
    "timeout": 30000
  }
}
```

开始节点输入（`meeting_file` 类型为「附件」）与运行日志证明：**附件确实被解码成了纯 base64 并进入了工作流上下文**，即平台侧已拿到文件内容，只是 HTTP 节点发不出去：

```
09-02 15:49:42 INFO node_executors.py:156: [Workflow] 开始节点 '开始' 完成,
  inputs={'meeting_file': 'JVBERi0xLjcKJcKzx9gNCjMgMCBvYmoNPDwvQXV0aG9y...', 'mode': 'generic'}
```

（`%PDF-1.7` 的 base64 头，即原始 PDF 文件内容。）

### 3.2 被调用方侧的 422 复现（开发机本地 curl，证明两种失败形态）

```bash
# A. 完全不带 file 字段 —— 与平台节点实际发出的请求等效
curl -X POST http://192.168.30.216:18091/m1/extract -F "mode=generic"
# → 422 {"detail":[{"type":"missing","loc":["body","file"],"msg":"Field required","input":null}]}

# B. file 作为普通文本字段（base64 字符串塞进 form 字段）
curl -X POST http://192.168.30.216:18091/m1/extract \
  -H "Content-Type: multipart/form-data" \
  --form-string "file=2026.8.24信息公司周例会工作安排备忘录.pdf" --form-string "mode=generic"
# → 422 {"detail":[{"type":"value_error","loc":["body","file"],
#         "msg":"Value error, Expected UploadFile, received: <class 'str'>"}]}

# C. 正确的 multipart 文件分片 —— 服务端完全正常
curl -X POST http://192.168.30.216:18091/m1/extract \
  -F "file=@2026.8.24信息公司周例会工作安排备忘录.pdf" -F "mode=generic"
# → 200，81.58 s，138 条事项
```

### 3.3 平台运行日志的查看方式（供管理员复核）

```bash
docker logs --since 1h sxzk-api 2>&1 | grep -E "执行节点|节点 '|工作流执行完成" | cut -c1-200
```

---

## 4. 建议的修复点（仅供参考，开发侧不动平台代码）

集中在 `HttpNodeExecutor.execute()` 一个函数内，改动可控：

```python
body_type = (cfg.bodyType or "none").lower()
raw_body = cfg.body if isinstance(cfg.body, dict) else cfg.extra.get("body")

if isinstance(raw_body, str) and raw_body:
    raw_body = _resolve_template(raw_body, context)   # 修缺陷 2：字符串也做插值
elif isinstance(raw_body, dict):
    raw_body = _resolve_dict(raw_body, context)

kwargs = {"method": method, "url": url, "params": params or None, "headers": headers or None}
if body_type == "json":
    kwargs["json"] = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
elif body_type in ("form-data", "x-www-form-urlencoded"):
    fields = dict(line.split("=", 1) for line in raw_body.splitlines() if "=" in line)
    kwargs["data" if body_type.endswith("urlencoded") else "files"] = <按 fields 组装；
        附件变量值按 base64 解码为 (filename, bytes) 元组>
...
```

另需：`_map_node_config` 不要把字符串 body 判空（`workflow_router.py:121-124`）；`_resolve_template` 支持按 label/displayName 兜底解析（`node_executors.py:64-90`）；输出节点在引用命中失败时返回 failed 而非 `""`（`node_executors.py:487-497`）。

---

## 5. 验收标准（补丁后请管理员按此回归）

1. 新建测试工作流：开始（附件）→ HTTP 请求（`form-data`，body `file={{<startNodeId>.<varName>}}`）→ 输出；指向任一 multipart 回显服务，服务端应收到**文件分片**而非 JSON 字符串。
2. 同一链路把 Body 类型改为 `json`，body 写 `{"a": {{<nodeId>.result}}}`，服务端应收到**JSON 对象**、且占位符已被真实值替换（不再是 `{{}}` 字面量）。
3. 引用写不存在的节点名时，运行应报错，而不是把 `{{...}}` 原样发出。
4. 上游节点 failed 时，`finalOutput` 应包含失败节点与错误原文，不应只得到 `{"result": ""}`。
5. 回归本工作流（ID=3）：`docker logs sxzk-api | grep 工作流执行完成` 应出现 `success=5/5`；随后
   `curl "http://192.168.30.216:18090/m1/stats?source_document_id=<文档ID>"` 应有 138 行；
   重复运行同一文件，行数不变（幂等）。

---

## 6. 在补丁到位之前，开发侧的临时规避思路（不依赖平台改动）

- 文件不进工作流：会议 PDF 由 M1 侧 `cron_scan.py` 目录扫描 + `/m1/ingest-file`（JSON `{"path": ...}`）入库，工作流退化为「触发 + 结果展示」。
- 唯一可用的动态传参通道是 **headers**（`node_executors.py:287` 做了插值），但受网关头大小限制，只能传短字符串（如文档名），不能传文件内容。
- 上述两条都有体验损失，因此仍建议按第 4 节修复节点执行器。
