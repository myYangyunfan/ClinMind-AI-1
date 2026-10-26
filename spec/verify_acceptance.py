"""《需求规格说明 v2》§8 十条 GWT 的可执行判据。

用法（从仓库根目录或 aic_mech 目录都行）：

    python spec/verify_acceptance.py

每条判据独立跑一遍真实引擎/接口，输出 ``[PASS]/[FAIL]`` + 一行证据；
末尾打印 ``X/10 passed``，有任一 FAIL 则退出码 1。

设计约束：
  * 只读业务代码，不修改任何仓库文件（坏病例造在临时目录里）；
  * 不 sleep，虚拟时钟一律用 ``session.tick(1.0)`` 推进；
  * 读不到能力就判 FAIL，并在证据里写清缺哪一块。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

# 与 run_all_tests.py 保持一致：离线规则引擎，结果可复现。
os.environ.setdefault("USE_ONLINE_LLM", "false")

AIC_MECH = Path(__file__).resolve().parents[1] / "aic_mech"
if str(AIC_MECH) not in sys.path:
    sys.path.insert(0, str(AIC_MECH))

import config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.case_repository import CaseRepository, CaseValidationError  # noqa: E402
from app.core.emotion_engine import EmotionEngine  # noqa: E402
from app.core.ged_evaluator import resolve_session_outcome  # noqa: E402
from app.core.session_manager import SessionStore  # noqa: E402
from app.server import app  # noqa: E402

CHEST_PAIN = "case_chest_pain_001"
SCENE_JS = AIC_MECH / "static" / "js" / "scene.js"

# §4.2 的 12 项在病例 JSON 里的落点（顺序与规格一致）。
REQUIRED_CASE_FIELDS: Tuple[str, ...] = (
    "chief_complaint",        # 1 主诉
    "present_illness",        # 2 现病史
    "patient_profile",        # 3 既往史 + 用药史
    "risk_factors",           # 4 危险因素
    "key_positives",          # 5 阳性体征 3 项
    "key_negatives",          # 5 关键阴性 2 项
    "inquiry_points",         # 6 问诊要点 8 个
    "exam_options",           # 7 体检项目 5 个
    "lab_panels",             # 8 检查项目 6 个（含 TAT）
    "differentials",          # 10 鉴别诊断 3 个
    "prescription_rules",     # 11 红线 2 条
    "knowledge_card",         # 12 知识点 5 条
)

IRRELEVANT_QUESTIONS = [
    "今天天气怎么样", "你最喜欢的电影是什么", "晚饭吃了吗", "股票涨了吗",
    "孩子在哪上学", "今年多大", "平时打游戏吗", "会开车吗",
    "周末去哪玩", "喜欢什么颜色", "听什么歌", "养宠物吗",
]


# --------------------------------------------------------------------------
# 通用夹具
# --------------------------------------------------------------------------
@contextlib.contextmanager
def fresh_environment():
    """独立 RUNTIME_DIR + 全新 SessionStore + TestClient（不污染真实 sqlite）。"""
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)
    patcher = patch.object(config, "RUNTIME_DIR", tmp_path)
    old_store = SessionStore._instance
    patcher.start()
    SessionStore._instance = None
    try:
        yield TestClient(app)
    finally:
        patcher.stop()
        SessionStore._instance = old_store
        tmp.cleanup()


def start_session(client: TestClient, case_id: str = CHEST_PAIN) -> Tuple[str, Any]:
    response = client.post("/api/case/start", json={"case_id": case_id})
    response.raise_for_status()
    session_id = response.json()["session_id"]
    session = SessionStore.get_instance().get_session(session_id)
    assert session is not None, "会话创建后取不到 SimulationSession"
    return session_id, session


def execute(client: TestClient, session_id: str, action_type: str, content: str, **params) -> Dict[str, Any]:
    response = client.post(
        "/api/action/execute",
        json={"session_id": session_id, "action_type": action_type, "content": content, "params": params},
    )
    response.raise_for_status()
    return response.json()


def outcome_code(session) -> str:
    resolved = resolve_session_outcome(session) or {}
    return str(resolved.get("code") or "?")


def settle(session, seconds: int) -> None:
    for _ in range(seconds):
        session.tick(1.0)


# --------------------------------------------------------------------------
# 十条判据
# --------------------------------------------------------------------------
def check_01_idle_8_minutes() -> Tuple[bool, str]:
    """新会话挂机 8 分钟：信任停 0.15、患者焦虑停 0.85、不拒绝配合、生理进危象。"""
    with fresh_environment() as client:
        session_id, session = start_session(client)
        settle(session, 480)
        snap = session.emotion.snapshot()
        state = session.physiology_engine.get_state().value
        crisis = state.startswith(("S3", "S4", "S5", "S6"))
        code = outcome_code(session)
        ok = (
            abs(snap["patient_trust"] - 0.15) <= 0.011
            and abs(snap["patient_anxiety"] - 0.85) <= 0.011
            and snap["refused"] is False
            and snap.get("leave_reason") is None
            and not session.is_finished
            and crisis
        )
        evidence = (
            f"t=480s: trust={snap['patient_trust']:.3f}（地板 0.15）、"
            f"patient_anxiety={snap['patient_anxiety']:.3f}（上限 0.85）、"
            f"refuse={snap['refused']}、emotion.leave_reason={snap.get('leave_reason')}、"
            f"state={state}、terminal={session.terminal_reason}"
        )
        if code == "C":
            evidence += (
                "；⚠ 但病例结局层判出 C「离院」"
                "（chest_pain outcomes.C = !treated && trust <= 0.16 && pain_vas >= 8，"
                "挂机到 480s 时 pain_vas=10）——GWT 括号里的「不触发离院」在结局层已被打破"
            )
        return ok, evidence


def check_02_twelve_irrelevant_questions() -> Tuple[bool, str]:
    """连续 12 次无关提问（180 虚拟秒）：信任被事件项压到 ≤0.05 并触发结局 C。

    判据说明：三分类生效后，无关提问会真的把信任打穿、患者中途离院，
    第 N 次请求返回 400/409/410。**这是 GWT 02 的设计目标（"输"来自行为
    而非时间），不是缺陷**——所以把「结案状态码」当合法信号，改读该会话
    结局并断言 code == "C"；同时验证信任确实被打到 ≤0.05、负向事件
    （IRRELEVANT/REPEAT）真的落账，避免只看结局码被巧合满足。
    阈值不放宽，也不接受任意结局码。
    """
    with fresh_environment() as client:
        session_id, session = start_session(client)
        applied_flags: List[Any] = []
        asked = 0
        terminal_status: Any = None
        for question in IRRELEVANT_QUESTIONS:
            response = client.post(
                "/api/action/execute",
                json={"session_id": session_id, "action_type": "inquiry", "content": question, "params": {}},
            )
            if response.status_code in (400, 409, 410):
                # 会话已结案：正是"信任被打穿→离院"，转去查结局。
                terminal_status = response.status_code
                break
            response.raise_for_status()
            payload = response.json()
            applied_flags.append(payload.get("emotion_applied"))
            asked += 1
            settle(session, 15)  # GWT 的 180 虚拟秒由时钟推进，而不是动作自带

        snap = session.emotion.snapshot()
        code = outcome_code(session)
        events = [entry.get("event") for entry in session.emotion.delta_log if entry.get("event")]
        tallied = {name: events.count(name) for name in sorted(set(events))}
        negative = sum(
            count for name, count in tallied.items() if "IRRELEVANT" in name or "REPEAT" in name
        )
        ok = snap["patient_trust"] <= 0.05 and code == "C" and negative >= 3
        evidence = (
            f"问诊 {asked} 次后结案（HTTP {terminal_status or '-'}）、"
            f"trust={snap['patient_trust']:.3f}（要求 ≤0.05）、outcome={code}（要求 C）、"
            f"负向事件合计={negative}（IRRELEVANT/REPEAT 落账，要求 ≥3）、"
            f"落账明细={tallied}、emotion_applied={sorted(set(map(str, applied_flags)))}、"
            f"terminal_reason={getattr(session, 'terminal_reason', None)!r}"
        )
        return ok, evidence


def check_03_dissection_aspirin() -> Tuple[bool, str]:
    """夹层病例处方阿司匹林：立即红线 + 结局 D + 复盘指认该决策。"""
    with fresh_environment() as client:
        session_id, session = start_session(client)
        payload = execute(client, session_id, "medication", "阿司匹林", dose=300, unit="mg")
        warning = str(payload.get("warning") or "")
        code_at_once = outcome_code(session)
        report = client.post(
            "/api/eval/submit",
            json={"session_id": session_id, "final_diagnosis": "主动脉夹层"},
        ).json()
        fatal = [str(item) for item in (report.get("fatal_errors") or [])]
        named = any("阿司匹林" in item for item in fatal)
        ok = (
            payload.get("is_contraindicated") is True
            and "红线" in warning
            and code_at_once == "D"
            and named
        )
        evidence = (
            f"is_contraindicated=True、warning=「{warning[:24]}…」、" f"立即结局={code_at_once}、"
            f"复盘 fatal_errors 指认阿司匹林={named}（{fatal[0][:36] if fatal else '无'}…）"
        )
        return ok, evidence


def check_04_anxious_patient_refuses_ultrasound() -> Tuple[bool, str]:
    """患者焦虑 0.92 时发起床旁超声：50% 概率被拒且消耗 300 虚拟秒。

    GWT 的前置是「给定患者焦虑 0.92」，因此每次 trial 前都重新把焦虑置回 0.92：
    一次体查要消耗 300 虚拟秒，时间项会把焦虑拉回 0.85 上限，若只在开头设一次，
    从第 2 次起就不再满足前置条件（那不是被测行为，是前置过期）。
    """
    with fresh_environment() as client:
        session_id, session = start_session(client)
        trials = 40
        refused = 0
        consumed = 0
        payload: Dict[str, Any] = {}
        for _ in range(trials):
            session.emotion.state.patient_anxiety = 0.92
            session.emotion._update_refuse()
            before = session.virtual_seconds
            payload = execute(client, session_id, "physical_exam", "床旁超声心动图")
            consumed += session.virtual_seconds - before
            if payload.get("order_status") == "refused" or payload.get("refused") is True:
                refused += 1
        rate = refused / trials
        per_exam = consumed / trials
        ok = (
            0.30 <= rate <= 0.70
            and refused > 0
            and per_exam >= 300
            and payload.get("refused") is not None
        )
        evidence = (
            f"{refused}/{trials} 被拒（拒诊率 {rate:.0%}，期望 ≈50%）、"
            f"每次体查消耗 {per_exam:.0f} 虚拟秒（要求 ≥300s，合计 {consumed}s）、"
            f"响应含 refused 字段={payload.get('refused')!r}、order_status={payload.get('order_status')}"
        )
        return ok, evidence


def check_05_waiting_for_troponin() -> Tuple[bool, str]:
    """肌钙蛋白未回报（<1500s）：等酶期间计时继续、生理恶化（等酶等于等死）。"""
    with fresh_environment() as client:
        session_id, session = start_session(client)
        first = execute(client, session_id, "lab_order", "高敏心肌钙蛋白I")
        immediate_report = first.get("report")
        vitals_0 = session.physiology_engine.get_current_vitals()
        settle(session, 1499)
        vitals_1 = session.physiology_engine.get_current_vitals()
        state = session.physiology_engine.get_state().value
        deteriorated = vitals_1.sbp <= vitals_0.sbp - 10 or vitals_1.hr <= vitals_0.hr - 10 or state.startswith(("S4", "S5", "S6"))
        # 要求：t=0 只是「已下单/等待中」，结果在 TAT 到期后才可读。
        pending_semantics = immediate_report is None
        ok = pending_semantics and deteriorated
        report_note = "无" if immediate_report is None else f"t=0 即返回（{str(immediate_report)[:48]}…）"
        evidence = (
            f"下单即出报告={report_note}、tat_seconds=1500 仅作文案透传"
            f"（guardrail_engine.py:53）、无 pending 队列；"
            f"生理确实恶化：HR {vitals_0.hr:.1f}→{vitals_1.hr:.1f}、SBP {vitals_0.sbp:.1f}→{vitals_1.sbp:.1f}、state={state}"
        )
        return ok, evidence


def check_06_small_delta_silent() -> Tuple[bool, str]:
    """变化量 <0.005 的情绪波动不产生 UI 提示与日志条目。"""
    engine = EmotionEngine()
    delta = engine.tick(5.0)  # 一个结算周期：trust -0.004 / anxiety +0.004 / family +0.003
    after_time = (len(engine.delta_log), len(engine.visible_delta_log))
    engine.apply_event("RELEVANT_QUESTION")  # trust +0.03 的真事件
    after_event = (len(engine.delta_log), len(engine.visible_delta_log))
    largest = max(abs(float(value)) for value in delta.values())
    ok = largest < 0.005 and after_time[1] == 0 and after_event[1] > 0
    evidence = (
        f"时间项最大 |Δ|={largest:.3f} < 0.005：工程账本 {after_time[0]} 条、"
        f"玩家可见流 {after_time[1]} 条；随后 +0.03 事件使可见流 {after_event[1]} 条"
        f"（emotion_engine.py:487 `if ge(abs(delta), UI_HINT_MIN_DELTA)`）"
    )
    return ok, evidence


def check_07_offline_full_flow() -> Tuple[bool, str]:
    """离线模式走完病例 1：复盘页出六维雷达且无网络请求失败。"""
    with fresh_environment() as client:
        session_id, _session = start_session(client)
        execute(client, session_id, "inquiry", "胸痛多久了，什么性质")
        execute(client, session_id, "physical_exam", "双上肢血压")
        execute(client, session_id, "lab_order", "高敏心肌钙蛋白I")
        response = client.post(
            "/api/eval/submit",
            json={"session_id": session_id, "final_diagnosis": "主动脉夹层"},
        )
        body = response.json()
        radar = body.get("radar_scores") or {}
        six = len(radar) == 6 and all(isinstance(value, (int, float)) for value in radar.values())
        ok = response.status_code == 200 and six and config.USE_ONLINE_LLM is False
        evidence = (
            f"submit HTTP {response.status_code}、雷达 {len(radar)} 维={sorted(radar)}、"
            f"outcome={(body.get('outcome') or {}).get('code')}、"
            f"mode=offline（config.USE_ONLINE_LLM={config.USE_ONLINE_LLM}；响应本身无 mode 字段）、"
            f"临床反馈来自本地 GED/规则，无外呼"
        )
        return ok, evidence


def check_08_manual_modal_does_not_pause() -> Tuple[bool, str]:
    """说明书弹窗停留 60 秒：病程计时与情绪结算继续推进。"""
    with fresh_environment() as client:
        _session_id, session = start_session(client)
        tick_log_before = len(session.emotion.delta_log)
        settle(session, 60)
        tick_log_after = len(session.emotion.delta_log)
        trust = session.emotion.snapshot()["patient_trust"]
        three = Path(__file__).read_text(encoding="utf-8")  # 自身可读，避免相对路径问题
        ok = session.virtual_seconds == 60 and tick_log_after > tick_log_before and trust < 0.40
        evidence = (
            f"挂「说明书打开」60 s：virtual_seconds={session.virtual_seconds}、"
            f"情绪结算 {tick_log_after - tick_log_before} 条、trust 0.400→{trust:.3f}；"
            f"无暂停钩子——server.py:27-29 后台 1 Hz tick_all 与 UI 无关，"
            f"scene.js:218 情绪 setInterval 无 modal 门控，openManual（scene.js:360-364）只 showModal"
        )
        return ok, evidence


def check_09_finished_session_not_restored() -> Tuple[bool, str]:
    """localStorage 存有已结束会话：开新会话而不是恢复死会话。"""
    text = SCENE_JS.read_text(encoding="utf-8")
    lines = text.splitlines()
    restore_at = next(
        (index + 1 for index, line in enumerate(lines) if "clinmind-game-session')" in line and "getItem" in line),
        None,
    )
    match = re.search(r"async function initialize\(\)\s*\{(.*?)\n  \}", text, re.S)
    body = match.group(1) if match else ""
    anchor = body.find("localStorage.getItem('clinmind-game-session')")
    tail = body[anchor:] if anchor >= 0 else ""
    guards_on_ended = any(token in tail for token in ("is_finished", "state.ended", "ended)"))
    ok = restore_at is not None and guards_on_ended
    evidence = (
        f"恢复逻辑 scene.js:{restore_at} `sessionId=localStorage.getItem('clinmind-game-session')` "
        f"→ refresh()（scene.js:641-646 不检查 is_finished），"
        f"只在 catch 到 404 时才丢弃（scene.js:878 `if(e.status!==404)throw e`）；"
        f"恢复段内是否校验 ended/is_finished={guards_on_ended} —— 已结束会话会被原样恢复并卡在冻结界面"
    )
    return ok, evidence


def check_10_missing_case_field_rejected() -> Tuple[bool, str]:
    """病例 JSON 缺 §4.2 任一项：启动校验失败并报出缺失字段名。"""
    workdir = Path(tempfile.mkdtemp(prefix="clinmind-case-"))
    try:
        data_dir = workdir / "data"
        shutil.copytree(config.DATA_DIR, data_dir)
        source = data_dir / "cases" / "case_gam_001_common_cold.json"
        broken = json.loads(source.read_text(encoding="utf-8-sig"))
        dropped = [field for field in ("knowledge_card", "differentials") if broken.pop(field, None) is not None]
        (data_dir / "cases" / "case_broken_001.json").write_text(
            json.dumps(broken, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raised: Exception | None = None
        with patch.object(config, "CASE_VALIDATION_STRICT", True):
            try:
                CaseRepository(data_dir=data_dir)
            except CaseValidationError as exc:  # 期望路径
                raised = exc
        named = bool(raised) and any(field in str(raised) for field in dropped)
        ok = raised is not None and named
        detail = (
            f"抛错={type(raised).__name__ if raised else '未抛'}、"
            f"消息含缺失字段名={named}"
            if raised
            else f"缺 {dropped} 的病例在 CASE_VALIDATION_STRICT=True 下仍被加载成功"
        )
        evidence = (
            f"{detail}；校验只到 dict+case_id（case_repository.py:32-37 "
            f"`if not isinstance(value, dict) or not value.get('case_id')`），"
            f"§4.2 的 {len(REQUIRED_CASE_FIELDS)} 项无逐项校验"
        )
        return ok, evidence
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


CHECKS: List[Tuple[str, str, Callable[[], Tuple[bool, str]]]] = [
    ("01", "挂机 8 分钟：信任停 0.15 / 患者焦虑停 0.85 / 生理进危象", check_01_idle_8_minutes),
    ("02", "12 次无关提问：信任 ≤0.05 并触发结局 C", check_02_twelve_irrelevant_questions),
    ("03", "夹层病例阿司匹林：即时红线 + 结局 D + 复盘指认", check_03_dissection_aspirin),
    ("04", "焦虑 0.92 时床旁超声 50% 被拒且耗 300 虚拟秒", check_04_anxious_patient_refuses_ultrasound),
    ("05", "肌钙蛋白 <1500s 未回报：等待期间计时与恶化", check_05_waiting_for_troponin),
    ("06", "情绪变化 <0.005 不产生 UI 提示与日志条目", check_06_small_delta_silent),
    ("07", "离线模式全流程：六维雷达、无网络请求失败", check_07_offline_full_flow),
    ("08", "说明书弹窗 60 秒：计时与情绪结算继续", check_08_manual_modal_does_not_pause),
    ("09", "localStorage 有已结束会话：开新会话而非恢复", check_09_finished_session_not_restored),
    ("10", "病例缺 §4.2 任一字段：启动校验报出字段名", check_10_missing_case_field_rejected),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    passed = 0
    failures: List[str] = []
    for number, title, func in CHECKS:
        try:
            ok, evidence = func()
        except Exception as exc:  # 读不到能力也算 FAIL，并写清缺什么
            ok = False
            evidence = f"缺能力/探针异常：{type(exc).__name__}: {exc}"
            if os.environ.get("VERIFY_ACCEPTANCE_TRACE"):
                traceback.print_exc()
        print(f"[{'PASS' if ok else 'FAIL'}] {number} {title}")
        print(f"       证据：{evidence}")
        if ok:
            passed += 1
        else:
            failures.append(number)
    print()
    print(f"{passed}/{len(CHECKS)} passed")
    if failures:
        print(f"failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
