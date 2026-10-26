"""Case Management and Session Initialization REST Routes."""

from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from app.models.schemas import StartCaseRequest, CaseSummaryDTO, SessionStateResponse
from app.core.session_manager import SessionStore
from app.core.case_repository import get_case_repository
import config

router = APIRouter(prefix="/api/case", tags=["Case Management"])


def emotion_view(session) -> Dict[str, Any]:
    """Read the emotion channels for API output (contract 05 §4).

    `session.emotion` (EmotionEngine) is the owner of the four emotion
    channels.  Older persisted sessions may predate it, so fall back to
    `agent_engine.state` values with the contract defaults instead of
    raising — the response shape must stay stable either way.
    """
    emotion = getattr(session, "emotion", None)
    snap: Dict[str, Any] = {}
    if emotion is not None:
        try:
            snap = emotion.snapshot() or {}
        except Exception:
            snap = {}
    agent_state = getattr(getattr(session, "agent_engine", None), "state", None)
    return {
        "patient_trust": float(snap.get("patient_trust", getattr(agent_state, "patient_trust", 0.40))),
        "patient_anxiety": float(snap.get("patient_anxiety", getattr(agent_state, "patient_anxiety", 0.50))),
        "family_anxiety": float(snap.get("family_anxiety", getattr(agent_state, "family_anxiety", 0.65))),
        "trust_trend": int(snap.get("trust_trend", 0)),
        "anxiety_trend": int(snap.get("anxiety_trend", 0)),
        "refuse": bool(snap.get("refuse", False)),
        "leave_reason": snap.get("leave_reason"),
        "terminal_reason": getattr(session, "terminal_reason", None),
    }


@router.get("/list", response_model=List[CaseSummaryDTO])
async def list_cases():
    """Return the bundled chest-pain case and the five catalogue cases."""
    return [CaseSummaryDTO(**item) for item in get_case_repository().summaries()]


@router.get("/detail/{case_id}")
async def get_case_detail(case_id: str):
    """Get full case briefing and patient profile."""
    data = get_case_repository().get(case_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Case not found")

    return {
        "case_id": data.get("case_id"),
        "title": data.get("title"),
        "chief_complaint": data.get("chief_complaint"),
        "patient_profile": data.get("patient_profile"),
        "initial_vitals": data.get("initial_vitals"),
        "difficulty": data.get("difficulty"),
        "category": data.get("category"),
        "game_extensions": data.get("game_extensions", {}),
    }


@router.post("/start")
async def start_case(req: StartCaseRequest):
    """Initialize or reset an active simulation training session."""
    if get_case_repository().get(req.case_id) is None:
        raise HTTPException(status_code=404, detail=f"病例不存在：{req.case_id}")
    store = SessionStore.get_instance()
    session = store.create_session(req.case_id, req.student_name)

    return {
        "status": "success",
        "message": "抢救室实训已就绪，患者已接入多参数心电监护仪，计时开始！",
        "session_id": session.session_id,
        "case_id": session.case_id,
        "student_name": session.student_name,
        "initial_vitals": session.physiology_engine.get_current_vitals(),
        "patient_state": session.physiology_engine.get_state().value
    }


@router.get("/session/status", response_model=SessionStateResponse)
async def get_session_status(session_id: str = Query(default="default")):
    """Get active training session snapshot."""
    store = SessionStore.get_instance()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Active session not found")

    vitals = session.physiology_engine.get_current_vitals()
    state = session.physiology_engine.get_state()
    emotion = emotion_view(session)

    return SessionStateResponse(
        session_id=session.session_id,
        case_id=session.case_id,
        student_name=session.student_name,
        is_active=session.is_active,
        elapsed_virtual_seconds=session.virtual_seconds,
        patient_state=state,
        vitals=vitals,
        agent_trust=round(emotion["patient_trust"], 2),
        # 历史遗留的误导性字段名：agent_anxiety 装的其实是「家属焦虑」
        # （family_anxiety），不是 Agent 自身的焦虑。兼容保留，勿再新增
        # 依赖；新前端请读 patient_anxiety / family_anxiety。
        agent_anxiety=round(emotion["family_anxiety"], 2),
        patient_anxiety=round(emotion["patient_anxiety"], 2),
        family_anxiety=round(emotion["family_anxiety"], 2),
        trust_trend=emotion["trust_trend"],
        anxiety_trend=emotion["anxiety_trend"],
        refuse=emotion["refuse"],
        terminal_reason=emotion["terminal_reason"],
        history_actions_count=len(session.action_history),
        active_alarms=session.agent_engine.state.active_critical_alarms
    )


@router.get("/session/{session_id}/workspace")
def workspace(session_id: str):
    """Restore both new UI screens from the same persistent session."""
    session = SessionStore.get_instance().get_session(session_id)
    if not session:
        raise HTTPException(404, "实训会话不存在，请重新开始接诊")
    assessment = session.final_assessment or session.ged_evaluator.evaluate(session.action_history, "", include_feedback=False)
    profile = session.case_data.get("patient_profile") or {}
    extensions = session.case_data.get("game_extensions") or {}
    emotion = emotion_view(session)
    return {
        "session_id": session.session_id, "case_id": session.case_id,
        "student_name": session.student_name, "is_active": session.is_active,
        "is_finished": session.is_finished, "virtual_seconds": session.virtual_seconds,
        "vitals": session.physiology_engine.get_current_vitals(),
        "patient_state": session.physiology_engine.get_state(),
        # trust_level 沿用旧字段名，值改由 EmotionEngine 提供（契约 02 §1.2：
        # 纯时间流逝要能把信任压到 0.15，而 agent_engine 没有时间项）。
        "trust_level": emotion["patient_trust"],
        # 情绪通道（契约 05 §4）：新字段只增不改。
        # family_anxiety 与 status 端点的 agent_anxiety 同值（历史字段名误导，
        # 它装的一直是家属焦虑）。
        "patient_anxiety": emotion["patient_anxiety"],
        "family_anxiety": emotion["family_anxiety"],
        "trust_trend": emotion["trust_trend"],
        "anxiety_trend": emotion["anxiety_trend"],
        "refuse": emotion["refuse"],
        "terminal_reason": emotion["terminal_reason"],
        "rupture_countdown": session.physiology_engine.get_rupture_countdown(),
        "dialogues": session.dialogues, "actions": session.action_history,
        "reports": [a.result for a in session.action_history if isinstance(a.result, dict)],
        "radar_scores": assessment.radar_scores,
        "assessment": session.final_assessment,
        "case_profile": {
            "case_id": session.case_data.get("case_id", session.case_id),
            "title": session.case_data.get("title", ""),
            "difficulty": session.case_data.get("difficulty", ""),
            "category": session.case_data.get("category", ""),
            "chief_complaint": session.case_data.get("chief_complaint", ""),
            "patient_profile": profile,
            "initial_vitals": session.case_data.get("initial_vitals", {}),
            "core_teaching": extensions.get("core_teaching") or session.case_data.get("core_teaching", ""),
            "game_extensions": extensions,
        },
    }


@router.post("/session/{session_id}/pause")
def pause_session(session_id: str):
    store = SessionStore.get_instance()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    session.is_active = False
    store.persist(session)
    return {"success": True}
