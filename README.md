# Meeting Task Manager

煤矿班前会／调度会材料到项目级任务待办的演示系统。

## 当前主链路

```text
M1_Extraction
  高召回抽取固定九字段 Meeting Item
    ↓
M2_SemanticConsolidator
  项目实体归一、同会议保守归并、PASS/REVIEW/ERROR
    ↓
M6_TaskManager
  历史候选召回、LLM 生命周期判断、确定性校验、事务写库与部门分发
```

## 目录

- `M1_Extraction/`：当前会议九字段业务 Item 抽取。
- `M2_SemanticConsolidator/`：项目实体归一、同会议归并与质量门。
- `M6_TaskManager/`：新 Task Lifecycle Manager 主线。
- `M1_Preprocess/`、`M2_TaskClassifier/`、`M1_5_ProjectNormalizer/`、
  `M6_TaskManager_Demo/`：旧链路，保留作回滚和 baseline，不再是新主线。

## 安装

建议使用 Python 3.11，并为模块创建虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r M1_Extraction/requirements.txt
pip install -r M2_SemanticConsolidator/requirements.txt
pip install -r M6_TaskManager/requirements.txt
```

模型服务使用 OpenAI Chat Completions 兼容接口。仓库不包含任何 API 凭据，默认模型
配置为本机 Qwen 兼容服务；需要认证时通过环境变量配置。

## 运行

M6 当前提供批处理 CLI。先初始化 Department Master 和历史任务，再处理一份 M2 PASS
输出：

```bash
cd M6_TaskManager
python -m src.cli init-db --database data/m6.db \
  --departments db/departments.example.json
python -m src.cli import-history examples/history.sample.json \
  --database data/m6.db
python -m src.cli process examples/m2_pass.sample.json \
  --database data/m6.db --output out/m6_result.json
```

主要环境变量：

```text
LLM_BASE_URL
LLM_MODEL
LLM_API_KEY
LLM_ENABLE_THINKING
LLM_TIMEOUT
```

## 测试

```bash
cd M1_Extraction && python -m pytest tests -q
cd ../M2_SemanticConsolidator && python -m pytest tests -q
cd ../M6_TaskManager && python -m pytest tests -q
```

## 未包含内容

仓库只包含源码、配置模板、提示词、前端、迁移脚本和测试，不包含：

- 会议原文、训练样本或其他数据集；
- SQLite 数据库及备份；
- 模型权重、checkpoint 或向量索引；
- 运行产物、日志和上传文件；
- 虚拟环境、wheelhouse 和依赖压缩包；
- API Key、Token、私钥或其他凭据。
