"""ClinMind-AI Comprehensive Unit and Integration Test Suite.

Verifies:
1. Physiology Engine: 1Hz state transitions, targeted medication, lethal rupture countdown, tamponade.
2. Guardrail Engine: Dual-track isolation, authentic Grade-3A lab and imaging reports.
3. Multi-Agent Engine: Patient trust evolution, family anxiety interruption, nurse critical alarms.
4. GED Evaluator: NetworkX topological graph edit distance, OSCE 6-dim radar scores, cognitive biases.
5. FastAPI HTTP Endpoints: Case startup, action execution, OSCE evaluation submission.
"""

import os
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient

from app.models.entities import ActionType, ActionRecord, PatientStateEnum
from app.core.physiology_engine import PhysiologyEngine
from app.core.guardrail_engine import GuardrailEngine
from app.core.agent_engine import AgentEngine
from app.core.ged_evaluator import GEDEvaluator
from app.server import app


# ================= 1. Physiology Engine Tests =================

def test_physiology_initial_and_natural_progression():
    engine = PhysiologyEngine()
    assert engine.get_state() == PatientStateEnum.S0_STABLE
    vitals = engine.get_vitals()
    assert 100 <= vitals.hr <= 115
    assert 185 <= vitals.sbp <= 205
    assert 110 <= vitals.dbp <= 125

    # Advance time without treatment past 300 seconds -> S1_HYPERTENSION_CRISIS
    for _ in range(310):
        engine.advance_time(1.0)
    assert engine.get_state() == PatientStateEnum.S1_HYPERTENSION_CRISIS
    assert engine.get_vitals().sbp >= 200


def test_physiology_targeted_anti_hypertensive():
    engine = PhysiologyEngine()
    # Apply targeted dissection control
    engine.apply_medication("艾司洛尔注射液", 0.5, "mg/kg")
    engine.apply_medication("硝普钠注射液", 0.5, "ug/kg/min")
    assert engine.get_state() == PatientStateEnum.S2_PHARMACO_CONTROLLED

    # Advance time to allow pharmacodynamics to smooth vitals down
    for _ in range(80):
        engine.advance_time(1.0)

    v = engine.get_vitals()
    assert v.hr < 85
    assert v.sbp < 145


def test_physiology_lethal_medication_rupture_countdown():
    engine = PhysiologyEngine()
    res = engine.apply_medication("阿司匹林肠溶片 300mg", 300, "mg")
    assert res["is_lethal"] is True
    assert engine.get_rupture_countdown() is not None
    assert engine.get_state() == PatientStateEnum.S3_PERICARDIAL_EFFUSION

    # Advance time through the countdown (120s)
    for _ in range(125):
        engine.advance_time(1.0)

    # Patient collapses into Tamponade / Rupture
    state = engine.get_state()
    assert state in [PatientStateEnum.S4_CARDIAC_TAMPONADE, PatientStateEnum.S5_AORTIC_RUPTURE, PatientStateEnum.S6_CARDIAC_ARREST]
    assert engine.get_vitals().sbp < 60


# ================= 2. Guardrail Engine Tests =================

def test_guardrail_physical_exam_routing():
    guard = GuardrailEngine()
    matched, res_bp, msg = guard.route_physical_exam("测量患者双侧肢体血压")
    assert matched is True
    assert res_bp is not None
    assert "压差 33 mmHg" in str(res_bp) or "33" in str(res_bp)

    matched, res_murmur, msg = guard.route_physical_exam("听诊主动脉瓣听诊区及心音杂音")
    assert matched is True
    assert res_murmur is not None
    assert "舒张期" in str(res_murmur) or "反流" in str(res_murmur)


def test_guardrail_lab_and_imaging_routing():
    guard = GuardrailEngine()
    # 18-lead ECG
    matched, ecg, msg = guard.route_lab_or_imaging("急查18导联心电图")
    assert matched is True
    assert ecg is not None
    assert "STEMI" in str(ecg)

    # D-Dimer
    matched, ddimer, msg = guard.route_lab_or_imaging("急查D-二聚体")
    assert matched is True
    assert ddimer is not None
    assert ddimer.get("critical_alert") is True or "8.45" in str(ddimer)

    # CTA (Gold Standard)
    matched, cta, msg = guard.route_lab_or_imaging("急诊胸腹主动脉全长增强CTA")
    assert matched is True
    assert cta is not None
    assert "Stanford A型" in str(cta)


def test_guardrail_routes_every_authored_lab_panel():
    """病例 JSON 里写的每个 lab_panels 都必须能被开出来。

    别名表曾只硬编码 8 个标准项，病例自带的 CRP、胸片、病原核酸、CT 等
    19 项一点就回 NOT_CONFIGURED —— 数据在、引擎读不到。这里按病例逐个
    断言，新增病例项忘了接线会立刻红。
    """
    import json
    from pathlib import Path

    data_dir = Path(__file__).resolve().parent.parent / "app" / "data"
    case_files = sorted((data_dir / "cases").glob("*.json")) + [data_dir / "case_chest_pain.json"]
    checked = 0
    for case_file in case_files:
        payload = json.loads(case_file.read_text(encoding="utf-8"))
        panels = payload.get("lab_panels") or []
        if not panels:
            continue
        guard = GuardrailEngine(case_file)
        for panel in panels:
            matched, report, _ = guard.route_lab_or_imaging(panel["name"])
            assert matched is True, f"{case_file.name} 的【{panel['name']}】点不出来"
            assert report is not None
            assert report.get("status") != "NOT_CONFIGURED"
            checked += 1
    assert checked >= 30, f"只检查到 {checked} 个检查项，病例库可能没读到"


# ================= 3. Multi-Agent Engine Tests =================

def test_agent_patient_trust_evolution():
    agent = AgentEngine()
    init_trust = agent.state.patient_trust

    # Empathy message boosts trust
    dialogues = agent.handle_inquiry("大爷别害怕，我们全力救治，马上给您止痛和控制血压！")
    assert agent.state.patient_trust > init_trust
    assert any(d.role == "patient" for d in dialogues)


def test_agent_nurse_order_and_critical_alarms():
    agent = AgentEngine()
    order_res = agent.handle_nurse_order(ActionType.LAB_ORDER, "急查18导联心电图")
    assert "医嘱已下达" in order_res["status"]

    # Contraindicated drug warning from nurse
    contra_res = agent.handle_nurse_order(ActionType.MEDICATION, "给患者嚼服阿司匹林300mg")
    assert contra_res.get("warning") is not None


# ================= 4. GED Evaluator Tests =================

def test_ged_evaluator_expert_pathway():
    evaluator = GEDEvaluator()
    # Build expert actions
    actions = [
        ActionRecord(
            action_id="act_1",
            step_id="N1",
            action_type=ActionType.INQUIRY,
            content="问诊疼痛性质、部位与放射痛",
            timestamp=time.time(),
            virtual_time_seconds=10
        ),
        ActionRecord(
            action_id="act_1b",
            step_id="N2",
            action_type=ActionType.INQUIRY,
            content="问诊发病诱因与背部放射痛",
            timestamp=time.time(),
            virtual_time_seconds=15
        ),
        ActionRecord(
            action_id="act_1c",
            step_id="N3",
            action_type=ActionType.INQUIRY,
            content="问诊高血压既往史与停药情况",
            timestamp=time.time(),
            virtual_time_seconds=18
        ),
        ActionRecord(
            action_id="act_2",
            step_id="N4",
            action_type=ActionType.RESCUE_DISPOSITION,
            content="高流量鼻导管吸氧并建立双静脉通路",
            timestamp=time.time(),
            virtual_time_seconds=20
        ),
        ActionRecord(
            action_id="act_3",
            step_id="N5",
            action_type=ActionType.PHYSICAL_EXAM,
            content="测量双侧上肢血压",
            timestamp=time.time(),
            virtual_time_seconds=30
        ),
        ActionRecord(
            action_id="act_4",
            step_id="N6",
            action_type=ActionType.PHYSICAL_EXAM,
            content="心脏听诊主动脉瓣舒张期杂音",
            timestamp=time.time(),
            virtual_time_seconds=40
        ),
        ActionRecord(
            action_id="act_5",
            step_id="N8",
            action_type=ActionType.LAB_ORDER,
            content="急查18导联心电图排除STEMI",
            timestamp=time.time(),
            virtual_time_seconds=50
        ),
        ActionRecord(
            action_id="act_5b",
            step_id="N9",
            action_type=ActionType.LAB_ORDER,
            content="急查高敏肌钙蛋白I",
            timestamp=time.time(),
            virtual_time_seconds=55
        ),
        ActionRecord(
            action_id="act_6",
            step_id="N10",
            action_type=ActionType.LAB_ORDER,
            content="急查D-二聚体",
            timestamp=time.time(),
            virtual_time_seconds=60
        ),
        ActionRecord(
            action_id="act_6b",
            step_id="N11",
            action_type=ActionType.INQUIRY,
            content="综合心电图与心肌酶排除急性心梗",
            timestamp=time.time(),
            virtual_time_seconds=65
        ),
        ActionRecord(
            action_id="act_7",
            step_id="N13",
            action_type=ActionType.MEDICATION,
            content="静脉微泵艾司洛尔降心率",
            timestamp=time.time(),
            virtual_time_seconds=80
        ),
        ActionRecord(
            action_id="act_8",
            step_id="N14",
            action_type=ActionType.MEDICATION,
            content="静脉微泵硝普钠控制血压",
            timestamp=time.time(),
            virtual_time_seconds=90
        ),
        ActionRecord(
            action_id="act_9",
            step_id="N15",
            action_type=ActionType.LAB_ORDER,
            content="急查胸腹主动脉CTA金标准确诊",
            timestamp=time.time(),
            virtual_time_seconds=120
        ),
        ActionRecord(
            action_id="act_10",
            step_id="N17",
            action_type=ActionType.RESCUE_DISPOSITION,
            content="紧急呼叫心胸外科急会诊",
            timestamp=time.time(),
            virtual_time_seconds=150
        ),
        ActionRecord(
            action_id="act_11",
            step_id="N18",
            action_type=ActionType.RESCUE_DISPOSITION,
            content="向家属签署急诊开胸手术知情同意书",
            timestamp=time.time(),
            virtual_time_seconds=180
        )
    ]

    report = evaluator.evaluate(actions, final_diagnosis="Stanford A型主动脉夹层")
    assert report.total_score >= 80.0
    assert report.topological_similarity >= 0.70
    assert len(report.fatal_errors) == 0
    assert report.radar_scores["safety_awareness"] == 100.0


def test_ged_evaluator_fatal_contraindication():
    evaluator = GEDEvaluator()
    # Student gives aspirin and misses physical exam
    actions = [
        ActionRecord(
            action_id="act_1",
            step_id="fatal",
            action_type=ActionType.MEDICATION,
            content="阿司匹林 300mg 嚼服抗血小板",
            timestamp=time.time(),
            virtual_time_seconds=10,
            is_contraindicated=True
        )
    ]
    report = evaluator.evaluate(actions, final_diagnosis="急性下壁心肌梗死 (误诊)")
    assert len(report.fatal_errors) > 0
    assert report.total_score < 40.0
    assert report.radar_scores["safety_awareness"] == 0.0


# ================= 5. FastAPI Endpoints Integration Tests =================

def test_api_full_flow():
    client = TestClient(app)

    # 1. Start session
    start_res = client.post("/api/case/start", json={
        "case_id": "case_chest_pain_001",
        "student_name": "测试医师"
    })
    assert start_res.status_code == 200
    sdata = start_res.json()
    session_id = sdata["session_id"]
    assert session_id is not None

    # 2. Case list and status
    list_res = client.get("/api/case/list")
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    status_res = client.get(f"/api/case/session/status?session_id={session_id}")
    assert status_res.status_code == 200
    assert status_res.json()["patient_state"] == "S0_STABLE"

    # 3. Execute actions
    act1 = client.post("/api/action/execute", json={
        "session_id": session_id,
        "action_type": "physical_exam",
        "content": "测量双侧上肢血压"
    })
    assert act1.status_code == 200
    assert act1.json()["report"] is not None

    act2 = client.post("/api/action/execute", json={
        "session_id": session_id,
        "action_type": "medication",
        "content": "艾司洛尔 0.5mg/kg 负荷静推"
    })
    assert act2.status_code == 200

    # 4. Submit evaluation
    eval_res = client.post("/api/eval/submit", json={
        "session_id": session_id,
        "final_diagnosis": "Stanford A型主动脉夹层",
        "differential_diagnoses": ["心肌梗死", "肺栓塞"],
        "clinical_rationale": "典型撕裂样胸痛，双上肢压差>30mmHg"
    })
    assert eval_res.status_code == 200
    eval_data = eval_res.json()
    assert "total_score" in eval_data
    assert "radar_scores" in eval_data
    assert "comparison_dag" in eval_data


if __name__ == "__main__":
    import sys
    test_functions = [
        obj for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    print(f"🚀 Running {len(test_functions)} ClinMind-AI tests...\n" + "=" * 50)
    passed = 0
    failed = 0
    for test in test_functions:
        try:
            print(f"Testing {test.__name__} ... ", end="", flush=True)
            test()
            print("✅ PASSED")
            passed += 1
        except Exception as e:
            print(f"❌ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("=" * 50)
    print(f"Test Summary: {passed} Passed, {failed} Failed out of {len(test_functions)}.")
    if failed > 0:
        sys.exit(1)
    else:
        print("🎉 ALL TESTS PASSED SUCCESSFULLY!")

