"""Case catalogue and loader shared by the API and simulation engines.

The original project shipped a small catalogue of additional teaching cases,
but the runtime only knew about the chest-pain case.  This module keeps the
catalogue data-driven while leaving the existing high-risk chest-pain engine
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


class CaseValidationError(ValueError):
    """Raised when a bundled case file violates the data contract (D-27)."""


# spec/04 §1 的顶层 24 键契约（与 spec/check_cases.py 的 TOP_LEVEL_KEYS 对齐）。
# 运行期只做「键是否存在 / outcomes 结构是否完整」的粗校验，逐字段内容由离线
# 的 spec/check_cases.py 负责——这里的目标是让坏病例在启动时就被点名。
REQUIRED_CASE_KEYS = (
    "case_id", "title", "difficulty", "difficulty_stars", "chief_complaint", "category",
    "patient_profile", "initial_vitals", "physical_exam_findings", "lab_and_imaging_reports",
    "dialogue_scripts", "hemodynamic_rules",
    "emotion_scripts", "exam_options", "lab_panels", "prescription_rules", "outcomes", "knowledge_card",
    "present_illness", "risk_factors", "key_positives", "key_negatives", "inquiry_points", "differentials",
)
# spec/04 §2：结局必须齐 A/B/C/D 四档，每档是 object
REQUIRED_OUTCOME_CODES = ("A", "B", "C", "D")


def missing_case_fields(value: Dict[str, Any]) -> List[str]:
    """列出病例缺了哪些 §1 契约字段（含 outcomes 的四档结构）。"""
    missing: List[str] = [key for key in REQUIRED_CASE_KEYS if key not in value]
    if "outcomes" in missing:
        return missing
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, dict):
        missing.append("outcomes(A/B/C/D)")
        return missing
    for code in REQUIRED_OUTCOME_CODES:
        if not isinstance(outcomes.get(code), dict):
            missing.append(f"outcomes.{code}")
    return missing


class CaseRepository:
    """Read-only repository for the bundled fictional teaching cases."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or config.DATA_DIR
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, Dict[str, Any]] = {}
        self._warnings: List[str] = []
        self._load_all()

    def _read_json(self, path: Path, enforce_contract: bool = True) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not value.get("case_id"):
            raise CaseValidationError(f"病例文件格式无效（缺 case_id）：{path}")
        if enforce_contract:
            missing = missing_case_fields(value)
            if missing:
                # 严格档据此拒绝启动；消息里点名缺失字段，便于作者一眼定位。
                raise CaseValidationError(f"{path.name} 缺少字段：{', '.join(missing)}")
        return value

    def _handle_invalid_case(self, path: Path, exc: Exception) -> None:
        """坏病例的处理：严格档拒绝启动，默认档跳过并留痕（D-27）。"""
        message = f"病例文件被拒绝：{path} -> {type(exc).__name__}: {exc}"
        if getattr(config, "CASE_VALIDATION_STRICT", False):
            raise CaseValidationError(message) from exc
        self._warnings.append(message)
        print(f"[CaseRepository] {message}")

    @property
    def warnings(self) -> List[str]:
        """被跳过的病例文件（默认档取证用，只读）。"""
        return list(self._warnings)

    def _load_all(self) -> None:
        index_path = self.data_dir / "cases" / "index.json"
        if index_path.exists():
            try:
                with index_path.open("r", encoding="utf-8-sig") as handle:
                    index_data = json.load(handle)
                self._index = {str(item["case_id"]): item for item in index_data.get("patients", []) if item.get("case_id")}
            except (OSError, ValueError, json.JSONDecodeError):
                self._index = {}
        paths = [self.data_dir / "case_chest_pain.json"]
        paths.extend(sorted((self.data_dir / "cases").glob("*.json")))
        for path in paths:
            if not path.exists() or path.name == "index.json":
                continue
            try:
                # §1 全键契约只针对 cases/ 下的通用教学病例：data 根下的核心胸痛病例
                # 由 physiology / guardrail 引擎直接消费，且既有夹具以最小 JSON 构造它。
                data = self._read_json(path, enforce_contract=path.parent.name == "cases")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                # 默认档：坏的可选病例不能挡住核心病例启动，但必须留痕而不是静默吞掉。
                self._handle_invalid_case(path, exc)
                continue
            self._cache[str(data["case_id"])] = data

    def ids(self) -> List[str]:
        return list(self._cache)

    def get(self, case_id: str) -> Optional[Dict[str, Any]]:
        aliases = {
            "CASE_2025_AORTIC_DISSECTION": "case_chest_pain_001",
            "case_aortic_dissection": "case_chest_pain_001",
        }
        return self._cache.get(aliases.get(case_id, case_id))

    def require(self, case_id: str) -> Dict[str, Any]:
        value = self.get(case_id)
        if value is None:
            raise KeyError(case_id)
        return value

    def summary(self, data: Dict[str, Any]) -> Dict[str, str]:
        catalogue = self._index.get(str(data["case_id"]), {})
        profile = data.get("patient_profile") or {}
        title = data.get("title") or catalogue.get("disease") or data.get("disease") or data["case_id"]
        complaint = data.get("chief_complaint") or catalogue.get("one_liner") or data.get("one_liner") or ""
        category = data.get("category") or catalogue.get("disease") or data.get("disease") or "临床综合训练"
        teaching = catalogue.get("core_teaching") or (data.get("game_extensions") or {}).get("core_teaching") or data.get("core_teaching") or category
        return {
            "case_id": str(data["case_id"]),
            "title": str(title),
            "difficulty": str(data.get("difficulty") or catalogue.get("difficulty") or "教学病例"),
            "chief_complaint": str(complaint),
            "target_competency": str(teaching),
            "description": str(catalogue.get("one_liner") or f"{profile.get('name', '虚拟患者')} · {category}"),
        }

    def summaries(self) -> List[Dict[str, str]]:
        return [self.summary(self._cache[key]) for key in self.ids()]


def get_case_repository() -> CaseRepository:
    """Return a fresh repository so development can add cases without restart."""
    return CaseRepository()
