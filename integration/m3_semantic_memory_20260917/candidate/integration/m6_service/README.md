# 当前语义批处理入口

此目录保留 `native_full_dataset.py`：严格 M1 九字段输入 → M3 项目语义记忆和历史任务检索 → M6 生命周期判断、校验、执行及复核。招投标由准入阶段路由至 M4。

从仓库根执行：

```bash
python integration/m3_semantic_memory_20260917/candidate/integration/m6_service/native_full_dataset.py \
  --inputs /absolute/path/frozen_m1 \
  --departments integration/m3_semantic_memory_20260917/departments.json \
  --output /absolute/path/new_replay
```

输入目录包含 `E2E*/m1.json`。每个 JSON 是 `{"items": [...]}`，可带 `mode` 和 `source_document_id`；条目字段和证据必须符合 M1 Schema。输入来源、项目分组和幂等身份都直接来自 M1；程序将 `candidate/` 加入导入路径。

运行前安装本目录 `requirements.txt`，配置 `M6_LLM_BASE_URL`、`M6_LLM_MODEL` 和 M3 的 embedding、rerank、Neo4j 环境变量。批处理默认重置所连接的语义记忆图，必须为回放提供独立测试 Neo4j 数据库；输出使用全新目录。执行结果仅写本次 SQLite/审计产物和 `test://` 派发队列。

产物包括 `lifecycle.sqlite`、逐会议 `m6.json`、`summary.json`、`records.jsonl`、`memory_snapshot.json` 和进度/失败信息。这是源码批处理入口；没有提供本目录 HTTP 服务或平台工作流，也没有在此次仓库清理中启动真实模型推理。

无网络回归：

```bash
python -m pytest integration/m3_semantic_memory_20260917/candidate/integration/m6_service/test_native_groups.py -q
```
