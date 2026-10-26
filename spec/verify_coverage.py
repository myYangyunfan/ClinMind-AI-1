"""Coverage self-check for the ClinMind-AI spec set.

Run from repo root:  python spec/verify_coverage.py
Exit code 0 = every ledger row resolves to a real spec section.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SPEC = Path(__file__).resolve().parent
ROOT = SPEC.parent
LEDGER = SPEC / "00-需求覆盖矩阵.md"
OVERVIEW = ROOT / "需求规格说明-v2.md"

FILES = {
    "总纲": OVERVIEW,
    "00": LEDGER,
    "01": SPEC / "01-病例内容规格.md",
    "02": SPEC / "02-数值与状态机规格.md",
    "03": SPEC / "03-UI交互规格.md",
    "04": SPEC / "04-AI能力与数据契约.md",
}

failures: list[str] = []
notes: list[str] = []


def headings(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.startswith("#")]


def heading_numbers(path: Path) -> list[str]:
    nums = []
    for h in headings(path):
        m = re.match(r"^#{2,4}\s*(?:§\s*)?(\d+(?:\.\d+)*)", h)
        if m:
            nums.append(m.group(1))
    return nums


def section_exists(path: Path, number: str) -> bool:
    """number like '3' or '3.4' — a heading must equal it or live under it."""
    for n in heading_numbers(path):
        if n == number or n.startswith(number + "."):
            return True
    return False


def parse_ledger() -> list[tuple[str, list[str], str]]:
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 编号"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        rid = cells[0]
        if not re.match(r"^(R\d+\.\d+|X\d+)$", rid):
            continue
        rows.append((rid, cells, line))
    return rows


def extract_refs(text: str) -> list[tuple[str, str]]:
    """Pull (file_key, section_number) pairs out of a 落点 cell."""
    refs: list[tuple[str, str]] = []
    for m in re.finditer(r"(总纲|0[1-4])\s*§\s*([\d.]+)(?:\s*[–\-~]\s*§?\s*([\d.]+))?", text):
        key, start, end = m.group(1), m.group(2), m.group(3)
        refs.append((key, start))
        if end:
            refs.append((key, end))
    return refs


def check_ledger() -> None:
    rows = parse_ledger()
    notes.append(f"台账条目数: {len(rows)}")
    for rid, cells, raw in rows:
        status = cells[-1]
        # 不同小节表格列序不同，直接整行扫引用，避免列错位
        refs = extract_refs(raw)
        if not refs:
            failures.append(f"{rid}: 该条目没有可解析的章节引用 -> 整行 {raw[:80]!r}")
            continue
        for key, num in refs:
            path = FILES[key]
            if not path.exists():
                failures.append(f"{rid}: 引用的文件不存在 {path}")
                continue
            if not section_exists(path, num):
                failures.append(f"{rid}: {key} §{num} 在 {path.name} 中找不到对应标题")
        if "⚠️" in status or "❌" in status:
            failures.append(f"{rid}: 状态仍是 {status}（未闭环）")


def check_acceptance() -> None:
    t02 = (SPEC / "02-数值与状态机规格.md").read_text(encoding="utf-8")
    for i in range(1, 11):
        if f"A{i:02d}" not in t02:
            failures.append(f"02: 缺少总纲验收条目 A{i:02d}")
    t03 = (SPEC / "03-UI交互规格.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"\bA(\d{2})\b", t03))
    notes.append(f"03 可验收交互条目: {len(ids)} 条 (A01–A{max(ids) if ids else '?'})")
    if len(ids) < 12:
        failures.append(f"03: 可验收条目只有 {len(ids)} 条，少于要求的 12 条")


def check_json_blocks() -> None:
    for name in ("01-病例内容规格.md", "04-AI能力与数据契约.md"):
        text = (SPEC / name).read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        ok = 0
        for i, block in enumerate(blocks, 1):
            parsed = False
            for candidate in (block, "{" + block + "}"):
                try:
                    json.loads(candidate)
                    parsed = True
                    break
                except json.JSONDecodeError:
                    continue
            if parsed:
                ok += 1
            else:
                failures.append(f"{name}: 第 {i} 个 json 代码块无法解析")
        notes.append(f"{name} JSON 代码块: {ok}/{len(blocks)} 可解析")


def check_placeholders() -> None:
    for key, path in FILES.items():
        text = path.read_text(encoding="utf-8")
        hits = [m for m in re.findall(r"(TBD|待补充|酌情|待定)", text)]
        # only report, some are legitimate ("禁止 TBD" prose)
        if hits:
            notes.append(f"{key}: 占位词出现 {len(hits)} 次（需人工确认是否为禁令描述）")


def check_cases() -> None:
    text = (SPEC / "01-病例内容规格.md").read_text(encoding="utf-8")
    for title in ("主动脉夹层", "急性左心衰", "急性阑尾炎", "短暂性脑缺血", "社区获得性肺炎"):
        if title not in text:
            failures.append(f"01: 缺少病例 {title}")
    for key in ("emotion_scripts", "exam_options", "lab_panels", "prescription_rules", "outcomes", "knowledge_card"):
        if key not in text:
            failures.append(f"01: 缺少字段契约 {key}")


def main() -> int:
    for key, path in FILES.items():
        if not path.exists():
            failures.append(f"缺失文件: {path}")
    if not failures:
        check_ledger()
        check_acceptance()
        check_json_blocks()
        check_placeholders()
        check_cases()

    print("== 覆盖度自检 ==")
    for n in notes:
        print("  ·", n)
    if failures:
        print(f"\n== 未通过 {len(failures)} 项 ==")
        for f in failures:
            print("  ✗", f)
        return 1
    print("\n== 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
