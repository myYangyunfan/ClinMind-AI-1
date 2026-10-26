# -*- coding: utf-8 -*-
"""Unit tests for the P0 emotion engine and its session integration.

Covers spec/05-P0接口契约.md §2-§3 and spec/02-数值与状态机规格.md §2-§4:
time-term bounds, the 5-second settlement accumulator, the three judgement
lines, multiplier snapshots, the 0.005 UI suppression rule and persistence.
"""

import json
import sqlite3
import tempfile
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
from app.models.entities import AgentState, PatientStateEnum
from app.core.session_manager import SessionStore

CASE_ID = "case_chest_pain_001"


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
