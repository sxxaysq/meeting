# 会议语义结果查询与人工复核

本工程展示当前 M1→M3→M6 语义批处理导出的任务、会议、审计和复核结果，并提供人工处理功能：

- 查询任务、会议来源、原文证据和变更历史；
- 人工新增、编辑、软删除任务；
- 保存复核表单、生成可编辑建议并人工确认执行；
- 组织机构和部门工作台；
- SQLite 事务、版本检查、幂等和证据校验；
- 训练样本导出。

网页自动上传尚未接入当前语义批处理。页面显示不可用说明，`POST /api/demo/runs` 返回 HTTP 503，不保存文件或创建运行。此目录不包含自动抽取编排、分类导入器或旧目标匹配器。

新材料应通过 [当前语义批处理](../integration/m3_semantic_memory_20260917/README.md) 生成结果，执行结果导出后，让 `M6_DATABASE_PATH` 指向展示库。已有运行的查询接口仍用于读取保存的结果。

## 本地验证

```bash
python -m pip install -r requirements.txt
python -m pip install httpx
python -m unittest discover -s tests -v
```

安装 Node.js 的环境可运行 `node tests/test_*_ui.cjs` 对应的每个前端回归脚本。

## 启动

```bash
python -m uvicorn app.asgi:app --host 127.0.0.1 --port 18098
```

默认展示库为 `data/demo.db`，默认不生成演示种子数据。详见 [使用手册](USER_GUIDE.md) 和 [内网部署说明](deploy/README.md)。

## 配置

| 环境变量 | 用途 |
| --- | --- |
| `M6_DATABASE_PATH` | 当前语义结果展示库 |
| `M6_RUNS_DIR` | 本地结果工作目录；网页上传不会创建运行 |
| `M6_STATIC_DIR` | 静态资源目录 |
| `M6_LLM_BASE_URL` | 人工复核建议模型接口 |
| `M6_LLM_MODEL` | 人工复核建议模型名 |
| `M6_LLM_API_KEY` | 模型认证值，通过环境变量配置 |
| `M6_LLM_TIMEOUT_SECONDS` | 复核建议请求超时 |
| `M6_SEED_DEMO_DATA` | 是否为测试生成种子任务；生产应为 false |

模型仅生成可编辑的复核建议。写入由人工确认后，经固定校验器和事务执行器处理；取消或删除使用软删除。

现有展示库中的历史计数列保留以兼容结果导出及既有数据，不启用历史分类处理能力。
