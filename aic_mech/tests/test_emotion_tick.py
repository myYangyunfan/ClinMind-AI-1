# -*- coding: utf-8 -*-
"""Unit tests for the P0 emotion engine and its session integration.

Covers spec/05-P0接口契约.md §2-§3 and spec/02-数值与状态机规格.md §2-§4:
time-term bounds, the 5-second settlement accumulator, the three judgement
lines, multiplier snapshots, the 0.005 UI suppression rule and persistence.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import config
from app.core.emotion_engine import (
    EmotionEngine,
    EmotionState,
    FLOAT_EPS,
    SETTLE_INTERVAL,
    TRUST_TIME_FLOOR,
    ANXIETY_TIME_CEILING,
    UI_HINT_MIN_DELTA,
)
from app.models.entities import ActionType, AgentState, PatientStateEnum
from app.core.session_manager import SessionStore
from app.core.agent_engine import AgentEngine
from app.core.physiology_engine import PhysiologyEngine

CASE_ID = "case_chest_pain_001"
DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "data"


def load_case(relative: str) -> dict:
    """Load a case JSON from ``app/data`` (UTF-8)."""
    with open(DATA_DIR / relative, encoding="utf-8") as handle:
        return json.load(handle)


class EmotionTimeTermTests(unittest.TestCase):
    """spec/02 §2.3 A-C: accumulator, bounds, multipliers."""

    def test_accumulator_settles_on_five_second_boundary(self):
        engine = EmotionEngine()
        for _ in range(4):
            delta = engine.tick(1.0)
            self.assertEqual(delta["patient_trust"], 0.0)
            self.assertEqual(delta["patient_anxiety"], 0.0)
        self.assertAlmostEqual(engine.accumulator, 4.0, places=9)
        self.assertEqual(engine.settle_count, 0)

        delta = engine.tick(1.0)
        self.assertAlmostEqual(engine.accumulator, 0.0, places=9)
        self.assertEqual(engine.settle_count, 1)
        self.assertAlmostEqual(delta["patient_trust"], -0.004, places=9)
        self.assertAlmostEqual(delta["patient_anxiety"], 0.004, places=9)
        self.assertAlmostEqual(delta["family_anxiety"], 0.003, places=9)

    def test_non_positive_dt_is_ignored(self):
        engine = EmotionEngine()
        self.assertEqual(sum(engine.tick(0.0).values()), 0.0)
        self.assertEqual(sum(engine.tick(-3.0).values()), 0.0)
        self.assertEqual(engine.accumulator, 0.0)
        self.assertEqual(engine.virtual_seconds, 0.0)

    def test_trust_time_floor_is_exact(self):
        engine = EmotionEngine()
        for _ in range(400):
            engine.tick(1.0)
        self.assertEqual(engine.state.patient_trust, TRUST_TIME_FLOOR)
        self.assertEqual(engine.state.patient_trust, 0.15)

        # further idle time must not push below the floor nor bounce back up
        for _ in range(20):
            engine.tick(1.0)
        self.assertEqual(engine.state.patient_trust, 0.15)
        self.assertIsNone(engine.leave_reason)

    def test_anxiety_time_ceiling_is_exact(self):
        engine = EmotionEngine()
        for _ in range(400):
            engine.tick(1.0)
        self.assertEqual(engine.state.patient_anxiety, ANXIETY_TIME_CEILING)
        self.assertEqual(engine.state.patient_anxiety, 0.85)
        for _ in range(20):
            engine.tick(1.0)
        self.assertEqual(engine.state.patient_anxiety, 0.85)

    def test_idle_eight_minutes_stays_in_room(self):
        engine = EmotionEngine()
        for _ in range(480):
            engine.tick(1.0)
        self.assertEqual(engine.settle_count, 96)
        self.assertIsNone(engine.leave_reason)
        self.assertFalse(engine.refused)
        self.assertFalse(engine.is_terminal)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["patient_trust"], 0.15)
        self.assertEqual(snapshot["patient_anxiety"], 0.85)
        self.assertFalse(snapshot["refuse"])
        self.assertIsNone(snapshot["leave_reason"])
        # floor trust never drops to the sustained (0.10) departure band
        self.assertEqual(engine.departure_watch_s, 0.0)

    def test_multipliers_snapshot_from_previous_values(self):
        engine = EmotionEngine(
            EmotionState(patient_trust=0.70, patient_anxiety=0.70, family_anxiety=0.65))
        engine.tick(SETTLE_INTERVAL)
        # anxiety >= 0.70 -> trust decays x1.5 ; trust >= 0.70 -> anxiety x0.5
        self.assertAlmostEqual(engine.state.patient_trust, 0.70 - 0.006, places=9)
        self.assertAlmostEqual(engine.state.patient_anxiety, 0.70 + 0.002, places=9)
        self.assertAlmostEqual(engine.state.family_anxiety, 0.653, places=9)

    def test_low_trust_multiplier_accelerates_anxiety(self):
        engine = EmotionEngine(
            EmotionState(patient_trust=0.20, patient_anxiety=0.50, family_anxiety=0.65))
        engine.tick(SETTLE_INTERVAL)
        self.assertAlmostEqual(engine.state.patient_anxiety, 0.50 + 0.004 * 1.3, places=9)

    def test_critical_anxiety_hold_doubles_trust_decay(self):
        engine = EmotionEngine(
            EmotionState(patient_trust=0.90, patient_anxiety=0.96, family_anxiety=0.85))
        engine.tick(SETTLE_INTERVAL * 7)  # 7 settlements, hold hits 30 s on the 7th
        self.assertAlmostEqual(engine.critical_anxiety_hold_s, 35.0, places=9)
        # first 6 settlements at x1.5, the 7th at x3.0 (1.5 * critical 2.0)
        expected = 0.90 - 0.006 * 6 - 0.012
        self.assertAlmostEqual(engine.state.patient_trust, expected, places=9)
        self.assertEqual(engine.state.patient_anxiety, 0.96)

    def test_trends_follow_net_direction(self):
        engine = EmotionEngine()
        engine.tick(10.0)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["trust_trend"], -1)
        self.assertEqual(snapshot["anxiety_trend"], 1)
        self.assertEqual(len(snapshot), 8)


class EmotionEventTests(unittest.TestCase):
    """spec/05 §2 event table, cooldowns and the three judgement lines."""

    def test_twelve_irrelevant_questions_trigger_immediate_leave(self):
        engine = EmotionEngine()
        for _ in range(11):
            engine.apply_event("IRRELEVANT_QUESTION")
        self.assertIsNone(engine.leave_reason)
        self.assertAlmostEqual(engine.state.patient_trust, 0.07, places=9)

        engine.apply_event("IRRELEVANT_QUESTION")
        self.assertLessEqual(engine.state.patient_trust, config.EMOTION_LEAVE_IMMEDIATE)
        self.assertGreaterEqual(engine.neg_interaction_streak, 3)
        self.assertEqual(engine.leave_reason, "leave_immediate")
        self.assertTrue(engine.is_terminal)
        self.assertEqual(engine.snapshot()["leave_reason"], "leave_immediate")

    def test_sustained_low_trust_triggers_sustained_leave(self):
        engine = EmotionEngine()
        for _ in range(10):
            engine.apply_event("IRRELEVANT_QUESTION")
        self.assertAlmostEqual(engine.state.patient_trust, 0.10, places=9)
        self.assertIsNone(engine.leave_reason)

        for _ in range(11):
            engine.tick(SETTLE_INTERVAL)
        self.assertIsNone(engine.leave_reason)  # 55 s < 60 s
        engine.tick(SETTLE_INTERVAL)
        self.assertEqual(engine.departure_watch_s, 60.0)
        self.assertEqual(engine.leave_reason, "leave_sustained")

    def test_comfort_cooldown_halves_delta(self):
        engine = EmotionEngine()
        first = engine.apply_event("COMFORT")
        self.assertAlmostEqual(first["patient_trust"], 0.05, places=9)
        second = engine.apply_event("COMFORT")
        self.assertAlmostEqual(second["patient_trust"], 0.025, places=9)
        self.assertAlmostEqual(second["patient_anxiety"], -0.04, places=9)

        # after the 60 s cooldown the full delta comes back
        engine.tick(SETTLE_INTERVAL * 12)
        third = engine.apply_event("COMFORT")
        self.assertAlmostEqual(third["patient_trust"], 0.05, places=9)

    def test_correct_treatment_capped_at_three(self):
        engine = EmotionEngine()
        for _ in range(3):
            delta = engine.apply_event("CORRECT_TREATMENT")
            self.assertAlmostEqual(delta["patient_trust"], 0.04, places=9)
        blocked = engine.apply_event("CORRECT_TREATMENT")
        self.assertEqual(sum(blocked.values()), 0.0)

    def test_red_line_medication_is_a_noop(self):
        engine = EmotionEngine()
        delta = engine.apply_event("RED_LINE_MED")
        self.assertEqual(sum(delta.values()), 0.0)
        self.assertEqual(engine.state.patient_trust, config.EMOTION_TRUST_INIT)

    def test_negative_streak_resets_on_positive_event(self):
        engine = EmotionEngine()
        for _ in range(3):
            engine.apply_event("IRRELEVANT_QUESTION")
        self.assertEqual(engine.neg_interaction_streak, 3)
        engine.apply_event("RELEVANT_QUESTION")
        self.assertEqual(engine.neg_interaction_streak, 0)

    def test_refusal_hysteresis_and_critical_state_suppression(self):
        engine = EmotionEngine()
        engine.state.patient_anxiety = 0.91
        engine.tick(SETTLE_INTERVAL)
        self.assertTrue(engine.refused)

        engine.state.patient_anxiety = 0.85  # inside the 0.80-0.90 hysteresis band
        engine.tick(SETTLE_INTERVAL)
        self.assertTrue(engine.refused)

        engine.state.patient_anxiety = 0.78
        engine.tick(SETTLE_INTERVAL)
        self.assertFalse(engine.refused)

        # S4/S5/S6 never refuse cooperation, even at 0.95 anxiety
        engine.clinical_state = "S4_CARDIAC_TAMPONADE"
        engine.state.patient_anxiety = 0.95
        engine.tick(SETTLE_INTERVAL)
        self.assertFalse(engine.refused)

    def test_idle_event_disabled_by_default(self):
        engine = EmotionEngine()
        self.assertEqual(sum(engine.apply_event("IDLE").values()), 0.0)
        self.assertFalse(config.EMOTION_IDLE_EVENT_ENABLED)

    def test_unknown_event_is_ignored(self):
        engine = EmotionEngine()
        self.assertEqual(sum(engine.apply_event("NOT_A_CODE").values()), 0.0)

    def test_ui_hint_suppression_keeps_accumulating(self):
        engine = EmotionEngine()
        for _ in range(5):
            engine.tick(1.0)
        # |0.004| < 0.005 -> silent for the player, still recorded internally
        self.assertLess(0.004, UI_HINT_MIN_DELTA)
        self.assertEqual(len(engine.visible_delta_log), 0)
        self.assertGreaterEqual(len(engine.delta_log), 3)
        self.assertAlmostEqual(engine.state.patient_trust, 0.396, places=9)
        self.assertAlmostEqual(engine.state.patient_anxiety, 0.504, places=9)
        self.assertAlmostEqual(engine.state.family_anxiety, 0.653, places=9)

        # an event delta above the hint threshold does reach the player stream
        engine.apply_event("COMFORT")
        self.assertGreaterEqual(len(engine.visible_delta_log), 1)


class EmotionPersistenceTests(unittest.TestCase):
    """spec/05 §3: dump/load must round-trip without losing state."""

    def test_dump_load_roundtrip(self):
        engine = EmotionEngine()
        engine.apply_event("RELEVANT_QUESTION")
        engine.tick(17.0)
        engine.state.patient_anxiety = 0.70
        engine.tick(10.0)

        payload = engine.dump()
        restored = EmotionEngine.load(json.loads(json.dumps(payload)))

        self.assertEqual(restored.snapshot(), engine.snapshot())
        self.assertAlmostEqual(restored.accumulator, engine.accumulator, places=9)
        self.assertAlmostEqual(restored.virtual_seconds, engine.virtual_seconds, places=9)
        self.assertEqual(restored.neg_interaction_streak, engine.neg_interaction_streak)
        self.assertEqual(restored.settle_count, engine.settle_count)
        self.assertAlmostEqual(restored.critical_anxiety_hold_s,
                               engine.critical_anxiety_hold_s, places=9)

    def test_load_missing_payload_uses_initial_values(self):
        for payload in (None, {}, {"state": {}}):
            engine = EmotionEngine.load(payload)
            self.assertEqual(engine.state.patient_trust, config.EMOTION_TRUST_INIT)
            self.assertEqual(engine.state.patient_anxiety,
                             config.EMOTION_PATIENT_ANXIETY_INIT)
            self.assertEqual(engine.state.family_anxiety,
                             config.EMOTION_FAMILY_ANXIETY_INIT)
            self.assertIsNone(engine.leave_reason)

    def test_partial_state_is_clamped(self):
        engine = EmotionEngine.load({"state": {"patient_trust": 4.0,
                                               "patient_anxiety": -2.0,
                                               "family_anxiety": "oops"}})
        self.assertEqual(engine.state.patient_trust, 1.0)
        self.assertEqual(engine.state.patient_anxiety, 0.0)
        self.assertEqual(engine.state.family_anxiety,
                         config.EMOTION_FAMILY_ANXIETY_INIT)


class SessionEmotionIntegrationTests(unittest.TestCase):
    """spec/05 §3: SimulationSession wiring and store persistence."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.store = SessionStore()
        self.session = self.store.create_session(CASE_ID, "测试学员")

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    def test_session_tick_settles_emotion(self):
        self.assertEqual(self.session.emotion_accum, 0.0)
        for _ in range(4):
            self.session.tick()
        self.assertAlmostEqual(self.session.emotion_accum, 4.0, places=9)
        self.session.tick()
        self.assertAlmostEqual(self.session.emotion_accum, 0.0, places=9)
        self.assertAlmostEqual(self.session.emotion.state.patient_trust,
                               0.396, places=9)
        self.assertAlmostEqual(self.session.agent_engine.state.patient_anxiety,
                               self.session.emotion.state.patient_anxiety, places=9)
        self.assertIsNone(self.session.terminal_reason)
        self.assertFalse(self.session.is_finished)

    def test_session_emotion_survives_store_restart(self):
        for _ in range(10):
            self.session.tick()
        self.session.emotion.apply_event("COMFORT")
        expected = self.session.emotion.snapshot()
        self.store.persist(self.session)

        SessionStore._instance = SessionStore()
        restored = SessionStore._instance.get_session(self.session.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.emotion.snapshot(), expected)
        self.assertAlmostEqual(restored.emotion_accum,
                               self.session.emotion_accum, places=9)
        self.assertEqual(restored.emotion.dump(), self.session.emotion.dump())
        self.assertIsNone(restored.terminal_reason)

    def test_cardiac_arrest_sets_terminal_reason_death(self):
        self.session.physiology_engine.state = PatientStateEnum.S6_CARDIAC_ARREST
        self.session.tick()
        self.assertEqual(self.session.terminal_reason, "death")
        self.assertTrue(self.session.is_finished)

        self.store.persist(self.session)
        SessionStore._instance = SessionStore()
        restored = SessionStore._instance.get_session(self.session.session_id)
        self.assertEqual(restored.terminal_reason, "death")

    def test_emotion_departure_marks_session_finished(self):
        for _ in range(12):
            self.session.emotion.apply_event("IRRELEVANT_QUESTION")
        self.assertEqual(self.session.terminal_reason, "leave_immediate")
        self.assertTrue(self.session.is_finished)

        self.store.persist(self.session)
        SessionStore._instance = SessionStore()
        restored = SessionStore._instance.get_session(self.session.session_id)
        self.assertEqual(restored.terminal_reason, "leave_immediate")
        self.assertEqual(restored.emotion.leave_reason, "leave_immediate")

    def test_legacy_snapshot_without_emotion_keys_loads(self):
        legacy = {
            "session_id": "sess_legacy01",
            "case_id": CASE_ID,
            "student_name": "老数据",
            "start_time": 1.0,
            "virtual_seconds": 42,
            "is_active": True,
            "is_finished": False,
            "actions": [],
            "dialogues": [],
            "agent_state": AgentState().model_dump(),
            "physiology": {},
            "assessment": None,
        }
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                       ("sess_legacy01", 1.0, json.dumps(legacy, ensure_ascii=False)))

        SessionStore._instance = None
        store = SessionStore()
        restored = store.get_session("sess_legacy01")
        self.assertIsNotNone(restored)
        self.assertIsNone(restored.terminal_reason)
        self.assertFalse(restored.is_finished)
        self.assertEqual(restored.emotion.state.patient_trust,
                         config.EMOTION_TRUST_INIT)
        self.assertEqual(restored.emotion.state.patient_anxiety,
                         config.EMOTION_PATIENT_ANXIETY_INIT)
        self.assertFalse(restored.emotion.refused)


class EmotionScriptBandTests(unittest.TestCase):
    """spec/01 §1.2: emotion_scripts 按 patient_trust 三档，缺失时无损回落旧台词."""

    @classmethod
    def setUpClass(cls):
        cls.chest = load_case("case_chest_pain.json")
        cls.cold = load_case("cases/case_gam_001_common_cold.json")

    def _engine(self, case=None):
        engine = AgentEngine(case_data=dict(case or self.chest))
        engine.sync_emotion({"patient_trust": 0.40, "patient_anxiety": 0.50})
        return engine

    def test_trust_bands_pick_high_mid_low_scripts(self):
        engine = self._engine()
        scripts = self.chest["emotion_scripts"]["E2_PAIN_DETAIL"]
        for trust, band in ((0.80, "high"), (0.50, "mid"), (0.20, "low")):
            with self.subTest(trust=trust):
                engine.sync_emotion({"patient_trust": trust})
                self.assertEqual(engine.resolve_emotion_band(trust), band)
                self.assertEqual(engine.emotion_script("E2_PAIN_DETAIL"), scripts[band])

    def test_band_boundaries_use_inclusive_ge(self):
        self.assertEqual(AgentEngine.resolve_emotion_band(0.70), "high")
        self.assertEqual(AgentEngine.resolve_emotion_band(0.6999999), "mid")
        self.assertEqual(AgentEngine.resolve_emotion_band(0.30), "mid")
        self.assertEqual(AgentEngine.resolve_emotion_band(0.29999), "low")

    def test_inquiry_returns_banded_patient_line(self):
        engine = self._engine()
        engine.sync_emotion({"patient_trust": 0.20})
        messages = engine.handle_inquiry("您胸口是怎么个疼法？")
        self.assertEqual(messages[-1].text, self.chest["emotion_scripts"]["E2_PAIN_DETAIL"]["low"])

        engine.sync_emotion({"patient_trust": 0.85})
        messages = engine.handle_inquiry("您胸口是怎么个疼法？")
        self.assertEqual(messages[-1].text, self.chest["emotion_scripts"]["E2_PAIN_DETAIL"]["high"])

    def test_comfort_uses_consent_node(self):
        engine = self._engine()
        engine.sync_emotion({"patient_trust": 0.80})
        messages = engine.handle_inquiry("大爷您别紧张，我们全力救治")
        self.assertEqual(messages[1].text, self.chest["emotion_scripts"]["E6_PRE_OUTCOME"]["high"])

    def test_free_chat_falls_back_to_consult_node(self):
        engine = self._engine()
        engine.sync_emotion({"patient_trust": 0.50})
        messages = engine.handle_inquiry("今天天气不错啊")
        self.assertEqual(messages[-1].text, self.chest["emotion_scripts"]["E1_CONSULT"]["mid"])

    def test_case_without_emotion_scripts_keeps_legacy_lines(self):
        legacy = AgentEngine(case_data={"case_id": CASE_ID})
        self.assertFalse(legacy.has_emotion_scripts())
        messages = legacy.handle_inquiry("您胸口是怎么个疼法？")
        self.assertIn("刀劈开撕裂", messages[-1].text)

        bare = AgentEngine()
        self.assertIsNone(bare.emotion_script("E2_PAIN_DETAIL"))
        self.assertEqual(bare.patient_emotion_line("E2_PAIN_DETAIL", "旧台词"), "旧台词")

    def test_malformed_emotion_scripts_fall_back(self):
        engine = AgentEngine(case_data={"case_id": CASE_ID, "emotion_scripts": {"E2_PAIN_DETAIL": {"high": "  "}}})
        self.assertFalse(engine.has_emotion_scripts())
        self.assertIsNone(engine.emotion_script("E2_PAIN_DETAIL", trust=0.9))
        engine.case_data["emotion_scripts"] = "not-a-mapping"
        self.assertIsNone(engine.emotion_script("E2_PAIN_DETAIL", trust=0.9))

    def test_catalogue_case_consumes_emotion_scripts(self):
        engine = AgentEngine(case_data=dict(self.cold))
        engine.state.patient_trust = 0.80
        messages = engine.handle_inquiry("大夫，别紧张，我们会帮您")
        self.assertEqual(messages[-1].text, self.cold["emotion_scripts"]["E6_PRE_OUTCOME"]["high"])


class PrescriptionRuleTests(unittest.TestCase):
    """spec/01 §1.5: prescription_rules[].severity 驱动 LETHAL / HARMFUL 分支."""

    @classmethod
    def setUpClass(cls):
        cls.asthma = load_case("cases/case_gam_003_asthma.json")
        cls.cold = load_case("cases/case_gam_001_common_cold.json")
        cls.chest = load_case("case_chest_pain.json")

    def test_lethal_rule_reaches_cardiac_arrest_within_30_seconds(self):
        engine = PhysiologyEngine(case_data=dict(self.asthma))
        self.assertFalse(engine.high_risk_aortic)
        result = engine.apply_medication("阿司匹林肠溶片 300mg", 300, "mg")
        self.assertTrue(result["is_lethal"])
        self.assertTrue(result["is_red_line"])
        self.assertEqual(result["severity"], "LETHAL")
        self.assertEqual(result["rule_id"], "RX_RED_LINE_NSAID")
        self.assertNotEqual(result["new_state"], PatientStateEnum.S6_CARDIAC_ARREST)

        for _ in range(30):
            engine.advance_time(1.0)
        self.assertEqual(engine.get_state(), PatientStateEnum.S6_CARDIAC_ARREST)
        self.assertTrue(engine.fatal_death_occurred)
        self.assertEqual(engine.get_vitals().hr, 0.0)

    def test_harmful_rule_deteriorates_without_killing(self):
        engine = PhysiologyEngine(case_data=dict(self.cold))
        result = engine.apply_medication("阿司匹林", 100, "mg")
        self.assertTrue(result["is_harmful"])
        self.assertFalse(result["is_lethal"])
        self.assertEqual(result["severity"], "HARMFUL")
        self.assertFalse(result["harmful_escalated"])
        for _ in range(60):
            engine.advance_time(1.0)
        self.assertNotEqual(engine.get_state(), PatientStateEnum.S6_CARDIAC_ARREST)
        self.assertFalse(engine.fatal_death_occurred)

    def test_harmful_rules_escalate_on_second_hit(self):
        engine = PhysiologyEngine(case_data=dict(self.cold))
        engine.apply_medication("阿司匹林", 100, "mg")
        second = engine.apply_medication("阿司匹林", 100, "mg")
        self.assertTrue(second["harmful_escalated"])
        self.assertEqual(second["harmful_hits"], 2)
        self.assertIsNotNone(engine.get_rupture_countdown())

    def test_red_line_rule_beats_keyword_fuzzy_match(self):
        # 病例 1 的硝普钠单用是 RED_LINE + HARMFUL：必须在关键词表之前被认出来
        engine = PhysiologyEngine(case_data=dict(self.chest))
        result = engine.apply_medication("硝普钠", 0.5, "ug/kg/min")
        self.assertEqual(result["rule_id"], "RX_REDLINE_VASODILATOR_ALONE")
        self.assertTrue(result["is_harmful"])
        self.assertFalse(result["is_lethal"])
        self.assertIn("反射性心动过速", result["message"])
        self.assertFalse(engine.vasodilator_active)

    def test_prerequisite_disables_vasodilator_red_line(self):
        engine = PhysiologyEngine(case_data=dict(self.chest))
        engine.apply_medication("艾司洛尔注射液", 0.5, "mg/kg")
        self.assertTrue(engine.beta_blocker_active)
        result = engine.apply_medication("硝普钠注射液", 0.5, "ug/kg/min")
        self.assertFalse(result["is_lethal"])
        self.assertFalse(result["is_harmful"])
        self.assertTrue(engine.vasodilator_active)
        self.assertEqual(engine.get_state(), PatientStateEnum.S2_PHARMACO_CONTROLLED)

    def test_malformed_prescription_rules_are_ignored(self):
        engine = PhysiologyEngine(case_data={"case_id": "x", "prescription_rules": ["bad", {"verdict": "RED_LINE"}]})
        self.assertEqual(engine.prescription_rules, [])
        self.assertIsNone(engine.match_prescription_rule("阿司匹林"))


class HemodynamicRuleSwitchTests(unittest.TestCase):
    """D-30 / D-33: 病例行为开关来自 hemodynamic_rules，时钟常量来自 config."""

    def test_catalogue_case_does_not_inherit_dissection_behaviour(self):
        engine = PhysiologyEngine(case_data=load_case("cases/case_gam_001_common_cold.json"))
        self.assertFalse(engine.high_risk_aortic)
        for _ in range(400):
            engine.advance_time(1.0)
        self.assertEqual(engine.get_state(), PatientStateEnum.S0_STABLE)
        self.assertIsNone(engine.get_rupture_countdown())
        self.assertFalse(engine.fatal_death_occurred)

    def test_dissection_case_reads_countdown_from_case_json(self):
        engine = PhysiologyEngine(case_data=load_case("case_chest_pain.json"))
        self.assertTrue(engine.high_risk_aortic)
        self.assertEqual(engine.rupture_to_effusion_seconds, 120.0)
        self.assertEqual(engine.crisis_timeout_seconds, 300.0)

    def test_non_chest_case_with_explicit_rules_gets_dissection_clock(self):
        engine = PhysiologyEngine(case_data={
            "case_id": "case_gam_001_common_cold",
            "hemodynamic_rules": {"lethal_rupture_countdown_seconds": 45},
        })
        self.assertTrue(engine.high_risk_aortic)
        engine.apply_medication("阿司匹林", 100, "mg")
        self.assertEqual(engine.get_rupture_countdown(), 45)

    def test_missing_case_data_keeps_legacy_behaviour(self):
        engine = PhysiologyEngine()
        self.assertTrue(engine.high_risk_aortic)
        self.assertEqual(engine.rupture_to_effusion_seconds, config.PHYS_RUPTURE_TO_EFFUSION_S)

    def test_lethal_countdown_uses_config_not_and_expression(self):
        self.assertEqual(config.PHYS_RUPTURE_TO_EFFUSION_S, 120.0)
        self.assertEqual(config.PHYS_TAMPONADE_STAGE_S, 30.0)
        self.assertEqual(config.PHYS_RUPTURE_TO_ARREST_S, 15.0)
        engine = PhysiologyEngine()
        engine.apply_medication("阿司匹林肠溶片 300mg", 300, "mg")
        self.assertEqual(engine.get_rupture_countdown(), int(config.PHYS_RUPTURE_TO_EFFUSION_S))
        for _ in range(166):
            engine.advance_time(1.0)
        self.assertEqual(engine.get_state(), PatientStateEnum.S6_CARDIAC_ARREST)


class _FakeLLMResponse:
    """Minimal stand-in for ``httpx.Response`` used by the online branches."""

    def __init__(self, content: str, status_code: int = 200):
        self.status_code = status_code
        self.text = content

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}


class LlmDialoguePathTests(unittest.TestCase):
    """Offline-safe coverage of AgentEngine's optional online branches."""

    def setUp(self):
        self.engine = AgentEngine()

    def _call_multi(self, content, *, provider="deepseek", key="sk-test", status=200):
        with patch.object(config, "is_online_llm_active", return_value=True), \
                patch.object(config, "LLM_PROVIDER", provider), \
                patch.object(config, "DEEPSEEK_API_KEY", key), \
                patch.object(config, "OPENAI_API_KEY", key), \
                patch("httpx.Client.post", return_value=_FakeLLMResponse(content, status)):
            return self.engine._generate_multi_agent_llm_response("哪里疼？")

    def test_json_reply_parses_patient_and_family(self):
        payload = json.dumps({
            "patient": "呃啊……胸口像被刀劈开。",
            "patient_emotion": "relieved",
            "family": "他在工地搬水泥时突然就疼起来了！",
            "family_emotion": "determined",
        }, ensure_ascii=False)
        trust_before = self.engine.state.patient_trust
        messages = self._call_multi(payload)
        self.assertEqual([m.speaker for m in messages], ["patient", "family"])
        self.assertGreater(self.engine.state.patient_trust, trust_before)

    def test_fenced_json_is_unwrapped(self):
        payload = "```json\n" + json.dumps({"patient": "疼……", "family": ""}, ensure_ascii=False) + "\n```"
        messages = self._call_multi(payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "patient")

    def test_plain_fence_is_unwrapped(self):
        payload = "```\n" + json.dumps({"patient": "疼……", "family": ""}, ensure_ascii=False) + "\n```"
        self.assertIsNotNone(self._call_multi(payload))

    def test_raw_text_falls_back_to_patient_bubble(self):
        messages = self._call_multi("（疼得说不出话）")
        self.assertEqual(len(messages), 1)
        self.assertIn("来自在线大模型协同", messages[0].text)

    def test_http_error_returns_none(self):
        self.assertIsNone(self._call_multi("boom", status=500))

    def test_openai_and_custom_providers(self):
        payload = json.dumps({"patient": "疼……", "family": ""}, ensure_ascii=False)
        self.assertIsNotNone(self._call_multi(payload, provider="openai"))
        self.assertIsNotNone(self._call_multi(payload, provider="custom", key=""))

    def test_missing_api_key_returns_none(self):
        self.assertIsNone(self._call_multi("x", key=""))

    def test_single_voice_llm_returns_text(self):
        with patch.object(config, "is_online_llm_active", return_value=True), \
                patch.object(config, "LLM_PROVIDER", "deepseek"), \
                patch.object(config, "DEEPSEEK_API_KEY", "sk-test"), \
                patch("httpx.Client.post", return_value=_FakeLLMResponse("呃啊……大夫……")):
            self.assertEqual(self.engine._generate_llm_response("疼吗"), "呃啊……大夫……")
        with patch.object(config, "LLM_PROVIDER", "deepseek"), \
                patch.object(config, "DEEPSEEK_API_KEY", "sk-test"), \
                patch("httpx.Client.post", return_value=_FakeLLMResponse("down", 503)):
            self.assertIsNone(self.engine._generate_llm_response("疼吗"))

    def test_online_branch_short_circuits_offline_templates(self):
        payload = json.dumps({"patient": "在线患者台词", "family": ""}, ensure_ascii=False)
        with patch.object(config, "is_online_llm_active", return_value=True), \
                patch.object(config, "LLM_PROVIDER", "deepseek"), \
                patch.object(config, "DEEPSEEK_API_KEY", "sk-test"), \
                patch("httpx.Client.post", return_value=_FakeLLMResponse(payload)):
            messages = self.engine.handle_inquiry("您哪里疼？")
        self.assertIn("在线患者台词", messages[-1].text)

    def test_spontaneous_family_interruption(self):
        self.engine.last_interaction_time = time.time() - 60
        messages = self.engine.check_spontaneous_events(60.0, PatientStateEnum.S0_STABLE)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.engine.state.family_interrupted_count, 1)

        # 第二次静默仍可触发，第三次达到上限后静默
        self.engine.last_interaction_time = time.time() - 60
        self.assertEqual(len(self.engine.check_spontaneous_events(60.0, PatientStateEnum.S0_STABLE)), 1)
        self.engine.last_interaction_time = time.time() - 60
        self.assertEqual(self.engine.check_spontaneous_events(60.0, PatientStateEnum.S0_STABLE), [])

    def test_spontaneous_interruption_uses_llm_when_online(self):
        self.engine.last_interaction_time = time.time() - 60
        with patch.object(config, "is_online_llm_active", return_value=True), \
                patch.object(config, "LLM_PROVIDER", "deepseek"), \
                patch.object(config, "DEEPSEEK_API_KEY", "sk-test"), \
                patch("httpx.Client.post", return_value=_FakeLLMResponse("医生！快点啊！")):
            messages = self.engine.check_spontaneous_events(60.0, PatientStateEnum.S0_STABLE)
        self.assertIn("在线大模型", messages[0].text)

    def test_nurse_generators_and_red_line_order_warning(self):
        order = self.engine.handle_nurse_order(ActionType.MEDICATION, "给患者嚼服阿司匹林300mg")
        self.assertIsNotNone(order["warning"])
        self.assertTrue(self.engine.state.active_critical_alarms)
        self.assertIn("护士执行", self.engine.notify_order_dispatched("吸氧").text)
        self.assertIn("心包填塞", self.engine.generate_tamponade_alert(60.0).text)
        self.assertIn("警觉拦截", self.engine.generate_nurse_warning("阿司匹林").text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
