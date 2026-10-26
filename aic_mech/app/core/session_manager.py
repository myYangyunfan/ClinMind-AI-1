"""Session Manager for ClinMind-AI Clinical Simulation.

Manages runtime state, instances of physiological engines, multi-agent engines,
action histories, and evaluation objects for concurrent or single-user sessions.
"""

import time
import uuid
import json
import sqlite3
import threading
from contextlib import closing
import config
from typing import Dict, Any, List, Optional
from app.models.entities import ActionRecord, OSCEAssessment, PatientStateEnum, VitalsVector, AgentState, DialogueMessage
from app.core.physiology_engine import PhysiologyEngine
from app.core.agent_engine import AgentEngine
from app.core.guardrail_engine import GuardrailEngine
from app.core.ged_evaluator import GEDEvaluator
from app.core.emotion_engine import EmotionEngine, EmotionState
from app.core.case_repository import get_case_repository


# Guards the session registry and the sqlite snapshot writes against the
# 1 Hz lifespan clock racing with API request threads.
_STORE_LOCK = threading.RLock()


class SimulationSession:
    """Encapsulates a single student's clinical training session."""

    def __init__(self, session_id: str, case_id: str, student_name: str = "参赛学员"):
        self.session_id: str = session_id
        self.case_id: str = case_id
        self.student_name: str = student_name
        self.start_time: float = time.time()
        self.virtual_seconds: int = 0
        self.is_active: bool = True

        # Terminal state: "death" | "leave_immediate" | "leave_sustained" | None.
        # ``is_finished`` is derived from it (see the properties below).
        self._terminal_reason: Optional[str] = None
        self._lock = threading.RLock()

        # Keep the selected teaching case with the session.  The chest-pain
        # case keeps its original high-risk engine; catalogue cases use their
        # own patient profile, vitals and dialogue/report data.
        self.case_data: Dict[str, Any] = get_case_repository().require(case_id)
        initial_vitals = self.case_data.get("initial_vitals") or {}
        initial_vector = VitalsVector.model_validate(initial_vitals) if initial_vitals else None

        # Core Engines
        self.physiology_engine: PhysiologyEngine = PhysiologyEngine(initial_vitals=initial_vector, case_data=self.case_data)
        self.agent_engine: AgentEngine = AgentEngine(case_data=self.case_data)
        self.guardrail_engine: GuardrailEngine = GuardrailEngine(case_data=self.case_data)
        self.ged_evaluator: GEDEvaluator = GEDEvaluator()
        # 5-second emotion settlement engine (spec/05 §3)
        self.emotion: EmotionEngine = EmotionEngine(EmotionState())
        self.emotion.sync_clinical_state(self.physiology_engine.get_state())
        self.emotion_accum: float = 0.0

        # Action history and final report
        self.action_history: List[ActionRecord] = []
        self.final_assessment: Optional[OSCEAssessment] = None
        self.dialogues: List[DialogueMessage] = []
        self.spontaneous_dialogues: List[DialogueMessage] = []

    # -- terminal state -----------------------------------------------------
    @property
    def terminal_reason(self) -> Optional[str]:
        """Effective terminal reason, including an emotion-driven departure."""
        if self._terminal_reason is not None:
            return self._terminal_reason
        emotion = getattr(self, "emotion", None)
        if emotion is not None and emotion.leave_reason:
            return emotion.leave_reason
        return None

    @terminal_reason.setter
    def terminal_reason(self, value: Optional[str]) -> None:
        self._terminal_reason = value

    @property
    def is_finished(self) -> bool:
        """Derived value kept for the existing routes and WebSocket frames."""
        return self.terminal_reason is not None

    @is_finished.setter
    def is_finished(self, value: bool) -> None:
        # Legacy callers (e.g. evaluation routes) still assign this flag.
        if not value:
            self._terminal_reason = None
        elif self._terminal_reason is None:
            self._terminal_reason = "finished"

    def _refresh_terminal(self) -> None:
        """Promote physiology / emotion terminals into ``terminal_reason``."""
        if self.terminal_reason is not None:
            return
        state = self.physiology_engine.get_state()
        if state == PatientStateEnum.S6_CARDIAC_ARREST or getattr(
                self.physiology_engine, "fatal_death_occurred", False):
            self._terminal_reason = "death"
            return
        if self.emotion.leave_reason:
            self._terminal_reason = self.emotion.leave_reason

    def tick(self, dt: float = 1.0) -> VitalsVector:
        """Advance session time by dt seconds (physiology then emotion)."""
        with self._lock:
            if not self.is_active or self.is_finished:
                return self.physiology_engine.get_current_vitals()

            self.virtual_seconds += int(dt)
            vitals = self.physiology_engine.advance_time(dt)

            # Emotion settles on its own 5-second accumulator; mirror the
            # clinical phase first so S4/S5/S6 suppress refusal in this tick.
            self.emotion.sync_clinical_state(self.physiology_engine.get_state())
            self.emotion.tick(dt)
            self.emotion_accum = self.emotion.accumulator
            self.agent_engine.state.patient_anxiety = self.emotion.state.patient_anxiety
            self._refresh_terminal()
            return vitals


class SessionStore:
    """In-memory singleton registry for active training sessions."""

    _instance: Optional["SessionStore"] = None

    def __init__(self):
        self._sessions: Dict[str, SimulationSession] = {}
        self._default_session_id: Optional[str] = None
        config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = config.RUNTIME_DIR / "clinmind.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, updated REAL NOT NULL, snapshot TEXT NOT NULL)")
            rows = db.execute("SELECT snapshot FROM sessions ORDER BY updated DESC LIMIT 50").fetchall()
        for (raw,) in rows:
            data = json.loads(raw)
            session = SimulationSession(data.get("session_id"), data.get("case_id"), data.get("student_name", "参赛学员"))
            session.start_time = data.get("start_time", time.time())
            session.virtual_seconds = data.get("virtual_seconds", 0)
            session.is_active = data.get("is_active", True)
            # Snapshots written before the P0 emotion work have no
            # ``terminal_reason`` / ``emotion`` keys: fall back to the legacy
            # finished flag and to a fresh engine (initial values).
            session.terminal_reason = data.get("terminal_reason")
            if session.terminal_reason is None and data.get("is_finished"):
                session.terminal_reason = "finished"
            session.emotion = EmotionEngine.load(data.get("emotion"))
            session.emotion_accum = session.emotion.accumulator
            session.action_history = [ActionRecord.model_validate(a) for a in data.get("actions", [])]
            session.dialogues = [DialogueMessage.model_validate(m) for m in data.get("dialogues", [])]
            session.agent_engine.state = AgentState.model_validate(data.get("agent_state", {}))
            session.agent_engine.last_interaction_time = time.time()
            for key, value in (data.get("physiology") or {}).items():
                if key == "vitals":
                    value = VitalsVector.model_validate(value)
                elif key == "state":
                    value = PatientStateEnum(value)
                setattr(session.physiology_engine, key, value)
            session.emotion.sync_clinical_state(session.physiology_engine.get_state())
            session.agent_engine.state.patient_anxiety = session.emotion.state.patient_anxiety
            if data.get("assessment"):
                session.final_assessment = OSCEAssessment.model_validate(data["assessment"])
            self._sessions[session.session_id] = session

    @classmethod
    def get_instance(cls) -> "SessionStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def create_session(self, case_id: str, student_name: str = "参赛学员") -> SimulationSession:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        session = SimulationSession(session_id, case_id, student_name)
        self._sessions[session_id] = session
        self._default_session_id = session_id
        self.persist(session)
        return session

    def get_session(self, session_id: Optional[str] = None) -> Optional[SimulationSession]:
        if not session_id or session_id == "default":
            session_id = self._default_session_id

        return self._sessions.get(session_id) if session_id else None

    def persist(self, session: SimulationSession) -> None:
        """Store a recoverable local snapshot without external database services."""
        # Hold the session lock so a concurrent API thread appending to
        # action_history/dialogues cannot mutate the lists mid-serialisation.
        with getattr(session, "_lock", _STORE_LOCK):
            snapshot = self._build_snapshot(session)
            with _STORE_LOCK:
                with closing(sqlite3.connect(self.db_path)) as db, db:
                    db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", (session.session_id, time.time(), json.dumps(snapshot, ensure_ascii=False)))

    @staticmethod
    def _build_snapshot(session: SimulationSession) -> Dict[str, Any]:
        engine = session.physiology_engine
        keys = ["elapsed_seconds", "active_medications", "rupture_countdown", "rupture_triggered_reason", "fatal_death_occurred", "beta_blocker_active", "vasodilator_active", "analgesic_active", "_noise_phase"]
        physiology = {key: getattr(engine, key) for key in keys}
        physiology.update(vitals=engine.get_current_vitals().model_dump(), state=engine.state.value)
        return {
            "session_id": session.session_id, "case_id": session.case_id,
            "student_name": session.student_name, "start_time": session.start_time,
            "virtual_seconds": session.virtual_seconds, "is_active": session.is_active,
            "is_finished": session.is_finished,
            "terminal_reason": session.terminal_reason,
            "emotion": session.emotion.dump(),
            "actions": [a.model_dump(mode="json") for a in session.action_history],
            "dialogues": [m.model_dump(mode="json") for m in session.dialogues],
            "agent_state": session.agent_engine.state.model_dump(), "physiology": physiology,
            "assessment": session.final_assessment.model_dump() if session.final_assessment else None,
        }

    def tick_all(self) -> None:
        """One server clock, shared by all browser tabs and WebSocket clients."""
        with _STORE_LOCK:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.spontaneous_dialogues = []
            if session.is_active and not session.is_finished:
                session.tick()
                session.spontaneous_dialogues = session.agent_engine.check_spontaneous_events(session.virtual_seconds, session.physiology_engine.get_state())
                session.dialogues.extend(session.spontaneous_dialogues)
                # Persist on the regular cadence and immediately on terminal.
                if session.virtual_seconds % 10 == 0 or session.is_finished:
                    self.persist(session)

    def get_all_sessions(self) -> Dict[str, SimulationSession]:
        return self._sessions
