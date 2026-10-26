# ClinMind-AI v2

> 面向临床思维训练与 OSCE 复盘的虚拟标准化患者系统。通过病例规则、连续生命体征仿真和多角色对话，把问诊、检查、处方与转归决策串成一个可回放的教学场景。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![UI](https://img.shields.io/badge/UI-HTML%2FCSS%2FVanilla%20JS-F7DF1E?logo=javascript&logoColor=222)](https://developer.mozilla.org/)
[![运行模式](https://img.shields.io/badge/默认运行-离线规则仿真-2ea44f)](./使用说明.md)

## 项目预览

### 接诊室

![ClinMind-AI 接诊室](./design/ui-concepts/01-接诊场景.png)

### 电脑工作站

![ClinMind-AI 电脑工作站](./design/ui-concepts/02-电脑工作站.png)

## 项目能做什么

- **新版接诊室与工作站**：接诊室负责沉浸式沟通，工作站负责监护、报告、医嘱和结案复盘。
- **连续生命体征仿真**：后端以 1 Hz 推进病例状态，生成带生理波动的 ECG、Pleth、心率、血压、SpO₂、呼吸频率和疼痛评分。
- **同源监护数据**：工作站监护与接诊室报告使用同一会话状态；ECG 报告中的心率会跟随当前监护数据。
- **动态医患交互**：患者、家属和护士拥有独立角色。问诊、解释病情、检查和用药会影响患者信任度与家属焦虑度，对话也会根据当前操作更新。
- **五类临床操作**：问诊、体查、检验、处方、转归，支持快捷操作和自定义指令。
- **病例驱动的客观报告**：提供 ECG、心肌损伤标志物、D-二聚体、床旁超声和主动脉 CTA 等教学报告，报告由病例数据和规则引擎生成。
- **安全边界演练**：对禁忌抗栓、抗凝或溶栓用药触发病理恶化与倒计时，用于训练高危胸痛鉴别和处置优先级。
- **实时推送与恢复**：通过 WebSocket 推送 1 Hz 监护数据；SQLite 保存会话、操作记录、报告和评分，刷新页面后可以恢复训练进度。
- **OSCE 复盘**：结案后生成总分、六维能力雷达图、操作时间线、致命错误和临床反馈。
- **可选模型服务**：默认使用离线规则引擎；也可在设置窗口配置 DeepSeek、OpenAI 或自定义 OpenAI 兼容接口，并执行 API 连通测试查看状态。
- **可扩展病例库**：融合旧版的 5 个通用病例（感冒、胃肠炎、哮喘、低血糖、阑尾炎）和原有高阶胸痛病例；病例索引、查体结果、检查报告、对话与治疗提示均从 JSON 读取。

## 快速开始

### Windows 一键启动（推荐）

1. 安装 Python 3.10 或更高版本。
2. 双击项目根目录的 `start.cmd`。
3. 浏览器打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

启动脚本会进入 `aic_mech`，优先使用其中的 `.venv`，服务就绪后自动打开浏览器；如果虚拟环境或依赖不存在，会自动创建并安装 `requirements.txt`。端口被占用时会自动选择后续可用端口。首次安装依赖需要网络，启动后的离线演示不需要 API Key。

### 手动启动

```powershell
Set-Location ".\aic_mech"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

服务默认只监听本机 `127.0.0.1:8000`。如果端口被占用，可在 `aic_mech/.env` 中设置 `PORT=8001` 后重启。

## 页面与接口

| 用途 | 地址 |
| --- | --- |
| 接诊室 | [http://127.0.0.1:8000/static/reception.html](http://127.0.0.1:8000/static/reception.html) |
| 电脑工作站 | [http://127.0.0.1:8000/static/workstation.html](http://127.0.0.1:8000/static/workstation.html) |
| FastAPI 文档 | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |
| 健康检查 | [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health) |

主要 API 分组如下：

| 分组 | 作用 |
| --- | --- |
| `/api/case` | 病例列表、启动会话、读取会话工作区 |
| `/api/action` | 执行问诊、体查、检查、用药和转归 |
| `/api/eval` | 提交结案、生成和读取 OSCE 评估 |
| `/api/config` | 读取、保存和测试模型配置 |
| `/ws/vitals/{session_id}` | WebSocket 实时监护推送 |

## 操作快捷键

| 按键 | 功能 |
| --- | --- |
| `1` | 打开问诊 |
| `2` | 打开体查 |
| `3` | 打开检验 |
| `4` | 打开处方 |
| `5` | 打开转归 |
| `E` | 从接诊室进入电脑工作站 |
| `Esc` | 从工作站返回接诊室 |

接诊室右上角的设置入口可以配置 API 模型、模型名称、服务地址和监护音量；连接测试完成后会显示“已连接”或失败原因。左侧声音按钮用于静音，右侧多余按钮已移除。

## 模型配置

系统默认使用**离线规则仿真**，适合无网络环境和课堂演示。在线模型是显式开启的可选能力，不会因为 `.env` 中存在 Key 就自动发送网络请求。

推荐直接在页面设置窗口中完成配置。也可以复制示例文件后手动编辑：

```powershell
Copy-Item ".\\aic_mech\\.env.example" ".\\aic_mech\\.env"
```

离线模式的最小配置：

```dotenv
USE_ONLINE_LLM=false
USE_OFFLINE_ONLY=true
LLM_PROVIDER=offline
```

使用在线服务时，将 `USE_ONLINE_LLM` 设为 `true`、`USE_OFFLINE_ONLY` 设为 `false`，再填写对应服务商的 Key、Base URL 和模型名。自定义服务需要提供 OpenAI 兼容的 `/chat/completions` 接口。`.env` 已被 Git 忽略，请勿提交真实 API Key。

## 当前教学病例

项目内置 6 个虚构教学病例，可从“下一位患者”选择。52 岁男性胸痛病例仍保留完整的主动脉夹层高风险引擎；其余 5 个病例使用各自的初始生命体征、病例报告、对话层和低风险仿真流程。胸痛病例目标是识别并处置 Stanford A 型主动脉夹层，学员可以在同一会话中完成：

1. 初始生命体征观察和结构化问诊；
2. 双上肢血压、心脏和神经系统等体查；
3. ECG、实验室检查、床旁超声和主动脉 CTA；
4. 控制心率与血压、避免禁忌用药、呼叫相关专科；
5. 提交诊断并查看 OSCE 评分与操作时间线。

病例数据位于 `aic_mech/app/data/case_chest_pain.json` 与 `aic_mech/app/data/cases/`，病例索引为 `aic_mech/app/data/cases/index.json`；专家决策图位于 `aic_mech/app/data/expert_golden_dag.json`。

## 系统结构

```text
ClinMind-AI-1/
├── start.cmd                       # Windows 双击启动入口
├── start.ps1                       # 自动创建环境并启动服务
├── 使用说明.md                     # 运行说明
├── README.md                       # 项目说明
├── design/ui-concepts/             # 设计交付物：场景原型、渲染图、设计说明与渲染脚本
└── aic_mech/
    ├── app/                        # FastAPI、病例、引擎和数据模型
    │   └── data/cases/             # 5 个通用病例及病例索引
    ├── static/                     # 接诊室与工作站前端（两页共用 scene.css / scene.js）
    ├── tests/                      # 后端、静态资源和浏览器联调测试
    ├── config.py                   # 服务、模型和仿真配置
    ├── main.py                     # Uvicorn 启动入口
    ├── requirements.txt            # Python 依赖
    └── .env.example                # 安全配置模板
```
后端使用 FastAPI + SQLite + WebSocket，前端为静态 HTML/CSS/JavaScript，不需要 Node.js 构建步骤。

## 测试与验证

在源码目录执行：

```powershell
Set-Location ".\aic_mech"
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe tests/run_all_tests.py
```

测试覆盖核心引擎、REST 主流程、静态资源、SQLite 重启恢复、会话隔离、非法输入、禁忌状态回传、实时监护和结案冻结。前端脚本也可以使用 Node.js 做语法检查；浏览器冒烟测试位于 `tests/browser_smoke.cjs`，需要额外安装 Playwright 和本机 Chrome。

## 数据与隐私

- 训练数据默认保存在源码目录的 `aic_mech/runtime/clinmind.sqlite3`，该目录已加入 `.gitignore`。
- 项目默认只监听本机地址，不提供账号系统、多人权限或真实患者接入。
- 不要把真实患者信息、真实 API Key 或生产环境数据写入仓库。

## 医疗免责声明

本项目仅用于医学教学、临床思维训练和软件演示。病例、生命体征和检查报告均为规则驱动的教学仿真，不能替代真实医疗机构、执业医师、临床指南或设备读数，不得用于真实患者的诊断、治疗或用药决策。

## 许可证

当前仓库未附带明确的开源许可证。若要在 GitHub 公开发布，请根据作者和团队的授权范围补充 `LICENSE` 文件；在许可证明确前，请勿默认将本项目用于商业分发或二次发布。



