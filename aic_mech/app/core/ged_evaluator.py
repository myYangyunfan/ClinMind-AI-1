"""Graph Edit Distance (GED) Clinical Decision Reasoning Evaluator.

Evaluates student clinical decision sequences against the Expert Golden DAG
using topological Graph Edit Distance (GED) with NetworkX. Outputs 6-dimensional
OSCE competency radar scores, cognitive bias attribution, and graph comparisons.
"""

import json
import math
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set
import networkx as nx

from app.models.entities import ActionRecord, ActionType, OSCEAssessment
import config


class GEDEvaluator:
    """Evaluates student clinical decision graph against Expert Golden Standard."""

    def __init__(self, golden_dag_path: Optional[Path] = None):
        self.golden_dag_path = golden_dag_path or (config.DATA_DIR / "expert_golden_dag.json")
        self.expert_data = self._load_golden_dag()
        self.expert_graph = self._build_expert_graph()

    def _load_golden_dag(self) -> Dict[str, Any]:
        """Load golden DAG definition."""
        if not self.golden_dag_path.exists():
            raise FileNotFoundError(f"Golden DAG definition not found at: {self.golden_dag_path}")
        with open(self.golden_dag_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_expert_graph(self) -> nx.DiGraph:
        """Construct NetworkX DiGraph for expert golden standard."""
        G = nx.DiGraph()
        for node in self.expert_data.get("nodes", []):
            G.add_node(
                node["id"],
                action_type=node["action_type"],
                name=node["name"],
                category=node["category"],
                keywords=node.get("keywords", []),
                score=node.get("score", 5.0),
                ged_penalty=node.get("ged_penalty", 10.0),
                critical=node.get("critical", False)
            )

        for edge in self.expert_data.get("edges", []):
            G.add_edge(edge["from"], edge["to"], relation=edge.get("relation", "prerequisite"), weight=edge.get("weight", 1.0))

        return G

    def match_action_to_golden_node(self, action: ActionRecord) -> Optional[str]:
        """Semantic matching of a single student action to the closest Golden DAG node."""
        text = (action.content + " " + str(action.params)).lower()
        best_node_id: Optional[str] = None
        best_score = 0

        for nid, data in self.expert_graph.nodes(data=True):
            keywords: List[str] = data.get("keywords", [])
            match_count = sum(1 for kw in keywords if kw.lower() in text)
            if match_count > best_score:
                best_score = match_count
                best_node_id = nid

        return best_node_id if best_score >= 1 else None

    def build_student_graph(self, action_records: List[ActionRecord]) -> Tuple[nx.DiGraph, Dict[str, str]]:
        """Construct the student decision DAG from temporal action records.

        Returns: (student_graph, action_id_to_node_map)
        """
        G = nx.DiGraph()
        matched_map: Dict[str, str] = {}
        ordered_nodes: List[str] = []

        unmatched_counter = 1
        for action in action_records:
            matched_id = self.match_action_to_golden_node(action)
            if matched_id:
                node_id = matched_id
                action.step_id = node_id
            else:
                node_id = f"U_{unmatched_counter}"
                unmatched_counter += 1
                action.step_id = node_id

            matched_map[action.action_id] = node_id

            if not G.has_node(node_id):
                G.add_node(
                    node_id,
                    action_type=action.action_type.value,
                    content=action.content,
                    timestamp=action.timestamp,
                    is_contraindicated=action.is_contraindicated
                )
                ordered_nodes.append(node_id)

        # Add temporal causal edges between successive decision nodes
        for i in range(len(ordered_nodes) - 1):
            u = ordered_nodes[i]
            v = ordered_nodes[i + 1]
            if not G.has_edge(u, v):
                G.add_edge(u, v, relation="temporal_sequence")

        # Fulfill expert causal precedence edges when student respects clinical ordering (u before v)
        for u, v in self.expert_graph.edges():
            if u in ordered_nodes and v in ordered_nodes:
                if ordered_nodes.index(u) < ordered_nodes.index(v):
                    if not G.has_edge(u, v):
                        G.add_edge(u, v, relation="causal_precedence")

        return G, matched_map

    def evaluate(self, action_records: List[ActionRecord], final_diagnosis: str = "", include_feedback: bool = True) -> OSCEAssessment:
        """Execute full Graph Edit Distance and OSCE 6-Dimensional competency scoring."""
        student_graph, _ = self.build_student_graph(action_records)

        # 2. Check Fatal Errors (一票否决)
        fatal_errors: List[str] = []
        fatal_occurred = False
        for action in action_records:
            if action.is_contraindicated:
                fatal_occurred = True
                fatal_errors.append(f"【致命红线失误】下达绝对禁忌用药：{action.content}，诱发假腔破裂心包填塞。")

        # 智能识别学员临床抢救思路：若执行了CTA、心外科急诊手术或降压靶向，自动认定确立夹层正确思路
        expert_node_ids = set(self.expert_graph.nodes())
        student_node_ids = set(student_graph.nodes())

        has_cta_or_surgery = ("N15" in student_node_ids) or ("N17" in student_node_ids) or ("N13" in student_node_ids and "N14" in student_node_ids)
        diag_lower = (final_diagnosis or "").lower()
        if not diag_lower and has_cta_or_surgery:
            diag_lower = "stanford a型主动脉夹层 (由救治行为智能确立)"

        if diag_lower and not any(kw in diag_lower for kw in ["主动脉夹层", "夹层", "stanford a", "a型", "aorta dissection"]):
            fatal_errors.append(f"【重大误诊】最终确诊结论【{final_diagnosis}】与金标准严重不符（漏诊 Stanford A型主动脉夹层）。")

        # 临床合理推导链自动对齐:
        if has_cta_or_surgery or any(kw in diag_lower for kw in ["主动脉夹层", "夹层", "stanford a", "a型"]):
            student_node_ids.add("N16")
            student_graph.add_node("N16", label="确诊Stanford A型主动脉夹层", category="diagnosis")

        if not fatal_occurred:
            student_node_ids.add("N12")
            student_graph.add_node("N12", label="严禁抗栓溶栓", category="safety")

        if "N8" in student_node_ids:
            student_node_ids.add("N11") # 查心电图即完成排查AMI鉴别思维
            student_graph.add_node("N11", label="排查AMI鉴别思维", category="synthesis")

        if "N5" in student_node_ids or "N1" in student_node_ids:
            student_node_ids.add("N7")  # 撕裂痛或压差即确立夹层高危假设
            student_graph.add_node("N7", label="压差结合杂音提示夹层高危", category="synthesis")

        # Refresh causal edges for implicit nodes
        for u, v in self.expert_graph.edges():
            if u in student_node_ids and v in student_node_ids and not student_graph.has_edge(u, v):
                student_graph.add_edge(u, v, relation="causal_precedence")

        matched_golden_nodes = expert_node_ids.intersection(student_node_ids)
        missing_golden_nodes = expert_node_ids - student_node_ids
        extraneous_nodes = {n for n in student_node_ids if n.startswith("U_")}

        # 3. Calculate Weighted Graph Edit Distance (GED)
        node_deletion_cost = sum(self.expert_graph.nodes[n].get("ged_penalty", 10.0) for n in missing_golden_nodes)
        node_insertion_cost = len(extraneous_nodes) * 3.0  # Shotgun testing penalty

        # Edge distance
        expert_edge_set = set(self.expert_graph.edges())
        student_edge_set = set(student_graph.edges())
        missing_edges = expert_edge_set - student_edge_set
        edge_cost = len(missing_edges) * 1.5

        total_ged = node_deletion_cost + node_insertion_cost + edge_cost

        # Max possible penalty for normalization
        max_possible_penalty = sum(d.get("ged_penalty", 10.0) for _, d in self.expert_graph.nodes(data=True)) + len(expert_edge_set) * 1.5 + 30.0
        topological_similarity = max(0.0, min(1.0, 1.0 - (total_ged / max_possible_penalty)))

        # 4. OSCE 6-Dimensional Radar Scoring (0 - 100)
        # Dim 1: 急救处置力 (Critical First Aid): N4 (急救通路), N13 (艾司洛尔), N14 (硝普钠)
        first_aid_target = {"N4", "N13", "N14"}
        first_aid_hits = len(first_aid_target.intersection(student_node_ids))
        score_first_aid = round((first_aid_hits / len(first_aid_target)) * 100.0, 1)

        # Dim 2: 诊断精准度 (Diagnostic Accuracy): N15 (CTA确诊), N16 (最终确诊)
        diag_target = {"N15", "N16"}
        diag_hits = len(diag_target.intersection(student_node_ids))
        if any(kw in diag_lower for kw in ["主动脉夹层", "夹层", "stanford a"]):
            diag_hits = min(2, diag_hits + 1)
        score_diag = round((diag_hits / len(diag_target)) * 100.0, 1)

        # Dim 3: 鉴别思维广度 (Differential Thinking): N5 (双侧血压), N6 (听诊), N8 (心电图), N9 (心肌酶), N10 (D-二聚体), N11 (排除AMI)
        diff_target = {"N5", "N6", "N8", "N9", "N10", "N11"}
        diff_hits = len(diff_target.intersection(student_node_ids))
        score_diff = round((diff_hits / len(diff_target)) * 100.0, 1)

        # Dim 4: 安全与禁忌防范 (Safety Awareness): N12
        if fatal_occurred:
            score_safety = 0.0
        else:
            score_safety = 100.0 if "N12" in student_node_ids or not fatal_errors else 85.0

        # Dim 5: 医患沟通力 (Physician-Patient Communication): N1, N2, N3, N18
        comm_target = {"N1", "N2", "N3", "N18"}
        comm_hits = len(comm_target.intersection(student_node_ids))
        score_comm = round((comm_hits / len(comm_target)) * 100.0, 1)

        # Dim 6: 临床路径效率 (Clinical Efficiency): penalized by extra actions and missing nodes
        efficiency_base = 100.0 - (len(extraneous_nodes) * 8.0) - (len(missing_golden_nodes) * 3.0)
        score_efficiency = max(20.0, min(100.0, round(efficiency_base, 1)))

        radar_scores = {
            "critical_first_aid": score_first_aid,
            "diagnostic_accuracy": score_diag,
            "differential_thinking": score_diff,
            "safety_awareness": score_safety,
            "physician_patient_comm": score_comm,
            "clinical_efficiency": score_efficiency
        }

        # 临床医学加权综合得分模型 (真实反映急救思路)
        # 核心主干权衡：急救时效(25%) + 确诊精度(25%) + 鉴别思维(20%) + 安全意识(20%) + 医患沟通(10%)
        if fatal_occurred:
            total_score = min(38.0, round(sum(radar_scores.values()) / 6.0 * 0.4, 1))
        else:
            # 基础雷达综合分
            base_calc = (
                score_first_aid * 0.25 +
                score_diag * 0.25 +
                score_diff * 0.20 +
                score_safety * 0.20 +
                score_comm * 0.10
            )
            # 黄金急救路径达成与漏项梯度扣分
            core_hits = sum(1 for k in ["N5", "N8", "N13", "N14", "N15", "N17"] if k in student_node_ids)
            if core_hits >= 4 and not fatal_errors:
                base_calc = max(base_calc, 86.0)
            elif core_hits >= 2 and not fatal_errors:
                base_calc = max(base_calc, 75.0)

            # 漏项适度扣分（非一票否决项）
            penalty = len(missing_golden_nodes) * 1.8
            total_score = round(max(20.0, min(99.0, base_calc - penalty)), 1)

        # 5. Cognitive Bias Classification and Clinical Feedback
        clinical_feedback = self._generate_feedback(
            student_node_ids=student_node_ids,
            extraneous_nodes=extraneous_nodes,
            fatal_occurred=fatal_occurred,
            fatal_errors=fatal_errors,
            action_records=action_records,
            total_score=total_score,
            radar_scores=radar_scores,
            final_diagnosis=final_diagnosis
        ) if include_feedback else []

        # 6. Action Sequence Summary for visual review
        action_summary = []
        for action in action_records:
            action_summary.append({
                "action_id": action.action_id,
                "step_id": action.step_id,
                "content": action.content,
                "type": action.action_type.value,
                "timestamp": action.timestamp,
                "matched_golden": action.step_id in expert_node_ids,
                "is_fatal": action.is_contraindicated
            })

        # 7. Comparison DAG Structure for Frontend ECharts
        comparison_dag = self._build_comparison_dag_export(student_node_ids, student_graph)

        return OSCEAssessment(
            total_score=total_score,
            ged_distance=round(total_ged, 2),
            topological_similarity=round(topological_similarity, 3),
            radar_scores=radar_scores,
            fatal_errors=fatal_errors,
            clinical_feedback=clinical_feedback,
            action_sequence_summary=action_summary,
            comparison_dag=comparison_dag
        )

    def _generate_feedback(
        self,
        student_node_ids: Set[str],
        extraneous_nodes: Set[str],
        fatal_occurred: bool,
        fatal_errors: List[str],
        action_records: Optional[List[Any]] = None,
        total_score: float = 0.0,
        radar_scores: Optional[Dict[str, float]] = None,
        final_diagnosis: str = ""
    ) -> List[str]:
        """Classify student cognitive bias and produce structured clinical guidance with LLM-enhanced review."""
        # 1. 优先分支: 在线大模型动态专家考官复盘 (Online LLM Clinical Review)
        if config.is_online_llm_active() and action_records:
            llm_feedback = self._generate_llm_clinical_review(
                action_records=action_records,
                student_node_ids=student_node_ids,
                total_score=total_score,
                radar_scores=radar_scores or {},
                fatal_occurred=fatal_occurred,
                fatal_errors=fatal_errors,
                final_diagnosis=final_diagnosis
            )
            if llm_feedback:
                return llm_feedback

        # 2. 备用分支: 高保真循证医学专家规则库 (Offline Fallback Engine)
        feedback = []

        if fatal_occurred:
            feedback.append("【典型认知偏差诊断：隧道视野-心梗锚定型 (AMI Anchoring)】")
            feedback.append("• 根因解析：在面对剧烈胸痛时未能拓宽‘胸痛三联征’鉴别广度，在缺乏双侧血压与心肌酶特异性证据前，先入为主下达抗凝/溶栓/双抗，直接导致假腔破裂心包填塞。")
            feedback.append("• 循证路径矫正：急诊胸痛首要是‘先排危、再靶向’。面对撕裂样疼痛伴双臂不对称，必须绝对封锁一切抗血小板用药！")

        if len(extraneous_nodes) >= 4:
            feedback.append("【典型认知偏差诊断：盲目撒网-无序检查型 (Shotgun Testing)】")
            feedback.append(f"• 根因解析：开具了 {len(extraneous_nodes)} 项非特异性无序检查，导致抢救黄金期被大量冗余化验稀释，未能假说驱动。")
            feedback.append("• 循证路径矫正：建立‘目标导向型快检思维’，优先利用床旁超声与双侧血压完成分流。")

        if "N5" not in student_node_ids and "N6" not in student_node_ids:
            feedback.append("【典型认知偏差诊断：体查漏项-忽视基本功型 (Physical Exam Neglect)】")
            feedback.append("• 根因解析：跳过了‘双侧上肢血压对比测量’与‘心脏主动脉瓣听诊’，严重依赖仪器化验，丢失了耗时最短、成本最低的关键临床线索。")

        if "N1" not in student_node_ids or "N3" not in student_node_ids:
            feedback.append("【典型认知偏差诊断：医患脱节-冰冷技术型 (Empathy Deficit)】")
            feedback.append("• 根因解析：问诊缺乏深度与人文温度，未深挖患者原发高血压及近期擅自停药两周的关键诱因。")

        if not feedback:
            feedback.append("【思维模式评价：逻辑缜密-指南对齐型 (Exemplary Clinical Reasoning)】")
            feedback.append("• 表现优异：规范完成了双臂血压测量与瓣膜听诊，迅速识别主动脉夹层线索，果断控制心率血压达标，安全规避溶栓禁忌，路径与国家级胸痛中心规范高度契合！")

        return feedback

    def _generate_llm_clinical_review(
        self,
        action_records: List[Any],
        student_node_ids: Set[str],
        total_score: float,
        radar_scores: Dict[str, float],
        fatal_occurred: bool,
        fatal_errors: List[str],
        final_diagnosis: str
    ) -> Optional[List[str]]:
        """Call external LLM API to produce a highly personalized, white-box OSCE clinical review."""
        import httpx
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

        # 兼容 URL 格式
        if "deepseek.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"
        elif "openai.com" in base_url and not base_url.endswith("/v1") and not base_url.endswith("/chat/completions"):
            base_url = f"{base_url}/v1"
        
        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

        # 提取学员具体操作时序
        action_names = []
        for i, a in enumerate(action_records, 1):
            name = getattr(a, "content", "") or getattr(a, "action_id", "")
            action_names.append(f"{i}. {name}")
        actions_str = "\n".join(action_names) if action_names else "无明确记录"

        system_prompt = (
            "你正在担任国家级医学虚拟仿真竞赛与急诊临床思维OSCE评测系统的特聘专家考官（中华医学会急诊医学分会/心血管胸痛中心督导专家）。\n"
            "你需要根据学员在本次【急性胸痛抢救室（52岁男性孙建国，突发撕裂样胸痛，确诊为Stanford A型主动脉夹层）】中的真实临床操作时序，"
            "输出一份专业、深刻、一针见血、白盒化的临床思维综合复盘诊断报告。\n"
            "【严禁套话与固定模板】：必须紧密围绕学员执行的具体操作、动作顺序、遗漏项及致命错误展开分析。\n"
            "【输出结构要求】：请严格按照以下三个标题分段输出（每段2~3句话，紧扣血流动力学、病理生理学与国际国内急救指南AHA/ESC）：\n"
            "1. 【AI特聘专家考官·临床决策思维白盒诊断】：针对学员本次具体操作序列的决策亮点或认知盲区进行客观分析。\n"
            "2. 【血流动力学与时序因果根因剖析】：从主动脉壁剪切力dP/dt、真假腔破裂机制、心包填塞、降压慢心率先后顺序等深度解析因果机制。\n"
            "3. 【国家胸痛中心循证规范进阶提升处方】：针对学员本次表现（如漏查体、顺序倒置或冗余检查），给出精准的下一阶段提升指导。"
        )

        user_content = (
            f"【学员考核档案】\n"
            f"- 最终评定总分: {total_score} 分 (百分制)\n"
            f"- 六维OSCE雷达指标: 急救处置力={radar_scores.get('critical_first_aid', 0)}%, "
            f"诊断精准度={radar_scores.get('diagnostic_accuracy', 0)}%, "
            f"鉴别思维广度={radar_scores.get('differential_thinking', 0)}%, "
            f"安全防范意识={radar_scores.get('safety_awareness', 0)}%, "
            f"医患沟通力={radar_scores.get('physician_patient_comm', 0)}%, "
            f"临床路径效率={radar_scores.get('clinical_efficiency', 0)}%\n"
            f"- 学员下的最终确诊结论: '{final_diagnosis or '未填写明确诊断'}'\n"
            f"- 是否发生致死性医疗差错: {'是！发生急性心包填塞/心脏停搏' if fatal_occurred else '否，未发生致命禁忌'}\n"
            f"- 致命差错详情: {', '.join(fatal_errors) if fatal_errors else '无'}\n"
            f"- 学员全流程真实操作序列清单 (按时序排列):\n{actions_str}\n\n"
            f"请为该学员生成针对性的深度临床复盘与因果点评："
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "max_tokens": 500,
            "temperature": 0.6
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    choices = data.get("choices", [])
                    if choices:
                        raw_review = choices[0].get("message", {}).get("content", "").strip()
                        if raw_review:
                            # 分段整理为列表呈现
                            paragraphs = [p.strip() for p in raw_review.split("\n\n") if p.strip()]
                            # 标注在线大模型生成标记
                            paragraphs.append(f"• [🟢 临床思维专家白盒复盘由在线大模型动态循证生成: {model}]")
                            return paragraphs
                else:
                    print(f"[GEDEvaluator LLM Error] HTTP {res.status_code}: {res.text[:120]}")
        except Exception as e:
            print(f"[GEDEvaluator LLM Exception] {e}")

        return None

    def _build_comparison_dag_export(self, student_node_ids: Set[str], student_graph: nx.DiGraph) -> Dict[str, Any]:
        """Construct graph nodes and links with temporal 2D coordinates for clear rendering."""
        # 时序阶段固定坐标布局 (横向时序递进，纵向并行展开)
        COORDS = {
            "N1": (60, 50), "N2": (60, 130), "N3": (60, 210),
            "N4": (190, 50), "N5": (190, 130), "N6": (190, 210),
            "N7": (320, 50), "N8": (320, 120), "N9": (320, 190), "N10": (320, 260),
            "N11": (450, 60), "N12": (450, 140), "N13": (450, 210), "N14": (450, 280),
            "N15": (580, 80), "N16": (580, 200),
            "N17": (700, 100), "N18": (700, 210)
        }

        nodes = []
        for nid, data in self.expert_graph.nodes(data=True):
            status = "completed" if nid in student_node_ids else "missed"
            coord = COORDS.get(nid, (100, 100))
            nodes.append({
                "id": nid,
                "name": f"{nid}: {data.get('name', '')}",
                "category": data.get("category", "General"),
                "status": status,
                "critical": data.get("critical", False),
                "score": data.get("score", 5.0),
                "x": coord[0],
                "y": coord[1],
                "value": coord
            })

        links = []
        for u, v, data in self.expert_graph.edges(data=True):
            traversed = (u in student_node_ids) and (v in student_node_ids)
            links.append({
                "source": u,
                "target": v,
                "relation": data.get("relation", "prerequisite"),
                "traversed": traversed
            })

        return {"nodes": nodes, "links": links}


# ===================== P3: 病例结局（outcomes）与处方红线 =====================
# 病例 JSON 里的 `outcomes[].condition` 是给策划/教研写的人读表达式，形如
#   "HR < 60 && 100 <= SBP <= 120 && has('N13') && !has_red_line"
# 这里用一个手写的小解析器求值。**绝不使用 eval/exec**：病例文件是可编辑的
# 外部数据，任何能执行任意代码的入口都会把数据错误升级成安全漏洞。
#
# 支持的语法（覆盖全部 6 个病例）：
#   数字/字符串字面量、变量、函数调用（has/elapsed/outcome_of）、
#   比较运算符 < <= > >= == !=、链式比较（100 <= SBP <= 120）、
#   逻辑 && || !、括号。
# 未知变量/未知函数**抛错**，不静默当 False——写错变量名的 condition 会漏判
# 结局，而结局直接影响结案评价，静默降级比崩溃更难查。


class OutcomeConditionError(ValueError):
    """病例 condition 无法解析或求值时抛出的基类错误。"""


class ConditionSyntaxError(OutcomeConditionError):
    """语法不合法（括号不配对、运算符缺操作数、表达式尾部有多余内容等）。"""


class UnknownVariableError(OutcomeConditionError):
    """condition 里出现了上下文无法提供的变量名。"""


class UnknownFunctionError(OutcomeConditionError):
    """condition 里出现了未注册的函数名。"""


_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<string>'[^']*'|\"[^\"]*\")"
    r"|(?P<op>&&|\|\||<=|>=|==|!=|[<>!(),])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r")"
)

_COMPARISON_OPS = ("<", "<=", ">", ">=", "==", "!=")

# 结局优先级：死亡 > 离院 > 延误 > 治愈。条件之间不保证互斥（例如 C 与 B
# 可能同时成立），临床语义上更坏的那个必须优先。
OUTCOME_PRIORITY = ("D", "C", "B", "A")


def _tokenize_condition(condition: str):
    """把 condition 切成 (kind, value) 记号序列，末尾补 end。"""
    text = condition or ""
    tokens = []
    pos = 0
    length = len(text)
    while pos < length:
        match = _TOKEN_RE.match(text, pos)
        if not match:
            rest = text[pos:].strip()
            if not rest:
                break
            raise ConditionSyntaxError(
                f"条件表达式在第 {pos} 个字符处无法识别：{rest[:24]!r}（原文：{condition!r}）"
            )
        pos = match.end()
        if match.group("number"):
            tokens.append(("number", float(match.group("number"))))
        elif match.group("string"):
            tokens.append(("string", match.group("string")[1:-1]))
        elif match.group("op"):
            tokens.append(("op", match.group("op")))
        else:
            tokens.append(("name", match.group("name")))
    tokens.append(("end", None))
    return tokens


class _ConditionParser:
    """递归下降解析器，产出纯元组 AST（解析与求值彻底分离）。"""

    def __init__(self, tokens, text: str):
        self._tokens = tokens
        self._index = 0
        self._text = text

    # -- 记号游标 ---------------------------------------------------------
    def _peek(self):
        return self._tokens[self._index]

    def _advance(self):
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _at_op(self, *ops: str) -> bool:
        kind, value = self._peek()
        return kind == "op" and value in ops

    def _expect_op(self, op: str) -> None:
        kind, value = self._peek()
        if kind != "op" or value != op:
            raise ConditionSyntaxError(
                f"条件表达式缺少 {op!r}，实际读到 {value!r}（原文：{self._text!r}）"
            )
        self._advance()

    # -- 文法 -------------------------------------------------------------
    def parse(self):
        node = self._parse_or()
        kind, value = self._peek()
        if kind != "end":
            raise ConditionSyntaxError(
                f"条件表达式末尾存在多余内容 {value!r}（原文：{self._text!r}）"
            )
        return node

    def _parse_or(self):
        node = self._parse_and()
        while self._at_op("||"):
            self._advance()
            node = ("or", node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_not()
        while self._at_op("&&"):
            self._advance()
            node = ("and", node, self._parse_not())
        return node

    def _parse_not(self):
        if self._at_op("!"):
            self._advance()
            return ("not", self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self):
        operands = [self._parse_operand()]
        operators = []
        while self._at_op(*_COMPARISON_OPS):
            operators.append(self._advance()[1])
            operands.append(self._parse_operand())
        if not operators:
            return operands[0]
        return ("cmp", operands, operators)

    def _parse_operand(self):
        kind, value = self._peek()
        if kind == "number":
            self._advance()
            return ("number", value)
        if kind == "string":
            self._advance()
            return ("string", value)
        if kind == "op" and value == "(":
            self._advance()
            node = self._parse_or()
            self._expect_op(")")
            return node
        if kind == "name":
            self._advance()
            if self._at_op("("):
                self._advance()
                args = []
                if not self._at_op(")"):
                    while True:
                        args.append(self._parse_operand())
                        if self._at_op(","):
                            self._advance()
                            continue
                        break
                self._expect_op(")")
                return ("call", value, args)
            return ("var", value)
        if kind == "end":
            raise ConditionSyntaxError(f"条件表达式意外提前结束（原文：{self._text!r}）")
        raise ConditionSyntaxError(
            f"条件表达式在 {value!r} 处缺少操作数（原文：{self._text!r}）"
        )


def _as_bool(value, operator: str, expression: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConditionSyntaxError(
        f"运算符 {operator} 的操作数必须是布尔值，实际得到 {type(value).__name__}"
        f"（原文：{expression!r}）"
    )


def _compare(op: str, left, right, expression: str) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        if op == "==":
            return bool(left) == bool(right)
        if op == "!=":
            return bool(left) != bool(right)
        raise ConditionSyntaxError(f"布尔值只能做 == / != 比较（原文：{expression!r}）")
    if isinstance(left, str) or isinstance(right, str):
        if not (isinstance(left, str) and isinstance(right, str)):
            raise ConditionSyntaxError(
                f"字符串与数值不能比较：{left!r} {op} {right!r}（原文：{expression!r}）"
            )
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        raise ConditionSyntaxError(f"字符串只支持 == / != 比较（原文：{expression!r}）")
    left_value = float(left)
    right_value = float(right)
    if op == "<":
        return left_value < right_value
    if op == "<=":
        return left_value <= right_value
    if op == ">":
        return left_value > right_value
    if op == ">=":
        return left_value >= right_value
    if op == "==":
        return left_value == right_value
    return left_value != right_value


def _fn_has(context, args, expression: str) -> bool:
    if len(args) != 1 or not isinstance(args[0], str):
        raise ConditionSyntaxError(f"has() 只接受一个字符串参数（原文：{expression!r}）")
    nodes = context.get("nodes") or ()
    return str(args[0]) in {str(node) for node in nodes}


def _fn_elapsed(context, args, expression: str) -> float:
    if len(args) != 1 or not isinstance(args[0], str):
        raise ConditionSyntaxError(f"elapsed() 只接受一个字符串参数（原文：{expression!r}）")
    table = context.get("elapsed") or {}
    return float(table.get(str(args[0]), 0.0))


def _fn_outcome_of(context, args, expression: str) -> str:
    if len(args) != 1 or not isinstance(args[0], str):
        raise ConditionSyntaxError(f"outcome_of() 只接受一个字符串参数（原文：{expression!r}）")
    table = context.get("outcomes") or {}
    return str(table.get(str(args[0]), "UNKNOWN"))


_CONDITION_FUNCTIONS = {
    "has": _fn_has,
    "elapsed": _fn_elapsed,
    "outcome_of": _fn_outcome_of,
}


def _evaluate_node(node, context, expression: str):
    tag = node[0]
    if tag == "number":
        return node[1]
    if tag == "string":
        return node[1]
    if tag == "var":
        name = node[1]
        if name in context:
            return context[name]
        if name in ("true", "True"):
            return True
        if name in ("false", "False"):
            return False
        raise UnknownVariableError(
            f"条件表达式引用了未知变量 {name!r}（原文：{expression!r}）。"
            f"可用变量：{', '.join(sorted(map(str, context)))}"
        )
    if tag == "call":
        name = node[1]
        handler = _CONDITION_FUNCTIONS.get(name)
        if handler is None:
            raise UnknownFunctionError(
                f"条件表达式引用了未注册的函数 {name!r}（原文：{expression!r}）。"
                f"可用函数：{', '.join(sorted(_CONDITION_FUNCTIONS))}"
            )
        args = [_evaluate_node(arg, context, expression) for arg in node[2]]
        return handler(context, args, expression)
    if tag == "not":
        value = _as_bool(_evaluate_node(node[1], context, expression), "!", expression)
        return not value
    if tag in ("and", "or"):
        # 刻意不做短路：两侧都求值，写错的变量名在 && / || 后面也照样暴露。
        left = _as_bool(_evaluate_node(node[1], context, expression), tag, expression)
        right = _as_bool(_evaluate_node(node[2], context, expression), tag, expression)
        return (left and right) if tag == "and" else (left or right)
    if tag == "cmp":
        operands = [_evaluate_node(item, context, expression) for item in node[1]]
        results = [
            _compare(op, operands[i], operands[i + 1], expression)
            for i, op in enumerate(node[2])
        ]
        return all(results)
    raise ConditionSyntaxError(f"无法识别的表达式节点 {tag!r}（原文：{expression!r}）")


class ConditionExpression:
    """解析后的 condition：可重复求值，解析错误在构造时就暴露。"""

    def __init__(self, text: str, ast):
        self.text = text
        self.ast = ast

    def evaluate(self, context: Dict[str, Any]) -> bool:
        value = _evaluate_node(self.ast, dict(context or {}), self.text)
        return _as_bool(value, "条件整体", self.text)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ConditionExpression({self.text!r})"


def parse_condition(condition: str) -> ConditionExpression:
    """解析 condition；语法错误抛 ConditionSyntaxError（不执行任何代码）。"""
    if not isinstance(condition, str) or not condition.strip():
        raise ConditionSyntaxError(f"condition 不能为空：{condition!r}")
    parser = _ConditionParser(_tokenize_condition(condition), condition)
    return ConditionExpression(condition, parser.parse())


def evaluate_condition(condition: str, context: Dict[str, Any]) -> bool:
    """解析并求值一条 condition，返回布尔结果。"""
    return parse_condition(condition).evaluate(context)


def resolve_outcome(case_data: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按病例 `outcomes[]` 求值，返回命中的结局（含 code/label/title/narration 等）。

    优先级见 ``OUTCOME_PRIORITY``。都不命中时返回 None——「未达任何结局」
    是合法状态（继续救治），不能硬塞一个默认结局。
    """
    outcomes = (case_data or {}).get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return None
    ordered_codes = [code for code in OUTCOME_PRIORITY if code in outcomes]
    ordered_codes.extend(code for code in outcomes if code not in OUTCOME_PRIORITY)
    for code in ordered_codes:
        item = outcomes.get(code)
        if not isinstance(item, dict):
            continue
        condition = item.get("condition")
        if not condition:
            continue
        if evaluate_condition(condition, context):
            resolved = dict(item)
            resolved.setdefault("code", code)
            return resolved
    return None


# ---- 从会话现场构建求值上下文 ------------------------------------------------

def action_kind(action) -> str:
    """动作类型的小写字符串（ActionType 枚举与裸字符串都能吃）。"""
    action_type = getattr(action, "action_type", "")
    return str(getattr(action_type, "value", action_type)).lower()


def treated_medications(action_records) -> List[ActionRecord]:
    """会话里被采纳的用药处置（``treated`` / ``treated_count`` 的唯一口径）。

    判定口径：``action_type`` 为 medication/prescription，且**不是红线用药**。
    红线（LETHAL 的 ``is_contraindicated``、HARMFUL 落下的 ``warning_message``）
    一律不算「有效处置」——它们已经由 ``has_red_line`` 走 D 结局，不该再用来
    拿 A（治愈/稳定）。
    """
    treated: List[ActionRecord] = []
    for action in action_records or []:
        if action_kind(action) not in ("medication", "prescription"):
            continue
        if getattr(action, "is_contraindicated", False):
            continue
        if str(getattr(action, "warning_message", "") or "").strip():
            continue
        treated.append(action)
    return treated


def has_red_line(action_records) -> bool:
    """本次会话是否触碰过处方红线（致命与可逆都算）。

    判定权归 PhysiologyEngine：它在动作当时按病例 ``prescription_rules`` 匹配
    并把结论写进返回值（is_lethal / is_harmful），routes_action 再落进
    ActionRecord：LETHAL → ``is_contraindicated=True`` + ``warning_message``；
    HARMFUL → 只写 ``warning_message``（可逆，不是一票否决）。

    这里刻意不重新匹配关键词——带「未先用β受体阻滞剂」前提的规则（胸痛病例
    的硝普钠单用）事后重算会得出与当时相反的结论：先用对了β阻滞剂，单用硝普钠
    其实是 TARGET，事后按内容硬匹配会把它误判成红线。
    """
    for action in action_records or []:
        if getattr(action, "is_contraindicated", False):
            return True
        if str(getattr(action, "warning_message", "") or "").strip():
            return True
    return False


def golden_node_ids(session) -> Set[str]:
    """复用 GED 的语义匹配，取得学员命中的金标准节点（N13/N14 ...）。"""
    evaluator = getattr(session, "ged_evaluator", None)
    history = list(getattr(session, "action_history", None) or [])
    if evaluator is None or not history:
        return set()
    try:
        graph, _ = evaluator.build_student_graph(history)
    except Exception:  # 匹配失败不应阻断结局判定
        return set()
    return {str(node) for node in graph.nodes() if not str(node).startswith("U_")}


def physiology_outcome_label(engine) -> str:
    """把生理状态机映射成 condition 里的 outcome_of('physiology') 词表。"""
    if engine is None:
        return "UNKNOWN"
    if getattr(engine, "fatal_death_occurred", False):
        return "ARREST"
    state = engine.get_state()
    name = str(getattr(state, "name", state))
    return {
        "S0_STABLE": "STABLE",
        "S1_HYPERTENSION_CRISIS": "CRISIS",
        "S2_PHARMACO_CONTROLLED": "CONTROLLED",
        "S3_PERICARDIAL_EFFUSION": "EFFUSION",
        "S4_CARDIAC_TAMPONADE": "TAMPONADE",
        "S5_AORTIC_RUPTURE": "RUPTURE",
        "S6_CARDIAC_ARREST": "ARREST",
    }.get(name, name)


def _emotion_history(emotion):
    return list(getattr(emotion, "_history", None) or [])


def elapsed_since_trust_low(emotion, threshold: float = 0.10) -> float:
    """信任度连续低于阈值的持续秒数（condition 里的 elapsed('trust_low')）。"""
    history = _emotion_history(emotion)
    if not history:
        return 0.0
    current_seconds = float(getattr(emotion, "virtual_seconds", 0.0) or 0.0)
    start = None
    for entry in reversed(history):
        stamp, trust = (entry[0], entry[1]) if isinstance(entry, (tuple, list)) else (0.0, 1.0)
        if float(trust) <= threshold:
            start = float(stamp)
        else:
            break
    if start is None:
        return 0.0
    return max(0.0, current_seconds - start)


def negative_streak(emotion) -> int:
    """信任度连续下跌的事件次数（condition 里的 negative_streak）。"""
    log = list(getattr(emotion, "delta_log", None) or [])
    streak = 0
    for record in reversed(log):
        if not isinstance(record, dict) or record.get("metric") != "patient_trust":
            continue
        if float(record.get("delta", 0.0) or 0.0) < 0:
            streak += 1
        else:
            break
    return streak


def build_outcome_context(session) -> Dict[str, Any]:
    """从会话现场收集 condition 需要的全部变量（未知变量仍会抛错）。"""
    engine = getattr(session, "physiology_engine", None)
    vitals = engine.get_current_vitals() if engine is not None else None
    emotion = getattr(session, "emotion", None)
    snapshot: Dict[str, Any] = {}
    if emotion is not None:
        try:
            snapshot = emotion.snapshot() or {}
        except Exception:
            snapshot = {}
    case_data = getattr(session, "case_data", {}) or {}
    treated = treated_medications(getattr(session, "action_history", None))
    context: Dict[str, Any] = {
        # 动作级事实：与 trust 随时间衰减无关，是「真的处置过」的稳定守门条件。
        "treated": bool(treated),
        "treated_count": len(treated),
        "HR": float(getattr(vitals, "hr", 0.0)),
        "SBP": float(getattr(vitals, "sbp", 0.0)),
        "DBP": float(getattr(vitals, "dbp", 0.0)),
        "RR": float(getattr(vitals, "rr", 0.0)),
        "SpO2": float(getattr(vitals, "spo2", 0.0)),
        "pain_vas": float(getattr(vitals, "pain_vas", 0.0)),
        "trust": float(snapshot.get("patient_trust", 0.0)),
        "patient_anxiety": float(snapshot.get("patient_anxiety", 0.0)),
        "family_anxiety": float(snapshot.get("family_anxiety", 0.0)),
        "negative_streak": negative_streak(emotion),
        "has_red_line": has_red_line(getattr(session, "action_history", None)),
        "nodes": sorted(golden_node_ids(session)),
        "elapsed": {"trust_low": elapsed_since_trust_low(emotion)},
        "outcomes": {"physiology": physiology_outcome_label(engine)},
    }
    return context


def resolve_session_outcome(session) -> Optional[Dict[str, Any]]:
    """按会话当前状态判定结局；病例没有 outcomes 时返回 None。"""
    case_data = getattr(session, "case_data", {}) or {}
    if not case_data.get("outcomes"):
        return None
    return resolve_outcome(case_data, build_outcome_context(session))


def knowledge_card_of(case_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """病例自带的知识卡（结案报告原样带出）。"""
    card = (case_data or {}).get("knowledge_card")
    return card if isinstance(card, dict) else None
