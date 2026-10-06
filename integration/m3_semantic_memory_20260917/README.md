# 语义主链路工作区

`candidate/` 是当前 M1→M3→M6 实现的位置；M4 招投标分离由 M6 准入阶段调用。主回放入口位于 `candidate/integration/m6_service/native_full_dataset.py`。

`departments.json` 是隔离回放的部门路由示例，只使用 `test://`；不要作为正式派发配置。

展示库导出在仓库根执行，源回放保持只读、目标文件必须不存在：

```bash
EXPORT_SOURCE=/absolute/path/replay EXPORT_TARGET=/absolute/path/new_demo.sqlite \
  python integration/m3_semantic_memory_20260917/export_semantic_demo.py
```

导出读取当前回放的 `summary.json`、`lifecycle.sqlite`，只纳入 `summary.documents` 中已完成的会议。可选 `EXPORT_METADATA` 指向 `{"documents":[{"source_document_id":"...","file_name":"..."}]}`，补充原文件名；未提供时使用来源文档 ID。

`probe_resolver.py` 是手动真实服务探针，会重置所连接的语义记忆，必须使用隔离测试 Neo4j 数据库，不能用于生产记忆库。单元回归不会调用它。

无网络导出回归：

```bash
python -m pytest integration/m3_semantic_memory_20260917/tests/test_demo_export.py -q
```

此次源码清理不构成新的真实数据回放或服务器部署验收。
