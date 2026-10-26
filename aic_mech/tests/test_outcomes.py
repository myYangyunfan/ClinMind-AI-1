"""P3 结局通道测试：病例 outcomes 条件解析、处方红线路由、结案报告结局。

覆盖三件事：
  * 6 个病例 JSON 里全部 condition 都能被手写解析器解析（不依赖 eval）；
  * 同一套 condition 在不同状态下分别命中 A / B / 都不命中；
  * 红线用药后 outcome 不再是 A（LETHAL 走致死倒计时，HARMFUL 只可逆恶化）；
  * /api/eval/submit 与 /api/eval/report 都带回 outcome 与 knowledge_card。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 与 run_all_tests.py 保持一致：离线规则引擎，结果可复现。
os.environ.setdefault("USE_ONLINE_LLM", "false")

from fastapi.testclient import TestClient

import config
from app.core.case_repository import get_case_repository
from app.core.ged_evaluator import (
    ConditionSyntaxError,
    OUTCOME_PRIORITY,
    UnknownVariableError,
    build_outcome_context,
    evaluate_condition,
    parse_condition,
    resolve_outcome,
)
from app.core.session_manager import SessionStore
from app.server import app

CHEST_PAIN_CASE_ID = "case_chest_pain_001"
ASTHMA_CASE_ID = "case_gam_003_asthma"
COMMON_COLD_CASE_ID = "case_gam_001_common_cold"
APPENDICITIS_CASE_ID = "case_gam_005_appendicitis"


def outcome_context(**overrides):
    """构造一份完整的 condition 上下文，再按需覆盖字段。"""
    context = {
        "HR": 55.0,
        "SBP": 110.0,
        "DBP": 70.0,
        "RR": 18.0,
        "SpO2": 98.0,
        "pain_vas": 3.0,
        "trust": 0.50,
        "patient_anxiety": 0.50,
        "family_anxiety": 0.65,
        "negative_streak": 0,
        "has_red_line": False,
        "treated": False,
        "treated_count": 0,
        "nodes": ["N13", "N14"],
        "elapsed": {"trust_low": 0.0},
        "outcomes": {"physiology": "STABLE"},
    }
    context.update(overrides)
    return context


class OutcomeConditionParserTests(unittest.TestCase):
    """条件解析器：全病例可解析、未知变量报错、不做 eval。"""

    def test_all_case_conditions_parse(self):
        repository = get_case_repository()
        self.assertGreaterEqual(len(repository.ids()), 6, "病例目录应包含 6 个病例")
        total = 0
        for case_id in repository.ids():
            case_data = repository.get(case_id)
            outcomes = case_data.get("outcomes") or {}
            self.assertTrue(outcomes, f"{case_id} 缺少 outcomes")
            for code, item in outcomes.items():
                condition = item.get("condition")
                self.assertIsInstance(condition, str, f"{case_id}/{code} 缺少 condition")
                expression = parse_condition(condition)  # 解析失败会直接抛错
                self.assertEqual(expression.text, condition)
                total += 1
        self.assertGreaterEqual(total, 24, "6 个病例各 4 个结局，至少 24 条条件")

    def test_condition_semantics(self):
        condition = "HR < 60 && 100 <= SBP <= 120 && has('N13') && has('N14') && !has_red_line"
        self.assertTrue(evaluate_condition(condition, outcome_context()))
        self.assertFalse(evaluate_condition(condition, outcome_context(HR=70.0)))
        self.assertFalse(evaluate_condition(condition, outcome_context(SBP=130.0)))
        self.assertFalse(evaluate_condition(condition, outcome_context(nodes=["N13"])))
        self.assertFalse(evaluate_condition(condition, outcome_context(has_red_line=True)))

    def test_unknown_variable_raises(self):
        with self.assertRaises(UnknownVariableError) as caught:
            evaluate_condition("HR < 60 && mystery_score > 1", outcome_context())
        message = str(caught.exception)
        # 报错必须告诉调用方有哪些变量可用，否则病例里写错一个字母就得翻源码。
        self.assertIn("mystery_score", message)
        self.assertIn("可用变量", message)
        for name in ("treated", "treated_count", "has_red_line", "trust"):
            self.assertIn(name, message)

    def test_syntax_error_raises(self):
        with self.assertRaises(ConditionSyntaxError):
            parse_condition("HR < 60 &&")
        with self.assertRaises(ConditionSyntaxError):
            parse_condition("(HR < 60")


class OutcomeResolutionTests(unittest.TestCase):
    """同一病例、三种状态：命中 A、命中 B、一个都不命中。"""

    def setUp(self):
        self.case_data = get_case_repository().require(CHEST_PAIN_CASE_ID)

    def test_hit_a(self):
        resolved = resolve_outcome(self.case_data, outcome_context())
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["code"], "A")
        self.assertEqual(resolved["label"], "治愈/稳定")
        self.assertTrue(resolved.get("title"))
        self.assertTrue(resolved.get("narration"))

    def test_hit_b(self):
        resolved = resolve_outcome(self.case_data, outcome_context(HR=96.0, SBP=160.0, nodes=[]))
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["code"], "B")

    def test_hit_none(self):
        # HR<60 且 SBP 在目标区间但没用药（缺 N13/N14）→ A 不成立；
        # HR<60 且 SBP<=120 → B 的 (HR>=60 || SBP>120) 也不成立；
        # 信任度正常 → C 不成立；没有红线、未停搏 → D 不成立。
        context = outcome_context(SBP=118.0, nodes=[], trust=0.50)
        self.assertIsNone(resolve_outcome(self.case_data, context))

    def test_red_line_forces_d_and_leave_forces_c(self):
        red = resolve_outcome(self.case_data, outcome_context(has_red_line=True))
        self.assertEqual(red["code"], "D")
        arrest = resolve_outcome(
            self.case_data, outcome_context(outcomes={"physiology": "ARREST"})
        )
        self.assertEqual(arrest["code"], "D")
        left = resolve_outcome(
            self.case_data,
            outcome_context(trust=0.05, elapsed={"trust_low": 120.0}),
        )
        self.assertEqual(left["code"], "C")

    def test_priority_order_is_death_first(self):
        self.assertEqual(OUTCOME_PRIORITY[0], "D")
        # 既有致死停搏又有红线时，仍然是 D（最坏结局优先）
        resolved = resolve_outcome(
            self.case_data,
            outcome_context(has_red_line=True, outcomes={"physiology": "ARREST"}),
        )
        self.assertEqual(resolved["code"], "D")


class OutcomeApiTests(unittest.TestCase):
    """走真实 HTTP 接口：红线用药 + 结案报告。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.client = TestClient(app)

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    # ---------------- helpers ----------------
    def start(self, case_id):
        response = self.client.post("/api/case/start", json={"case_id": case_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session_id"]

    def action(self, session_id, content, action_type="medication", **params):
        return self.client.post(
            "/api/action/execute",
            json={
                "session_id": session_id,
                "action_type": action_type,
                "content": content,
                "params": params,
            },
        )

    def session(self, session_id):
        return SessionStore.get_instance().get_session(session_id)

    def outcome_of(self, session_id):
        from app.core.ged_evaluator import resolve_session_outcome

        resolved = resolve_session_outcome(self.session(session_id))
        return (resolved or {}).get("code")

    # ---------------- 红线用药 ----------------
    def test_lethal_red_line_drug_never_scores_a(self):
        session_id = self.start(CHEST_PAIN_CASE_ID)
        response = self.action(session_id, "阿司匹林 300mg", dose=300, unit="mg")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["is_contraindicated"])
        self.assertTrue(payload["warning"], "LETHAL 红线必须回护栏反馈")
        self.assertIn("红线", payload["warning"])
        self.assertNotEqual(self.outcome_of(session_id), "A")
        self.assertEqual(self.outcome_of(session_id), "D")

    def test_lethal_red_line_in_catalogue_case_reaches_death(self):
        # asthma 的阿司匹林 AERD 是 LETHAL：引擎走 30s 速崩倒计时。
        session_id = self.start(ASTHMA_CASE_ID)
        response = self.action(session_id, "阿司匹林", dose=100, unit="mg")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_contraindicated"])
        session = self.session(session_id)
        self.assertNotEqual(self.outcome_of(session_id), "A")
        for _ in range(35):
            session.tick(1)
        self.assertEqual(session.physiology_engine.get_current_vitals().hr, 0.0)
        self.assertEqual(session.terminal_reason, "death")
        self.assertEqual(self.outcome_of(session_id), "D")

    def test_harmful_red_line_is_recoverable_and_visible(self):
        # common cold 的阿司匹林是 HARMFUL：可逆恶化，不判死，但必须明确报警。
        session_id = self.start(COMMON_COLD_CASE_ID)
        session = self.session(session_id)
        before = session.physiology_engine.get_current_vitals()
        response = self.action(session_id, "阿司匹林", dose=100, unit="mg")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["is_contraindicated"], "HARMFUL 不是一票否决")
        self.assertTrue(payload["warning"], "HARMFUL 分支不允许静默")
        after = session.physiology_engine.get_current_vitals()
        self.assertGreater(after.hr, before.hr, "可逆恶化应体现在生命体征上")
        self.assertIsNone(session.terminal_reason)
        self.assertNotEqual(self.outcome_of(session_id), "A")
        self.assertTrue(session.agent_engine.state.active_critical_alarms)

    def test_safe_medication_keeps_a_reachable(self):
        session_id = self.start(APPENDICITIS_CASE_ID)
        response = self.action(session_id, "头孢曲松", dose=2, unit="g")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_contraindicated"])
        self.assertEqual(self.outcome_of(session_id), "A")

    def test_outcome_survives_reload(self):
        # has_red_line 是靠 ActionRecord 里落库的证据判定的，重载后必须一致。
        session_id = self.start(COMMON_COLD_CASE_ID)
        self.action(session_id, "阿司匹林", dose=100, unit="mg")
        self.assertEqual(self.outcome_of(session_id), "D")

        SessionStore._instance = None
        reloaded = SessionStore.get_instance().get_session(session_id)
        self.assertIsNotNone(reloaded, "会话应能从 sqlite 快照恢复")
        from app.core.ged_evaluator import resolve_session_outcome

        self.assertEqual((resolve_session_outcome(reloaded) or {}).get("code"), "D")

    def test_prerequisite_rule_is_not_a_red_line_after_beta_blocker(self):
        # 胸痛病例的硝普钠单用是 HARMFUL，但先用艾司洛尔控住心率后就是 TARGET。
        session_id = self.start(CHEST_PAIN_CASE_ID)
        self.action(session_id, "艾司洛尔", dose=50, unit="mg")
        response = self.action(session_id, "硝普钠", dose=0.5, unit="ug")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_contraindicated"])
        self.assertIsNone(response.json()["warning"], "联合用药不应再报红线")
        self.assertNotEqual(self.outcome_of(session_id), "D")

    # ---------------- treated / treated_count（动作级守门条件） ----------------
    def test_treated_flags_follow_real_actions(self):
        session_id = self.start(APPENDICITIS_CASE_ID)
        context = build_outcome_context(self.session(session_id))
        self.assertFalse(context["treated"], "零动作不能算处置过")
        self.assertEqual(context["treated_count"], 0)

        self.assertEqual(
            self.action(session_id, "头孢曲松", dose=2, unit="g").status_code, 200
        )
        context = build_outcome_context(self.session(session_id))
        self.assertTrue(context["treated"])
        self.assertEqual(context["treated_count"], 1)

    def test_lethal_red_line_is_not_treated(self):
        # 红线药不是「有效处置」：它已经由 has_red_line 走 D 结局。
        session_id = self.start(ASTHMA_CASE_ID)
        self.action(session_id, "阿司匹林", dose=100, unit="mg")
        context = build_outcome_context(self.session(session_id))
        self.assertFalse(context["treated"])
        self.assertEqual(context["treated_count"], 0)
        self.assertTrue(context["has_red_line"])
        self.assertEqual(self.outcome_of(session_id), "D")

    def test_harmful_red_line_is_not_treated(self):
        session_id = self.start(COMMON_COLD_CASE_ID)
        self.action(session_id, "阿司匹林", dose=100, unit="mg")
        context = build_outcome_context(self.session(session_id))
        self.assertFalse(context["treated"], "可逆红线同样不算有效处置")
        self.assertTrue(context["has_red_line"])

    def test_treated_survives_idle_trust_decay_and_still_scores_a(self):
        """处方后挂机 400 秒，trust 已从 0.44 衰减到地板 0.15，结局仍是 A。

        这正是本次要根治的窗口问题：病例 A 曾用 ``trust > 0.40`` 当「处置过」的
        守门条件，而信任每秒衰减约 0.0008，学员用药后拖几分钟结案就会掉回 B。
        现在 6 个病例的 A 都改用动作级 ``treated``（由 ``build_outcome_context``
        提供，与时间衰减无关），窗口才真正关上。这里用真实会话跑，不构造假
        session，条件文本也直接取病例 JSON 里的原样字符串。
        """
        session_id = self.start(APPENDICITIS_CASE_ID)
        self.assertEqual(
            self.action(session_id, "头孢曲松", dose=2, unit="g").status_code, 200
        )
        session = self.session(session_id)
        while session.virtual_seconds < 400:
            session.tick(1)

        context = build_outcome_context(session)
        self.assertLess(context["trust"], 0.40, "信任应已随时间衰减")
        self.assertTrue(context["treated"], "处置事实不随时间消失")
        self.assertEqual(context["treated_count"], 1)

        condition = session.case_data["outcomes"]["A"]["condition"]
        self.assertIn("treated", condition, "病例 A 的守门条件应是动作级 treated")
        self.assertTrue(
            evaluate_condition(condition, context),
            f"用药后挂机 400 秒仍应满足 A 的处置守门条件：{condition}",
        )
        # 同一份真实病例、真实上下文跑完整判定：结果必须是 A 而不是 B。
        resolved = resolve_outcome(session.case_data, context)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["code"], "A")

    # ---------------- 结案报告 ----------------
    def test_evaluation_report_carries_outcome_and_knowledge_card(self):
        session_id = self.start(APPENDICITIS_CASE_ID)
        # 病例 A 结局要求「真的处置过」（outcomes.A 用动作级 treated 守门），
        # 零操作直接结案只会拿到 B。这里先给一剂安全用药，再验证报告字段齐全——
        # 本用例测的是「结案报告带回 outcome / knowledge_card」，不是「白送 A」。
        self.assertEqual(
            self.action(session_id, "头孢曲松", dose=2, unit="g").status_code, 200
        )
        response = self.client.post(
            "/api/eval/submit",
            json={"session_id": session_id, "final_diagnosis": "急性阑尾炎"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("total_score", payload, "旧字段一个都不能少")
        self.assertIn("comparison_dag", payload)
        outcome = payload["outcome"]
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["code"], "A")
        self.assertTrue(outcome["title"])
        self.assertTrue(outcome["description"])
        card = payload["knowledge_card"]
        self.assertIsInstance(card, dict)
        self.assertTrue(card.get("title"))
        self.assertTrue(card.get("items"))

        # /report 走同一份组装逻辑
        fetched = self.client.get(f"/api/eval/report/{session_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["outcome"]["code"], "A")
        self.assertEqual(
            fetched.json()["knowledge_card"]["title"], card["title"]
        )

    def test_evaluation_report_after_red_line_is_not_a(self):
        session_id = self.start(CHEST_PAIN_CASE_ID)
        self.action(session_id, "阿司匹林 300mg", dose=300, unit="mg")
        response = self.client.post(
            "/api/eval/submit",
            json={"session_id": session_id, "final_diagnosis": "Stanford A型主动脉夹层"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        outcome = response.json()["outcome"]
        self.assertIsNotNone(outcome)
        self.assertNotEqual(outcome["code"], "A")
        self.assertEqual(outcome["code"], "D")


if __name__ == "__main__":
    unittest.main(verbosity=2)
