# -*- coding: utf-8 -*-
"""Emotion & relationship engine for ClinMind-AI.

The public surface is frozen by ``spec/05-P0接口契约.md`` §2; the numeric
algorithm follows ``spec/02-数值与状态机规格.md`` §1-§4.

Design notes
------------
* **Virtual time only.**  The engine never reads the wall clock, so a session
  replays deterministically from (``accumulator``, ``dt`` stream) alone.
* **Two clocks.**  ``tick(dt)`` feeds an internal 5-second accumulator; every
  completed period runs exactly one ``_settle_once()`` settlement.
* **Bounds.**  The time term may only decay trust down to
  ``EMOTION_TRUST_TIME_FLOOR`` and raise anxiety up to
  ``EMOTION_ANXIETY_TIME_CEILING``.  Event deltas may cross both bounds, and
  the pinned bound is returned exactly (0.15 / 0.85 are ``==``-assertable).
* **Multipliers** are snapshotted once per settlement from the *previous*
  values, never from the values produced by the same settlement.
* **Judgement order** is fixed: immediate leave -> sustained leave -> refusal.
* ``EMOTION_UI_HINT_MIN_DELTA`` (0.005) only silences the player-facing delta
  stream; the engineering ledger records every real accumulation (C-04).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Callable, Deque, Dict, Optional

import config


# ---------------------------------------------------------------------------
# Numeric constants (fixed by spec/02; not part of the frozen config contract)
# ---------------------------------------------------------------------------
FLOAT_EPS: float = 1e-9            # EMOTION_FLOAT_EPS
UI_HINT_MIN_DELTA: float = float(config.EMOTION_UI_HINT_MIN_DELTA)
SETTLE_INTERVAL: float = float(config.EMOTION_TICK_SECONDS)
MAX_CATCHUP_STEPS: int = 60
MAX_BACKLOG_SECONDS: float = SETTLE_INTERVAL * MAX_CATCHUP_STEPS
LOG_CAP: int = 2000
NEG_INTERACTION_WINDOW: int = 3
TREND_WINDOW_SECONDS: float = float(config.EMOTION_TREND_WINDOW_SECONDS)

TRUST_TIME_FLOOR: float = float(config.EMOTION_TRUST_TIME_FLOOR)
ANXIETY_TIME_CEILING: float = float(config.EMOTION_ANXIETY_TIME_CEILING)
TRUST_TIME_DELTA: float = float(config.EMOTION_TRUST_TIME_DELTA)
PATIENT_ANXIETY_TIME_DELTA: float = float(config.EMOTION_PATIENT_ANXIETY_TIME_DELTA)
FAMILY_ANXIETY_TIME_DELTA: float = float(config.EMOTION_FAMILY_ANXIETY_TIME_DELTA)

TH_LEAVE_IMMEDIATE: float = float(config.EMOTION_LEAVE_IMMEDIATE)
TH_LEAVE_SUSTAINED: float = float(config.EMOTION_LEAVE_SUSTAINED)
DEPART_SUSTAIN_SECONDS: float = float(config.EMOTION_LEAVE_SUSTAINED_SECONDS)
TH_REFUSE: float = float(config.EMOTION_REFUSE_THRESHOLD)
TH_REFUSE_EXIT: float = 0.80       # 滞回退出阈值

# Multiplier switches (spec/02 §2.3 A)
MULT_ANXIETY_TRIGGER: float = 0.70          # 焦虑 >= 0.70 -> 信任衰减 x1.5
MULT_TRUST_TRIGGER: float = 0.70            # 信任 >= 0.70 -> 焦虑增长 x0.5
MULT_TRUST_LOW_TRIGGER: float = 0.20        # 信任 <= 0.20 -> 焦虑增长 x1.3
MULT_TRUST_DECAY_HIGH_ANXIETY: float = 1.5
MULT_TRUST_DECAY_CRITICAL: float = 2.0
MULT_TRUST_TOTAL_CAP: float = 4.5
MULT_ANXIETY_GROWTH_HIGH_TRUST: float = 0.5
MULT_ANXIETY_GROWTH_LOW_TRUST: float = 1.3

# 危重焦虑持时（spec/02 §2.3 D）
CRITICAL_ANXIETY: float = 0.95
CRITICAL_HOLD_SECONDS: float = 30.0

# 情绪状态滞回（spec/02 §4.1 线3 前置）
TH_AGITATED: float = 0.70
TH_AGITATED_EXIT: float = 0.65

# 不配合判定被抑制的危重状态
NO_REFUSE_STATES = frozenset({
    "S4_CARDIAC_TAMPONADE",
    "S5_AORTIC_RUPTURE",
    "S6_CARDIAC_ARREST",
})

TREND_TRUST = "patient_trust"
TREND_ANXIETY = "patient_anxiety"
DELTA_KEYS = ("patient_trust", "patient_anxiety", "family_anxiety")


# ---------------------------------------------------------------------------
# Floating point helpers (spec/02 §4.2: no bare equality against thresholds)
# ---------------------------------------------------------------------------
def ge(a: float, b: float) -> bool:
    """a >= b within tolerance."""
    return a >= b - FLOAT_EPS


def le(a: float, b: float) -> bool:
    """a <= b within tolerance."""
    return a <= b + FLOAT_EPS


def gt(a: float, b: float) -> bool:
    """a > b within tolerance."""
    return a > b + FLOAT_EPS


def lt(a: float, b: float) -> bool:
    """a < b within tolerance."""
    return a < b - FLOAT_EPS


def clamp01(value: float) -> float:
    """Clamp to the closed [0, 1] interval."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def bounded(value: float, delta: float,
            floor: Optional[float] = None,
            ceiling: Optional[float] = None) -> float:
    """Apply ``delta`` and pin to a hard bound when the step points at it.

    Pinning only happens when the step pushes *against* the bound, so an event
    that already moved the value past the bound is never dragged back to it.
    """
    target = value + delta
    if delta < 0.0 and floor is not None and target < floor:
        target = floor
    elif delta > 0.0 and ceiling is not None and target > ceiling:
        target = ceiling
    return clamp01(target)


# ---------------------------------------------------------------------------
# Event table (spec/05 §2 x spec/02 §1.3)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventSpec:
    """Delta triple plus optional throttle for one interaction event."""
    trust: float
    anxiety: float
    family: float
    cooldown_s: float = 0.0
    cooldown_delta_factor: float = 1.0
    max_times: Optional[int] = None


EVENT_TABLE: Dict[str, EventSpec] = {
    "RELEVANT_QUESTION": EventSpec(0.03, -0.02, -0.01),
    "REPEAT_QUESTION": EventSpec(-0.02, 0.03, 0.01),
    "IRRELEVANT_QUESTION": EventSpec(-0.03, 0.02, 0.00),
    "COMFORT": EventSpec(0.05, -0.08, -0.06,
                        cooldown_s=60.0, cooldown_delta_factor=0.5),
    "EXPLAIN_EXAM": EventSpec(0.04, -0.05, -0.03),
    "SKIP_ALLERGY": EventSpec(-0.10, 0.05, 0.04),
    "EXAM_CORRECT": EventSpec(0.02, -0.02, -0.01),
    "CORRECT_TREATMENT": EventSpec(0.04, -0.06, -0.08, max_times=3),
    "RED_LINE_MED": EventSpec(-0.12, 0.15, 0.15),
    # Secondary codes from spec/02 §1.3 kept available for the API layer.
    "IDLE": EventSpec(0.0, 0.03, 0.02, cooldown_s=30.0),
    "FAMILY_INTERRUPT": EventSpec(-0.01, 0.01, 0.02,
                                  cooldown_s=90.0, max_times=3),
    "CONSENT": EventSpec(0.02, -0.02, -0.20),
    "LLM_POSITIVE": EventSpec(0.15, -0.10, -0.15,
                              cooldown_s=45.0, max_times=3),
    "EXAM_REFUSED": EventSpec(-0.02, 0.02, 0.01),
    "UNCONSCIOUS_Q": EventSpec(0.0, 0.01, 0.01),
}

# spec/02 short names -> spec/05 frozen names
EVENT_ALIASES: Dict[str, str] = {
    "RELEVANT_Q": "RELEVANT_QUESTION",
    "REPEAT_Q": "REPEAT_QUESTION",
    "IRRELEVANT_Q": "IRRELEVANT_QUESTION",
    "REASSURE": "COMFORT",
    "CORRECT_EXAM": "EXAM_CORRECT",
    "CORRECT_TX": "CORRECT_TREATMENT",
    "LETHAL_DRUG": "RED_LINE_MED",
}

# Codes gated by config switches
_EVENT_SWITCHES = {
    "IDLE": "EMOTION_IDLE_EVENT_ENABLED",
}


@dataclass
class EmotionState:
    """Mutable emotional vector shared by the engine and the UI."""
    patient_trust: float = float(config.EMOTION_TRUST_INIT)
    patient_anxiety: float = float(config.EMOTION_PATIENT_ANXIETY_INIT)
    family_anxiety: float = float(config.EMOTION_FAMILY_ANXIETY_INIT)


class EmotionEngine:
    """5-second settlement engine for trust / anxiety / departure decisions."""

    def __init__(self, state: Optional[EmotionState] = None) -> None:
        self.state: EmotionState = state if state is not None else EmotionState()
        self._lock = threading.RLock()

        # Virtual clock, advanced only by tick(dt)
        self.virtual_seconds: float = 0.0
        self.accumulator: float = 0.0
        self.settle_count: int = 0

        # Slow-moving trackers
        self.critical_anxiety_hold_s: float = 0.0
        self.departure_watch_s: float = 0.0
        self.neg_interaction_streak: int = 0

        # Judgement flags
        self.refused: bool = False
        self.agitated: bool = False
        self.leave_reason: Optional[str] = None
        self.clinical_state: Optional[str] = None  # set by SimulationSession

        # 状态镜像回调（spec/02 §1 唯一真值）：每次结算/事件后把三真值推给
        # SimulationSession，由它写进 AgentState，避免两本账。默认不挂。
        self.on_state_change: Optional[Callable[["EmotionState"], None]] = None

        # Throttling
        self._cooldowns: Dict[str, float] = {}
        self._event_counts: Dict[str, int] = {}

        # Ledgers: engineering (everything) vs player-facing (>= 0.005)
        self._delta_log: Deque[Dict[str, Any]] = deque(maxlen=LOG_CAP)
        self._visible_log: Deque[Dict[str, Any]] = deque(maxlen=LOG_CAP)
        self._history: Deque[tuple] = deque(maxlen=600)
        self._history.append(
            (0.0, self.state.patient_trust, self.state.patient_anxiety))

    # -- introspection ------------------------------------------------------
    @property
    def delta_log(self) -> Deque[Dict[str, Any]]:
        """Engineering ledger: every non-zero accumulation, silent or not."""
        return self._delta_log

    @property
    def visible_delta_log(self) -> Deque[Dict[str, Any]]:
        """Player-facing stream: only deltas >= EMOTION_UI_HINT_MIN_DELTA."""
        return self._visible_log

    @property
    def is_terminal(self) -> bool:
        return self.leave_reason is not None

    @property
    def refused_cooperation(self) -> bool:
        return self.refused

    # -- clock --------------------------------------------------------------
    def tick(self, dt: float) -> Dict[str, float]:
        """Advance virtual time by ``dt`` seconds and settle whole periods.

        Returns the cumulative change applied during this call.  A call that
        does not complete a 5-second period returns an all-zero delta.
        """
        zero = self._zero_delta()
        with self._lock:
            try:
                step = float(dt)
            except (TypeError, ValueError):
                return zero
            if step <= 0.0 or self.is_terminal:
                return zero

            self.virtual_seconds += step
            self.accumulator += step
            if self.accumulator > MAX_BACKLOG_SECONDS:
                self.accumulator = MAX_BACKLOG_SECONDS

            settled = 0
            total = self._zero_delta()
            while ge(self.accumulator, SETTLE_INTERVAL) and settled < MAX_CATCHUP_STEPS:
                self.accumulator -= SETTLE_INTERVAL
                delta = self._settle_once()
                for key in DELTA_KEYS:
                    total[key] += delta[key]
                settled += 1
                if self.is_terminal:
                    self.accumulator = 0.0
                    break
            if settled >= MAX_CATCHUP_STEPS:
                self.accumulator = 0.0
            if settled:
                self._publish_state()
            return total

    def _settle_once(self) -> Dict[str, float]:
        """Run one 5-second settlement (spec/02 §2.3)."""
        prev_trust = self.state.patient_trust
        prev_anxiety = self.state.patient_anxiety
        prev_family = self.state.family_anxiety

        # A. snapshot multipliers from the previous values only
        mult_trust = 1.0
        if ge(prev_anxiety, MULT_ANXIETY_TRIGGER):
            mult_trust *= MULT_TRUST_DECAY_HIGH_ANXIETY
        if ge(self.critical_anxiety_hold_s, CRITICAL_HOLD_SECONDS):
            mult_trust *= MULT_TRUST_DECAY_CRITICAL
        if mult_trust > MULT_TRUST_TOTAL_CAP:
            mult_trust = MULT_TRUST_TOTAL_CAP

        mult_anxiety = 1.0
        if ge(prev_trust, MULT_TRUST_TRIGGER):
            mult_anxiety *= MULT_ANXIETY_GROWTH_HIGH_TRUST
        elif le(prev_trust, MULT_TRUST_LOW_TRIGGER):
            mult_anxiety *= MULT_ANXIETY_GROWTH_LOW_TRUST

        # B. time term only inside the open bounds
        d_trust = TRUST_TIME_DELTA * mult_trust if gt(prev_trust, TRUST_TIME_FLOOR) else 0.0
        d_anxiety = (PATIENT_ANXIETY_TIME_DELTA * mult_anxiety
                     if lt(prev_anxiety, ANXIETY_TIME_CEILING) else 0.0)
        d_family = (FAMILY_ANXIETY_TIME_DELTA
                    if lt(prev_family, ANXIETY_TIME_CEILING) else 0.0)

        # C. apply + pin
        new_trust = bounded(prev_trust, d_trust, floor=TRUST_TIME_FLOOR)
        new_anxiety = bounded(prev_anxiety, d_anxiety, ceiling=ANXIETY_TIME_CEILING)
        new_family = bounded(prev_family, d_family, ceiling=ANXIETY_TIME_CEILING)
        self.state.patient_trust = new_trust
        self.state.patient_anxiety = new_anxiety
        self.state.family_anxiety = new_family

        # D. critical anxiety hold feeds next period's trust multiplier
        if ge(new_anxiety, CRITICAL_ANXIETY):
            self.critical_anxiety_hold_s += SETTLE_INTERVAL
        else:
            self.critical_anxiety_hold_s = 0.0

        self.settle_count += 1
        self._log_delta("patient_trust", prev_trust, new_trust, "time", None, mult_trust)
        self._log_delta("patient_anxiety", prev_anxiety, new_anxiety, "time", None, mult_anxiety)
        self._log_delta("family_anxiety", prev_family, new_family, "time", None, 1.0)
        self._record_history()

        # E. thresholds
        self._update_affect()
        self._update_departure_watch()
        self._evaluate()

        return {
            "patient_trust": new_trust - prev_trust,
            "patient_anxiety": new_anxiety - prev_anxiety,
            "family_anxiety": new_family - prev_family,
        }

    # -- events -------------------------------------------------------------
    def apply_event(self, code: str, ctx: Optional[dict] = None) -> Dict[str, float]:
        """Apply one interaction event and re-run the threshold checks."""
        zero = self._zero_delta()
        with self._lock:
            if self.is_terminal:
                return zero
            name = EVENT_ALIASES.get(str(code or "").upper(), str(code or "").upper())
            spec = EVENT_TABLE.get(name)
            if spec is None:
                return zero
            switch = _EVENT_SWITCHES.get(name)
            if switch and not bool(getattr(config, switch, False)):
                return zero

            times = self._event_counts.get(name, 0)
            if spec.max_times is not None and times >= spec.max_times:
                return zero

            factor = 1.0
            ready_at = self._cooldowns.get(name)
            if ready_at is not None and lt(self.virtual_seconds, ready_at):
                factor = spec.cooldown_delta_factor
            if spec.cooldown_s > 0.0:
                self._cooldowns[name] = self.virtual_seconds + spec.cooldown_s
            self._event_counts[name] = times + 1

            prev_trust = self.state.patient_trust
            prev_anxiety = self.state.patient_anxiety
            prev_family = self.state.family_anxiety

            new_trust = clamp01(prev_trust + spec.trust * factor)
            new_anxiety = clamp01(prev_anxiety + spec.anxiety * factor)
            new_family = clamp01(prev_family + spec.family * factor)
            self.state.patient_trust = new_trust
            self.state.patient_anxiety = new_anxiety
            self.state.family_anxiety = new_family

            # negative interaction streak follows the event intent, not the
            # clamped arithmetic (a saturated value is still one more insult)
            if spec.trust < 0.0:
                self.neg_interaction_streak += 1
            elif spec.trust > 0.0:
                self.neg_interaction_streak = 0

            self._log_delta("patient_trust", prev_trust, new_trust, "event", name, factor)
            self._log_delta("patient_anxiety", prev_anxiety, new_anxiety, "event", name, factor)
            self._log_delta("family_anxiety", prev_family, new_family, "event", name, factor)
            self._record_history()

            self._update_affect()
            self._evaluate()
            self._publish_state()

            return {
                "patient_trust": new_trust - prev_trust,
                "patient_anxiety": new_anxiety - prev_anxiety,
                "family_anxiety": new_family - prev_family,
            }

    # -- thresholds ---------------------------------------------------------
    def _publish_state(self) -> None:
        """把三真值推给镜像方（SimulationSession → AgentState）。失败不影响结算。"""
        callback = self.on_state_change
        if callback is None:
            return
        try:
            callback(self.state)
        except Exception:  # pragma: no cover - 镜像失败不能拖垮情绪结算
            pass

    def _update_affect(self) -> None:
        """Agitated flag with hysteresis (0.70 in / 0.65 out)."""
        anxiety = self.state.patient_anxiety
        if self.agitated:
            if le(anxiety, TH_AGITATED_EXIT):
                self.agitated = False
        elif ge(anxiety, TH_AGITATED):
            self.agitated = True

    def _update_departure_watch(self) -> None:
        """Accumulate low-trust dwell time, reset immediately when it recovers."""
        if le(self.state.patient_trust, TH_LEAVE_SUSTAINED):
            self.departure_watch_s += SETTLE_INTERVAL
        else:
            self.departure_watch_s = 0.0

    def _physiology_blocks_refusal(self) -> bool:
        return (self.clinical_state or "") in NO_REFUSE_STATES

    def _update_refuse(self) -> None:
        """Refusal flag with hysteresis (0.90 in / 0.80 out)."""
        if self._physiology_blocks_refusal():
            self.refused = False
            return
        anxiety = self.state.patient_anxiety
        if not self.refused:
            if ge(anxiety, TH_REFUSE):
                self.refused = True
        elif le(anxiety, TH_REFUSE_EXIT):
            self.refused = False
        else:
            # hold while between 0.80 and 0.90
            pass

    def _evaluate(self) -> None:
        """Fixed order: immediate leave -> sustained leave -> refusal."""
        if self.is_terminal:
            return
        if self._should_leave_immediately():
            self._mark_leave("leave_immediate")
            return
        if self._should_leave_sustained():
            self._mark_leave("leave_sustained")
            return
        self._update_refuse()

    def _should_leave_immediately(self) -> bool:
        return (le(self.state.patient_trust, TH_LEAVE_IMMEDIATE)
                and self.neg_interaction_streak >= NEG_INTERACTION_WINDOW)

    def _should_leave_sustained(self) -> bool:
        return (le(self.state.patient_trust, TH_LEAVE_SUSTAINED)
                and ge(self.departure_watch_s, DEPART_SUSTAIN_SECONDS))

    def _mark_leave(self, reason: str) -> None:
        self.leave_reason = reason
        self.refused = False
        self.accumulator = 0.0

    # -- bookkeeping --------------------------------------------------------
    @staticmethod
    def _zero_delta() -> Dict[str, float]:
        return {key: 0.0 for key in DELTA_KEYS}

    def _log_delta(self, metric: str, before: float, after: float,
                   source: str, event: Optional[str],
                   mult: float) -> None:
        """Record a change.  Sub-0.005 steps stay in the engineering ledger."""
        delta = after - before
        if abs(delta) < FLOAT_EPS:
            return
        record = {
            "metric": metric,
            "before": round(before, 6),
            "after": round(after, 6),
            "delta": round(delta, 6),
            "source": source,
            "event": event,
            "mult": mult,
            "t": round(self.virtual_seconds, 3),
        }
        self._delta_log.append(record)
        if ge(abs(delta), UI_HINT_MIN_DELTA):
            self._visible_log.append(record)

    def _record_history(self) -> None:
        self._history.append(
            (self.virtual_seconds, self.state.patient_trust, self.state.patient_anxiety))

    def _trend(self, metric: str) -> int:
        """-1 / 0 / 1 net direction over the trailing trend window."""
        if len(self._history) < 2:
            return 0
        index = 1 if metric == TREND_TRUST else 2
        current = self.state.patient_trust if metric == TREND_TRUST else self.state.patient_anxiety
        cutoff = self.virtual_seconds - TREND_WINDOW_SECONDS
        base = None
        for entry in self._history:
            if ge(entry[0], cutoff):
                base = entry
                break
        if base is None:
            base = self._history[-1]
        diff = current - base[index]
        if gt(diff, 0.0):
            return 1
        if lt(diff, 0.0):
            return -1
        return 0

    # -- public snapshot / persistence --------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Eight-key view consumed by the API and the web UI."""
        with self._lock:
            return {
                "patient_trust": round(self.state.patient_trust, 4),
                "patient_anxiety": round(self.state.patient_anxiety, 4),
                "family_anxiety": round(self.state.family_anxiety, 4),
                "trust_trend": self._trend(TREND_TRUST),
                "anxiety_trend": self._trend(TREND_ANXIETY),
                "refuse": self.refused,
                "leave_reason": self.leave_reason,
                "refused": self.refused,
            }

    def dump(self) -> Dict[str, Any]:
        """Persistence form; ``load`` restores it byte-for-byte."""
        with self._lock:
            return {
                "version": 1,
                "state": asdict(self.state),
                "virtual_seconds": self.virtual_seconds,
                "accumulator": self.accumulator,
                "settle_count": self.settle_count,
                "critical_anxiety_hold_s": self.critical_anxiety_hold_s,
                "departure_watch_s": self.departure_watch_s,
                "neg_interaction_streak": self.neg_interaction_streak,
                "refused": self.refused,
                "agitated": self.agitated,
                "leave_reason": self.leave_reason,
                "clinical_state": self.clinical_state,
                "cooldowns": dict(self._cooldowns),
                "event_counts": dict(self._event_counts),
                # trend baseline so snapshot() survives a dump/load round trip
                "history": [[t, trust, anxiety]
                            for t, trust, anxiety in list(self._history)[-8:]],
            }

    @classmethod
    def load(cls, data: Optional[Dict[str, Any]]) -> "EmotionEngine":
        """Rebuild an engine from ``dump()``; missing/corrupt data -> initial."""
        engine = cls()
        if not isinstance(data, dict):
            return engine

        raw_state = data.get("state")
        if isinstance(raw_state, dict):
            engine.state = EmotionState(
                patient_trust=clamp01(_as_float(raw_state.get("patient_trust"),
                                                engine.state.patient_trust)),
                patient_anxiety=clamp01(_as_float(raw_state.get("patient_anxiety"),
                                                  engine.state.patient_anxiety)),
                family_anxiety=clamp01(_as_float(raw_state.get("family_anxiety"),
                                                 engine.state.family_anxiety)),
            )
        engine.virtual_seconds = max(0.0, _as_float(data.get("virtual_seconds"), 0.0))
        engine.accumulator = max(0.0, _as_float(data.get("accumulator"), 0.0))
        if engine.accumulator > MAX_BACKLOG_SECONDS:
            engine.accumulator = MAX_BACKLOG_SECONDS
        engine.settle_count = int(_as_float(data.get("settle_count"), 0.0))
        engine.critical_anxiety_hold_s = max(
            0.0, _as_float(data.get("critical_anxiety_hold_s"), 0.0))
        engine.departure_watch_s = max(0.0, _as_float(data.get("departure_watch_s"), 0.0))
        engine.neg_interaction_streak = int(
            _as_float(data.get("neg_interaction_streak"), 0.0))
        engine.refused = bool(data.get("refused", False))
        engine.agitated = bool(data.get("agitated", False))
        leave_reason = data.get("leave_reason")
        engine.leave_reason = str(leave_reason) if leave_reason else None
        clinical_state = data.get("clinical_state")
        engine.clinical_state = str(clinical_state) if clinical_state else None

        cooldowns = data.get("cooldowns")
        if isinstance(cooldowns, dict):
            engine._cooldowns = {str(k): _as_float(v, 0.0) for k, v in cooldowns.items()}
        counts = data.get("event_counts")
        if isinstance(counts, dict):
            engine._event_counts = {str(k): int(_as_float(v, 0.0)) for k, v in counts.items()}

        engine._history.clear()
        history = data.get("history")
        if isinstance(history, list):
            for item in history[-8:]:
                try:
                    t, trust, anxiety = item
                    engine._history.append(
                        (float(t), clamp01(float(trust)), clamp01(float(anxiety))))
                except (TypeError, ValueError):
                    continue
        if not engine._history or engine._history[-1][0] != engine.virtual_seconds:
            engine._history.append(
                (engine.virtual_seconds, engine.state.patient_trust,
                 engine.state.patient_anxiety))
        return engine

    # -- misc ---------------------------------------------------------------
    def sync_clinical_state(self, state: Any) -> None:
        """Mirror the physiological state so refusal can be suppressed."""
        value = getattr(state, "value", state)
        self.clinical_state = str(value) if value is not None else None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
