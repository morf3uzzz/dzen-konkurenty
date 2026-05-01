const $ = (id) => document.getElementById(id);
const logEl = $("log");
const statusEl = $("status");
const startBtn = $("start");
const stopBtn = $("stop");
const apiKeyEl = $("api_key");
const modelEl = $("model");
const loadModelsBtn = $("load_models");
const elapsedEl = $("elapsed");
const aiCostPill = $("ai-cost-pill");
const aiCostText = $("ai-cost-text");

let logOffset = 0;
let pollTimer = null;
let wasRunning = false;
let firstEvent = false;
let totalAiCost = 0;
let stages = {};   // {stageNum: 'active'|'done'}

function setStatus(text, cls) {
  const dot = statusEl.querySelector(".status-dot");
  const txt = statusEl.querySelector(".status-text");
  txt.textContent = text;
  statusEl.className = "status-pill" + (cls ? " " + cls : "");
}

function appendLog(text, kind) {
  if (!text) return;
  const near = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
  const span = document.createElement("span");
  span.textContent = text + "\n";
  if (kind) span.className = "log-" + kind;
  logEl.appendChild(span);
  if (near) logEl.scrollTop = logEl.scrollHeight;
}

function resetLog() {
  logEl.innerHTML = "";
  logOffset = 0;
  firstEvent = false;
  totalAiCost = 0;
  aiCostPill.classList.add("hidden");
  // reset stages
  document.querySelectorAll(".progress-stage").forEach(el => {
    el.classList.remove("active", "done");
  });
  stages = {};
}

function updateStage(num) {
  const idx = parseInt(num);
  // mark previous as done, current as active
  document.querySelectorAll(".progress-stage").forEach(el => {
    const n = parseInt(el.dataset.stage);
    if (n < idx) {
      el.classList.remove("active");
      el.classList.add("done");
    } else if (n === idx) {
      el.classList.add("active");
      el.classList.remove("done");
    } else {
      el.classList.remove("active", "done");
    }
  });
}

function finalizeStages() {
  // mark all as done
  document.querySelectorAll(".progress-stage").forEach(el => {
    el.classList.remove("active");
    el.classList.add("done");
  });
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({detail: r.statusText}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

function fmtElapsed(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return m > 0 ? `${m}м ${s}с` : `${s}с`;
}

async function refreshStatus() {
  try {
    const s = await fetchJSON("/status");
    if (s.running) {
      const t = fmtElapsed(s.elapsed || 0);
      setStatus(`${s.niche} · ${t}`, "running");
      elapsedEl.textContent = `Идёт: ${t}`;
      startBtn.disabled = true;
      stopBtn.disabled = false;
      wasRunning = true;
    } else {
      if (wasRunning) {
        setStatus("Завершено", "done");
        finalizeStages();
        await loadReports();
        wasRunning = false;
      } else {
        const txt = statusEl.querySelector(".status-text").textContent;
        if (txt === "Ожидание" || txt.includes("·")) setStatus("Ожидание");
      }
      startBtn.disabled = false;
      stopBtn.disabled = true;
    }
  } catch (e) {}
}

async function pullEvents() {
  try {
    const r = await fetchJSON(`/events?offset=${logOffset}`);
    logOffset = r.offset;
    if (r.lines && r.lines.length) {
      if (!firstEvent) {
        logEl.innerHTML = "";
        firstEvent = true;
      }
      for (const line of r.lines) {
        // detect stage from text (Стадия N/6)
        const m = line.text.match(/Стадия (\d)\/6/);
        if (m) updateStage(m[1]);
        // detect AI cost
        const cost = line.text.match(/\$(\d+\.\d+)/);
        if (cost && line.text.includes("Всего AI")) {
          totalAiCost = parseFloat(cost[1]);
          aiCostText.textContent = `AI · $${totalAiCost.toFixed(4)}`;
          aiCostPill.classList.remove("hidden");
        }
        // Также обновляем pill во время прогона по промежуточным расходам
        if (line.text.match(/\(\+\$(\d+\.\d+)\)/)) {
          const m = line.text.match(/\(\+\$(\d+\.\d+)\)/);
          totalAiCost += parseFloat(m[1]);
          aiCostText.textContent = `AI · $${totalAiCost.toFixed(4)}`;
          aiCostPill.classList.remove("hidden");
        }
        const kind = {
          stage: "stage", info: "info", warn: "warn", done: "done", start: "stage"
        }[line.kind] || "info";
        appendLog(line.text, kind);
      }
    }
  } catch (e) {}
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    await pullEvents();
    await refreshStatus();
  }, 800);
}

// ---------- Buttons ----------

startBtn.addEventListener("click", async () => {
  const niche = $("niche").value.trim();
  const description = $("description").value.trim();
  const api_key = apiKeyEl.value.trim();
  const min_subs = $("min_subs").value;
  const model = modelEl.value;
  if (!niche) { alert("Введи нишу"); return; }
  resetLog();
  try {
    startBtn.disabled = true;
    await fetchJSON("/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({niche, description, api_key, min_subs, model}),
    });
    setStatus("Запуск…", "running");
    startPolling();
  } catch (e) {
    alert(e.message);
    startBtn.disabled = false;
  }
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  try {
    await fetchJSON("/stop", {method: "POST"});
    setStatus("Останавливаю…", "stopped");
  } catch (e) { alert(e.message); }
});

// ---------- Models ----------

apiKeyEl.addEventListener("input", () => {
  // Сбрасываем модели при изменении ключа
  modelEl.disabled = true;
  modelEl.innerHTML = '<option value="">Сначала введи ключ выше</option>';
});

loadModelsBtn.addEventListener("click", async () => {
  const key = apiKeyEl.value.trim();
  if (!key) { alert("Сначала введи ключ"); return; }
  loadModelsBtn.disabled = true;
  loadModelsBtn.textContent = "…";
  try {
    const r = await fetchJSON("/models", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({api_key: key}),
    });
    if (!r.models || !r.models.length) {
      modelEl.innerHTML = '<option value="">Не нашлось Claude-моделей</option>';
      return;
    }
    modelEl.innerHTML = r.models.map(m => {
      const price = m.in_per_m
        ? `$${m.in_per_m}/$${m.out_per_m} per M`
        : "(цена не указана)";
      return `<option value="${m.id}">${m.name} · ${price}</option>`;
    }).join("");
    // По умолчанию выбираем Haiku 4.5
    const haiku = r.models.find(m => m.id.includes("haiku-4.5"));
    if (haiku) modelEl.value = haiku.id;
    modelEl.disabled = false;
    $("model_hint").textContent = `Загружено ${r.models.length} моделей. Цены: ввод/вывод за миллион токенов.`;
  } catch (e) {
    alert("Ошибка: " + e.message);
  } finally {
    loadModelsBtn.disabled = false;
    loadModelsBtn.textContent = "↻";
  }
});

// ---------- Reports ----------

async function loadReports() {
  const target = $("reports");
  try {
    const r = await fetchJSON("/reports");
    if (!r.reports.length) {
      target.innerHTML = `<div class="empty">Ещё нет завершённых прогонов.</div>`;
      return;
    }
    target.innerHTML = r.reports.map(rep => {
      const ts = rep.timestamp.replace(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/, "$3.$2.$1 · $4:$5");
      const links = [];
      if (rep.articles) {
        const kb = Math.round(rep.articles.size / 1024);
        links.push(`<a href="/download/${encodeURIComponent(rep.articles.name)}">📄 статьи · ${kb} КБ</a>`);
      }
      if (rep.channels) {
        const kb = Math.round(rep.channels.size / 1024);
        links.push(`<a href="/download/${encodeURIComponent(rep.channels.name)}">📋 каналы · ${kb} КБ</a>`);
      }
      return `<div class="report-item">
        <div class="report-niche">${escapeHtml(rep.niche)}</div>
        <div class="report-ts">${ts}</div>
        <div class="report-links">${links.join("")}</div>
      </div>`;
    }).join("");
  } catch (e) {
    target.textContent = "Ошибка загрузки";
  }
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[c]));
}

// ---------- Init ----------
(async () => {
  try {
    const s = await fetchJSON("/status");
    if (!s.running) {
      const tail = await fetchJSON("/events?offset=0");
      logOffset = tail.offset || 0;
      firstEvent = true;
    }
  } catch (e) {}
  refreshStatus();
  loadReports();
  startPolling();
})();
