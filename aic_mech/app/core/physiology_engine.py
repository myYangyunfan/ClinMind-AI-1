"""Continuous Hemodynamic Physiology State Machine Engine (1Hz).

Simulates cardiovascular hemodynamics, state transitions, pharmacodynamics (PD),
and lethal rupture countdowns for acute aortic dissection.
"""

import math
import random
import time
from typing import Dict, Any, List, Optional, Tuple
from app.models.entities import PatientStateEnum, VitalsVector
import config


class PhysiologyEngine:
    """1Hz Continuous Hemodynamic Engine with State Machine transitions."""

    def __init__(self, initial_vitals: Optional[VitalsVector] = None, case_data: Optional[Dict[str, Any]] = None):
        self.case_data: Dict[str, Any] = case_data or {}
        self.case_id = str(self.case_data.get("case_id", "case_chest_pain_001"))
        # D-30: 病例行为开关来自病例 JSON 的 ``hemodynamic_rules``，不再按 case_id 硬编码。
        self.hemodynamic_rules: Dict[str, Any] = (
            self.case_data.get("hemodynamic_rules")
            if isinstance(self.case_data.get("hemodynamic_rules"), dict)
            else {}
        )
        self.high_risk_aortic = self._detect_high_risk_aortic()
        # 夹层病程时钟：病例 JSON 优先，缺失时回落配置常量（默认 120/30/15/300）
        self.rupture_to_effusion_seconds: float = self._rule_seconds(
            "lethal_rupture_countdown_seconds", config.PHYS_RUPTURE_TO_EFFUSION_S
        )
        self.crisis_timeout_seconds: float = self._rule_seconds(
            "uncontrolled_crisis_timeout_seconds", config.PHYS_CRISIS_TIMEOUT_S
        )
        # prescription_rules：内容层红线（spec/01 §1.5），红线判定优先于关键词模糊匹配
        self.prescription_rules: List[Dict[str, Any]] = self._normalise_prescription_rules()
        self.harmful_hits: int = 0
        self.applied_prescription_rules: List[Dict[str, Any]] = []
        self.last_prescription_rule: Optional[Dict[str, Any]] = None
        self.state: PatientStateEnum = PatientStateEnum.S0_STABLE
        self.vitals: VitalsVector = initial_vitals or VitalsVector(
            hr=108.0,
            sbp=195.0,
            dbp=115.0,
            spo2=96.0,
            rr=22.0,
            pain_vas=9,
            ecg_rhythm="Sinus Tachycardia (窦性心动过速)",
            consciousness="神志清楚，极度痛苦面容，大汗淋漓"
        )
        self.baseline_vitals: VitalsVector = self.vitals.model_copy()

        # Elapsed virtual simulation time in seconds
        self.elapsed_seconds: float = 0.0

        # Medication infusion rates / active states
        self.active_medications: Dict[str, float] = {}

        # Lethal countdown mechanism
        self.rupture_countdown: Optional[float] = None
        self.rupture_triggered_reason: Optional[str] = None
        self.fatal_death_occurred: bool = False

        # Target control flags
        self.beta_blocker_active: bool = False
        self.vasodilator_active: bool = False
        self.analgesic_active: bool = False

        # Random walk seed for monitor realism
        self._noise_phase: float = 0.0

    def get_vitals(self) -> VitalsVector:
        """Get the current hemodynamic vitals vector."""
        return self.vitals

    # ------------------------------------------------------------------
    # D-30 / D-33: case-JSON driven hemodynamic switches
    # ------------------------------------------------------------------
    def _detect_high_risk_aortic(self) -> bool:
        """Decide whether the aortic-dissection state machine applies.

        Resolution order (front-compatible with the old ``case_id`` check):
        1. ``hemodynamic_rules.high_risk_aortic`` explicit boolean;
        2. ``hemodynamic_rules`` declaring a dissection clock
           (``lethal_rupture_countdown_seconds``) -> dissection case;
        3. ``hemodynamic_rules.pattern`` present (catalogue teaching cases:
           ``stable_low_risk`` & friends) -> non-dissection;
        4. no usable rules at all -> legacy behaviour: only the chest-pain case.
        """
        rules = self.hemodynamic_rules
        if "high_risk_aortic" in rules:
            return bool(rules.get("high_risk_aortic"))
        if "lethal_rupture_countdown_seconds" in rules:
            return True
        if str(rules.get("pattern") or "").strip():
            return False
        return self.case_id == "case_chest_pain_001"

    def _rule_seconds(self, key: str, default: float) -> float:
        """Read a positive number of seconds from ``hemodynamic_rules``."""
        try:
            value = float(self.hemodynamic_rules.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return value if value > 0 else float(default)

    def _normalise_prescription_rules(self) -> List[Dict[str, Any]]:
        """Normalise ``prescription_rules[]`` into a lowercase keyword index."""
        normalised: List[Dict[str, Any]] = []
        raw_rules = self.case_data.get("prescription_rules")
        if not isinstance(raw_rules, list):
            return normalised
        for raw in raw_rules:
            if not isinstance(raw, dict):
                continue
            keywords = [
                str(k).strip().lower()
                for k in (raw.get("match_keywords") or [])
                if str(k).strip()
            ]
            if not keywords:
                continue
            severity = str(raw.get("severity") or "").strip().upper()
            rule = dict(raw)
            rule["keywords"] = keywords
            rule["verdict"] = str(raw.get("verdict") or "").strip().upper()
            # spec/01 §1.5: severity 仅对 RED_LINE 生效，默认 LETHAL
            rule["severity"] = (severity or "LETHAL") if rule["verdict"] == "RED_LINE" else severity
            normalised.append(rule)
        return normalised

    def match_prescription_rule(self, drug_name: str) -> Optional[Dict[str, Any]]:
        """Match a drug name against ``prescription_rules[]`` (red lines first).

        A RED_LINE rule that carries a ``prerequisite`` is *not* a red line while
        ``self.beta_blocker_active`` is set: spec/01 §2.7 R2 only fires when the
        vasodilator is used before heart-rate control.
        """
        needle = (drug_name or "").strip().lower()
        if not needle or not self.prescription_rules:
            return None
        candidates: List[Tuple[int, int, Dict[str, Any]]] = []
        for rule in self.prescription_rules:
            matched = [kw for kw in rule["keywords"] if kw in needle or needle in kw]
            if not matched:
                continue
            if rule["verdict"] == "RED_LINE" and rule.get("prerequisite") and self.beta_blocker_active:
                continue
            candidates.append((0 if rule["verdict"] == "RED_LINE" else 1, -max(len(k) for k in matched), rule))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def apply_prescription_red_line(self, rule: Dict[str, Any], drug_name: str) -> Dict[str, Any]:
        """Route a matched RED_LINE rule by ``severity`` (LETHAL | HARMFUL)."""
        result: Dict[str, Any] = {
            "is_lethal": False,
            "is_harmful": False,
            "is_red_line": True,
            "rule_id": rule.get("rule_id"),
            "verdict": rule.get("verdict"),
            "severity": rule.get("severity"),
            "drug_name": drug_name,
            "status": "applied",
            "message": "",
            "harmful_hits": self.harmful_hits,
            "harmful_escalated": False,
            "previous_state": self.state,
            "new_state": self.state,
        }
        self.last_prescription_rule = rule
        self.applied_prescription_rules.append(
            {"rule_id": rule.get("rule_id"), "severity": rule.get("severity"), "drug_name": drug_name}
        )

        if str(rule.get("severity") or "LETHAL").upper() == "HARMFUL":
            self.harmful_hits += 1
            escalated = self.harmful_hits >= config.PRESCRIPTION_HARMFUL_ESCALATION_HITS
            if self.high_risk_aortic and self.state == PatientStateEnum.S0_STABLE:
                self.state = PatientStateEnum.S1_HYPERTENSION_CRISIS
            self.vitals.hr = min(150.0, self.vitals.hr + 10.0)
            self.vitals.sbp = min(230.0, self.vitals.sbp + (10.0 if self.high_risk_aortic else 6.0))
            if self.vitals.pain_vas < 10:
                self.vitals.pain_vas += 1
            result.update(
                is_harmful=True,
                harmful_hits=self.harmful_hits,
                harmful_escalated=escalated,
                message=(
                    f"【有害红线】{rule.get('drug_name') or drug_name}当前并非首选："
                    f"{rule.get('consequence_text') or '加重病情、恶化病程'}，可恢复，请立即纠正。"
                ),
            )
            if escalated:
                self.trigger_iatrogenic_rupture(
                    f"累计 {self.harmful_hits} 次有害处方【{drug_name}】导致病程恶化不可逆"
                )
                result["message"] += "【累计恶化】有害处方累计达限，病程进入不可逆恶化。"
            result["new_state"] = self.state
            return result

        # severity == LETHAL: 即刻致死恶化
        self.trigger_iatrogenic_rupture(
            f"误用禁忌药物【{drug_name}】{(rule.get('consequence_text') or '').strip()}"
        )
        result.update(
            is_lethal=True,
            message=(
                f"【致命红线触发】{rule.get('drug_name') or drug_name}属于本病例绝对禁忌！"
                f"{rule.get('consequence_text') or '触发致命恶化倒计时'}。"
            ),
        )
        result["new_state"] = self.state
        return result

    def apply_medication(self, drug_name: str, dose: float = 1.0, unit: str = "mg") -> Dict[str, Any]:
        """Apply a medication order to the hemodynamic system."""
        normalized_name = drug_name.strip().lower()
        result = {
            "is_lethal": False,
            "is_harmful": False,
            "is_red_line": False,
            "drug_name": drug_name,
            "status": "applied",
            "message": "",
            "previous_state": self.state,
            "new_state": self.state
        }

        # 0. 内容层处方红线（prescription_rules，spec/01 §1.5）。
        #    优先级高于下面的关键词模糊匹配：病例 JSON 显式声明的 severity 为准。
        rule = self.match_prescription_rule(normalized_name)
        if rule is not None and rule["verdict"] == "RED_LINE":
            return self.apply_prescription_red_line(rule, drug_name)

        # The five catalogue cases are low-risk/general teaching scenarios.
        # They use the same safe API but must not inherit the aortic-dissection
        # red-line or rupture countdown from the high-risk case.
        if not self.high_risk_aortic:
            return self._apply_generic_medication(drug_name, normalized_name, result)

        # 1. Check for RED-LINE LETHAL CONTRAINDICATIONS
        is_lethal = any(keyword in normalized_name for keyword in config.LETHAL_MEDICATION_KEYWORDS)
        if is_lethal:
            self.trigger_iatrogenic_rupture(f"误用禁忌药物【{drug_name}】诱发内膜假腔破裂大出血")
            result["is_lethal"] = True
            result["is_red_line"] = True
            result["message"] = (
                f"【致命红线触发】在 Stanford A型主动脉夹层中禁忌使用【{drug_name}】！"
                "抗血小板/抗凝/溶栓药物导致撕裂内膜假腔暴发性破裂，触发 120s 致命心包填塞倒计时！"
            )
            result["new_state"] = self.state
            return result

        # 2. Check for TARGET BETA-BLOCKERS (Esmolol / Metoprolol / Labetalol)
        if any(bb in normalized_name for bb in ["esmolol", "艾司洛尔", "betaloc", "美托洛尔", "labetalol", "拉贝洛尔"]):
            self.beta_blocker_active = True
            self.active_medications["beta_blocker"] = dose
            result["message"] = f"已静脉泵注【{drug_name}】，负荷剂量起效中，开始抑制心肌收缩力(dp/dt)与心率。"
            self._evaluate_state_transition()
            result["new_state"] = self.state
            return result

        # 3. Check for TARGET VASODILATORS (Nitroprusside / Nicardipine / Nitroglycerin)
        if any(vd in normalized_name for vd in ["nitroprusside", "硝普钠", "nicardipine", "尼卡地平", "nitroglycerin", "硝酸甘油"]):
            self.vasodilator_active = True
            self.active_medications["vasodilator"] = dose
            if not self.beta_blocker_active:
                result["message"] = (
                    f"已静脉泵注【{drug_name}】。提示：在未充分使用β受体阻滞剂控制心率前单用血管扩张剂，"
                    "可能引起反射性心动过速并增加主动脉壁切应力(dp/dt)，建议立即联合艾司洛尔！"
                )
            else:
                result["message"] = f"已联合静脉泵注【{drug_name}】，双靶向血流动力学控制方案起效，收缩压平稳回落。"
            self._evaluate_state_transition()
            result["new_state"] = self.state
            return result

        # 4. Check for ANALGESIA / SEDATION (Morphine / Fentanyl)
        if any(ag in normalized_name for ag in ["morphine", "吗啡", "fentanyl", "芬太尼", "pethidine", "哌替啶"]):
            self.analgesic_active = True
            self.vitals.pain_vas = max(2, self.vitals.pain_vas - 5)
            result["message"] = f"已静脉注射【{drug_name}】，中枢镇痛起效，患者交感风暴减轻，疼痛评分降至 {self.vitals.pain_vas} 分。"
            return result

        # Generic acceptable medication
        result["message"] = f"已执行用药医嘱【{drug_name}】。"
        return result

    def _apply_generic_medication(self, drug_name: str, normalized_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a small, deterministic response for catalogue teaching cases."""
        if any(k in normalized_name for k in ["对乙酰氨基酚", "扑热息痛", "退热", "止痛", "布洛芬", "paracetamol", "ibuprofen"]):
            self.vitals.pain_vas = max(0, self.vitals.pain_vas - 1)
            self.vitals.hr = max(55.0, self.vitals.hr - 2.0)
            result["message"] = f"已执行【{drug_name}】，对症处理起效，疼痛/不适逐步缓解。"
        elif any(k in normalized_name for k in ["沙丁胺醇", "salbutamol", "雾化", "吸入"]):
            self.vitals.rr = max(12.0, self.vitals.rr - 2.0)
            self.vitals.spo2 = min(99.0, self.vitals.spo2 + 1.0)
            result["message"] = f"已执行【{drug_name}】，气道症状开始改善，持续观察呼吸状态。"
        elif any(k in normalized_name for k in ["葡萄糖", "glucose", "补液", "生理盐水", "口服补液"]):
            self.vitals.consciousness = "神志逐渐清楚，能够配合交流"
            self.vitals.hr = max(60.0, self.vitals.hr - 4.0)
            result["message"] = f"已执行【{drug_name}】，支持治疗已开始，复测生命体征与症状。"
        else:
            result["message"] = f"已执行用药医嘱【{drug_name}】，请结合病例目标观察疗效。"
        result["new_state"] = self.state
        return result

    def lethal_countdown_seconds(self) -> float:
        """Countdown length for a red-line drug, per case anatomy (D-33)."""
        if self.high_risk_aortic:
            return float(self.rupture_to_effusion_seconds)
        # 非夹层病例没有心包填塞解剖，红线药物走 30 s 速崩
        return float(config.PHYS_REDLINE_COLLAPSE_S)

    def trigger_iatrogenic_rupture(self, reason: str, countdown: Optional[float] = None) -> None:
        """Trigger lethal countdown. ``countdown`` overrides the case default (D-33)."""
        if self.state in [PatientStateEnum.S4_CARDIAC_TAMPONADE, PatientStateEnum.S5_AORTIC_RUPTURE, PatientStateEnum.S6_CARDIAC_ARREST]:
            return
        self.state = PatientStateEnum.S3_PERICARDIAL_EFFUSION
        self.rupture_countdown = float(self.lethal_countdown_seconds() if countdown is None else countdown)
        self.rupture_triggered_reason = reason
        self.vitals.consciousness = "痛苦呻吟，烦躁加剧，胸痛放射至全腹及背部"

    def advance_time(self, dt: float = 1.0) -> VitalsVector:
        """Advance physiological state by dt seconds."""
        self.elapsed_seconds += dt
        self._noise_phase += dt * 0.5

        # 1. Lethal Cardiac Arrest / Death State (irreversible for every case)
        if self.state == PatientStateEnum.S6_CARDIAC_ARREST:
            return self._freeze_death()

        # 2. Lethal red-line countdown runs for aortic *and* content-layer cases
        self._tick_lethal_countdown(dt)
        if self.state == PatientStateEnum.S6_CARDIAC_ARREST:
            return self.vitals

        if not self.high_risk_aortic:
            self._advance_generic(dt)
            return self.vitals

        # 3. Pathophysiological State Dynamics
        if self.state == PatientStateEnum.S0_STABLE:
            # If student does not lower BP for > crisis timeout, crisis escalates
            if self.elapsed_seconds > self.crisis_timeout_seconds and not (self.beta_blocker_active or self.vasodilator_active):
                self.state = PatientStateEnum.S1_HYPERTENSION_CRISIS

        if self.state == PatientStateEnum.S1_HYPERTENSION_CRISIS:
            # BP escalates dangerously
            self.vitals.sbp = min(220.0, max(202.0, self.vitals.sbp + 0.15 * dt))
            self.vitals.dbp = min(128.0, self.vitals.dbp + 0.08 * dt)
            self.vitals.hr = min(130.0, self.vitals.hr + 0.10 * dt)
            self.vitals.pain_vas = 10
            self.vitals.consciousness = "极度痛苦大叫，濒死感加重"
            # Spontaneous rupture if SBP stays > 215 for > 60s
            if self.vitals.sbp >= 215.0 and self.elapsed_seconds > 420.0:
                self.trigger_iatrogenic_rupture("未及时降压，恶性高血压切应力诱发夹层自发性破裂入心包")

        elif self.state == PatientStateEnum.S2_PHARMACO_CONTROLLED:
            # Smooth pharmacodynamics convergence to clinical targets
            target_hr = 65.0
            target_sbp = 112.0
            target_dbp = 70.0
            rate_hr = 0.03 * dt
            rate_bp = 0.04 * dt

            self.vitals.hr += (target_hr - self.vitals.hr) * rate_hr
            self.vitals.sbp += (target_sbp - self.vitals.sbp) * rate_bp
            self.vitals.dbp += (target_dbp - self.vitals.dbp) * rate_bp
            if self.vitals.pain_vas > 3:
                self.vitals.pain_vas = max(3, int(self.vitals.pain_vas - 0.05 * dt))
            self.vitals.ecg_rhythm = "Normal Sinus Rhythm (窦性心律)"
            self.vitals.consciousness = "神志清楚，胸痛明显缓解，呼吸平稳"

        elif self.state == PatientStateEnum.S3_PERICARDIAL_EFFUSION:
            # Blood leaking into pericardium
            self.vitals.sbp = max(80.0, self.vitals.sbp - 0.4 * dt)
            self.vitals.dbp = max(50.0, self.vitals.dbp - 0.3 * dt)
            self.vitals.hr = min(140.0, self.vitals.hr + 0.25 * dt)
            self.vitals.spo2 = max(88.0, self.vitals.spo2 - 0.05 * dt)
            self.vitals.ecg_rhythm = "Sinus Tachycardia with Low Voltage (低电压伴窦速)"

        elif self.state == PatientStateEnum.S4_CARDIAC_TAMPONADE:
            # Full Beck's Triad (Hypotension, JVD, Muffled heart sounds)
            self.vitals.sbp = max(45.0, self.vitals.sbp - 0.8 * dt)
            self.vitals.dbp = max(25.0, self.vitals.dbp - 0.5 * dt)
            self.vitals.hr = max(38.0, self.vitals.hr - 0.5 * dt)  # paradoxical bradycardia
            self.vitals.spo2 = max(70.0, self.vitals.spo2 - 0.3 * dt)
            self.vitals.consciousness = "呼之不应，意识丧失，面色苍白湿冷，贝氏三联征爆发"
            self.vitals.ecg_rhythm = "Severe Bradycardia with Electrical Alternans (电交替伴窦缓)"

        elif self.state == PatientStateEnum.S5_AORTIC_RUPTURE:
            # Terminal rupture shock
            self.vitals.sbp = max(0.0, self.vitals.sbp - 2.0 * dt)
            self.vitals.dbp = max(0.0, self.vitals.dbp - 1.5 * dt)
            self.vitals.hr = max(0.0, self.vitals.hr - 1.5 * dt)
            self.vitals.spo2 = max(0.0, self.vitals.spo2 - 2.0 * dt)
            self.vitals.consciousness = "心搏呼吸骤停，无自主意识"
            self.vitals.ecg_rhythm = "PEA (无脉性电活动) / 室颤波"

        # Apply realistic physiological micro-oscillations (Brownian noise)
        self._apply_realistic_noise()

        return self.vitals

    def _freeze_death(self) -> VitalsVector:
        """Zero every vital sign once cardiac arrest is reached."""
        self.vitals.hr = 0.0
        self.vitals.sbp = 0.0
        self.vitals.dbp = 0.0
        self.vitals.spo2 = 0.0
        self.vitals.rr = 0.0
        self.vitals.pain_vas = 0
        self.vitals.ecg_rhythm = "Asystole / PEA (心室停搏/无脉电活动)"
        self.vitals.consciousness = "深昏迷，双侧瞳孔散大固定，大动脉搏动消失 (已死亡)"
        return self.vitals

    def _tick_lethal_countdown(self, dt: float) -> None:
        """Advance the red-line countdown chain (S3 → S4 → S5 → S6)."""
        if self.rupture_countdown is None:
            return
        self.rupture_countdown = max(0.0, self.rupture_countdown - dt)
        if self.rupture_countdown > 0.0:
            return

        if not self.high_risk_aortic:
            # 非夹层病例：无心包填塞分期，倒计时到点即心搏骤停
            self.state = PatientStateEnum.S6_CARDIAC_ARREST
            self.fatal_death_occurred = True
            self.rupture_countdown = None
            self._freeze_death()
            return

        if self.state == PatientStateEnum.S3_PERICARDIAL_EFFUSION:
            self.state = PatientStateEnum.S4_CARDIAC_TAMPONADE
            self.vitals.sbp = min(self.vitals.sbp, 55.0)
            self.vitals.dbp = min(self.vitals.dbp, 35.0)
            self.vitals.hr = 45.0
            self.rupture_countdown = float(config.PHYS_TAMPONADE_STAGE_S)
        elif self.state == PatientStateEnum.S4_CARDIAC_TAMPONADE:
            self.state = PatientStateEnum.S5_AORTIC_RUPTURE
            self.rupture_countdown = float(config.PHYS_RUPTURE_TO_ARREST_S)
        elif self.state == PatientStateEnum.S5_AORTIC_RUPTURE:
            self.state = PatientStateEnum.S6_CARDIAC_ARREST
            self.fatal_death_occurred = True
            self.rupture_countdown = None
            self._freeze_death()

    def _advance_generic(self, dt: float) -> None:
        """Keep catalogue cases physiologically plausible without chest-pain escalation."""
        if self.state == PatientStateEnum.S6_CARDIAC_ARREST:
            self._freeze_death()
            return
        if self.rupture_countdown is not None:
            # 红线致死倒计时期间生命体征单向恶化，不再向基线回归
            self.vitals.hr = min(160.0, self.vitals.hr + 0.8 * dt)
            self.vitals.sbp = max(40.0, self.vitals.sbp - 1.0 * dt)
            self.vitals.spo2 = max(70.0, self.vitals.spo2 - 0.2 * dt)
            return
        # Gentle regression toward the configured case baseline.  It gives the
        # monitor realistic movement while avoiding fabricated deterioration.
        rate = min(0.08, max(0.01, 0.02 * dt))
        for field in ("hr", "sbp", "dbp", "spo2", "rr"):
            baseline = getattr(self.baseline_vitals, field)
            current = getattr(self.vitals, field)
            setattr(self.vitals, field, current + (baseline - current) * rate)
        self._apply_realistic_noise()

    def _evaluate_state_transition(self) -> None:
        """Evaluate if criteria for S2_PHARMACO_CONTROLLED are satisfied."""
        if self.state in [
            PatientStateEnum.S3_PERICARDIAL_EFFUSION,
            PatientStateEnum.S4_CARDIAC_TAMPONADE,
            PatientStateEnum.S5_AORTIC_RUPTURE,
            PatientStateEnum.S6_CARDIAC_ARREST
        ]:
            return

        if self.beta_blocker_active and self.vasodilator_active:
            self.state = PatientStateEnum.S2_PHARMACO_CONTROLLED

    def _apply_realistic_noise(self) -> None:
        """Inject micro-fluctuations so bedside monitor looks authentic."""
        if self.state in [PatientStateEnum.S5_AORTIC_RUPTURE, PatientStateEnum.S6_CARDIAC_ARREST]:
            return

        # Micro-variation ±0.6 bpm, ±0.8 mmHg
        wave = math.sin(self._noise_phase) * 0.6
        noise = random.uniform(-0.4, 0.4)
        total_jitter = wave + noise

        self.vitals.hr = max(20.0, round(self.vitals.hr + total_jitter * 0.5, 1))
        self.vitals.sbp = max(30.0, round(self.vitals.sbp + total_jitter * 0.8, 1))
        self.vitals.dbp = max(15.0, round(self.vitals.dbp + total_jitter * 0.4, 1))
        self.vitals.rr = max(8.0, round(self.vitals.rr + (random.uniform(-0.2, 0.2)), 1))
        self.vitals.spo2 = min(100.0, max(60.0, round(self.vitals.spo2 + (random.uniform(-0.1, 0.1)), 1)))

    def get_current_vitals(self) -> VitalsVector:
        """Retrieve current vitals snapshot."""
        return self.vitals.model_copy()

    def get_state(self) -> PatientStateEnum:
        """Retrieve current pathophysiological state."""
        return self.state

    def get_rupture_countdown(self) -> Optional[int]:
        """Return remaining rupture seconds, or None if safe."""
        if self.rupture_countdown is not None:
            return int(self.rupture_countdown)
        return None
