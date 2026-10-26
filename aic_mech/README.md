# ClinMind-AI：因果图谱与生理演化双核驱动的虚拟标准化病人与临床思维OSCE实训系统

> **2026-10-16 新 UI 接入更新**：在压缩包最外层双击 `start.cmd`，访问 `http://127.0.0.1:8000` 即进入新的接诊场景。点击电脑进入工作站。新版已接入离线问诊、检查、监护、评分及 SQLite 会话恢复；当前只有一个教学病例。详见最外层 `使用说明.md`。API Key 不会自动启用联网，需在配置窗口主动开启在线模型。

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-36%20passed%20%2F%20100%25-brightgreen.svg)]()
[![Competition](https://img.shields.io/badge/Category-%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E7%A7%91%E5%AD%A6%E6%8A%80%E6%9C%AF%E5%A4%A7%E8%B5%9B%E6%96%B9%E6%A1%88%E8%B5%9B%E9%81%93-red.svg)]()

> **参赛作品声明**：本作品严格对照人工智能+医疗健康与学科交叉应用大赛规范开发，坚决摒弃通用大模型“套皮问诊”模式，提出“因果知识图谱防幻觉 + 连续血流动力学状态机 + 医患多Agent认知博弈 + 图编辑距离 (GED) 思维拓扑对齐”四大创新机制，实现真正具备临床病理演化闭环与客观评估能力的国家级高水平医工交叉作品。

---

## 🌟 核心创新与技术亮点 (Core Innovations)

```
                     [ 学员临床操作流：语音/文本问诊 + 目标查体 + 检查开立 + 药物处方 ]
                                                    │
┌───────────────────────────────────────────────────▼───────────────────────────────────────────────────┐
│                                   ClinMind-AI 四层三核智能中枢                                         │
│                                                                                                       │
│  【核一：认知博弈】 Multi-Agent 医患交互网络                                                           │
│   • 患者 Agent (孙建国)：搭载信任动力学方程 T_trust(t)，同理心安抚与对症镇痛解锁隐藏停药史            │
│   • 家属 Agent (李秀梅)：焦虑阻抗打断机制，提供重体力劳动诱发史旁证                                   │
│   • 责任护士 Agent (林美琴)：执行医嘱时钟队列，双上肢压差>30mmHg与危急值主动报警                      │
│                                                                                                       │
│  【核二：医学约束】 双轨制 Graph-RAG 防幻觉物理隔离护栏                                               │
│   • 对话渲染轨 (Track A) 与 客观检查硬隔离轨 (Track B) 物理隔绝                                       │
│   • 18导联ECG、心肌酶谱、D-二聚体、床旁POCUS超声、全长增强CTA权威标准字典刚性输出，杜绝LLM捏造数字    │
│                                                                                                       │
│  【核三：演化推演】 动态病理生理演化状态机 (Dynamic Physiology Engine)                                 │
│   • 1Hz 连续差分动力学推演血流动力学向量 [HR, SBP_R, SBP_L, DBP, SpO2, RR, Pain, Rhythm]             │
│   • 规范降压分支：硝普钠+艾司洛尔靶向控制血压 110-120 mmHg，心率 60-70 bpm                           │
│   • 致命互斥处方：误用阿司匹林/替格瑞洛/肝素/溶栓即刻触发 120s 破裂倒计时，进入心包压塞Beck三联征致死 │
│                                                                                                       │
│  【量化评估】 基于图编辑距离 (GED) 的 OSCE 白盒思维拓扑复盘                                            │
│   • 将学员操作流实时转换为 DAG 图 G_user，与 18节点/22边专家金标准图 G_expert 计算拓扑图编辑距离     │
│   • 输出 OSCE 六维能力雷达图，智能归因“锚定偏倚、过早闭环、隧道视野、漏诊误治”四大临床思维缺陷原型    │
└───────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 📂 项目全景工程结构

```
aic_mech/
├── app/                             # 系统核心后端服务 (FastAPI)
│   ├── api/                         # RESTful 接口与 WebSocket 实时广播
│   │   ├── routes_case.py           # 病例管理与初始化
│   │   ├── routes_action.py         # 问诊、查体、检查、处方、转归决策接口
│   │   ├── routes_eval.py           # 结案提交与 OSCE 评分报告
│   │   ├── routes_config.py         # LLM 模型配置读写与连通性测试
│   │   └── websocket_feed.py        # 1Hz 遥测体征广播与危急值推送
│   ├── core/                        # 核心算法引擎与会话管理
│   │   ├── physiology_engine.py     # 连续血流动力学状态机 (含 120s 破裂倒计时)
│   │   ├── guardrail_engine.py      # 双轨制 Graph-RAG 防幻觉物理隔离护栏
│   │   ├── agent_engine.py          # 多角色认知博弈引擎 (患者/家属/护士)
│   │   ├── ged_evaluator.py         # 基于 NetworkX 的图编辑距离 (GED) 算法
│   │   ├── case_repository.py       # 病例目录加载、校验与归一化
│   │   └── session_manager.py       # 多学员会话状态生命周期管理
│   ├── data/                        # 确定性知识图谱与病例底座
│   │   ├── case_chest_pain.json     # Stanford A型主动脉夹层王牌示范病例
│   │   ├── expert_golden_dag.json   # 18节点/22边专家金标准决策拓扑图
│   │   └── cases/                   # 通用训练病例 5 例 + index.json 目录
│   ├── models/                      # 数据实体与 Pydantic 校验模型
│   │   ├── entities.py              # 生命体征、操作记录、OSCE评估量表实体
│   │   └── schemas.py               # API 请求与响应结构定义
│   └── server.py                    # FastAPI 实例装配与静态资源挂载
│
├── static/                          # 前端 (HTML5/CSS3/Vanilla JS，无构建步骤)
│   ├── reception.html               # 接诊室 · 沉浸式医患沟通场景
│   ├── workstation.html             # 电脑工作站 · 监护/病历/操作/六维雷达
│   ├── css/
│   │   └── scene.css                # 接诊室与工作站共享场景样式 (单一来源)
│   └── js/
│       ├── scene.js                 # 双页共享交互层：场景、监护、接诊记录、报告、配置
│       └── audio_alerts.js          # Web Audio 监护仪蜂鸣与危急值警报 (双页共用)
│
├── tests/                           # 自动化测试套件 (53 项，语句覆盖率 56.3%)
│   ├── test_engines.py              # 核心引擎功能自检
│   ├── test_e2e_edge_cases.py       # 极限边界、熔断致死与并发用例
│   ├── test_ai_native_capabilities.py # 多智能体原生能力用例
│   ├── test_v2_regressions.py       # v2 回归用例
│   ├── test_session_backend.py      # 会话持久化/隔离/结案冻结与静态资源可达性
│   ├── test_static_assets.py        # 双页前端资源可达性与样式/交互层契约校验
│   ├── browser_smoke.cjs            # Puppeteer 冒烟脚本 (需手动执行)
│   ├── browser_v2.cjs               # Puppeteer 双页联调脚本 (需手动执行)
│   └── run_all_tests.py             # 综合测试运行器与代码覆盖率统计
│
├── config.py                        # 全局配置中心 (端口/模型Key/离线开关)
├── .env.example                     # 环境变量模板 (在线模型 Key 等)
├── requirements.txt                 # 项目依赖项清单
├── main.py                          # 单命令一键启动入口 (自动挂载前端)
└── runtime/                         # 运行时会话快照 (本地生成，已 gitignore)
```

---

## 🚀 快速启动指南 (Quick Start)

系统采用**零外部破坏性依赖**设计，无需提前安装配置复杂数据库或构建环境，开箱即用。

### 1. 环境准备与依赖安装
确保系统已安装 Python 3.10+ 环境：
```bash
pip install -r requirements.txt
```
*(主要依赖：fastapi, uvicorn, pydantic, networkx, numpy, websockets, httpx)*

### 2. 一键启动系统
```bash
python main.py
```
启动成功后，控制台将输出服务地址。在浏览器中打开：
👉 **http://127.0.0.1:8000**

即可直接进入**高保真急诊抢救室工作台**，开始接诊 52 岁剧烈撕裂样胸痛患者孙建国！

### 3. 执行全套自动化测试
```bash
python tests/run_all_tests.py
```
*(将自动执行 36 项自动化测试用例，并输出模块覆盖率报告。预期结论：36 Passed, 0 Failed)*

---

## 🎯 核心实训演练体验流程

1. **初始评估**：观察右侧 60fps 动态心电监护仪（HR 108次/分，右上肢血压 185/110 mmHg，呼吸急促）。
2. **多角色问诊**：
   - 与患者自然对话：若直接逼问可能受挫；若给予“同理心安慰+给药止痛”，信任度提升至 $\ge 50$，患者主动坦白“5年高血压长期私自停药史”。
   - 关注家属打断：家属李秀梅焦急催促，解释病情可解锁“重体力搬重物诱发”关键信息。
3. **精准体检与双轨制防幻觉检查**：
   - 下达“测双上肢血压”：系统刚性返回“左上肢 148/92 mmHg，双侧收缩压差 37 mmHg”的特征体征。
   - 调阅心电图与心肌酶：ST段轻度改变但 cTnI 阴性，D-二聚体显著升高（8.86 mg/L），提示主动脉夹层极高危！
4. **决策与恶化推演**：
   - **错误决策演练**：误按心梗下达阿司匹林/替格瑞洛/肝素双抗抗凝，监护仪立刻红闪警报并启动 120 秒破裂倒计时，倒计时归零后患者发生心包压塞濒死猝死。
   - **正确决策演练**：下达艾司洛尔+硝普钠靶向降压控心率，紧急开立急诊全长增强 CTA 确诊 Stanford A 型夹层，呼叫心胸血管外科急诊手术。
5. **OSCE 白盒复盘**：点击“提交结案并查看 OSCE 思维复盘报告”，系统秒级生成学员思维 DAG 拓扑图与金标准图对比、六维能力雷达图与因果失误归因。

---
