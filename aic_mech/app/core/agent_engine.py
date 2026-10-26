"""Multi-Agent Doctor-Patient-Family-Nurse Collaboration Engine.

Features:
- Patient Agent: dynamic trust T_trust, pain perception, unlocked clinical history.
- Family Agent: anxiety impedance equation, interruption countdown, surgical consent.
- Nurse Agent: order execution pipeline, active critical value alarms, safety warnings.
- Dual-mode architecture: 100% offline high-fidelity simulation engine with optional online LLM connector.
"""

import time
import random
import re
from typing import Dict, Any, List, Optional, Mapping, Tuple
import httpx
from app.models.entities import AgentState, DialogueMessage, ActionType, PatientStateEnum
from app.core.llm_guard import guard_structured, guard_text, parse_structured
import config


# spec/01 §1.2: emotion_scripts 按 patient_trust 分三档（high / mid / low）
FLOAT_EPS = 1e-9
EMOTION_BAND_HIGH = float(getattr(config, "EMOTION_SCRIPT_TRUST_HIGH", 0.70))
EMOTION_BAND_MID = float(getattr(config, "EMOTION_SCRIPT_TRUST_MID", 0.30))
EMOTION_BANDS = ("high", "mid", "low")

# 情绪文案节点（病例 JSON 的 emotion_scripts 键，6 个关键节点）
EMOTION_NODE_CONSULT = "E1_CONSULT"
EMOTION_NODE_PAIN = "E2_PAIN_DETAIL"
EMOTION_NODE_PHYSICAL = "E3_PHYSICAL"
EMOTION_NODE_WAIT_LAB = "E4_WAIT_LAB"
EMOTION_NODE_RX = "E5_RX"
EMOTION_NODE_CONSENT = "E6_PRE_OUTCOME"

# ----------------------------------------------------------------------
# spec/02 §2 事件表：问诊意图三分类（切题 / 重复 / 无关）
# 引擎只负责判定并暴露，落账由 API 层按 question_kind 映射事件码。
# ----------------------------------------------------------------------
QUESTION_RELEVANT = "relevant"
QUESTION_REPEAT = "repeat"
QUESTION_IRRELEVANT = "irrelevant"
QUESTION_KINDS = (QUESTION_RELEVANT, QUESTION_REPEAT, QUESTION_IRRELEVANT)

# 判「重复」的近期窗口：与最近 N 次问诊比对（归一化后相同或互为子串）
INQUIRY_RECENT_WINDOW = 8
# 判「切题」的字符二元组重合下限：少于此数不算问到病例内容
INQUIRY_MIN_NGRAM_HITS = 2
# 问句通用虚词二元组：不承载病例信息，不进重合计数
_QUESTION_FILLER_BIGRAMS = frozenset({
    "什么", "怎么", "么样", "为什", "是不", "不是", "有没", "没有", "多少",
    "哪些", "是否", "可以", "能不", "请问", "一下", "这个", "那个",
})
# 病例里承载问诊目标的文本字段（判切题的语料来源）
_INQUIRY_CORPUS_FIELDS = (
    "chief_complaint", "present_illness", "risk_factors",
    "key_positives", "key_negatives", "inquiry_points",
    "knowledge_card", "differentials", "outcomes",
)
# inquiry_points 的引导动词：仅用于剥词头，不参与匹配
_INQUIRY_LEAD_VERBS = ("询问一下", "询问", "问一下", "了解", "评估", "确认", "核实", "请描述")
_INQUIRY_NORMALIZE_DROP = set(
    " \t\r\n\u3000，。、；：？！…—·“”‘’（）《》〈〉【】〔〕,.;:?!\"'()[]{}<>/\\|~`@#$%^&*-_+="
)
_INQUIRY_CLAUSE_SPLIT = set("，。、；：？！,.;:?!/")


def normalize_inquiry(text: Any) -> str:
    """问诊文本归一化：小写 + 去空白与标点。判重与判切题都走这一层。"""
    return "".join(ch for ch in str(text or "").lower() if ch not in _INQUIRY_NORMALIZE_DROP)


def _bigrams(normalized: str) -> set:
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)} if len(normalized) >= 2 else set()


def _split_clauses(text: str) -> List[str]:
    """按中英文标点把一句问诊要点切成短语（保留原字，不做归一化）。"""
    chunks: List[str] = []
    current = []
    for ch in str(text or ""):
        if ch in _INQUIRY_CLAUSE_SPLIT:
            if current:
                chunks.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        chunks.append("".join(current))
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _split_conjunctions(token: str) -> List[str]:
    """把「用药史与药物过敏史」这类并列词条拆成独立词条。"""
    parts: List[str] = []
    current = []
    for ch in str(token or ""):
        if ch in "与和及、":
            if current:
                parts.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _iter_case_strings(value: Any):
    """递归吐出病例 JSON 里的所有字符串（只取文本，不碰结构化数值）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_case_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_case_strings(item)


class InquiryMessages(list):
    """handle_inquiry 的返回容器：仍是 list（旧 callers 不受影响），另带判定。"""

    def __init__(self, dialogues: Optional[List[DialogueMessage]] = None, question_kind: Optional[str] = None):
        super().__init__(dialogues or [])
        self.question_kind = question_kind
        self.dialogues = self


class AgentEngine:
    """Manages the dynamic multi-agent clinical simulation."""

    def __init__(self, initial_state: Optional[AgentState] = None, case_data: Optional[Dict[str, Any]] = None):
        self.case_data: Dict[str, Any] = case_data or {}
        self.state = initial_state or AgentState(
            patient_trust=0.40,
            patient_pain=9,
            family_anxiety=0.65,
            family_interrupted_count=0,
            nurse_orders_queue=[],
            active_critical_alarms=[]
        )
        self.dialogue_history: List[DialogueMessage] = []
        self.history_actions_count: int = 0
        self.last_interaction_time: float = time.time()
        # 可注入的 EmotionEngine.snapshot()（信任度权威来源）；未注入时退回 AgentState
        self.emotion_snapshot: Optional[Dict[str, Any]] = None
        # 问诊意图分类（spec/02 §2）：最近一次判定 + 近期问句窗口 + 病例语料缓存
        self.last_question_kind: Optional[str] = None
        self._recent_inquiries: List[str] = []
        self._inquiry_corpus: Optional[Tuple[frozenset, frozenset, frozenset]] = None

    # ------------------------------------------------------------------
    # LLM 人设（D-32）：全部取病例 JSON，缺失时回落夹层原文，保证旧行为不变
    # ------------------------------------------------------------------
    def _persona_field(self, key: str, default: str = "") -> str:
        profile = self.case_data.get("patient_profile") or {}
        value = profile.get(key)
        return str(value).strip() if value not in (None, "") else default

    def _family_relation(self) -> str:
        companion = self._persona_field("family_companion")
        if companion:
            relation = companion.split("（")[0].split("(")[0].strip()
            if relation:
                return relation
        return "妻子"

    def _persona_scene(self) -> str:
        """【场景设定】整句：患者基本信息 + 主诉 + 家属陪同。"""
        name = self._persona_field("name")
        if not name:
            return (
                "急诊抢救室，重症患者孙建国（男，52岁，建筑工人），"
                "突发极剧烈胸背部撕裂样、刀劈样剧痛3小时，大汗淋漓、面色惨白、极度濒死感；"
                "旁边是极度焦虑惶恐的家属妻子李秀梅（49岁）。"
            )
        gender = self._persona_field("gender")
        age = self._persona_field("age")
        occupation = self._persona_field("occupation")
        chief = str(self.case_data.get("chief_complaint") or "").strip()
        who = f"{name}（{gender}，{age}岁，{occupation}）" if occupation else f"{name}（{gender}，{age}岁）"
        tail = "；旁边是极度焦虑惶恐的家属" + self._family_relation() + "。" if self._persona_field("family_companion") else "。"
        return f"急诊抢救室，重症患者{who}" + (f"，{chief}" if chief else "") + tail

    def _persona_history(self) -> str:
        """【隐秘病史设定】整段：既往史 + 用药史 + 过敏史。"""
        parts = [
            self._persona_field("past_history"),
            "；".join(str(x) for x in (self.case_data.get("patient_profile") or {}).get("medication_history") or [] if str(x).strip()),
            self._persona_field("allergy_history"),
        ]
        body = "".join(p if p.endswith(("。", "；")) else p + "。" for p in parts if p)
        if body:
            return body
        return (
            "孙建国有10年原发高血压史，最高曾达200/115mmHg。"
            "两周前嫌麻烦擅自停降压药，今天下午在工地搬运重水泥猛用力时突发剧痛。"
        )

    # ------------------------------------------------------------------
    # 人设（D-32 / K5）：从病例 patient_profile 生成，裸构造时回落旧文案
    # ------------------------------------------------------------------
    def _patient_role_title(self) -> str:
        """患者气泡的抬头，取病例的患者姓名/性别/年龄。"""
        profile = self.case_data.get("patient_profile") or {}
        name = str(profile.get("name") or "").strip()
        age = profile.get("age")
        gender = str(profile.get("gender") or "").strip()
        if name:
            if age and gender:
                return f"患者 {name} ({gender}，{age}岁)"
            return f"患者 {name} ({age}岁)" if age else f"患者 {name}"
        return "患者 孙建国 (52岁)"

    def _family_role_title(self) -> str:
        """家属气泡的抬头，取病例的陪同关系（family_companion 括号前的关系词）。"""
        profile = self.case_data.get("patient_profile") or {}
        companion = str(profile.get("family_companion") or "").strip()
        if companion:
            relation = companion.split("（")[0].split("(")[0].strip()
            if relation:
                return f"家属（{relation}）"
        return "家属（妻子李秀梅）"


    # ------------------------------------------------------------------
    # 情绪文案分档（spec/01 §1.2；spec/02 §3 阈值）
    # ------------------------------------------------------------------
    def sync_emotion(self, snapshot: Optional[Mapping[str, Any]]) -> None:
        """Adopt an ``EmotionEngine.snapshot()`` dict, or clear it with ``None``."""
        self.emotion_snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else None

    def current_trust(self) -> float:
        """Authoritative patient trust: injected emotion snapshot, else AgentState."""
        if self.emotion_snapshot:
            try:
                return float(self.emotion_snapshot.get("patient_trust"))
            except (TypeError, ValueError):
                pass
        return float(self.state.patient_trust)

    @staticmethod
    def resolve_emotion_band(trust: Optional[float] = None) -> str:
        """Map a trust value to ``high`` / ``mid`` / ``low`` (inclusive bounds)."""
        value = 0.0 if trust is None else float(trust)
        if value >= EMOTION_BAND_HIGH - FLOAT_EPS:
            return "high"
        if value >= EMOTION_BAND_MID - FLOAT_EPS:
            return "mid"
        return "low"

    def emotion_scripts(self) -> Dict[str, Any]:
        """Return the case's ``emotion_scripts`` mapping, or ``{}`` when unusable."""
        scripts = self.case_data.get("emotion_scripts")
        return scripts if isinstance(scripts, dict) else {}

    def has_emotion_scripts(self) -> bool:
        """Whether this case supplies at least one usable banded script."""
        for entry in self.emotion_scripts().values():
            if isinstance(entry, str) and entry.strip():
                return True
            if isinstance(entry, dict):
                if any(isinstance(v, str) and v.strip() for v in entry.values()):
                    return True
        return False

    def emotion_script(self, node: str, trust: Optional[float] = None) -> Optional[str]:
        """Banded patient line for ``node``; ``None`` when absent or malformed."""
        entry = self.emotion_scripts().get(node)
        band = self.resolve_emotion_band(self.current_trust() if trust is None else trust)
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            text = entry.get(band)
        else:
            return None
        if not isinstance(text, str):
            return None
        text = text.strip()
        return text or None

    def patient_emotion_line(self, node: str, fallback: str) -> str:
        """Banded line when available, otherwise the legacy hard-coded line."""
        return self.emotion_script(node) or fallback

    # ------------------------------------------------------------------
    # 问诊意图三分类（spec/02 §2）：切题 / 重复 / 无关
    # 判据全部从 self.case_data 推导，不使用跨病例的全局关键词表。
    # ------------------------------------------------------------------
    def _case_inquiry_corpus(self) -> Tuple[frozenset, frozenset]:
        """返回 (病例问诊词条, 病例全量文本二元组)，按需构建并缓存。

        语料全部取自 self.case_data：inquiry_points / chief_complaint /
        present_illness / risk_factors / key_positives / key_negatives /
        knowledge_card / differentials / outcomes、dialogue_scripts、
        game_extensions.dialogue_layers（该病例自己写好的问诊分支）。
        """
        if self._inquiry_corpus is not None:
            return self._inquiry_corpus

        keywords = set()
        all_texts: List[str] = []
        data = self.case_data or {}

        for field in _INQUIRY_CORPUS_FIELDS:
            if field in data:
                all_texts.extend(_iter_case_strings(data[field]))
        if "dialogue_scripts" in data:
            all_texts.extend(_iter_case_strings(data["dialogue_scripts"]))
        layers = (data.get("game_extensions") or {}).get("dialogue_layers") or {}
        if isinstance(layers, dict):
            all_texts.extend(_iter_case_strings(layers))
            inquiry = layers.get("inquiry") or {}
            if isinstance(inquiry, dict):
                # 分组名本身就是该病例的问诊维度（症状 / 诱因 / 既往史 / 伴随症状）
                keywords.update(normalize_inquiry(key) for key in inquiry.keys())

        # inquiry_points 是作者手写的问诊目标：去掉括号注释与引导动词后按标点切成词条
        for point in data.get("inquiry_points") or []:
            if not isinstance(point, str):
                continue
            text = re.sub(r"[（(][^）)]*[）)]", "", point)
            for clause in _split_clauses(text):
                token = clause
                for verb in _INQUIRY_LEAD_VERBS:
                    if token.startswith(verb):
                        token = token[len(verb):]
                        break
                for sub in _split_conjunctions(token):
                    normalized = normalize_inquiry(sub)
                    if len(normalized) >= 2:
                        keywords.add(normalized)

        all_grams = {bg for bg in _bigrams(normalize_inquiry("".join(all_texts))) if bg not in _QUESTION_FILLER_BIGRAMS}
        self._inquiry_corpus = (frozenset(keywords), frozenset(all_grams))
        return self._inquiry_corpus

    def _matches_case_inquiry(self, normalized: str) -> bool:
        """归一化问句是否问到该病例的问诊内容。

        两级判据（都从 self.case_data 推导，跨病例不共享关键词）：
        1. 命中作者写好的问诊词条（inquiry_points 切片 / dialogue_layers 分组名）；
        2. 与病例全量文本的非虚词二元组重合 ≥ INQUIRY_MIN_NGRAM_HITS。

        这里刻意偏保守：只命中 1 个非虚词二元组不算切题——「今天 / 平时 / 颜色 /
        喜欢」这类词在病例文本里同样出现，单命中会把闲聊误判成切题，反而把信任
        往上推（正是 spec/02 §2 要修的病灶）。
        """
        if not normalized:
            return False
        keywords, all_grams = self._case_inquiry_corpus()
        for token in keywords:
            if len(token) >= 2 and token in normalized:
                return True
            if len(normalized) >= 2 and normalized in token:
                return True
        grams = {bg for bg in _bigrams(normalized) if bg not in _QUESTION_FILLER_BIGRAMS}
        return len(grams & all_grams) >= INQUIRY_MIN_NGRAM_HITS

    def classify_inquiry(self, query: str) -> str:
        """把一次问诊判成 relevant / repeat / irrelevant，并记到 last_question_kind。

        - 重复：与近期问过的问句归一化后相同或互为子串（先判，重复的切题句也记重复）。
        - 切题：命中病例自带的问诊词条，或与该病例文本的二元组重合达阈值。
        - 其余：无关。
        """
        normalized = normalize_inquiry(query)
        if not normalized:
            self.last_question_kind = QUESTION_IRRELEVANT
            return self.last_question_kind

        kind = QUESTION_RELEVANT if self._matches_case_inquiry(normalized) else QUESTION_IRRELEVANT
        for previous in self._recent_inquiries:
            if normalized == previous or normalized in previous or previous in normalized:
                kind = QUESTION_REPEAT
                break

        self._recent_inquiries.append(normalized)
        if len(self._recent_inquiries) > INQUIRY_RECENT_WINDOW:
            del self._recent_inquiries[0]
        self.last_question_kind = kind
        return kind

    def handle_inquiry_classified(
        self, query: str, current_state: PatientStateEnum = PatientStateEnum.S0_STABLE
    ) -> InquiryMessages:
        """handle_inquiry 的分类版：返回列表子类，额外带 question_kind。"""
        kind = self.classify_inquiry(query)
        return InquiryMessages(self._handle_inquiry_impl(query, current_state), question_kind=kind)

    def handle_inquiry(self, query: str, current_state: PatientStateEnum = PatientStateEnum.S0_STABLE) -> List[DialogueMessage]:
        """Process clinician inquiry directed towards patient or family.

        返回值仍是 List[DialogueMessage]（InquiryMessages 子类），旧 callers 不用改；
        本次判定同时挂在 result.question_kind 与 self.last_question_kind 上。
        """
        return self.handle_inquiry_classified(query, current_state)

    def _handle_inquiry_impl(self, query: str, current_state: PatientStateEnum = PatientStateEnum.S0_STABLE) -> List[DialogueMessage]:
        """原 handle_inquiry 主体：只产出对话气泡，不参与意图判定。"""
        self.last_interaction_time = time.time()
        self.history_actions_count += 1
        q = query.strip().lower()
        now = time.time()
        messages: List[DialogueMessage] = []

        # Clinician speaking bubble
        messages.append(DialogueMessage(
            speaker="doctor",
            text=query,
            timestamp=now,
            role_title="主治医师/接诊医生",
            emotion="professional"
        ))

        if self.case_data.get("case_id") and self.case_data.get("case_id") != "case_chest_pain_001":
            return self._handle_catalogue_inquiry(query, q, now, messages)

        # Check for Patient Unconsciousness / Tamponade / Arrest
        if current_state in [PatientStateEnum.S4_CARDIAC_TAMPONADE, PatientStateEnum.S5_AORTIC_RUPTURE, PatientStateEnum.S6_CARDIAC_ARREST]:
            messages.append(DialogueMessage(
                speaker="patient",
                text="（双眼紧闭，意识丧失，对呼唤与摇晃无任何反应，口唇严重发绀）",
                timestamp=now + 0.5,
                role_title=self._patient_role_title(),
                emotion="unconscious"
            ))
            messages.append(DialogueMessage(
                speaker="family",
                text="老孙！老孙你怎么了？！医生求求你快救救他啊！怎么不说话了啊老孙！",
                timestamp=now + 1.0,
                role_title=self._family_role_title(),
                emotion="terrified"
            ))
            return messages

        # ==========================================
        # 1. 优先分支: 在线大模型多角色协同推演 (Online Multi-Agent LLM Branch)
        # ==========================================
        if config.is_online_llm_active():
            llm_messages = self._generate_multi_agent_llm_response(query)
            if llm_messages:
                return llm_messages

        # ==========================================
        # 2. 备用分支: 高保真离线多场景规则引擎
        # ==========================================
        # 1. 安慰与同理心沟通
        # Accept the wording used by the UI quick actions as well as natural
        # variations clinicians commonly type (e.g. "镇痛" instead of
        # "止痛").  These are all empathy/safety statements and should move
        # the trust state before the hidden history question is asked.
        if any(k in q for k in [
            "别紧张", "放松", "别害怕", "马上止痛", "镇痛", "止痛",
            "控制血压", "别慌", "我们全力救治", "全力救治", "信任我们"
        ]):
            self.state.patient_trust = min(1.0, self.state.patient_trust + 0.15)
            self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.15)
            messages.append(DialogueMessage(
                speaker="patient",
                text=self.patient_emotion_line(
                    EMOTION_NODE_CONSENT,
                    "好……大夫，有您这句话我就放心点了……但我胸口真的快被撕开了，求您快救救我……",
                ),
                timestamp=now + 0.8,
                role_title=self._patient_role_title(),
                emotion="relieved"
            ))
            messages.append(DialogueMessage(
                speaker="family",
                text="大夫，我们全家都听您的，只要能让我爱人止住疼，您怎么治都行！",
                timestamp=now + 1.2,
                role_title=self._family_role_title(),
                emotion="appreciative"
            ))
            return messages

        # 2. 问疼痛性质与放射 (金标准节点 N1)
        if any(k in q for k in ["撕裂", "刀割", "后背", "性质", "怎么疼", "疼痛", "哪里疼", "胸口"]):
            messages.append(DialogueMessage(
                speaker="patient",
                text=self.patient_emotion_line(
                    EMOTION_NODE_PAIN,
                    "大夫！我胸骨后面像被生生用刀劈开撕裂了一样！而且剧痛一下子窜到了后背两块肩胛骨中间！疼得我浑身冷汗直冒，喘不上气！",
                ),
                timestamp=now + 0.8,
                role_title=self._patient_role_title(),
                emotion="agony"
            ))
            return messages

        # 3. 问发病诱因 (金标准节点 N2)
        if any(k in q for k in ["重体力", "用力", "搬东西", "诱因", "怎么发作的", "情绪", "干什么时", "什么时候开始"]):
            messages.append(DialogueMessage(
                speaker="patient",
                text="今天下午在工地上……我帮工友们搬了一大包重水泥，刚一使劲猛抬，胸口突然就像‘啪’的一声撕裂炸开了！持续疼了整整3个多小时了！",
                timestamp=now + 0.8,
                role_title=self._patient_role_title(),
                emotion="groaning"
            ))
            return messages

        # 4. 问高血压病史与服药遵从度 (金标准节点 N3)
        if any(k in q for k in ["高血压", "服药", "停药", "降压药", "平时血压", "吃什么药", "既往史", "病史"]):
            # 解锁门槛须高于初始信任 0.40，否则同理心博弈机制形同虚设（文档口径：≥50 解锁）。
            if self.state.patient_trust >= 0.50:
                messages.append(DialogueMessage(
                    speaker="patient",
                    text="我……我有高血压十年了，最高量过 200/115！平时嫌吃药麻烦经常漏服……最近赶工期这两周擅自停药了……",
                    timestamp=now + 0.8,
                    role_title=self._patient_role_title(),
                    emotion="regretful"
                ))
                messages.append(DialogueMessage(
                    speaker="family",
                    text="大夫！他根本不听劝！天天熬夜加班，擅自把降压药全停了，我怎么骂他都不吃！",
                    timestamp=now + 1.3,
                    role_title=self._family_role_title(),
                    emotion="anxious"
                ))
            else:
                messages.append(DialogueMessage(
                    speaker="family",
                    text="大夫，他有高血压好多年了，经常不好好服药！大夫您快救救他，别光问这个了！",
                    timestamp=now + 0.8,
                    role_title=self._family_role_title(),
                    emotion="distressed"
                ))
            return messages

        # 5. 急诊手术与知情同意 (金标准节点 N18)
        if any(k in q for k in [
            "知情同意", "同意书", "签署", "签字", "手术", "病危",
            "病危通知", "心胸外科", "开胸", "交代病情"
        ]):
            self.state.family_anxiety = max(0.4, self.state.family_anxiety - 0.2)
            messages.append(DialogueMessage(
                speaker="family",
                text="（流着眼泪颤抖拿笔）大夫……我们全家都听您的！我们全力配合！字我们马上签，求医院心胸外科马上安排急诊手术！",
                timestamp=now + 1.0,
                role_title=self._family_role_title(),
                emotion="determined"
            ))
            return messages

        # 6. 多场景上下文离线兜底 (轮换多样化回应，杜绝复读机)
        fallbacks = [
            "大夫……我胸口和后背两块骨头中间像被刀劈开一样钻心地疼，喘气都费劲……快救救我……",
            "大夫，我全身都在冒冷汗，两只胳膊发沉发麻，您问的我全配合，快给我上点止痛针吧！",
            "大夫，我下午搬水泥用力猛了突然发作的，现在连吸气胸腔都像要炸开一样……",
            "大夫……我平时血压确实很高，这次是不是心脏或者大血管破了啊……我还撑得住吗大夫……"
        ]
        import random
        chosen_text = self.emotion_script(EMOTION_NODE_CONSULT) or random.choice(fallbacks)
        messages.append(DialogueMessage(
            speaker="patient",
            text=chosen_text,
            timestamp=now + 0.8,
            role_title=self._patient_role_title(),
            emotion="pleading"
        ))
        return messages

    def _handle_catalogue_inquiry(self, query: str, q: str, now: float, messages: List[DialogueMessage]) -> List[DialogueMessage]:
        """Use the authored dialogue layers for the five general cases."""
        data = self.case_data
        profile = data.get("patient_profile") or {}
        name = profile.get("name", "虚拟患者")
        age = profile.get("age", "")
        gender = profile.get("gender", "")
        title = self._patient_role_title()
        layers = (data.get("game_extensions") or {}).get("dialogue_layers") or {}
        scripts = data.get("dialogue_scripts") or {}

        empathy = any(k in q for k in ["别紧张", "放松", "别害怕", "安慰", "解释", "放心", "配合", "我们会", "别慌"])
        if empathy:
            self.state.patient_trust = min(1.0, self.state.patient_trust + 0.12)
            self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.10)
            text = self.emotion_script(EMOTION_NODE_CONSENT) or scripts.get("trust_high") or "谢谢医生解释，我心里踏实多了。"
            messages.append(DialogueMessage(speaker="patient", text=text, timestamp=now + 0.8, role_title=title, emotion="relieved"))
            return messages

        inquiry = layers.get("inquiry") or {}
        selected = None
        groups = [
            (["既往史", "病史", "药", "过敏"], "既往史"),
            (["诱因", "什么时候", "何时", "怎么", "原因", "开始"], "诱因"),
            (["伴随", "还有", "同时", "其他"], "伴随症状"),
            (["症状", "哪里", "疼", "发热", "咳", "喘", "呕", "泻"], "症状"),
        ]
        for keywords, preferred in groups:
            if any(k in q for k in keywords):
                selected = next((key for key in inquiry if preferred in str(key)), None)
                if selected:
                    break
        if selected is None and inquiry:
            selected = next(iter(inquiry))
        options = inquiry.get(selected, []) if selected else []
        if isinstance(options, str):
            options = [options]
        text = random.choice(options) if options else (
            self.emotion_script(EMOTION_NODE_CONSULT)
            or data.get("chief_complaint", "我现在有些不舒服，请医生帮我看看。")
        )
        self.state.patient_trust = min(1.0, self.state.patient_trust + 0.03)
        messages.append(DialogueMessage(speaker="patient", text=text, timestamp=now + 0.8, role_title=title, emotion="anxious" if self.state.patient_trust < 0.55 else "cooperative"))

        companion = str(profile.get("family_companion") or "")
        if companion and not any(k in companion for k in ["独自", "无（", "无（"]):
            messages.append(DialogueMessage(
                speaker="family", text="陪同者：医生，麻烦您再说清楚一些，我们会配合检查。",
                timestamp=now + 1.2, role_title="陪同者", emotion="anxious"
            ))
        return messages

    def notify_order_dispatched(self, action_name: str, duration_sec: int = 15) -> DialogueMessage:
        """Nurse confirms execution of medical order."""
        return DialogueMessage(
            speaker="nurse",
            text=f"【护士执行】收到医嘱：已立即执行【{action_name}】。",
            timestamp=time.time(),
            role_title="急诊抢救室 林护士",
            emotion="efficient"
        )

    def observe_action(self, action_type: ActionType, content: str, *, safe: bool = True) -> None:
        """Update trust and anxiety from observable bedside care behavior."""
        q = (content or "").lower()
        if action_type == ActionType.INQUIRY:
            if any(k in q for k in ["安慰", "别紧张", "止痛", "镇痛", "全力救治", "解释", "沟通"]):
                self.state.patient_trust = min(1.0, self.state.patient_trust + 0.08)
                self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.08)
            else:
                self.state.patient_trust = min(1.0, self.state.patient_trust + 0.025)
        elif action_type == ActionType.PHYSICAL_EXAM:
            self.state.patient_trust = min(1.0, self.state.patient_trust + 0.035)
            self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.025)
        elif action_type == ActionType.LAB_ORDER:
            self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.035)
        elif action_type == ActionType.RESCUE_DISPOSITION:
            if any(k in q for k in ["吸氧", "监护", "静脉", "会诊", "手术", "知情", "签字", "抢救"]):
                self.state.patient_trust = min(1.0, self.state.patient_trust + 0.06)
                self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.07)
        elif action_type == ActionType.MEDICATION and not safe:
            self.state.patient_trust = max(0.0, self.state.patient_trust - 0.12)
            self.state.family_anxiety = min(1.0, self.state.family_anxiety + 0.15)

    def generate_nurse_warning(self, drug_name: str) -> DialogueMessage:
        """Nurse issues an emergency safety intercept alert."""
        return DialogueMessage(
            speaker="nurse",
            text=(
                f"【护士警觉拦截】王大夫！您开具了【{drug_name}】？！"
                "患者表现为突发剧烈撕裂样胸痛伴双臂血压显著不对称，未排除大血管病变！"
                "使用抗血小板/抗凝/溶栓极易诱发假腔暴发性破裂！您确定要强制推注吗？！"
            ),
            timestamp=time.time(),
            role_title="急诊抢救室 林护士 (危急拦截)",
            emotion="urgent"
        )

    def generate_tamponade_alert(self, sbp: float) -> DialogueMessage:
        """Nurse sounds fatal cardiac tamponade alert."""
        return DialogueMessage(
            speaker="nurse",
            text=(
                f"【危急抢救警报】患者突发呼之不应！血压骤降至 {int(sbp)}/30 mmHg！心音遥远！颈静脉怒张！"
                "疑似夹层破裂急性心包填塞！请立即准备抢救车与心包穿刺！"
            ),
            timestamp=time.time(),
            role_title="急诊抢救室 林护士 (紧急呼救)",
            emotion="alarm"
        )

    def handle_nurse_order(self, action_type: ActionType, order_desc: str) -> Dict[str, Any]:
        """Process nurse order execution status."""
        warning = None
        desc_lower = order_desc.lower()
        if action_type == ActionType.MEDICATION:
            from config import LETHAL_MEDICATION_KEYWORDS
            if any(k in desc_lower for k in LETHAL_MEDICATION_KEYWORDS):
                warning = "【护士紧急警示拦截】王医生，患者双臂血压相差巨大且剧烈撕裂样胸背痛，高度怀疑主动脉夹层！指南严禁使用抗栓药物，该医嘱将诱发夹层破裂大出血！请再次确认！"
                self.state.active_critical_alarms.append(warning)

        msg = self.notify_order_dispatched(f"{action_type.value}: {order_desc}")
        return {
            "status": "医嘱已下达 (dispatched)",
            "order_desc": order_desc,
            "nurse_response": msg.text,
            "warning": warning,
            "timestamp": time.time()
        }

    def check_spontaneous_events(self, elapsed_seconds: float, current_state: PatientStateEnum) -> List[DialogueMessage]:
        """Check for time-triggered family interruptions or nurse alarms."""
        messages: List[DialogueMessage] = []
        now = time.time()

        # Family interruption if silence / inactivity > 40s
        if now - self.last_interaction_time > 40.0 and self.state.family_interrupted_count < 2:
            self.state.family_interrupted_count += 1
            self.last_interaction_time = now

            family_text = "医生！怎么停下了？！我爱人脸色煞白一身虚汗，你们还要商量多久？！电视上说胸痛要嚼阿司匹林，到底给不给他吃啊？！"
            # 若在线大模型已激活，可动态生成家属突发催促
            if config.is_online_llm_active():
                dynamic_prompt = f"请扮演急诊抢救室极度焦急的{self._family_relation()}，看到抢救暂停了40秒，说一句催促医生救救患者的简短话（不超过40字，口语、急切恐慌）。"
                dynamic_text = self._generate_llm_response(dynamic_prompt)
                if dynamic_text:
                    family_text = f"{dynamic_text.strip()}\n\n[🟢 来自在线大模型情境推演: {config.LLM_MODEL}]"

            messages.append(DialogueMessage(
                speaker="family",
                text=family_text,
                timestamp=now,
                role_title=f"{self._family_role_title()} - 焦虑催促",
                emotion="anxious"
            ))

        return messages

    def _generate_multi_agent_llm_response(self, user_query: str) -> Optional[List[DialogueMessage]]:
        """Call external LLM API to collaboratively generate patient & family dialogue dynamically."""
        import json
        import time

        provider = (config.LLM_PROVIDER or "").lower()
        if provider == "deepseek":
            api_key = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
            base_url = (config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
            model = config.LLM_MODEL or "deepseek-chat"
        elif provider == "openai":
            api_key = config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY
            base_url = (config.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model = config.LLM_MODEL or "gpt-4o-mini"
        else:
            api_key = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
            base_url = (config.OPENAI_BASE_URL or config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
            model = config.LLM_MODEL or "deepseek-chat"

        if not api_key and provider != "custom":
            return None

        if "deepseek.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"
        elif "openai.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"

        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

        system_prompt = (
            "你正在参与国家级医工交叉与临床思维虚拟仿真模拟，负责在急诊抢救室协同推演多角色交互。\n"
            f"【场景设定】：{self._persona_scene()}\n"
            f"【隐秘病史设定】：{self._persona_history()}\n"
            "【多角色协同生成规则】：\n"
            "1. 绝不允许输出套话与机械模板。必须根据医生的发言内容进行自然、真实的人性化回应。\n"
            "2. 如果医生询问身体感受（如痛感性质、部位、放射方向、发病时间）：以【患者】为主回答（口语化、痛苦呻吟断续、喘息），【家属】在旁焦急补充生活细节。\n"
            f"3. 如果医生询问既往史/诱因/吃药情况，或明确对家属讲话：以【家属{self._family_relation()}】为主回答（焦急、懊悔自责患者停药），【患者】在旁呻吟忍痛。\n"
            "4. 如果医生进行心理安慰、交代病情、知情同意或要求签字手术：【家属】情绪转为高度配合与信任感激（表态全家全力配合签字），【患者】表示放心与感激。\n"
            "5. 严禁患者或家属说出'主动脉夹层'等专业医学诊断名词！他们只是普通工人群体。\n"
            "【严格JSON返回格式】：\n"
            "{\n"
            '  "patient": "患者的回答文本（若此时无法发声可为简短呻吟）",\n'
            '  "patient_emotion": "groaning | pleading | relieved | regretful",\n'
            '  "family": "家属妻子的回答或补充文本（若无需发言可为空字符串）",\n'
            '  "family_emotion": "anxious | distressed | determined | grateful"\n'
            "}"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ],
            "max_tokens": 300,
            "temperature": 0.6
        }

        try:
            with httpx.Client(timeout=float(getattr(config, "LLM_TIMEOUT_SECONDS", 8.0))) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices:
                        raw_content = choices[0].get("message", {}).get("content", "").strip()
                        now = time.time()
                        messages: List[DialogueMessage] = []

                        # A3+A2 闸：JSON 结构坏、字段含诊断名/剂量 → 整条回退离线文案
                        # （return None 让调用方走离线脚本，不把未过检的文本灌进对话）
                        guarded, _reason = guard_structured(
                            raw_content,
                            {"patient": "", "family": ""},
                            source="agent.multi_agent",
                        )
                        if _reason is not None:
                            return None
                        parsed = guarded
                        try:
                            p_text = (parsed.get("patient") or "").strip()
                            p_emo = "groaning"
                            f_text = (parsed.get("family") or "").strip()
                            f_emo = "anxious"
                            raw_meta = parse_structured(raw_content) or {}
                            p_emo = str(raw_meta.get("patient_emotion") or p_emo)
                            f_emo = str(raw_meta.get("family_emotion") or f_emo)

                            if p_text:
                                messages.append(DialogueMessage(
                                    speaker="patient",
                                    text=f"{p_text}\n\n[🟢 来自在线大模型多角色协同: {model}]",
                                    timestamp=now + 0.6,
                                    role_title=self._patient_role_title(),
                                    emotion=p_emo
                                ))
                            if f_text:
                                messages.append(DialogueMessage(
                                    speaker="family",
                                    text=f"{f_text}",
                                    timestamp=now + 1.2,
                                    role_title=self._family_role_title(),
                                    emotion=f_emo
                                ))

                            # 联动状态机
                            if "relieved" in p_emo or "grateful" in f_emo or "determined" in f_emo:
                                self.state.patient_trust = min(1.0, self.state.patient_trust + 0.15)
                                self.state.family_anxiety = max(0.2, self.state.family_anxiety - 0.15)

                            if messages:
                                return messages
                        except Exception:
                            return None
                else:
                    print(f"[AgentEngine LLM Error] HTTP {res.status_code}: {res.text[:120]}")
        except Exception as e:
            print(f"[AgentEngine LLM Exception] {e}")

        return None

    def _generate_llm_response(self, user_query: str) -> Optional[str]:
        """Call external LLM API (DeepSeek/OpenAI compatible) for realistic natural language response."""
        # 针对当前配置的 Provider 动态判定凭证与端点
        provider = (config.LLM_PROVIDER or "").lower()
        if provider == "deepseek":
            api_key = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
            base_url = (config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
            model = config.LLM_MODEL or "deepseek-chat"
        elif provider == "openai":
            api_key = config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY
            base_url = (config.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model = config.LLM_MODEL or "gpt-4o-mini"
        else:
            api_key = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
            base_url = (config.OPENAI_BASE_URL or config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
            model = config.LLM_MODEL or "deepseek-chat"

        if not api_key and provider != "custom":
            return None

        # 兼容 v1 路径拼接
        if "deepseek.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"
        elif "openai.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"
        
        if base_url.endswith("/chat/completions"):
            url = base_url
        else:
            url = f"{base_url}/chat/completions"

        system_prompt = (
            f"你正在参与国家级医科临床思维实训模拟。你扮演急诊抢救室{self._persona_field('age', '52')}岁{self._persona_field('gender', '男')}性重症患者{self._persona_field('name', '孙建国')}。\n"
            f"【当前病理特征】{self.case_data.get('chief_complaint') or '你突发极其剧烈的胸背部撕裂样、刀劈样剧痛3小时，伴大汗淋漓、胸闷气短，极度痛苦恐慌。'}\n"
            f"【隐秘病史设定】{self._persona_history()}\n"
            "【回答规则】\n"
            "1. 严格使用第一人称患者口吻，口语化、痛苦呻吟断续、真实反映剧痛濒死感，每次回答带拟声词或粗重呼吸（如‘呃啊……’、‘咳……’）。\n"
            "2. 每次回答控制在45~90字以内，重点回答医生的问题，不要长篇大论。\n"
            "3. 严禁自行说出‘主动脉夹层’等专业医学诊断名词！你只是个普通工地工人。\n"
            "4. 真实回答病情细节：比如后背剧痛、两周前擅自停药、搬重水泥用力诱发等。"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ],
            "max_tokens": 160,
            "temperature": 0.7
        }

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices:
                        raw_text = choices[0].get("message", {}).get("content", "").strip()
                        # A1+A2 闸：单声部患者台词也要过检（诊断名/剂量/超长一律回退离线文案）
                        guarded = guard_text(raw_text, "", source="agent.single_voice")
                        if not guarded.accepted:
                            return None
                        return guarded.text
                else:
                    print(f"[AgentEngine LLM Error] HTTP {res.status_code}: {res.text[:120]}")
        except Exception as e:
            print(f"[AgentEngine LLM Exception] {e}")
            return None
        return None
