"""LLM 输出后置校验（spec/04 《AI能力与数据契约》§3.6 三道闸）。

在线大模型返回的文本在进入对话 / 复盘之前必须过这里，命中即丢弃并回退到
离线兜底文案，同时记一条 ``llm_rejected reason=A?`` 日志，**不向 UI 报错**。

| 闸 | 规则 | 命中处理 |
|---|---|---|
| A1 长度 | 40–300 字（上限读 ``config.LLM_GUARD_MAX_CHARS``） | 丢弃 → 模板 |
| A2 禁词 | 诊断名（主动脉夹层 / Stanford / 心肌梗死 / 肺栓塞）、剂量正则 ``\\d+\\s*(mg\\|µg\\|g\\|mL\\|U)`` | 丢弃 → 模板 |
| A3 结构 | 多角色分支必须返回 ``{patient, family}`` 合法 JSON | 丢弃 → 模板 |

设计约束：
  * 不抛异常打断会话——所有失败都退化成兜底文案；
  * ``config`` 读取一律 ``getattr`` 兜底，键未落地也不影响离线模式；
  * 纯函数 + 显式参数，测试可直接断言 ``reason``。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger(__name__)

# ---- 契约默认值（config 缺键时使用） --------------------------------------
DEFAULT_MIN_CHARS = 40          # §3.6：40–300 字
DEFAULT_MAX_CHARS = 300
DEFAULT_BLOCK_DIAGNOSIS = True

# §3.6 A2 诊断名表；另附玩家可见的高危口语简称，宁可多拦。
DIAGNOSIS_TERMS: Tuple[str, ...] = (
    "主动脉夹层",
    "Stanford",
    "stanford",
    "心肌梗死",
    "肺栓塞",
    "夹层动脉瘤",
)

# §3.6 A2 剂量正则：数字 + 剂量单位。µ/μ 两种写法都收。
DOSE_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|MG|Mg|µg|μg|ug|UG|g|G|mL|ml|ML|U|IU)\b")

# 结构化输出必须带齐的键（§3.6 A3）。
STRUCTURED_KEYS: Tuple[str, ...] = ("patient", "family")


def _cfg(name: str, default: Any) -> Any:
    """config 键可能还没落地（config.py 不归本模块维护），读不到用契约默认值。"""
    return getattr(config, name, default)


def max_chars() -> int:
    value = _cfg("LLM_GUARD_MAX_CHARS", DEFAULT_MAX_CHARS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def min_chars() -> int:
    value = _cfg("LLM_GUARD_MIN_CHARS", DEFAULT_MIN_CHARS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_CHARS


def block_diagnosis() -> bool:
    return bool(_cfg("LLM_GUARD_BLOCK_DIAGNOSIS", DEFAULT_BLOCK_DIAGNOSIS))


@dataclass
class GuardResult:
    """一次校验的结果。``accepted=False`` 时 ``text`` 已是兜底文案。"""

    text: str
    accepted: bool
    reason: Optional[str] = None
    original: str = ""
    detail: str = ""

    @property
    def degraded(self) -> bool:
        return not self.accepted


def _log_rejected(reason: str, source: str, detail: str) -> None:
    logger.warning("llm_rejected reason=%s source=%s %s", reason, source, detail)


def scan_forbidden(text: str, extra_terms: Iterable[str] = ()) -> Optional[str]:
    """A2：命中诊断名或剂量正则返回 ``"A2"``，否则 ``None``。"""
    if not block_diagnosis():
        return None
    for term in tuple(DIAGNOSIS_TERMS) + tuple(extra_terms):
        if term and term in text:
            return "A2"
    if DOSE_PATTERN.search(text):
        return "A2"
    return None


def guard_text(
    text: Optional[str],
    fallback: str,
    *,
    source: str = "llm",
    enforce_length: bool = True,
    extra_terms: Iterable[str] = (),
) -> GuardResult:
    """A1+A2：校验一段自然语言，失败返回兜底文案。

    ``enforce_length=False`` 只查禁词——离线模板文案本身就是短句，
    不能按「LLM 40 字下限」判死（§3.6 的三道闸只作用于 LLM 输出）。
    """
    raw = (text or "").strip()
    reason: Optional[str] = None
    detail = ""

    if not raw:
        reason, detail = "A3", "empty"
    elif enforce_length and len(raw) > max_chars():
        # A1 只守上限：临床对话里合法的短句（「呃啊……大夫……」）不该被长度下限判死，
        # 而超长文本会把整段生成结果灌进对话、破坏气泡布局。
        reason, detail = "A1", f"len={len(raw)}>{max_chars()}"
    else:
        reason = scan_forbidden(raw, extra_terms)
        if reason:
            detail = "forbidden_term"
        elif len(raw) > max_chars():
            # 禁词关闭时仍守住硬上限，避免超长文本灌进对话。
            reason, detail = "A1", f"len={len(raw)}>{max_chars()}"

    if reason:
        _log_rejected(reason, source, detail)
        return GuardResult(text=fallback, accepted=False, reason=reason, original=raw, detail=detail)
    return GuardResult(text=raw, accepted=True, original=raw)


def parse_structured(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """A3：解析 ``{patient, family}``；容忍 ```json 代码围栏，其余一律 None。

    只强制 ``patient``：家属可以在这一轮不说话（``family`` 缺省视为空串），
    强制两个字段都在会把「患者单独回一句」的合法生成结果判成坏 JSON。
    """
    body = (raw or "").strip()
    if not body:
        return None
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    patient = data.get("patient")
    if not isinstance(patient, str) or not patient.strip():
        return None
    if data.get("family") is None:
        data["family"] = ""
    elif not isinstance(data.get("family"), str):
        return None
    return data


def guard_structured(
    raw: Optional[str],
    fallback: Dict[str, str],
    *,
    source: str = "llm",
    extra_terms: Iterable[str] = (),
) -> Tuple[Dict[str, str], Optional[str]]:
    """A3（含 A2 二次校验）：坏 JSON 或字段含禁词 → 回退离线文案。"""
    parsed = parse_structured(raw)
    if parsed is None:
        _log_rejected("A3", source, "invalid_json")
        return dict(fallback), "A3"
    for key in STRUCTURED_KEYS:
        if scan_forbidden(parsed[key], extra_terms):
            _log_rejected("A2", source, f"field={key}")
            return dict(fallback), "A2"
    return {key: str(parsed[key]).strip() for key in STRUCTURED_KEYS}, None


def guard_dialogues(
    messages: Sequence[Any],
    fallback: str,
    *,
    source: str = "agent_engine",
    enforce_length: bool = False,
    extra_terms: Iterable[str] = (),
) -> Tuple[List[Any], List[str]]:
    """对患者 / 家属台词逐条过闸，返回 (消息列表, 命中原因列表)。

    医生、护士、系统的台词是本地模板，不送闸（否则会误伤"阿司匹林禁忌"
    这类教学提示——A2 只约束模型生成的患者/家属口吻内容）。
    """
    reasons: List[str] = []
    for message in messages:
        speaker = str(getattr(message, "speaker", "") or "").lower()
        if speaker not in ("patient", "family"):
            continue
        result = guard_text(
            getattr(message, "text", ""),
            fallback,
            source=source,
            enforce_length=enforce_length,
            extra_terms=extra_terms,
        )
        if result.degraded:
            reasons.append(result.reason or "?")
            try:
                message.text = result.text
            except AttributeError:  # pragma: no cover - pydantic 冻结模型
                pass
    return list(messages), reasons
