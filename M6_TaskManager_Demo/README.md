# M6 会议任务数据库自动维护演示

该工程复用远端现有 M1、M2 CLI，并新增：

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
cd /home/yty/M6_TaskManager_Demo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 启动

```bash
cd /home/yty/M6_TaskManager_Demo
.venv/bin/uvicorn app.asgi:app --host 127.0.0.1 --port 8088
```

演示电脑：

```bash
ssh -L 8088:127.0.0.1:8088 yty@192.168.30.214
```

浏览器访问 `http://127.0.0.1:8088`。

## 关键环境变量

```text
M6_DATABASE_PATH
M6_RUNS_DIR
M6_M1_PROJECT
M6_M2_PROJECT
M6_PIPELINE_PYTHON
M6_LLM_BASE_URL
M6_LLM_MODEL
M6_LLM_API_KEY
M6_LLM_TIMEOUT_SECONDS
M6_PIPELINE_TIMEOUT_SECONDS
M6_MAX_UPLOAD_BYTES
M6_MAX_TASK_CANDIDATES
M6_SEED_DEMO_DATA
M6_ALLOW_DECISION_CREATE
```

默认值适配 `node2` 当前目录和内网 Qwen 服务。模型不会直接执行 SQL；所有数据库
写入都由固定 repository 和事务执行器完成。取消或删除统一使用软删除。
