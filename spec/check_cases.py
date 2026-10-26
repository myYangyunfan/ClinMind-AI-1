#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""病例 JSON 内容自检（只读，不修改任何文件）。

校验范围
--------
1. aic_mech/app/data/cases/*.json —— 5 个通用练习病例（正式交付对象）
2. aic_mech/app/data/cases/../case_chest_pain.json —— 病例 1（主动脉夹层，由 case-dev 维护）

校验内容（依据 spec/04-AI能力与数据契约.md §1）
------------------------------------------------
* 24 个顶层键齐全
* patient_profile 8 字段 / initial_vitals 9 字段
* §4.2 承载键：present_illness 5 键、risk_factors ≥3、key_positives 恰 3、
  key_negatives 恰 2、inquiry_points 恰 8、differentials 恰 3
* emotion_scripts：恰 6 节点 × 恰 high/mid/low 三档 = 18 条，每条 20–120 字
* exam_options / lab_panels / prescription_rules / outcomes / knowledge_card 逐字段

退出码
------
0 = 5 个通用病例全部通过（胸痛病例的问题单独标注，不影响退出码）
1 = 通用病例存在缺失项，或任一文件无法 json.loads

用法
----
    python spec/check_cases.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES_DIR = REPO / "aic_mech" / "app" / "data" / "cases"
CHEST_CASE = REPO / "aic_mech" / "app" / "data" / "case_chest_pain.json"

TOP_LEVEL_KEYS = [
    "case_id", "title", "difficulty", "difficulty_stars", "chief_complaint", "category",
    "patient_profile", "initial_vitals", "physical_exam_findings", "lab_and_imaging_reports",
    "dialogue_scripts", "hemodynamic_rules",
    "emotion_scripts", "exam_options", "lab_panels", "prescription_rules", "outcomes", "knowledge_card",
    "present_illness", "risk_factors", "key_positives", "key_negatives", "inquiry_points", "differentials",
]

EMOTION_NODES = ["E1_CONSULT", "E2_PAIN_DETAIL", "E3_PHYSICAL", "E4_WAIT_LAB", "E5_RX", "E6_PRE_OUTCOME"]
EMOTION_TIERS = ["high", "mid", "low"]

PE_FIELDS = ["name", "gender", "age", "occupation",
             "past_history", "medication_history", "allergy_history", "family_companion"]
VITALS_FIELDS = ["hr", "sbp", "dbp", "spo2", "rr", "pain_vas",
                 "temperature_c", "ecg_rhythm", "consciousness"]

EXAM_DURATIONS = {30, 60, 90, 180, 300}
MODALITIES = {"ECG", "LAB", "IMAGING", "ULTRASOUND"}
DECISION_WINDOWS = {"WITHIN_CLOCK", "BEYOND_CLOCK"}
VERDICTS = {"TARGET", "SUPPORTIVE", "WARN_ONLY", "RED_LINE", "OFF_LIST"}
SEVERITIES = {"LETHAL", "HARMFUL"}

RE_PE_ID = re.compile(r"^PE_[A-Z0-9_]+$")
RE_PANEL_ID = re.compile(r"^(LAB|IMG)_[A-Z0-9_]+$")
RE_RX_ID = re.compile(r"^RX_[A-Z0-9_]+$")
RE_LOWER_EN = re.compile(r"^[a-z][a-z0-9 \-]*$")


def is_str(value, min_len: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_len


def is_str_list(value, min_len: int = 1) -> bool:
    return isinstance(value, list) and len(value) >= min_len and all(isinstance(v, str) for v in value)


class Report:
    def __init__(self) -> None:
        self.issues: list[str] = []

    def add(self, where: str, reason: str) -> None:
        self.issues.append(f"{where} → {reason}")


def check_emotion_scripts(case, rep: Report) -> None:
    es = case["emotion_scripts"]
    if not isinstance(es, dict):
        rep.add("emotion_scripts", "类型应为 object")
        return
    missing = [n for n in EMOTION_NODES if n not in es]
    extra = [n for n in es if n not in EMOTION_NODES]
    if missing:
        rep.add("emotion_scripts", f"缺节点 {missing}")
    if extra:
        rep.add("emotion_scripts", f"多余节点 {extra}（只允许 E1–E6）")
    count = 0
    for node in EMOTION_NODES:
        tiers = es.get(node)
        if not isinstance(tiers, dict):
            rep.add(f"emotion_scripts.{node}", "应为 object 且含 high/mid/low")
            continue
        miss = [t for t in EMOTION_TIERS if t not in tiers]
        if miss:
            rep.add(f"emotion_scripts.{node}", f"缺档位 {miss}")
        for tier in EMOTION_TIERS:
            if tier not in tiers:
                continue
            text = tiers[tier]
            count += 1
            if not isinstance(text, str):
                rep.add(f"emotion_scripts.{node}.{tier}", "应为 string")
            elif not 20 <= len(text) <= 120:
                rep.add(f"emotion_scripts.{node}.{tier}", f"长度 {len(text)} 超出 20–120")
    if count != 18:
        rep.add("emotion_scripts", f"文案条数 {count}，应为 18")


def check_exam_options(case, rep: Report) -> None:
    items = case["exam_options"]
    if not isinstance(items, list) or not items:
        rep.add("exam_options", "应为非空 array")
        return
    seen = set()
    required_true = 0
    for i, item in enumerate(items):
        where = f"exam_options[{i}]"
        if not isinstance(item, dict):
            rep.add(where, "应为 object")
            continue
        eid = item.get("exam_id", "")
        if not isinstance(eid, str) or not RE_PE_ID.match(eid):
            rep.add(f"{where}.exam_id", f"'{eid}' 不匹配 ^PE_[A-Z0-9_]+$")
        elif eid in seen:
            rep.add(f"{where}.exam_id", f"'{eid}' 病例内重复")
        else:
            seen.add(eid)
        name = item.get("name", "")
        if not is_str(name, 2):
            rep.add(f"{where}.name", "缺失或过短")
        kws = item.get("match_keywords")
        if not is_str_list(kws, 3):
            rep.add(f"{where}.match_keywords", "应为 array 且 ≥3 项")
        elif is_str(name) and not any(k in name or name in k for k in kws):
            # spec/04 §1.4.2 原写「首项须为 name 的子串」，但其自带样例
            # （name=周围血管搏动触诊 / 首项=脉搏）并不满足，规则不可满足。
            # 此处按可满足的等价意图校验：至少一个关键词与 name 互为子串。
            rep.add(f"{where}.match_keywords", f"没有任何关键词与 name 互为子串（{kws}）")
        if item.get("duration_seconds") not in EXAM_DURATIONS:
            rep.add(f"{where}.duration_seconds", f"{item.get('duration_seconds')} 不在 {{30,60,90,180,300}}")
        if not isinstance(item.get("required"), bool):
            rep.add(f"{where}.required", "应为 boolean")
        elif item["required"]:
            required_true += 1
        if not isinstance(item.get("is_abnormal"), bool):
            rep.add(f"{where}.is_abnormal", "应为 boolean")
        findings = item.get("findings")
        if not isinstance(findings, dict) or set(findings) != {"normal", "abnormal"}:
            rep.add(f"{where}.findings", "应恰含 normal / abnormal 两键")
        elif not (is_str(findings["normal"]) and is_str(findings["abnormal"])):
            rep.add(f"{where}.findings", "normal / abnormal 须为非空 string")
        if not is_str(item.get("clinical_point"), 4):
            rep.add(f"{where}.clinical_point", "缺失或过短")
    if required_true < 5:
        rep.add("exam_options", f"required=true 的体查仅 {required_true} 项，应 ≥5")


def check_lab_panels(case, rep: Report) -> None:
    items = case["lab_panels"]
    if not isinstance(items, list) or not items:
        rep.add("lab_panels", "应为非空 array")
        return
    seen = set()
    gold = []
    for i, item in enumerate(items):
        where = f"lab_panels[{i}]"
        if not isinstance(item, dict):
            rep.add(where, "应为 object")
            continue
        pid = item.get("panel_id", "")
        if not isinstance(pid, str) or not RE_PANEL_ID.match(pid):
            rep.add(f"{where}.panel_id", f"'{pid}' 不匹配 ^(LAB|IMG)_[A-Z0-9_]+$")
        elif pid in seen:
            rep.add(f"{where}.panel_id", f"'{pid}' 病例内重复")
        else:
            seen.add(pid)
        if not is_str(item.get("name"), 2):
            rep.add(f"{where}.name", "缺失或过短")
        alias = item.get("alias")
        if not is_str_list(alias, 2):
            rep.add(f"{where}.alias", "应为 array 且 ≥2 项")
        elif not any(RE_LOWER_EN.match(a) for a in alias):
            rep.add(f"{where}.alias", "缺至少 1 个纯小写英文别名")
        if item.get("modality") not in MODALITIES:
            rep.add(f"{where}.modality", f"{item.get('modality')} 不在 {sorted(MODALITIES)}")
        tat = item.get("tat_seconds")
        if not isinstance(tat, int) or not 60 <= tat <= 3600:
            rep.add(f"{where}.tat_seconds", f"{tat} 不在 60–3600")
        for flag in ("is_key", "is_gold_standard", "is_trap"):
            if not isinstance(item.get(flag), bool):
                rep.add(f"{where}.{flag}", "应为 boolean")
        if item.get("is_gold_standard") is True:
            gold.append(item.get("name"))
        if item.get("decision_window") not in DECISION_WINDOWS:
            rep.add(f"{where}.decision_window",
                    f"{item.get('decision_window')} 不在 {sorted(DECISION_WINDOWS)}")
        for field in ("result_text", "conclusion", "clinical_hint"):
            if not is_str(item.get(field), 2):
                rep.add(f"{where}.{field}", "缺失或过短")
        # 影像类允许填 "—"，故 ref_range 只要求非空
        if not is_str(item.get("ref_range"), 1):
            rep.add(f"{where}.ref_range", "缺失（影像类应填 —）")
    if len(gold) != 1:
        rep.add("lab_panels", f"is_gold_standard=true 的检查有 {len(gold)} 个，应恰为 1")


def check_prescription_rules(case, rep: Report) -> None:
    items = case["prescription_rules"]
    if not isinstance(items, list) or len(items) < 2:
        rep.add("prescription_rules", f"应为 array 且 ≥2 条（当前 {len(items) if isinstance(items, list) else 'N/A'}）")
        return
    seen_id, seen_drug = set(), set()
    red_lines = 0
    for i, item in enumerate(items):
        where = f"prescription_rules[{i}]"
        if not isinstance(item, dict):
            rep.add(where, "应为 object")
            continue
        rid = item.get("rule_id", "")
        if not isinstance(rid, str) or not RE_RX_ID.match(rid):
            rep.add(f"{where}.rule_id", f"'{rid}' 不匹配 ^RX_[A-Z0-9_]+$")
        elif rid in seen_id:
            rep.add(f"{where}.rule_id", f"'{rid}' 病例内重复")
        else:
            seen_id.add(rid)
        drug = item.get("drug_name", "")
        if not is_str(drug, 2):
            rep.add(f"{where}.drug_name", "缺失或过短")
        elif drug in seen_drug:
            rep.add(f"{where}.drug_name", f"'{drug}' 病例内重复")
        else:
            seen_drug.add(drug)
        kws = item.get("match_keywords")
        if not is_str_list(kws, 1):
            rep.add(f"{where}.match_keywords", "应为 array 且 ≥1 项")
        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            rep.add(f"{where}.verdict", f"{verdict} 不在 {sorted(VERDICTS)}")
        for field in ("dose", "route", "frequency", "consequence_text"):
            if not is_str(item.get(field), 2):
                rep.add(f"{where}.{field}", "缺失或过短")
        # 无靶目标的处方按规范填 "—"，故 target 只要求非空
        if not is_str(item.get("target"), 1):
            rep.add(f"{where}.target", "缺失（无靶目标应填 —）")
        delta = item.get("score_delta")
        if not isinstance(delta, (int, float)) or not -0.20 <= float(delta) <= 0.10:
            rep.add(f"{where}.score_delta", f"{delta} 不在 -0.20..0.10")
        if verdict == "RED_LINE":
            red_lines += 1
            if item.get("severity") not in SEVERITIES:
                rep.add(f"{where}.severity", f"红线 severity 必填且须为 LETHAL/HARMFUL（当前 {item.get('severity')}）")
            if isinstance(kws, list) and not any(isinstance(k, str) and RE_LOWER_EN.match(k) for k in kws):
                rep.add(f"{where}.match_keywords", "红线须同时含中文名与英文名")
        elif verdict in VERDICTS and "severity" in item:
            rep.add(f"{where}.severity", "severity 仅对 RED_LINE 有效，非红线应删除")
        if verdict == "WARN_ONLY" and not is_str(item.get("prerequisite"), 2):
            rep.add(f"{where}.prerequisite", "WARN_ONLY 必填 prerequisite")
    if red_lines < 2:
        rep.add("prescription_rules", f"RED_LINE 仅 {red_lines} 条，每例应 ≥2")


def check_outcomes(case, rep: Report) -> None:
    oc = case["outcomes"]
    if not isinstance(oc, dict):
        rep.add("outcomes", "应为 object")
        return
    if set(oc) != {"A", "B", "C", "D"}:
        rep.add("outcomes", f"顶层键应恰为 A/B/C/D（当前 {sorted(oc)}）")
    for code in ["A", "B", "C", "D"]:
        entry = oc.get(code)
        where = f"outcomes.{code}"
        if not isinstance(entry, dict):
            rep.add(where, "缺失或不是 object")
            continue
        if entry.get("code") != code:
            rep.add(f"{where}.code", f"'{entry.get('code')}' 应等于键名 '{code}'")
        for field in ("label", "condition", "patient_line", "family_line", "next_step"):
            if not is_str(entry.get(field), 2):
                rep.add(f"{where}.{field}", "缺失或过短")
        title = entry.get("title", "")
        if not is_str(title, 2) or len(title) > 16:
            rep.add(f"{where}.title", f"应为 2–16 字（当前 {len(title) if isinstance(title, str) else 'N/A'}）")
        narration = entry.get("narration", "")
        if not isinstance(narration, str) or not 60 <= len(narration) <= 160:
            rep.add(f"{where}.narration",
                    f"应为 60–160 字（当前 {len(narration) if isinstance(narration, str) else 'N/A'}）")
        items = entry.get("review_items")
        if not (isinstance(items, list) and len(items) >= 3 and all(isinstance(x, str) for x in items)):
            rep.add(f"{where}.review_items", "应为 array 且 ≥3 项")


def check_knowledge_card(case, rep: Report) -> None:
    kc = case["knowledge_card"]
    if not isinstance(kc, dict):
        rep.add("knowledge_card", "应为 object")
        return
    title = kc.get("title", "")
    if not is_str(title, 2) or len(title) > 20:
        rep.add("knowledge_card.title", f"应为 2–20 字（当前 {len(title) if isinstance(title, str) else 'N/A'}）")
    items = kc.get("items")
    if not isinstance(items, list) or len(items) != 5:
        rep.add("knowledge_card.items", f"长度应恰为 5（当前 {len(items) if isinstance(items, list) else 'N/A'}）")
        return
    for i, item in enumerate(items):
        where = f"knowledge_card.items[{i}]"
        if not isinstance(item, dict):
            rep.add(where, "应为 object")
            continue
        if item.get("no") != i + 1:
            rep.add(f"{where}.no", f"{item.get('no')} 应与下标一致（{i + 1}）")
        t = item.get("title", "")
        if not is_str(t, 2) or len(t) > 14:
            rep.add(f"{where}.title", f"应为 2–14 字（当前 {len(t) if isinstance(t, str) else 'N/A'}）")
        one = item.get("one_liner", "")
        if not is_str(one, 2) or len(one) > 60:
            rep.add(f"{where}.one_liner", f"应为 2–60 字（当前 {len(one) if isinstance(one, str) else 'N/A'}）")
        pit = item.get("pitfall", "")
        if not is_str(pit, 2) or len(pit) > 60:
            rep.add(f"{where}.pitfall", f"应为 2–60 字（当前 {len(pit) if isinstance(pit, str) else 'N/A'}）")
        elif not pit.startswith("常见误区："):
            rep.add(f"{where}.pitfall", "须以「常见误区：」开头")


def check_carrier_keys(case, rep: Report) -> None:
    pi = case["present_illness"]
    if not isinstance(pi, dict):
        rep.add("present_illness", "应为 object")
    else:
        for field in ("onset", "character", "radiation", "trigger", "relief"):
            if not is_str(pi.get(field), 4):
                rep.add(f"present_illness.{field}", "缺失或过短")

    if not is_str_list(case["risk_factors"], 3):
        rep.add("risk_factors", "应为 array 且 ≥3 项")
    if not is_str_list(case["key_positives"], 3) or len(case["key_positives"]) != 3:
        rep.add("key_positives", f"长度应恰为 3（当前 {len(case['key_positives']) if isinstance(case['key_positives'], list) else 'N/A'}）")
    if not is_str_list(case["key_negatives"], 2) or len(case["key_negatives"]) != 2:
        rep.add("key_negatives", f"长度应恰为 2（当前 {len(case['key_negatives']) if isinstance(case['key_negatives'], list) else 'N/A'}）")
    if not is_str_list(case["inquiry_points"], 8) or len(case["inquiry_points"]) != 8:
        rep.add("inquiry_points", f"长度应恰为 8（当前 {len(case['inquiry_points']) if isinstance(case['inquiry_points'], list) else 'N/A'}）")

    diffs = case["differentials"]
    if not isinstance(diffs, list) or len(diffs) != 3:
        rep.add("differentials", f"长度应恰为 3（当前 {len(diffs) if isinstance(diffs, list) else 'N/A'}）")
    else:
        for i, d in enumerate(diffs):
            if not isinstance(d, dict):
                rep.add(f"differentials[{i}]", "应为 object")
                continue
            # name 是病名本身（急性阑尾炎=5 字、急性脑卒中=5 字），只要求非空；
            # why_support / why_not 是论述句，按 spec/04 §1.3 要求 ≥6 字。
            if not is_str(d.get("name"), 2):
                rep.add(f"differentials[{i}].name", "缺失或过短")
            for field in ("why_support", "why_not"):
                if not is_str(d.get(field), 6):
                    rep.add(f"differentials[{i}].{field}", "缺失或不足 6 字")


def check_profile_and_vitals(case, rep: Report) -> None:
    pp = case["patient_profile"]
    if not isinstance(pp, dict):
        rep.add("patient_profile", "应为 object")
    else:
        missing = [f for f in PE_FIELDS if f not in pp]
        if missing:
            rep.add("patient_profile", f"缺字段 {missing}")
        if isinstance(pp.get("medication_history"), list):
            if not pp["medication_history"]:
                rep.add("patient_profile.medication_history", "不应为空数组")
        elif "medication_history" in pp:
            rep.add("patient_profile.medication_history", "应为 array")
        if isinstance(pp.get("gender"), str) and pp["gender"] not in ("男", "女"):
            rep.add("patient_profile.gender", f"'{pp['gender']}' 应为 男/女")

    iv = case["initial_vitals"]
    if not isinstance(iv, dict):
        rep.add("initial_vitals", "应为 object")
    else:
        missing = [f for f in VITALS_FIELDS if f not in iv]
        if missing:
            rep.add("initial_vitals", f"缺字段 {missing}")
        try:
            if float(iv.get("dbp", 0)) >= float(iv.get("sbp", 1)):
                rep.add("initial_vitals", "dbp 应小于 sbp")
        except (TypeError, ValueError):
            rep.add("initial_vitals", "sbp/dbp 应为数值")


def check_case(path: Path) -> tuple[list[str], bool]:
    """返回 (问题列表, 是否可解析)。"""
    rep = Report()
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if text.startswith("\ufeff"):
            rep.add("<file>", "文件以 UTF-8 BOM 开头，json.loads 会失败")
            return rep.issues, False
        case = json.loads(text)
    except json.JSONDecodeError as exc:
        rep.add("<file>", f"json 解析失败：{exc}")
        return rep.issues, False
    except OSError as exc:
        rep.add("<file>", f"读取失败：{exc}")
        return rep.issues, False

    if not isinstance(case, dict):
        rep.add("<file>", "顶层应为 object")
        return rep.issues, True

    missing = [k for k in TOP_LEVEL_KEYS if k not in case]
    if missing:
        rep.add("顶层键", f"缺 {len(missing)} 个：{missing}")

    checks = {
        "emotion_scripts": check_emotion_scripts,
        "exam_options": check_exam_options,
        "lab_panels": check_lab_panels,
        "prescription_rules": check_prescription_rules,
        "outcomes": check_outcomes,
        "knowledge_card": check_knowledge_card,
    }
    for key, fn in checks.items():
        if key in case:
            fn(case, rep)

    for key, fn in (("present_illness", check_carrier_keys),):
        if all(k in case for k in ("present_illness", "risk_factors", "key_positives",
                                   "key_negatives", "inquiry_points", "differentials")):
            fn(case, rep)
        break
    if all(k in case for k in ("patient_profile", "initial_vitals")):
        check_profile_and_vitals(case, rep)

    return rep.issues, True


def main() -> int:
    if not CASES_DIR.is_dir():
        print(f"[ERROR] 病例目录不存在：{CASES_DIR}")
        return 1

    general_files = sorted(p for p in CASES_DIR.glob("*.json") if p.name != "index.json")
    skipped = [p.name for p in CASES_DIR.glob("*.json") if p.name == "index.json"]

    general_passed = 0
    total_issues = 0

    print("=" * 72)
    print("通用练习病例（5 例，正式交付）")
    print("=" * 72)
    for path in general_files:
        issues, parsed = check_case(path)
        if issues:
            total_issues += len(issues)
            print(f"\n[FAIL] {path.name}（{len(issues)} 项）")
            for msg in issues:
                print(f"    {path.name} → {msg}")
        elif parsed:
            general_passed += 1
            print(f"[PASS] {path.name}")
        else:
            print(f"[FAIL] {path.name}（无法解析，见上方）")
    if skipped:
        print(f"[SKIP] {', '.join(skipped)}（索引文件，非病例）")

    print()
    print("=" * 72)
    print("病例 1：主动脉夹层（case_chest_pain.json，由 case-dev 维护）")
    print("=" * 72)
    chest_issues: list[str] = []
    if CHEST_CASE.exists():
        chest_issues, _ = check_case(CHEST_CASE)
        if chest_issues:
            print(f"[PENDING] {CHEST_CASE.name}（{len(chest_issues)} 项，不影响本次退出码）")
            for msg in chest_issues[:12]:
                print(f"    {CHEST_CASE.name} → {msg}")
            if len(chest_issues) > 12:
                print(f"    …… 另有 {len(chest_issues) - 12} 项未显示")
        else:
            print(f"[PASS] {CHEST_CASE.name}")
    else:
        print(f"[WARN] 未找到 {CHEST_CASE}")

    print()
    print("-" * 72)
    print(f"汇总：通用病例 {general_passed}/{len(general_files)} 通过，共 {total_issues} 项缺失；"
          f"胸痛病例 {'通过' if not chest_issues else str(len(chest_issues)) + ' 项待补'}")
    ok = general_passed == len(general_files) and total_issues == 0
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
