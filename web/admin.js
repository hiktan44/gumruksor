const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const formatDate = (value) =>
  value
    ? new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "short" }).format(
        new Date(Number(value) * 1000)
      )
    : "—";

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  setTimeout(() => node.classList.remove("visible"), 2500);
}

async function json(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "İstek tamamlanamadı.");
  return data;
}

// Tab Switching
function switchTab(tabId) {
  $$(".admin-tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  $$(".admin-tab-content").forEach((section) => {
    section.classList.toggle("active", section.id === `tab-${tabId}`);
  });

  if (tabId === "llm") loadLLMExpenses();
  else if (tabId === "payments") loadPayments();
  else if (tabId === "logs") loadLogs();
}

window.switchTab = switchTab;

$$(".admin-tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// Cache state
let allUsers = [];
let allLogs = [];
let currentLlmFilter = "monthly";

// TAB 1: OVERVIEW & USERS
async function loadOverview() {
  try {
    const data = await json("/api/admin/overview");
    allUsers = data.users || [];
    $("#adminUserCount").textContent = allUsers.length;

    const stats = {
      "Kayıtlı Kullanıcı": allUsers.length,
      "Kanıt Dosyaları": data.dossier_count || 0,
      "Görsel Analiz": data.usage?.vision || 0,
      "GTİP Tespiti": data.usage?.classification || 0,
      "Ön Değerlendirme": data.usage?.precheck || 0,
    };

    $("#adminStats").innerHTML = Object.entries(stats)
      .map(
        ([label, value]) => `
      <article class="quota-card">
        <header><b>${escapeHtml(label)}</b><span>bu ay</span></header>
        <strong>${escapeHtml(value)}</strong>
      </article>`
      )
      .join("");

    renderUsers(allUsers);
    renderQuickUsers(allUsers.slice(0, 5));

    // Consultants
    const consultants = data.consultants || [];
    $("#adminConsultantCount").textContent = consultants.length;
    $("#adminConsultants").innerHTML = consultants.length
      ? consultants
          .map(
            (item) => `
        <tr data-consultant="${escapeHtml(item.google_sub)}">
          <td><b>${escapeHtml(item.display_name)}</b><br><small>${escapeHtml(item.email)}</small></td>
          <td><b>${escapeHtml(item.title)}</b><br><small>${escapeHtml(item.bio)}</small><br><small>${(
              item.expertise || []
            )
              .map(escapeHtml)
              .join(" · ")}</small></td>
          <td>${escapeHtml(item.city || "—")}<br><small>${escapeHtml(item.experience_years)} yıl · ${escapeHtml(
              item.service_mode
            )}</small></td>
          <td>
            <select data-consultant-status class="admin-select">
              <option value="pending">İncelemede</option>
              <option value="active">Yayında</option>
              <option value="suspended">Askıda</option>
            </select>
          </td>
          <td><button type="button" class="btn-save" data-save-consultant>Kaydet</button></td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="5">Danışman başvurusu bulunmamaktadır.</td></tr>';

    consultants.forEach((item) => {
      const row = $(`[data-consultant="${CSS.escape(item.google_sub)}"]`);
      if (row) row.querySelector("[data-consultant-status]").value = item.status;
    });
  } catch (error) {
    $("#adminUsers").innerHTML = `<tr><td colspan="6" class="error-cell">${escapeHtml(error.message)}</td></tr>`;
  }
}

function renderQuickUsers(users) {
  $("#adminQuickUsers").innerHTML = users.length
    ? users
        .map(
          (user) => `
      <tr>
        <td><b>${escapeHtml(user.name || "Adsız")}</b><br><small>${escapeHtml(user.email)}</small></td>
        <td><span class="plan-badge plan-${escapeHtml(user.plan_code)}">${escapeHtml(user.plan_code.toUpperCase())}</span></td>
        <td><span class="status-badge status-${escapeHtml(user.subscription_status)}">${escapeHtml(user.subscription_status)}</span></td>
        <td>${escapeHtml(formatDate(user.last_login_at))}</td>
      </tr>`
        )
        .join("")
    : '<tr><td colspan="4">Kayıt yok.</td></tr>';
}

function renderUsers(users) {
  $("#adminUsers").innerHTML = users.length
    ? users
        .map(
          (user) => `
      <tr data-user="${escapeHtml(user.google_sub)}" data-email="${escapeHtml(user.email)}" data-name="${escapeHtml(user.name || '')}">
        <td>
          <b>${escapeHtml(user.name || "Adsız")}</b><br>
          <small class="user-email-text">${escapeHtml(user.email)}</small>
        </td>
        <td>
          <select data-plan class="admin-select">
            <option value="starter">Başlangıç</option>
            <option value="expert">Uzman</option>
            <option value="team">Ekip</option>
            <option value="institutional">Kurumsal</option>
          </select>
        </td>
        <td>
          <select data-status class="admin-select">
            <option value="active">Aktif</option>
            <option value="pending">Bekliyor</option>
            <option value="past_due">Ödeme Gecikmiş</option>
            <option value="cancelled">İptal</option>
          </select>
        </td>
        <td>
          <select data-role class="admin-select" title="Rol: editör veri inceleme kuyruğunu görür; yönetici her şeyi yönetir">
            <option value="user">Kullanıcı</option>
            <option value="consultant">Danışman</option>
            <option value="editor">Editör</option>
            <option value="admin">Yönetici</option>
          </select>
        </td>
        <td>${escapeHtml(formatDate(user.last_login_at))}</td>
        <td class="action-cell">
          <button type="button" class="btn-save" data-save>Kaydet</button>
          <button type="button" class="btn-credit" data-grant-credit title="Kredi / Kota Ekle">+ Kredi</button>
        </td>
      </tr>`
        )
        .join("")
    : '<tr><td colspan="6">Kullanıcı bulunamadı.</td></tr>';

  users.forEach((user) => {
    const row = $(`[data-user="${CSS.escape(user.google_sub)}"]`);
    if (row) {
      row.querySelector("[data-plan]").value = user.plan_code;
      row.querySelector("[data-status]").value = user.subscription_status;
      const roleSelect = row.querySelector("[data-role]");
      roleSelect.value = user.role || "user";
      roleSelect.dataset.initial = user.role || "user";
    }
  });
}

// User Search Filter
$("#userSearchInput").addEventListener("input", (event) => {
  const term = event.target.value.toLowerCase().trim();
  if (!term) {
    renderUsers(allUsers);
    return;
  }
  const filtered = allUsers.filter(
    (u) =>
      (u.email || "").toLowerCase().includes(term) ||
      (u.name || "").toLowerCase().includes(term) ||
      (u.plan_code || "").toLowerCase().includes(term)
  );
  renderUsers(filtered);
});

// TAB 2b: LLM CONNECTIVITY DIAGNOSTICS
function renderRecentLlmEvents(events) {
  const rows = (events || [])
    .map((event) => `
      <tr class="${event.ok ? "diag-ok" : "diag-fail"}">
        <td>${escapeHtml(formatDate(event.at))}</td>
        <td><b>${escapeHtml(event.operation || "")}</b><br><small>${escapeHtml([event.provider, event.model].filter(Boolean).join(" · "))}</small></td>
        <td>${event.ok ? "✅ Başarılı" : "❌ Başarısız"}<br><small>${event.elapsed_s != null ? `${escapeHtml(event.elapsed_s)} sn` : ""}</small></td>
        <td>${escapeHtml(event.detail || "")}</td>
      </tr>`)
    .join("");
  return `
    <h3>Son gerçek çağrılar (sunucu belleği, en yeni üstte)</h3>
    <table class="mini-table">
      <thead><tr><th>Zaman (UTC)</th><th>İşlem / sağlayıcı</th><th>Sonuç</th><th>Ayrıntı</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4">Sunucu yeniden başladığından beri kayıtlı çağrı yok.</td></tr>'}</tbody>
    </table>`;
}

async function showRecentLlmEvents() {
  const output = $("#llmDiagnosticsOutput");
  const buttons = [$("#llmDiagRecent"), $("#llmDiagText"), $("#llmDiagVision")].filter(Boolean);
  buttons.forEach((btn) => { btn.disabled = true; });
  output.innerHTML = "<p>Son çağrılar yükleniyor…</p>";
  try {
    const data = await json("/api/admin/llm-diagnostics?recent=1");
    output.innerHTML = renderRecentLlmEvents(data.recent);
  } catch (error) {
    output.innerHTML = `<p class="answer-error">${escapeHtml(error.message)}</p>`;
  } finally {
    buttons.forEach((btn) => { btn.disabled = false; });
  }
}

async function runLlmDiagnostics(vision) {
  const output = $("#llmDiagnosticsOutput");
  const buttons = [$("#llmDiagRecent"), $("#llmDiagText"), $("#llmDiagVision")].filter(Boolean);
  buttons.forEach((btn) => { btn.disabled = true; });
  output.innerHTML = `<p>${vision ? "Görsel" : "Metin"} testi çalışıyor… Her sağlayıcı için en fazla 25 saniye bekleniyor.</p>`;
  try {
    const data = await json(`/api/admin/llm-diagnostics?vision=${vision ? "1" : "0"}`);
    const keys = Object.entries(data.keys || {})
      .map(([name, present]) => `${escapeHtml(name)}: ${present ? "anahtar var" : "anahtar yok"}`)
      .join(" · ");
    const chains = [
      `birincil (${escapeHtml(data.primary)}): ${escapeHtml((data.chains?.primary || []).join(" → "))}`,
      ...(data.chains?.fallbacks || []).map((fb) => `yedek ${escapeHtml(fb.provider)}: ${escapeHtml((fb.models || []).join(" → "))}`),
    ].join("<br>");
    const rows = (data.checks || [])
      .map((check) => `
        <tr class="${check.ok ? "diag-ok" : "diag-fail"}">
          <td><b>${escapeHtml(check.provider)}</b><br><small>${escapeHtml(check.model || "")}</small></td>
          <td>${check.ok ? "✅ Yanıt verdi" : "❌ Başarısız"}</td>
          <td>${check.status != null ? `HTTP ${escapeHtml(check.status)}` : "—"}<br><small>${check.latency_ms != null ? `${escapeHtml(check.latency_ms)} ms` : ""}</small></td>
          <td>${escapeHtml(check.error || check.reply || "")}${check.schema_rejected ? `<br><small>Şema reddedildi, şemasız yeniden denendi: ${escapeHtml(check.schema_rejected)}</small>` : ""}</td>
        </tr>`)
      .join("");
    output.innerHTML = `
      <p><b>${data.healthy ? "En az bir sağlayıcı çalışıyor." : "Hiçbir sağlayıcı yanıt vermedi."}</b> Mod: ${escapeHtml(data.mode)} · Birincil: ${escapeHtml(data.primary)} (${escapeHtml(data.primary_host || "")})${data.primary_override ? ` · LLM_PRIMARY_PROVIDER=${escapeHtml(data.primary_override)}` : ""}</p>
      <p><small>${keys}</small></p>
      <p><small>${chains}</small></p>
      <p><small>Süre sınırları: istek ${escapeHtml(data.timeouts?.request_seconds)} sn · birincil pay ${escapeHtml(data.timeouts?.primary_budget_seconds)} sn · toplam ${escapeHtml(data.timeouts?.total_deadline_seconds)} sn</small></p>
      <table class="mini-table">
        <thead><tr><th>Sağlayıcı / model</th><th>Sonuç</th><th>Durum</th><th>Ayrıntı</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4">Test edilecek sağlayıcı bulunamadı.</td></tr>'}</tbody>
      </table>
      ${renderRecentLlmEvents(data.recent)}`;
  } catch (error) {
    output.innerHTML = `<p class="answer-error">${escapeHtml(error.message)}</p>`;
  } finally {
    buttons.forEach((btn) => { btn.disabled = false; });
  }
}

$("#llmDiagRecent")?.addEventListener("click", () => showRecentLlmEvents());
$("#llmDiagText")?.addEventListener("click", () => runLlmDiagnostics(false));
$("#llmDiagVision")?.addEventListener("click", () => runLlmDiagnostics(true));

// TAB 2: LLM EXPENSES
async function loadLLMExpenses(filter = currentLlmFilter) {
  currentLlmFilter = filter;
  $$("#llmFilterPills .filter-pill").forEach((pill) => {
    pill.classList.toggle("active", pill.dataset.filter === filter);
  });

  try {
    const data = await json(`/api/admin/llm-expenses?filter=${encodeURIComponent(filter)}`);
    $("#llmTotalCostUsd").textContent = `$${Number(data.total_cost_usd || 0).toFixed(4)}`;
    $("#llmTotalCostTry").textContent = `≈ ${Number(data.total_cost_try || 0).toLocaleString("tr-TR", { minimumFractionDigits: 2 })} TL`;
    $("#llmTotalTokens").textContent = Number(data.total_tokens || 0).toLocaleString("tr-TR");
    $("#llmCallCount").textContent = Number(data.call_count || 0).toLocaleString("tr-TR");
    $("#llmPeriodLabel").textContent = data.filter_label || filter;

    // Quick summary for tab 1
    const quickLogs = (data.recent_logs || []).slice(0, 5);
    $("#adminQuickLLM").innerHTML = quickLogs.length
      ? quickLogs
          .map(
            (log) => `
        <tr>
          <td><b>${escapeHtml(log.operation)}</b><br><small>${escapeHtml(log.model)}</small></td>
          <td>${Number(log.total_tokens || 0).toLocaleString("tr-TR")}</td>
          <td><strong>$${Number(log.cost_usd || 0).toFixed(4)}</strong></td>
          <td>${escapeHtml(formatDate(log.created_at))}</td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="4">Henüz LLM çağrısı yapılmamış.</td></tr>';

    // Breakdown by Model
    $("#llmByModel").innerHTML = (data.by_model || []).length
      ? `<table class="mini-table">
          <thead><tr><th>Model</th><th>İstek</th><th>Token</th><th>Tutar ($)</th></tr></thead>
          <tbody>` +
        data.by_model
          .map(
            (m) => `
          <tr>
            <td><code>${escapeHtml(m.model)}</code></td>
            <td>${m.call_count}</td>
            <td>${Number(m.total_tokens).toLocaleString("tr-TR")}</td>
            <td><strong>$${Number(m.total_cost_usd).toFixed(4)}</strong></td>
          </tr>`
          )
          .join("") +
        `</tbody></table>`
      : "<p class='empty-note'>Seçili dönemde model kullanımı yok.</p>";

    // Breakdown by Operation
    $("#llmByOperation").innerHTML = (data.by_operation || []).length
      ? `<table class="mini-table">
          <thead><tr><th>İşlem</th><th>İstek</th><th>Token</th><th>Tutar ($)</th></tr></thead>
          <tbody>` +
        data.by_operation
          .map(
            (op) => `
          <tr>
            <td><b>${escapeHtml(op.operation)}</b></td>
            <td>${op.call_count}</td>
            <td>${Number(op.total_tokens).toLocaleString("tr-TR")}</td>
            <td><strong>$${Number(op.total_cost_usd).toFixed(4)}</strong></td>
          </tr>`
          )
          .join("") +
        `</tbody></table>`
      : "<p class='empty-note'>Seçili dönemde işlem verisi yok.</p>";

    // Detailed Logs
    $("#llmRecentTable").innerHTML = (data.recent_logs || []).length
      ? data.recent_logs
          .map(
            (l) => `
        <tr>
          <td>${escapeHtml(formatDate(l.created_at))}</td>
          <td><small>${escapeHtml(l.email || l.google_sub || "Anonim / Sistem")}</small></td>
          <td><span class="op-badge">${escapeHtml(l.operation)}</span></td>
          <td><code>${escapeHtml(l.model)}</code></td>
          <td>${Number(l.prompt_tokens || 0).toLocaleString("tr-TR")} / ${Number(l.completion_tokens || 0).toLocaleString("tr-TR")} (${Number(l.total_tokens || 0).toLocaleString("tr-TR")})</td>
          <td><strong>$${Number(l.cost_usd || 0).toFixed(4)}</strong></td>
          <td><span class="status-badge status-${escapeHtml(l.status)}">${escapeHtml(l.status)}</span></td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="7">Kayıtlı çağrı bulunmuyor.</td></tr>';
  } catch (error) {
    toast(`LLM harcama verisi alınamadı: ${error.message}`);
  }
}

$("#llmFilterPills").addEventListener("click", (event) => {
  const pill = event.target.closest(".filter-pill");
  if (!pill) return;
  loadLLMExpenses(pill.dataset.filter);
});

// TAB 4: PAYMENTS
async function loadPayments() {
  try {
    const data = await json("/api/admin/payments");
    const subscriptions = data.subscriptions || [];
    const sessions = data.sessions || [];

    $("#adminSubscriptionsTable").innerHTML = subscriptions.length
      ? subscriptions
          .map(
            (s) => `
        <tr>
          <td><b>${escapeHtml(s.name || "—")}</b><br><small>${escapeHtml(s.email)}</small></td>
          <td><span class="plan-badge plan-${escapeHtml(s.plan_code)}">${escapeHtml(s.plan_code.toUpperCase())}</span></td>
          <td>${escapeHtml(s.billing_cycle || "aylık")}</td>
          <td><span class="status-badge status-${escapeHtml(s.status)}">${escapeHtml(s.status)}</span></td>
          <td>${escapeHtml(s.provider || "Stripe")}</td>
          <td><code>${escapeHtml(s.provider_subscription_ref || "—")}</code></td>
          <td>${escapeHtml(formatDate(s.updated_at))}</td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="7">Aktif ücretli abonelik kaydı yok.</td></tr>';

    $("#adminPaymentSessionsTable").innerHTML = sessions.length
      ? sessions
          .map(
            (p) => `
        <tr>
          <td><code>${escapeHtml(p.id.slice(0, 8))}…</code></td>
          <td><b>${escapeHtml(p.name || "—")}</b><br><small>${escapeHtml(p.email || p.google_sub)}</small></td>
          <td>${escapeHtml(p.plan_code)} / ${escapeHtml(p.billing_cycle)}</td>
          <td><span class="status-badge status-${escapeHtml(p.status)}">${escapeHtml(p.status)}</span></td>
          <td><code>${escapeHtml(p.provider_customer_ref || "—")}</code></td>
          <td>${escapeHtml(formatDate(p.created_at))}</td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="6">Ödeme oturumu kaydı yok.</td></tr>';
  } catch (error) {
    toast(`Ödeme verisi alınamadı: ${error.message}`);
  }
}

$("#refreshPaymentsBtn").addEventListener("click", loadPayments);

// TAB 5: LOGS
async function loadLogs() {
  try {
    const data = await json("/api/admin/logs?limit=200");
    allLogs = data.logs || [];
    renderLogs(allLogs);
  } catch (error) {
    $("#adminLogsTable").innerHTML = `<tr><td colspan="6" class="error-cell">${escapeHtml(error.message)}</td></tr>`;
  }
}

function renderLogs(logs) {
  $("#adminLogsTable").innerHTML = logs.length
    ? logs
        .map(
          (l) => `
      <tr>
        <td><span class="log-badge log-${escapeHtml(l.type)}">${escapeHtml(l.type)}</span></td>
        <td><small>${escapeHtml(l.actor || "Sistem")}</small></td>
        <td><b>${escapeHtml(l.action)}</b></td>
        <td><code>${escapeHtml(l.target || "—")}</code></td>
        <td><small class="details-text">${escapeHtml(l.details || "")}</small></td>
        <td>${escapeHtml(formatDate(l.created_at))}</td>
      </tr>`
        )
        .join("")
    : '<tr><td colspan="6">Log kaydı bulunamadı.</td></tr>';
}

$("#logSearchInput").addEventListener("input", (event) => {
  const term = event.target.value.toLowerCase().trim();
  if (!term) {
    renderLogs(allLogs);
    return;
  }
  const filtered = allLogs.filter(
    (l) =>
      (l.action || "").toLowerCase().includes(term) ||
      (l.actor || "").toLowerCase().includes(term) ||
      (l.target || "").toLowerCase().includes(term) ||
      (l.details || "").toLowerCase().includes(term)
  );
  renderLogs(filtered);
});

// KREDİ EKLEME MODALI ETKİLEŞİMİ
const modal = $("#creditModal");

function openCreditModal(userSub, userDisplay) {
  $("#modalUserSub").value = userSub;
  $("#modalUserDisplay").value = userDisplay;
  $("#creditQuantity").value = "10";
  $("#creditNote").value = "";
  modal.hidden = false;
}

function closeCreditModal() {
  modal.hidden = true;
}

$("#closeCreditModal").addEventListener("click", closeCreditModal);
$("#cancelCreditModal").addEventListener("click", closeCreditModal);
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeCreditModal();
});

$$(".quick-credit-pills .quick-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    $("#creditQuantity").value = pill.dataset.amount;
  });
});

$("#creditGrantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const sub = $("#modalUserSub").value;
  const operation = $("#creditOperation").value;
  const quantity = Number($("#creditQuantity").value);
  const note = $("#creditNote").value;

  const btn = $("#saveCreditBtn");
  btn.disabled = true;
  try {
    await json(`/api/admin/users/${encodeURIComponent(sub)}/credits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation, quantity, note }),
    });
    toast(`Kullanıcıya başarıyla +${quantity} kredi tanımlandı.`);
    closeCreditModal();
    loadOverview();
  } catch (error) {
    toast(`Hata: ${error.message}`);
  } finally {
    btn.disabled = false;
  }
});

// Save Plan & Status Handler
$("#adminUsers").addEventListener("click", async (event) => {
  const saveBtn = event.target.closest("[data-save]");
  const creditBtn = event.target.closest("[data-grant-credit]");
  const row = event.target.closest("[data-user]");
  if (!row) return;

  if (creditBtn) {
    const name = row.dataset.name || "Adsız";
    const email = row.dataset.email || "";
    openCreditModal(row.dataset.user, `${name} (${email})`);
    return;
  }

  if (saveBtn) {
    saveBtn.disabled = true;
    try {
      await json(`/api/admin/subscriptions/${encodeURIComponent(row.dataset.user)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_code: row.querySelector("[data-plan]").value,
          status: row.querySelector("[data-status]").value,
        }),
      });
      const roleSelect = row.querySelector("[data-role]");
      if (roleSelect && roleSelect.value !== roleSelect.dataset.initial) {
        await json(`/api/admin/users/${encodeURIComponent(row.dataset.user)}/role`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: roleSelect.value }),
        });
        roleSelect.dataset.initial = roleSelect.value;
      }
      toast("Abonelik ve rol güncellendi; denetim kaydı oluşturuldu.");
    } catch (error) {
      toast(error.message);
    } finally {
      saveBtn.disabled = false;
    }
  }
});

// Consultant Actions
$("#adminConsultants").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-save-consultant]");
  if (!button) return;
  const row = button.closest("[data-consultant]");
  button.disabled = true;
  try {
    await json(`/api/admin/consultants/${encodeURIComponent(row.dataset.consultant)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: row.querySelector("[data-consultant-status]").value }),
    });
    toast("Danışman profili güncellendi ve denetim kaydı oluşturuldu.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

// Initial load
loadOverview();
loadLLMExpenses();
