"""OSCE Clinical Reasoning Assessment and GED Evaluation REST Routes."""

from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Query
from app.models.schemas import EvaluationRequest, EvaluationResponse
from app.core.session_manager import SessionStore
from app.core.ged_evaluator import (
    OutcomeConditionError,
    knowledge_card_of,
    resolve_session_outcome,
)
import json
import config

router = APIRouter(prefix="/api/eval", tags=["Clinical Assessment & Evaluation"])


def _outcome_payload(session) -> Dict[str, Any]:
    """结案报告新增的两块内容：命中的结局 + 病例自带知识卡。

    病例 condition 写错时（未知变量/语法错误）不能让整个结案 500：打印明确
    告警并把 outcome 置空，评估分与旧字段照常返回。
    """
    case_data = getattr(session, "case_data", {}) or {}
    payload: Dict[str, Any] = {
        "outcome": None,
        "knowledge_card": knowledge_card_of(case_data),
    }
    try:
        outcome = resolve_session_outcome(session)
    except OutcomeConditionError as exc:
        # 纯 ASCII 前缀：run_all_tests 的 Tee 会把 stdout 再写给 GBK 控制台，
        # emoji 会抛 UnicodeEncodeError 把结案请求打成 500。
        print(f"[Evaluation] 病例结局条件求值失败，已跳过 outcome：{exc}")
        return payload
    if outcome:
        payload["outcome"] = {
            "code": outcome.get("code"),
            "label": outcome.get("label"),
            "title": outcome.get("title"),
            "description": outcome.get("narration") or outcome.get("description") or "",
            "narration": outcome.get("narration"),
            "patient_line": outcome.get("patient_line"),
            "family_line": outcome.get("family_line"),
            "review_items": outcome.get("review_items") or [],
            "next_step": outcome.get("next_step"),
            "condition": outcome.get("condition"),
        }
    return payload


def _evaluation_response(session, assessment) -> EvaluationResponse:
    """组装评测响应（旧字段原样保留，新增 outcome / knowledge_card）。"""
    extra = _outcome_payload(session)
    return EvaluationResponse(
        session_id=session.session_id,
        student_name=session.student_name,
        total_score=assessment.total_score,
        ged_distance=assessment.ged_distance,
        topological_similarity=assessment.topological_similarity,
        radar_scores=assessment.radar_scores,
        fatal_errors=assessment.fatal_errors,
        clinical_feedback=assessment.clinical_feedback,
        action_sequence_summary=assessment.action_sequence_summary,
        comparison_dag=assessment.comparison_dag,
        outcome=extra["outcome"],
        knowledge_card=extra["knowledge_card"],
    )


@router.post("/submit", response_model=EvaluationResponse)
def submit_case_evaluation(req: EvaluationRequest):
    """Finalize clinical case, trigger GED topological evaluation, and return OSCE radar report."""
    store = SessionStore.get_instance()
    session = store.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Execute NetworkX Graph Edit Distance evaluation
    assessment = session.ged_evaluator.evaluate(
        action_records=session.action_history,
        final_diagnosis=req.final_diagnosis
    )
    session.final_assessment = assessment
    session.is_finished = True
    session.is_active = False
    store.persist(session)

    return _evaluation_response(session, assessment)


@router.get("/report/{session_id}", response_model=EvaluationResponse)
def get_evaluation_report(session_id: str):
    """Retrieve existing evaluation report for session."""
    store = SessionStore.get_instance()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.final_assessment:
        # If not evaluated yet, generate on-the-fly
        assessment = session.ged_evaluator.evaluate(session.action_history, "")
        session.final_assessment = assessment
        store.persist(session)

    assessment = session.final_assessment
    return _evaluation_response(session, assessment)


@router.get("/golden_dag")
async def get_expert_golden_dag():
    """Retrieve the raw 18-node, 22-edge Expert Golden Standard DAG definition."""
    path = config.DATA_DIR / "expert_golden_dag.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Golden DAG file not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
