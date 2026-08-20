# -*- coding: utf-8 -*-
"""把 M2 实际做出的合并全部导出，供逐条人工核对。

**为什么需要这个脚本**：人工标注里没有一个"同一业务目标被 M1 拆开"的正例
（见 ``gold_merge_groups.py`` 顶部），所以 merge precision 无法从标注算出来。
M2 每份文档只合并个位数条目，逐条看完全可行，这是目前唯一可信的
merge 质量评估方式。

用法::

    python eval/dump_merges.py --m2-dir out_m2_block -o out_m2_block/merges.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def render(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["items"]
    lines = []
    for entry in payload["merge_trace"]:
        if not entry.get("merged"):
            continue
        item = items[entry["item_index"]]
        lines.append("### {} · 合并 {} 条（来源 {}）".format(
            path.name.replace(".m2.json", ""),
            len(entry["source_indexes"]),
            ", ".join(str(i) for i in entry["source_indexes"]),
        ))
        lines.append("")
        lines.append("- **标题**：{}（来源：{}）".format(item["title"], entry.get("title_source")))
        lines.append("- **项目**：{}".format(item.get("project")))
        lines.append("- **部门/交付组**：{} / {}".format(
            item.get("department"), item.get("delivery_group")
        ))
        lines.append("- **evidence**：{}，连续={}，区间 [{}, {}]".format(
            entry.get("evidence_mode"),
            entry.get("evidence_contiguous"),
            item["evidence"]["start_char"],
            item["evidence"]["end_char"],
        ))
        lines.append("- **合并后 content**：")
        for part in item["content"].split("\n"):
            lines.append("  - {}".format(part))
        if entry.get("notes"):
            lines.append("- **备注**：{}".format("；".join(entry["notes"])))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 M2 的全部合并供人工核对")
    parser.add_argument("--m2-dir", required=True)
    parser.add_argument("--output", "-o", required=True)
    args = parser.parse_args()

    blocks = []
    total = 0
    for path in sorted(Path(args.m2_dir).glob("*.m2.json")):
        text = render(path)
        if text.strip():
            blocks.append(text)
            total += text.count("### ")

    body = "# M2 实际合并案例（共 {} 组）\n\n".format(total) + "\n".join(blocks)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(body, encoding="utf-8")
    print("导出 {} 组合并 → {}".format(total, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
