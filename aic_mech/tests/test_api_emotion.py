"""API-level emotion channel tests (contract 05 §3/§4).

Covers the frozen P0 contract from the API side only:
  * workspace / status / WS payload carry the 6 new emotion fields;
  * the old fields are still there and `agent_anxiety` still equals
    `family_anxiety` (it has always been family anxiety under a misleading
    name);
  * every action reaches `session.emotion.apply_event()` per §4 mapping,
    and an unjudged inquiry honestly reports `emotion_applied=false`;
  * idling never flips `refuse` to true.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Keep the offline rule engine deterministic, exactly like run_all_tests.py.
os.environ.setdefault("USE_ONLINE_LLM", "false")

from fastapi.testclient import TestClient
import config
from app.core.session_manager import SessionStore
from app.server import app

EMOTION_KEYS = (
    "patient_anxiety",
    "family_anxiety",
    "trust_trend",
    "anxiety_trend",
    "refuse",
    "terminal_reason",
)


class ApiEmotionChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.client = TestClient(app)
        self.session_id = self.client.post("/api/case/start", json={}).json()["session_id"]

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    # ---------------- helpers ----------------
    def action(self, content, action_type, **extra):
        return self.client.post(
            "/api/action/execute",
            json={"session_id": self.session_id, "action_type": action_type, "content": content, **extra},
        )

    def workspace(self):
        resp = self.client.get(f"/api/case/session/{self.session_id}/workspace")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def status(self):
        resp = self.client.get("/api/case/session/status", params={"session_id": self.session_id})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def assert_emotion_shape(self, data, where):
        for key in EMOTION_KEYS:
            self.assertIn(key, data, f"{where} 缺少情绪字段 {key}")
        self.assertIsInstance(data["patient_anxiety"], float, where)
        self.assertIsInstance(data["family_anxiety"], float, where)
        # bool 是 int 的子类，趋势位要先卡类型再卡取值。
        self.assertIsInstance(data["trust_trend"], int, where)
        self.assertIsInstance(data["anxiety_trend"], int, where)
        self.assertNotIsInstance(data["trust_trend"], bool, where)
        self.assertNotIsInstance(data["anxiety_trend"], bool, where)
        self.assertIn(data["trust_trend"], (-1, 0, 1), where)
        self.assertIn(data["anxiety_trend"], (-1, 0, 1), where)
        self.assertIsInstance(data["refuse"], bool, where)
        self.assertTrue(
            data["terminal_reason"] is None or isinstance(data["terminal_reason"], str), where
        )
        for key in ("patient_anxiety", "family_anxiety"):
            self.assertGreaterEqual(data[key], 0.0, f"{where}.{key}")
            self.assertLessEqual(data[key], 1.0, f"{where}.{key}")

    # ---------------- workspace / status ----------------
    def test_workspace_exposes_emotion_channels_after_actions(self):
        self.assertEqual(self.action("请告诉我胸痛的性质和位置", "inquiry").status_code, 200)
        self.assertEqual(self.action("测量双侧上肢血压对比", "physical_exam").status_code, 200)

        data = self.workspace()
        self.assert_emotion_shape(data, "workspace")
        # 旧字段一个都不能少（只增不改）。
        for legacy in ("trust_level", "family_anxiety", "is_finished", "vitals", "patient_state"):
            self.assertIn(legacy, data)
        self.assertFalse(data["is_finished"])
        self.assertIsNone(data["terminal_reason"])

    def test_status_agent_anxiety_is_family_anxiety(self):
        status = self.status()
        self.assert_emotion_shape(status, "status")
        # 兼容字段的真实语义：agent_anxiety 装的一直是家属焦虑。
        self.assertEqual(status["agent_anxiety"], status["family_anxiety"])

        workspace = self.workspace()
        self.assertAlmostEqual(workspace["family_anxiety"], status["family_anxiety"], places=2)

    # ---------------- websocket ----------------
    def test_ws_payload_carries_emotion_channels(self):
        url = f"/ws/vitals/{self.session_id}"
        with self.client.websocket_connect(url) as ws:
            payload = ws.receive_json()
        self.assertEqual(payload["type"], "vitals_update")
        self.assert_emotion_shape(payload, "ws")
        self.assertIn("trust_level", payload)
        self.assertIn("is_finished", payload)

    def test_emotion_channels_survive_store_restart(self):
        self.assertEqual(self.action("测量双侧上肢血压对比", "physical_exam").status_code, 200)
        before = self.workspace()

        store = SessionStore.get_instance()
        store.persist(store.get_session(self.session_id))
        SessionStore._instance = SessionStore()

        after = self.workspace()
        self.assert_emotion_shape(after, "workspace-after-restart")
        for key in EMOTION_KEYS:
            self.assertEqual(after[key], before[key], f"{key} 未随会话持久化")

    # ---------------- action -> event mapping ----------------
    def test_unjudged_inquiry_falls_back_without_claiming_judgement(self):
        from app.api import routes_action

        before = self.workspace()
        real_split = routes_action._split_inquiry_result
        # 判定由 agent_engine 提供（engine-dev 已接入三分类）；本用例只锁定
        # API 层的兜底分支：一旦拿不到 question_kind，必须按 RELEVANT_QUESTION
        # 落账并如实回 emotion_applied=false。这里把判定「摘掉」，不等 engine 行为。
        with patch.object(
            routes_action,
            "_split_inquiry_result",
            lambda result: (real_split(result)[0], None),
        ):
            body = self.action("请告诉我胸痛的性质和位置", "inquiry").json()
        self.assertIn("emotion_applied", body)
        self.assertIs(body["emotion_applied"], False)
        # 标志是 false，事件本身仍要真的落账：信任 +0.03、患者焦虑 -0.02。
        after = self.workspace()
        self.assertGreater(after["trust_level"], before["trust_level"])
        self.assertLess(after["patient_anxiety"], before["patient_anxiety"])

    def test_physical_exam_applies_exam_event(self):
        body = self.action("测量双侧上肢血压对比", "physical_exam").json()
        self.assertIs(body["emotion_applied"], True)

    def test_comfort_inquiry_applies_comfort_event(self):
        body = self.action("请您别紧张，我们会全力救治，请相信我", "inquiry").json()
        self.assertIs(body["emotion_applied"], True)

    def test_consent_disposition_lowers_family_anxiety(self):
        before = self.workspace()
        body = self.action("向家属交代病情并签署知情同意书", "rescue_disposition").json()
        self.assertIs(body["emotion_applied"], True)
        after = self.workspace()
        self.assertLess(after["family_anxiety"], before["family_anxiety"])

    def test_medication_maps_correct_treatment_and_red_line(self):
        ok = self.action(
            "艾司洛尔 0.5mg/kg 静脉推注", "medication", params={"dose": 0.5, "unit": "mg/kg"}
        ).json()
        self.assertFalse(ok["is_contraindicated"])
        self.assertIs(ok["emotion_applied"], True)

        bad = self.action(
            "阿司匹林肠溶片 300mg", "medication", params={"dose": 300, "unit": "mg"}
        ).json()
        self.assertTrue(bad["is_contraindicated"])
        self.assertIs(bad["emotion_applied"], True)
        self.assertTrue(bad["warning"])

    # ---------------- idle safety ----------------
    def test_idling_never_sets_refuse(self):
        store = SessionStore.get_instance()
        # 8 分钟挂机（验收 05 §6-4）：纯时间流逝不得判负、不得拒绝配合。
        for _ in range(480):
            store.tick_all()

        data = self.workspace()
        self.assert_emotion_shape(data, "idle-workspace")
        self.assertIs(data["refuse"], False)
        self.assertFalse(data["is_finished"])
        self.assertIsNone(data["terminal_reason"])
        # 验收 05 §6-4：纯时间项只能把信任压到 0.15、焦虑抬到 0.85，不得判负。
        self.assertAlmostEqual(data["trust_level"], 0.15, places=2)
        self.assertAlmostEqual(data["patient_anxiety"], 0.85, places=2)


class TerminalStatusCodeTests(unittest.TestCase):
    """T13/T14：终局状态码要能区分交卷/离院（410）与死亡（409）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.client = TestClient(app)
        self.session_id = self.client.post("/api/case/start", json={}).json()["session_id"]
        self.store = SessionStore.get_instance()

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    def action(self, content="再补做一次体格检查", action_type="physical_exam", **extra):
        return self.client.post(
            "/api/action/execute",
            json={"session_id": self.session_id, "action_type": action_type, "content": content, **extra},
        )

    def test_finished_session_returns_410(self):
        resp = self.client.post(
            "/api/eval/submit",
            json={"session_id": self.session_id, "final_diagnosis": "主动脉夹层"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.store.get_session(self.session_id).terminal_reason, "finished")

        blocked = self.action()
        self.assertEqual(blocked.status_code, 410, blocked.text)
        self.assertIn("已结束", blocked.json()["detail"])

    def test_death_terminal_returns_409(self):
        # 夹层病例输注阿司匹林：红线致死，随后靠时钟推进到 S6 死亡终局。
        lethal = self.action("阿司匹林肠溶片 300mg", "medication", params={"dose": 300, "unit": "mg"})
        self.assertEqual(lethal.status_code, 200, lethal.text)
        self.assertTrue(lethal.json()["is_contraindicated"])

        session = self.store.get_session(self.session_id)
        for _ in range(600):
            if session.is_finished:
                break
            self.store.tick_all()
        self.assertTrue(session.is_finished)
        self.assertEqual(session.terminal_reason, "death")

        blocked = self.action()
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("死亡", blocked.json()["detail"])

    def test_unknown_terminal_reason_falls_back_to_400(self):
        session = self.store.get_session(self.session_id)
        # 未知终局不许猜：既不是死亡也不是正常结案时仍走 400 兜底。
        session.terminal_reason = "mystery_end"
        self.assertTrue(session.is_finished)

        blocked = self.action()
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertIn("已结案", blocked.json()["detail"])


class LegacyObserveActionRetiredTests(unittest.TestCase):
    """K-05：observe_action 的 legacy 直写拆掉后，agent 账必须与 engine 一致。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "RUNTIME_DIR", Path(self.temp.name))
        self.path_patch.start()
        self.old_store = SessionStore._instance
        SessionStore._instance = None
        self.client = TestClient(app)
        self.session_id = self.client.post("/api/case/start", json={}).json()["session_id"]
        self.store = SessionStore.get_instance()

    def tearDown(self):
        SessionStore._instance = self.old_store
        self.path_patch.stop()
        self.temp.cleanup()

    def test_agent_matches_engine_after_repeated_treatments(self):
        session = self.store.get_session(self.session_id)
        for index in range(4):
            resp = self.client.post(
                "/api/action/execute",
                json={
                    "session_id": self.session_id,
                    "action_type": "medication",
                    "content": "艾司洛尔 0.5mg/kg 静脉推注",
                    "params": {"dose": 0.5, "unit": "mg/kg"},
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            # 第 4 次 CORRECT_TREATMENT 会被 max_times=3 压掉（delta=0），
            # 正是 legacy 直写会把 agent 账抬高的那个窗口。
            for _ in range(3):
                self.store.tick_all()
            engine_trust = session.emotion.snapshot()["patient_trust"]
            self.assertAlmostEqual(
                session.agent_engine.state.patient_trust, engine_trust, places=6,
                msg=f"第 {index + 1} 次处置后 agent 账偏离 engine",
            )
            self.assertAlmostEqual(
                session.agent_engine.state.family_anxiety,
                session.emotion.snapshot()["family_anxiety"], places=6,
            )


if __name__ == "__main__":
    unittest.main()
