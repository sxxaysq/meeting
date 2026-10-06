# Meeting Task Manager

会议材料抽取、项目语义记忆、任务生命周期判断及人工复核系统。

本分支 snapshot/latest-20261005 的源码来自 216 服务器 2026-10-05 快照，2026-10-06 清理了历史处理链路、兼容输入适配器和已废弃的调用入口。当前只保留 M1 → M3 → M6 语义处理，M4 在准入阶段旁路处理招投标事项。

## 当前代码入口

| 功能 | 路径 |
| --- | --- |
| M1 文档抽取与原文证据 | M1_Extraction/ |
| M3 项目语义记忆与历史任务检索 | integration/m3_semantic_memory_20260917/candidate/M3_KnowledgeGraph/ |
| M6 生命周期判断、校验与事务执行 | integration/m3_semantic_memory_20260917/candidate/M6_TaskManager/ |
| 语义批处理入口 | integration/m3_semantic_memory_20260917/candidate/integration/m6_service/native_full_dataset.py |
| 结果导出 | integration/m3_semantic_memory_20260917/export_semantic_demo.py |
| 结果查询、人工复核、组织与部门工作台 | M6_TaskManager_Demo/ |
| 文档抽取服务及 M4 招投标旁路 | integration/m1_service/、m1_staging/、m1_web/、m4_bidding/ |

完整语义实现保留在上述 candidate 目录。业务处理入口只接受当前 M1 九字段 Item，抽取后直接进行项目解析、历史候选检索、生命周期判断、规则校验和事务执行。

## 页面与批处理边界

页面保留已有结果查询、任务编辑和人工复核。页面的新文档自动处理入口暂不可用，API 明确返回 503；前端不再提供历史上传编排。处理新材料需运行上述 M1 抽取和语义批处理，再导出结果。在线语义上传编排及人工处理与 native 库的统一写入仍需另行接入和验收。

本次清理仅更新此 GitHub 分支。服务器现有服务、数据库与文件未随本次提交部署。

## 运行与配置

模型、向量服务、Neo4j、运行数据库及真实会议输入需要按目标环境单独配置。模块 README、requirements、配置模板及测试随源码保留；服务器路径和内网地址需按部署环境调整。

- [页面使用说明](M6_TaskManager_Demo/USER_GUIDE.md)
- [内网部署说明](M6_TaskManager_Demo/deploy/README.md)
- [当前集成入口](integration/README.md)
- [M1 抽取说明](M1_Extraction/README.md)
- [语义 M3](integration/m3_semantic_memory_20260917/candidate/M3_KnowledgeGraph/README.md)
- [语义 M6](integration/m3_semantic_memory_20260917/candidate/M6_TaskManager/README.md)

本分支不包含本地环境文件、密钥、运行数据库、缓存、备份、原始会议数据集和新增运行结果。原基线已跟踪的样例与评测工件继续保留，历史结果不能替代当前功能验收。
