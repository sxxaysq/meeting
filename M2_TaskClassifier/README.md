# M2：煤矿会议任务候选分类器（提示词基线）

M2 直接读取 M1 的 `clean_segments.json`，以当前片段及同一会议内相邻各一条片段为窗口，调用现有 OpenAI 兼容模型完成七分类，并输出 `m2.v1` 结构化结果。当前没有足够标注样本，因此这是零样本提示词基线，不是已训练分类模型，也不能据此宣称达到生产验收指标。

## 边界

- 主标签仅允许 `task`、`task_update`、`decision`、`issue`、`report`、`suggestion`、`non_task`；
- `task/task_update/decision/issue` 必须进入 `EXTRACT` 或 `HUMAN_REVIEW`；
- 弱规则只能提高召回路由，不能直接生成正式待办；
- evidence 必须是 M1 `clean_text` 的原文切片，原始 ASR 仍通过 `segment_id` 回溯到 M1 `raw_text`；
- 模型若沿用 M1 的 `text/reason` 歧义字段，只允许无损归一化为 M2 的 `field/description`，不会据此改写业务语义；
- 证据区间错误时，仅当 evidence 在 `clean_text` 中精确唯一匹配，或仅空白折叠后唯一匹配，才恢复为 M1 原文及正确区间；恢复动作写入审计；
- 不猜测 M1 未提供的会议日期、会议类型、人员、地点或截止时间；
- 无效模型响应只修复一次，仍失败的片段写入人工复核队列，命令返回退出码 2。

## 目录

- `prompts/m2_system_prompt.txt`：固定零样本系统提示词；
- `config/m2_output.schema.json`：批量输出 JSON Schema；
- `src/prompt_builder.py`：M1 到 M2 短窗口和提示词；
- `src/response_validator.py`：Schema、标签、证据和高召回路由校验；
- `src/classifier.py`：调用、一次修复与复核降级；
- `src/main.py`：命令行入口；
- `tests/`：不访问外部模型的自动化测试。

## 配置

默认配置连接本机 OpenAI Chat Completions 兼容 Qwen 服务，不包含有效 API Key：

```yaml
llm:
  api_key: EMPTY
  base_url: http://127.0.0.1:8000/v1
  model: Qwen/Qwen3.6-35B-A3B
```

也可通过 `M2_API_KEY`、`M2_BASE_URL`、`M2_MODEL` 环境变量覆盖。审计文件记录模型响应和 token 用量，但不会记录密钥。

默认 `max_tokens=4096`，用于避免结构化 JSON 在过小输出上限处被截断，不代表允许模型输出长解释。M1 当前仍有少量超长混合话轮，后续应在 M1 继续改进原子语义切分。

## 运行

```bash
cd /home/yty/M2_TaskClassifier
/home/yty/myvenv/bin/python src/main.py
```

小规模 smoke test，输出到独立目录：

```bash
/home/yty/myvenv/bin/python src/main.py \
  --limit-segments 3 \
  --output-dir data/output/smoke
```

定点回归一个或多个片段：

```bash
/home/yty/myvenv/bin/python src/main.py \
  --segment-id seg-0003 \
  --output-dir data/output/segment_regression
```

测试：

```bash
/home/yty/myvenv/bin/python -m unittest discover -s tests -v
```

## 输出

- `m2_classifications.json`：通过契约校验的分类结果；
- `m2_model_audit.json`：每次生成、修复、校验错误及路由覆盖记录；
- `m2_review_queue.json`：修复后仍无效的片段及 M1 原始证据。

当 `m2_review_queue.json` 非空时，主命令返回退出码 2，避免编排器把不完整批次误判为成功。

## 当前验收口径

当前可验收的是接口、提示词、安全边界、证据校验、一次修复、复核降级和 M1→M2 链路可运行。候选召回率、Macro-F1、分类阈值和校准都必须等片段级金标数据形成后评测；之后应以轻量中文 Encoder 为默认生产方案，提示词链路保留为基线或灰区复核方案。
