"""Domain Entities for ClinMind-AI System.

Represents all core clinical, physiological, multi-agent, and pedagogical concepts.
"""

import time
from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, computed_field


class DialogueMessage(BaseModel):
    """Dialogue message exchanged between agents."""
    speaker: str  # doctor, patient, family, nurse, system
    text: str
    timestamp: float = Field(default_factory=time.time)
    role_title: str = ""
    emotion: Optional[str] = None

    @computed_field
    @property
    def role(self) -> str:
        return self.speaker

    @computed_field
    @property
    def content(self) -> str:
        return self.text



class PatientStateEnum(str, Enum):
    """Hemodynamic / Pathophysiological State Enum."""
    S0_STABLE = "S0_STABLE"
    S1_HYPERTENSION_CRISIS = "S1_HYPERTENSION_CRISIS"
    S2_PHARMACO_CONTROLLED = "S2_PHARMACO_CONTROLLED"
    S3_PERICARDIAL_EFFUSION = "S3_PERICARDIAL_EFFUSION"
    S4_CARDIAC_TAMPONADE = "S4_CARDIAC_TAMPONADE"
    S5_AORTIC_RUPTURE = "S5_AORTIC_RUPTURE"
    S6_CARDIAC_ARREST = "S6_CARDIAC_ARREST"


class ActionType(str, Enum):
    """Clinical Decision Action Categories."""
    INQUIRY = "INQUIRY"
    PHYSICAL_EXAM = "PHYSICAL_EXAM"
    LAB_ORDER = "LAB_ORDER"
    MEDICATION = "MEDICATION"
    RESCUE_DISPOSITION = "RESCUE_DISPOSITION"


class VitalsVector(BaseModel):
    """Dynamic Vital Signs for Emergency Monitor."""
    hr: float = Field(default=108.0, description="Heart Rate (bpm)")
    sbp: float = Field(default=195.0, description="Systolic Blood Pressure (mmHg)")
    dbp: float = Field(default=115.0, description="Diastolic Blood Pressure (mmHg)")
    spo2: float = Field(default=96.0, description="Peripheral Oxygen Saturation (%)")
    rr: float = Field(default=22.0, description="Respiratory Rate (bpm)")
    pain_vas: int = Field(default=9, description="VAS Pain Score (0-10)")
    ecg_rhythm: str = Field(default="Sinus Tachycardia", description="Rhythm diagnosis")
    consciousness: str = Field(default="Clear, Agitated", description="Neurological status")

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class AgentState(BaseModel):
    """Multi-Agent dynamic mental & workflow state."""
    # NOTE: 已由 EmotionEngine 接管，写入不再对外生效（见 spec/02 §7 K-03，P1 清理）
    patient_trust: float = Field(default=0.40, ge=0.0, le=1.0, description="Patient compliance score")
    patient_anxiety: float = Field(default=0.50, ge=0.0, le=1.0, description="Patient anxiety level (0-1), mirrors EmotionEngine")
    patient_pain: int = Field(default=9, ge=0, le=10, description="Patient pain perception")
    # NOTE: 已由 EmotionEngine 接管，写入不再对外生效（见 spec/02 §7 K-03，P1 清理）
    family_anxiety: float = Field(default=0.65, ge=0.0, le=1.0, description="Family anxiety level")
    family_interrupted_count: int = Field(default=0, description="Family interruption count")
    nurse_orders_queue: List[Dict[str, Any]] = Field(default_factory=list, description="Executing orders")
    active_critical_alarms: List[str] = Field(default_factory=list, description="Triggered alarms")


class ActionRecord(BaseModel):
    """Log record for each student clinical decision."""
    action_id: str
    step_id: Optional[str] = None
    action_type: ActionType
    content: str
    params: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float
    virtual_time_seconds: int = 0
    result: Any = None
    is_contraindicated: bool = False
    warning_message: Optional[str] = None


class GoldenDAGNode(BaseModel):
    """Single clinical decision node in the Golden Standard DAG."""
    id: str
    action_type: ActionType
    description: str
    category: str
    severity_weight: float = 1.0  # Weight for GED cost
    critical: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class GoldenDAGEdge(BaseModel):
    """Prerequisite / causality relation edge in the Golden Standard DAG."""
    from_node: str
    to_node: str
    relation: str = "prerequisite"  # e.g., prerequisite, temporal, safety_gate
    weight: float = 1.0


class OSCEAssessment(BaseModel):
    """Comprehensive OSCE Evaluation Report Output."""
    total_score: float = Field(ge=0.0, le=100.0)
    ged_distance: float
    topological_similarity: float
    radar_scores: Dict[str, float] = Field(
        default_factory=lambda: {
            "critical_first_aid": 0.0,
            "diagnostic_accuracy": 0.0,
            "differential_thinking": 0.0,
            "safety_awareness": 0.0,
            "physician_patient_comm": 0.0,
            "clinical_efficiency": 0.0
        }
    )
    fatal_errors: List[str] = Field(default_factory=list)
    clinical_feedback: List[str] = Field(default_factory=list)
    action_sequence_summary: List[Dict[str, Any]] = Field(default_factory=list)
    comparison_dag: Dict[str, Any] = Field(default_factory=dict)
