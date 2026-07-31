# 简化版 M1：会议待办抽取

只有一个源码文件：`src/main.py`。流程是：读取 TXT/PDF/DOCX 或旧 M1 `clean_segments.json` 快照 → 按长度分片 → 每片调用一次模型 → 按 `部门 + 项目` 合并 → 输出 JSON。

`work_items` 保存设计、安装、联调、测试等小事件；它们不单独提交为任务。任务数不设硬上限，只由“部门 + 项目”主体归并结果决定。

```bash
pip install -r requirements.txt
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_MODEL=Qwen/Qwen3.6-35B-A3B
# 仅当模型服务要求认证时设置：
# export LLM_API_KEY=...
python src/main.py meeting.pdf --output data_output/meeting.tasks.json
```

默认连接本机 OpenAI Chat Completions 兼容服务，不包含任何 API 凭据。

只检查分片、不会调用模型：

```bash
python src/main.py meeting.pdf --dry-run
```
