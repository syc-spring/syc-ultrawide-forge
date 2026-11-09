/* ULTRAWIDE FORGE 前端逻辑:控制台 ↔ SSE 进度 ↔ 全景查看器 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const RATIOS = [
  { label: "3:1", note: "单块可达" },
  { label: "6:1", note: "2 块+" },
  { label: "8:1", note: "题目要求" },
  { label: "10:1", note: "" },
  { label: "12:1", note: "极限宽" },
  { label: "21:9", note: "影视" },
];

const state = {
  caps: null,
  ratio: "8:1",
  height: 1024,
  jobId: null,
  es: null,          // EventSource
  pollTimer: null,
  t0: 0,
  tick: null,
  seamXs: [],        // 接缝位置(占输出宽度比例)
  tilesMeta: [],
};

/* ───────── 初始化 ───────── */
async function init() {
  await loadCaps();
  bindControls();
  renderEstimate();
  loadShelf("history");
}

async function loadCaps() {
  try {
    const r = await fetch("/api/capabilities");
    state.caps = await r.json();
  } catch {
    state.caps = { tile_width: 3072, tile_height: 1024, overlap_ratio: 0.25, presets: RATIOS.map(x => x.label) };
  }
  const c = state.caps;
  const chips = $("#status-chips");
  const html = [];
  if (c.mock) html.push(`<span class="s-chip mock">MOCK 演示模式 · 未配置 API Key</span>`);
  else html.push(`<span class="s-chip live">${c.model || "image-api"}</span>`);
  html.push(`<span class="s-chip">单块 ${c.tile_width}×${c.tile_height}(≈${(c.tile_width / c.tile_height).toFixed(1)}:1)</span>`);
  html.push(`<span class="s-chip">重叠 ${Math.round(c.overlap_ratio * 100)}%</span>`);
  chips.innerHTML = html.join("");
}

function bindControls() {
  // 比例选择
  const grid = $("#ratio-grid");
  grid.innerHTML = RATIOS.map((r) => {
    const rw = Math.min(76, 16 + parseFloat(r.label) * 5.5);
    return `<div class="ratio-opt${r.label === state.ratio ? " on" : ""}" data-ratio="${r.label}">
      <span class="bar" style="width:${rw}px"></span>
      <span class="r-label">${r.label}</span>
      <span class="r-sub">${r.note}</span>
    </div>`;
  }).join("");
  grid.addEventListener("click", (e) => {
    const opt = e.target.closest(".ratio-opt");
    if (!opt) return;
    selectRatio(opt.dataset.ratio);
  });

  // 自定义比例
  $("#ratio-custom").addEventListener("input", (e) => {
    const v = e.target.value.trim();
    if (/^\d{1,3}([.:]\d{1,3})?$/.test(v)) {
      const norm = v.includes(":") || v.includes(".") ? v.replace(".", ":") : v + ":1";
      selectRatio(norm, true);
    }
  });

  // 高度
  $("#height-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $$("#height-seg button").forEach((x) => x.classList.toggle("on", x === b));
    state.height = +b.dataset.h;
    renderEstimate();
  });

  // 示例提示词
  $("#prompt-presets").addEventListener("click", (e) => {
    const c = e.target.closest(".chip");
    if (c) { $("#prompt").value = c.dataset.prompt; $("#prompt").focus(); }
  });

  $("#btn-generate").addEventListener("click", generate);

  // 结果工具
  $("#btn-guides").addEventListener("click", toggleGuides);
  bindPan();

  // 架子 tab
  $$(".tab").forEach((t) => t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("on", x === t));
    loadShelf(t.dataset.tab);
  }));
}

function selectRatio(ratio, fromCustom = false) {
  state.ratio = ratio;
  if (!fromCustom) $("#ratio-custom").value = "";
  $$(".ratio-opt").forEach((o) => o.classList.toggle("on", o.dataset.ratio === ratio));
  renderEstimate();
}

/* ───────── 工程读数估算(与后端 compute_plan 同一套公式)───────── */
function parseRatio(s) {
  const m = /^\s*(\d{1,3}(?:\.\d+)?)\s*[:/]\s*(\d{1,3}(?:\.\d+)?)\s*$/.exec(s);
  if (!m) return null;
  return parseFloat(m[1]) / parseFloat(m[2]);
}

function renderEstimate() {
  const ratio = parseRatio(state.ratio);
  const el = $("#estimate");
  const c = state.caps || { tile_width: 3072, tile_height: 1024, overlap_ratio: 0.25 };
  if (!ratio) { el.innerHTML = `比例格式:<b>W:H</b>,如 6:1、8:1、21:9`; return; }
  const tw = c.tile_width, th = c.tile_height;
  const overlap = Math.max(64, Math.round(tw * c.overlap_ratio));
  const targetW = Math.round(th * ratio);
  const n = targetW <= tw ? 1 : 1 + Math.ceil((targetW - tw) / (tw - overlap));
  const outW = Math.round(state.height * ratio);
  const overLimit = ratio > tw / th;
  el.innerHTML = `
    目标输出 <b>${outW}×${state.height}</b>(${state.ratio})<br>
    生成块数 <b>${n}</b> · API 调用 ${n} 次 · 单块 ${tw}×${th}<br>
    ${overLimit ? `<span class="warn">超出单次 ${(tw / th).toFixed(1)}:1 上限 → 滚动外绘模式</span>` : `单块直出模式`}`;
}

/* ───────── 触发生成 ───────── */
async function generate() {
  const prompt = $("#prompt").value.trim();
  if (prompt.length < 2) { $("#prompt").focus(); return flashFootnote("请先输入画面描述(至少 2 个字符)"); }
  const ratio = parseRatio(state.ratio);
  if (!ratio) return flashFootnote("比例格式应为 W:H,例如 6:1");

  $("#btn-generate").disabled = true;
  $("#btn-sub").textContent = "generating…";
  openProgress(prompt);

  try {
    const r = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, ratio: state.ratio, height: state.height }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
    const { job_id } = await r.json();
    state.jobId = job_id;
    subscribe(job_id);
  } catch (e) {
    failJob(`提交失败:${e.message}`);
  }
}

function flashFootnote(msg) {
  const f = $("#footnote");
  f.textContent = msg;
  f.style.color = "var(--amber)";
  setTimeout(() => { f.style.color = ""; f.textContent = "图 1 文生图起步 · 其余各块以上块右缘为锚续画 · 重叠区线性羽化拼接"; }, 2600);
}

/* ───────── 进度区 ───────── */
function openProgress(prompt) {
  $("#empty").classList.add("hidden");
  $("#result").classList.add("hidden");
  $("#progress").classList.remove("hidden");
  $("#filmstrip").innerHTML = "";
  $("#beats").classList.add("hidden");
  $("#log").textContent = "";
  setProgressHead(`已提交:${prompt.slice(0, 24)}${prompt.length > 24 ? "…" : ""}`, "");
  state.t0 = performance.now();
  clearInterval(state.tick);
  state.tick = setInterval(() => { $("#progress-elapsed").textContent = ((performance.now() - state.t0) / 1000).toFixed(1) + "s"; }, 100);
}

function setProgressHead(text, cls) {
  $("#progress-label").textContent = text;
  const d = $("#rec-dot");
  d.className = "rec-dot" + (cls ? " " + cls : "");
}

function logLine(msg, cls = "") {
  const el = $("#log");
  const t = new Date().toTimeString().slice(3, 8);
  el.innerHTML += `<span class="l-time">${t}</span> <span class="${cls}">${msg}</span>\n`;
  el.scrollTop = el.scrollHeight;
}

function frameEl(i) { return $(`#frame-${i}`); }

function ensureFrames(n) {
  const strip = $("#filmstrip");
  strip.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const f = document.createElement("div");
    f.className = "frame"; f.id = `frame-${i}`;
    f.innerHTML = `<div class="placeholder">待曝光</div><div class="f-tag"><b>Tile ${i + 1}</b> · ${i === 0 ? "文生图" : "锚定续画"}</div>`;
    strip.appendChild(f);
  }
}

function showThumb(i, thumb) {
  const f = frameEl(i);
  if (!f) return;
  f.querySelector(".placeholder")?.remove();
  const img = document.createElement("img");
  img.src = thumb;
  img.alt = `tile ${i + 1}`;
  f.prepend(img);
  f.scrollIntoView({ behavior: "smooth", inline: "end", block: "nearest" });
}

function renderBeats(beats, cur) {
  const box = $("#beats");
  if (!beats?.length) return;
  box.classList.remove("hidden");
  box.innerHTML = beats.map((b, i) =>
    `<div class="beat${i === cur ? " cur" : ""}"><span class="b-i">▚ ${String(i + 1).padStart(2, "0")}</span><span>${b}</span></div>`).join("");
}

/* ───────── SSE 订阅(失败自动降级轮询)───────── */
function subscribe(jobId) {
  closeStream();
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  state.es = es;
  es.onmessage = (e) => handleEvent(JSON.parse(e.data));
  es.addEventListener("end", () => closeStream());
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      closeStream();
      logLine("SSE 断开,降级为轮询…", "l-err");
      startPolling(jobId);
    }
  };
}

function closeStream() {
  state.es?.close();
  state.es = null;
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function startPolling(jobId) {
  state.pollTimer = setInterval(async () => {
    try {
      const j = await (await fetch(`/api/jobs/${jobId}`)).json();
      if (j.done_tiles > 0) for (let i = 0; i < j.done_tiles; i++) showThumb(i, `${location.origin}/api/results/${jobId}/tile/${i + 1}`);
      if (j.status === "done") { handleEvent({ type: "done", result_url: j.result_url, width: j.width, height: j.height_out, elapsed: j.elapsed }); }
      if (j.status === "error") handleEvent({ type: "error", message: j.error });
    } catch { /* 网络抖动,下个周期再试 */ }
  }, 2500);
}

function handleEvent(ev) {
  switch (ev.type) {
    case "status":
      setProgressHead(ev.message, ev.status === "harmonizing" ? "" : "");
      logLine(ev.message);
      break;
    case "planned":
      ensureFrames(ev.n_tiles);
      renderBeats(ev.beats, 0);
      state.seamXs = computeSeams(ev.geo, ev.n_tiles);
      logLine(`分镜完成:${ev.n_tiles} 块 · 目标 ${ev.geo.target_w}×${ev.geo.tile_h} → 输出 ${ev.geo.out_w}×${ev.geo.out_h}`);
      setProgressHead(`规划完成,共 ${ev.n_tiles} 块`);
      break;
    case "tile_start":
      setProgressHead(ev.message, "");
      renderBeats(state._beats, ev.i);
      logLine(`${ev.message} …`);
      break;
    case "tile_done":
      showThumb(ev.i, ev.thumb);
      if (ev.total > 0) renderBeats(state._beats, ev.i + 1);
      logLine(`Tile ${ev.i + 1} 完成 · 累计宽度 ${ev.width}px`, "l-ok");
      setProgressHead(`已完成 ${ev.done}/${ev.total} 块`);
      break;
    case "done":
      clearInterval(state.tick);
      setProgressHead(`生成完成 · ${ev.elapsed}s`, "ok");
      logLine(`✔ 完成:${ev.width}×${ev.height},耗时 ${ev.elapsed}s`, "l-ok");
      setTimeout(() => showResult(ev), 700);
      loadShelf(currentTab());
      break;
    case "error":
      failJob(ev.message);
      break;
  }
  if (ev.type === "planned") state._beats = ev.beats;
}

function failJob(msg) {
  clearInterval(state.tick);
  closeStream();
  setProgressHead("生成失败", "err");
  logLine(`✘ ${msg}`, "l-err");
  $("#btn-generate").disabled = false;
  $("#btn-sub").textContent = "rolling outpainting";
}

/* 接缝位置:与后端 stitch 几何一致(工作坐标系比例) */
function computeSeams(geo, n) {
  if (!geo || n <= 1) return [];
  const advance = geo.tile_w - geo.overlap_px;
  const xs = [];
  for (let k = 1; k < n; k++) {
    const waK = geo.tile_w + k * advance;          // 拼完第 k+1 块后的宽度
    xs.push((waK - geo.overlap_px / 2) / geo.target_w);
  }
  return xs;
}

/* ───────── 结果查看器 ───────── */
function showResult(ev) {
  const url = ev.result_url || `/api/results/${state.jobId}/final.png`;
  $("#progress").classList.add("hidden");
  $("#result").classList.remove("hidden");
  $("#result-dims").textContent = `${ev.width} × ${ev.height} · ${(ev.width / ev.height).toFixed(2)} : 1`;
  const img = $("#pan-img");
  img.src = url + "?t=" + Date.now();
  $("#btn-download").href = url;
  $("#btn-download").setAttribute("download", `ultrawide_${ev.width}x${ev.height}.png`);
  $("#btn-raw").onclick = () => window.open(url, "_blank");
  $("#panwrap").scrollLeft = 0;
  img.decode?.().then(() => updateMinimap()).catch(() => {});
  $("#btn-generate").disabled = false;
  $("#btn-sub").textContent = "rolling outpainting";
}

function bindPan() {
  const wrap = $("#panwrap");
  let down = false, sx = 0, sl = 0;
  wrap.addEventListener("pointerdown", (e) => { down = true; sx = e.clientX; sl = wrap.scrollLeft; wrap.setPointerCapture(e.pointerId); });
  wrap.addEventListener("pointermove", (e) => { if (down) wrap.scrollLeft = sl - (e.clientX - sx); });
  wrap.addEventListener("pointerup", () => { down = false; });
  wrap.addEventListener("scroll", updateMinimap);
  window.addEventListener("resize", updateMinimap);
}

function updateMinimap() {
  const wrap = $("#panwrap"), img = $("#pan-img"), view = $("#minimap-view");
  if (!img.naturalWidth) return;
  const frac = Math.min(1, wrap.clientWidth / img.clientWidth);
  view.style.width = frac * 100 + "%";
  view.style.left = (wrap.scrollLeft / (img.clientWidth - wrap.clientWidth || 1)) * (100 - frac * 100) + "%";
}

function toggleGuides() {
  const btn = $("#btn-guides"), box = $("#guides");
  const on = !btn.classList.contains("on");
  btn.classList.toggle("on", on);
  box.innerHTML = "";
  if (!on) return;
  box.innerHTML = state.seamXs.map((x, i) =>
    `<div class="g" data-label="缝 ${i + 1}" style="left:${(x * 100).toFixed(2)}%"></div>`).join("");
  if (!state.seamXs.length) box.innerHTML = `<div class="g" data-label="单块直出,无接缝" style="left:50%"></div>`;
}

/* ───────── 历史 / 样例架子 ───────── */
function currentTab() { return $$(".tab").find((t) => t.classList.contains("on"))?.dataset.tab || "history"; }

async function loadShelf(tab) {
  const body = $("#shelf-body");
  try {
    if (tab === "history") {
      const { jobs } = await (await fetch("/api/jobs")).json();
      if (!jobs.length) { body.innerHTML = `<div class="shelf-empty">还没有生成记录 —— 在左侧提交第一个任务</div>`; return; }
      body.innerHTML = jobs.map(renderCard).join("");
      $$(".h-card[data-id]").forEach((card) => card.addEventListener("click", () => {
        const j = jobs.find((x) => x.id === card.dataset.id);
        if (j.status === "done") showResult({ result_url: j.result_url, width: j.width, height: j.height_out });
      }));
    } else {
      const { samples } = await (await fetch("/api/samples")).json();
      if (!samples.length) { body.innerHTML = `<div class="shelf-empty">案例集为空 —— 仓库 samples/ 目录提供 6:1 与 8:1 实际案例</div>`; return; }
      body.innerHTML = samples.map((s) => {
        const url = `/api/samples/${s.dir}/final.png`;
        return `<div class="h-card" onclick="window.open('${url}','_blank')">
          <div class="h-img" style="background-image:url('${url}')"></div>
          <div class="h-meta"><span class="h-ratio">${s.ratio_label}</span><b>${(s.prompt || "").slice(0, 18)}…</b><br>${s.width}×${s.height_out} · ${s.elapsed}s</div>
        </div>`;
      }).join("");
    }
  } catch {
    body.innerHTML = `<div class="shelf-empty">加载失败,请刷新重试</div>`;
  }
}

function renderCard(j) {
  const time = new Date(j.created_at * 1000).toTimeString().slice(0, 5);
  const statusMap = { done: "完成", error: "失败", pending: "排队中", planning: "规划中", generating: "生成中" };
  const img = j.status === "done" ? `style="background-image:url('${j.result_url}')"` : "";
  return `<div class="h-card${j.status === "generating" || j.status === "planning" ? " running" : ""}" data-id="${j.id}">
    <div class="h-img" ${img}>${j.status === "done" ? "" : statusMap[j.status] || j.status}</div>
    <div class="h-meta"><span class="h-ratio">${j.ratio_label}</span><b>${(j.prompt || "").slice(0, 18)}${(j.prompt || "").length > 18 ? "…" : ""}</b><br>
    ${j.status === "done" ? `${j.width}×${j.height_out}` : statusMap[j.status] || j.status} · ${time}</div>
  </div>`;
}

init();
