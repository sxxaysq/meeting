"""简化版会议待办抽取：读取、分片、一次抽取、按部门/项目归并、写出 JSON。"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "config.json"
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}
STATUS_ORDER = {"进行中": 0, "待执行": 1, "已延期": 2, "已完成": 3}


SYSTEM_PROMPT = """你是煤矿班前会/调度会待办抽取器。只返回合法 JSON：
{"tasks":[{"department":"","project":"","title":"","description":"","work_items":[],"assignee":"","deadline":"","priority":"高|中|低","status":"待执行|进行中|已完成|已延期","evidence":"","confidence":0.0}]}

任务粒度是“部门下的项目、专项或可独立验收的工作目标”，不是一句动作。
1. 同一部门、同一项目/专项中的设计、采购、安装、联调、测试、整改、验收等必须合为一个任务；动作写入 work_items。
2. 不要把进度汇报、问题描述、会议讨论、背景、建议单独变成任务；没有明确后续动作时不输出。
3. department 与 project 必须来自原文；未知填空字符串，绝不猜测。project 是本任务的主体，不能填地点或某个小动作。
4. title 使用“项目/专项 + 当前总体目标”，不要使用“安装XX”“测试XX”等单个环节作为标题。
5. evidence 必须是本片段中的原文短句。confidence 为 0 到 1 的自评置信度；不确定是否构成任务时仍输出候选并给出低于 0.8 的置信度。不要输出 Markdown 或解释。
6. 每个片段最多输出 8 个任务主体；title 不超过 30 字，description 不超过 80 字，evidence 不超过 100 字，work_items 最多 6 项且每项不超过 30 字。"""


def read_document(path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        records = snapshot if isinstance(snapshot, list) else snapshot.get("segments", [])
        if not isinstance(records, list):
            raise RuntimeError("JSON 输入必须是 M1 话轮数组或包含 segments 数组")
        texts = [
            clean_text(item.get("clean_text") or item.get("raw_text"))
            for item in records
            if isinstance(item, dict)
        ]
        return "\n".join(text for text in texts if text)
    if suffix == ".txt":
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        try:
            import fitz
        except ImportError:
            pdftotext = shutil.which("pdftotext")
            if not pdftotext:
                raise RuntimeError(
                    "读取 PDF 需要 PyMuPDF，或系统可用的 pdftotext"
                )
            result = subprocess.run(
                [pdftotext, "-enc", "UTF-8", str(path), "-"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "pdftotext 读取 PDF 失败：" + result.stderr[-300:]
                )
            return result.stdout
        document = fitz.open(path)
        try:
            return "\n".join(page.get_text() for page in document)
        finally:
            document.close()
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            try:
                with zipfile.ZipFile(path) as archive:
                    document_xml = archive.read("word/document.xml")
                root = ET.fromstring(document_xml)
            except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
                raise RuntimeError("DOCX 文件无法读取") from error
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = []
            for paragraph in root.iter(namespace + "p"):
                text = "".join(
                    node.text or "" for node in paragraph.iter(namespace + "t")
                ).strip()
                if text:
                    paragraphs.append(text)
            return "\n".join(paragraphs)
        return "\n".join(p.text for p in Document(path).paragraphs if p.text.strip())
    raise RuntimeError("仅支持 .txt、.pdf、.docx、M1 快照 .json")


def split_text(text, size, overlap):
    text = re.sub(r"\r\n?", "\n", text).strip()
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind(mark, start + size // 2, end) for mark in "。！？；\n")
            if boundary >= start + size // 2:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _http_status(error):
    """Return an HTTP status code from an OpenAI-compatible exception."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_retryable_llm_error(error):
    status = _http_status(error)
    if status is not None:
        return status in {408, 409, 429, 500, 502, 503, 504}
    return error.__class__.__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
    }


def _model_error_message(error, retried=False):
    status = _http_status(error)
    if status is not None:
        message = "模型服务暂时不可用（HTTP {}）".format(status)
    else:
        message = "模型服务连接异常"
    if retried:
        return message + "，已自动重试 3 次，请稍后重新提交。"
    return message + "，请检查模型服务配置后重试。"


def _uses_local_qwen(config):
    model = os.getenv("LLM_MODEL", config["model"])
    base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    return model.lower().startswith("qwen/") or base_url.startswith(
        ("http://127.0.0.1", "http://localhost", "http://192.168.")
    )


def call_llm(chunk, config):
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("需要 openai：pip install -r requirements.txt")
    api_key = os.getenv("LLM_API_KEY", "EMPTY")
    base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    model = os.getenv("LLM_MODEL", config["model"])
    client = OpenAI(api_key=api_key, base_url=base_url)
    # vLLM's Qwen chat template requires this explicit option.
    is_local_qwen = _uses_local_qwen(config)
    if is_local_qwen:
        extra_body = {"chat_template_kwargs": {"enable_thinking": True}}
    else:
        extra_body = {}
    max_tokens = int(config["max_tokens"])
    if is_local_qwen:
        # Long PDF chunks can contain several task units.  The generic 5,000
        # token cap truncates their JSON even with thinking disabled.
        max_tokens = max(max_tokens, 32768)
    user_prompt = "<meeting_chunk>\n" + chunk + "\n</meeting_chunk>"
    last_shape = "not_json"
    for attempt in range(2):
        for request_attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=config["temperature"],
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    extra_body=extra_body,
                )
                break
            except Exception as error:
                if not _is_retryable_llm_error(error):
                    raise RuntimeError(_model_error_message(error)) from error
                if request_attempt == 2:
                    raise RuntimeError(
                        _model_error_message(error, retried=True)
                    ) from error
                time.sleep(2 ** request_attempt)
        content = response.choices[0].message.content or ""
        reasoning = getattr(response.choices[0].message, "reasoning_content", "") or ""
        last_shape = "content_len={}, reasoning_len={}, finish_reason={}".format(
            len(content), len(reasoning), response.choices[0].finish_reason
        )
        try:
            parsed = json.loads(content or "{}")
            if isinstance(parsed.get("tasks"), list):
                return parsed["tasks"]
            last_shape += "; keys={}, tasks_type={}".format(
                sorted(parsed) if isinstance(parsed, dict) else type(parsed).__name__,
                type(parsed.get("tasks")).__name__ if isinstance(parsed, dict) else "missing",
            )
        except json.JSONDecodeError:
            pass
        user_prompt += "\n上次输出无效。只返回 {\"tasks\": [...]}，tasks 必须是数组。"
    raise RuntimeError("模型连续两次未返回 tasks 数组 ({})".format(last_shape))


def clean_text(value):
    return value.strip() if isinstance(value, str) else ""


def clean_list(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        item = clean_text(item)
        if item and item not in result:
            result.append(item)
    return result


def clean_confidence(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return None


def normalize_task(value):
    if not isinstance(value, dict):
        return None
    task = {
        "department": clean_text(value.get("department")),
        "project": clean_text(value.get("project")),
        "title": clean_text(value.get("title")),
        "description": clean_text(value.get("description")),
        "work_items": clean_list(value.get("work_items")),
        "assignee": clean_text(value.get("assignee")),
        "deadline": clean_text(value.get("deadline")),
        "priority": clean_text(value.get("priority")) or "中",
        "status": clean_text(value.get("status")) or "待执行",
        "evidence": clean_text(value.get("evidence")),
        "confidence": clean_confidence(value.get("confidence")),
    }
    if not task["title"] or not task["evidence"]:
        return None
    if task["priority"] not in PRIORITY_ORDER:
        task["priority"] = "中"
    if task["status"] not in STATUS_ORDER:
        task["status"] = "待执行"
    if not task["work_items"] and task["description"]:
        task["work_items"] = [task["description"]]
    return task


def subject_key(task):
    # 项目是主键；没有项目时，不能猜测合并关系，只按标题保守保留。
    subject = task["project"] or task["title"]
    return task["department"], subject


def merge_tasks(candidates, limit):
    merged = {}
    for candidate in candidates:
        task = normalize_task(candidate)
        if not task:
            continue
        key = subject_key(task)
        if key not in merged:
            merged[key] = task
            continue
        current = merged[key]
        current["work_items"] = clean_list(
            current["work_items"] + task["work_items"] + [task["description"]]
        )
        if task["evidence"] and task["evidence"] not in current["evidence"]:
            current["evidence"] += "\n" + task["evidence"]
        if PRIORITY_ORDER[task["priority"]] < PRIORITY_ORDER[current["priority"]]:
            current["priority"] = task["priority"]
        if STATUS_ORDER[task["status"]] < STATUS_ORDER[current["status"]]:
            current["status"] = task["status"]
        if not current["assignee"]:
            current["assignee"] = task["assignee"]
        if not current["deadline"]:
            current["deadline"] = task["deadline"]
        if task["confidence"] is not None:
            current["confidence"] = min(
                current["confidence"] if current["confidence"] is not None else 1.0,
                task["confidence"],
            )
    tasks = list(merged.values())
    tasks.sort(key=lambda item: (PRIORITY_ORDER[item["priority"]], item["department"], item["project"], item["title"]))
    if limit is None:
        return tasks, []
    return tasks[:limit], tasks[limit:]


def main():
    parser = argparse.ArgumentParser(description="简化版会议待办抽取")
    parser.add_argument("input", type=Path, help="输入 TXT、PDF 或 DOCX")
    parser.add_argument("--output", "-o", type=Path, help="输出 JSON 路径")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="仅显示分片数，不调用模型")
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit("输入文件不存在：{}".format(args.input))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    text = read_document(args.input)
    chunks = split_text(text, config["chunk_chars"], config["chunk_overlap_chars"])
    if args.dry_run:
        print("chunks={}".format(len(chunks)))
        return

    candidates = []
    for index, chunk in enumerate(chunks, start=1):
        print("抽取分片 {}/{}".format(index, len(chunks)))
        candidates.extend(call_llm(chunk, config))
    tasks, review_candidates = merge_tasks(
        candidates, config.get("max_tasks_per_document")
    )
    result = {
        "source_file": args.input.name,
        "task_count": len(tasks),
        "tasks": tasks,
        "review_candidates": review_candidates,
        "note": "任务按 department + project 归并；未设置任务数量上限。",
    }
    output = args.output or (ROOT / "data_output" / (args.input.stem + ".tasks.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已保存 {} 条任务至 {}".format(len(tasks), output))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print("错误：{}".format(error), file=sys.stderr)
        sys.exit(1)
