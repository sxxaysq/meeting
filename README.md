# Meeting Task Manager

会议材料抽取、项目语义记忆、任务生命周期判断及人工复核系统。

本分支 snapshot/latest-20261005 保存 2026-10-05 从 216 服务器 /home/yty-s/meeting-m2-work 读取的最新源码。它基于仓库已有的 baseline-m1-m2-m3-m6-integration 提交 890ddf4，包含服务器尚未提交的更新。

## 当前代码入口

| 功能 | 路径 |
| --- | --- |
| M1 文档抽取与原文证据 | M1_Extraction/ |
| M3 项目语义记忆与历史任务检索 | integration/m3_semantic_memory_20260917/candidate/M3_KnowledgeGraph/ |
| M6 生命周期判断、校验与事务执行 | integration/m3_semantic_memory_20260917/candidate/M6_TaskManager/ |
| 语义批处理入口 | integration/m3_semantic_memory_20260917/candidate/integration/m6_service/native_full_dataset.py |
| 结果导出 | integration/m3_semantic_memory_20260917/export_semantic_demo.py |
| 页面、人工复核、组织与部门工作台 | M6_TaskManager_Demo/ |
| HTTP 服务及 M4 招投标旁路 | integration/ 下的 m1_service、m1_staging、m2_service、m4_bidding、m6_service |

当前语义批处理采用 M1 → M3 → M6，M4 在准入阶段旁路处理。语义版仍保留在上述 candidate 目录；根目录 M3_KnowledgeGraph/、M6_TaskManager/ 是较早实现，不应当作语义版入口。

M6_TaskManager_Demo 提供已有结果查询和人工处理。其旧上传编排尚未接入完整语义批处理，不应据页面在线推断新材料上传推理已闭环。

## 运行与配置

各模块的 README、requirements、配置模板和测试随源码保留。模型、向量服务、Neo4j、运行数据库及真实会议输入需要按目标环境单独配置；服务器路径和内网地址可能需要调整。

- [页面使用说明](M6_TaskManager_Demo/USER_GUIDE.md)
- [内网部署说明](M6_TaskManager_Demo/deploy/README.md)
- [集成交接记录](integration/HANDOFF.md)
- [M1 抽取说明](M1_Extraction/README.md)
- [旧版根目录 README](docs/README_legacy.md)

本次上传排除本地环境文件、密钥、运行数据库、缓存、备份、原始会议数据集和新增运行结果。原基线已跟踪的样例与评测工件继续保留。部署环境文件须在目标机器上自行创建。

## 本次核验范围

上传前核对服务器文件哈希，并检查 Python 语法和 JSON 格式。本次仅同步项目，未重新运行模型服务或端到端回放；历史回放记录不代表全部现行源码已重新验收。
