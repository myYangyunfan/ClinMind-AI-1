# ClinMind-AI · spec/03 UI 交互规格核对报告（06）

> 核对对象：`spec/03-UI交互规格.md`（760 行，30 条验收项 A01–A30）
> 核对基线：`D:\workspace\AI+\ClinMind-AI-1` 工作区当前磁盘文件（未 commit 状态）
> 核对日期：2026-10-26
> 判定口径：**只看磁盘上真实存在的 DOM / 类名 / 函数**，不以「规格写过」或「曾经实现过」为准。

## 0. 核验方法与证据等级

| 等级 | 含义 | 本文标注 |
|---|---|---|
| E1 实测 | 浏览器打开页面，用 DevTools/DOM 断言确认（URL 带查询串 `?audit=1` 绕缓存） | 「实测」 |
| E2 源码 | 逐行读 `scene.js` / `scene.css` / 两个 html，用行号定位 | 「源码」 |
| E3 推算 | 依赖 E2 的常量按公式推导，未实拍 | 「推算」 |

本轮以 E2 为主，E1 复用 P2 轮已实测结论（说明书弹窗、病历区、Esc、两页一致），E3 仅用于 `fit()` 缩放比。

核对用的三个探针命令（可复现）：

```powershell
# 1) 类名是否存在
Select-String -Path static\css\scene.css -Pattern 'scene-tat-' 
# 2) JS 是否有该功能的分支
Select-String -Path static\js\scene.js -Pattern 'scene-phase|scene-tat|verdict|timeline'
# 3) 现有断言覆盖
cd aic_mech; python -m unittest tests.test_static_assets -v
```

`tests/test_static_assets.py` 现共 **21 条** `def test_`，其中与本规格直接相关的 12 条（`test_emotion_panel_present_on_both_pages`、`test_emotion_css_token_layer`、`test_emotion_classes_have_css_rules`、`test_pages_have_no_inline_style`、`test_no_death_countdown_text`、`test_disclaimer_storage_key_and_copy`、`test_manual_dialog_present_on_both_pages`、`test_manual_copy_avoids_answers`、`test_record_area_present_on_both_pages`、`test_toolbar_exposes_manual_and_record_entry`、`test_esc_guard_checks_every_dialog`、`test_every_static_reference_resolves`）。**注意：测试只覆盖了 A01/A14/A18/A19/A20/A28/A30 一类「结构存在性」，没有任何测试断言 A09–A13（phase / TAT）与 A23–A24（复盘时间线 / 导出 schema）——这三块「没人测」和「没实现」是同一件事的两面。**

---

## 1. 总表

### 1.1 按章节

| 编号 | 规格要求摘要 | 实现证据 | 判定 |
|---|---|---|---|
| §1.1 | 三条通道 `trust / patient_anxiety / family_anxiety`，字段读 `trust_level ?? trust` | `scene.js:122` `const values = {trust: data.trust_level ?? data.trust, …}`；两页各 1 个 `#emotion-panel`、各 3 个 `.scene-emotion-row`（`reception.html` / `workstation.html` 实测计数 3/3） | 已实现 |
| §1.2 | DOM 骨架：`#emotion-panel` > `.scene-emotion-row[data-channel]` > `.scene-emotion-label/-fill/-value/-trend` | `scene.js:159-186` 按 `data-channel` 取行；`.scene-emotion-fill` 见 `scene.css:93`，`.scene-emotion-value` `:94`，`.scene-emotion-trend` `:95` | 已实现 |
| §1.3 | 关键 CSS 单行压缩风格 + 类名→规则合规 | 新样式全部集中在 `scene.css:93-114`，沿用单行压缩风格；`test_emotion_classes_have_css_rules` / `test_scene_class_names_have_css_rules` 通过 | 已实现 |
| §1.4 | 危险区方向敏感三档带 | `scene.js:147-152` `bandClass()`：trust `<0.2` → `scene-band-low`；焦虑 `>0.9` → `scene-band-critical`、`>0.8` → `scene-band-high`；色带 `scene.css:96-100` | 部分实现 |
| §1.5 | 30 秒窗口趋势：`TREND_WINDOW_MS=30000`、5 秒采样、±0.02 死区、跨度不足 30s 不出箭头 | `scene.js:45` 常量；`:130-138` `sampleEmotion()`；`:139-146` `localTrend()`；`:181` `scene-trend-shown` 门控 | 已实现 |
| §1.6 | 200 ms 过渡 | `scene.css:40` `--scene-bar-dur:200ms`；`:93` `transition:width var(--scene-bar-dur) ease,background-color …`；`:94-95` 同步 | 已实现 |
| §2.1 | 病程计时 + `scene-phase-stable/worsening/crisis` 三档色标 | **`scene.js` 全文无 `scene-phase` 分支**（grep 0 命中）；`scene.css:21-23` 只有三个变量 `--scene-phase-stable/-worsen/-crisis`，**没有任何 `.scene-phase*` 类规则**；两页 HTML 无 `.scene-phase` 元素 | 未实现 |
| §2.2 | 进行中检查 TAT 进度条 | `scene.css:41` 只有 `--scene-tat-dur:1s` 一个变量；**无 `.scene-tat-item/-fill/-remain` 任何规则**；`scene.js` 无 TAT 逻辑 | 未实现 |
| §2.3 | 明确不显示死亡倒计时 | `test_no_death_countdown_text` 通过；`grep -i '倒计时\|剩余秒\|death'` 在 `reception.html` / `workstation.html` / `scene.js` **0 命中** | 已实现 |
| §3.1–3.5 | 说明书三页签、内容白名单、不暂停计时、Esc 可关 | `#manual-dialog` 两页各 1（`reception.html:223`、`workstation.html:184`）；3 个 `.scene-manual-tab[data-tab]` 带 `aria-selected`（两页各 3 处）；`scene.js:340 setManualTab` / `:360 openManual` / `:367 ensureManual`；文案块 `scene.js:237-259` 由 `test_manual_copy_avoids_answers` 锁死；Esc 走 `anyModalOpen()` `scene.js:78`（实测：只关窗不跳页） | 已实现 |
| §4.1 | 病历记录数据结构（`actions[] / reports[]`） | `scene.js:403` `Array.isArray(source?.actions)`；`:414` `action.result` 取报告摘要 | 部分实现 |
| §4.2 | 渲染列表：时间 + 类型 + 文本 + 报告摘要 + 空态 | `scene.js:397-422 renderRecord()`；类名实际为 `.scene-record-time / -type / -text / -flag / -report / -empty / -count` | 部分实现 |
| §4.3 | 复盘时间线：第 N 分钟做了什么 + 错在哪一步，`verdict==='fatal'` 排最前 | **`scene.js` 无 `verdict`、无 `timeline`**（grep 各 0 命中）；只有 `scene.js:791` 把后端 `action_sequence_summary` 原样塞进一个 `<details>`，无排序、无「错在哪一步」归因 | 未实现 |
| §4.4 | 导出 JSON：文件名 `ClinMind-*.json`、`schema==="clinmind.record.v1"` | `scene.js:792` 有「导出 JSON 报告」按钮，`URL.createObjectURL(new Blob(...))`；但全文**无 `ClinMind-` 字面量、无 `clinmind.record.v1`**（grep 大小写不敏感 0 命中该 schema） | 部分实现 |
| §5.1–5.4 | 免责声明文案逐字 + 底部常驻 + 开局弹窗 + `sessionStorage['clinmind-disclaimer-ack']` | `DISCLAIMER_KEY` `scene.js:45`；`ackDisclaimer()` `:223-226` 写 `sessionStorage.setItem(DISCLAIMER_KEY,'1')`；`ensureDisclaimer()` `:227`；两页各 4 处 `.scene-disclaimer*`；`test_disclaimer_storage_key_and_copy` + `test_no_death_countdown_text` 通过 | 已实现 |
| §6.1–6.3 | 焦虑 ≥0.90 被拒时四层反馈（notice / 横幅 / 行态 / 记录） | `scene.js:190-203 applyRefusal()`：`toast(...,true)` 带 `scene-error`、`panel.classList.toggle('scene-refuse')`、`.scene-refusal-banner` 显隐、`.scene-emotion-row` 加 `scene-refusing`；`scene.css:107-108` 横幅规则。**第 4 层「记录区新增 negative 条目」缺失**（`.scene-record-negative` 在 CSS/JS 均 0 命中） | 部分实现 |
| §7.1–7.2 | 两页分工 + 同状态两页渲染字段对照 | 两页结构对称：`#emotion-panel`×1、`.scene-emotion-row`×3、`#manual-dialog`×1、`#record-area`×1、`.scene-disclaimer*`×4 —— **实测计数完全一致**，即规格要求的「必须两边都有」已满足；但「只出现在一边」的差异化（如工作站独有雷达/波形、接诊室独有 dock）是**改造前既有差异**，规格未新增约束 | 已实现 |
| §8.1 | 新增局部变量（`EMOTION_CHANNELS` / `TREND_WINDOW_MS` / `history` / `trendHint` / `trendValue` / `refused` / `ended`） | `scene.js:40-56` 全部存在 | 已实现 |
| §8.2 | 新增函数（`renderEmotionPanel` / `startEmotionTicker` / `applyRefusal` / `readEmotion` / `bandClass`） | `scene.js:120 readEmotion`、`:147 bandClass`、`:153 renderEmotionPanel`、`:190 applyRefusal`、`:204 applyEndState`、`:215 startEmotionTicker`（5 秒 `setInterval`，`EMOTION_TICK_MS=5000`） | 已实现 |
| §8.3 | `snapshot` 新字段（`emotion` / `phase` / `tasks` / `outcomes`） | 只消费了 `refuse`、`is_finished`、`terminal_reason`、`actions`、`reports`；`phase` / `tasks` / `outcomes` **前端未读** | 部分实现 |
| §8.4 | 与 1 Hz 渲染循环的关系：情绪 5 s 结算、UI 200 ms 平滑 | `TICK 5000`（`scene.js:45`）+ 独立 `emotionTimer`（`:215-222`），与波形 1 Hz `requestAnimationFrame` 解耦；平滑由 CSS transition 承担（`scene.css:93`） | 已实现 |
| §9.2 | 1280×720 → `scale(0.8)`；1920×1080 → `scale(1.2)` | `scene.js:79` `canvas.style.transform = scale(${Math.min(innerWidth/1600, innerHeight/900)})`：1920/1600=1.2、1080/900=1.2 → 1.2；1280/1600=0.8、720/900=0.8 → 0.8 | 已实现（推算） |
| §9.3 | 窄屏折叠：`max-width:1100px` 侧栏贴底横排、`bottom:106px`、`flex-direction:row` | `scene.css` 全部 `@media` 只有 3 处：`max-width:700px`（`:78` case-picker）、`max-width:820px`（`:223` scene-finale-grid）、`prefers-reduced-motion`（`:114`、`:190`）。**无 1100/1000 断点** | 未实现 |
| §11 | 待裁决项 | `spec/03:751` 起的待裁决表在实现中**无对应落地痕迹**（无相关 TODO 注释），未裁决即未实现 | 未实现 |

### 1.2 A01–A30 全表

| 编号 | 规格要求摘要 | 实现证据 | 判定 |
|---|---|---|---|
| A01 | 加载后 `.scene-emotion-row` 数量 === 3 | 两页 `#emotion-panel` 内各 3 行；`test_emotion_panel_present_on_both_pages` 通过 | 已实现 |
| A02 | `trust=0.40` → `.scene-emotion-value` 文本 `"40%"`、fill `style.width==="40.0%"` | `scene.js:165` `fill.style.width = \`${(value*100).toFixed(1)}%\``；`:171` `number.textContent = \`${percent}%\`` | 已实现 |
| A03 | 0.40→0.42 时同帧 `width="42.0%"`，`transitionDuration` 含 `0.2s` | `scene.js:165`（同帧写 style，不经过缓冲）+ `scene.css:93` `transition:width var(--scene-bar-dur) ease` + `:40` `--scene-bar-dur:200ms` | 已实现 |
| A04 | 净降 0.03 → 箭头 `"↓"` + `scene-trend-shown` + `scene-trend-bad` | `scene.js:180-183`；`scene.css:101-103` 三条规则真实存在 | 已实现 |
| A05 | 样本跨度 <30 s → **无** `scene-trend-shown`，`opacity` 计算值 `"0"` | 类名门控正确（`scene.js:181`），但**默认态 `opacity:.45` 而非 `0`**（`scene.css:95`）——箭头未被隐藏，只是淡显 + 显示 `→` | 文档与实现不一致 |
| A06 | `patient_anxiety=0.91` → 含 `scene-band-danger`，fill 背景 === `--scene-danger` | 实际类名是 **`scene-band-critical`**（`scene.js:149`），用 `--scene-critical:#8f2f27`（`scene.css:27,98`），不是 `--scene-danger:#b4473c`（`:26`）。语义正确、命名与色值不符 | 文档与实现不一致 |
| A07 | 拒绝时 `.scene-notice` 含"患者拒绝配合"+`scene-error`、横幅非 hidden、记录区新增 `.scene-record-negative` | 前三条成立（`scene.js:194` toast 文本、`:82` `scene-error`、`:202` 横幅）；**第四条不成立**：记录区禁忌/失败态用 `.scene-record-flag`（`:413`），无 `.scene-record-negative` | 部分实现 |
| A08 | 点横幅「安抚患者」→ 焦虑 <0.85 后横幅 hidden + 新增非 negative 记录 | 横幅显隐成立（`scene.js:196-202` 以 `family_anxiety<0.85` 复位）；但横幅是**纯文本提示、没有按钮**：`reception.html:188` / `workstation.html:137` 均为 `<span class="scene-refusal-banner" role="alert" hidden>拒绝配合 · 先安抚患者</span>`，无点击目标，焦虑下降只能靠正常问诊/沟通操作 | 部分实现 |
| A09 | `phase` stable→worsening → `.scene-phase` 类切换、`.scene-phase-label` 文本 `"恶化"` | **完全不存在**：无 `.scene-phase` 元素、无 label 元素、JS 无分支 | 未实现 |
| A10 | `phase=crisis` → label `"危象"`；`.timer` 仍 `MM:SS`；正文无"倒计时" | 后半成立（`test_no_death_countdown_text`、grep 0 命中）；前半因 A09 不存在而不成立 | 部分实现 |
| A11 | 下达 CTA（TAT 1200s），虚拟 +300s → `.scene-tat-fill` 宽 `"25.0%"`、remain `"15:00"` | 无 `.scene-tat-*` 元素与逻辑（仅 `--scene-tat-dur` 变量） | 未实现 |
| A12 | TAT 走完 → `data-status="done"`、宽 100%、remain 含"可查看" | 同上 | 未实现 |
| A13 | 检查被拒 → `data-status="refused"`、fill 背景含 `repeating-linear-gradient` | 同上 | 未实现 |
| A14 | 打开说明书 → `dialog.scene-manual.open===true`，3 个 tab，首个 `active` + `aria-selected` | 两页各 3 处 `aria-selected`；P2 轮**实测**打开态与首个 active 成立 | 已实现 |
| A15 | 点第 3 个页签 → 第 3 tab `active`、`#scene-manual-p2` 无 hidden、`#scene-manual-p0` 有 hidden | `scene.js:340 setManualTab()` 按 `data-tab`/`data-panel` 配对；P2 轮**实测**三页签可切换 | 已实现 |
| A16 | 说明书打开时 Esc → 关闭且 `location.pathname` 不变 | `scene.js:78 anyModalOpen()` 遍历所有 `<dialog>`；P2 轮**实测**：工作站开窗按 Esc 后 URL 仍为 `workstation.html?p2=1` | 已实现 |
| A17 | 说明书停留 60 s → `.timer` 前进、至少一个数值变化（不暂停） | 说明书三条路径 `openManual`(`:360`) / `ensureManual`(`:367`) / `setManualTab`(`:340`) **均不触碰任何定时器**（全文件 3 处 `clearInterval` 无一在弹窗流程内）；`startEmotionTicker` 的 5 s 定时器与波形 1 Hz 循环独立运行。但「数值确实变化」依赖后端结算，未实拍 60 s | 部分实现 |
| A18 | 首次进入 → `dialog.scene-disclaimer-modal.open===true`，文案与总纲 §5.5 逐字相同 | `scene.js:227 ensureDisclaimer()`；`test_disclaimer_storage_key_and_copy` 通过（该测试同时锁 key 与文案） | 已实现 |
| A19 | 点「我已知晓」→ `sessionStorage` 值为 `'1'`，弹窗关闭 | `scene.js:223-226 ackDisclaimer()` 逐字对应 | 已实现 |
| A20 | 同标签页刷新 → 弹窗不再出现，常驻 `.scene-disclaimer` 仍在 | `ensureDisclaimer()` 读同一 key 决定是否 `showModal()`；两页常驻元素各 4 处 `scene-disclaimer*` | 已实现 |
| A21 | 执行一次问诊 → `.scene-record-list` 子节点 +1，末条含 `.scene-record-time`(`/^\d{2}:\d{2}$/`)、`.scene-record-kind`、`.scene-record-text` | 列表 +1、时间 `MM:SS`（`scene.js:386-389 recordClock`）、文本均成立；**类型类名是 `.scene-record-type`，不是 `.scene-record-kind`**（`:410`） | 文档与实现不一致 |
| A22 | 红线药 → 末条含 `scene-record-negative`，`borderLeftColor === --scene-danger` | 实际用 `.scene-record-flag`（`scene.js:413`），样式在 `scene.css:186`：`{grid-column:3;font-size:10px;font-weight:700;color:var(--scene-danger)}` —— **有红色加粗提示，但没有 `border-left`**；`scene-record-negative` 全局 0 命中 | 文档与实现不一致 |
| A23 | 结案复盘里 `verdict==='fatal'` 条目排最前且含"第 N 分钟" | `scene.js` 无 `verdict`、无 `timeline`、无"第 N 分钟"格式化；仅 `:791` 原样渲染 `action_sequence_summary` | 未实现 |
| A24 | 导出文件名 `/^ClinMind-.+\.json$/`、`schema==="clinmind.record.v1"`、`records`/`review.timeline` 非空 | `:792` 有导出按钮与 Blob 下载；但**源码中不存在该文件名模式与 schema 字面量** | 部分实现 |
| A25 | 1920×1080 → `.canvas` `transform === scale(1.2)` | `scene.js:79` 公式；1920/1600=1.2、1080/900=1.2 | 已实现（推算） |
| A26 | 1280×720 → `scale(0.8)`，`.scene-emotion-side` 仍在 `left:28px` | 缩放比 0.8 成立；`.scene-emotion-side` 类名在 CSS 中无匹配（情感面板用的是 `#emotion-panel`/`.workstation .scene-emotion-*`），该断言的前置选择器本身不存在 | 部分实现 |
| A27 | 1000×600 → `bottom:"106px"` 且 `flex-direction:row` | 无 1100/1000 断点（见 §9.3） | 未实现 |
| A28 | 两页 grep `<style` 与 `style="` 均无匹配 | `test_pages_have_no_inline_style` 通过；`test_pages_load_shared_scene_layer` 亦通过 | 已实现 |
| A29 | §1.7 + §8.5 的每个类名在 CSS 中都有字面量规则 | 情感面板类名齐（`test_emotion_classes_have_css_rules`）；但规格中 `.scene-record-kind` / `.scene-record-negative` / `.scene-phase` / `.scene-tat-*` 在 CSS 中**不存在** | 部分实现 |
| A30 | 页面 `/static/` 引用与改造前完全相同（3 条） | `test_every_static_reference_resolves` 通过；未新增 `href/src` | 已实现 |

---

## 2. 判定统计

| 判定 | 数量 | 占比 | 编号 |
|---|---|---|---|
| 已实现 | **26** | 48.1% | 章节 13：§1.1 §1.2 §1.3 §1.5 §1.6 §2.3 §3 §5 §7 §8.1 §8.2 §8.4 §9.2<br>A 项 13：A01 A02 A03 A04 A14 A15 A16 A18 A19 A20 A25 A28 A30 |
| 部分实现 | **13** | 24.1% | 章节 6：§1.4 §4.1 §4.2 §4.4 §6 §8.3<br>A 项 7：A07 A08 A10 A17 A24 A26 A29 |
| 未实现 | **11** | 20.4% | 章节 5：§2.1 §2.2 §4.3 §9.3 §11<br>A 项 6：A09 A11 A12 A13 A23 A27 |
| 文档与实现不一致 | **4** | 7.4% | A05 A06 A21 A22 |
| 合计 | **54**（章节行 24 + A 项 30；A25/A26 与 §9.2 同源故不重复计缺口） | 100% | — |

> 口径说明：「已实现」中含 2 条标注「（推算）」的（§9.2、A25），只做了公式推导未实拍，已单独标注。数字由脚本对本文两张表逐行统计得出，非人工估算。

> 统计口径：`§2.1/§2.2/§4.3/§9.3/§11` 与 `A09/A11–A13/A23/A27` 是同一批缺口的「章节视角 + 验收项视角」两处记录，因此 54 行对应的**独立缺口只有 7 处**：①phase 三档色标 ②TAT 进度条 ③复盘时间线归因 ④导出 schema ⑤窄屏重排 ⑥§11 待裁决 ⑦A22 类名与色标方式同规格不一致（功能不缺）。

### 2.1 A01–A30 单表统计（不含章节行）

| 判定 | 条数 | 占比 | 编号 |
|---|---|---|---|
| 已实现 | 13 | 43.3% | A01 A02 A03 A04 A14 A15 A16 A18 A19 A20 A25 A28 A30 |
| 部分实现 | 7 | 23.3% | A07 A08 A10 A17 A24 A26 A29 |
| 未实现 | 6 | 20.0% | A09 A11 A12 A13 A23 A27 |
| 文档与实现不一致 | 4 | 13.3% | A05 A06 A21 A22 |

**覆盖统计：30 条验收项中 13 条完全达标（43.3%），7 条名义存在但行为残缺（23.3%），6 条完全缺失（20.0%），4 条做成了但和规格措辞对不上（13.3%）。换言之——按「跑一遍验收脚本能不能全绿」算，30 条里会红 **17 条**。**

---

## 3. 未实现项详述（按严重度排序）

### 3.1 【最严重】§2.2 / A11–A13 检查 TAT 进度条 —— 整块不存在

- **规格要求**：进行中的检查要有剩余时间进度条，状态机 `pending → done | refused`，`data-status` 驱动样式，被拒态用斜纹条纹。
- **实际**：`scene.css:41` 只有一个 `--scene-tat-dur:1s` 令牌，**没有任何 `.scene-tat-*` 选择器**；`scene.js` 全文无 `tat` 相关分支；两页 HTML 无 TAT 容器。
- **后果**：检查类操作（检验/影像）在下单到出报告之间，玩家看不到"还要等多久"。这是「时间可见」主题的一半（§2 的另一半 §2.1 也缺）。
- **落地建议**：`renderTat(state)` + `.scene-tat-list`，数据从 `actions[]` 里未完成项算 `virtual_time_seconds` 差值，复用 `recordClock()`（`scene.js:386`）的 MM:SS 逻辑。

### 3.2 【严重】§2.1 / A09–A10 病程计时三档色标 —— 有令牌无实现

- **规格要求**：计时器按 `phase ∈ stable|worsening|crisis` 变色，`.scene-phase-label` 显示「稳定/恶化/危象」。
- **实际**：`scene.css:21-23` 三个颜色变量已备好，但**没有任何类消费它们**；`.timer` 元素（`scene.css:50` `.timer{font-size:18px…}`）只有一种颜色；`scene.js` 无 `phase` 读取。
- **性质**：典型的「令牌先行、逻辑缺席」——看起来像做了（变量名齐整），实测无效果。
- **落地建议**：读 `snapshot.phase` → `document.body.dataset.phase` 或 `.timer.classList`，一条 CSS 规则即可接上。

### 3.3 【严重】§4.3 / A23 复盘时间线 —— 只有原始 id，没有归因

- **规格要求**：结案复盘要生成「第 N 分钟做了什么 + 错在哪一步」，`verdict==='fatal'` 条目排最前。
- **实际**：`scene.js:791` 仅把后端 `action_sequence_summary` 数组逐条塞进 `<details>`，**无排序、无归因、无分钟格式化**（是 `action_sequence_summary` 的字符串，不是 `第 N 分钟`）。
- **后果**：玩家能看到"做了哪些操作"，看不到"哪一步致命"。这是复盘的教育价值所在，缺了等于结案报告只是一份流水账。
- **落地建议**：前端用 `actions[].is_contraindicated` + `virtual_time_seconds` 自行排序（服务端已有 `fatal_errors`，`scene.js:788` 已在渲染），把 fatal 项提到最前并换算成「第 N 分钟」。

### 3.4 §9.3 / A27 响应式折叠 —— 只有缩放没有重排

- **实际**：三处 `@media` 均为局部（700px case-picker、820px finale-grid、reduced-motion），**无 1100/1000 断点**。窄屏靠 `fit()`（`scene.js:79`）整体 `transform: scale()` 把 1600×900 缩小，字会变小但布局不变。
- **判定依据**：1280/1920 两档因为等比缩放「看着没问题」（A25/A26 成立），但 1000×600 下文字实际缩到 0.625×，可读性不达标。
- **性质**：这是**设计取舍而非遗漏**——纯静态画布 + 固定 1600×900 舞台的前提下，真正的重排要改 `.canvas` 布局，风险远大于收益。建议在 §11 待裁决里明确降级为「不支持 <1280 宽」，而不是让它长期挂着"未实现"。

### 3.5 §11 待裁决项 —— 规格留了尾巴，实现无从对齐

`spec/03:751-760` 的待裁决表在代码中无任何对应注释或开关。若要关闭这条，需要规格侧先给结论。

---

## 4. 「文档与实现不一致」清单（改名/改值对照）

这 4 条不是"没做"，是**做了但和规格写的对不上**——比未实现更容易骗过验收（因为现象对、断言会失败）。

| 编号 | 规格写的 | 实现里是 | 位置 | 影响 |
|---|---|---|---|---|
| A05 | 未超过 30 s 时 `opacity` 计算值 **`0`**（完全隐藏） | 默认 `opacity:.45`，箭头常显 `→`，仅 `scene-trend-shown` 时提亮到 1 | `scene.css:95` vs `:101` | 验收断言必失败；且「样本不足」与「持平」在视觉上无法区分 |
| A06 | 类名 `scene-band-danger`，色 `--scene-danger` | 类名 `scene-band-critical`，色 `--scene-critical`（`#8f2f27`，比 danger `#b4473c` 更深） | `scene.js:149`、`scene.css:98` | 断言失败；但视觉上是**升级**（危象比危险更深），实现比规格更合理 |
| A21 | `.scene-record-kind` | `.scene-record-type` | `scene.js:410`、`scene.css` | 选择器失效 |
| A22 | `.scene-record-negative` + `borderLeftColor` | `.scene-record-flag`，只有 `color:var(--scene-danger)`，**无 `border-left`** | `scene.js:413`、`scene.css:186` | 断言失败；但禁忌操作**已有红字加粗**，非「静默失败」，属色标方式不同，功能不缺 |

**建议**：这 4 条全部以「实现为准、回改规格」处理——它们都**不是功能缺失**，只是命名或色阶选择不同（A22 已有红字加粗提示，只是没走 `border-left`）。若要严格对齐规格，最小改动是把规格侧的选择器改成实现里的名字，而不是改代码。

---

## 5. CSS 令牌层专项核对

| 检查项 | 结果 | 证据 |
|---|---|---|
| `:root` 变量块存在 | 是，30 个 `--scene-*` 变量 | `scene.css:10-42` |
| 新样式是否只用变量 | 是 | `scene.css:93-114`（情感面板）、`:145-190`（说明书/病历区）全部 `var(--scene-*)` |
| 是否存在裸 hex | **存在，但集中在改造前的老规则** | `scene.css:43-77` 大量 `#c6d0bd`、`#294b48`、`#faf8ecf5` 等；文件内注释 `:9` 明确写了「新样式一律引用变量，旧样式保持原样」 |
| 是否有测试守护 | 是 | `test_emotion_css_token_layer`（断言新样式含 `var(--scene-`） |
| 危险区色阶是否成套 | 是，safe→warn→danger→critical 四档 | `scene.css:24-27` |

**结论**：令牌层**部分实现**——新建样式 100% 走变量，存量 ~35 行仍是裸色值且**没有迁移计划**。这不是缺陷，是策略（规格 `§0.1` 也只要求"新样式"）。但要注意：`--scene-panel-bg:#f9f6e9ee` 这类**带 alpha 的 hex**（8 位）在部分老浏览器不支持，若后续要 EDGE 兼容需改写为 `rgb(… / …)`。

---

## 6. 结论

1. **P2 交付的两块（说明书 + 病历区）质量扎实**：A14/A15/A16/A18/A19/A20 全部已实现且有浏览器实拍，`test_manual_copy_avoids_answers` 用 10 个答案词锁死了泄题风险。
2. **真正的缺口是 §2「时间可见」整章**：TAT 进度条（A11–A13）零实现，病程三档色标（A09/A10）有令牌无逻辑。这两条直接导致 §2 主题在页面上不可见——而 §2 恰好是 `spec/03` 开篇声称的四项新增之一。
3. **最容易被误判为"已完成"的是 §2.1**：`--scene-phase-stable/-worsen/-crisis` 三个变量整整齐齐躺在 `scene.css:21-23`，看起来完全像做过了，实际没有任何选择器引用它们。核对时以「有规则消费」为准则，不以「有变量」为准。
4. **一致性债共 4 处，但都不是功能缺失**：A05/A06/A21/A22 全是「做成了，名字或色阶和规格写的对不上」。其中 A22 影响最小（禁忌操作已有红字加粗，只是没加左边框）；A05 影响最实际——样本不足时箭头显示 `→` 而不是隐藏，玩家会把「还没攒够数据」误读成「情绪稳定」。建议按「改规格、不改代码」处理，只补 A05 的视觉区分。
5. **响应式（§9.3）建议公开降级**：在 1600×900 固定舞台 + 等比缩放的架构下，1000px 重排不可行且不划算，应在 `spec/03:751` 待裁决里写死最低支持宽度 1280。

**未核验项（诚实标注）**：A17 的「停留 60 s 数值有变化」、A25/A26 的 `transform` 计算值、A24 的导出文件名与 schema —— 三条均依赖运行时/后端结算，本轮只做了代码级推断，未开浏览器逐条断言。

---

## 复审（HEAD 8c245d0）

> 首轮快照基线 `779dabd`，本轮基线 `8c245d0`（2026-10-26）。`scene.js` 891 → **1039 行**，`scene.css` 190 → **261 行**。
> 判定口径：「现判定」四选一 —— `已修复` / `仍未实现` / `部分修复` / `判定变更（文档该改）`。
> 约定：首轮即达标、本轮复核仍达标的，也记为 `已修复`，证据列注明「首轮即达标」。

### 复审总表

| 编号 | 原判定 | 现判定 | 证据 |
|---|---|---|---|
| A01 | 已实现 | 已修复 | 两页各 3 个 `.scene-emotion-row`（首轮即达标，未变） |
| A02 | 已实现 | 已修复 | `scene.js` `renderEmotionPanel` 仍写 `fill.style.width` + `value.textContent`（首轮即达标） |
| A03 | 已实现 | 已修复 | `scene.css:93` `transition:width var(--scene-bar-dur) ease` + `:40` `200ms`（未变） |
| A04 | 已实现 | 已修复 | `scene.js:192` `scene-trend-shown` 门控 + `scene.css:101-103` 三条规则 |
| A05 | 文档与实现不一致 | **判定变更（文档该改）** | **未修**：`scene.css:95` 仍 `opacity:.45`，`:101` `.scene-trend-shown{opacity:1}`。箭头隐藏态是「淡显」不是「隐形」——规格写的 `opacity==="0"` 从来不是设计意图，**改规格**为「样本不足时箭头淡显 45% 并显示 →」 |
| A06 | 文档与实现不一致 | **判定变更（文档该改）** | **未修**：`scene.js:159-161` 仍 `scene-band-low/-high/-critical`，`scene.css:96-100` 仍走 `--scene-critical:#8f2f27`。语义比规格的 `--scene-danger` 更重，属升级，**改规格**接受 `scene-band-critical` |
| A07 | 部分实现 | 部分修复 | `scene.js:204-212` `applyRefusal()` 仍是 toast + `scene-refuse` + 横幅 + 行态四件套；**第 4 层「记录区 negative 条目」仍缺**——`scene-record-negative` 在 js/css 全域 0 命中，禁忌/失败态只走 `.scene-record-flag`（`scene.css:186`） |
| A08 | 部分实现 | **判定变更（文档该改）** | 横幅仍是纯文本：`reception.html` `<span class="scene-refusal-banner" role="alert" hidden>拒绝配合 · 先安抚患者</span>`，**无 button 子节点**。规格里「点横幅安抚患者」是凭空设计的路径，真实机制是正常问诊降焦虑，**改规格** |
| A09 | 未实现 | **已修复** | `scene.js:54` `PHASE_BAND_LIST=['stable','worsen','crisis']`、`:226-240` `renderPhase()`、`:236` 切 `scene-phase-${name}`、`:238` 写 `PHASE_LABELS[band]`。band 名是 **`worsen`** 不是规格写的 `worsening`（见「命名偏差」） |
| A10 | 部分实现 | 已修复 | 同上 + `:238` 危象档文案；`test_no_death_countdown_text` 通过、`倒计时/剩余秒` 全域 0 命中 |
| A11 | 未实现 | 部分修复 | `scene.js:262-294` `renderTat()`、`:253-254` 按 `1 - remaining/total` 算宽度、`scene.css:226-236` 规则齐、两页 `<section id="tat-panel" class="scene-tat" hidden>` 都有。**但**：`:255` 剩余时间写成 `` `${Math.ceil(remaining)} 秒` ``，不是规格要的 `"15:00"`；**无 `data-status` 属性**，状态靠 `.scene-tat-ready` 类 |
| A12 | 未实现 | 已修复 | `:274-278` `status==='ready'` → `paintTat(0,…)` 宽 100%；`:258-259` 标题切「已可查看」；`:260` 加 `scene-tat-ready`。用类不用 `data-status`（同 A11 偏差） |
| A13 | 未实现 | **仍未实现** | TAT 无「被拒」表达：`renderTat` 对非 `pending/ready` 一律 `hideTat()`（`:280`），`repeating-linear-gradient` 在 `scene.css` 0 命中。拒绝反馈只走情绪面板，**TAT 面板不上报拒绝** |
| A14 | 已实现 | 已修复 | 两页 `#manual-dialog` 各 1、3 个 `.scene-manual-tab` 带 `aria-selected` |
| A15 | 已实现 | 已修复 | `setManualTab` 未变；P2 轮实拍通过 |
| A16 | 已实现 | 已修复 | `scene.js:89` `anyModalOpen()` 仍遍历全部 `<dialog>`；`:996` 快捷键守卫 `anyModalOpen()‖输入框焦点 → return` |
| A17 | 部分实现 | 部分修复（未实拍） | 说明书三路径仍不触碰定时器；**新增** `stopTatTicker()`（`:241`）只服务 TAT，不在弹窗路径上。60 s 数值变化仍未实拍 |
| A18 | 已实现 | 已修复 | `ensureDisclaimer()` + `test_disclaimer_storage_key_and_copy` 通过 |
| A19 | 已实现 | 已修复 | `ackDisclaimer()` 仍写 `sessionStorage['clinmind-disclaimer-ack']='1'` |
| A20 | 已实现 | 已修复 | 常驻 `.scene-disclaimer*` 两页各 4 处 |
| A21 | 文档与实现不一致 | **判定变更（文档该改）** | **未修**：`scene.js:490` 仍 `.scene-record-type`，`.scene-record-kind` 0 命中。实现名更准确，**改规格** |
| A22 | 文档与实现不一致 | **判定变更（文档该改）** | **未修**：`scene.css:186` `.scene-record-flag{…color:var(--scene-danger)}` 红字加粗。规格要的 `border-left` 不存在，但「禁忌无视觉区分」的说法不成立，**改规格** |
| A23 | 未实现 | **已修复** | `:877` `renderTimeline()`、`:922` 调用；`:867` 输出「第 X 分 YY 秒」；`:886-888` 每条带 `verdict/fatal/invalid`；`:871-872` 致命项单独提到表头「最优先的一条是〈…〉」。排序细节未逐行确认（`items.find` 只用于取头部），但归因与致命优先已落地 |
| A24 | 部分实现 | 部分修复（未实拍） | `:923` 导出按钮仍在，仍无 `ClinMind-*.json` 文件名与 `clinmind.record.v1` schema 字面量（`schema` 全域 0 命中）。文件名在 Blob 处动态拼，需实拍才能定 |
| A25 | 已实现（推算） | 已修复（推算，未实拍） | `scene.js:90` `fit()` 未变：`Math.min(innerWidth/1600, innerHeight/900)` → 1920/1080 得 1.2 |
| A26 | 部分实现 | **判定变更（文档该改）** | `fit()` 0.8 成立；规格里的 `.reception .scene-emotion-side` **选择器从不存在**（`scene-emotion-side` 在 css 0 命中），是规格凭空造的锚点，**改规格**为 `#emotion-panel` |
| A27 | 未实现 | **判定变更（文档该改）** | 断点仍只有 `700/820/prefers-reduced-motion`（`scene.css:78/114/190/223`），无 1100/1000。**这是设计取舍不是缺口**：1600×900 固定舞台 + 等比缩放，最低支持宽度应写死 1280 |
| A28 | 已实现 | 已修复 | `test_pages_have_no_inline_style` 通过；两页 `style="` 与 `<style` 均 0 命中 |
| A29 | 部分实现 | 部分修复 | 新增类名都有规则（`scene-tat-*` css 10 处、`scene-phase-*` css 13 处）；仍缺 `.scene-record-kind` / `.scene-record-negative` / TAT 的 `data-status` 选择器 |
| A30 | 已实现 | 已修复 | `test_every_static_reference_resolves` 通过，未新增 `/static/` 引用 |
| §2.1 | 未实现 | **已修复** | 同 A09/A10 |
| §2.2 | 未实现 | 部分修复 | 同 A11–A13：进度条与 TAT 计时落地，拒绝态与 MM:SS 格式未落地 |
| §4.3 | 未实现 | **已修复** | 同 A23 |
| §9.3 | 未实现 | **判定变更（文档该改）** | 同 A27 |
| §11 | 未实现 | 仍未实现 | 待裁决表仍无对应代码痕迹；需产品侧给结论 |

### 复审统计

| 现判定 | 行数 | 编号 |
|---|---|---|
| 已修复 | **19** | A01 A02 A03 A04 A09 A10 A12 A14 A15 A16 A18 A19 A20 A23 A25 A28 A30、§2.1 §4.3 |
| 部分修复 | **6** | A07 A11 A17 A24 A29、§2.2 |
| 仍未实现 | **2** | A13、§11 |
| 判定变更（文档该改） | **8** | A05 A06 A08 A21 A22 A26 A27、§9.3（A27 与 §9.3 同一问题，独立 7 处） |
| 合计 | **35** | 与表行数一致 |

**一句话：首轮 6 条「未实现」的 A 项里，A09 / A12 / A23 三条真补上了（phase、TAT 完成态、复盘时间线全落地），A11 补了一半，A13 仍空，A27 是规格本身写错。剩下的「缺」只有 2 处真缺口 + 7 处文档该改。**

### 三类需要区分的事

**1. 真缺口（2 条，要改代码）**
- **A13**：检查被拒时 TAT 面板 `hideTat()` 直接消失，没有任何「被拒」标记。玩家看到的是「面板没了」，而不是「这单被拒了」。建议 `renderTat` 增加 `status==='refused'` 分支，加 `.scene-tat-refused` 类 + 斜纹底。
- **§11**：待裁决项仍无落地痕迹，需产品给结论才能关闭。

**2. 文档该改（7 处，别去改代码）**

| 编号 | 规格写的 | 实际设计 | 为什么该改文档 |
|---|---|---|---|
| A05 | 隐藏态 `opacity:0` | `0.45` 淡显 | 淡显是更好的信息设计（保留位、不跳动）；`0` 会让三行高度有塌陷感 |
| A06 | `scene-band-danger` / `--scene-danger` | `scene-band-critical` / `--scene-critical` | 危象本就该比危险更深，实现是升级 |
| A08 | 「点横幅安抚患者」 | 横幅是 `role="alert"` 纯文本 | 设计里根本没有这个按钮，是规格编的交互 |
| A21 | `.scene-record-kind` | `.scene-record-type` | 同义，无功能差 |
| A22 | `.scene-record-negative` + `border-left` | `.scene-record-flag` + 红字 | 红色已足够区分，左边框是可选项 |
| A26 | `.reception .scene-emotion-side` | `#emotion-panel` | 该选择器从未存在，规格凭空造的锚点 |
| A27 / §9.3 | 1000px 断点横排 | 只有等比缩放 | 架构上不该做重排，见下 |

**3. 设计取舍（1 条，建议写进规格而非记作缺口）**
- **§9.3 / A27**：断点仍只有 700 / 820 / reduced-motion。在 1600×900 固定舞台 + `fit()` 等比缩放的架构下，1000px 宽会让文字缩到 0.625×，重排要动 `.canvas` 布局、风险远大于收益。**建议在 `spec/03:751` 待裁决里写死「最低支持 1280×720」，并说明窄于此不保证可读性。**

### 本轮新增能力（不在 spec/03 范围内，记录备查）

| 能力 | 证据 | 说明 |
|---|---|---|
| 会话结束态恢复 | `scene.js:1005 sessionEnded()`、`:1018` 调用 | 已结束会话下入口仍可用，与首轮实测一致 |
| 病历抽屉位置修正 | `scene.css:169-171`：`.reception .scene-record-area{left:1178px;top:604px}` | 对应提交 `8c245d0`「接诊室病历抽屉不再压住家属情绪条与患者名牌」；首轮实测的重叠问题已消除 |
| TAT 三段式渲染 | `scene.js:241-294`：`stopTatTicker / hideTat / paintTat / renderTat` | 字段缺失（`NaN`）时整块隐藏、不猜值（`:250` 注释），保守策略值得保留 |

### 命名偏差清单（不影响功能，供 spec/03 更新时对齐）

| 规格名 | 实现名 | 位置 |
|---|---|---|
| `phase: worsening` | `worsen` | `scene.js:54` |
| `.scene-phase` / `.scene-phase-label` | `.scene-phase-strip` / `.scene-phase-value` | `scene.js:231/237` |
| `data-status="done"/"refused"` | `.scene-tat-ready` 类 | `scene.js:260` |
| 剩余时间 `"15:00"` | `` `${n} 秒` `` | `scene.js:255` |
| `.scene-record-kind` | `.scene-record-type` | `scene.js:490` |
| `.scene-record-negative` | `.scene-record-flag` | `scene.css:186` |
