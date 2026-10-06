# M1 抽取模块交接

当前保留链路为 M1_Extraction → M3 项目语义记忆与检索 → M6 任务生命周期处理。M1 读取完整文档并输出九字段 Item，保留原文证据，不访问任务库，不生成生命周期动作，不对项目名称自行补后缀或归一。

## 数据契约

每条 Item 固定包含 department、work_section、delivery_group、project、item_type、assignee、title、content、evidence。证据页码、字符范围及 exact_match 由程序回对原文计算。输入字段、事项粒度与禁止推断要求以 schemas/m1_items.schema.json 和 prompts/ 为准。

## 入口与验证

- Python 入口：src/cli.py；block 与 generic 模式参见 README.md。
- Dify 入口：dify/m1_workflow.yml；带 M1 staging 尾节点的配置参见 dify/README.md。
- 本地测试：在本目录执行 python -m pytest tests -q。
- 质量分析：eval/compare_annotation.py、eval/batch_regression.py；已知问题参见 KNOWN_ISSUES.md。

M1 的条目应直接交给当前语义服务的 process_m1_file；运行环境、模型和会议材料由部署环境单独配置。历史样例与评测输出不能替代当前端到端验收。
