# 顿悟智体平台配置清单（交人类管理员执行）

> 红线提醒：开发侧对顿悟平台**全程只读**（仅登录浏览与只读 API 查询），
> 本清单所有写操作（导入、配置、发布）均由管理员在平台上执行。
> 调研证据与决策门结论见 `acceptance_report.md`。

## 0. 决策门结论摘要（2026-08-20 只读调研）

- 顿悟"工作流编排"是**平台自研画布**（后端 API：`GET /dev-api/yuxi/api/workflow`，现存 1 个草稿工作流），
  **没有 Dify DSL 导入入口**（前端 JS 中无任何 "导入/Import/DSL" 字样）。
- 节点面板类型清单：`start / llm(LLM) / output(输出) / condition(条件分支) / loop(循环) /
  http(HTTP 请求，支持 {{变量}} 插值) / docExtractor(文档提取器) / paramExtractor(参数提取器) /
  code(代码执行，沙箱 JS/Python) / iteration(迭代) / knowledge(知识检索)`。
- 结论：**Track B（编排壳 + 本地执行服务）为主**；Track A 的 DSL 产物
  `M1_Extraction/dify/m1_workflow_dunwu.yml` 作为 Dify 原生实例可用时的备选交付物保留。

---

## 1. Track B（主轨）：顿悟工作流编排清单

在顿悟"智能体管理 → 工作流编排"新建工作流，按以下节点顺序拖拽连线
（节点类型均已确认平台支持）：

| 序 | 节点类型 | 配置要点 |
|---|---|---|
| 1 | start（开始） | 输入变量：`meeting_file`（文件，PDF/DOCX/TXT）；可选 `mode`（默认 block） |
| 2 | http（HTTP 请求） | POST `http://192.168.30.214:8091/m1/extract`（multipart：`file`=开始节点文件，`mode` 表单字段）；超时建议 ≥600s（抽取耗时随文档长度线性增长） |
| 3 | code（代码执行） | 九字段复核：校验返回 JSON 的 `items` 每个对象恰含 9 个字段、`item_type` 四值枚举、`evidence.start_char<=end_char`；不合规则抛错终止（不静默修正） |
| 4 | http（HTTP 请求） | POST `http://192.168.30.214:8090/m1/ingest`，JSON body：`{"source_document_id","file_name","meeting_date","mode","items"}`（items 取节点 2 返回值）；服务端 422 即视为失败 |
| 5 | output（输出） | 返回 ingest 结果或 items |

要点：
- 节点 2 与节点 4 的目标 IP 为本开发机 `192.168.30.214`，需确认平台服务器（192.168.30.216）
  到该地址 8090/8091 端口的防火墙放行；
- 该链路保留真实页码（m1-service 走 PyMuPDF），是 Track A 页码 null 限制的补集；
- m1-service 启动方式（开发机用户态，无 systemd/docker）：
  `bash /home/yty/m1x/integration/m1_service/start_m1_service.sh start`；
  staging 服务：`bash /home/yty/m1x/integration/m1_staging/start_staging.sh start`。

备选：把 m1-service 注册进"插件市场/插件管理"（对齐平台 `query_data` 工具范式），
由智能体以工具方式调用，URL 同上。

## 2. Track A（备选）：Dify DSL 导入（仅当未来部署 Dify 原生实例时适用）

> 顿悟现有工作流体系**不是 Dify**、无导入入口；以下仅在管理员另行部署 Dify
> 或顿悟后续版本提供 DSL 导入时执行。

1. **模型服务**（插件工具 → 模型服务）：
   - 供应商：`openai_api_compatible`；
   - API Base（二选一）：
     - 直连 vLLM：`http://192.168.30.215:8000/v1`（模型需能透传
       `chat_template_kwargs.enable_thinking=false`，M1 客户端已带该参数）；
     - **禁思考兜底代理（推荐）**：`http://192.168.30.214:8002/v1`，
       开发机启动命令 `bash /home/yty/m1x/integration/start_no_think_proxy_8002.sh start`
       （监听 8002，转发 192.168.30.215:8000，注入禁思考指令并剥离推理块）；
   - 模型名（已实测核对 `GET /v1/models`）：`Qwen/Qwen3.6-35B-A3B`。
2. **导入 DSL**：工作室 → 导入 DSL 文件 → `M1_Extraction/dify/m1_workflow_dunwu.yml`
   （由 `build_workflow.py --tail-ingest-url http://192.168.30.214:8090/m1/ingest` 生成，
   已含尾 HTTP 节点"写入 staging"；Item Extractor 节点选定上述模型）。
3. **沙箱参数**：`CODE_MAX_STRING_LENGTH=2000000`（docker/.env 修改后
   `docker compose up -d dify-sandbox dify-api`；若平台不适用 docker 部署，
   请以平台等价的沙箱配置项调整，目标值不变）。
4. **触发方式**（三选一）：
   - UI 运行：上传会议文件直接运行；
   - API（Dify 兼容）：先 `POST /v1/files/upload` 取 `upload_file_id`，再
     `POST /v1/workflows/run`（`response_mode=blocking`，inputs 含
     `meeting_file`、可选 `file_name/meeting_date`），curl 模板沿用
     `M1_Extraction/dify/README.md`；
   - 注册为智能体工具供会话调用。
5. **已知限制（不要"修复"）**：该链路 `evidence.page_start/page_end` 恒为 null；
   需要真实页码请走 Track B。

## 3. 验证方法（管理员执行后）

1. 运行工作流上传一份会议 PDF；
2. `curl http://192.168.30.214:8090/m1/stats?source_document_id=<文档ID>` 应出现对应行数；
3. 重复运行同一文件，行数不应增加（幂等）。
