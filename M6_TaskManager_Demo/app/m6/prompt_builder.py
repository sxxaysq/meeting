"""M6 数据库命令提示词构建。"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """你是煤矿会议任务数据库命令生成器。

你只能根据当前 M2 记录、会议元数据以及允许的唯一目标任务生成结构化 JSON。

严格规则：
1. 只输出一个 JSON 对象，不输出 Markdown、解释或思维过程。
2. db_action 只能是 CREATE、UPDATE_FIELDS、UPDATE_STATUS、SOFT_DELETE、NOOP。
3. task 表示明确新任务时使用 CREATE。
4. task_update 只有在 unique_target_task_id 非空时才能删改；否则必须 NOOP。
5. “完成”使用 UPDATE_STATUS/completed；“受阻”使用 UPDATE_STATUS/blocked。
6. “取消、不再执行、删除该任务”使用 SOFT_DELETE；数据库执行器只会软删除。
7. 不得输出 SQL，不得创造输入中不存在的 task_id。
8. 不得把纯汇报、问题、建议或背景变成任务。
9. 没有明确责任主体、时间或字段时填 null，不得猜测。
10. evidence.text 必须逐字复制当前 source.text 中直接支持该命令的片段。
11. “任务”的最小粒度是具有统一业务目标、统一验收结果、统一责任主体和共同生命周期的工作单元。设计、研发、测试、修复、联调等动作若共同服务于同一项目/平台目标，只能生成一个 CREATE；这些动作写入 work_items，不得按动作词拆成多个任务。
12. 只有业务目标或可独立验收的最终交付物不同，或责任主体/状态/生命周期需要独立流转时，才可输出多个 CREATE，最多 4 条。冒号前已有项目、平台或系统名称时，默认将其作为一个业务目标。
13. task_patch 必须始终包含 title、description、work_items、assignee_raw、deadline_raw、status 六个键。work_items 是工作环节字符串数组；无工作环节时填 null。
14. CREATE 的 target_task_id/expected_version 必须为 null。
15. UPDATE/DELETE 必须使用 unique_target_task_id 及该候选的 version。
16. NOOP 的 target_task_id、expected_version 和全部 task_patch 值必须为 null。
"""


def build_user_prompt(
    meeting: dict,
    source: dict,
    match_context: dict,
) -> str:
    payload = {
        "schema_version": "m6_db_command_v1",
        "meeting": meeting,
        "source": source,
        "task_candidates": match_context.get("candidates", []),
        "unique_target_task_id": match_context.get("unique_target_task_id"),
        "match_mode": match_context.get("match_mode"),
        "allowed_actions": [
            "CREATE",
            "UPDATE_FIELDS",
            "UPDATE_STATUS",
            "SOFT_DELETE",
            "NOOP",
        ],
        "required_output_shape": {
            "schema_version": "m6_db_command_v1",
            "source_key": (
                f"{meeting['meeting_id']}:{source['segment_id']}:"
                f"{source['subsegment_id']}"
            ),
            "commands": [
                {
                    "command_index": 1,
                    "db_action": "CREATE|UPDATE_FIELDS|UPDATE_STATUS|SOFT_DELETE|NOOP",
                    "target_task_id": None,
                    "expected_version": None,
                    "task_patch": {
                        "title": None,
                        "description": None,
                        "work_items": None,
                        "assignee_raw": None,
                        "deadline_raw": None,
                        "status": None,
                    },
                    "evidence": {
                        "segment_id": source["segment_id"],
                        "subsegment_id": source["subsegment_id"],
                        "text": source["text"],
                    },
                    "reason_code": "UPPER_SNAKE_CASE",
                    "ambiguities": [],
                }
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
