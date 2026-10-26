/* Interactive adapter for the two supplied UI designs. No external libraries. */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const names = {doctor: '接诊医生', patient: '患者 · 孙建国', family: '家属 · 李秀梅', nurse: '责任护士', system: '系统'};
  const defaultMenu = [
    {label: '问诊', type: 'inquiry', items: [
      ['疼痛性质与放射范围', '怎么疼？疼痛向哪里放射？'],
      ['起病诱因与持续时长', '什么时候开始发作，有什么诱因？'],
      ['高血压病史与规律服药', '您有高血压病史吗？平时服药规律吗？'],
      ['安抚患者与家属', '别紧张，我们全力救治，马上给您止痛。']
    ]},
    {label: '体查', type: 'physical_exam', items: [
      ['双上肢血压对比', '测量双侧上肢血压'], ['心脏听诊', '心脏听诊主动脉瓣杂音'],
      ['周围动脉搏动', '触诊双侧桡动脉脉搏'], ['神经系统检查', '检查瞳孔与四肢肌力']
    ]},
    {label: '检验', type: 'lab_order', items: [
      ['床旁心电图', '急查18导联心电图'], ['心肌损伤标志物', '急查高敏肌钙蛋白I'],
      ['D-二聚体', '急查D-二聚体'], ['全主动脉增强 CTA', '急诊胸腹主动脉全长增强CTA']
    ]},
    {label: '处方', type: 'medication', items: [
      ['艾司洛尔', '艾司洛尔'], ['硝普钠', '硝普钠'],
      ['吗啡镇痛', '吗啡镇痛'], ['阿司匹林 · 训练风险选项', '阿司匹林']
    ]},
    {label: '转归', type: 'rescue_disposition', items: [
      ['建立急救通路与监护', '建立双通道静脉通路并吸氧监护'],
      ['心胸外科急会诊', '紧急呼叫心胸外科急会诊'],
      ['病危沟通与知情同意', '签署急诊手术知情同意书及病危通知书'],
      ['抢救处置', '心肺复苏评估']
    ]}
  ];
  let menu = defaultMenu.map(group => ({...group, items: group.items.map(item => [...item])}));
  let sessionId, tab = 0, snapshot, busy = false, stopped = false, ws, noticeTimer, reconnectTimer, speechTimer;
  let speechSignature = '';
  let reconnectCount = 0, streamConnected = false, lastTelemetryAt = 0, lastAlarmAt = -Infinity;
  let lastWaveAt = null, heartPhase = 0, breathPhase = 0;
  const monitorState = {hr: 108, spo2: 96, sbp: 195, dbp: 115, rr: 22, pain: 9, patientState: 'S0_STABLE'};
  // 医患状态面板：6 个 WS 字段可能暂时缺失，缺字段时保持上一次数值，不崩、不归零。
  const EMOTION_CHANNELS = [
    {key: 'trust', label: '信任度', trendKey: 'trust_trend'},
    {key: 'patient_anxiety', label: '患者焦虑', trendKey: 'anxiety_trend'},
    {key: 'family_anxiety', label: '家属焦虑', trendKey: 'anxiety_trend'}
  ];
  const TREND_WINDOW_MS = 30000, TREND_EPS = 0.02, EMOTION_TICK_MS = 5000, DISCLAIMER_KEY = 'clinmind-disclaimer-ack';
  // 病程映射只在常量里维护一处：稳定 / 恶化 / 危象三档。
  const PHASE_BANDS = {
    S0_STABLE: 'stable', S2_PHARMACO_CONTROLLED: 'stable',
    S3_PERICARDIAL_EFFUSION: 'worsen',
    S1_HYPERTENSION_CRISIS: 'crisis', S4_CARDIAC_TAMPONADE: 'crisis',
    S5_AORTIC_RUPTURE: 'crisis', S6_CARDIAC_ARREST: 'crisis'
  };
  const PHASE_LABELS = {stable: '稳定期', worsen: '恶化期', crisis: '危象期'};
  const PHASE_BAND_LIST = ['stable', 'worsen', 'crisis'];
  const TAT_TICK_MS = 500, TAT_HIDE_MS = 3200;
  const state = {
    ended: false,
    refused: false,
    emotion: {trust: 0.40, patient_anxiety: 0.60, family_anxiety: 0.65},
    trendHint: {trust: null, patient_anxiety: null, family_anxiety: null},
    trendValue: {trust: 0, patient_anxiety: 0, family_anxiety: 0},
    history: {trust: [], patient_anxiety: [], family_anxiety: []},
    lastSampleAt: 0
  };
  let emotionTimer;
  let tatTick, tatHideTimer;
  const waveSvg = $('.wave-svg');
  const waveLines = waveSvg ? $$('polyline', waveSvg) : [];
  if (waveSvg) {
    waveSvg.setAttribute('viewBox', '0 0 560 124');
    const resp = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    resp.setAttribute('fill', 'none'); resp.setAttribute('stroke', '#e1e5bf'); resp.setAttribute('stroke-width', '1.8');
    resp.setAttribute('aria-label', '呼吸波形'); waveSvg.append(resp); waveLines.push(resp);
  }
  const unlockAudio = () => window.monitorAudio?.unlock();
  addEventListener('pointerdown', unlockAudio, {once: true});
  addEventListener('keydown', unlockAudio, {once: true});
  const canvas = $('.canvas');
  const workstation = !!$('.computer');
  const notice = document.createElement('div'); notice.className = 'scene-notice'; notice.setAttribute('role', 'status'); document.body.append(notice);
  const alert = document.createElement('div'); alert.className = 'scene-alert'; canvas.append(alert);
  const log = document.createElement('aside'); log.className = 'scene-log'; log.hidden = true;
  log.innerHTML = '<div class="scene-log-head"><b>接诊记录</b><button aria-label="关闭记录">×</button></div><div class="scene-log-body"></div>';
  document.body.append(log); $('button', log).onclick = () => { log.hidden = true; };
  const dialog = document.createElement('dialog'); dialog.className = 'scene-dialog'; document.body.append(dialog);
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
  // 全局快捷键只认这一个 dialog 时，说明书窗口开着按 Esc 会「一边关窗一边跳页」。
  // 统一按「当前有没有任何 modal 打开」判断。
  function anyModalOpen() { return [...document.querySelectorAll('dialog')].some(node => node.open); }
  function fit() { canvas.style.transform = `scale(${Math.min(innerWidth / 1600, innerHeight / 900)})`; }
  addEventListener('resize', fit); fit();
  function toast(text, error = false) {
    notice.textContent = text; notice.classList.toggle('scene-error', error);
    clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { notice.textContent = ''; }, error ? 7000 : 3500);
  }
  async function api(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const res = await fetch(path, {signal: controller.signal, ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${res.status}），请检查输入`), {status: res.status});
      return data;
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('请求超时，请检查服务或模型连接后重试');
      throw e;
    } finally { clearTimeout(timer); }
  }
  function interactive(el, handler, label) {
    if (!el) return;
    el.setAttribute('role', 'button'); el.tabIndex = 0;
    if (label) el.setAttribute('aria-label', label);
    el.addEventListener('click', handler);
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(e); } });
  }
  function openDialog(title) {
    dialog.replaceChildren();
    const head = document.createElement('div'); head.className = 'scene-dialog-head';
    const h = document.createElement('h2'); h.textContent = title;
    const close = document.createElement('button'); close.className = 'secondary'; close.textContent = '关闭'; close.onclick = () => dialog.close();
    head.append(h, close); const body = document.createElement('div'); body.className = 'scene-dialog-body'; dialog.append(head, body);
    if (!dialog.open) dialog.showModal();
    return body;
  }
  function element(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
  function ratio(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    return Math.max(0, Math.min(1, number > 1 ? number / 100 : number));
  }
  function readEmotion(data) {
    if (!data) return;
    const values = {trust: data.trust_level ?? data.trust, patient_anxiety: data.patient_anxiety, family_anxiety: data.family_anxiety};
    for (const {key, trendKey} of EMOTION_CHANNELS) {
      const value = ratio(values[key]);
      if (value !== null) state.emotion[key] = value;
      const hint = Number(data[trendKey]);
      state.trendHint[key] = hint === 1 || hint === -1 || hint === 0 ? hint : null;
    }
  }
  function sampleEmotion(now) {
    if (now - state.lastSampleAt < EMOTION_TICK_MS - 500) return;
    state.lastSampleAt = now;
    for (const {key} of EMOTION_CHANNELS) {
      const buffer = state.history[key];
      buffer.push({t: now, v: state.emotion[key]});
      while (buffer.length > 1 && now - buffer[0].t > TREND_WINDOW_MS) buffer.shift();
    }
  }
  function localTrend(key, now) {
    const buffer = state.history[key];
    if (!buffer.length || now - buffer[0].t < TREND_WINDOW_MS) return null;
    const delta = state.emotion[key] - buffer[0].v;
    if (delta > TREND_EPS) return 1;
    if (delta < -TREND_EPS) return -1;
    return 0;
  }
  function bandClass(channel, value) {
    if (channel === 'trust') return value < 0.2 ? 'scene-band-low' : '';
    if (value > 0.9) return 'scene-band-critical';
    if (value > 0.8) return 'scene-band-high';
    return '';
  }
  function renderEmotionPanel(source) {
    readEmotion(source);
    const panel = $('#emotion-panel');
    if (!panel) return;
    const now = Date.now();
    sampleEmotion(now);
    for (const {key, label} of EMOTION_CHANNELS) {
      const row = $(`[data-channel="${key}"]`, panel);
      if (!row) continue;
      const value = state.emotion[key];
      const percent = Math.round(value * 100);
      const fill = $('.scene-emotion-fill', row);
      if (fill) fill.style.width = `${(value * 100).toFixed(1)}%`;
      row.classList.remove('scene-band-low', 'scene-band-high', 'scene-band-critical');
      const band = bandClass(key, value);
      if (band) row.classList.add(band);
      const number = $('.scene-emotion-value', row);
      if (number && number.dataset.percent !== String(percent)) {
        number.textContent = `${percent}%`;
        number.dataset.percent = String(percent);
      }
      const trendElement = $('.scene-emotion-trend', row);
      if (trendElement) {
        const local = localTrend(key, now);
        const hint = state.trendHint[key];
        const trend = hint !== null ? hint : (local ?? 0);
        state.trendValue[key] = trend;
        trendElement.textContent = trend > 0 ? '↑' : trend < 0 ? '↓' : '→';
        trendElement.classList.toggle('scene-trend-shown', hint !== null || local !== null);
        trendElement.classList.toggle('scene-trend-good', trend !== 0 && (key === 'trust' ? trend > 0 : trend < 0));
        trendElement.classList.toggle('scene-trend-bad', trend !== 0 && (key === 'trust' ? trend < 0 : trend > 0));
        trendElement.setAttribute('aria-label', `${label}趋势${trend > 0 ? '上升' : trend < 0 ? '下降' : '持平'}`);
      }
    }
    applyRefusal(source);
    applyEndState();
  }
  function applyRefusal(data) {
    const panel = $('#emotion-panel');
    if (!panel) return;
    if (data?.refuse === true) {
      if (!state.refused) toast('患者当前拒绝配合，请先安抚情绪后继续问诊。', true);
      state.refused = true;
    } else if (state.emotion.family_anxiety < 0.85) {
      state.refused = false;
    }
    panel.classList.toggle('scene-refuse', state.refused);
    $$('.scene-emotion-row', panel).forEach(row => row.classList.toggle('scene-refusing', state.refused));
    const banner = $('.scene-refusal-banner', panel);
    if (banner) banner.hidden = !state.refused;
  }
  function applyEndState() {
    const reason = snapshot?.terminal_reason;
    const ended = !!snapshot?.is_finished || (typeof reason === 'string' && reason.trim() !== '');
    state.ended = ended;
    document.body.classList.toggle('is-finished', !!snapshot?.is_finished);
    document.body.classList.toggle('scene-ended', ended);
    input.disabled = ended;
    $$('.dock-tab,.tab,.inquiry-button,.send,.next-btn,.end-btn').forEach(node => node.setAttribute('aria-disabled', String(ended)));
    const tick = $('.scene-emotion-tick');
    if (tick) tick.textContent = ended ? '已结束 · 操作冻结' : '5 秒刷新';
  }
  function phaseBandOf(stateName) {
    const key = String(stateName || '').trim().toUpperCase();
    return PHASE_BANDS[key] || (key ? 'worsen' : '');
  }
  function renderPhase(stateName) {
    const strip = $('.scene-phase-strip');
    if (!strip) return;
    const band = phaseBandOf(stateName);
    if (!band) { strip.hidden = true; return; }
    strip.hidden = false;
    PHASE_BAND_LIST.forEach(name => strip.classList.toggle(`scene-phase-${name}`, name === band));
    const value = $('.scene-phase-value', strip);
    if (value) value.textContent = PHASE_LABELS[band];
    strip.setAttribute('data-phase', String(stateName || ''));
  }
  function stopTatTicker() {
    clearInterval(tatTick); tatTick = null;
    clearTimeout(tatHideTimer); tatHideTimer = null;
  }
  function hideTat() {
    stopTatTicker();
    const panel = $('.scene-tat');
    if (panel) { panel.hidden = true; panel.classList.remove('scene-tat-ready', 'scene-tat-refused'); delete panel.dataset.status; }
  }
  // 剩余时间统一 mm:ss，避免「90 秒」这种不好读的写法。
  function formatTatLeft(remaining) {
    const sec = Math.max(0, Math.ceil(Number(remaining) || 0));
    return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
  }
  // 只按 pending/ready + 剩余秒数推进；字段缺失（NaN）时整块隐藏，不做猜测。
  function paintTat(remaining, total, itemName) {
    const panel = $('.scene-tat'); if (!panel) return;
    const done = total > 0 ? Math.min(1, Math.max(0, 1 - remaining / total)) : 0;
    const fill = $('.scene-tat-fill', panel); if (fill) fill.style.width = `${(done * 100).toFixed(1)}%`;
    const left = $('.scene-tat-remaining', panel); if (left) left.textContent = formatTatLeft(remaining);
    const item = $('.scene-tat-item', panel); if (item && itemName) item.textContent = itemName;
    const ready = remaining <= 0;
    const title = $('.scene-tat-title', panel);
    if (title) title.textContent = ready ? '已可查看' : '检查进行中';
    panel.classList.toggle('scene-tat-ready', ready);
    panel.classList.remove('scene-tat-refused');
    panel.dataset.status = ready ? 'ready' : 'pending';
  }
  // 被拒不是「面板消失」：留下斜纹卡片 + 明确文案，保留到下一次动作再收场。
  function paintTatRefused(data) {
    const panel = $('.scene-tat'); if (!panel) return;
    stopTatTicker();
    const itemName = String(data.content || data.report?.title || data.report?.name || '检查项目').slice(0, 80);
    panel.hidden = false;
    panel.classList.remove('scene-tat-ready');
    panel.classList.add('scene-tat-refused');
    panel.dataset.status = 'refused';
    const title = $('.scene-tat-title', panel); if (title) title.textContent = '患者拒绝配合';
    const item = $('.scene-tat-item', panel); if (item) item.textContent = itemName;
    const left = $('.scene-tat-remaining', panel); if (left) left.textContent = '已拒绝';
    const fill = $('.scene-tat-fill', panel); if (fill) fill.style.width = '100%';
  }
  function renderTat(data) {
    const panel = $('.scene-tat'); if (!panel) return;
    if (!data || typeof data !== 'object') { hideTat(); return; }
    const status = String(data.status || data.order_status || '').toLowerCase();
    const total = Number(data.tat_seconds);
    const availableAt = Number(data.available_at_seconds);
    const virtualNow = Number(snapshot?.virtual_seconds);
    let remaining = Number(data.remaining_seconds);
    if (!Number.isFinite(remaining)) {
      remaining = Number.isFinite(availableAt) && Number.isFinite(virtualNow) ? availableAt - virtualNow : NaN;
    }
    const span = Number.isFinite(total) && total > 0 ? total : remaining;
    if (data.refused === true || status === 'refused' || status === 'rejected') {
      paintTatRefused(data);
      return;
    }
    if (status === 'ready') {
      stopTatTicker();
      paintTat(0, Number.isFinite(span) && span > 0 ? span : 1, data.content);
      tatHideTimer = setTimeout(hideTat, TAT_HIDE_MS);
      return;
    }
    if (status !== 'pending' || !Number.isFinite(remaining) || remaining <= 0) { hideTat(); return; }
    const itemName = String(data.content || data.report?.title || data.report?.name || '检查项目').slice(0, 80);
    stopTatTicker();
    panel.hidden = false;
    paintTat(remaining, span, itemName);
    const deadline = performance.now() + remaining * 1000;
    tatTick = setInterval(() => {
      const left = (deadline - performance.now()) / 1000;
      paintTat(left, span);
      if (left <= 0) {
        stopTatTicker();
        tatHideTimer = setTimeout(hideTat, TAT_HIDE_MS);
      }
    }, TAT_TICK_MS);
  }
  function startEmotionTicker() {
    clearInterval(emotionTimer);
    renderEmotionPanel(snapshot || null);
    emotionTimer = setInterval(() => {
      renderEmotionPanel(snapshot || null);
      if (!snapshot && sessionId) refresh().catch(() => {});
    }, EMOTION_TICK_MS);
  }
  function ackDisclaimer() {
    sessionStorage.setItem(DISCLAIMER_KEY, '1');
    $('.scene-disclaimer-modal')?.close();
  }
  function ensureDisclaimer() {
    const modal = $('.scene-disclaimer-modal');
    if (!modal) return;
    modal.addEventListener('cancel', event => event.preventDefault());
    $('.scene-disclaimer-ack', modal)?.addEventListener('click', ackDisclaimer);
    if (sessionStorage.getItem(DISCLAIMER_KEY) === '1') return;
    if (!modal.open) modal.showModal();
  }
  /* ===== 训练说明书：独立 <dialog>（#manual-dialog），不共用全局 dialog 实例 =====
     Esc 由浏览器原生 cancel 关闭本窗；全局 keydown 用 anyModalOpen() 兜住，不再跳页。 */
  // #manual-copy-begin —— 说明书全部文案只在这里维护（测试按标记切片检查答案泄漏）
  const MANUAL_MODULES = [
    ['问诊', '按方向追问主诉的时间轴、诱因、演变与既往史，也可以用输入框问自己的问题。', '记录问诊条数与相关性，计入「医患沟通感」与「鉴别思维广」。'],
    ['体查', '做双侧对比、听诊、触诊等床旁动作，把「哪里不一样」变成可写进病历的证据。', '记录查体的针对性与顺序，计入「急救处置力」与「安全规避度」。'],
    ['检验', '下达客观检查，结果以报告形式返回，可随时在「报告」入口复看。', '记录有没有做能证实或证伪当前判断的关键检查，计入「确诊决策度」。'],
    ['处方', '选择处置的分类与时机（药理类别、血流动力学目标），系统不给标准答案。', '只判断类别与时机是否匹配；具体品种与剂量由你自己负责。'],
    ['转归', '建立通道、请会诊、谈知情同意、启动抢救流程，决定这一例的走向。', '记录处置顺序与时效，是「临床时效性」的主要来源。']
  ];
  const MANUAL_SIGNALS = [
    '时间轴：症状何时开始、怎么演变、什么让它加重或缓解——先确认这些线索能串成一条解释。',
    '危险信号：先回答「有没有必须立刻处理的致命可能」，再考虑常见病；顺序错了，后面的检查就白做。',
    '可验证性：每个判断都要说得出「哪一项检查或体征支持它」，说不出依据的判断不计分。'
  ];
  const MANUAL_SCORING = [
    ['急救处置力', '危急问题的处理是否先于常规流程。'],
    ['临床时效性', '关键动作出现在病程的第几分钟，有没有反复空转。'],
    ['医患沟通感', '有没有解释、安抚与告知，问诊是否答到点上。'],
    ['安全规避度', '是否触发禁忌操作、是否遗漏必须排除的风险。'],
    ['鉴别思维广', '是否用检查主动区分几个候选方向，而不是只赌一个。'],
    ['确诊决策度', '最终判断与已取得的证据链是否对得上。']
  ];
  const MANUAL_BOUNDARY = '说明书不给出诊断名称，也不给具体药名与剂量：方向与方法可以讲，答案要你自己在问诊、体查、检验里取。';
  // #manual-copy-end
  const manualDialog = $('#manual-dialog');
  let manualTab = 0;

  function manualCaseFacts() {
    const profile = snapshot?.case_profile || {};
    const patient = profile.patient_profile || {};
    const initialVitals = profile.initial_vitals || null;
    const vitals = initialVitals || snapshot?.vitals || {};
    const facts = [];
    if (profile.chief_complaint) facts.push(['主诉', String(profile.chief_complaint)]);
    const who = [patient.gender, patient.age ? `${patient.age} 岁` : ''].filter(Boolean).join(' · ');
    if (patient.name || who) facts.push(['患者', `${patient.name || '虚拟患者'}${who ? ` · ${who}` : ''}`]);
    if (profile.category) facts.push(['分科', String(profile.category)]);
    if (profile.difficulty) facts.push(['难度', String(profile.difficulty)]);
    const vitalsText = [
      Number.isFinite(vitals.hr) ? `HR ${Math.round(vitals.hr)}` : '',
      Number.isFinite(vitals.sbp) ? `BP ${Math.round(vitals.sbp)}/${Math.round(vitals.dbp ?? 0)}` : '',
      Number.isFinite(vitals.spo2) ? `SpO₂ ${Math.round(vitals.spo2)}%` : ''
    ].filter(Boolean).join(' · ');
    if (vitalsText) facts.push([initialVitals ? '初始体征' : '当前体征', vitalsText]);
    return facts;
  }

  function fillManualOverview(panel) {
    panel.replaceChildren();
    panel.append(element('p', '本页只给观察方向：先问什么、先查什么、先排除什么。不给出诊断名称，也不给用药答案。', 'scene-manual-lead'));
    const facts = manualCaseFacts();
    const grid = element('dl', undefined, 'scene-manual-grid');
    if (facts.length) facts.forEach(([key, value]) => { grid.append(element('dt', key), element('dd', value)); });
    else grid.append(element('dt', '病例'), element('dd', '尚未接入病例：开始接诊后这里显示主诉、分科与初始体征。'));
    panel.append(grid);
    panel.append(element('h3', '三条通用观察线'));
    const list = element('ol', undefined, 'scene-manual-list');
    MANUAL_SIGNALS.forEach(text => list.append(element('li', text)));
    panel.append(list);
    panel.append(element('p', MANUAL_BOUNDARY, 'scene-manual-note'));
  }

  function fillManualModules(panel) {
    panel.replaceChildren();
    panel.append(element('p', '底部五个页签决定你能拿到哪一类证据。规则评的是「做了什么、顺序对不对」，不评你背了多少。', 'scene-manual-lead'));
    const table = element('table', undefined, 'scene-manual-table');
    const head = document.createElement('thead'); const headRow = document.createElement('tr');
    ['模块', '你能做什么', '评分怎么记'].forEach(text => headRow.append(element('th', text)));
    head.append(headRow); table.append(head);
    const body = document.createElement('tbody');
    MANUAL_MODULES.forEach(([name, can, score]) => {
      const row = document.createElement('tr');
      row.append(element('td', name), element('td', can), element('td', score));
      body.append(row);
    });
    table.append(body); panel.append(table);
    panel.append(element('p', '页签 1–5 与输入框等价：点页签只是把整条指令填好，措辞可以自己写。', 'scene-manual-note'));
  }

  function fillManualScoring(panel) {
    panel.replaceChildren();
    panel.append(element('p', '评分来自你的操作序列与规则模型，不看最终诊断的字面写法，也不读心。', 'scene-manual-lead'));
    const table = element('table', undefined, 'scene-manual-table');
    const head = document.createElement('thead'); const headRow = document.createElement('tr');
    ['维度', '看什么'].forEach(text => headRow.append(element('th', text)));
    head.append(headRow); table.append(head);
    const body = document.createElement('tbody');
    MANUAL_SCORING.forEach(([name, desc]) => {
      const row = document.createElement('tr');
      row.append(element('td', name), element('td', desc));
      body.append(row);
    });
    table.append(body); panel.append(table);
    const list = element('ul', undefined, 'scene-manual-list');
    [
      '训练总分：六个维度加权后的 0–100 分。',
      '拓扑相似度：你的操作序列与标准路径的结构相似程度。',
      '图编辑距离：把操作序列改成标准路径需要几步，越小越接近。',
      '禁忌操作：直接进入复盘反馈的致命问题清单，并压低安全类得分。'
    ].forEach(text => list.append(element('li', text)));
    panel.append(list);
    panel.append(element('p', '结案后可导出 JSON 报告，含六维分数、复盘反馈与操作时间线。', 'scene-manual-note'));
  }

  function setManualTab(index) {
    manualTab = Math.max(0, Math.min(2, Number(index) || 0));
    if (!manualDialog) return;
    $$('.scene-manual-tab', manualDialog).forEach((tab, i) => {
      const active = i === manualTab;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    $$('.scene-manual-panel', manualDialog).forEach((panel, i) => { panel.hidden = i !== manualTab; });
  }

  function renderManual() {
    if (!manualDialog) return;
    [fillManualOverview, fillManualModules, fillManualScoring].forEach((fill, index) => {
      const panel = $(`.scene-manual-panel[data-panel="${index}"]`, manualDialog);
      if (panel) fill(panel);
    });
    setManualTab(manualTab);
  }

  function openManual() {
    if (!manualDialog) return;
    renderManual();
    if (!manualDialog.open) manualDialog.showModal();
    $('.scene-manual-tab[aria-selected="true"]', manualDialog)?.focus();
  }

  function ensureManual() {
    if (!manualDialog) return;
    $('.scene-manual-close', manualDialog)?.addEventListener('click', () => manualDialog.close());
    manualDialog.addEventListener('click', event => { if (event.target === manualDialog) manualDialog.close(); });
    $$('.scene-manual-tab', manualDialog).forEach((tab, index) => {
      tab.addEventListener('click', () => setManualTab(index));
      tab.addEventListener('keydown', event => {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
        event.preventDefault();
        const next = (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
        setManualTab(next);
        $$('.scene-manual-tab', manualDialog)[next]?.focus();
      });
    });
    setManualTab(0);
  }

  /* ===== 病历记录区：已执行操作 + 检查结果摘要，按 virtual_time_seconds 排序展示 ===== */
  const ACTION_LABELS = {inquiry: '问诊', physical_exam: '体查', lab_order: '检验', medication: '处方', rescue_disposition: '转归'};
  function recordClock(action) {
    const total = Math.max(0, Math.round(Number(action?.virtual_time_seconds) || 0));
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  }
  function reportSummary(result) {
    if (!result || typeof result !== 'object') return '';
    const pairs = Object.entries(result.details || {}).slice(0, 2).map(([key, value]) => `${key} ${value}`);
    const conclusion = String(result.conclusion || result.interpretation || '').trim();
    const text = [...pairs, conclusion].filter(Boolean).join(' · ');
    return text.length > 72 ? `${text.slice(0, 71)}…` : text;
  }
  function renderRecord(source) {
    const root = $('#record-area');
    if (!root) return;
    const list = $('.scene-record-list', root);
    const empty = $('.scene-record-empty', root);
    const count = $('.scene-record-count', root);
    const actions = Array.isArray(source?.actions) ? source.actions : [];
    if (list) {
      list.replaceChildren();
      actions.forEach(action => {
        const item = element('li', undefined, 'scene-record-item');
        item.append(
          element('span', recordClock(action), 'scene-record-time'),
          element('span', ACTION_LABELS[String(action?.action_type || '').toLowerCase()] || '操作', 'scene-record-type'),
          element('span', String(action?.content || '（未记录内容）'), 'scene-record-text')
        );
        if (action?.is_contraindicated) item.append(element('span', `禁忌操作 · ${action.warning_message || '与安全底线冲突'}`, 'scene-record-flag'));
        const brief = reportSummary(action?.result);
        if (brief) item.append(element('span', brief, 'scene-record-report'));
        list.append(item);
      });
      list.hidden = actions.length === 0;
    }
    if (empty) empty.hidden = actions.length > 0;
    if (count) count.textContent = `${actions.length} 条`;
  }
  function openRecord() {
    const details = $('#record-area details');
    if (!details) return;
    renderRecord(snapshot);
    details.open = true;
    $('.scene-record-summary', details)?.focus();
  }
  function showLog() {
    const body = $('.scene-log-body', log); body.replaceChildren();
    const messages = snapshot?.dialogues || [];
    if (!messages.length) body.append(element('p', '尚无操作记录。选择页签或输入指令开始接诊。'));
    messages.slice(-200).forEach(m => {
      const div = element('div', undefined, `scene-message ${m.speaker}`);
      div.append(element('b', names[m.speaker] || m.role_title || '系统'), document.createTextNode(m.text)); body.append(div);
    });
    log.hidden = false; body.scrollTop = body.scrollHeight;
  }
  function reportCard(report) {
    const card = element('article', undefined, 'scene-report'); card.append(element('h3', report.title || report.item_name || '检查报告'));
    const details = report.details || {};
    Object.entries(details).forEach(([k,v]) => card.append(element('p', `${k}：${v}`)));
    if (!Object.keys(details).length && report.result) card.append(element('p', `结果：${report.result}`));
    card.append(element('p', report.conclusion || report.interpretation || '报告已签发'));
    return card;
  }
  function showReports(filter = '') {
    const body = openDialog(filter || '客观检查报告');
    const reports = (snapshot?.reports || []).filter(r => !filter || (filter === '心电图' ? r.modality === 'ECG' : filter === '影像检查' ? ['CTA','ECHO','IMAGING','ULTRASOUND'].includes(r.modality) || /CTA|超声/.test(r.title) : ['LAB','EXAM'].includes(r.modality)));
    if (!reports.length) body.append(element('p', '尚未取得该类报告。请先在“体查”或“检验”页签下达指令。'));
    reports.slice().reverse().forEach(r => body.append(reportCard(r)));
  }
  function updateSpeech() {
    const latestAction = (snapshot?.actions || []).at(-1);
    const actionTime = Number(latestAction?.timestamp || 0);
    const actionType = String(latestAction?.action_type || '');
    const latestHuman = (snapshot?.dialogues || []).filter(m => m.speaker === 'patient' || m.speaker === 'family').at(-1);
    const latestHumanTime = Number(latestHuman?.timestamp || 0);
    const latestHumanSignature = latestHuman ? `${latestHuman.timestamp}:${latestHuman.speaker}:${latestHuman.text}` : '';
    const isRecentSpontaneous = latestHumanTime > 0 && (Date.now() / 1000 - latestHumanTime) < 12;
    const hasCurrentResponse = latestHuman && (latestHumanTime >= actionTime || (actionType === 'INQUIRY' && actionTime - latestHumanTime < 5));
    const shouldShow = !!latestHuman && (hasCurrentResponse || isRecentSpontaneous) && Date.now() / 1000 - latestHumanTime < 12;
    if (shouldShow && latestHumanSignature !== speechSignature) {
      speechSignature = latestHumanSignature;
      clearTimeout(speechTimer);
      speechTimer = setTimeout(() => {
        $$('.speech.patient,.speech.family').forEach(el => { el.hidden = true; });
      }, 12000);
    }
    for (const role of ['patient', 'family']) {
      const last = (snapshot?.dialogues || []).filter(m => m.speaker === role).at(-1);
      const p = $(`.speech.${role} p`);
      const bubble = $(`.speech.${role}`);
      const messageTime = Number(last?.timestamp || 0);
      const roleVisible = shouldShow && Date.now() / 1000 - messageTime < 12;
      if (roleVisible && p && bubble) {
        p.textContent = last.text; p.style.fontSize = last.text.length > 70 ? '16px' : ''; bubble.hidden = false;
      } else if (p && bubble) {
        p.textContent = ''; bubble.hidden = true;
      }
    }
  }
  function radar(scores) {
    const svg = $('.radar-svg'); if (!svg) return;
    const keys = ['critical_first_aid','clinical_efficiency','physician_patient_comm','safety_awareness','differential_thinking','diagnostic_accuracy'];
    const points = keys.map((k,i) => { const a = -Math.PI/2 + i*Math.PI/3, r = Math.max(0, Math.min(100, scores?.[k] || 0)) * .9; return [306+Math.cos(a)*r,128+Math.sin(a)*r]; });
    const shape = $('polygon[fill-opacity]', svg); if (shape) shape.setAttribute('points', points.map(p=>p.join(',')).join(' '));
    $$('circle', svg).forEach((c,i) => { if (points[i]) { c.setAttribute('cx',points[i][0]); c.setAttribute('cy',points[i][1]); }});
    const legend = $('.radar-legend'); if (legend) legend.textContent = '依据已执行操作计算 · 规则评分';
  }
  function ecgY(phase, t, baseline) {
    // One normalized heartbeat: P wave, sharp QRS complex, then T wave.
    const noise = Math.sin(t * 3.7 + phase * 31) * .65;
    if (phase < .12) return baseline - 3.5 * Math.sin(Math.PI * phase / .12);
    if (phase >= .18 && phase < .205) return baseline + 5 * (phase - .18) / .025;
    if (phase >= .205 && phase < .225) return baseline + 5 - 58 * (phase - .205) / .02;
    if (phase >= .225 && phase < .255) return baseline - 53 + 98 * (phase - .225) / .03;
    if (phase >= .255 && phase < .28) return baseline + 45 - 45 * (phase - .255) / .025;
    if (phase >= .40 && phase < .54) return baseline - 8 * Math.sin(Math.PI * (phase - .40) / .14);
    return baseline + noise;
  }
  function renderDynamicWaveform(timestamp) {
    const dt = lastWaveAt === null ? 0 : Math.min(.1, Math.max(0, (timestamp - lastWaveAt) / 1000));
    lastWaveAt = timestamp;
    // A disconnected/finalized monitor holds its last trace; it must not imply
    // fresh clinical readings when the backend is no longer sending data.
    if (snapshot && streamConnected && !snapshot.is_finished) {
      const oldPhase = heartPhase;
      heartPhase = (heartPhase + dt * monitorState.hr / 60) % 1;
      breathPhase = (breathPhase + dt * monitorState.rr / 60) % 1;
      if (heartPhase < oldPhase && monitorState.patientState !== 'S6_CARDIAC_ARREST') window.monitorAudio?.playQRSBeep(monitorState.spo2);
    }
    if (waveLines.length && snapshot && streamConnected && !snapshot.is_finished) {
      const t = timestamp / 1000;
      const hr = Math.max(0, Number(monitorState.hr) || 0);
      // The strip represents roughly four seconds at 25 mm/s, so a 100–120
      // bpm rhythm shows about 6–8 QRS complexes across the monitor.
      const beats = Math.max(.1, hr / 60 * 4);
      const flat = hr <= 1 || monitorState.patientState === 'S6_CARDIAC_ARREST';
      const ecg = [], pleth = [], resp = [];
      for (let x = 0; x <= 560; x += 2) {
        const phase = ((x / 560 * beats + heartPhase) % 1 + 1) % 1;
        const y = flat ? 35 + Math.sin(t * 2 + x * .08) * .4 : ecgY(phase, t, 35);
        const plethPhase = phase;
        const plethY = flat ? 76 : 76 - 9 * Math.pow(Math.max(0, Math.sin(Math.PI * plethPhase)), 1.8);
        const respY = flat || monitorState.rr <= 0 ? 109 : 109 - 7 * Math.sin(2 * Math.PI * (x / 560 * monitorState.rr / 60 * 4 + breathPhase));
        ecg.push(`${x},${y.toFixed(1)}`); pleth.push(`${x},${plethY.toFixed(1)}`); resp.push(`${x},${respY.toFixed(1)}`);
      }
      waveLines[0].setAttribute('points', ecg.join(' '));
      if (waveLines[1]) waveLines[1].setAttribute('points', pleth.join(' '));
      if (waveLines[2]) waveLines[2].setAttribute('points', resp.join(' '));
    }
    requestAnimationFrame(renderDynamicWaveform);
  }
  requestAnimationFrame(renderDynamicWaveform);
  function telemetry(data) {
    snapshot = {...snapshot, ...data};
    const v = data.vitals;
    if (v) {
      lastTelemetryAt = performance.now();
      for (const field of ['hr', 'spo2', 'sbp', 'dbp', 'rr']) {
        if (typeof v[field] === 'number' && Number.isFinite(v[field])) monitorState[field] = v[field];
      }
      if (Number.isFinite(v.pain_vas)) monitorState.pain = v.pain_vas;
      monitorState.patientState = data.patient_state || snapshot.patient_state || 'S0_STABLE';
      const values = [Math.round(monitorState.hr), `${Math.round(monitorState.sbp)}/${Math.round(monitorState.dbp)}`, `${Math.round(monitorState.spo2)} %`, `${monitorState.pain} / 10`];
      $$('.vital strong').forEach((el,i) => { el.textContent = values[i]; });
      const wave = $('.wave-svg'); if (wave) wave.style.opacity = v.hr === 0 ? '.15' : '1';
      const label = $('.wave-label');
      if (label) label.lastElementChild.textContent = `ECG / Pleth / RESP · RR ${Math.round(monitorState.rr)}/min`;
      if (!snapshot.is_finished && ((data.alarms?.length || 0) > 0 || snapshot.rupture_countdown != null) && performance.now() - lastAlarmAt >= 3000) {
        lastAlarmAt = performance.now(); window.monitorAudio?.playCriticalAlarm();
      }
    }
    const seconds = snapshot.virtual_seconds || 0;
    const timer = $('.timer'); if (timer) timer.textContent = `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
    const trust = Math.round((snapshot.trust_level ?? .4)*100), anxiety = Math.round((snapshot.family_anxiety ?? .65)*100);
    [['.status-tag.trust',trust],['.status-tag.anxiety',anxiety]].forEach(([s,val]) => { const el=$(s); if(el){$('strong',el).textContent=`${val}%`;$('.bar span',el).style.width=`${val}%`;}});
    [[$('.relation:not(.anxious)'), trust], [$('.relation.anxious'), anxiety]].forEach(([el, val]) => {
      if (!el) return;
      const label = $('b', el), bar = $('.line span', el);
      if (label) label.textContent = `${val}%`;
      if (bar) bar.style.width = `${val}%`;
    });
    renderEmotionPanel(snapshot);
    renderPhase(snapshot.patient_state || monitorState.patientState);
    const criticalState = snapshot.rupture_countdown != null || (snapshot.patient_state && snapshot.patient_state !== 'S0_STABLE');
    alert.textContent = criticalState ? `模拟危急状态 · ${snapshot.patient_state || '生命体征变化'} · 声光报警已触发` : '';
    if (snapshot.is_finished) { const live=$('.live'); if(live) live.textContent='已结案 · 监护冻结'; }
    if (data.spontaneous_dialogues?.length) {
      const existing = new Set((snapshot.dialogues || []).map(m=>`${m.timestamp}:${m.text}`));
      snapshot.dialogues = [...(snapshot.dialogues || []),...data.spontaneous_dialogues.filter(m=>!existing.has(`${m.timestamp}:${m.text}`))]; updateSpeech();
    }
    updateSpeech();
  }
  function renderCaseProfile(profile) {
    if (!profile) return;
    const patient = profile.patient_profile || {};
    const name = patient.name || '虚拟患者';
    const age = patient.age ? `${patient.gender || ''} · ${patient.age} 岁` : (patient.gender || '');
    const companion = patient.family_companion || '';
    names.patient = `患者 · ${name}`;
    names.family = companion && !/独自|无（/.test(companion) ? '陪同者' : '家属';
    const patientName = $('.patient-name b'); if (patientName) patientName.textContent = name;
    const patientMeta = $('.patient-name span'); if (patientMeta) patientMeta.textContent = age;
    const identity = $('.patient-identity b'); if (identity) identity.textContent = name;
    const meta = $('.patient-meta'); if (meta) meta.textContent = age;
    const tag = $('.identity-tag'); if (tag) tag.textContent = `${profile.category || '教学病例'}`;
    const complaint = $('.record-row.complaint .value'); if (complaint) complaint.textContent = profile.chief_complaint || '主诉待问诊';
    const history = $('.record-row.history .value'); if (history) history.textContent = patient.past_history || '既往史待问诊。';
    const setSpeaker = (selector, text) => { const node = [...( $(selector)?.childNodes || [])].find(item => item.nodeType === Node.TEXT_NODE); if (node) node.nodeValue = text; };
    setSpeaker('.speech.patient .speaker', name);
    setSpeaker('.speech.family .speaker', names.family);
    const familyBubble = $('.speech.family'); if (familyBubble) familyBubble.hidden = !companion;
    const title = $('.work-title'); if (title && profile.title) title.dataset.caseTitle = profile.title;
    const startHint = $('.scene-hint'); if (startHint && profile.chief_complaint) startHint.lastChild.textContent = ` ${profile.chief_complaint}`;
    if (profile.case_id !== 'case_chest_pain_001') {
      const ext = profile.game_extensions || {};
      const exams = (ext.required_exams || []).slice(0, 4).map(item => [String(item), `安排${item}`]);
      const meds = ((ext.treatment_plan || {}).medications || []).slice(0, 4).map(item => [String(item.name || item), String(item.name || item)]);
      while (exams.length < 4) exams.push(['一般查体', '进行一般查体']);
      while (meds.length < 4) meds.push(['对症处理', '请根据病例目标制定对症处理方案']);
      menu = [
        {label: '问诊', type: 'inquiry', items: [['主要症状', '请描述主要症状、起病时间和严重程度'], ['发病经过', '症状从什么时候开始？有什么诱因？'], ['既往史与用药', '请问既往病史、用药和过敏史？'], ['安抚患者', '别紧张，我们会结合检查结果解释和处理。']]},
        {label: '体查', type: 'physical_exam', items: [['一般查体', '进行一般查体'], ['重点查体', '请根据主诉进行针对性查体'], ['生命体征', '复测生命体征'], ['沟通感受', '询问患者目前不适变化']]},
        {label: '检验', type: 'lab_order', items: exams},
        {label: '处方', type: 'medication', items: meds},
        {label: '转归', type: 'rescue_disposition', items: [['观察与复评', '安排观察并复评生命体征'], ['健康宣教', '向患者解释检查结果与注意事项'], ['复诊建议', '说明复诊和危险信号'], ['完成本例', '完成本病例的诊疗计划']]}
      ];
    } else {
      menu = defaultMenu.map(group => ({...group, items: group.items.map(item => [...item])}));
    }
    setTab(tab, false);
  }
  function setTab(i, openQuick = true) {
    if (state.ended && openQuick) { toast('本次训练已结束，可查看报告或开始下一次训练。'); return; }
    tab = i; $$('.dock-tab,.tab').forEach((el,j) => el.classList.toggle('active', j === i));
    const questions = $('.questions');
    if (questions) {
      questions.replaceChildren(element('div', `选择${menu[i].label}方向 · 所有操作均为虚拟训练`, 'questions-label'));
      menu[i].items.forEach(([title,cmd],j) => {
        const row=element('div',undefined,'inquiry'); row.append(element('div',String(j+1).padStart(2,'0'),'inquiry-num'));
        const desc=element('div'); desc.append(element('div',title,'inquiry-text'),element('div',cmd,'inquiry-hint'));
        const run=element('div',i===0?'询问':'执行','inquiry-button'); interactive(run,()=>execute(cmd)); row.append(desc,run); questions.append(row);
      });
    }
    input.placeholder=`输入${menu[i].label}指令……`;
    if (!workstation && openQuick) {
      const body=openDialog(`${menu[i].label} · 快捷操作`);
      const choices=element('div',undefined,'scene-actions');
      menu[i].items.forEach(([title,cmd])=>{const b=element('button',title);b.onclick=()=>{dialog.close();execute(cmd);};choices.append(b);});
      body.append(choices,element('p','也可以关闭此窗口，在底部输入自定义指令。','scene-footnote'));
    }
  }
  const inputWrapper = $('.question-entry .text-input') || $('.action-dock .text-input');
  const input = document.createElement('input'); input.className='scene-input'; input.placeholder='输入追问或诊疗指令……'; input.setAttribute('aria-label','问诊或诊疗指令'); input.maxLength=4000;
  inputWrapper.replaceChildren(input); input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing)submit();});
  function submit(){if(input.value.trim())execute(input.value.trim());}
  interactive($('.send'),submit,'发送指令');
  $$('.dock-tab,.tab').forEach((el,i)=>interactive(el,()=>setTab(i),menu[i].label));
  async function refresh() {
    snapshot=await api(`/api/case/session/${encodeURIComponent(sessionId)}/workspace`);
    renderCaseProfile(snapshot.case_profile);
    telemetry(snapshot); radar(snapshot.radar_scores); updateSpeech(); renderRecord(snapshot);
    $$('.report .paper-bottom span').forEach(el=>{el.textContent=(snapshot.reports?.length?'可查看':'未获取');});
  }
  async function execute(content) {
    if (busy || !sessionId) return;
    if (state.ended) {toast('本次训练已结束，可查看报告或开始下一次训练。');return;}
    busy=true;document.body.classList.add('is-busy');
    try {
      const result=await api('/api/action/execute',{session_id:sessionId,action_type:menu[tab].type,content,params:{}});
      renderTat({...result, content});
      input.value='';await refresh();showLog();
      if(result.report){const body=openDialog('客观检查报告');body.append(reportCard(result.report));}
      toast(result.warning || result.message,!!result.warning);
    } catch(e){toast(e.message,true);}finally{busy=false;document.body.classList.remove('is-busy');}
  }
  function connect() {
    if(stopped || !sessionId)return;
    const proto=location.protocol==='https:'?'wss:':'ws:';
    const socket=ws=new WebSocket(`${proto}//${location.host}/ws/vitals/${encodeURIComponent(sessionId)}`);
    socket.onmessage=e=>{if(ws!==socket)return;try{const data=JSON.parse(e.data);if(data.type==='vitals_update'){streamConnected=true;telemetry(data);}}catch(err){toast('监护数据解析失败',true);}};
    socket.onopen=()=>{if(ws!==socket)return;reconnectCount=0;streamConnected=true;lastTelemetryAt=performance.now();const el=$('.live');if(el)el.textContent=snapshot?.is_finished?'已结案 · 监护冻结':'实时监护 · 1 Hz';};
    socket.onclose=e=>{
      if(stopped || ws!==socket)return;
      streamConnected=false;
      const el=$('.live') || $('.hud-status');
      if(e.code===1008 || e.code===4404){
        if(el)el.textContent='会话已失效';
        toast('实训会话已失效，请刷新页面或开始下一次训练。',true);return;
      }
      if(reconnectCount>=5){
        if(el)el.textContent='监护已断开';
        toast('监护重连失败，请检查启动窗口并刷新页面。',true);return;
      }
      const delay=Math.min(2000*2**reconnectCount,15000);
      reconnectCount+=1;
      if(el)el.textContent=`监护重连中（${reconnectCount}/5）`;
      clearTimeout(reconnectTimer);reconnectTimer=setTimeout(connect,delay);
    };
  }
  const staleTimer=setInterval(()=>{
    if(streamConnected && lastTelemetryAt && performance.now()-lastTelemetryAt>6000){
      streamConnected=false;ws?.close();
    }
  },1000);
  // ===== 结案报告：结局卡 / 复盘知识点 / 情绪终值 =====
  // 后端字段可能暂未上线（outcome / knowledge_card），也可能是旧版响应：
  // 缺失时整块不渲染，绝不因为读取 undefined 抛错，报告其余部分照常显示。
  function reportOutcome(outcome) {
    if (!outcome || typeof outcome !== 'object' || Array.isArray(outcome)) return null;
    const code = String(outcome.code || '').trim().toUpperCase().slice(0, 1);
    const title = String(outcome.title || '').trim();
    const description = String(outcome.description || '').trim();
    if (!code && !title && !description) return null;
    return {code, title, description};
  }
  function reportKnowledge(card) {
    if (!card || typeof card !== 'object' || Array.isArray(card)) return null;
    const title = String(card.title || '').trim();
    const raw = card.points || card.items || card.key_points || card.bullets || card.tips || [];
    const points = (Array.isArray(raw) ? raw : String(raw).split('\n'))
      .map(point => {
        if (point && typeof point === 'object') {
          const heading = String(point.title || point.heading || '').trim();
          const body = String(point.one_liner || point.description || point.text || '').trim();
          const pitfall = String(point.pitfall || point.warning || '').trim();
          return heading || body || pitfall ? {heading, body, pitfall} : null;
        }
        const text = String(point == null ? '' : point).trim();
        return text ? {heading: '', body: text, pitfall: ''} : null;
      }).filter(Boolean);
    if (!title && !points.length) return null;
    return {title, points};
  }
  function appendOutcomeCard(body, outcome) {
    const card = element('section', undefined, `scene-outcome-card${outcome.code ? ` scene-outcome-${outcome.code.toLowerCase()}` : ''}`);
    const code = element('div', outcome.code || '·', 'scene-outcome-code');
    const text = element('div', undefined, 'scene-outcome-body');
    if (outcome.title) text.append(element('h3', outcome.title, 'scene-outcome-title'));
    if (outcome.description) text.append(element('p', outcome.description, 'scene-outcome-desc'));
    card.append(code, text); body.append(card);
  }
  function appendKnowledgeCard(body, knowledge) {
    const card = element('section', undefined, 'scene-knowledge-card');
    card.append(element('h3', knowledge.title || '复盘知识点', 'scene-knowledge-title'));
    if (knowledge.points.length) {
      const list = element('ul', undefined, 'scene-knowledge-list');
      knowledge.points.forEach(point => {
        const item = element('li', undefined, 'scene-knowledge-item');
        if (point.heading) item.append(element('b', point.heading, 'scene-knowledge-item-title'));
        if (point.body) item.append(element('span', point.body, 'scene-knowledge-item-body'));
        if (point.pitfall) item.append(element('span', point.pitfall, 'scene-knowledge-item-pitfall'));
        list.append(item);
      });
      card.append(list);
    }
    body.append(card);
  }
  function finaleValues(source) {
    const pick = (...keys) => {
      if (!source || typeof source !== 'object') return null;
      for (const key of keys) {
        const raw = source[key];
        if (raw == null || raw === '') continue;
        const number = Number(raw);
        if (!Number.isFinite(number)) continue;
        return Math.max(0, Math.min(1, number > 1 ? number / 100 : number));
      }
      return null;
    };
    return {
      trust: pick('trust_level', 'patient_trust', 'trust'),
      patient_anxiety: pick('patient_anxiety'),
      family_anxiety: pick('family_anxiety')
    };
  }
  function appendEmotionFinale(body, data) {
    // 结案响应优先；没有就回落到会话快照（/workspace 与 WS 帧同源），仍缺则整块隐藏。
    const live = finaleValues(data), cached = finaleValues(snapshot);
    const rows = [
      ['信任度', live.trust ?? cached.trust, 'scene-finale-trust'],
      ['患者焦虑', live.patient_anxiety ?? cached.patient_anxiety, 'scene-finale-anxiety'],
      ['家属焦虑', live.family_anxiety ?? cached.family_anxiety, 'scene-finale-family']
    ].filter(([, value]) => value != null);
    if (!rows.length) return;
    const card = element('section', undefined, 'scene-emotion-finale');
    card.append(element('h3', '结案时情绪终值', 'scene-finale-title'));
    const grid = element('div', undefined, 'scene-finale-grid');
    rows.forEach(([label, value, cls]) => {
      const item = element('div', undefined, `scene-finale-item ${cls}`);
      item.append(element('span', label, 'scene-finale-label'),
        element('b', `${Math.round(value * 100)}%`, 'scene-finale-value'));
      grid.append(item);
    });
    card.append(grid, element('p', '取结案时刻的会话状态，用于复盘沟通效果。', 'scene-footnote'));
    body.append(card);
  }
  function timelineVerdict(item) {
    if (item.is_fatal) return '禁忌';
    return item.matched_golden ? '正常' : '无效';
  }
  function formatTimelineOffset(seconds) {
    const total = Math.max(0, Math.round(seconds));
    return `第 ${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, '0')} 秒`;
  }
  function timelineAttribution(items) {
    if (items.length < 3) return '动作过少，无法归因。';
    const fatal = items.find(item => item.fatal);
    if (fatal) return `最先出错的一步是「${fatal.name}」——禁忌操作，直接触发生命风险。`;
    const invalid = items.find(item => item.invalid);
    if (invalid) return `最先出错的一步是「${invalid.name}」——未命中关键处置步骤。`;
    return '关键动作顺序完整，未发现明显错误。';
  }
  function renderTimeline(body, data) {
    const raw = Array.isArray(data?.action_sequence_summary) ? data.action_sequence_summary : [];
    if (!raw.length) return;
    const stamps = raw.map(item => Number(item?.timestamp)).filter(Number.isFinite);
    const origin = stamps.length ? Math.min(...stamps) : NaN;
    const items = raw.map((action, index) => ({
      order: index,
      offset: Number.isFinite(Number(action?.timestamp)) && Number.isFinite(origin) ? Number(action.timestamp) - origin : NaN,
      name: String(action?.content || action?.type || '动作').slice(0, 90),
      verdict: timelineVerdict(action || {}),
      fatal: !!action?.is_fatal,
      invalid: !action?.is_fatal && !action?.matched_golden
    }));
    // 禁忌/致命项置顶，其余按时间升序；无时间戳时退回原始顺序。
    items.sort((a, b) => {
      if (a.fatal !== b.fatal) return a.fatal ? -1 : 1;
      if (Number.isFinite(a.offset) && Number.isFinite(b.offset)) return a.offset - b.offset;
      return a.order - b.order;
    });
    const details = document.createElement('details');
    details.className = 'scene-timeline';
    details.append(element('summary', `操作时间线（${items.length} 项）`));
    items.forEach(item => {
      const row = element('p', undefined, `scene-timeline-item${item.fatal ? ' scene-timeline-item-fatal' : ''}${item.invalid ? ' scene-timeline-invalid' : ''}`);
      const time = Number.isFinite(item.offset) ? formatTimelineOffset(item.offset) : `第 ${item.order + 1} 步`;
      row.append(element('span', time, 'scene-timeline-time'),
        element('span', item.name, 'scene-timeline-name'),
        element('span', `· ${item.verdict}`, 'scene-timeline-result'));
      details.append(row);
    });
    details.append(element('p', timelineAttribution(items), 'scene-timeline-attribution'));
    body.append(details);
  }
  function renderAssessment(data) {
    const body=openDialog('OSCE 临床思维训练报告');
    const outcome=reportOutcome(data.outcome || data.assessment?.outcome);
    if(outcome)appendOutcomeCard(body,outcome);
    const metrics=element('div',undefined,'scene-metrics');
    [['训练总分',`${data.total_score} / 100`],['拓扑相似度',`${Math.round(data.topological_similarity*100)}%`],['图编辑距离',data.ged_distance]].forEach(([label,val])=>{const d=element('div');d.append(element('small',label),element('b',val));metrics.append(d);});body.append(metrics);
    appendEmotionFinale(body, data);
    const table=document.createElement('table');Object.entries(data.radar_scores).forEach(([key,val])=>{const tr=table.insertRow();tr.insertCell().textContent=({critical_first_aid:'急救处置力',diagnostic_accuracy:'确诊决策度',differential_thinking:'鉴别思维',safety_awareness:'安全意识',physician_patient_comm:'医患沟通',clinical_efficiency:'临床效率'})[key]||key;tr.insertCell().textContent=`${val} / 100`;});body.append(table);
    body.append(element('h3','复盘反馈'));
    [...data.fatal_errors,...data.clinical_feedback].forEach(t=>body.append(element('p',t)));
    const knowledge=reportKnowledge(data.knowledge_card || data.assessment?.knowledge_card || data.knowledge);
    if(knowledge)appendKnowledgeCard(body,knowledge);
    renderTimeline(body, data);
    const actions=element('div',undefined,'scene-actions');const download=element('button','导出 JSON 报告');download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`ClinMind-${sessionId}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};const next=element('button','开始下一次训练','secondary');next.onclick=()=>startNew();actions.append(download,next);body.append(actions,element('p','此报告由演示病例与规则评分生成，用于临床思维教学模拟。','scene-footnote'));
  }
  function evaluation() {
    if(snapshot?.assessment){renderAssessment({...snapshot.assessment,session_id:sessionId});return;}
    const body=openDialog('结案生成报告');body.append(element('p','填写本次训练的最终判断，结案后将停止计时并冻结本次操作。'));
    const label=element('label','最终诊断');const diagnosis=element('input');diagnosis.placeholder='请输入最终诊断';diagnosis.maxLength=1000;label.append(diagnosis);body.append(label);
    const button=element('button','提交并生成报告');const status=element('p');body.append(button,status);
    button.onclick=async()=>{if(!diagnosis.value.trim()){status.textContent='请填写最终诊断';return;}button.disabled=true;status.textContent='正在计算评分……';try{const report=await api('/api/eval/submit',{session_id:sessionId,final_diagnosis:diagnosis.value.trim()});await refresh();renderAssessment(report);}catch(e){status.textContent=e.message;button.disabled=false;}};
  }
  function startNew() {
    const body=openDialog('开始下一次训练');body.append(element('p','选择病例后会归档当前训练，并从选定病例重新开始。全部病例均为虚构教学数据。'));
    const list=element('div',undefined,'case-picker');body.append(list);
    api('/api/case/list').then(cases=>{
      cases.forEach(item=>{
        const b=element('button',`${item.title} · ${item.difficulty}`);b.className='secondary';b.title=item.chief_complaint || item.target_competency;
        b.onclick=async()=>{b.disabled=true;try{if(sessionId && !snapshot?.is_finished)await api(`/api/case/session/${encodeURIComponent(sessionId)}/pause`,{});localStorage.removeItem('clinmind-game-session');localStorage.setItem('clinmind-case-id',item.case_id);stopped=true;streamConnected=false;clearTimeout(reconnectTimer);ws?.close();ws=null;sessionId=null;snapshot=null;await initialize();dialog.close();toast(`已开始：${item.title}`);}catch(e){toast(e.message,true);b.disabled=false;}};
        list.append(b);
      });
    }).catch(e=>list.append(element('p',`病例库读取失败：${e.message}`)));
  }
  function settings() {
    const body=openDialog('AI 模型配置');body.classList.add('scene-settings');
    body.append(element('p','默认离线即可完成全部训练。在线模式会将模拟对话发送至所选服务商。'));
    const select=document.createElement('select');[['offline','离线规则引擎（免 Key）'],['deepseek','DeepSeek'],['openai','OpenAI'],['custom','自定义兼容接口']].forEach(([v,t])=>{const o=element('option',t);o.value=v;select.append(o);});body.append(select);
    const key=element('input');key.type='password';key.autocomplete='off';key.placeholder='API Key（仅保存在本机 .env，留空沿用已保存值）';const base=element('input');base.placeholder='接口 Base URL';const model=element('input');model.placeholder='模型名称';body.append(key,base,model);
    select.setAttribute('aria-label','模型服务商');key.setAttribute('aria-label','API Key');base.setAttribute('aria-label','接口 Base URL');model.setAttribute('aria-label','模型名称');
    const volumeLabel = element('label','监护音量'); const volume = document.createElement('input'); volume.type='range'; volume.min='0'; volume.max='1'; volume.step='.01'; volume.value=String(window.monitorAudio?.volume ?? .08); volumeLabel.append(volume); body.append(volumeLabel);
    volume.oninput=()=>window.monitorAudio?.setVolume(volume.value);
    const presets={deepseek:['https://api.deepseek.com/v1','deepseek-chat'],openai:['https://api.openai.com/v1','gpt-4o-mini'],offline:['','offline-rule-engine'],custom:['http://127.0.0.1:11434/v1','']};
    const actions=element('div',undefined,'scene-actions'); const test=element('button','测试连接并保存'); const save=element('button','仅保存','secondary'); actions.append(test,save); body.append(actions);
    const status=element('p'); status.className='connection-status'; body.append(status);
    const statusLabel=element('span','连接状态：'); const statusValue=element('strong','尚未测试'); const statusRow=element('div',undefined,'connection-state connection-unknown'); statusRow.append(statusLabel,statusValue); body.insertBefore(statusRow,status);
    function showConnection(ok, message, testedAt='') {
      statusRow.classList.toggle('connection-ok', ok === true); statusRow.classList.toggle('connection-failed', ok === false); statusRow.classList.toggle('connection-unknown', ok !== true && ok !== false);
      statusValue.textContent = select.value === 'offline' ? '离线模式' : ok === true ? '已连接' : ok === false ? '连接失败' : '尚未测试';
      status.textContent = select.value === 'offline' ? '离线规则引擎，无需 API Key 或连接测试。' : (message || '点击“测试连接并保存”验证服务。') + (testedAt ? `（最近测试：${new Date(testedAt).toLocaleString()}）` : '');
    }
    select.onchange=()=>{
      [base.value,model.value]=presets[select.value];
      for(const field of [key,base,model])field.disabled=select.value==='offline';
      showConnection(null,'配置已更改，需重新测试并保存。');
    };
    for(const field of [key,base,model])field.oninput=()=>showConnection(null,'配置已更改，需重新测试并保存。');
    async function persist(testConnection){
      test.disabled=true; save.disabled=true;
      statusValue.textContent=testConnection && select.value!=='offline'?'连接测试中…':'保存中…';
      try { const r=await api('/api/config/llm',{provider:select.value,api_key:key.value,base_url:base.value,model:model.value,test_connection:testConnection}); showConnection(r.connection_ok, r.message, r.connection_tested_at); key.value=''; }
      catch(e){showConnection(false,e.message);}
      finally{test.disabled=false;save.disabled=false;}
    }
    test.onclick=()=>persist(true); save.onclick=()=>persist(false);
    api('/api/config/llm').then(current=>{select.value=current.provider || 'offline'; base.value=current.base_url || ''; model.value=current.model || ''; select.onchange(); if(current.provider !== 'offline') { base.value=current.base_url || base.value; model.value=current.model || model.value; } showConnection(current.connection_ok, current.connection_message, current.connection_tested_at); }).catch(()=>showConnection(false,'无法读取当前连接状态，请检查服务是否启动。'));
    select.onchange();
  }
  const tools=element('div',undefined,'scene-tools');
  [['接诊记录',showLog],['病历记录',openRecord],['说明书',openManual],['报告',()=>showReports()],['结案报告',evaluation],['AI 配置',settings]].forEach(([text,fn])=>{const b=element('button',text);b.onclick=fn;tools.append(b);});
  ($('.work-header') || $('.hud')).append(tools);
  interactive($('.monitor-label'),()=>{location.href='/static/workstation.html';},'打开工作站');
  // The monitor illustration itself is a hit target as well.
  if(!workstation){const hit=element('div');Object.assign(hit.style,{position:'absolute',left:'85px',top:'402px',width:'397px',height:'220px',cursor:'pointer'});canvas.append(hit);interactive(hit,()=>{location.href='/static/workstation.html';},'打开电脑工作站');}
  interactive($('.return-btn'),()=>{location.href='/static/reception.html';});
  interactive($('.next-btn'),startNew);interactive($('.end-btn'),evaluation);
  $$('.report').forEach((el,i)=>interactive(el,()=>showReports(['心电图','检验报告','影像检查'][i])));
  $$('.square-button').forEach((el,i)=>{
    if(i===0){const muted=window.monitorAudio?.isMuted;el.classList.toggle('is-muted',!!muted);el.setAttribute('aria-pressed',String(!!muted));}
    if(i === 0) interactive(el,()=>{ const muted=window.monitorAudio?.toggleMute() ?? false; el.setAttribute('aria-label',muted?'打开声音':'关闭声音'); el.setAttribute('aria-pressed',String(muted)); el.title=muted?'打开声音':'关闭声音'; el.classList.toggle('is-muted',muted); toast(muted?'声音已关闭':'声音已打开'); },window.monitorAudio?.isMuted?'打开声音':'关闭声音');
    else if(i === 1) interactive(el,settings,'打开设置');
  });
  addEventListener('keydown',e=>{
    const tag=document.activeElement?.tagName;
    if(e.isComposing||e.ctrlKey||e.metaKey||e.altKey||document.activeElement?.isContentEditable)return;
    // 有任一 modal（免责声明 / 说明书 / 通用弹窗）开着时，快捷键交给该 modal 自己处理：
    // 否则 Esc 会一边关窗一边跳页。
    if(anyModalOpen()||['INPUT','TEXTAREA','SELECT'].includes(tag))return;
    if(e.key==='Escape'&&workstation)location.href='/static/reception.html';
    if(!workstation && e.key.toLowerCase()==='e') location.href='/static/workstation.html';
    if(!workstation && /^[1-5]$/.test(e.key)) setTab(Number(e.key)-1);
  });
  addEventListener('beforeunload',()=>{stopped=true;clearTimeout(reconnectTimer);clearTimeout(speechTimer);clearInterval(staleTimer);clearInterval(emotionTimer);ws?.close();});
  // 验收09：localStorage 里可能留着一个已结案/已终止的旧会话，恢复它只会把用户卡在冻结界面。
  // 判定只依赖快照里真实存在的字段（is_finished / terminal_reason），以及本页已推算出的 state.ended；
  // 工作区快照没有创建时间/最后活动时间字段，所以不做「过期时长」判断，避免硬造数据。
  function sessionEnded() {
    const reason = snapshot?.terminal_reason;
    return state.ended === true || !!snapshot?.is_finished || (typeof reason === 'string' && reason.trim() !== '');
  }
  async function initialize() {
    stopped=false;reconnectCount=0;lastAlarmAt=-Infinity;
    state.ended=false;state.refused=false;state.lastSampleAt=0;
    for(const key of Object.keys(state.history))state.history[key]=[];
    for(const key of Object.keys(state.trendHint))state.trendHint[key]=null;
    document.body.classList.remove('scene-ended','is-finished');
    sessionId=localStorage.getItem('clinmind-game-session');
    let discarded=false;
    if(sessionId){try{await refresh();
      if(sessionEnded()){
        localStorage.removeItem('clinmind-game-session');
        sessionId=null;snapshot=null;discarded=true;
        state.ended=false;state.refused=false;
        document.body.classList.remove('scene-ended','is-finished');
      }
    }catch(e){if(e.status!==404)throw e;sessionId=null;localStorage.removeItem('clinmind-game-session');}}
    if(!sessionId){const caseId=localStorage.getItem('clinmind-case-id') || 'case_chest_pain_001';const data=await api('/api/case/start',{case_id:caseId,student_name:'临床思维训练学员'});sessionId=data.session_id;localStorage.setItem('clinmind-game-session',sessionId);await refresh();}
    // 刷新完成后再提示，保证状态条展示的是新会话而不是被丢弃的旧终态。
    if(discarded)toast('上一次训练已结束，已为你开启新会话');
    if(workstation)setTab(0,false);
    connect();
  }
  // Keep the visual draft's unknown history consistent with the case engine.
  const history=$('.record-row.history .value');if(history)history.textContent='高血压病史，具体年限与用药待问诊。';
  const familySpeech=$('.speech.family p');if(familySpeech)familySpeech.textContent='他平时有高血压，具体用药情况还需要问他。';
  ensureManual();
  ensureDisclaimer();
  renderRecord(snapshot);
  startEmotionTicker();
  initialize().catch(e=>toast(`服务连接失败：${e.message}。请使用 start.cmd 启动后访问页面。`,true));
})();
