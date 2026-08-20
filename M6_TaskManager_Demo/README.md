# M6 会议任务数据库自动维护演示

当前上传主链路已切换为：

```text
M1_Extraction --mode generic
  → M2_SemanticConsolidator --source-mode generic
  → M6_TaskManager
  → 原任务管理前端 API
```

旧 `M1_Preprocess → M2 task gate → M1.5 → 旧 M6 importer` 代码仅保留作回滚参考，
不再由 `app/orchestrator.py` 调用。新 M6 使用独立 `data/lifecycle.db` 保存权威状态，
再由仓储兼容层投影为原前端 API Schema；原页面 HTML/CSS/布局保持不变。

该工程提供：

- M6 结构化数据库命令生成；
- 唯一目标任务匹配；
- JSON Schema、证据、字段和版本校验；
- SQLite 参数化事务执行；
- 幂等事件键和软删除；
- FastAPI 编排与查询接口；
- Vue 3 单页任务与审计展示。

## 本地测试

```bash
python -m unittest discover -s tests -v
python -m scripts.smoke_m6
python -m scripts.smoke_live_model
```

## 远端安装

```bash
cd /home/yty/m1x/meeting-m6-work/M6_TaskManager_Demo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 启动

```bash
cd /home/yty/m1x/meeting-m6-work/M6_TaskManager_Demo
M6_PIPELINE_PYTHON=/home/yty/m1x_venv/bin/python \
LLM_MAX_TOKENS=65536 \
/home/yty/myvenv/bin/python -m uvicorn app.asgi:app --host 127.0.0.1 --port 8088
```

演示电脑：

```bash
ssh -L 8088:127.0.0.1:8088 yty@192.168.30.214
```

浏览器访问 `http://127.0.0.1:8088`。

0407 / 0413 的新 M6 结果直接在原任务清单、部门视图、会议信息、人工复核和操作记录
页面展示；会议下拉筛选沿用原交互。

## 关键环境变量

```text
M6_DATABASE_PATH
M6_RUNS_DIR
M6_M1_PROJECT
M6_M2_PROJECT
M6_M6_PROJECT
M6_PIPELINE_PYTHON
M6_LIFECYCLE_DATABASE_PATH
M6_DEPARTMENTS_PATH
M6_LLM_BASE_URL
M6_LLM_MODEL
M6_LLM_API_KEY
M6_LLM_TIMEOUT_SECONDS
M6_PIPELINE_TIMEOUT_SECONDS
M6_MAX_UPLOAD_BYTES
M6_MAX_TASK_CANDIDATES
M6_SEED_DEMO_DATA
M6_ALLOW_DECISION_CREATE
LLM_MAX_TOKENS
```

默认值适配 `node2` 当前目录和内网 Qwen 服务。模型不会直接执行 SQL；所有数据库
写入都由固定 repository 和事务执行器完成。取消或删除统一使用软删除。
