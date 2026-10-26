"""P3 验收：6 个病例 × 3 个场景，走真实 HTTP 接口核对结局判定。

用法（仓库根目录）：
    python spec/verify_outcomes.py

只读校验：不修改任何病例数据；全部断言通过时 exit 0，否则 exit 1 并列出差异。

场景定义
--------
瞬时（instant）：开始接诊后立刻结案，零动作、零时间 → 命中结局不得是 A
                 （直接对应「t=0 零操作白送 A」这个缺陷）。
空转（idle）   ：开始接诊后只用服务器时钟 ``SessionStore.tick_all()`` 推进虚拟
                 时间，不下任何医嘱（问诊/查体/检查/用药一律不发）→ 命中结局不得是 A。
最优（best）   ：按病例说明书推荐顺序执行关键处置后再推进时间 → 命中结局必须是 A。
最优久候（long）：最优处置完全相同，但结案前把虚拟时间推到 400 秒 → 命中结局仍必须是 A。
                 这一条锁死「处置正确、只是等久了就判延误」的信任衰减窗口
                 （守门条件用与时间无关的 ``treated``，不是 ``trust > 0.40``）。

时间推进为什么用 tick_all 而不是 HTTP：``/api/action/execute`` 不推进虚拟时钟，
服务器自身的时钟就是 lifespan 里每秒一次的 ``store.tick_all()``（见
``app/server.py:26-30``），本脚本直接驱动同一个函数，等价于「只等时间」。
动作本身仍然全部走 HTTP 接口，结局判定也只读 ``/api/eval/submit`` 的返回。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
MECH = ROOT / "aic_mech"
sys.path.insert(0, str(MECH))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.ged_evaluator import build_outcome_context  # noqa: E402
from app.core.session_manager import SessionStore  # noqa: E402
from app.server import create_app  # noqa: E402

DATA_DIR = MECH / "app" / "data"
CASES_DIR = DATA_DIR / "cases"

# 动作元组：(action_type, content, params)
Action = Tuple[str, str, Dict[str, Any]]

# 「最优」场景：按各病例说明书 / prescription_rules 的推荐顺序。
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "case_chest_pain_001": {
        "source": str(DATA_DIR / "case_chest_pain.json"),
        "diagnosis": "Stanford A型主动脉夹层",
        "ticks": 400,
        "best": [
            ("medication", "盐酸艾司洛尔注射液", {"dose": 50, "unit": "mg"}),
            ("medication", "注射用硝普钠", {"dose": 50, "unit": "mg"}),
        ],
    },
    "case_gam_001_common_cold": {
        "source": str(CASES_DIR / "case_gam_001_common_cold.json"),
        "diagnosis": "急性上呼吸道感染（病毒性感冒）",
        "ticks": 60,
        "best": [
            ("medication", "对乙酰氨基酚片", {"dose": 0.5, "unit": "g"}),
            ("medication", "0.9% 氯化钠溶液（含漱）", {"dose": 200, "unit": "mL"}),
            ("lab_order", "急诊血常规 (CBC)", {}),
        ],
    },
    "case_gam_002_gastroenteritis": {
        "source": str(CASES_DIR / "case_gam_002_gastroenteritis.json"),
        "diagnosis": "急性感染性胃肠炎",
        "ticks": 60,
        "best": [
            ("medication", "口服补液盐Ⅲ", {"dose": 1, "unit": "袋"}),
            ("medication", "蒙脱石散", {"dose": 3, "unit": "g"}),
        ],
    },
    "case_gam_003_asthma": {
        "source": str(CASES_DIR / "case_gam_003_asthma.json"),
        "diagnosis": "支气管哮喘急性发作",
        "ticks": 60,
        "best": [
            ("medication", "硫酸沙丁胺醇气雾剂", {"dose": 200, "unit": "µg"}),
            ("medication", "吸入用布地奈德混悬液", {"dose": 1, "unit": "mg"}),
        ],
    },
    "case_gam_004_hypoglycemia": {
        "source": str(CASES_DIR / "case_gam_004_hypoglycemia.json"),
        "diagnosis": "低血糖症",
        "ticks": 60,
        "best": [
            ("medication", "葡萄糖片（15 g）", {"dose": 15, "unit": "g"}),
            ("lab_order", "指尖血糖 (Fingerstick Glucose)", {}),
        ],
    },
    "case_gam_005_appendicitis": {
        "source": str(CASES_DIR / "case_gam_005_appendicitis.json"),
        "diagnosis": "急性阑尾炎",
        "ticks": 60,
        "best": [
            ("medication", "注射用头孢唑林钠", {"dose": 2, "unit": "g"}),
            ("medication", "乳酸林格液", {"dose": 500, "unit": "mL"}),
        ],
    },
}

_A_NODE_RE = re.compile(r"has\('(N\d+)'\)")

# 「最优久候」场景统一推进秒数：足够让 trust 从 0.44 衰减到地板 0.15。
LONG_WAIT_SECONDS = 400


def _load_case(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _start(client: TestClient, case_id: str) -> str:
    response = client.post(
        "/api/case/start", json={"case_id": case_id, "student_name": "结局验收"}
    )
    assert response.status_code == 200, f"{case_id} 启动失败：{response.status_code} {response.text[:200]}"
    return response.json()["session_id"]


def _act(client: TestClient, session_id: str, action_type: str, content: str, params: Dict[str, Any]) -> None:
    response = client.post(
        "/api/action/execute",
        json={"session_id": session_id, "action_type": action_type, "content": content, "params": params or {}},
    )
    assert response.status_code == 200, f"动作失败：{content} → {response.status_code} {response.text[:200]}"


def _tick(seconds: int) -> None:
    """推进虚拟时间：等价于服务器每秒一次的 clock()，但不下任何医嘱。"""
    store = SessionStore.get_instance()
    for _ in range(seconds):
        store.tick_all()


def _submit(client: TestClient, session_id: str, diagnosis: str) -> Dict[str, Any]:
    response = client.post("/api/eval/submit", json={"session_id": session_id, "final_diagnosis": diagnosis})
    assert response.status_code == 200, f"结案失败：{response.status_code} {response.text[:200]}"
    return response.json()


def _run(client: TestClient, case_id: str, spec: Dict[str, Any], best: bool,
         ticks: int | None = None) -> Dict[str, Any]:
    session_id = _start(client, case_id)
    if best:
        for action_type, content, params in spec["best"]:
            _act(client, session_id, action_type, content, params)
    _tick(int(spec["ticks"] if ticks is None else ticks))

    session = SessionStore.get_instance().get_session(session_id)
    context = build_outcome_context(session)
    payload = _submit(client, session_id, spec["diagnosis"])
    outcome = payload.get("outcome") or {}
    case_data = _load_case(spec["source"])
    a_condition = str((case_data.get("outcomes") or {}).get("A", {}).get("condition") or "")
    required_nodes = _A_NODE_RE.findall(a_condition)
    have_nodes = [str(n) for n in context.get("nodes") or []]
    missing = [n for n in required_nodes if n not in have_nodes]
    return {
        "code": outcome.get("code"),
        "label": outcome.get("label"),
        "condition": outcome.get("condition"),
        "nodes": have_nodes,
        "required_nodes": required_nodes,
        "missing_nodes": missing,
        "vitals": {k: context.get(k) for k in ("HR", "SBP", "DBP", "SpO2", "RR", "pain_vas", "trust")},
        "treated": context.get("treated"),
        "virtual_seconds": getattr(session, "virtual_seconds", None),
        "state": str(getattr(context.get("outcomes"), "get", lambda *_: None)("physiology")),
    }


def main() -> int:
    client = TestClient(create_app())
    failures: List[str] = []
    rows: List[Dict[str, Any]] = []

    for case_id, spec in SCENARIOS.items():
        idle = _run(client, case_id, spec, best=False)
        best = _run(client, case_id, spec, best=True)
        instant = _run(client, case_id, spec, best=False, ticks=0)
        # 最优处置 + 400 秒久候：处置正确就不该因时间流逝被判延误。
        long_wait = _run(client, case_id, spec, best=True, ticks=LONG_WAIT_SECONDS)
        rows.append(
            {"case_id": case_id, "idle": idle, "best": best, "instant": instant, "long": long_wait}
        )

        if idle["code"] == "A":
            failures.append(f"{case_id} 空转场景命中 A（零处置也拿治愈结局）")
        if instant["code"] == "A":
            failures.append(f"{case_id} t=0 零操作直接命中 A（白送治愈结局）")
        if best["code"] != "A":
            failures.append(
                f"{case_id} 最优场景未命中 A：实际 {best['code']}（{best['label']}）"
                f"，缺失节点 {best['missing_nodes'] or '无'}，体征 {best['vitals']}"
            )
        if long_wait["code"] != "A":
            failures.append(
                f"{case_id} 最优处置后等待 {LONG_WAIT_SECONDS} 秒未命中 A：实际 {long_wait['code']}"
                f"（{long_wait['label']}），treated={long_wait['treated']}，体征 {long_wait['vitals']}"
            )

    header = f"{'病例':<28}{'场景':<10}{'结局':<4}{'label':<10}{'缺失节点':<16}{'HR/SBP/trust/treated':<24}"
    print("=" * len(header) * 2)
    print(header)
    print("-" * len(header) * 2)
    for row in rows:
        for scene in ("instant", "idle", "best", "long"):
            item = row[scene]
            vit = item["vitals"]
            vitals_txt = f"{vit['HR']:.1f}/{vit['SBP']:.1f}/{vit['trust']:.2f}/{item['treated']}"
            label = str(item["label"] or "")
            print(
                f"{row['case_id']:<28}{scene:<10}{str(item['code']):<4}{label:<10}"
                f"{','.join(item['missing_nodes']) or '-':<16}{vitals_txt:<24}"
            )
    print("=" * len(header) * 2)

    print("\n节点命中情况（最优场景）：")
    for row in rows:
        print(f"  {row['case_id']}  命中={row['best']['nodes']}  需要={row['best']['required_nodes']}")

    if failures:
        print("\n[FAIL] 结局验收未通过：")
        for line in failures:
            print("  - " + line)
        return 1

    print(f"\n[OK] {len(rows) * 4} 个场景全部符合预期（瞬时/空转≠A，最优=A，最优久候 400 s 仍=A）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
