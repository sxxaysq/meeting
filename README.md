# Meeting Task Manager

煤矿班前会／调度会材料到项目级任务待办的演示系统。

## 当前主链路

```text
M1_Preprocess
  文档读取、字符分片、LLM 项目级任务候选抽取
    ↓
M2_TaskClassifier/src/m1_task_gate.py
  字段、状态和置信度准入门控
    ↓
M1_5_ProjectNormalizer
  项目主表和别名归并，补充 project_id
    ↓
M6_TaskManager_Demo
  历史任务匹配、CREATE/UPDATE/REVIEW、校验、事务执行和审计
```

## 目录

- `M1_Preprocess/`：会议材料读取、分片和项目级任务候选抽取。
- `M2_TaskClassifier/`：当前 task gate，以及保留的七分类提示词基线。
- `M1_5_ProjectNormalizer/`：项目主数据、别名和项目归并。
- `M6_TaskManager_Demo/`：FastAPI、前端、SQLite Schema、任务历史匹配和事务执行。

## 安装

建议为各模块创建独立 Python 虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r M6_TaskManager_Demo/requirements.txt
pip install -r M1_Preprocess/requirements.txt
pip install -r M2_TaskClassifier/requirements.txt
pip install -r M1_5_ProjectNormalizer/requirements.txt
```

模型服务使用 OpenAI Chat Completions 兼容接口。仓库不包含任何 API 凭据，默认模型
配置为本机 Qwen 兼容服务；需要认证时通过环境变量配置。

## 运行

```bash
cd M6_TaskManager_Demo
uvicorn app.main:app --host 0.0.0.0 --port 8016
```

主要环境变量：

```text
M6_LLM_BASE_URL
M6_LLM_MODEL
M6_LLM_API_KEY
M6_DATABASE_PATH
M6_RUNS_DIR
M6_PIPELINE_PYTHON
```

## 测试

```bash
cd M1_5_ProjectNormalizer
PYTHONPATH=src python -m unittest discover -s tests -v

cd ../M2_TaskClassifier
python -m unittest discover -s tests -v

cd ../M6_TaskManager_Demo
python -m unittest discover -s tests -v
```

## 未包含内容

仓库只包含源码、配置模板、提示词、前端、迁移脚本和测试，不包含：

- 会议原文、训练样本或其他数据集；
- SQLite 数据库及备份；
- 模型权重、checkpoint 或向量索引；
- 运行产物、日志和上传文件；
- 虚拟环境、wheelhouse 和依赖压缩包；
- API Key、Token、私钥或其他凭据。
