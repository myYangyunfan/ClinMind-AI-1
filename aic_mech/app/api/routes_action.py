"""Clinical Action Execution and Decision Router REST Routes."""

import random
import time
import uuid
from typing import Dict, Any, List, Callable, Optional
from fastapi import APIRouter, HTTPException
from app.models.schemas import ActionRequest, ActionResponse, DialogueMessage
from app.models.entities import ActionRecord, ActionType, PatientStateEnum
from app.core.session_manager import SessionStore
from app.core.guardrail_engine import exam_refusal_settings, lab_pending_threshold_seconds
from app.core import llm_guard
import config

router = APIRouter(prefix="/api/action", tags=["Action Execution"])


# ================= 情绪事件映射（契约 05 §4） =================
# 安抚语义：问诊/处置的自由文字命中这些词才算 COMFORT，60 s 冷却由
# EmotionEngine 记账。刻意不收「止痛/镇痛/控制血压」——那些是处置动作，
# 不是安抚；把医嘱误记成安抚会让信任度虚高。
_COMFORT_KEYWORDS = (
    "安慰", "别紧张", "放松", "别害怕", "别慌", "耐心", "理解",
    "放心", "我们会", "我们全力救治", "全力救治", "信任我们",
    "陪着你", "陪您", "会好起来",
)
# 「解释检查目的」：体查文字里出现这些词，说明学生在向患者解释而非单纯查体。
_EXPLAIN_KEYWORDS = ("解释", "说明", "告知", "为什么", "目的", "意义", "必要性")

_QUESTION_KIND_TO_EVENT = {
    "relevant": "RELEVANT_QUESTION",
    "relevant_question": "RELEVANT_QUESTION",
    "切题": "RELEVANT_QUESTION",
    "repeat": "REPEAT_QUESTION",
    "repeated": "REPEAT_QUESTION",
    "duplicate": "REPEAT_QUESTION",
    "重复": "REPEAT_QUESTION",
    "irrelevant": "IRRELEVANT_QUESTION",
    "unrelated": "IRRELEVANT_QUESTION",
    "off_topic": "IRRELEVANT_QUESTION",
    "无关": "IRRELEVANT_QUESTION",
}


def _hit(text: str, keywords) -> bool:
    haystack = (text or "").lower()
    return any(k in haystack for k in keywords)


# ================= 体查拒绝 / 检查 TAT（验收 GWT 04、05） =================
# 概率源做成模块级可调用对象，测试直接 monkeypatch ``_refusal_rng``。
_refusal_rng: Callable[[], float] = random.random


def _exam_refused(session, rng: Optional[Callable[[], float]] = None) -> bool:
    """患者是否拒绝配合本次体查：焦虑达阈值后按配置概率掷一次。

    阈值与概率来自 ``guardrail_engine.exam_refusal_settings()``（config 可覆盖），
    这里只做判决，不记账。
    """
    threshold, probability = exam_refusal_settings()
    anxiety = float(session.emotion.snapshot().get("patient_anxiety", 0.0))
    roll = float((rng or _refusal_rng)())
    return anxiety >= threshold and roll < probability


def _advance_virtual_time(session, seconds: float) -> int:
    """把操作耗时记到会话自己的虚拟时钟上（同一个时钟，不另造计时器）。

    只推 ``virtual_seconds``，不在动作里再 settle 生理/情绪：那 1 Hz 的
    ``SessionStore.tick_all`` 本来就在推进病程与情绪结算，动作里再结算一次
    等于把同一段时间算两遍。更关键的是床旁操作耗时是「操作节奏成本」——
    查体所见已经由 guardrail 按病例数据给出，若这 300 秒还要把患者推到
    S6，则「焦虑 0.92 时反复采样拒绝概率」根本采样不完（本病例 untreated
    在 ~590 s 就进心脏骤停）。单次动作上限 1 小时，防呆。
    """
    total = int(max(0.0, min(float(seconds or 0.0), 3600.0)))
    if total <= 0:
        return 0
    session.virtual_seconds += total
    return total


def _pending_checks(session) -> Dict[str, Dict[str, Any]]:
    """会话级待回报登记簿（同一检查按 panel_id 去重）。

    SessionManager 目前不持久化这个字段，进程内重启后待回报单会重新排队；
    对实训场景可接受，等 engine-dev 把字段收进 snapshot 再升级。
    """
    pending = getattr(session, "pending_checks", None)
    if not isinstance(pending, dict):
        pending = {}
        try:
            session.pending_checks = pending
        except AttributeError:  # pragma: no cover - 未来若加了 __slots__
            pass
    return pending


def _split_inquiry_result(result):
    """兼容 agent_engine.handle_inquiry 的多种返回形态。

    当前返回 List[DialogueMessage]，还没有切题/重复/无关的判定；若后续改成
    ``(dialogues, question_kind)``、``{"dialogues": ..., "question_kind": ...}``
    或带 ``question_kind`` 属性的对象，也能直接取到判定，不必再改这里。
    拿不到判定时返回 kind=None，由调用方兜底（契约 05 §4：不得瞎猜）。
    """
    kind = None
    dialogues = result
    if isinstance(result, tuple) and len(result) == 2:
        first, second = result
        if isinstance(first, list):
            dialogues, kind = first, second
        elif isinstance(second, list):
            kind, dialogues = first, second
    elif isinstance(result, dict):
        dialogues = result.get("dialogues") or result.get("messages") or []
        kind = result.get("question_kind")
    else:
        kind = getattr(result, "question_kind", None)
        if kind is not None:
            dialogues = getattr(result, "dialogues", dialogues)
    if not isinstance(dialogues, list):
        dialogues = []
    if isinstance(kind, str):
        kind = kind.strip().lower() or None
    return dialogues, kind


def _apply_emotion(session, code: str, ctx: Dict[str, Any]) -> bool:
    """调 EmotionEngine 落一次情绪事件，返回是否真的落账。

    引擎缺席（老会话/未接线的会话）或内部异常时不拖垮动作主流程：
    诊断与抢救优先，情绪是旁路。
    """
    if not code:
        return False
    emotion = getattr(session, "emotion", None)
    if emotion is None:
        return False
    try:
        result = emotion.apply_event(code, ctx)
    except TypeError:
        # 引擎实现成 apply_event(code) 也能用（契约里 ctx 为可选）。
        try:
            result = emotion.apply_event(code)
        except Exception:
            return False
    except Exception:
        return False
    if isinstance(result, dict) and "applied" in result:
        return bool(result["applied"])
    return True


# ============ LLM 输出后置校验（spec/04 §3.6 三道闸）============
# 患者 / 家属台词是模型生成的自由文本，进入对话前必须过 A1 长度、A2 禁词；
# 命中就换成离线兜底句，绝不把诊断名和剂量递给学员。医生、护士台词是本地
# 模板，不送闸（否则会误伤"阿司匹林禁忌"这类教学提示）。
_LLM_FALLBACK_LINE = "（患者迟疑了一下，没有接话）"


def _guard_llm_dialogues(dialogues: List[DialogueMessage]) -> List[str]:
    """就地清洗模型台词，返回命中原因列表（空列表 = 全部通过）。"""
    _, reasons = llm_guard.guard_dialogues(
        dialogues,
        _LLM_FALLBACK_LINE,
        source="agent_engine",
        enforce_length=bool(config.is_online_llm_active()),
    )
    return reasons


# ============ 处方红线（病例 JSON prescription_rules）============
# 规则匹配与 severity 判罚都在 PhysiologyEngine 里完成（apply_medication 的
# 第 0 步会先按病例 prescription_rules 匹配，命中 RED_LINE 就按 LETHAL /
# HARMFUL 返回 is_lethal / is_harmful）。这里只负责把引擎的判定翻译成学员
# 看得见的护栏反馈与报警，不重复实现匹配，避免两套数值互相叠加。


@router.post("/execute", response_model=ActionResponse)
def execute_action(req: ActionRequest):
    """Execute a student clinical decision action."""
    store = SessionStore.get_instance()
    session = store.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Active session not found")

    if session.is_finished:
        raise HTTPException(status_code=400, detail="本次实训会话已结案，无法继续下达医嘱。")

    action_id = f"act_{uuid.uuid4().hex[:8]}"
    content = req.content.strip()
    action_type = req.action_type
    current_state = session.physiology_engine.get_state()

    dialogues: List[DialogueMessage] = []
    report_dict = None
    is_contraindicated = False
    warning_text = None
    order_status = "executed"
    feedback_message = ""
    # 体查拒绝/耗时与检查 TAT（验收 GWT 04、05）。
    refused = False
    status = "executed"
    elapsed_seconds = 0.0
    available_at_seconds: Optional[float] = None
    remaining_seconds: Optional[float] = None

    # 情绪事件（契约 05 §4）：分支里定 code，动作执行完统一落账。
    emotion_code = None
    emotion_applied = False
    emotion_ctx: Dict[str, Any] = {}
    inquiry_kind = None

    # ================= 1. INQUIRY (问诊沟通) =================
    if action_type in (ActionType.INQUIRY, "inquiry", "ask"):
        inquiry_result = session.agent_engine.handle_inquiry(content, current_state)
        dialogues, inquiry_kind = _split_inquiry_result(inquiry_result)
        _guard_llm_dialogues(dialogues)
        emotion_ctx = {"question_kind": inquiry_kind}
        if _hit(content, _COMFORT_KEYWORDS):
            # 安抚语义优先于问诊三分类：学生这句话的疗效来自安抚本身。
            emotion_code, emotion_applied = "COMFORT", True
        elif inquiry_kind in _QUESTION_KIND_TO_EVENT:
            emotion_code, emotion_applied = _QUESTION_KIND_TO_EVENT[inquiry_kind], True
        else:
            # 拿不到判定就不猜：按 RELEVANT_QUESTION 兜底落账，但如实回
            # emotion_applied=false，让上层知道这次判定是缺省的。
            emotion_code, emotion_applied = "RELEVANT_QUESTION", False
        feedback_message = f"已完成问诊沟通：【{content}】"

    # ================= 2. PHYSICAL EXAM (专科体查) =================
    elif action_type in (ActionType.PHYSICAL_EXAM, "physical_exam", "exam"):
        matched, report, msg = session.guardrail_engine.route_physical_exam(content)
        procedure = float(session.guardrail_engine.procedure_seconds(content) or 0.0)
        emotion_ctx = {"item": content, "matched": matched}
        # GWT 04：焦虑到阈值后按配置概率拒绝，被拒也照样消耗操作耗时。
        if _exam_refused(session):
            refused = True
            report = None
            order_status = "refused"
            status = "refused"
            elapsed_seconds = float(_advance_virtual_time(session, procedure))
            feedback_message = (
                f"【患者拒绝配合】患者当前焦虑水平过高，拒绝配合「{content}」，检查未能完成。"
                f"本次尝试已消耗 {int(elapsed_seconds)} 秒；请先安抚患者、解释检查目的后再试。"
            )
            dialogues.append(session.agent_engine.notify_order_dispatched(f"体格检查：{content}（患者拒绝）"))
            dialogues.append(DialogueMessage(
                speaker="patient",
                text="我现在很难受，不想再做什么检查了……",
                timestamp=time.time() + 0.3,
                role_title="患者 (拒绝配合)",
            ))
            # 引擎里 EXAM_REFUSED 的 delta 就是为这条支路准备的。
            emotion_code, emotion_applied = "EXAM_REFUSED", True
        else:
            if _hit(content, _EXPLAIN_KEYWORDS):
                emotion_code, emotion_applied = "EXPLAIN_EXAM", True
            else:
                emotion_code, emotion_applied = "EXAM_CORRECT", True
            # GWT 04：未被拒时按 exam_options[].duration_seconds 推进虚拟时间。
            elapsed_seconds = float(_advance_virtual_time(session, procedure))
            feedback_message = msg
            if procedure > 0:
                feedback_message = f"{msg}\n（本次检查操作耗时 {int(procedure)} 秒）"
            nurse_msg = session.agent_engine.notify_order_dispatched(f"体格检查：{content}")
            dialogues.append(nurse_msg)
            # 紧接着将客观体查读数与结论以床旁回报消息推入会话流
            if msg:
                report_msg = DialogueMessage(
                    speaker="nurse",
                    text=msg,
                    timestamp=time.time() + 0.5,
                    role_title="责任护士 (床旁体查回报)"
                )
                dialogues.append(report_msg)

            # 格式化客观凭证卡片数据
            if report:
                norm = dict(report)
                norm["title"] = report.get("title") or report.get("item_name") or f"体格检查：{content}"
                norm["modality"] = report.get("modality") or "EXAM"
                norm["conclusion"] = report.get("conclusion") or report.get("interpretation") or (report.get("findings") if isinstance(report.get("findings"), str) else "") or "双侧血压对比明显不对称，提示大血管病变可能"
                details = {}
                if "right_arm" in report:
                    details["右上臂血压"] = report.get("right_arm")
                    details["左上臂血压"] = report.get("left_arm")
                    details["双臂压差"] = report.get("difference")
                    if report.get("interpretation"):
                        details["临床提示"] = report.get("interpretation")
                elif "findings" in report and isinstance(report["findings"], str):
                    details["查体所见"] = report["findings"]
                norm["details"] = details
                report_dict = norm

    # ================= 3. LAB / IMAGING ORDER (辅助检查) =================
    elif action_type in (ActionType.LAB_ORDER, "lab_order", "order", "lab"):
        matched, report, msg = session.guardrail_engine.route_lab_or_imaging(
            content, session.physiology_engine.get_current_vitals()
        )
        # 02 §1.3 E05 的触发源就是「检查下单」，每项检查 1 次由引擎记账。
        emotion_ctx = {"item": content, "matched": matched}
        emotion_code, emotion_applied = "EXPLAIN_EXAM", True
        feedback_message = msg

        # GWT 05：TAT 到期前只回「已送检」，不给报告。同一项检查按 panel_id
        # 去重——到期前重复下单仍回 pending 并带剩余秒数，到点后才转 ready。
        panel = session.guardrail_engine.find_lab_panel(content) or {}
        report_meta = report or {}
        tat = float(report_meta.get("tat_seconds") or panel.get("tat_seconds") or 0.0)
        item_name = report_meta.get("item_name") or panel.get("name") or content
        if tat > 0 and tat > lab_pending_threshold_seconds():
            pending = _pending_checks(session)
            key = str(report_meta.get("panel_id") or panel.get("panel_id") or content.strip().lower())
            now = float(session.virtual_seconds)
            entry = pending.get(key)
            if entry is None:
                entry = {
                    "item_name": item_name,
                    "order": content,
                    "tat_seconds": tat,
                    "ordered_at_seconds": now,
                    "available_at_seconds": now + tat,
                }
                pending[key] = entry
            available_at = float(entry["available_at_seconds"])
            if now < available_at:
                remaining = max(0.0, available_at - now)
                report = None
                order_status = "pending"
                status = "pending"
                available_at_seconds = available_at
                remaining_seconds = remaining
                feedback_message = (
                    f"【已送检·等待回报】{item_name} 已送检，TAT 约 {int(tat)} 秒，"
                    f"当前剩余 {int(remaining)} 秒，报告尚未回报。"
                )
                dialogues.append(session.agent_engine.notify_order_dispatched(f"辅助检查开单送检：{content}"))
                dialogues.append(DialogueMessage(
                    speaker="nurse",
                    text=f"标本/患者已送达，{item_name} 预计 {int(remaining)} 秒后出结果。",
                    timestamp=time.time() + 0.4,
                    role_title="急诊检验/放射科 (接收回执)",
                ))
            else:
                pending.pop(key, None)
                order_status = "ready"
                status = "ready"
                dialogues.append(session.agent_engine.notify_order_dispatched(f"辅助检查开单送检：{content}"))
        else:
            order_status = "ready"
            status = "ready"
            dialogues.append(session.agent_engine.notify_order_dispatched(f"辅助检查开单送检：{content}"))

        # 将报告出具通知同步推入会话流（pending 时 report 为空，不推报告卡）
        if report and msg:
            report_msg = DialogueMessage(
                speaker="nurse",
                text=msg,
                timestamp=time.time() + 0.8,
                role_title="急诊检验/放射科 (报告回报)"
            )
            dialogues.append(report_msg)
            
        # 格式化客观检查报告单
        if report:
            norm = dict(report)
            norm["title"] = report.get("title") or report.get("item_name") or f"辅助检查：{content}"
            norm["modality"] = report.get("modality") or "LAB"
            norm["conclusion"] = report.get("conclusion") or report.get("interpretation") or "检查报告已签发"
            details = {}
            if "result" in report:
                details["检测数值"] = report.get("result")
                if report.get("ref_range"):
                    details["参考区间"] = report.get("ref_range")
                if report.get("status"):
                    details["检验状态"] = report.get("status")
            elif "parameters" in report and isinstance(report["parameters"], dict):
                for pk, pv in report["parameters"].items():
                    details[str(pk).upper()] = str(pv)
            elif "findings" in report:
                if isinstance(report["findings"], list):
                    for idx, f in enumerate(report["findings"], 1):
                        details[f"征象{idx}"] = f
                elif isinstance(report["findings"], str):
                    details["影像所见"] = report["findings"]
            norm["details"] = details
            report_dict = norm

    # ================= 4. MEDICATION (药物处方) =================
    elif action_type in (ActionType.MEDICATION, "medication", "treat", "prescription"):
        try:
            dose = float(req.params.get("dose", 1.0)) if req.params else 1.0
            if not (0 < dose < float("inf")):
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(422, "剂量须为有限正数")
        unit = str(req.params.get("unit", "mg")) if req.params else "mg"
        engine = session.physiology_engine
        # apply_medication 第 0 步已按病例 prescription_rules 匹配红线并完成
        # severity 判罚（LETHAL 走致死倒计时、HARMFUL 走可逆恶化+累计升级）。
        med_result = engine.apply_medication(content, dose, unit)
        is_red_line = bool(med_result.get("is_red_line"))
        lethal = bool(med_result.get("is_lethal"))
        harmful = bool(med_result.get("is_harmful")) and not lethal
        escalated = bool(med_result.get("harmful_escalated"))

        emotion_ctx = {
            "item": content,
            "dose": dose,
            "unit": unit,
            "rule_id": med_result.get("rule_id"),
            "verdict": med_result.get("verdict"),
            "severity": med_result.get("severity"),
            "red_line": is_red_line,
        }
        if lethal:
            is_contraindicated = True
            warning_text = med_result.get("message")
            feedback_message = warning_text
            # Nurse emergency intercept alarm
            nurse_warn = session.agent_engine.generate_nurse_warning(content)
            dialogues.append(nurse_warn)
            # 红线：判罚归引擎/病例规则，情绪层只记账不越权。
            emotion_code, emotion_applied = "RED_LINE_MED", True
        elif harmful:
            # 可恢复恶化：不设 is_contraindicated（那不是一票否决），但必须在
            # 响应里给出护栏反馈——之前这条路径是静默的。
            warning_text = med_result.get("message") or (
                f"【有害红线】{content} 在本病例并非首选，已出现可逆性恶化，请立即纠正。"
            )
            feedback_message = warning_text
            if escalated:
                feedback_message = f"{warning_text}【累计恶化】有害处方累计达限，病程已进入不可逆恶化。"
            dialogues.append(session.agent_engine.generate_nurse_warning(content))
            emotion_code, emotion_applied = "RED_LINE_MED", True
        else:
            feedback_message = med_result.get("message")
            nurse_msg = session.agent_engine.notify_order_dispatched(f"用药：{content} {dose}{unit}")
            dialogues.append(nurse_msg)
            emotion_code, emotion_applied = "CORRECT_TREATMENT", True

    # ================= 5. RESCUE / DISPOSITION (急救与终末转归) =================
    elif action_type in (ActionType.RESCUE_DISPOSITION, "rescue_disposition", "dispo", "rescue"):
        c_lower = content.lower()
        emotion_ctx = {"item": content}
        if _hit(content, _COMFORT_KEYWORDS):
            # E04 的触发源写明是「问诊/处置」，处置里的安抚话同样计入。
            emotion_code, emotion_applied = "COMFORT", True
        if any(k in c_lower for k in ["吸氧", "双通道", "静脉通路", "监护", "抢救"]):
            feedback_message = "【抢救通道建立】已予鼻导管高流量吸氧，快速开放左右上肢双侧静脉通道，持续多参数心电血压监护！"
            nurse_msg = session.agent_engine.notify_order_dispatched("建立双通道静脉通路与吸氧监护")
            dialogues.append(nurse_msg)

        elif any(k in c_lower for k in ["心胸外", "外科", "急会诊", "血管外", "手术室"]):
            feedback_message = "【多学科急救响应】已紧急呼叫心胸外科与大血管外科二线主任急会诊！手术室及体外循环机已进入紧急备班状态！"
            nurse_msg = session.agent_engine.notify_order_dispatched("紧急通知心胸外科主任床旁急会诊")
            dialogues.append(nurse_msg)

        elif any(k in c_lower for k in ["知情同意", "病危", "沟通", "交代", "签字"]):
            feedback_message = "【知情同意交代】已向家属详尽交待急性主动脉夹层极高破裂风险，家属李秀梅已含泪签署急诊开胸手术知情同意书及病危通知书。"
            consent_dialogues = session.agent_engine.handle_inquiry("签署知情同意与病危通知", current_state)
            _guard_llm_dialogues(consent_dialogues)
            dialogues.extend(consent_dialogues)
            if emotion_code is None:
                # 02 §1.3 E12（知情同意/签字）的 delta 已内置在引擎里，且注释
                # 写明是留给 API 层的；这条支路本身就是判定，不是猜。
                emotion_code, emotion_applied = "CONSENT", True

        elif any(k in c_lower for k in ["cpr", "心肺复苏", "胸外按压", "电除颤", "心包穿刺"]):
            if current_state in [PatientStateEnum.S4_CARDIAC_TAMPONADE, PatientStateEnum.S5_AORTIC_RUPTURE]:
                feedback_message = "【心肺复苏执行】立即开展胸外心脏按压与床旁超声引导下心包穿刺引流！全力维持脑血流灌注！"
            else:
                feedback_message = "【临床提示】患者当前自主心搏节律尚存，未发生心室停搏或室颤，无需盲目电除颤。"

        else:
            feedback_message = f"已执行转归处置：{content}"
            nurse_msg = session.agent_engine.notify_order_dispatched(content)
            dialogues.append(nurse_msg)

    # Check for spontaneous alarms
    vitals = session.physiology_engine.get_current_vitals()
    new_state = session.physiology_engine.get_state()

    session.agent_engine.observe_action(action_type, content, safe=not is_contraindicated)

    # 情绪事件统一落账（契约 05 §4）：动作已执行完，生理/护理副作用都已生效。
    # 注意 emotion_applied 只是"判定是否可信"的标志，不能短路掉 apply_event：
    # 问诊拿不到判定时也要按 RELEVANT_QUESTION 真的落账一次。
    if emotion_code:
        emotion_ctx.setdefault("content", content)
        emotion_ctx.setdefault(
            "action_type",
            action_type.value if isinstance(action_type, ActionType) else str(action_type),
        )
        emotion_ctx.setdefault("virtual_seconds", session.virtual_seconds)
        applied_ok = _apply_emotion(session, emotion_code, emotion_ctx)
        emotion_applied = emotion_applied and applied_ok

    if new_state in [PatientStateEnum.S4_CARDIAC_TAMPONADE, PatientStateEnum.S5_AORTIC_RUPTURE]:
        dialogues.append(session.agent_engine.generate_tamponade_alert(vitals.sbp))

    # Append to session action history
    action_record = ActionRecord(
        action_id=action_id,
        step_id="",
        action_type=action_type,
        content=content,
        params=req.params or {},
        timestamp=time.time(),
        virtual_time_seconds=session.virtual_seconds,
        result=report_dict or feedback_message,
        is_contraindicated=is_contraindicated,
        warning_message=warning_text
    )
    session.action_history.append(action_record)
    if action_type != ActionType.INQUIRY:
        session.dialogues.append(DialogueMessage(speaker="doctor", text=content, role_title="接诊医生"))
    session.dialogues.extend(dialogues)
    if warning_text:
        # 致命与可逆红线都要进报警列表：可逆恶化同样需要学员看到并纠正。
        session.agent_engine.state.active_critical_alarms.append(warning_text)
    session.final_assessment = None
    store.persist(session)

    return ActionResponse(
        action_id=action_id,
        success=True,
        action_type=action_type,
        message=feedback_message,
        dialogues=dialogues,
        report=report_dict,
        order_status=order_status,
        vitals=vitals,
        patient_state=new_state,
        warning=warning_text,
        is_contraindicated=is_contraindicated,
        emotion_applied=emotion_applied,
        refused=refused,
        elapsed_seconds=elapsed_seconds,
        status=status,
        available_at_seconds=available_at_seconds,
        remaining_seconds=remaining_seconds,
    )
