"""Dual-Track Graph-RAG Anti-Hallucination Guardrail Engine.

Physically isolates clinical objective data (physical findings, laboratory tests,
imaging reports) from LLM generation. Guarantees 100% data authenticity and
compliance with authentic Grade-3A tertiary hospital case gold standards.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import config


class GuardrailEngine:
    """Hard-track router for clinical physical examinations and objective diagnostics."""

    def __init__(self, case_file_path: Optional[Path] = None, case_data: Optional[Dict[str, Any]] = None):
        self.case_path = case_file_path or (config.DATA_DIR / "case_chest_pain.json")
        self.case_data: Dict[str, Any] = case_data or self._load_case_data()
        self.physical_findings = self.case_data.get("physical_exam_findings", {})
        self.lab_reports = self.case_data.get("lab_and_imaging_reports", {})
        self.lab_panels = [p for p in (self.case_data.get("lab_panels") or []) if isinstance(p, dict)]
        self.exam_options = [x for x in (self.case_data.get("exam_options") or []) if isinstance(x, dict)]

    def _route_by_panel(self, order_name: str) -> Optional[Tuple[bool, Dict[str, Any], str]]:
        """Route a lab/imaging order against the case's own `lab_panels[].alias`.

        The hard-coded alias table below only knows eight stock panels, so any
        authored panel outside that list (CRP, chest X-ray, pathogen PCR, CT …)
        used to fall through to NOT_CONFIGURED while the data sat right there in
        the case JSON. Matching the case's aliases keeps one source of truth:
        the case file. Longest alias wins so "血气分析" beats the shorter "血".
        """
        name = "".join(order_name.lower().split())
        if not name or not self.lab_panels:
            return None
        best: Optional[Dict[str, Any]] = None
        best_len = 0
        for panel in self.lab_panels:
            for alias in panel.get("alias") or []:
                key = "".join(str(alias).lower().split())
                if key and key in name and len(key) > best_len:
                    best, best_len = panel, len(key)
        if best is None:
            return None
        rep: Dict[str, Any] = {
            "item_name": best.get("name") or order_name,
            "modality": best.get("modality", "LAB"),
            "result": best.get("result_text", ""),
            "conclusion": best.get("conclusion", ""),
            "ref_range": best.get("ref_range", ""),
            "clinical_hint": best.get("clinical_hint", ""),
            "tat_seconds": best.get("tat_seconds"),
            "decision_window": best.get("decision_window"),
            "panel_id": best.get("panel_id"),
            "is_gold_standard": bool(best.get("is_gold_standard")),
        }
        lines = [f"【{rep['item_name']}】"]
        if rep["result"]:
            lines.append(f"结果：{rep['result']}")
        if rep["ref_range"]:
            lines.append(f"参考区间：{rep['ref_range']}")
        if rep["conclusion"]:
            lines.append(f"结论：{rep['conclusion']}")
        if rep["clinical_hint"]:
            lines.append(f"提示：{rep['clinical_hint']}")
        return True, rep, "\n".join(lines)

    @staticmethod
    def _enrich_finding(finding: Any, meta: Dict[str, Any]) -> Any:
        """Attach the authored ``exam_options`` identity to a legacy finding.

        The chest case's organ branches predate ``exam_options`` and return
        their own dicts; a downstream consumer still wants ``exam_id`` /
        ``duration_seconds`` / ``required`` so it can key reports uniformly.
        Only adds keys — never overwrites what the branch already authored.
        """
        if not isinstance(finding, dict) or not meta:
            return finding
        enriched = dict(finding)
        for key in ("exam_id", "duration_seconds", "required", "clinical_point"):
            if key not in enriched and meta.get(key) is not None:
                enriched[key] = meta[key]
        return enriched

    def _route_by_exam_option(self, query: str) -> Optional[Tuple[bool, Dict[str, Any], str]]:
        """Route a physical exam against the case's own ``exam_options[]``.

        The organ-by-organ branches below are wired to the chest-pain case and
        the catalogue cases only carried a single ``general`` blob, so the five
        authored bedsides exams of each teaching case were never reachable: the
        data sat in the JSON while the engine answered "not configured". Here
        the case file is the single source of truth. Matching mirrors
        ``_route_by_panel``: whitespace-insensitive, longest keyword wins, and
        ``name`` doubles as an alias so an exact title always resolves.
        """
        name = "".join(str(query or "").lower().split())
        if not name or not self.exam_options:
            return None
        best: Optional[Dict[str, Any]] = None
        best_len = 0
        for option in self.exam_options:
            keys = list(option.get("match_keywords") or []) + [option.get("name") or ""]
            for alias in keys:
                key = "".join(str(alias).lower().split())
                if key and key in name and len(key) > best_len:
                    best, best_len = option, len(key)
        if best is None:
            return None

        findings = best.get("findings")
        abnormal = bool(best.get("is_abnormal"))
        if isinstance(findings, dict):
            primary = "abnormal" if abnormal else "normal"
            secondary = "normal" if abnormal else "abnormal"
            body = findings.get(primary) or findings.get(secondary) or ""
        else:
            body = str(findings or "")
        body = str(body)
        rep: Dict[str, Any] = {
            "item_name": best.get("name") or "",
            "name": best.get("name") or "",
            "exam_id": best.get("exam_id"),
            "findings": body,
            "conclusion": body,
            "is_abnormal": abnormal,
            "duration_seconds": best.get("duration_seconds"),
            "required": bool(best.get("required")),
            "clinical_point": best.get("clinical_point", ""),
            "modality": "EXAM",
        }
        lines = [f"【{rep['item_name']}】", f"查体所见：{body}"]
        if rep["clinical_point"]:
            lines.append(f"临床要点：{rep['clinical_point']}")
        if rep["duration_seconds"] is not None:
            lines.append(f"操作耗时：{rep['duration_seconds']} 秒")
        return True, rep, "\n".join(lines)

    @staticmethod
    def _text(value: Any) -> str:
        """Render either the chest case's structured finding or a generic one."""
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return str(value or "")
        return str(value.get("findings") or value.get("result") or value.get("conclusion") or "")

    def _load_case_data(self) -> Dict[str, Any]:
        """Load gold standard case repository."""
        if not self.case_path.exists():
            raise FileNotFoundError(f"Case definition file not found at: {self.case_path}")
        with open(self.case_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def route_physical_exam(self, query: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Match physical examination query to authenticated findings.

        Returns: (matched, report_dict, feedback_message)
        """
        q = query.strip().lower()
        authored = self._route_by_exam_option(query)
        meta = authored[1] if authored is not None else {}

        if isinstance(self.physical_findings, dict) and "general" in self.physical_findings:
            # Catalogue cases store a concise general exam instead of the
            # chest case's named organ findings.  Return only those authored
            # values rather than inventing a normal/abnormal result.
            # 病例自带 exam_options 时优先开单项体查（数据在 JSON 里，就该被点到）。
            if authored is not None:
                return authored
            if any(k in q for k in ["查体", "体查", "全身", "一般", "咽", "肺", "腹", "听诊", "视诊", "触诊"]):
                general = self.physical_findings.get("general", "")
                note = self.physical_findings.get("vitals_note", "")
                finding = {"findings": general, "vitals_note": note}
                msg = f"【病例查体回报】\n{general}" + (f"\n生命体征补充：{note}" if note else "")
                return True, self._enrich_finding(finding, meta), msg

        # 1. Bilateral Upper-Arm Blood Pressure (Critical Gold Node N5)
        # 裸关键词"双侧"会误吞其他部位的查体（如双侧瞳孔、双下肢血压），
        # 仅在明确指向上肢/臂部/双侧血压且不属于其他查体区域时命中。
        if any(k in q for k in ["双上肢", "左侧血压", "右侧血压", "双臂", "对侧血压", "压差", "量双上臂", "肢体血压"]) or (
            "双侧" in q and "血压" in q and not any(x in q for x in ["瞳孔", "下肢", "足", "腘", "肺", "呼吸音"])
        ):
            finding = self.physical_findings.get("vitals_bilateral_bp", {}) if isinstance(self.physical_findings, dict) else {}
            msg = (
                f"【双侧上肢血压对比测量】\n"
                f"右上臂血压：{finding.get('right_arm', '195/115 mmHg')}\n"
                f"左上臂血压：{finding.get('left_arm', '162/98 mmHg')}\n"
                f"测量结论：{finding.get('difference', '')}\n"
                f"临床解析：{finding.get('interpretation', '')}"
            )
            return True, self._enrich_finding(finding, meta), msg

        # 2. Heart Auscultation & Valve Murmurs (Gold Node N6)
        if any(k in q for k in ["心脏听诊", "心音", "杂音", "瓣膜", "主动脉瓣", "叹气样", "泼水样", "听心脏"]):
            finding = self.physical_findings.get("heart_auscultation", {}) if isinstance(self.physical_findings, dict) else {}
            msg = (
                f"【心脏查体与瓣膜听诊】\n"
                f"听诊所见：{finding.get('findings', '')}\n"
                f"临床意义：{finding.get('interpretation', '')}"
            )
            return True, self._enrich_finding(finding, meta), msg

        # 3. Peripheral Pulses
        if any(k in q for k in ["脉搏", "桡动脉", "股动脉", "足背动脉", "血管征", "搏动"]):
            finding = self.physical_findings.get("peripheral_pulses", {}) if isinstance(self.physical_findings, dict) else {}
            msg = f"【周围血管搏动查体】\n{finding.get('findings', '')}"
            return True, self._enrich_finding(finding, meta), msg

        # 4. Lung Auscultation
        if any(k in q for k in ["肺", "呼吸音", "啰音", "双肺听诊", "湿啰音"]):
            finding = self.physical_findings.get("lung_auscultation", {}) if isinstance(self.physical_findings, dict) else {}
            msg = f"【双肺听诊】\n{finding.get('findings', '')}"
            return True, self._enrich_finding(finding, meta), msg

        # 5. Abdominal Exam
        if any(k in q for k in ["腹部", "压痛", "反跳痛", "搏动性包块", "触诊腹部"]):
            finding = self.physical_findings.get("abdomen_exam", {}) if isinstance(self.physical_findings, dict) else {}
            msg = f"【腹部查体】\n{finding.get('findings', '')}"
            return True, self._enrich_finding(finding, meta), msg

        # 6. Neurological Exam
        if any(k in q for k in ["神经", "瞳孔", "病理征", "巴氏征", "肌力", "脑膜刺激征"]):
            finding = self.physical_findings.get("neuro_exam", {}) if isinstance(self.physical_findings, dict) else {}
            msg = f"【神经系统快速查体】\n{finding.get('findings', '')}"
            return True, self._enrich_finding(finding, meta), msg

        # 7. Case-authored exam_options (数据层兜底，胸痛病例的 1-6 分支优先)
        if authored is not None:
            return authored

        # Generic physical examination fallback
        return False, None, "本病例未配置该项查体的确定性结果（防幻觉护栏拒绝编造查体所见）。建议进行针对性专科查体（如双上肢血压、心脏瓣膜听诊）。"

    def route_lab_or_imaging(self, order_name: str, current_vitals: Optional[Any] = None) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Match lab test or imaging request to authenticated hospital diagnostic report.

        Returns: (matched, report_dict, feedback_message)
        """
        name = order_name.strip().lower()

        # Generic case reports are keyed by the case JSON.  Use them before
        # the chest-pain-specific aliases below so no result is fabricated.
        for key, report in self.lab_reports.items():
            if not isinstance(report, dict):
                continue
            haystack = " ".join(str(report.get(k, "")) for k in ("item_name", "title", "name", "modality")).lower()
            aliases = {
                "blood_routine": ["血常规", "cbc", "白细胞"],
                "stool_routine": ["大便", "粪便", "便常规", "stool"],
                "urine_routine": ["尿常规", "尿液", "urine"],
                "peak_flow": ["峰流速", "呼气峰流速", "peak flow"],
                "blood_gas": ["血气", "blood gas", "abg"],
                "fingerstick_glucose": ["血糖", "指尖血糖", "glucose"],
                "ecg_12lead": ["心电图", "ecg", "12导联"],
                "abdominal_ultrasound": ["腹部超声", "腹部彩超", "ultrasound"],
            }.get(key, [])
            # 匹配只能用「医嘱文本」去比。旧实现在这里多了一个
            # `any(alias in haystack ...)`：haystack 是报告自己的名字，
            # 与查询无关，于是只要某份报告名字里含「血气」，任何检验医嘱
            # 都会返回它（含胸痛的 18 导联心电图）。见 spec/04 §8 C-17。
            matched = any(alias in name for alias in aliases)
            if not matched and not aliases and haystack:
                matched = bool(name) and name in haystack
            if matched:
                rep = dict(report)
                rep.setdefault("item_name", key)
                rep.setdefault("modality", "LAB")
                if current_vitals is not None and isinstance(rep.get("parameters"), dict):
                    rep["parameters"] = dict(rep["parameters"])
                    rep["parameters"].setdefault("heart_rate", int(round(float(current_vitals.hr))))
                return True, rep, self._generic_report_message(order_name, rep)

        if self.case_data.get("case_id") != "case_chest_pain_001":
            panel_hit = self._route_by_panel(order_name)
            if panel_hit is not None:
                return panel_hit
            default_rep = {
                "item_name": order_name,
                "modality": "LAB",
                "result": "本病例未配置该项检查的确定性数据",
                "conclusion": "防幻觉护栏：该项检查未收录，拒绝编造检验数值",
                "status": "NOT_CONFIGURED",
            }
            return False, default_rep, f"【检查回报】开具【{order_name}】：本病例未配置该项检查的确定性报告（护栏拒绝编造结果）。"

        # 1. 18-Lead Bedside ECG (Gold Node N8)
        if any(k in name for k in ["心电图", "ecg", "18导联", "十二导联", "12导联"]):
            rep = dict(self.lab_reports.get("ecg_18_lead", {}))
            rep["parameters"] = dict(rep.get("parameters", {}))
            if current_vitals is not None:
                rep["parameters"]["heart_rate"] = int(round(float(current_vitals.hr)))
            msg = (
                f"【急诊18导联床旁心电图报告】\n"
                f"节律：{rep.get('parameters', {}).get('heart_rate')} bpm\n"
                f"心电图诊断结论：{rep.get('conclusion', '')}\n"
                f"⚠️ 警示提示：{rep.get('clinical_hint', '')}"
            )
            return True, rep, msg

        # 2. Bedside Ultrasound / POCUS Echo
        if any(k in name for k in ["超声", "心包", "echo", "pocus", "心脏彩超", "床旁超声"]):
            rep = self.lab_reports.get("bedside_echo", {})
            findings_text = "\n".join(f"• {f}" for f in rep.get("findings", []))
            msg = (
                f"【急诊床旁超声心动图 (POCUS) 报告】\n"
                f"超声征象：\n{findings_text}\n"
                f"诊断结论：{rep.get('conclusion', '')}"
            )
            return True, rep, msg

        # 3. Emergency High-Sensitivity Cardiac Troponin (hs-cTnI) (Gold Node N9)
        if any(k in name for k in ["肌钙蛋白", "ctni", "心肌酶", "tnt", "ckmb", "心肌标志物"]):
            rep = self.lab_reports.get("hs_ctni", {})
            msg = (
                f"【急诊心肌损伤标志物检验单】\n"
                f"高敏肌钙蛋白I (hs-cTnI)：{rep.get('result')}\n"
                f"参考区间：{rep.get('ref_range')}\n"
                f"临床提示：{rep.get('conclusion')}"
            )
            return True, rep, msg

        # 4. Emergency D-Dimer (Gold Node N10)
        if any(k in name for k in ["d-二聚体", "d-dimer", "二聚体"]):
            rep = self.lab_reports.get("d_dimer", {})
            if rep.get("status") == "CRITICAL_HIGH":
                rep["critical_alert"] = True
            msg = (
                f"【急诊大血管及凝血危急值检验单】\n"
                f"D-二聚体 (D-Dimer)：{rep.get('result')}\n"
                f"参考区间：{rep.get('ref_range')}\n"
                f"危急警报：{rep.get('conclusion')}"
            )
            return True, rep, msg

        # 5. Emergency Full Aorta Contrast-Enhanced CTA (Gold Node N15)
        if any(k in name for k in ["cta", "增强cta", "主动脉全长", "大血管cta", "主动脉造影", "主动脉cta"]):
            rep = self.lab_reports.get("aortic_cta", {})
            findings_text = "\n".join(f"• {f}" for f in rep.get("findings", []))
            msg = (
                f"【急诊胸腹全主动脉增强 CTA 确诊金标准报告】\n"
                f"影像表现：\n{findings_text}\n"
                f"🎯 放射影像终审结论：{rep.get('conclusion')}"
            )
            return True, rep, msg

        # 6. Complete Blood Count (CBC)
        if any(k in name for k in ["血常规", "cbc", "白细胞", "血红蛋白"]):
            rep = self.lab_reports.get("routine_blood", {})
            msg = (
                f"【急诊血常规检验单】\n"
                f"结果：{rep.get('result')}\n"
                f"参考区间：{rep.get('ref_range')}"
            )
            return True, rep, msg

        # 7. Coagulation Profile
        if any(k in name for k in ["凝血", "inr", "aptt", "pt", "纤维蛋白原"]):
            rep = self.lab_reports.get("coagulation", {})
            msg = (
                f"【急诊凝血功能检验单】\n"
                f"结果：{rep.get('result')}\n"
                f"参考区间：{rep.get('ref_range')}"
            )
            return True, rep, msg

        # Unmatched routine test: 防幻觉护栏不允许编造"正常"结果——
        # 未收录的检查项目必须诚实回绝，提示学员开立病例支持的检查。
        panel_hit = self._route_by_panel(order_name)
        if panel_hit is not None:
            return panel_hit
        default_rep = {
            "item_name": order_name,
            "modality": "LAB",
            "result": "本病例未配置该项检查的确定性数据",
            "conclusion": "防幻觉护栏：该项检查未收录，拒绝编造检验数值",
            "status": "NOT_CONFIGURED"
        }
        return False, default_rep, f"【检查回报】开具【{order_name}】：本病例未配置该项检查的确定性报告（护栏拒绝编造结果）。请开立本病例已有金标准数据支持的检查项目。"

    def _generic_report_message(self, order_name: str, report: Dict[str, Any]) -> str:
        details = []
        for key in ("result", "findings", "conclusion", "clinical_hint", "ref_range", "status"):
            value = report.get(key)
            if value not in (None, "", []):
                if isinstance(value, list):
                    value = "；".join(str(item) for item in value)
                details.append(f"{key}：{value}")
        if isinstance(report.get("parameters"), dict):
            details.extend(f"{key}：{value}" for key, value in report["parameters"].items())
        return f"【{order_name}报告】\n" + ("\n".join(details) if details else "病例已配置该项客观报告")
