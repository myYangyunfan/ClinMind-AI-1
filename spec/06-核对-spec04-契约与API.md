# 06 核对 · spec/04 契约与 API 的实现状况

> 核对对象：`spec/04-AI能力与数据契约.md`（833 行）
> 代码基线：`HEAD=779dabd`（fix(case): 胸痛知识卡的心率口径与新结局阈值对齐）
> 核对时间：2026-10-26
> **基线声明**：代码结论钉在 `HEAD=779dabd`——所有 `文件:行` 都用 `git show HEAD:<path>` 逐条复核过；数据结论（24 键、计数、文案长度）以**当前工作树**的 JSON 为准。
>
> **核对期间发生了并发改动**（他人仍在写，非本次核对所改）：`agent_engine.py`（707→908 行，新增 `question_kind` 三分类）、`guardrail_engine.py`（391→448）、`routes_action.py`、`schemas.py`、`scene.js`、6 个病例 JSON、`test_static_assets.py`、`spec/verify_outcomes.py`。若这批改动合入，**§3.4 G8、§4 患者对话、§6 #12** 三条结论需重跑（`question_kind` 接通后，问诊的 `emotion_applied` 会由 `false` 变 `true`——是改进方向，不是回归）。

## 0. 口径

**判定四选一**

| 判定 | 含义 |
|---|---|
| 已实现 | 有生产代码路径 + 本次实测证据（运行输出或 `文件:行`） |
| 部分实现 | 主路径存在，但契约要求的字段/分支/端点缺一块 |
| 未实现 | 生产代码中查无此项（grep 0 处 / 端点 404 / 脚本不存在） |
| 文档与实现不一致 | 代码做了，但字段名、位置、数值、数量与文档不同 |

**取证手段**

| 手段 | 用途 |
|---|---|
| `python spec/check_cases.py` | 病例数据体检（PASS 6/6） |
| `fastapi.testclient.TestClient` 打真实端点 | 响应键与值 |
| `EmotionEngine.tick` / `SessionStore.create_session` 模拟 480 s | 挂机与离院行为 |
| 全仓 `Select-String` | 字段名/错误码是否存在 |
| `cmd /c` 复核退出码 | 规避 PowerShell 把 stderr 当失败 |

**总览**

| 节 | 条目数 | 已实现 | 部分实现 | 未实现 | 不一致 |
|---|---|---|---|---|---|
| §1 schema | 43 | 40 | 1 | 0 | 2 |
| §2 校验器 | 43 | 0 | 6 | 37 | 0 |
| §3 LLM | 32 | 13 | 6 | 13 | 0 |
| §4 离线矩阵 | 15 | 8 | 2 | 5 | 0 |
| §5 会话字段 | 10 | 1 | 0 | 3 | 6 |
| §6 API | 23 | 0 | 4 | 19 | 0 |
| §7 迁移 | 6 | 3 | 0 | 3 | 0 |
| §8 冲突 | 16 | 7 | 3 | 5 | 1 |
| **合计** | **188** | **72** | **22** | **85** | **9** |

> 计数口径：一个"判定行"＝表格里以四个判定词之一收尾的行；同一契约条目在不同小节被拆开核对的，按行计（例如 24 键占 24 行）。

---

## 1. §1 病例 JSON 完整 Schema（24 键）

### 1.1 顶层 24 键（实测：6/6 病例齐全）

取证脚本：逐个 `json.load` 后与 24 键白名单求差集。输出 `missing(0)` × 6 文件。

| # | 键 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|---|
| 1 | `case_id` | string 必填，全系统主键 | 6/6 存在；`case_repository.py:35` 仅此项被强制 | 已实现 |
| 2 | `title` | string 必填 | 6/6 存在；`case_repository.py:93` 读取 | 已实现 |
| 3 | `difficulty` | string 非枚举 | 6/6 存在；`case_repository.py:100` | 已实现 |
| 4 | `difficulty_stars` | integer 可选，缺省 1 | 6/6 存在（无默认补齐代码，见 §2 CM-502） | 已实现 |
| 5 | `chief_complaint` | string 含时间量词 | 6/6 存在；`case_repository.py:94` | 已实现 |
| 6 | `category` | string | 6/6 存在；`case_repository.py:95` | 已实现 |
| 7 | `patient_profile` | object（含 past/medication history） | 6/6；`agent_engine.py:56` `_persona_field` 读取 | 已实现 |
| 8 | `initial_vitals` | object → `VitalsVector` | 6/6；`routes_case.py` 返回 `initial_vitals` | 已实现 |
| 9 | `physical_exam_findings` | object（镜像） | 6/6；`guardrail_engine.py` 读 | 已实现 |
| 10 | `lab_and_imaging_reports` | object（镜像） | 6/6；`guardrail_engine.py` 读 | 已实现 |
| 11 | `dialogue_scripts` | object | 6/6；`agent_engine` 离线兜底 | 已实现 |
| 12 | `hemodynamic_rules` | object | 6/6；`physiology_engine.py:91-93` 读 `high_risk_aortic` / `lethal_rupture_countdown_seconds` | 已实现 |
| 13 | `emotion_scripts` | object 6 节点 × 3 档 | 6/6；`agent_engine.py:18-21,152-169` | 已实现 |
| 14 | `exam_options` | array | 6/6；`guardrail_engine.py:23,87-110,168,228` | 已实现 |
| 15 | `lab_panels` | array | 6/6；`guardrail_engine` 报告值直出 | 已实现 |
| 16 | `prescription_rules` | array | 6/6；`physiology_engine.py:35-36,107-175,231` | 已实现 |
| 17 | `outcomes` | object A/B/C/D | 6/6；`ged_evaluator.py:489-700` 条件求值 + `routes_eval.py` | 已实现 |
| 18 | `knowledge_card` | object 5 条 | 6/6；`ged_evaluator.py:993` + `schemas.py:135` | 已实现 |
| 19 | `present_illness` | object 五要素 | 6/6，键恰为 `onset/character/radiation/trigger/relief` | 已实现 |
| 20 | `risk_factors` | array ≥3 | 6/6（实测 4/4/5/5/5/5） | 已实现 |
| 21 | `key_positives` | array 3 | 6/6（均 3） | 已实现 |
| 22 | `key_negatives` | array 2 | 6/6（均 2） | 已实现 |
| 23 | `inquiry_points` | array 8 | 6/6（均 8） | 已实现 |
| 24 | `differentials` | array 3，含 `why_not` | 6/6（均 3，键 `name/why_not/why_support`） | 已实现 |

**§1.1 结论**：数据侧 24/24 全部落地；**但没有任何代码按 24 键做"缺键即拒"**（见 §2）。

### 1.2 六个新增键的字段级核对

| 键 | 契约字段 | 实测字段（病例 1） | 判定 |
|---|---|---|---|
| `emotion_scripts` | 6 节点 × `high/mid/low`，20–120 字 | 6 节点齐全；108 条文案（6 病例 ×18），长度 20–57 字，越界 0 条 | 已实现 |
| `exam_options[]` | `exam_id`/`name`/`match_keywords`/`duration_seconds`/`required`/`is_abnormal`/`findings`/`clinical_point`/`guard_note?` | `clinical_point, duration_seconds, exam_id, findings, guard_note, is_abnormal, match_keywords, name, required`（9 键全中） | 已实现 |
| | `duration_seconds ∈ {30,60,90,180,300}` | 病例 1 = {60,90}；gam = {30,60,90} ⊂ 允许集 | 已实现 |
| `lab_panels[]` | 13 字段（`panel_id`…`clinical_hint`） | 13 键全中；`modality ∈ {ECG,LAB,IMAGING,ULTRASOUND}`；`tat ∈ {120,300,600,900,1200,1500,1800}` ⊂ 60..3600 | 已实现 |
| `prescription_rules[]` | 10 字段；`verdict ∈ {TARGET,SUPPORTIVE,WARN_ONLY,RED_LINE,OFF_LIST}` | 10 键全中；实测 verdict 集 = {TARGET, SUPPORTIVE, RED_LINE}（病例 1）/ 加 WARN_ONLY（gam_001）；`OFF_LIST` 无数据 | 部分实现 |
| `outcomes` | 4 键 × 9 字段 | 键恰为 A/B/C/D；`outcome.A` 含 `code,condition,family_line,label,narration,next_step,patient_line,review_items,title` 9 键全中 | 已实现 |
| `knowledge_card` | `title` + `items`(5)，`items[].no/title/one_liner/pitfall` | 全中；`items` 恒 5 | 已实现 |

`OFF_LIST` 判部分实现的理由：`physiology_engine._normalise_prescription_rules`（`:107-131`）只登记关键词，无 `OFF_LIST` 专用分支（拒绝开具且不消耗虚拟时间）；现由「关键词未命中 → 拒绝」的兜底路径代替，行为近似但无独立判定出口。

### 1.3 §4.2 十二条数量约束实测

| # | 约束 | 病例1 | gam_001 | gam_002 | gam_003 | gam_004 | gam_005 | 判定 |
|---|---|---|---|---|---|---|---|---|
| ① | 主诉含时间 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 已实现 |
| ② | 现病史五要素 | 5 | 5 | 5 | 5 | 5 | 5 | 已实现 |
| ③ | 既往史+用药史 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 已实现 |
| ④ | `risk_factors ≥ 3` | 4 | 4 | 5 | 5 | 5 | 5 | 已实现 |
| ⑤ | 阳性 3 / 阴性 2 | 3/2 | 3/2 | 3/2 | 3/2 | 3/2 | 3/2 | 已实现 |
| ⑥ | `inquiry_points == 8` | 8 | 8 | 8 | 8 | 8 | 8 | 已实现 |
| ⑦ | `required == 5` | 5 | 5 | 5 | 5 | 5 | 5 | 已实现 |
| ⑧ | `is_key == 6` | **6** | **2** | **3** | **2** | **2** | **4** | **文档与实现不一致** |
| ⑨ | 金标准 == 1 | 1 | 1 | 1 | 1 | 1 | 1 | 已实现 |
| ⑩ | 陷阱 ≥ 1 | 2 | **0** | 1 | 3 | 2 | 1 | **文档与实现不一致** |
| ⑪ | 红线 == 2 | 2 | 2 | 2 | 2 | 2 | 2 | 已实现 |
| ⑫ | 知识点 == 5 | 5 | 5 | 5 | 5 | 5 | 5 | 已实现 |

**⑧/⑩ 的实质**：`CM-407`（`is_key==6`）与 `CM-408`（必有陷阱）**按字面实现会直接拒掉 5/5 通用病例**。`spec/check_cases.py` 没有实现这两条（它只查 `required ≥ 5`，`check_cases.py:161-162`），所以 6/6 PASS。这是**契约条文自身过窄**（`is_key==6` 只对病例 1 成立），不是数据错误——但必须二选一：改 `CM-407` 为「`is_key ≤ 6` 且 `lab_panels ≥ 6`」，或把 §4.2⑧ 表述改回「检查项目 6 个」而不是「6 个 is_key」。

---

## 2. §2 启动校验器规格

### 2.1 契约要求 vs 现状

| 编号 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|
| 2.1-a | 新建 `app/core/case_schema.py` 并提供 `validate_case` | `Test-Path` = **False**；全仓 grep `validate_case` = 0 处 | 未实现 |
| 2.1-b | 启动时（import/app 初始化）执行校验 | `get_case_repository()` 每次请求新建（`case_repository.py:110-112`），`server.py` 的 `lifespan` 内无任何病例库调用 | 未实现 |
| 2.1-c | 校验失败**阻断启动** | 仅 `CASE_VALIDATION_STRICT=True` 时 `raise`（`case_repository.py:42-43`），**默认值为 False**（`config.py:195`）| 部分实现 |
| 2.1-d | 错误格式＝字段路径 + 期望类型 + 实际 | 唯一错误串 `"病例文件格式无效（缺 case_id）：{path}"`（`case_repository.py:36`）| 未实现 |
| 2.1-e | 与验收第 10 条「报出缺失字段名」 | 严格档报 `"病例文件被拒绝：{path} -> {ExcType}: {msg}"`（`case_repository.py:41`），仍不含字段名 | 部分实现 |

**现状替代物（三个）**

1. `CaseValidationError`（`case_repository.py:18-19`）+ `_read_json` 的 dict/`case_id` 检查（`:35-36`）。
2. D-27 双档：`_handle_invalid_case`（`:39-45`）——严格档抛错、默认档打印 `[CaseRepository] 病例文件被拒绝：…` 并继续。实测：测试套件里能看到该行打印（临时目录 `case_broken.json`）。
3. `spec/check_cases.py`（501 行，离线体检工具，非运行时）：`python spec/check_cases.py` → `RESULT: PASS`，通用病例 5/5、胸痛 1/1。它按 §4.2 十二条查"存在性与数量"，**不产出任何 CM 码**。

### 2.2 37 个错误码逐码核对

全仓 grep `CM-\d{3}`：**命中 67 处，全部在 `spec/04-AI能力与数据契约.md` 自身**；`aic_mech/**/*.py` 与其余 spec 文档 0 处。

| 码 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|
| CM-101 | 文件不可读/编码错误 | 只有 `OSError` 被 `except` 吞（`case_repository.py:68`），无码无文案 | 未实现 |
| CM-102 | 不是合法 JSON | `json.JSONDecodeError` 同上路径，无码 | 未实现 |
| CM-103 | 顶层不是 object | `case_repository.py:35` 只抛 `格式无效` 通用串 | 未实现 |
| CM-104 | `case_id` 缺失/格式非法 | `:35-36` 检查存在性，无格式正则 | 未实现 |
| CM-105 | `case_id` 跨文件重复 | `:72` 直接覆盖 `self._cache[case_id]`，无重复检测 | 未实现 |
| CM-201 | 必填键缺失 | 无 | 未实现 |
| CM-202 | 类型不符 | 无 | 未实现 |
| CM-203 | 空值/占位词 | 无 | 未实现 |
| CM-204 | 枚举越界 | 无 | 未实现 |
| CM-205 | 数值越界 | 无 | 未实现 |
| CM-206 | 字符串长度越界 | 无（实测 108 条文案 20–57 字，未越界但无校验）| 未实现 |
| CM-207 | 数组长度不符 | 无 | 未实现 |
| CM-208 | 唯一性冲突 | 无 | 未实现 |
| CM-209 | 命名/前缀不合规 | 无 | 未实现 |
| CM-210 | 禁止占位符 | 无 | 未实现 |
| CM-301 | `emotion_scripts` 节点 ≠6 | `check_cases.py:112-113` 只查"18 条文案"，在 spec 侧 | 未实现 |
| CM-302 | `match_keywords` 全不命中 | 无 | 未实现 |
| CM-303 | `lab_panels` 不命中别名表 | 无 | 未实现 |
| CM-304 | `RED_LINE < 2` | 无（数据 6/6 均 2 条） | 未实现 |
| CM-305 | 金标准 ≠1 | 无 | 未实现 |
| CM-306 | 镜像与真值差 >20% | 无 | 未实现 |
| CM-307 | `lab_panels` 找不到对应报告 | 无 | 未实现 |
| CM-308 | `key_pos/neg` 数量不符 | 无 | 未实现 |
| CM-309 | `outcomes` 键 ≠ABCD | 无 | 未实现 |
| CM-310 | `condition` 白名单外变量 | **有等价实现**：`ged_evaluator.py:502-515` `UnknownVariableError`/`UnknownFunctionError` 在求值时抛错 | 部分实现 |
| CM-401 | 主诉缺时间量词 | 无 | 未实现 |
| CM-402 | 现病史五要素不全 | 无 | 未实现 |
| CM-403 | `medication_history` 为空 | 无 | 未实现 |
| CM-404 | `risk_factors < 3` | 无 | 未实现 |
| CM-405 | `inquiry_points ≠ 8` | 无 | 未实现 |
| CM-406 | `required ≠ 5` | `check_cases.py:161-162` 用 `>= 5`（与"恰 5"不符）| 部分实现 |
| CM-407 | `is_key ≠ 6` | 无；且按字面会拒 5/5 通用病例 | 未实现 |
| CM-408 | 无陷阱项 | `check_cases.py` 无此项 | 未实现 |
| CM-409 | `differentials ≠ 3` | `check_cases.py:357-364` 查存在与长度 | 部分实现 |
| CM-410 | `knowledge_card.items ≠ 5` | `check_cases.py:299+` 查结构 | 部分实现 |
| CM-501 | `game_extensions` 缺失（warning） | 无；且 §1.1 已声明该键不校验 | 未实现 |
| CM-502 | `difficulty_stars` 缺失补 1（warning） | 无补齐代码 | 未实现 |

**结论**：37 个错误码在运行时代码中 **0/37 实现**；只有 CM-310/CM-406/CM-409/CM-410 在 `spec/check_cases.py` 有近似替代（且是 spec 侧工具，不参与启动）。若 D-27 决定"不引入 CM 码体系"，应把 §2.3 整表标注为「设计稿，未实现」，否则验收表会指向不存在的码。

### 2.3 与验收第 10 条对应

| §4.2 条目 | 契约校验码 | 运行时实际行为 | 判定 |
|---|---|---|---|
| ①–⑫ | CM-201…CM-410 | 默认档：坏文件被跳过 + 打印路径，**不报字段名、不阻断**；严格档：报"病例文件被拒绝：path -> CaseValidationError: 病例文件格式无效（缺 case_id）" | 未实现 |

补充实证：`/api/health` 不依赖病例库，`TestClient` 实测永远返回 `{"status":"ok","version":"2.0.0","mode":"offline","storage":"sqlite"}`。所以 §2.4 的验法「重启服务后 `/api/health` 无法连接」在本实现下**不可能成立**——health 与病例加载无耦合，需改为「进程退出码 ≠ 0」或「`/api/case/list` 返回 500」。

---

## 3. §3 LLM 调用契约

### 3.1 参数（§3.1，6 个 config 键）

| 编号 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|
| 3.1-1 | `LLM_TIMEOUT_SECONDS = 8.0` | `config.py` 无此键（grep 0 处） | 未实现 |
| 3.1-2 | `LLM_CONNECT_TIMEOUT_SECONDS = 3.0` | 无 | 未实现 |
| 3.1-3 | `LLM_MAX_RETRIES = 0` | 无；`httpx.Client(...)` 默认不重试（行为等效） | 未实现 |
| 3.1-4 | `LLM_MAX_TOKENS = 300` | 无 | 未实现 |
| 3.1-5 | `LLM_TEMPERATURE = 0.6` | 无 | 未实现 |
| 3.1-6 | `LLM_DEGRADED_NOTICE = "离线模式"` | 无常量；前端文案硬编码 `'离线模式'`（`scene.js:826`）| 部分实现 |

**超时实测四处不一致**

| 位置 | 实际值 | 说明 |
|---|---|---|
| `agent_engine.py:572` | `httpx.Client(timeout=9.0)` | 多患者/多角色分支 |
| `agent_engine.py:695` | `httpx.Client(timeout=8.0)` | 单患者分支，唯一符合契约的 |
| `ged_evaluator.py:427` | `httpx.Client(timeout=10.0)` | 复盘点评，**违反 §3.2「禁止直接 httpx」** |
| `routes_config.py:143` | `httpx.Timeout(20.0, connect=5.0)` | 连通测试，§3.1 未列 |

### 3.2 唯一入口（§3.2）

| 编号 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|
| 3.2-1 | 仅 `_generate_llm_response` / `_generate_multi_agent_llm_response` 可发请求 | 两者存在（`agent_engine.py:638`、`:509`）| 已实现 |
| 3.2-2 | 失败返回 `None` 不抛 | 实测 8 处 `return None`；测试输出 `[AgentEngine LLM Error] HTTP 500: boom` 后仍出模板文案 | 已实现 |
| 3.2-3 | `GEDEvaluator` 禁止直接 httpx | `ged_evaluator.py:352` `import httpx` + `:427` 直连 | 未实现 |
| 3.2-4 | `GuardrailEngine`/`PhysiologyEngine`/`routes_*` 禁止 httpx | `routes_config.py:143` 直连（属配置探测，语义可辩，但条文未豁免）| 部分实现 |

### 3.3 失败静默降级序列（§3.3，6 步）

| 步 | 要求 | 代码证据 | 判定 |
|---|---|---|---|
| ① | 未启用则直接模板 | `agent_engine.py:227,493` `if config.is_online_llm_active()` | 已实现 |
| ② | 发请求并计时 | 有，无统一计时常量 | 部分实现 |
| ③ | 失败 → 记 `LLM_LAST_ERROR`、会话 `llm_degraded=True`、返回 None | 返回 None ✓；`llm_degraded` 字段不存在（grep 0） | 部分实现 |
| ④ | 调用方落模板 | 模板兜底路径存在，测试覆盖 | 已实现 |
| ⑤ | WS 下一帧带 `llm_mode` | WS payload 无此键（`websocket_feed.py:76-96`） | 未实现 |
| ⑥ | UI 角标「离线模式」 | 仅设置页出现（`scene.js:826`），主界面无角标 | 部分实现 |

### 3.4 绝不交给 LLM 的判定（G1–G10）

| # | 判定 | 代码证据 | 判定 |
|---|---|---|---|
| G1 | 六维雷达分 | `ged_evaluator.evaluate()`；实测返回 6 键 `critical_first_aid…clinical_efficiency` | 已实现 |
| G2 | 红线药物 | `physiology_engine.py:155` `apply_prescription_red_line` + `:231` 先查 `prescription_rules`；实测阿司匹林 → `is_contraindicated=true` + 致命红线文案 | 已实现 |
| G3 | 死亡/骤停 | `physiology_engine` 状态机；实测 480 s 挂机进 `S3_PERICARDIAL_EFFUSION` | 已实现 |
| G4 | 离院结局 | `emotion_engine`；实测 12 次无关提问 → `leave_reason="leave_immediate"` | 已实现 |
| G5 | 检查结果数值 | `lab_panels[].result_text` 原文经 `guardrail_engine` 直出，无改写路径 | 已实现 |
| G6 | TAT 与耗时 | `lab_panels[].tat_seconds` / `exam_options[].duration_seconds` 读 JSON | 已实现 |
| G7 | 情绪数值 | `EmotionEngine`（621 行，实测 tick 落地） | 已实现 |
| G8 | 三档文案选择 | `agent_engine.py:20-21` 读 `EMOTION_SCRIPT_TRUST_HIGH/MID`（`config.py:219-220`）；测试 `test_trust_bands_pick_high_mid_low_scripts` | 已实现 |
| G9 | `outcomes[].condition` 求值 | `ged_evaluator.py:489-700` 自研 tokenizer/parser，未知变量抛 `UnknownVariableError` | 已实现 |
| G10 | 说明书内容（后端白名单裁剪） | 无 handbook 端点；说明书为前端硬编码字符串（`scene.js:237-258`） | 未实现 |

### 3.5 输出后置校验（§3.6 三道闸）

| 编号 | 要求 | 代码证据 | 判定 |
|---|---|---|---|
| A1 | 长度 40–300 字，越界丢模板 | grep `must_not_say`/长度闸 = 0 处 | 未实现 |
| A2 | 诊断名词表 + 剂量正则拦截 | 无 | 未实现 |
| A3 | 多角色必须返回 `{patient, family}` 合法 JSON | 无结构校验 | 未实现 |

### 3.6 可观测字段（§3.7）

| 编号 | 要求 | 代码证据 | 判定 |
|---|---|---|---|
| 3.7-1 | `/api/health` 含 `llm_provider`/`llm_model`/`llm_last_error` | 实测 4 键：`status/version/mode/storage`（`server.py:80`） | 未实现 |
| 3.7-2 | WS `llm_mode` | 无 | 未实现 |
| 3.7-3 | `DialogueMessage.role_title` 区分在线/离线 | 字段存在（`entities.py`），但恒取人设名（`agent_engine._patient_role_title`），无「（模板文案）」分支 | 部分实现 |

---

## 4. §4 离线兜底矩阵

| 能力 | 契约要求 | 代码证据 | 判定 |
|---|---|---|---|
| 患者对话 · 离线入口 | 关键词 + `inquiry_points` 命中表 | `agent_engine` 关键词路由 + 模板 | 已实现 |
| 患者对话 · 离线输出 | `emotion_scripts[节点][档位]` 或 `dialogue_scripts.patient.*` | 实测 6/6 病例可产出；无脚本病例回落旧台词（`test_case_without_emotion_scripts_keeps_legacy_lines`） | 已实现 |
| 患者对话 · 在线 | ≤300 字、8 s | 生成路径存在，超时 9.0/8.0 两值 | 部分实现 |
| 辅助判读 · 离线 | `conclusion` + `clinical_hint` 原文 | `guardrail_engine` 直出报告字段 | 已实现 |
| 辅助判读 · 在线 | 上下文解读 + 追问建议 | 无对应 LLM 分支 | 未实现 |
| 自动复盘 · 离线 | 六维 + `fatal_errors` + `review_items` | 实测 `radar_scores` 6 键 + `fatal_errors` + `outcome.review_items` | 已实现 |
| 自动复盘 · 在线 | 生成式点评 ≤200 字 | `ged_evaluator._generate_llm_clinical_review`（`:341-443`）| 已实现 |
| 分数一致性 | 全模式由 `ged_evaluator` 产出 | 唯一产出点 | 已实现 |

### 4.1 离线断言 O-1…O-7

| # | 断言 | 实测 | 判定 |
|---|---|---|---|
| O-1 | 无 Key 启动 → `mode=="offline"` | `GET /api/health` = `{"mode": "offline", …}` | 已实现 |
| O-2 | 走完病例 1 全流程无 5xx | `start → INQUIRY → PHYSICAL_EXAM → MEDICATION → eval/submit` 全 200，无 5xx | 已实现 |
| O-3 | 六维齐全且均 > 0 | 键齐全 ✓；`start → submit`（零操作）实测 `critical_first_aid=0.0, differential_thinking=0.0, physician_patient_comm=0.0` | **部分实现** |
| O-4 | `?node=E1_CONSULT&tier=high` 返回非空 | `/api/case/{id}/emotion-script` 端点不存在（404） | 未实现 |
| O-5 | `tab=case|symptoms|management` 全部 200 | `/api/case/{id}/handbook` 不存在；说明书是静态 DOM（两页各有 3 个页签，`test_static_assets.py:233-245`） | 未实现 |
| O-6 | 导出含 `clinmind.record.v1` | `/api/record/{sid}/export` 不存在 | 未实现 |
| O-7 | 断网 `llm_mode` 由 online 变 degraded | WS 无 `llm_mode` | 未实现 |

**O-3 的取证边界**：本次只跑了「开单即交卷」的极简会话。三个 0.0 说明雷达分在无对应行为时**不保证 > 0**；「走完全流程后均 > 0」需要一次完整带问诊/体查/用药的会话才能证实——我没有跑完，故不给"已实现"，也不断言失败。

---

## 5. §5 会话状态扩展（10 个字段）

`_build_snapshot` 实际顶层键（`session_manager.py:201-216`）：`session_id, case_id, student_name, start_time, virtual_seconds, is_active, is_finished, terminal_reason, emotion, actions, dialogues, agent_state, physiology, assessment`。

| 字段 | 契约持久化位置 | 实际位置 | 判定 |
|---|---|---|---|
| `patient_anxiety` | `snapshot.agent_state.patient_anxiety` | `entities.py:71` 字段存在；`agent_state` 整包落库 ✓ | 已实现 |
| `emotion_log` | `snapshot.emotion_log`（顶层） | 无顶层键；引擎内 `delta_log`/`visible_delta_log`（`emotion_engine.py:233-241`）+ `dump().history` | 文档与实现不一致 |
| `leave_reason` | `snapshot.leave_reason` | 在 `snapshot.emotion.leave_reason`（`dump()` 15 键之一） | 文档与实现不一致 |
| `outcome_code` | `snapshot.outcome_code` | 无；结局走 `EvaluationResponse.outcome.code`（`schemas.py:135`） | 文档与实现不一致 |
| `refused_exams` | `snapshot.agent_state.refused_exams` | 全仓 grep 0 处 | 未实现 |
| `anxiety_cap_seconds` | `snapshot.agent_state.*` | 等价量 `critical_anxiety_hold_s` 在 `emotion.dump()` | 文档与实现不一致 |
| `negative_streak` | `snapshot.agent_state.*` | 等价量 `neg_interaction_streak` | 文档与实现不一致 |
| `trust_low_seconds` | `snapshot.agent_state.*` | 等价量 `departure_watch_s` | 文档与实现不一致 |
| `llm_degraded` | `snapshot.llm_degraded` | 无 | 未实现 |
| `schema_version` | `snapshot.schema_version` + 列 | 无 | 未实现 |

**迁移方案（§5.3 `_upgrade_snapshot`）**：函数不存在。实际兼容靠两处兜底：`EmotionEngine.load(data.get("emotion"))` 接受 `None` 建默认引擎（`session_manager.py:152`，`emotion_engine.py:554-583`），以及 `terminal_reason` 的老数据回落（`:149-151`，`is_finished` 为真则补 `"finished"`）。→ **部分实现**（能读老数据，但不按 `schema_version` 分支，也不写回升级后的快照）。

一致性判断：5 个字段只是**换了名字/位置**（数据没丢），2 个真缺（`refused_exams`、`llm_degraded`），1 个改名（`outcome_code`），1 个完全未落（`schema_version`）。若坚持 §5.2 的字段路径，需在 `_build_snapshot` 增 4 个派生键做投影——成本很低，但**不要把引擎内部名改成文档名**（`dump()` 已被 15 键持久化契约和测试依赖）。

---

## 6. §6 API 变更清单（16 条）

现有全部路由实测（`@router`/`@app` 全量 grep，共 13 条）：`/api/case/{list,detail/{id},start,session/status,session/{sid}/workspace,session/{sid}/pause}`、`/api/action/execute`、`/api/config/llm`(GET/POST)、`/api/eval/{submit,report/{sid},golden_dag}`、`/ws/vitals/{sid}`、`/api/health`、`/`。

| # | 类型 | 方法 路径 | 代码证据 | 判定 |
|---|---|---|---|---|
| 1 | 新增 | `GET /api/emotion/state` | 无 `routes_emotion.py`；404 | 未实现 |
| 2 | 新增 | `GET /api/emotion/log` | 同上 | 未实现 |
| 3 | 新增 | `GET /api/case/{id}/handbook` | 无（`routes_case.py` 仅 6 路由） | 未实现 |
| 4 | 新增 | `GET /api/record/{sid}/export` | 无 `routes_record.py` | 未实现 |
| 5 | 新增 | `GET /api/case/session/{sid}/closure` | 无 | 未实现 |
| 6 | 新增 | `GET /api/case/{id}/exam-options` | 无（数据在 JSON，未开端点） | 未实现 |
| 7 | 新增 | `GET /api/case/{id}/prescription-form` | 无 | 未实现 |
| 8 | 新增 | `GET /api/case/{id}/knowledge-card` | 无（知识点只随 eval 报告返回） | 未实现 |
| 9 | 新增 | `GET /api/case/{id}/emotion-script` | 无 | 未实现 |
| 10 | 修改 | `GET /api/case/detail/{id}` | 仍只回 8 键且含 `game_extensions`（`routes_case.py:53-63`），无新键裁剪 | 未实现 |
| 11 | 修改 | `GET /api/case/session/status` | 实测 17 键，含 `patient_anxiety/family_anxiety/trust_trend/anxiety_trend/refuse/terminal_reason`（`:110-115`）；无 LLM 字段 | 部分实现 |
| 12 | 修改 | `POST /api/action/execute` | 实测响应含 `emotion_applied`；`report.rule_id` 存在（`routes_action.py:263`）；无拒绝分支、无 409 结案守卫 | 部分实现 |
| 13 | 修改 | `GET /api/eval/report/{sid}` | 实测含 `outcome`（`code/label/title/narration/...`）与 `knowledge_card`；字段名不是 `outcome_code`，无 `mode` | 部分实现 |
| 14 | 修改 | `GET /api/health` | 仍 4 键（`server.py:80`） | 未实现 |
| 15 | 修改 | `WS /ws/vitals/{sid}` | 实测 6 个情绪字段齐全；无 `llm_mode`、无 `refused_exams`、仍手写 dict（未过 `VitalsStreamPayload`） | 部分实现 |
| 16 | 新增 | `POST /api/case/session/{sid}/close` | 无 | 未实现 |

### 6.1 统一错误体（7 码）与本实现对照

现状：所有错误都是 `HTTPException(detail="...")` 纯字符串。

| HTTP | 契约 code | 现实现 | 判定 |
|---|---|---|---|
| 404 | `SESSION_NOT_FOUND` | `detail="Session not found"`（`routes_eval.py:76`） | 未实现 |
| 404 | `CASE_NOT_FOUND` | `detail="Case not found"`（`routes_case.py:52`） | 未实现 |
| 409 | `SESSION_FINISHED` | 无守卫（已结案仍可打 `/action/execute`） | 未实现 |
| 403 | `KNOWLEDGE_LOCKED` | 无端点 | 未实现 |
| 409 | `NO_OUTCOME` | 无端点 | 未实现 |
| 422 | `INVALID_TAB` / `INVALID_TIER` / `INVALID_DRUG` | Pydantic 自动 422，code 为 FastAPI 默认结构（实测 `{"detail":[{"type":"missing",…}]}`） | 未实现 |
| — | 统一结构 `{code,message,field}` | 不存在 | 未实现 |

**实测取证**：`POST /api/action/execute` 只给 `{"session_id":…,"action_type":"INQUIRY","payload":{…}}` 会返回 422 `{"detail":[{"type":"missing","loc":["body","content"],…}]}`——证明请求体字段名与契约示例不一致（契约写 `payload`，实际是 `content` + `params`，见 `schemas.py:24-29`）。这不是缺陷（`content` 是既有约定），但**§6.2 的请求示例必须改成实际字段名**，否则照抄会 422。

---

## 7. §7 数据迁移

| 编号 | 要求摘要 | 代码证据 | 判定 |
|---|---|---|---|
| 7.1 | 目标态＝24 键新 schema | 6/6 病例已满足（§1.1） | 已实现 |
| 7.2 | `tools/migrate_cases_v2.py` 一次性可重跑脚本 | 无 `tools/` 目录；`Get-ChildItem -Filter *migrat*` = 0 | 未实现 |
| 7.3 | 幂等性（重跑不产生重复/不覆盖人工改动） | 无脚本可验 | 未实现 |
| 7.4 | `game_extensions` 保留、不校验、不作真值 | 5 个 gam 文件仍带该键；`case_repository.py:96` 仍读它作 `core_teaching` 兜底 | 已实现 |
| 7.5 | 回滚方式 | 无脚本；只能靠 git 回退数据文件 | 未实现 |
| 7.6 | 迁移验收（24 键 + `check_cases` PASS） | `python spec/check_cases.py` → `RESULT: PASS`（6/6） | 已实现 |

**判断**：迁移**目的已达成，手段没落地**。数据是 case-dev 直接改成的 24 键，没有脚本可复现、没有幂等保证、没有回滚。若项目要交付"可重跑的迁移"，仍需补脚本；若接受"数据即迁移"，则应把 §7.2/7.3/7.5 标为「不适用」并说明数据由人工维护——**不要留着一份跑不起来的脚本规格**。

---

## 8. §8 冲突清单 C-01…C-16 的当前状态

| # | 冲突（摘要） | 现在 | 判定 |
|---|---|---|---|
| C-01 | 坏病例静默 `continue` | 改双档：严格抛错、默认跳过并打印（`case_repository.py:39-45,68-71`）；默认仍不阻断 | 部分实现 |
| C-02 | 只校验 dict + `case_id` | 未变（`:35-36`） | 未实现 |
| C-03 | prompt 硬编码孙建国/李秀梅 | 已模板化 `_persona_scene`（`agent_engine.py:68-90`），但**保留硬编码兜底串**（`:73-77`）；6/6 病例有 `name` 故不触发 | 部分实现 |
| C-04 | `agent_anxiety` 实为家属焦虑 | 已加 `patient_anxiety`（`entities.py:71`、`schemas.py:101-106`、`routes_case.py:110-115`），旧字段保留并注明 DEPRECATED | 已实现 |
| C-05 | 超时 9.0 vs 8.0，无统一常量 | 未变，且新增 10.0（`ged_evaluator.py:427`） | 未实现 |
| C-06 | 夹层专用红线表 | 判定入口已改为 `prescription_rules`（`physiology_engine.py:35-36,231`），关键词表降为兜底 | 已实现 |
| C-07 | `case_id` 同时是行为开关 | 改为 `_detect_high_risk_aortic()`：先读 `hemodynamic_rules.high_risk_aortic`，`case_id` 仅兜底（`:79-97`） | 已实现 |
| C-08 | 通用用药无红线分支 | 有 `apply_prescription_red_line` + `severity` 分流（`:155-175`） | 已实现 |
| C-09 | `rupture_countdown` 恒 120 | 改为 `lethal_countdown_seconds()`（`:30,93,322`），读 `hemodynamic_rules` 或 `config.PHYS_RUPTURE_TO_EFFUSION_S` | 已实现 |
| C-10 | 体查硬编码关键词 | 已数据驱动（`guardrail_engine.py:23,87-110,168,228`），保留"拒绝编造"兜底 | 已实现 |
| C-11 | 急救分支硬编码"心胸外/李秀梅" | 未变（`routes_action.py:306-312`） | 未实现 |
| C-12 | `extensions.get("core_teaching")` 恒空 | 未变（`routes_case.py:162`），回落到 `case_data.get("core_teaching")` 仍空 | 未实现 |
| C-13 | WS 缺 `llm_mode`/`patient_anxiety`/`refused_exams` | 已补 `patient_anxiety/family_anxiety/trend/refuse/terminal_reason`；缺 `llm_mode`、`refused_exams` | 部分实现 |
| C-14 | WS 手写 dict，`VitalsStreamPayload` 形同虚设 | 仍手写（`websocket_feed.py:76-96`），模型未实例化 | 未实现 |
| C-15 | `persist` 只存 8 个生理字段 | 已存 `emotion.dump()` + `terminal_reason`（`session_manager.py:211-212`），情绪内部量随之落库 | 已实现 |
| C-16 | 连通测试 20 s 与 8 s 契约不一致 | 仍 20.0（`routes_config.py:143`），未在文档标注例外 | 文档与实现不一致 |

**C-14 附带风险**：WS 手写 dict 里 `"vitals": vitals.model_dump()` 而 `VitalsStreamPayload.vitals` 是 `VitalsVector`，一旦有人改成 `VitalsStreamPayload(**payload)` 会因缺 `timestamp` 之类字段直接抛错——这就是契约类闲置的代价。

---

## 9. 未实现项详述（按影响排序）

1. **§2 整套字段级校验（37 码）不存在**。验收第 10 条在默认配置下**不成立**：删掉一个 `is_key` 项，服务照常启动，`/api/health` 照常 200。这是本文件最硬的缺口，且与 D-27 的双档设计只差"把严格档设为默认 + 补字段级规则"。
2. **§3 的 8 秒契约没有任何常量承载**（`LLM_TIMEOUT_SECONDS` 不存在），实测 4 个不同超时值（8/9/10/20 s）。演示环境一旦走在线模式，患者对话最坏 9 s，复盘 10 s。
3. **§3.2「GEDEvaluator 禁止直接 httpx」被违反**（`ged_evaluator.py:352,427`）。这意味着"复盘点评"这条内容不在唯一入口的失败兜底里——LLM 挂掉时它自己 catch（`:441-443`），行为与契约的"统一返回 None"不同。
4. **§3.6 三道闸（A1/A2/A3）全缺**：在线模式下模型若说出"主动脉夹层"或"阿司匹林 300mg"，没有任何拦截。这是**教学内容安全事故**级别，不是体验问题。
5. **§6 九个新端点全缺**（情绪查询/手册/导出/结案/目录/处方单/知识卡/文案）。离线断言 O-4/O-5/O-6 因此全部不可通过，"演示前跑通断网全流程"只能靠前端硬编码文案兜着。
6. **§3.7 可观测字段缺**：`/api/health` 无法区分在线/离线明细，WS 无 `llm_mode`，运维与评委都看不出当前是哪条路径。
7. **§7 迁移脚本缺失**：数据已升级但不可复现、不可回滚。
8. **§6 统一错误体缺失**：前端无法按 `code` 分支（只能匹配中文串），且已结案会话仍能继续调动作接口。

## 10. 需要裁决的契约缺陷（不是代码问题）

| # | 缺陷 | 建议 |
|---|---|---|
| D1 | `CM-407 is_key==6` 与 §4.2⑧「检查 6 项」矛盾，按字面拒 5/5 通用病例 | 改为 `lab_panels.length ≥ 6` 且 `is_key ≤ 6` |
| D2 | `CM-408 必须有陷阱`：gam_001 无陷阱项 | 要么给 gam_001 加一条陷阱检查，要么把该码降为 warning |
| D3 | §6.1 请求示例用 `payload`，实际字段是 `content`+`params` | 改示例 |
| D4 | §5.2 的 4 个字段名与引擎内部名不同（`critical_anxiety_hold_s` 等） | 二选一：快照投影 4 键，或改文档名并注明"存于 `emotion` 子对象" |
| D5 | §2.4 验法依赖 `/api/health` 无法连接，但 health 不依赖病例库 | 改为"进程非零退出"或 `/api/case/list` 500 |
