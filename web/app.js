/* 智析 · 前端交互层
   负责：粒子背景 / SSE 流式渲染 / Agent 流水线动画 / SQL 高亮 / ECharts / 报告打字机 / 数字滚动 */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const api = {
    meta: () => fetch("/api/meta").then((r) => r.json()),
    examples: () => fetch("/api/examples").then((r) => r.json()),
    memory: (u) => fetch(`/api/memory/${u}`).then((r) => r.json()),
    audit: () => fetch("/api/audit").then((r) => r.json()),
    setPref: (u, key, value) =>
      fetch(`/api/memory/${u}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, value }) }).then((r) => r.json()),
    activate: (id) => fetch(`/api/datasets/${id}/activate`, { method: "POST" }).then((r) => r.json()),
    drop: (id) => fetch(`/api/datasets/${id}`, { method: "DELETE" }).then((r) => r.json()),
  };

  const USER_ID = "demo";
  const NODES = [
    { key: "supervisor", label: "主管 Agent", sub: "拆解·路由", icon: "◈" },
    { key: "probe", label: "数据摸底", sub: "schema·样本", icon: "⌕" },
    { key: "tools", label: "工具路由", sub: "26 tools", icon: "⌗" },
    { key: "sql", label: "SQL 工程师", sub: "NL→SQL", icon: "⌘" },
    { key: "analyst", label: "分析推理师", sub: "Pandas", icon: "◔" },
    { key: "reflector", label: "自反思", sub: "≤3 轮", icon: "◉" },
    { key: "writer", label: "报告撰写官", sub: "溯源", icon: "✎" },
  ];
  const NODE_CN = Object.fromEntries(NODES.map((n) => [n.key, n.label]));
  const LOG_TYPE = { supervisor: "think", probe: "obs", tools: "act", sql: "act", analyst: "think", reflector: "think", writer: "obs" };
  const CAT_COLOR = { data: "#6ee7ff", calc: "#a78bfa", retrieval: "#f472b6", meta: "#34d399" };

  let chart = null;
  let running = false;
  let lastConclusion = null;
  let lastResult = null;

  /* ================= 背景粒子 ================= */
  (function particles() {
    const c = $("#bg");
    const ctx = c.getContext("2d");
    let w, h, dots = [];
    const mouse = { x: -999, y: -999 };
    function resize() {
      w = c.width = innerWidth;
      h = c.height = innerHeight;
      const n = Math.min(90, Math.floor((w * h) / 22000));
      dots = Array.from({ length: n }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.28,
        vy: (Math.random() - 0.5) * 0.28,
        r: Math.random() * 1.5 + 0.6,
      }));
    }
    function frame() {
      ctx.clearRect(0, 0, w, h);
      for (const d of dots) {
        d.x += d.vx;
        d.y += d.vy;
        if (d.x < 0 || d.x > w) d.vx *= -1;
        if (d.y < 0 || d.y > h) d.vy *= -1;
        ctx.beginPath();
        ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(140,180,255,.55)";
        ctx.fill();
      }
      for (let i = 0; i < dots.length; i++) {
        for (let j = i + 1; j < dots.length; j++) {
          const dx = dots[i].x - dots[j].x;
          const dy = dots[i].y - dots[j].y;
          const dist = Math.hypot(dx, dy);
          if (dist < 130) {
            ctx.beginPath();
            ctx.moveTo(dots[i].x, dots[i].y);
            ctx.lineTo(dots[j].x, dots[j].y);
            ctx.strokeStyle = `rgba(110,231,255,${0.1 * (1 - dist / 130)})`;
            ctx.lineWidth = 0.6;
            ctx.stroke();
          }
        }
        const md = Math.hypot(dots[i].x - mouse.x, dots[i].y - mouse.y);
        if (md < 160) {
          ctx.beginPath();
          ctx.moveTo(dots[i].x, dots[i].y);
          ctx.lineTo(mouse.x, mouse.y);
          ctx.strokeStyle = `rgba(167,139,250,${0.16 * (1 - md / 160)})`;
          ctx.stroke();
        }
      }
      requestAnimationFrame(frame);
    }
    addEventListener("resize", resize);
    addEventListener("mousemove", (e) => {
      mouse.x = e.clientX;
      mouse.y = e.clientY;
    });
    resize();
    frame();
  })();

  /* ================= 工具函数 ================= */
  const fmtMoney = (v) => {
    const a = Math.abs(v);
    if (a >= 1e8) return { num: (v / 1e8).toFixed(2), unit: "亿" };
    if (a >= 1e4) return { num: (v / 1e4).toFixed(1), unit: "万" };
    return { num: Math.round(v).toLocaleString("zh-CN"), unit: "" };
  };
  const esc = (s) => String(s ?? "").replace(/[&<>]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m]));

  function countUp(el, target, unit, duration = 800) {
    const start = performance.now();
    const from = 0;
    function tick(now) {
      // rAF 的时间戳在极端情况下可能早于起点（切后台、虚拟时间），
      // 不夹到 [0,1] 会算出负数，KPI 上闪一下负值很难看
      const p = Math.min(1, Math.max(0, (now - start) / duration));
      const eased = 1 - Math.pow(1 - p, 3);
      const cur = from + (target - from) * eased;
      const f = fmtMoney(cur);
      el.textContent = f.num;
      const u = el.parentElement.querySelector(".kpi-unit");
      if (u) u.textContent = (f.unit || "") + (unit || "");
      if (p < 1) requestAnimationFrame(tick);
      else {
        el.textContent = fmtMoney(target).num;
        if (u) u.textContent = (fmtMoney(target).unit || "") + (unit || "");
      }
    }
    requestAnimationFrame(tick);
  }

  /* ================= 流水线 ================= */
  function buildFlow() {
    const flow = $("#flow");
    flow.innerHTML = "";
    NODES.forEach((n, i) => {
      const el = document.createElement("div");
      el.className = "node";
      el.dataset.key = n.key;
      el.innerHTML = `<div class="orb">${n.icon}</div><div class="nm">${n.label}</div><div class="sub">${n.sub}</div><div class="timer">0.0s</div>`;
      flow.appendChild(el);
      if (i < NODES.length - 1) {
        const l = document.createElement("div");
        l.className = "link";
        l.dataset.i = i;
        flow.appendChild(l);
      }
    });
  }
  /* 节点计时：走的是真实墙钟，不是装饰 */
  let nodeClock = null;
  function startClock(key) {
    stopClock();
    const el = document.querySelector(`.node[data-key="${key}"] .timer`);
    if (!el) return;
    const t0 = performance.now();
    nodeClock = setInterval(() => {
      el.textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s";
    }, 100);
  }
  function stopClock() {
    if (nodeClock) clearInterval(nodeClock);
    nodeClock = null;
  }
  function setNode(key, state) {
    const el = document.querySelector(`.node[data-key="${key}"]`);
    if (!el) return;
    el.classList.remove("active", "done", "warn");
    if (state) el.classList.add(state);
    const idx = NODES.findIndex((n) => n.key === key);
    document.querySelectorAll(".link").forEach((l) => {
      const i = Number(l.dataset.i);
      if (i < idx) l.classList.add("on");
      if (state === "active" && i === idx - 1) l.classList.add("on");
    });
  }
  function resetFlow() {
    document.querySelectorAll(".node").forEach((n) => n.classList.remove("active", "done", "warn"));
    document.querySelectorAll(".link").forEach((l) => l.classList.remove("on"));
    logQueue.length = 0;
    $("#log").innerHTML = "";
    $("#logCount").textContent = "0";
    $("#sql").innerHTML = '<span class="code-empty">-- 生成后可查看参数化 SQL</span>';
    $("#sqlMeta").innerHTML = "";
    $("#checks").innerHTML = "";
    $("#slots").innerHTML = '<span class="empty">槽位识别：等待提问</span>';
    $("#report").innerHTML = "";
    $("#evList").innerHTML = '<div class="empty">正在取数，证据会逐条登记…</div>';
    $("#evFlow").innerHTML = "";
    $("#evCount").textContent = "0 条";
    evData = [];
    setScore(null, "执行中", "置信度由 6 项硬检查加权得出");
    $("#kpiValue").textContent = "—";
    $("#kpiUnit").textContent = "";
    $("#kpiDelta").textContent = "计算中…";
    $("#kpiList").innerHTML = "";
    $("#kpiLabel").textContent = "销售额";
    $("#kpiWindow").textContent = "—";
    $("#groundTag").className = "tag";
    $("#groundTag").textContent = "数字溯源";
  }

  function appendEvidence(list) {
    if (!list || !list.length) return;
    const host = $("#evList");
    if (!evData.length) host.innerHTML = "";
    const base = evData.length;
    evData.push(...list);
    $("#evCount").textContent = `${evData.length} 条`;
    host.insertAdjacentHTML("beforeend", list.map((e, i) => evCard(e, base + i, false)).join(""));
    host.scrollTop = host.scrollHeight;
  }

  /* ================= 思考流（队列播放，保证每条都看得清）================= */
  let logCount = 0;
  const logQueue = [];
  let pumping = false;

  function pushLog(nodeKey, text, kind = "info") {
    logQueue.push({ nodeKey, text, kind });
    if (!pumping) pumpLog();
  }
  function pumpLog() {
    pumping = true;
    const step = () => {
      if (!logQueue.length) { pumping = false; return; }
      const item = logQueue.shift();
      renderLogLine(item.nodeKey, item.text, item.kind);
      setTimeout(step, 165);
    };
    step();
  }
  function waitLogs() {
    return new Promise((res) => {
      const t = setInterval(() => {
        if (!logQueue.length && !pumping) { clearInterval(t); res(); }
      }, 70);
    });
  }
  function renderLogLine(nodeKey, text, kind) {
    const body = $("#log");
    if (logCount === 0) body.innerHTML = "";
    const row = document.createElement("div");
    row.className = `log-line ${kind} ${LOG_TYPE[nodeKey] || "think"}`;
    row.innerHTML = `<span class="node-name">${NODE_CN[nodeKey] || nodeKey}</span><span class="txt"></span>`;
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
    logCount++;
    $("#logCount").textContent = String(logCount);
    // 打字机：一句一句显出来，比一次性刷完更像"在思考"
    const span = row.querySelector(".txt");
    let i = 0;
    const txt = String(text || "");
    const type = setInterval(() => {
      if (i >= txt.length) { clearInterval(type); return; }
      span.textContent = txt.slice(0, ++i);
      body.scrollTop = body.scrollHeight;
    }, 12);
  }

  /* ================= SQL 高亮 ================= */
  function highlightSql(sql) {
    const kw = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|LIMIT|JOIN|LEFT|ON|AS|AND|OR|BETWEEN|WITH|SUM|COUNT|ROUND|AVG|DISTINCT|NULLIF|SUBSTR|ASC|DESC)\b/g;
    return esc(sql)
      .replace(/(--[^\n]*)/g, '<span class="cmt">$1</span>')
      .replace(/'([^']*)'/g, '<span class="str">\'$1\'</span>')
      .replace(kw, '<span class="kw">$1</span>');
  }

  function renderSql(sql, meta = {}) {
    if (!sql) return;
    $("#sql").innerHTML = highlightSql(sql);
    const bits = [];
    if (meta.status === "ok") bits.push(`<span class="good">执行成功</span>`);
    if (meta.row_count != null) bits.push(`<span>${meta.row_count} 行</span>`);
    if (meta.elapsed_ms != null) bits.push(`<span>${meta.elapsed_ms} ms</span>`);
    if (meta.params) bits.push(`<span>${meta.params.length} 个绑定参数</span>`);
    bits.push(`<span>只读连接 · query_only</span>`);
    $("#sqlMeta").innerHTML = bits.join("");
    $("#sqlTag").textContent = meta.status === "ok" ? "已执行" : meta.status === "error" ? "失败" : "只读";
    $("#sqlTag").className = "tag " + (meta.status === "ok" ? "ok" : meta.status === "error" ? "err" : "");
  }

  /* ================= 数据来源（证据卡）================= */
  let evData = [];
  let evExpanded = false;

  function renderEvidence(list) {
    evData = Array.isArray(list) ? list : [];
    const host = $("#evList");
    $("#evCount").textContent = `${evData.length} 条`;
    if (!evData.length) {
      host.innerHTML = '<div class="empty">还没有取数记录。</div>';
      $("#evFlow").innerHTML = "";
      return;
    }

    // 顶部链路：来源 → 表 → 行数，让人一眼看出这次到底动了哪些数据
    const seen = new Map();
    evData.forEach((e) => (e.tables_meta || []).forEach((t) => seen.set(t.table, t)));
    const first = evData[0] || {};
    const srcChip = first.source_label
      ? `<span class="ev-chip" style="border-color:rgba(52,211,153,.3)"><i class="dot" style="background:#34d399"></i>${
          first.source_url && /^https?:\/\//.test(first.source_url)
            ? `<a class="src-link" href="${esc(first.source_url)}" target="_blank" rel="noopener noreferrer">${esc(first.source_label)} ↗</a>`
            : esc(first.source_label)
        }</span>`
      : `<span class="ev-chip" style="border-color:rgba(52,211,153,.3)"><i class="dot" style="background:#34d399"></i>${esc(shortPath(first.source_file))}</span>`;
    $("#evFlow").innerHTML =
      srcChip +
      [...seen.values()]
        .map(
          (t) =>
            `<span class="ev-chip">表 <b>${esc(t.table)}</b>${t.rows != null ? ` · ${Number(t.rows).toLocaleString("zh-CN")} 行` : ""}${
              t.time_col ? ` · ${esc(t.time_col)}` : ""
            }</span>`
        )
        .join("") +
      `<span class="ev-chip" style="border-color:rgba(167,139,250,.3)">共 <b>${evData.length}</b> 次取数</span>`;

    const SHOW = 3;
    host.innerHTML =
      evData
        .map((e, i) => evCard(e, i, !evExpanded && i >= SHOW))
        .join("") +
      (!evExpanded && evData.length > SHOW
        ? `<div class="ev-more">还有 ${evData.length - SHOW} 条取数记录<button class="ev-toggle" id="evMoreBtn">展开全部</button></div>`
        : "");
    $("#evToggle").textContent = evExpanded ? "收起" : "展开全部";
    const more = document.getElementById("evMoreBtn");
    if (more) more.addEventListener("click", () => { evExpanded = true; renderEvidence(evData); });
  }

  function shortPath(p) {
    if (!p) return "SQLite";
    const parts = String(p).split("/");
    return parts.slice(-2).join("/");
  }

  function evCard(e, i, hidden) {
    if (hidden) return "";
    const badges = [];
    if (e.tables && e.tables.length) badges.push(`<span class="ev-badge">表 <b>${esc(e.tables.join(" + "))}</b></span>`);
    badges.push(`<span class="ev-badge">返回 <b>${e.row_count ?? 0}</b> 行</span>`);
    badges.push(`<span class="ev-badge">耗时 <b>${e.elapsed_ms ?? 0}</b> ms</span>`);
    if (e.params && e.params.length) badges.push(`<span class="ev-badge">绑定 <b>${e.params.length}</b> 个参数</span>`);
    if (e.status !== "ok") badges.push(`<span class="ev-badge warn">${esc(e.error || "失败")}</span>`);

    let sampleHtml = "";
    if (e.sample && e.sample.length) {
      const cols = Object.keys(e.sample[0]).slice(0, 8);
      const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
      const body = e.sample
        .slice(0, 5)
        .map((r) => `<tr>${cols.map((c) => `<td>${esc(fmtCell(r[c]))}</td>`).join("")}</tr>`)
        .join("");
      sampleHtml = `<div class="ev-sample"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
        <div class="more">仅展示前 ${Math.min(5, e.sample.length)} 行 · 共 ${e.row_count} 行</div></div>`;
    }

    const paramLine =
      e.params && e.params.length
        ? `<div class="ev-plan">参数绑定：<b>${e.params.map((p) => esc(JSON.stringify(p))).join(", ")}</b></div>`
        : "";
    const planLine = e.query_plan && e.query_plan.length ? `<div class="ev-plan">扫描计划：<b>${esc(e.query_plan[0])}</b></div>` : "";

    const srcLine = e.source_label
      ? `<div class="ev-src">来源：${
          e.source_url && /^https?:\/\//.test(e.source_url)
            ? `<a class="src-link" href="${esc(e.source_url)}" target="_blank" rel="noopener noreferrer">${esc(e.source_label)} ↗</a>`
            : esc(e.source_label)
        }</div>`
      : "";

    return `<div class="ev-item ${e.role || "query"}" id="ev-${esc(e.evidence_id)}" style="animation-delay:${i * 45}ms">
      <div class="ev-top">
        <span class="ev-id">${esc(e.evidence_id || "S?")}</span>
        <span class="ev-purpose">${esc(e.purpose || "取数")}</span>
        <span class="tag ${e.status === "ok" ? "ok" : "err"}">${e.status === "ok" ? "成功" : "失败"}</span>
      </div>
      ${srcLine}
      <div class="ev-badges">${badges.join("")}</div>
      <div class="ev-sql">${highlightSql(e.sql || "")}</div>
      ${paramLine}${planLine}${sampleHtml}
    </div>`;
  }

  function fmtCell(v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("zh-CN") : v.toFixed(2);
    return String(v);
  }

  function focusEvidence(id) {
    let el = document.getElementById("ev-" + id);
    // 证据卡默认只显示前 3 条，点第 4 条以后的角标要先展开，否则会静默失败
    if (!el && !evExpanded && evData.length) {
      evExpanded = true;
      renderEvidence(evData);
      el = document.getElementById("ev-" + id);
    }
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 2200);
  }

  /* ================= 数据源 / 上传 ================= */
  let dsState = { items: [], active: null };

  function renderDatasets() {
    const host = $("#dsList");
    const activeId = (dsState.active || {}).id || null;
    $("#dsTag").textContent = activeId ? "上传数据" : "内置示例";
    $("#dsTag").className = "tag " + (activeId ? "ok" : "");
    host.innerHTML = dsState.items
      .map((d) => {
        const on = d.id === activeId;
        return `<div class="ds-item ${on ? "on" : ""}" data-id="${d.id}">
          <span class="nm" title="${esc(d.name)}">${on ? "● " : ""}${esc(d.name)}</span>
          <span class="meta">${d.rows}行/${d.columns}列</span>
          <span class="del" data-del="${d.id}" title="删除">✕</span>
        </div>`;
      })
      .join("");
    // 画像只在有上传数据时展示（当前激活那个）
    const act = dsState.items.find((d) => d.id === activeId);
    const prof = $("#dsProf");
    if (act && prof) {
      prof.classList.add("show");
      prof.innerHTML =
        `<div class="row" style="color:var(--dim)">字段画像 · 时间 ${esc(act.time_col || "无")}</div>` +
        (act.profile || [])
          .map(
            (p) =>
              `<div class="row"><span class="cn" title="${esc(p.name)}">${esc(p.name)}</span>
               <span class="role ${p.role}">${roleCn(p.role)}</span>
               <span class="meta" style="color:var(--dim-2);font-size:9.5px">${p.unique}值</span></div>`
          )
          .join("");
    } else if (prof) {
      prof.classList.remove("show");
      prof.innerHTML = "";
    }
  }

  function roleCn(r) {
    return { time: "时间", metric: "指标", dim: "维度", id: "标识" }[r] || r;
  }

  async function uploadFile(file) {
    if (!file) return;
    const drop = $("#drop");
    drop.classList.add("busy");
    drop.querySelector(".drop-t").textContent = "解析中…";
    try {
      const fd = new FormData();
      fd.append("file", file);
      const src = ($("#dsSource") && $("#dsSource").value.trim()) || "";
      if (src) { fd.append("source_url", src); fd.append("source_name", file.name); }
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "上传失败");
      await loadMeta();
      await loadExamples();
      drop.querySelector(".drop-t").textContent = `已加载 ${j.rows} 行`;
      if ($("#dsSource")) $("#dsSource").value = "";
      setTimeout(() => (drop.querySelector(".drop-t").textContent = "拖入 CSV / Excel"), 2200);
    } catch (err) {
      drop.querySelector(".drop-t").textContent = "拖入 CSV / Excel";
      alert("解析失败：" + err.message);
    } finally {
      drop.classList.remove("busy");
    }
  }


  /* ================= 槽位 ================= */
  function renderSlots(plan) {
    if (!plan) return;
    const items = [
      ["时间", `${plan.time_range.label}`],
      ["指标", plan.metric],
      ["维度", (plan.dimensions || []).join(" · ") || "整体"],
      ["过滤", (plan.filters || []).map((f) => `${f.field}=${f.value}`).join(" ") || "无"],
      ["对比", plan.compare?.label || "无"],
      ["规划器", plan.planner || "rule"],
    ];
    $("#slots").innerHTML = items
      .map(([k, v], i) => `<span class="slot" style="animation-delay:${i * 40}ms">${k}<b>${esc(v)}</b></span>`)
      .join("");
  }

  /* ================= KPI ================= */
  // 事实对象里的 source 形如 "S2" 或 "本期 - 上期（派生自 S2）"，取出可点击的证据编号
  const evOf = (s) => (String(s || "").match(/S\d+/) || [null])[0];

  function renderKpi(c) {
    if (!c) return;
    lastConclusion = c;
    const unit = c.metric === "margin" ? "%" : c.metric === "roi" ? "倍" : c.metric_label === "客单价" ? "元" : "";
    $("#kpiLabel").textContent = c.metric_label;
    $("#kpiWindow").textContent = (c.chart && c.chart.x) || "整体";
    if (c.metric === "margin" || c.metric === "roi" || c.metric === "turnover") {
      $("#kpiValue").textContent = Number(c.current).toFixed(2);
      $("#kpiUnit").textContent = c.unit;
    } else {
      countUp($("#kpiValue"), c.current, "", 900);
    }
    if (c.delta_pct != null) {
      const up = c.delta_pct >= 0;
      $("#kpiDelta").innerHTML = `环比 <span class="${up ? "up" : "down"}">${up ? "▲" : "▼"} <b>${Math.abs(c.delta_pct).toFixed(2)}%</b></span>` +
        (c.previous != null ? ` <span style="color:var(--dim-2)">上期 ${esc(fmtMoney(c.previous).num + fmtMoney(c.previous).unit)}</span>` : "");
    } else {
      $("#kpiDelta").textContent = c.previous == null ? "未指定对比区间" : "";
    }
    // 主数字挂第一条事实的证据（本期值），派生数字挂它依赖的那条
    const mainEv = evOf(((c.facts || [])[0] || {}).source);
    const kpiVal = $("#kpiValue");
    if (mainEv) {
      kpiVal.dataset.ev = mainEv;
      kpiVal.title = `主指标来自 ${mainEv} · 点击查看取数记录`;
      kpiVal.classList.add("ev-link");
    } else {
      delete kpiVal.dataset.ev;
      kpiVal.classList.remove("ev-link");
      kpiVal.title = "";
    }

    $("#kpiList").innerHTML = (c.facts || [])
      .slice(0, 5)
      .map((f, i) => {
        const ev = evOf(f.source);
        const attr = ev ? ` data-ev="${ev}" title="来自 ${ev} · 点击查看取数记录"` : "";
        return `<div class="kpi-row${ev ? " ev-link" : ""}"${attr} style="animation-delay:${i * 50}ms"><span>${esc(f.label)}</span><b>${esc(f.display)}</b></div>`;
      })
      .join("");
  }

  /* ================= 图表 ================= */
  const fmtAxis = (v) => {
    if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(1) + "亿";
    if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(0) + "万";
    return v;
  };
  const isDate = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s);

  function renderChart(c) {
    if (!c || !c.chart) return;
    const el = $("#chart");
    if (!chart) chart = echarts.init(el, null, { renderer: "canvas" });
    const spec = c.chart;
    const baseGrid = { left: 54, right: 26, top: 34, bottom: 30 };
    const axisStyle = {
      axisLine: { lineStyle: { color: "rgba(140,165,220,.2)" } },
      axisLabel: { color: "#8792b2", fontSize: 10.5 },
      splitLine: { lineStyle: { color: "rgba(140,165,220,.07)" } },
      axisTick: { show: false },
    };
    let option;

    if (spec.type === "line") {
      const pts = spec.points || [];
      option = {
        grid: baseGrid,
        tooltip: {
          trigger: "axis",
          backgroundColor: "rgba(12,17,30,.95)",
          borderColor: "rgba(120,150,255,.25)",
          textStyle: { color: "#e9eefc", fontSize: 11.5 },
          valueFormatter: (v) => fmtMoney(v).num + fmtMoney(v).unit + " 元",
        },
        xAxis: { type: "category", data: pts.map((p) => (isDate(p.x) ? p.x.slice(5) : p.x)), boundaryGap: false, ...axisStyle },
        yAxis: { type: "value", ...axisStyle, axisLabel: { ...axisStyle.axisLabel, formatter: fmtAxis } },
        series: [
          {
            type: "line",
            smooth: 0.35,
            symbol: "circle",
            symbolSize: 4,
            showSymbol: false,
            data: pts.map((p) => p.y),
            lineStyle: { width: 2.4, color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: "#6ee7ff" }, { offset: 0.6, color: "#a78bfa" }, { offset: 1, color: "#f472b6" }]) },
            itemStyle: { color: "#6ee7ff" },
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "rgba(110,231,255,.34)" }, { offset: 1, color: "rgba(110,231,255,0)" }]),
            },
            markPoint: {
              symbol: "pin",
              symbolSize: 34,
              data: [{ type: "max", name: "峰值" }, { type: "min", name: "谷值" }],
              label: { fontSize: 9, color: "#04070f" },
              itemStyle: { color: "#a78bfa" },
            },
            animationDuration: 1100,
            animationEasing: "cubicOut",
          },
        ],
      };
    } else if (spec.type === "bar") {
      const pts = (spec.points || []).slice(0, 12).reverse();
      option = {
        grid: { ...baseGrid, left: 70, top: 18, bottom: 26 },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          backgroundColor: "rgba(12,17,30,.95)",
          borderColor: "rgba(120,150,255,.25)",
          textStyle: { color: "#e9eefc", fontSize: 11.5 },
          valueFormatter: (v) => fmtMoney(v).num + fmtMoney(v).unit,
        },
        xAxis: { type: "value", ...axisStyle, axisLabel: { ...axisStyle.axisLabel, formatter: fmtAxis } },
        yAxis: { type: "category", data: pts.map((p) => p.x), ...axisStyle, splitLine: { show: false } },
        series: [
          {
            type: "bar",
            barWidth: 13,
            data: pts.map((p) => p.y),
            itemStyle: {
              borderRadius: [0, 7, 7, 0],
              color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: "rgba(110,231,255,.35)" }, { offset: 1, color: "#a78bfa" }]),
            },
            label: { show: true, position: "right", color: "#9fb0d6", fontSize: 10, formatter: (o) => fmtMoney(o.value).num + fmtMoney(o.value).unit },
            animationDuration: 900,
            animationDelay: (i) => i * 45,
            emphasis: { itemStyle: { color: "#6ee7ff" } },
          },
        ],
      };
    } else if (spec.type === "compare") {
      // 只有环比、没有维度可拆时，给本期/上期双柱：有对比才看得出变化
      const pts = spec.points || [];
      const cur = pts[0] ? pts[0].y : 0;
      const prev = pts[1] ? pts[1].y : 0;
      const pct = prev ? ((cur - prev) / prev) * 100 : null;
      option = {
        grid: { ...baseGrid, left: 58, right: 24, top: 40, bottom: 28 },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          backgroundColor: "rgba(12,17,30,.95)",
          borderColor: "rgba(120,150,255,.25)",
          textStyle: { color: "#e9eefc", fontSize: 11.5 },
          valueFormatter: (v) => fmtMoney(v).num + fmtMoney(v).unit,
        },
        xAxis: { type: "category", data: pts.map((p) => p.x), ...axisStyle, splitLine: { show: false } },
        yAxis: { type: "value", ...axisStyle, axisLabel: { ...axisStyle.axisLabel, formatter: fmtAxis } },
        series: [
          {
            type: "bar",
            barWidth: 44,
            data: pts.map((p, i) => ({
              value: p.y,
              itemStyle: {
                borderRadius: [5, 5, 0, 0],
                color:
                  i === 0
                    ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#6ee7ff" }, { offset: 1, color: "rgba(110,231,255,.22)" }])
                    : "rgba(140,165,220,.32)",
              },
            })),
            label: { show: true, position: "top", color: "#cfe3ff", fontSize: 10.5, formatter: (o) => fmtMoney(o.value).num + fmtMoney(o.value).unit },
            animationDuration: 900,
          },
        ],
        graphic: pct == null ? [] : [
          {
            type: "text",
            right: 16,
            top: 8,
            style: {
              text: `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(2)}%`,
              fill: pct >= 0 ? "#ff6b6b" : "#34d399",
              fontSize: 12,
              fontWeight: 600,
            },
          },
        ],
      };
    } else {
      option = {
        graphic: [
          { type: "text", left: "center", top: "42%", style: { text: fmtMoney(spec.value).num + fmtMoney(spec.value).unit, fill: "#fff", fontSize: 34, fontWeight: 700 } },
          { type: "text", left: "center", top: "56%", style: { text: spec.label || "", fill: "#8792b2", fontSize: 12 } },
        ],
      };
    }
    // 点柱子/折线 → 跳到这张图背后的拆分 SQL
    chart.off("click");
    chart.on("click", () => {
      if (spec.type === "compare") {
        const agg = evData.find((e) => /全表聚合/.test(e.purpose || "") && !/环比/.test(e.purpose || ""));
        const id = (agg || evData.find((e) => e.role === "query") || {}).evidence_id;
        if (id) focusEvidence(id);
        return;
      }
      const dim = spec.x || "";
      const byDim = evData.find((e) => dim && (e.purpose || "").includes(`按${dim}拆分`));
      const anySplit = evData.find((e) => (e.purpose || "").includes("拆分"));
      const id = (byDim || anySplit || {}).evidence_id;
      if (id) focusEvidence(id);
    });

    chart.setOption(option, true);
    setTimeout(() => chart && chart.resize(), 60);
  }

  /* ================= 报告 markdown ================= */
  function mdToBlocks(md) {
    const lines = md.split("\n");
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (line.startsWith("### ")) {
        blocks.push(`<h3>${inline(line.slice(4))}</h3>`);
        i++; continue;
      }
      if (line.startsWith("|")) {
        const rows = [];
        while (i < lines.length && lines[i].startsWith("|")) { rows.push(lines[i]); i++; }
        const cells = rows.map((r) => r.split("|").slice(1, -1).map((c) => c.trim()));
        const head = cells[0];
        const body = cells.slice(2);
        blocks.push(
          `<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body
            .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
            .join("")}</tbody></table>`
        );
        continue;
      }
      if (line.startsWith("- ")) {
        const items = [];
        while (i < lines.length && lines[i].startsWith("- ")) { items.push(lines[i].slice(2)); i++; }
        blocks.push(`<ul>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>`);
        continue;
      }
      blocks.push(`<p class="p">${inline(line)}</p>`);
      i++;
    }
    return blocks;
    function inline(t) {
      return esc(t)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        // [名称](https://...) → 可点击的真实来源链接
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a class="src-link" href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>')
        // ⟦S3⟧ → 可点击的溯源角标
        .replace(/⟦(S\d+)⟧/g, '<sup class="cite" data-ev="$1" title="查看这条数字的取数记录">$1</sup>');
    }
  }

  let typeTimer = null;
  function renderReport(md, animate = true) {
    const host = $("#report");
    if (typeTimer) { clearInterval(typeTimer); typeTimer = null; }
    const blocks = mdToBlocks(md || "");
    host.innerHTML = "";
    if (!animate) { host.innerHTML = blocks.join(""); return; }
    let idx = 0;
    typeTimer = setInterval(() => {
      if (idx >= blocks.length) { clearInterval(typeTimer); typeTimer = null; return; }
      host.insertAdjacentHTML("beforeend", blocks[idx]);
      idx++;
      host.scrollTop = host.scrollHeight;
    }, 75);
  }

  /* ================= 自反思 ================= */
  function setScore(score, title, sub) {
    const ring = $("#scoreRing");
    const deg = score == null ? 0 : Math.round((score / 100) * 360);
    const color = score == null ? "rgba(110,150,255,.1)" : score >= 90 ? "#34d399" : score >= 75 ? "#6ee7ff" : "#fbbf24";
    ring.style.background = `conic-gradient(${color} ${deg}deg, rgba(110,150,255,.08) ${deg}deg)`;
    $("#scoreVal").textContent = score == null ? "—" : score.toFixed(0);
    $("#scoreTitle").textContent = title;
    $("#scoreSub").textContent = sub;
  }
  function renderChecks(rf) {
    if (!rf) return;
    const failed = rf.checks.filter((c) => !c.passed).length;
    setScore(rf.score, rf.passed ? "自检通过" : `未通过（${failed} 项异常）`, `第 ${rf.rounds} 轮 · 6 项硬检查加权，模型自评只作辅助`);
    $("#roundTag").textContent = `${rf.rounds} 轮`;
    $("#checks").innerHTML = rf.checks
      .map(
        (c, i) =>
          `<div class="check ${c.passed ? "pass" : "fail"}" style="animation-delay:${i * 45}ms"><i>${c.passed ? "✓" : "✗"}</i>
           <div>${esc(c.name)}<span class="cd">${esc(c.detail)}</span></div></div>`
      )
      .join("");
  }

  /* ================= 侧栏 ================= */
  async function loadMeta() {
    const m = await api.meta();
    $("#engineChip").textContent = m.engine === "langgraph" ? "LangGraph" : "Native";
    $("#modelChip").textContent = m.model;
    $("#memBackend").textContent = m.memory;
    $("#toolCount").textContent = m.tools.length;
    $("#toolGrid").innerHTML = m.tools
      .map((t) => `<div class="tool" data-tool="${t.name}" title="${esc(t.description)}"><i style="background:${CAT_COLOR[t.category] || "#6ee7ff"}"></i>${esc(t.label)}</div>`)
      .join("");

    dsState = { items: m.datasets || [], active: m.active_dataset || null };
    if (dsState.active) {
      // 列表里要展示字段画像，取完整版
      try {
        const full = await fetch("/api/datasets").then((r) => r.json());
        dsState.items = full.items || dsState.items;
      } catch (e) {}
    }
    renderDatasets();

    if (m.active_dataset) {
      const d = m.active_dataset;
      $("#footStats").textContent = `上传数据 ${d.name} · ${d.rows.toLocaleString("zh-CN")} 行 / ${d.columns} 列 · 时间列 ${d.time_col || "无"}`;
    } else {
      const rows = m.tables.map((t) => `${t.table.split("_").pop()} ${t.rows.toLocaleString("zh-CN")}`).join(" · ");
      $("#footStats").textContent = `数据 ${m.data_bounds.from} ~ ${m.data_bounds.to} · ${rows}`;
    }
  }
  async function loadExamples() {
    const e = await api.examples();
    $("#examples").innerHTML = e.examples
      .slice(0, 4)
      .map((x) => `<span class="example">${esc(x)}</span>`)
      .join("");
    document.querySelectorAll(".example").forEach((el) => {
      el.addEventListener("click", () => { $("#q").value = el.textContent; run(); });
    });
  }
  async function loadMemory() {
    const m = await api.memory(USER_ID);
    $("#memBackend").textContent = m.backend;
    const keys = Object.entries(m.prefs || {});
    $("#prefs").innerHTML = keys.length
      ? keys.map(([k, v]) => `<span class="pref">${esc(k)}: ${esc(v)}</span>`).join("")
      : '<span class="empty">还没有沉淀偏好</span>';
    $("#turns").innerHTML = (m.turns || []).length
      ? m.turns.map((t) => `<div class="turn">${esc(t.q)}</div>`).join("")
      : '<span class="empty">本次会话还没有提问</span>';
  }
  async function loadAudit() {
    const a = await api.audit();
    $("#audit").innerHTML = a.items.length
      ? a.items
          .map(
            (r) =>
              `<div class="audit-row"><span class="t">${esc((r.ts || "").slice(11))}</span>${esc(r.question).slice(0, 22)}
               <span class="s">${esc((r.sql || "").slice(0, 46))}… <span class="ok">${r.rows_returned}行</span></span></div>`
          )
          .join("")
      : '<span class="empty">暂无记录</span>';
  }

  /* ================= 主流程 ================= */
  function run() {
    if (running) return;
    const q = $("#q").value.trim();
    if (!q) { $("#q").focus(); return; }
    running = true;
    $("#send").disabled = true;
    $("#runState").textContent = "执行中";
    $("#runState").className = "tag warn";
    logCount = 0;
    resetFlow();

    const es = new EventSource(`/api/analyze/stream?question=${encodeURIComponent(q)}&user_id=${USER_ID}`);
    es.onmessage = async (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "end") { es.close(); return; }
      if (msg.type === "error") {
        pushLog("supervisor", msg.message, "error");
        finish(false);
        es.close();
        return;
      }
      if (msg.type === "done") {
        lastResult = msg.data.result;
        if (!evData.length && msg.data.result.evidence) appendEvidence(msg.data.result.evidence);
        await waitLogs(); // 让思考流播完，报告和结论再落地
        finalize(msg.data.result, msg.data.elapsed_ms, msg.data.engine);
        finish(true);
        return;
      }
      if (msg.type !== "node") return;
      const node = msg.node;
      const d = msg.data || {};
      document.querySelectorAll(".node").forEach((n) => n.classList.remove("active"));
      const prevIdx = NODES.findIndex((n) => n.key === node);
      NODES.slice(0, Math.max(0, prevIdx)).forEach((n) => setNode(n.key, "done"));
      setNode(node, "active");
      startClock(node);
      if (Array.isArray(d.trace)) d.trace.forEach((t) => pushLog(t.node || node, t.text, t.kind || "info"));
      if (Array.isArray(d.evidence) && d.evidence.length) appendEvidence(d.evidence);
      if (d.plan) renderSlots(d.plan);
      if (d.tool_names) markTools(d.tool_names);
      if (d.sql_result && d.sql_result.sql) renderSql(d.sql_result.sql, d.sql_result);
      if (d.conclusion && !d.out_of_scope) renderKpi(d.conclusion);
      if (d.reflection) renderChecks(d.reflection);
      if (d.report) {
        renderReport(d.report.markdown, true);
        $("#groundTag").className = "tag " + (d.report.grounded ? "ok" : "err");
        $("#groundTag").textContent = d.report.grounded ? "数字全部溯源" : `${d.report.ungrounded_numbers.length} 个数字无出处`;
      }
      if (node === "sql" && d.sql_result && d.sql_result.status !== "ok") setNode(node, "warn");
    };
    es.onerror = () => { pushLog("supervisor", "连接中断，请重开页面再试", "error"); finish(false); es.close(); };
  }

  function markTools(names) {
    document.querySelectorAll(".tool").forEach((el) => el.classList.remove("on"));
    names.forEach((n) => {
      const el = document.querySelector(`.tool[data-tool="${n}"]`);
      if (el) el.classList.add("on");
    });
  }

  function finalize(result, elapsed, engine) {
    if (!result) return;
    stopClock();
    NODES.forEach((n) => setNode(n.key, "done"));
    document.querySelectorAll(".link").forEach((l) => l.classList.add("on"));
    if (result.plan) renderSlots(result.plan);
    if (result.sql_result?.sql) renderSql(result.sql_result.sql, result.sql_result);
    if (result.conclusion && !result.out_of_scope) { renderKpi(result.conclusion); renderChart(result.conclusion); }
    if (result.reflection) renderChecks(result.reflection);
    if (result.evidence) renderEvidence(result.evidence);
    if (result.report) {
      renderReport(result.report.markdown, false);
      $("#groundTag").className = "tag " + (result.out_of_scope ? "warn" : result.report.grounded ? "ok" : "err");
      $("#groundTag").textContent = result.out_of_scope
        ? "未取数 · 未生成数字"
        : result.report.grounded ? "数字全部溯源" : "存在无出处数字";
    }
    $("#runState").textContent = `${engine || "langgraph"} · ${elapsed}ms`;
    $("#runState").className = "tag ok";
    loadAudit();
    loadMemory();
  }

  function finish(ok) {
    running = false;
    $("#send").disabled = false;
    if (!ok) { $("#runState").textContent = "异常中断"; $("#runState").className = "tag err"; }
  }

  /* ================= 绑定 ================= */
  $("#send").addEventListener("click", run);
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); run(); }
  });
  $("#prefSave").addEventListener("click", async () => {
    const key = $("#prefKey").value;
    const val = $("#prefVal").value.trim();
    if (!val) return;
    await api.setPref(USER_ID, key, val);
    $("#prefVal").value = "";
    loadMemory();
  });
  addEventListener("resize", () => chart && chart.resize());

  /* ---- 上传 ---- */
  const drop = $("#drop");
  drop.addEventListener("click", () => $("#file").click());
  $("#file").addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (f) uploadFile(f);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("over");
    })
  );
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });

  /* ---- 数据集切换 / 删除 ---- */
  $("#dsList").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      if (!confirm("删除这个数据集？它的表会一起删掉。")) return;
      await api.drop(del.dataset.del);
      await loadMeta();
      await loadExamples();
      return;
    }
    const item = e.target.closest(".ds-item");
    if (!item) return;
    await api.activate(item.dataset.id);
    await loadMeta();
    await loadExamples();
  });
  $("#dsReset").addEventListener("click", async () => {
    await api.activate("none");
    await loadMeta();
    await loadExamples();
  });

  /* ---- 证据展开 / 角标跳转 ---- */
  $("#evToggle").addEventListener("click", () => {
    evExpanded = !evExpanded;
    renderEvidence(evData);
  });
  document.addEventListener("click", (e) => {
    // 所有挂 data-ev 的元素都统一走这里：报告角标、KPI 主数字、KPI 明细行、图上柱子
    const hit = e.target.closest("[data-ev]");
    if (hit && hit.dataset.ev) focusEvidence(hit.dataset.ev);
  });

  buildFlow();
  loadMeta();
  loadExamples();
  loadMemory();
  loadAudit();
})();
