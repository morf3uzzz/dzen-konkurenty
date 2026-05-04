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
let lastEventAt = 0;  // timestamp последнего события — для детекта «завис»

function setStatus(text, cls) {
  const dot = statusEl.querySelector(".status-dot");
  const txt = statusEl.querySelector(".status-text");
  txt.textContent = text;
  statusEl.className = "status-pill" + (cls ? " " + cls : "");
  // Brand-mark пульсирует ярче во время прогона
  const mark = document.querySelector(".brand-mark");
  if (mark) {
    if (cls === "running") mark.classList.add("running");
    else mark.classList.remove("running");
  }
}

// ---------- Toasts ----------
function toast(text, kind = "info", ttl = 4000) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  const icon = kind === "warn" ? "⚠️" : kind === "error" ? "✕" : "✓";
  el.innerHTML = `<span class="toast-icon">${icon}</span><span class="toast-text"></span>`;
  el.querySelector(".toast-text").textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("fade");
    setTimeout(() => el.remove(), 300);
  }, ttl);
}

// ---------- AI cost color indicator ----------
function updateAiCostColor(cost, budget = 1.0) {
  const pill = aiCostPill;
  const bar = $("ai-cost-bar");
  pill.classList.remove("cost-mid", "cost-high");
  const pct = Math.min(100, (cost / budget) * 100);
  if (bar) bar.style.width = `${pct}%`;
  if (cost >= budget * 0.8) pill.classList.add("cost-high");
  else if (cost >= budget * 0.5) pill.classList.add("cost-mid");
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
      stopBtn.disabled = false;
      // Скрываем финальную панель пока идёт прогон
      $("finish_panel").classList.add("hidden");
      wasRunning = true;
    } else {
      if (wasRunning) {
        setStatus("Завершено", "done");
        finalizeStages();
        await loadReports();
        await showFinishPanel();
        wasRunning = false;
        toast("Прогон завершён, CSV готов", "info", 6000);
        try {
          if (Notification && Notification.permission === "granted") {
            new Notification("Дзен · Конкуренты", {body: "Прогон завершён, CSV готов"});
          }
        } catch (e) {}
      } else {
        const txt = statusEl.querySelector(".status-text").textContent;
        if (txt === "Ожидание" || txt.includes("·")) setStatus("Ожидание");
      }
      stopBtn.disabled = true;
      validateForm();   // обновим состояние кнопки старт
    }
  } catch (e) {}
}

let lastReports = null;

async function showFinishPanel() {
  try {
    const r = await fetchJSON("/last-report");
    if (!r.report) return;
    const latest = r.report;
    lastReports = latest;

    $("finish_niche").textContent = latest.niche;

    const s = latest.stats || {};
    // Готовим целевые числа для анимации
    const targets = {
      channels: s.total_channels || 0,
      articles: 0,  // считаем ниже из CSV-стораджа, или показываем dash
      profile: s.profile_count || 0,
      avg_subs: s.avg_subscribers || 0,
    };
    // articles: из CSV пустой файл = только BOM+заголовки (~700 байт),
    // вычитаем header overhead, чтобы не показать «1 статья» при нуле.
    if (latest.articles) {
      const HEADER_BYTES = 800;
      const PER_ROW = 700;
      const dataBytes = Math.max(0, latest.articles.size - HEADER_BYTES);
      targets.articles = Math.round(dataBytes / PER_ROW);
    }

    document.querySelectorAll("#finish_grid .finish-stat").forEach(el => {
      const key = el.dataset.key;
      const num = el.querySelector(".finish-stat-num");
      const target = targets[key] || 0;
      num.dataset.target = target;
      animateCount(num, target);
    });

    // Топ-3 канала из live-preview
    try {
      const lp = await fetchJSON("/live-preview");
      const top = $("finish_top");
      if (lp.channels && lp.channels.length) {
        top.innerHTML = lp.channels.slice(0, 3).map((ch, i) => {
          const subs = ch.subscribers ? formatNum(ch.subscribers) + " подп." : "";
          const rel = ch.relevance != null ? `· ${ch.relevance}/10` : "";
          return `<div class="finish-top-item">
            <span class="finish-top-rank">${i + 1}</span>
            <span class="finish-top-name">${escapeHtml(ch.title)}</span>
            <span class="finish-top-meta">${subs} ${rel}</span>
          </div>`;
        }).join("");
      } else top.innerHTML = "";
    } catch (e) {}

    $("finish_panel").classList.remove("hidden");
  } catch (e) {}
}

function animateCount(el, target) {
  const dur = 1200;
  const start = performance.now();
  const fmt = (n) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
    return Math.round(n).toLocaleString("ru-RU");
  };
  function step(now) {
    const t = Math.min(1, (now - start) / dur);
    // ease-out cubic
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = fmt(target * eased);
    if (t < 1) requestAnimationFrame(step);
    else el.textContent = fmt(target);
  }
  requestAnimationFrame(step);
}

async function saveLatest(kind) {
  if (!lastReports || !lastReports[kind]) return;
  const name = lastReports[kind].name;
  const btn = $(kind === "articles" ? "save_articles" : "save_channels");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "сохраняю…";
  try {
    const r = await fetchJSON("/save-to-disk", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
    if (r.cancelled) {
      btn.textContent = orig;
    } else if (r.ok) {
      btn.textContent = "✓ сохранено";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
      return;
    }
  } catch (e) { alert(e.message); btn.textContent = orig; }
  btn.disabled = false;
}

$("save_articles").addEventListener("click", () => saveLatest("articles"));
$("save_channels").addEventListener("click", () => saveLatest("channels"));
$("open_folder").addEventListener("click", async () => {
  try { await fetchJSON("/open-data-folder"); } catch (e) { alert(e.message); }
});

// Превью топ-30
$("show_preview").addEventListener("click", () => openPreview());
$("preview_close").addEventListener("click", () => closePreview());
$("preview_backdrop").addEventListener("click", () => closePreview());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePreview();
    closeHelp();
  }
});

// Справка по таблицам — кнопка в правой колонке + в финал-панели
function openHelp() { $("help_modal").classList.remove("hidden"); }
function closeHelp() { $("help_modal").classList.add("hidden"); }
$("show_help")?.addEventListener("click", openHelp);
$("show_help_finish")?.addEventListener("click", openHelp);
$("help_close")?.addEventListener("click", closeHelp);
$("help_backdrop")?.addEventListener("click", closeHelp);

async function openPreview() {
  if (!lastReports || !lastReports.timestamp) return;
  const modal = $("preview_modal");
  modal.classList.remove("hidden");
  $("preview_title").textContent = `Топ-30 каналов · ${lastReports.niche}`;
  const tbody = $("preview_tbody");
  tbody.innerHTML = `<tr><td colspan="9">Загрузка…</td></tr>`;
  try {
    const r = await fetchJSON(`/top-channels?timestamp=${lastReports.timestamp}&limit=30`);
    if (!r.rows || !r.rows.length) {
      tbody.innerHTML = `<tr><td colspan="9">Пусто</td></tr>`;
      return;
    }
    tbody.innerHTML = r.rows.map(row => {
      const relCell = (row.relevance != null)
        ? `<span class="preview-rel-cell ${row.relevance >= 7 ? "high" : row.relevance >= 4 ? "mid" : "low"}">${row.relevance}/10</span>`
        : "";
      const subs = row.subscribers ? formatNum(row.subscribers) : "—";
      const med = row.median_views ? formatNum(row.median_views) : "—";
      const top1 = row.top1
        ? `<a href="${escapeHtml(row.top1_url)}" target="_blank" title="${escapeHtml(row.top1)}">${escapeHtml(row.top1.slice(0, 60))}${row.top1.length > 60 ? "…" : ""}</a>`
        : "—";
      return `<tr>
        <td class="preview-rank">${row.rank ?? ""}</td>
        <td><a href="${escapeHtml(row.url)}" target="_blank">${escapeHtml(row.title)}</a></td>
        <td class="preview-num">${subs}</td>
        <td>${relCell}</td>
        <td class="preview-cat">${escapeHtml(row.category || "—")}</td>
        <td class="preview-num">${row.articles_30d ?? "—"}</td>
        <td class="preview-num">${med}</td>
        <td class="preview-num">${row.read_through || "—"}</td>
        <td>${top1}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9">Ошибка: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function closePreview() {
  $("preview_modal").classList.add("hidden");
}

// Кнопка «вставить пример описания»
$("load_example").addEventListener("click", (e) => {
  e.preventDefault();
  const niche = $("niche").value.trim() || "недвижимость";
  $("description").value =
    `Рынок ${niche === "недвижимость" ? "недвижимости в России" : niche} ` +
    `для профессиональной аудитории — экспертов, предпринимателей, ` +
    `инвесторов. Интересуют каналы с экспертной аналитикой, сделками, ` +
    `обзорами, юридическими разборами, статистикой рынка. ` +
    `НЕ интересуют: лайфстайл, личные истории, ремонт/дизайн интерьеров, ` +
    `общеразвлекательный контент.`;
  validateForm();
});

// Подсказка времени прогона по min_subs
$("min_subs").addEventListener("input", () => {
  const v = parseInt($("min_subs").value || "0");
  let hint;
  if (v >= 5000) hint = "Только крупные каналы. Прогон ~10 мин.";
  else if (v >= 1000) hint = "Средние и крупные. Прогон ~20 мин.";
  else if (v >= 100) hint = "Включая мелкие каналы. Прогон ~30–40 мин.";
  else hint = "Все каналы без отсева. Прогон может занять час и больше.";
  $("min_subs_hint").innerHTML = hint;
});

async function pullEvents() {
  try {
    const r = await fetchJSON(`/events?offset=${logOffset}`);
    logOffset = r.offset;
    if (r.lines && r.lines.length) {
      lastEventAt = Date.now();
      if (!firstEvent) {
        logEl.innerHTML = "";
        firstEvent = true;
      }
      for (const line of r.lines) {
        // detect stage from text (Стадия N/5)
        const m = line.text.match(/Стадия (\d)\/5/);
        if (m) updateStage(m[1]);
        // AI cost: «Всего AI: $X.XXXX» — финальная цифра, перезаписывает счётчик.
        // «(+$0.0212)» — промежуточные накапливаем.
        if (line.text.includes("Всего AI")) {
          const m = line.text.match(/\$(\d+\.\d+)/);
          if (m) {
            totalAiCost = parseFloat(m[1]);
            aiCostText.textContent = `AI · $${totalAiCost.toFixed(4)}`;
            aiCostPill.classList.remove("hidden");
            updateAiCostColor(totalAiCost);
          }
        } else {
          const inc = line.text.match(/\(\+\$(\d+\.\d+)\)/);
          if (inc) {
            totalAiCost += parseFloat(inc[1]);
            aiCostText.textContent = `AI · $${totalAiCost.toFixed(4)}`;
            aiCostPill.classList.remove("hidden");
            updateAiCostColor(totalAiCost);
          }
        }
        // Toast-уведомления для ключевых событий
        if (line.kind === "warn") {
          toast(line.text.slice(0, 120), "warn");
        }
        if (line.text.includes("Стадия 3/5") && line.text.includes("прошли фильтр")) {
          const m = line.text.match(/(\d+) из (\d+) прошли/);
          if (m) toast(`Отсев: ${m[1]} из ${m[2]} прошли в детальный анализ`, "info");
        }
        if (line.text.includes("Стадия 5/5")) {
          toast("Финальная стадия: AI оценивает релевантность", "info");
        }
        const kind = {
          stage: "stage", info: "info", warn: "warn", done: "done", start: "stage"
        }[line.kind] || "info";
        appendLog(line.text, kind);
      }
    }
  } catch (e) {}
}

function checkStallDetector() {
  // Если идёт прогон, и >2 минут не было новых событий — показываем плашку
  if (!wasRunning) return;
  if (lastEventAt === 0) return;
  const since = (Date.now() - lastEventAt) / 1000;
  const stallEl = $("stall_warning");
  if (since > 120 && stallEl) {
    stallEl.classList.remove("hidden");
    stallEl.textContent = `⏱ Прогон ничего не выводит ${Math.floor(since)}с — возможно, Дзен тротлит. Можно подождать или нажать Стоп.`;
  } else if (stallEl) {
    stallEl.classList.add("hidden");
  }
}

function formatNum(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    await pullEvents();
    await refreshStatus();
    checkStallDetector();
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

function validateForm() {
  const niche = $("niche").value.trim();
  const key = apiKeyEl.value.trim();
  startBtn.disabled = !niche;
  if (!niche) {
    startBtn.title = "Введи нишу";
  } else if (!key) {
    startBtn.title = "Без ключа OpenRouter — будет работать с шаблонными запросами";
  } else {
    startBtn.title = "Запустить прогон";
  }

  // Подсветка поля ключа: формат должен быть sk-or-...
  if (key && !key.startsWith("sk-or-")) {
    apiKeyEl.classList.add("invalid");
  } else {
    apiKeyEl.classList.remove("invalid");
  }
}

apiKeyEl.addEventListener("input", () => {
  modelEl.disabled = true;
  modelEl.innerHTML = '<option value="">Сначала введи ключ и нажми ↻</option>';
  validateForm();
});
$("niche").addEventListener("input", validateForm);
// Валидируем при загрузке
validateForm();

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
    // Рекомендации основаны на бенчмарке: Haiku — оптимально, Sonnet — чуть точнее
    const RECO = {
      "anthropic/claude-haiku-4.5": "🥇 рекомендую",
      "anthropic/claude-sonnet-4.6": "🥈 чуть точнее",
    };
    modelEl.innerHTML = r.models.map(m => {
      const price = m.in_per_m
        ? `$${m.in_per_m}/$${m.out_per_m} за 1М (вход/выход)`
        : "(цена не указана)";
      const tag = RECO[m.id] ? ` ${RECO[m.id]}` : "";
      return `<option value="${m.id}">${m.name} — ${price}${tag}</option>`;
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

// Делегируем клики на кнопки скачивания и удаления
$("reports").addEventListener("click", async (e) => {
  const previewBtn = e.target.closest(".preview-btn");
  if (previewBtn) {
    const ts = previewBtn.dataset.ts;
    const niche = previewBtn.dataset.niche;
    lastReports = {timestamp: ts, niche};
    openPreview();
    return;
  }
  const dlBtn = e.target.closest(".dl-btn");
  if (dlBtn) {
    const name = dlBtn.dataset.name;
    if (!name) return;
    dlBtn.disabled = true;
    const orig = dlBtn.textContent;
    dlBtn.textContent = "сохраняю…";
    try {
      const r = await fetchJSON("/save-to-disk", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name}),
      });
      if (r.cancelled) {
        dlBtn.textContent = orig;
      } else if (r.ok) {
        dlBtn.textContent = "✓ сохранено";
        setTimeout(() => { dlBtn.textContent = orig; dlBtn.disabled = false; }, 1800);
        return;
      }
    } catch (err) {
      alert("Ошибка сохранения: " + err.message);
      dlBtn.textContent = orig;
    }
    dlBtn.disabled = false;
    return;
  }

  const delBtn = e.target.closest(".report-del");
  if (delBtn) {
    if (!confirm("Удалить этот прогон? CSV будут стёрты с диска.")) return;
    const ts = delBtn.dataset.ts;
    const oldIdx = _reportIdx;
    try {
      await fetchJSON("/delete-report", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({timestamp: ts}),
      });
      // loadReports сбросит индекс на 0 — но если удалили из середины, лучше
      // остаться на той же позиции (или предыдущей, если это был последний)
      const r = await fetchJSON("/reports");
      _allReports = r.reports || [];
      _reportIdx = Math.min(oldIdx, Math.max(0, _allReports.length - 1));
      _renderCurrentReport();
    } catch (e) { alert(e.message); }
  }
});

// Храним список прогонов и текущий индекс — показываем по одной карточке
let _allReports = [];
let _reportIdx = 0;

function _renderHistoryNav() {
  const nav = $("history_nav");
  const counter = $("hist_counter");
  if (_allReports.length <= 1) {
    nav.classList.add("hidden");
    return;
  }
  nav.classList.remove("hidden");
  counter.textContent = `${_reportIdx + 1} / ${_allReports.length}`;
  $("hist_prev").disabled = _reportIdx <= 0;
  $("hist_next").disabled = _reportIdx >= _allReports.length - 1;
}

function _renderCurrentReport() {
  const target = $("reports");
  if (!_allReports.length) {
    target.innerHTML = `<div class="empty">Ещё нет завершённых прогонов.</div>`;
    _renderHistoryNav();
    return;
  }
  const rep = _allReports[_reportIdx];
  target.innerHTML = _reportItemHtml(rep);
  _renderHistoryNav();
}

function _reportItemHtml(rep) {
  const ts = rep.timestamp.replace(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/, "$3.$2.$1 · $4:$5");
  const links = [];
  links.push(`<button class="dl-btn preview-btn" data-ts="${rep.timestamp}" data-niche="${escapeHtml(rep.niche)}">📊 топ-30</button>`);
  if (rep.articles) {
    const kb = Math.round(rep.articles.size / 1024);
    links.push(`<button class="dl-btn" data-name="${escapeHtml(rep.articles.name)}">📄 статьи · ${kb} КБ</button>`);
  }
  if (rep.channels) {
    const kb = Math.round(rep.channels.size / 1024);
    links.push(`<button class="dl-btn" data-name="${escapeHtml(rep.channels.name)}">📋 каналы · ${kb} КБ</button>`);
  }
  let stats = "";
  if (rep.stats) {
    const s = rep.stats;
    const fmtSubs = (n) => n >= 1000 ? `${(n/1000).toFixed(1)}K` : `${n}`;
    stats = `<div class="report-stats">
      <span>${s.total_channels} каналов</span>
      ${s.profile_count ? `<span>· ${s.profile_count} профильных</span>` : ""}
      ${s.avg_subscribers ? `<span>· ср. ${fmtSubs(s.avg_subscribers)} подп.</span>` : ""}
    </div>${s.top_channel ? `<div class="report-top">🏆 ${escapeHtml(s.top_channel)}</div>` : ""}`;
  }
  return `<div class="report-item">
    <button class="report-del" data-ts="${rep.timestamp}" title="Удалить прогон">✕</button>
    <div class="report-niche">${escapeHtml(rep.niche)}</div>
    <div class="report-ts">${ts}</div>
    ${stats}
    <div class="report-links">${links.join("")}</div>
  </div>`;
}

// Стрелки навигации — один раз, безопасно при любом состоянии загрузки.
function _attachHistoryNav() {
  $("hist_prev")?.addEventListener("click", () => {
    if (_reportIdx > 0) { _reportIdx--; _renderCurrentReport(); }
  });
  $("hist_next")?.addEventListener("click", () => {
    if (_reportIdx < _allReports.length - 1) { _reportIdx++; _renderCurrentReport(); }
  });
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _attachHistoryNav);
} else {
  _attachHistoryNav();
}

async function loadReports() {
  try {
    const r = await fetchJSON("/reports");
    _allReports = r.reports || [];
    // При обновлении показываем самый свежий (индекс 0)
    _reportIdx = 0;
    _renderCurrentReport();
  } catch (e) {
    $("reports").textContent = "Ошибка загрузки";
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
  // Тихо запрашиваем разрешение на уведомления — пользователь увидит при первом запуске
  try {
    if (Notification && Notification.permission === "default") {
      Notification.requestPermission();
    }
  } catch (e) {}
  refreshStatus();
  loadReports();
  startPolling();
})();
