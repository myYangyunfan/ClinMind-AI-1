"""Global configuration for ClinMind-AI system."""

from pathlib import Path
import os
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent

# Auto-load .env file if present
ENV_FILE = BASE_DIR / ".env"
def _load_env_file():
    if not ENV_FILE.exists():
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"Warning loading .env: {e}")

_load_env_file()

# Server Config
HOST: str = os.getenv("HOST", "127.0.0.1")
PORT: int = int(os.getenv("PORT", "8000"))
DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

# Directories
STATIC_DIR: Path = BASE_DIR / "static"
DATA_DIR: Path = BASE_DIR / "app" / "data"
RUNTIME_DIR: Path = Path(os.getenv("CLINMIND_RUNTIME_DIR", str(BASE_DIR / "runtime")))

# LLM Configuration
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek")  # "deepseek" | "openai" | "custom" | "offline"
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")

def _optional_bool(value: str):
    value = (value or "").strip().lower()
    if value in {"true", "1", "yes", "ok", "success"}:
        return True
    if value in {"false", "0", "no", "error", "failed"}:
        return False
    return None

# Last explicit connectivity check, shown in the model settings dialog.  This
# is metadata only; it never stores or returns the API credential.
LLM_CONNECTION_OK = _optional_bool(os.getenv("LLM_CONNECTION_OK", ""))
LLM_CONNECTION_TESTED_AT: str = os.getenv("LLM_CONNECTION_TESTED_AT", "")
LLM_CONNECTION_MESSAGE: str = os.getenv("LLM_CONNECTION_MESSAGE", "尚未执行连接测试")

# Online model calls are an explicit opt-in.  A key in a local .env file is
# not enough to enable network traffic: this keeps a fresh checkout fully
# deterministic/offline and prevents an accidentally committed or inherited
# key from being used by the simulation.
ONLINE_LLM_ENABLED: bool = os.getenv("USE_ONLINE_LLM", "false").lower() in ("true", "1", "yes")

def is_online_llm_active() -> bool:
    if not ONLINE_LLM_ENABLED:
        return False
    if os.getenv("USE_OFFLINE_ONLY", "false").lower() in ("true", "1", "yes"):
        return False
    if LLM_PROVIDER == "deepseek" and DEEPSEEK_API_KEY:
        return True
    if LLM_PROVIDER == "custom" and OPENAI_BASE_URL:
        return True
    if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
        return True
    return False

USE_ONLINE_LLM: bool = is_online_llm_active()

def _write_env_file(managed: dict) -> None:
    """Rewrite .env updating only the managed LLM keys.

    Lines the LLM config does not own (HOST / PORT / DEBUG / custom entries)
    are preserved verbatim so toggling providers never silently drops them.
    """
    managed = {k.upper(): v for k, v in managed.items()}
    kept = []
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip().upper()
                    if key in managed:
                        continue
                kept.append(line.rstrip("\n"))
    lines = ["# ClinMind-AI Configuration"] + kept + [f"{k}={v}" for k, v in managed.items()]
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def save_llm_config(provider: str, api_key: str, base_url: str = "", model: str = "", *, connection_tested: bool = False, connection_ok=None, connection_message: str = ""):
    """Dynamically save LLM settings to .env and reload in-memory variables."""
    global LLM_PROVIDER, OPENAI_API_KEY, OPENAI_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, LLM_MODEL, USE_ONLINE_LLM, ONLINE_LLM_ENABLED, LLM_CONNECTION_OK, LLM_CONNECTION_TESTED_AT, LLM_CONNECTION_MESSAGE

    if provider == "offline":
        LLM_CONNECTION_OK = None
        LLM_CONNECTION_TESTED_AT = ""
        LLM_CONNECTION_MESSAGE = "离线模式，无需连接测试"
    elif connection_tested:
        LLM_CONNECTION_OK = connection_ok if connection_ok in (True, False) else None
        LLM_CONNECTION_TESTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
        LLM_CONNECTION_MESSAGE = connection_message or ("API 连接成功" if connection_ok else "API 连接失败")
    elif provider != "offline":
        LLM_CONNECTION_OK = None
        LLM_CONNECTION_TESTED_AT = ""
        LLM_CONNECTION_MESSAGE = "配置已保存，尚未执行连接测试"
    
    LLM_PROVIDER = provider
    if provider == "deepseek":
        DEEPSEEK_API_KEY = api_key
        if base_url:
            DEEPSEEK_BASE_URL = base_url
        if model:
            LLM_MODEL = model
        else:
            LLM_MODEL = "deepseek-chat"
    elif provider in ("openai", "custom"):
        OPENAI_API_KEY = api_key
        if base_url:
            OPENAI_BASE_URL = base_url
        if model:
            LLM_MODEL = model
        else:
            LLM_MODEL = "gpt-4o-mini"
    elif provider == "offline":
        os.environ["USE_OFFLINE_ONLY"] = "true"
        os.environ["USE_ONLINE_LLM"] = "false"
        ONLINE_LLM_ENABLED = False
        USE_ONLINE_LLM = False
        LLM_MODEL = "offline-rule-engine"
        try:
            _write_env_file({
                "USE_OFFLINE_ONLY": "true",
                "USE_ONLINE_LLM": "false",
                "LLM_PROVIDER": "offline",
                "LLM_MODEL": "offline-rule-engine",
                "LLM_CONNECTION_OK": "",
                "LLM_CONNECTION_TESTED_AT": "",
                "LLM_CONNECTION_MESSAGE": "离线模式，无需连接测试",
            })
        except Exception as e:
            return False, f"写入本地配置失败: {e}"
        return True, "已切换为纯离线高仿真规则引擎模式 (免Key 100%全保真)"

    os.environ["USE_OFFLINE_ONLY"] = "false"
    os.environ["USE_ONLINE_LLM"] = "true"
    ONLINE_LLM_ENABLED = True
    USE_ONLINE_LLM = is_online_llm_active()

    # Write to .env (preserving unrelated keys such as HOST/PORT/DEBUG)
    try:
        _write_env_file({
            "USE_OFFLINE_ONLY": "false",
            "USE_ONLINE_LLM": "true",
            "LLM_PROVIDER": LLM_PROVIDER,
            "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
            "DEEPSEEK_BASE_URL": DEEPSEEK_BASE_URL,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "OPENAI_BASE_URL": OPENAI_BASE_URL,
            "LLM_MODEL": LLM_MODEL,
            "LLM_CONNECTION_OK": "true" if LLM_CONNECTION_OK is True else "false" if LLM_CONNECTION_OK is False else "",
            "LLM_CONNECTION_TESTED_AT": LLM_CONNECTION_TESTED_AT,
            "LLM_CONNECTION_MESSAGE": LLM_CONNECTION_MESSAGE,
        })
    except Exception as e:
        print(f"Error persisting .env: {e}")
        return False, f"写入本地配置失败: {e}"

    mode_str = "在线大模型协同" if USE_ONLINE_LLM else "离线规则仿真"
    return True, f"配置已成功保存并生效！当前运行模式: {mode_str}"

# Simulation Engine Parameters
VIRTUAL_TICK_RATE_HZ: float = 1.0  # 1Hz physiology tick
TIME_ACCELERATION_FACTOR: float = 1.0  # 1.0 = real-time, can accelerate virtual clock

# ---------------------------------------------------------------------------
# Emotion / relationship engine parameters
# Frozen by spec/05-P0接口契约.md §1 -- names, types and defaults must match it.
# ---------------------------------------------------------------------------
# 病例数据校验（D-27）：默认档「跳过并留痕」，严格档「拒绝启动」
CASE_VALIDATION_STRICT: bool = False

# ---------------------------------------------------------------------------
EMOTION_TICK_SECONDS: float = 5.0          # 情绪结算周期（秒）
EMOTION_TRUST_INIT: float = 0.40           # 信任度初始值
EMOTION_PATIENT_ANXIETY_INIT: float = 0.50  # 患者焦虑初始值
EMOTION_FAMILY_ANXIETY_INIT: float = 0.65   # 家属焦虑初始值
EMOTION_TRUST_TIME_DELTA: float = -0.004     # 每个结算周期信任时间项
EMOTION_PATIENT_ANXIETY_TIME_DELTA: float = 0.004   # 每个结算周期患者焦虑时间项
EMOTION_FAMILY_ANXIETY_TIME_DELTA: float = 0.003    # 每个结算周期家属焦虑时间项
EMOTION_TRUST_TIME_FLOOR: float = 0.15      # 时间项信任下限
EMOTION_ANXIETY_TIME_CEILING: float = 0.85  # 时间项焦虑上限
EMOTION_IDLE_EVENT_ENABLED: bool = False    # 空转事件开关（默认关）
EMOTION_REFUSE_THRESHOLD: float = 0.90      # 拒绝配合阈值
EMOTION_LEAVE_IMMEDIATE: float = 0.05       # 立即离院阈值
EMOTION_LEAVE_SUSTAINED: float = 0.10       # 持续离院阈值
EMOTION_LEAVE_SUSTAINED_SECONDS: int = 60   # 持续离院计时（秒）
EMOTION_TREND_WINDOW_SECONDS: int = 30      # 趋势箭头窗口（秒）
EMOTION_UI_HINT_MIN_DELTA: float = 0.005    # UI/日志提示阈值（不影响引擎累计）

# ---------------------------------------------------------------------------
# 交互与 LLM 侧参数（spec/02 §4 检查拒诊、spec/04 §3 检验 TAT、spec/04 §5 LLM 守卫）。
# API 层此前用 getattr(config, ...) 兜底，这里补上真键，缺省值与兜底值一致。
# ---------------------------------------------------------------------------
EXAM_REFUSE_ANXIETY: float = 0.90           # 体查/检查被拒的患者焦虑阈值
EXAM_REFUSE_PROBABILITY: float = 0.50       # 达到阈值后被拒的概率
LAB_PENDING_TAT_THRESHOLD_SECONDS: int = 900  # TAT 超过该秒数的检验项进 pending
LLM_TIMEOUT_SECONDS: float = 8.0            # 统一 LLM 超时（秒）
LLM_GUARD_MAX_CHARS: int = 220              # LLM 输出单段最大长度
LLM_GUARD_BLOCK_DIAGNOSIS: bool = True      # 拦截专业诊断名与剂量

# ---------------------------------------------------------------------------
# P3 接线参数：情绪文案分档 + 处方红线严重度 + 血流动力学规则（spec/01 §1.2/§1.5，
# spec/02 §K-11）。全部为可注入常量，病例 JSON 的 ``hemodynamic_rules`` 优先。
# ---------------------------------------------------------------------------
EMOTION_SCRIPT_TRUST_HIGH: float = 0.70     # 文案 high 档下限（patient_trust ≥ 0.70）
EMOTION_SCRIPT_TRUST_MID: float = 0.30      # 文案 mid 档下限（0.30 ≤ trust < 0.70）

# 夹层病程时钟（原 physiology_engine 魔法数字 120/30/15 常态化为配置项）
PHYS_RUPTURE_TO_EFFUSION_S: float = 120.0   # 致死红线 → S3 心包渗血倒计时
PHYS_TAMPONADE_STAGE_S: float = 30.0        # S3 → S4 心包填塞
PHYS_RUPTURE_TO_ARREST_S: float = 15.0      # S4 → S5 破裂 / S5 → S6 骤停
PHYS_CRISIS_TIMEOUT_S: float = 300.0        # 未降压（S0）超时进入高血压危象

# 非夹层教学病例的红线致死路径：无心包填塞解剖，直接 30 s 速崩至 S6
PHYS_REDLINE_COLLAPSE_S: float = 30.0
# HARMFUL 处方累计次数达到该值即升级为致死恶化（spec/01 §1.5：累计 2 次进结局 D）
PRESCRIPTION_HARMFUL_ESCALATION_HITS: int = 2

# Fatal Red-line Medications (Contraindicated for Stanford Type A Aortic Dissection)
LETHAL_MEDICATION_KEYWORDS = [
    "aspirin", "阿司匹林",
    "ticagrelor", "替格瑞洛",
    "clopidogrel", "氯吡格雷",
    "heparin", "肝素",
    "alteplase", "阿替普酶", "rtpa", "rt-pa",
    "urokinase", "尿激酶",
    "tenecteplase", "替奈普酶",
    "bivalirudin", "比伐卢定",
    "warfarin", "华法林",
    "rivaroxaban", "利伐沙班"
]

# Targeted Anti-hypertensive & Heart Rate Lowering Medications
TARGET_DISSECTION_MEDICATIONS = [
    "esmolol", "艾司洛尔", "betaloc", "美托洛尔", "labetalol", "拉贝洛尔",
    "nitroprusside", "硝普钠", "nicardipine", "尼卡地平", "nitroglycerin", "硝酸甘油"
]
