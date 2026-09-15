const state = {
  assistantHistory: [],
  scope: "ticaret",
  activeKind: "",
  offset: 0,
  page: 1,
  limit: 20,
  total: 0,
  currentDocument: null,
  currentContent: "",
  currentOffset: 0,
  currentTrigger: null,
  sourceData: null,
  customsImageData: null,
  customsVisionStatus: "idle",
  customsVisionResult: null,
  customsDocumentPages: null,
  customsClassificationResult: null,
  customsAutoGtip: null,
  customsGtipSelectionConfirmed: false,
  customsPendingSubmit: false,
  tariffRateSources: {},
  consultationBadge: 0,
  customsExactGtipConfirmed: false,
  customsApplyingTariffSelection: false,
  customsTariffTree: null,
  customsSelectedCandidate: null,
  customsClassificationAnswers: {},
  decisionAnswers: { precheck: {}, tool: {} },
  auth: null,
  currentCustomsResult: null,
  selectedConsultant: null,
  consultants: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const safeStorage = {
  getItem(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  },
  setItem(key, value) {
    try { localStorage.setItem(key, value); } catch (_) {}
  },
  removeItem(key) {
    try { localStorage.removeItem(key); } catch (_) {}
  },
};
const form = $("#searchForm");
const queryInput = $("#query");
const resultList = $("#resultList");
const statusNote = $("#status");
const resultCount = $("#resultCount");
const resultsTitle = $("#resultsTitle");
const pagination = $("#pagination");
const prevPage = $("#prevPage");
const nextPage = $("#nextPage");
const pageLabel = $("#pageLabel");
const reader = $("#reader");
const readerContent = $("#readerContent");
const readerSearch = $("#readerSearch");
const loadMoreContent = $("#loadMoreContent");
const numberFormat = new Intl.NumberFormat("tr-TR");

const kindLabels = {
  mevzuat: "Mevzuat",
  destek: "Devlet desteği",
  veri: "Veri ve istatistik",
  rapor: "Rapor",
  ulke_bilgisi: "Ülke bilgisi",
  iletisim: "İletişim",
  yayin: "Yayın",
};

const sourceLabels = {
  resmi_gazete_guncel: "Resmî Gazete / güncel mevzuat",
  gumruk: "Gümrük işlemleri",
  ihracat: "İhracat",
  ithalat: "İthalat",
  ithalat_duyurular: "İthalat Genel Müdürlüğü duyuruları",
  destekler: "Devlet destekleri",
  ic_ticaret: "İç ticaret",
  tuketici: "Tüketici",
  serbest_bolgeler: "Serbest bölgeler",
  urun_guvenligi: "Ürün güvenliği",
  hizmet_ticareti: "Hizmet ticareti",
  esnaf_kooperatif: "Esnaf ve kooperatif",
  urun_kurallari: "Ürün kuralları",
  istatistikler_veri: "İstatistikler",
  musavirlik_pazar: "Müşavirlik ve pazar bilgileri",
  musavirlik_blog_guncel: "Müşavirlik güncel bilgileri",
  yurtdisi_teskilati: "Yurt dışı teşkilatı",
  musavirlik_iletisim: "Müşavirlik iletişim",
  bakanlik_yayinlari: "Bakanlık yayınları",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch (_) {
    return "#";
  }
}

function parseLocalizedNumber(raw) {
  // Accepts Turkish ("1.234,56", "12,5"), plain ("1234.56") and thousands-only ("12.500") input.
  let value = String(raw ?? "").trim().replace(/\s+/g, "").replace(/[₺$€£]|TL|USD|EUR|GBP|%/gi, "");
  if (!value) return null;
  if (value.includes(",")) {
    value = value.replace(/\./g, "").replace(",", ".");
  } else if (/^-?\d{1,3}(\.\d{3})+$/.test(value)) {
    value = value.replace(/\./g, "");
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function nullableNumber(selector) {
  const input = $(selector);
  const number = parseLocalizedNumber(input?.value);
  if (number == null) {
    input?.classList.toggle("input-invalid", Boolean(input?.value?.trim()));
    return null;
  }
  const min = input?.dataset?.min != null ? Number(input.dataset.min) : null;
  const max = input?.dataset?.max != null ? Number(input.dataset.max) : null;
  const outOfRange = (min != null && number < min) || (max != null && number > max);
  input?.classList.toggle("input-invalid", outOfRange);
  return outOfRange ? null : number;
}

const localizedNumberFormat = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 4 });

function bindLocalizedNumberInputs() {
  document.querySelectorAll("input[data-number]").forEach((input) => {
    input.addEventListener("blur", () => {
      const number = parseLocalizedNumber(input.value);
      if (number == null) {
        input.classList.toggle("input-invalid", Boolean(input.value.trim()));
        return;
      }
      input.classList.remove("input-invalid");
      input.value = localizedNumberFormat.format(number);
    });
    input.addEventListener("focus", () => {
      // Raw digits are easier to edit; the value is re-formatted on blur.
      const number = parseLocalizedNumber(input.value);
      if (number != null) input.value = String(number).replace(".", ",");
    });
  });
}

async function loadCountryList() {
  const list = $("#countryList");
  if (!list) return;
  try {
    const data = await fetchJson("/api/tariff/countries");
    list.innerHTML = (data.items || []).map((item) => {
      const label = `${item.iso2} · ${item.regime_label}${item.pending_note ? " · yürürlük doğrulanmalı" : ""}`;
      return `<option value="${escapeHtml(item.name)}" label="${escapeHtml(label)}"></option>`;
    }).join("");
    state.countryList = data.items || [];
  } catch (error) {
    console.warn("Ülke listesi yüklenemedi", error);
  }
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat("tr-TR", includeTime
    ? { dateStyle: "medium", timeStyle: "short" }
    : { dateStyle: "medium" }).format(date);
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 2200);
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch (_) {
    showToast("Kopyalama izni alınamadı.");
  }
}

const AI_ANALYSIS_TIMEOUT_MS = 170000;
const AI_TIMEOUT_MESSAGE = "Analiz beklenenden uzun sürdü ve durduruldu. Lütfen tekrar deneyin; görsel için daha küçük veya daha net bir fotoğraf da yardımcı olur.";

async function fetchJson(url, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  let timer = null;
  if (timeoutMs && typeof AbortController !== "undefined") {
    const controller = new AbortController();
    fetchOptions.signal = controller.signal;
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  let response;
  try {
    response = await fetch(url, fetchOptions);
  } catch (error) {
    if (timer && error && error.name === "AbortError") throw new Error(AI_TIMEOUT_MESSAGE);
    throw new Error("Sunucuya ulaşılamadı. Bağlantınızı kontrol edip tekrar deneyin.");
  } finally {
    if (timer) clearTimeout(timer);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Sunucunun sebep kodu ve HTTP durumu hatayla birlikte tasinir: kota dolmasi ile
    // model arizasi arayuzde ayni gorunmemeli.
    const error = new Error(data.error || "İstek tamamlanamadı.");
    error.status = response.status;
    if (data.code) error.code = data.code;
    if (data.retry_after) error.retryAfter = Number(data.retry_after) || null;
    if (Array.isArray(data.upgrade) && data.upgrade.length) error.upgrade = data.upgrade;
    if (data.operation) error.operation = data.operation;
    throw error;
  }
  return data;
}

function describeRequestError(error, fallback = "İşlem tamamlanamadı.") {
  // Kullaniciya NE olduguna gore eyleme donuk tek cumle: kota, hiz siniri, oturum ya da
  // gercek bir ariza. Sunucunun kendi metni her zaman esas alinir.
  const message = (error && error.message) || fallback;
  if (!error || !error.status) return message;
  if (error.code === "quota_exceeded") {
    const next = (error.upgrade || []).find((plan) => plan.purchasable) || (error.upgrade || [])[0];
    if (!next) return message;
    const price = next.monthly_price_try
      ? `${numberFormat.format(next.monthly_price_try)} TL + KDV / ay`
      : "teklif usulü";
    const quota = next.quota == null ? "sınırsız" : `${numberFormat.format(next.quota)} adet/ay`;
    return `${message} ${next.name} paketi bu işlem için ${quota} veriyor (${price}).`;
  }
  if (error.status === 429) {
    const wait = error.retryAfter ? `${error.retryAfter} saniye` : "kısa bir süre";
    return `${message} ${wait} sonra tekrar deneyebilirsiniz.`;
  }
  if (error.status === 401 || error.code === "authentication_required") {
    return `${message} Sağ üstteki Google ile giriş düğmesini kullanın.`;
  }
  if (error.status === 413) {
    return `${message} Daha küçük bir dosya deneyin.`;
  }
  return message;
}

function confidenceLabel(value) {
  const map = { high: "yüksek", medium: "orta", low: "düşük" };
  return map[String(value || "").toLowerCase()] || "belirsiz";
}

function setLoading() {
  statusNote.hidden = true;
  pagination.hidden = true;
  resultList.innerHTML = Array.from({ length: 6 }, () => '<div class="skeleton" aria-hidden="true"></div>').join("");
  resultCount.textContent = "…";
  $("#resultContext").textContent = "kaynak taranıyor";
}

function setStatus(title, message, isError = false) {
  resultList.innerHTML = "";
  pagination.hidden = true;
  statusNote.hidden = false;
  statusNote.classList.toggle("error", isError);
  statusNote.querySelector("span").textContent = isError ? "!" : "00";
  statusNote.querySelector("strong").textContent = title;
  statusNote.querySelector("p").textContent = message;
}

function activeTicaretFilters() {
  const chips = [];
  if (state.activeKind) chips.push({ key: "kind", label: `Katman: ${kindLabels[state.activeKind] || state.activeKind}` });
  const source = $("#sourceFilter");
  if (source.value) chips.push({ key: "source", label: `Kaynak: ${source.selectedOptions[0]?.textContent || source.value}` });
  if ($("#documentType").value) chips.push({ key: "type", label: `Belge türü: ${$("#documentType").value}` });
  if ($("#yearFilter").value) chips.push({ key: "year", label: `Yıl: ${$("#yearFilter").value}` });
  if (!$("#includeRepealed").checked) chips.push({ key: "repealed", label: "Mülga kayıtlar gizli" });
  return chips;
}

function renderActiveFilterChips() {
  const chips = activeTicaretFilters();
  const container = statusNote.querySelector("p");
  if (!chips.length) { container.insertAdjacentHTML("beforeend", " Şu anda hiçbir filtre uygulanmıyor."); return; }
  container.insertAdjacentHTML("beforeend", `<span class="filter-chips">${chips.map((chip) => `<button type="button" class="filter-chip" data-clear-filter="${chip.key}"><span>${escapeHtml(chip.label)}</span><i aria-hidden="true">×</i></button>`).join("")}${chips.length > 1 ? '<button type="button" class="filter-chip clear-all" data-clear-filter="all">Tümünü kaldır</button>' : ""}</span>`);
}

function updatePagination(total, current, hasNext) {
  const page = state.scope === "ticaret" ? Math.floor(current / state.limit) + 1 : current;
  const pages = Math.max(1, Math.ceil(total / state.limit));
  pageLabel.textContent = `${page} / ${pages}`;
  prevPage.disabled = page <= 1;
  nextPage.disabled = !hasNext;
  pagination.hidden = total <= state.limit;
}

function renderTicaretResults(data) {
  statusNote.hidden = true;
  resultList.innerHTML = "";
  state.total = data.total;
  resultCount.textContent = numberFormat.format(data.total);
  $("#resultContext").textContent = data.excluded_repealed
    ? `kayıt · ${numberFormat.format(data.excluded_repealed)} mülga hariç`
    : "kayıt";
  updatePagination(data.total, data.offset, data.has_next);

  if (!data.documents.length) {
    setStatus("Bu dosyada kayıt bulunamadı.", "Daha kısa bir ifade deneyin veya aşağıdaki filtrelerden birini kaldırın.");
    renderActiveFilterChips();
    resultCount.textContent = "0";
    return;
  }

  data.documents.forEach((doc) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `result-item kind-${escapeHtml(doc.content_kind || "other")}${doc.is_repealed ? " repealed" : ""}`;
    item.dataset.kind = doc.content_kind || "other";
    const meta = [doc.document_type || kindLabels[doc.content_kind], doc.number ? `No ${doc.number}` : ""].filter(Boolean);
    item.innerHTML = `
      <span class="result-node" aria-hidden="true"></span>
      <span class="result-main">
        <strong class="result-title">${escapeHtml(doc.title)}</strong>
        <span class="result-subline">${meta.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</span>
        ${doc.context ? `<span class="result-context">${escapeHtml(doc.context)}</span>` : ""}
      </span>
      <span class="result-source">${escapeHtml(sourceLabels[doc.source_id] || doc.source_id)}</span>
      <span class="result-date">${escapeHtml(doc.publication_date || doc.page_updated_at || "—")}</span>
      <span class="result-arrow" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg></span>`;
    item.addEventListener("click", () => openTicaretDocument(doc, item));
    resultList.append(item);
  });
}

function renderGeneralResults(data) {
  statusNote.hidden = true;
  resultList.innerHTML = "";
  state.total = data.total;
  resultCount.textContent = numberFormat.format(data.total);
  $("#resultContext").textContent = "genel mevzuat kaydı";
  updatePagination(data.total, data.page, data.has_next);

  if (!data.documents.length) {
    setStatus("Bu ölçütlerle mevzuat bulunamadı.", "Arama alanını değiştirin veya mevzuat türü filtresini kaldırın.");
    resultCount.textContent = "0";
    return;
  }

  data.documents.forEach((doc) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "result-item";
    const meta = [doc.type_label, doc.number ? `No ${doc.number}` : "", doc.gazette_number ? `RG ${doc.gazette_number}` : ""].filter(Boolean);
    item.innerHTML = `
      <span class="result-node" aria-hidden="true"></span>
      <span class="result-main"><strong class="result-title">${escapeHtml(doc.title)}</strong><span class="result-subline">${meta.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</span></span>
      <span class="result-source">${escapeHtml(doc.type_label || "Mevzuat")}</span>
      <span class="result-date"${doc.date_warning ? ` title="${escapeHtml(doc.date_warning)}"` : ""}>${doc.gazette_date ? `${escapeHtml(formatDate(doc.gazette_date))}${doc.date_warning ? " ⚠" : ""}` : "—"}</span>
      <span class="result-arrow" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></svg></span>`;
    item.addEventListener("click", () => openGeneralDocument(doc, item));
    resultList.append(item);
  });
}

async function runTicaretSearch({ offset = 0 } = {}) {
  state.offset = offset;
  const query = queryInput.value.trim();
  const request = {
    query,
    content_kinds: state.activeKind ? [state.activeKind] : [],
    source_ids: $("#sourceFilter").value ? [$("#sourceFilter").value] : [],
    document_types: $("#documentType").value ? [$("#documentType").value] : [],
    year: $("#yearFilter").value || null,
    include_repealed: $("#includeRepealed").checked,
    offset,
    limit: state.limit,
  };
  resultsTitle.textContent = query ? `“${query}” dosyası` : (state.activeKind ? kindLabels[state.activeKind] : "Katalogdan son kayıtlar");
  setLoading();
  try {
    const data = await fetchJson("/api/ticaret/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
    });
    renderTicaretResults(data);
  } catch (error) {
    resultCount.textContent = "—";
    $("#resultContext").textContent = "arama başarısız";
    setStatus("Araştırma tamamlanamadı.", error.message || "Bağlantınızı kontrol edip yeniden deneyin.", true);
  }
}

async function runGeneralSearch({ page = 1 } = {}) {
  state.page = page;
  const query = queryInput.value.trim();
  const request = {
    query,
    mode: form.querySelector('input[name="generalMode"]:checked').value,
    type: $("#generalType").value,
    start_date: $("#startDate").value || null,
    end_date: $("#endDate").value || null,
    page,
    page_size: state.limit,
  };
  resultsTitle.textContent = query ? `“${query}” için genel mevzuat` : "Son yayımlanan mevzuat";
  setLoading();
  try {
    renderGeneralResults(await fetchJson("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
    }));
  } catch (error) {
    resultCount.textContent = "—";
    $("#resultContext").textContent = "arama başarısız";
    setStatus("Genel mevzuat araması tamamlanamadı.", error.message || "Bağlantınızı kontrol edip yeniden deneyin.", true);
  }
}

function runSearch(args = {}) {
  return state.scope === "ticaret" ? runTicaretSearch(args) : runGeneralSearch(args);
}

function citationRows(rows) {
  $("#citationSpine").innerHTML = rows.map(([label, value, warning, url]) => {
    const renderedValue = url
      ? `<a href="${safeUrl(url)}" target="_blank" rel="noreferrer">${escapeHtml(value || "Resmî sayfayı aç")}<span aria-hidden="true">↗</span></a>`
      : `<b>${escapeHtml(value || "—")}</b>`;
    return `<div class="citation-line${warning ? " warning" : ""}"><span>${escapeHtml(label)}</span>${renderedValue}</div>`;
  }).join("");
}

function renderOfficialLinks(doc, resolvedUrl = "") {
  const candidates = [
    [doc.document_url, doc.file_type && doc.file_type !== "html" && doc.file_type !== "link" ? `Belgeyi aç · ${String(doc.file_type).toUpperCase()}` : "Belge sayfasını aç", "Seçilen kayıt"],
    [doc.source_page_url, "Yayımlandığı sayfayı aç", sourceLabels[doc.source_id] || "Resmî kaynak"],
    [resolvedUrl, "Okunan resmî adresi aç", "Tam metnin alındığı adres"],
  ];
  const seen = new Set();
  const links = candidates.filter(([url]) => {
    if (!url) return false;
    const href = safeUrl(url || "");
    if (href === "#" || seen.has(href)) return false;
    seen.add(href);
    return true;
  });
  $("#readerLinks").innerHTML = links.map(([url, title, description]) => `
    <a href="${safeUrl(url)}" target="_blank" rel="noreferrer">
      <span><b>${escapeHtml(title)}</b><small>${escapeHtml(description)}</small></span><i aria-hidden="true">↗</i>
    </a>`).join("");
  $(".reader-links").hidden = links.length === 0;
}

function showReader(documentData, trigger) {
  state.currentDocument = documentData;
  state.currentTrigger = trigger;
  state.currentContent = "";
  state.currentOffset = 0;
  $("#evidenceEmpty").hidden = true;
  $("#evidenceDocument").hidden = false;
  reader.classList.add("open");
  reader.setAttribute("aria-hidden", "false");
  document.body.classList.add("reader-open");
  readerSearch.value = "";
  $("#readerOutline").hidden = true;
  $("#readerOutlineNav").innerHTML = "";
  $("#readerWarning").hidden = true;
  loadMoreContent.hidden = true;
  $$(".result-item").forEach((item) => item.classList.toggle("selected", item === trigger));
  if (window.innerWidth <= 1260) $(".panel-close").focus();
}

function highlightReaderText(value, term = "") {
  if (!term) return escapeHtml(value);
  const source = String(value);
  const lower = source.toLocaleLowerCase("tr-TR");
  const needle = term.toLocaleLowerCase("tr-TR");
  if (!needle) return escapeHtml(source);
  let cursor = 0;
  let output = "";
  while (cursor < source.length) {
    const index = lower.indexOf(needle, cursor);
    if (index < 0) {
      output += escapeHtml(source.slice(cursor));
      break;
    }
    output += `${escapeHtml(source.slice(cursor, index))}<mark>${escapeHtml(source.slice(index, index + term.length))}</mark>`;
    cursor = index + term.length;
  }
  return output;
}

function readerReferenceKey(value) {
  return String(value).trim().replace(/\s+/g, " ").toLocaleLowerCase("tr-TR");
}

function readerBlocks(content) {
  const normalised = String(content || "")
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+(?=(?:GEÇİCİ\s+)?MADDE\s+\d+[A-Za-z]?\s*[-–—])/giu, "\n");
  const blocks = [];
  normalised.split("\n").forEach((rawLine) => {
    const line = rawLine.trim();
    if (!line) return;
    const article = line.match(/^((?:GEÇİCİ\s+)?MADDE\s+\d+[A-Za-z]?)\s*[-–—]\s*(.*)$/iu);
    if (article) {
      blocks.push({ type: "heading", text: article[1], article: true });
      if (article[2]) blocks.push({ type: "paragraph", text: article[2] });
      return;
    }
    const numberedSection = /^(?:(?:BİRİNCİ|İKİNCİ|ÜÇÜNCÜ|DÖRDÜNCÜ|BEŞİNCİ|ALTINCI|YEDİNCİ|SEKİZİNCİ|DOKUZUNCU|ONUNCU)\s+)?(?:KISIM|BÖLÜM|FASIL)\b/iu.test(line);
    const appendixHeading = /^(?:EK|CETVEL|TABLO)\s*[-–—:]?\s*[A-Z0-9/]+(?:\s|$)/iu.test(line) && line.length <= 120;
    blocks.push({ type: numberedSection || appendixHeading ? "heading" : "paragraph", text: line, article: false });
  });
  return blocks;
}

function formatReaderText(value, term = "") {
  const source = String(value);
  const tokenPattern = /https?:\/\/[^\s<>"']+/giu;
  let cursor = 0;
  let output = "";
  for (const match of source.matchAll(tokenPattern)) {
    const index = match.index ?? 0;
    output += highlightReaderText(source.slice(cursor, index), term);
    let token = match[0];
    if (/^https?:\/\//iu.test(token)) {
      const trailing = token.match(/[),.;:]+$/u)?.[0] || "";
      if (trailing) token = token.slice(0, -trailing.length);
      output += `<a class="reader-external-link" href="${safeUrl(token)}" target="_blank" rel="noreferrer">${highlightReaderText(token, term)}</a>${escapeHtml(trailing)}`;
    }
    cursor = index + match[0].length;
  }
  output += highlightReaderText(source.slice(cursor), term);
  return output;
}

function renderReaderContent(term = "") {
  const content = state.currentContent || "";
  if (!content) {
    readerContent.textContent = "Bu kayıtta çıkarılabilir metin bulunamadı. Resmî kaynak bağlantısını açın.";
    $("#readerOutline").hidden = true;
    return;
  }

  const blocks = readerBlocks(content);
  const groups = [];
  let current = { id: "reader-preamble", title: "Belge başlangıcı", outline: false, blocks: [] };
  let sectionIndex = 0;
  blocks.forEach((block) => {
    if (block.type !== "heading") {
      current.blocks.push(block);
      return;
    }
    if (current.blocks.length || current.outline) groups.push(current);
    sectionIndex += 1;
    const articleKey = block.article ? readerReferenceKey(block.text) : "";
    const id = block.article
      ? `reader-${articleKey.replaceAll(" ", "-").replaceAll("ç", "c").replaceAll("ı", "i").replaceAll("ş", "s").replaceAll("ğ", "g").replaceAll("ü", "u").replaceAll("ö", "o")}`
      : `reader-section-${sectionIndex}`;
    current = { id, title: block.text, outline: true, blocks: [] };
  });
  if (current.blocks.length || current.outline) groups.push(current);

  const outlineGroups = groups.filter((group) => group.outline);
  const outline = $("#readerOutline");
  outline.hidden = outlineGroups.length === 0;
  $("#readerOutlineCount").textContent = `${numberFormat.format(outlineGroups.length)} bölüm`;
  $("#readerOutlineNav").innerHTML = outlineGroups.map((group) => `
    <button type="button" data-reader-anchor="${escapeHtml(group.id)}"><span>${escapeHtml(group.title)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg></button>`).join("");

  readerContent.innerHTML = groups.map((group) => {
    const body = group.blocks.map((block) => `<p>${formatReaderText(block.text, term)}</p>`).join("");
    if (!group.outline) return `<div class="reader-preamble">${body}</div>`;
    return `<details class="reader-section" id="${escapeHtml(group.id)}" tabindex="-1" open>
      <summary><span>${highlightReaderText(group.title, term)}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></summary>
      <div class="reader-section-body">${body || '<p class="reader-empty-section">Bu başlık altında ayrı metin bulunamadı.</p>'}</div>
    </details>`;
  }).join("");
}

function setReaderContent(content, append = false) {
  state.currentContent = append ? `${state.currentContent}\n${content}` : content;
  renderReaderContent(readerSearch.value.trim());
  $("#readerLength").textContent = `${numberFormat.format(state.currentContent.length)} karakter`;
}

async function openTicaretDocument(doc, trigger) {
  showReader(doc, trigger);
  $("#readerKind").textContent = `${kindLabels[doc.content_kind] || "Belge"} · ${String(doc.file_type || "web").toUpperCase()}`;
  $("#readerTitle").textContent = doc.title;
  $("#officialLink").href = safeUrl(doc.document_url || doc.source_page_url);
  renderOfficialLinks(doc);
  citationRows([
    ["Katman", kindLabels[doc.content_kind]],
    ["Kaynak", sourceLabels[doc.source_id] || doc.source_id, false, doc.source_page_url],
    ["Bölüm", doc.section, false, doc.source_page_url],
    ["Tür / no", [doc.document_type, doc.number].filter(Boolean).join(" · "), false, doc.document_url],
    ["Tarih", doc.publication_date || doc.page_updated_at, false, doc.source_page_url],
    ["Yürürlük", doc.is_repealed ? "Mülga / yürürlükten kaldırılmış" : "Kaynağından doğrulayın", doc.is_repealed],
  ]);
  readerContent.textContent = "Belge metni resmî kaynaktan çıkarılıyor…";
  $("#readerLength").textContent = "bekleniyor";
  try {
    const data = await fetchJson(`/api/ticaret/document/${encodeURIComponent(doc.id)}`);
    setReaderContent(data.content);
    renderOfficialLinks(doc, data.resolved_url);
    state.currentOffset = data.offset + data.returned_characters;
    loadMoreContent.hidden = !data.truncated;
    if (data.warnings?.length) {
      $("#readerWarning").textContent = data.warnings.join(" · ");
      $("#readerWarning").hidden = false;
    }
  } catch (error) {
    readerContent.textContent = error.message || "Belge metni alınamadı. Resmî kaynak bağlantısını kullanın.";
    $("#readerLength").textContent = "metin alınamadı";
  }
}

async function openGeneralDocument(doc, trigger) {
  showReader(doc, trigger);
  $("#readerKind").textContent = "Genel mevzuat";
  $("#readerTitle").textContent = doc.title;
  $("#officialLink").href = safeUrl(doc.source_url || "https://www.mevzuat.gov.tr");
  renderOfficialLinks({
    ...doc,
    document_url: doc.source_url || "https://www.mevzuat.gov.tr",
    source_page_url: doc.source_url || "https://www.mevzuat.gov.tr",
    source_id: "genel_mevzuat",
    file_type: "html",
  });
  citationRows([
    ["Katman", "Genel mevzuat"], ["Tür", doc.type_label, false, doc.source_url], ["Mevzuat no", doc.number, false, doc.source_url],
    ["Resmî Gazete", [doc.gazette_date && formatDate(doc.gazette_date), doc.gazette_number && `Sayı ${doc.gazette_number}`].filter(Boolean).join(" · "), false, doc.source_url],
  ]);
  readerContent.textContent = "Mevzuat metni resmî kaynaktan alınıyor…";
  $("#readerLength").textContent = "bekleniyor";
  try {
    const data = await fetchJson(`/api/document/${encodeURIComponent(doc.id)}`);
    setReaderContent(data.content);
  } catch (error) {
    readerContent.textContent = error.message || "Metin alınamadı.";
    $("#readerLength").textContent = "metin alınamadı";
  }
}

function closeReader() {
  reader.classList.remove("open");
  reader.setAttribute("aria-hidden", "true");
  document.body.classList.remove("reader-open");
  $("#evidenceDocument").hidden = true;
  $("#evidenceEmpty").hidden = false;
  $$(".result-item").forEach((item) => item.classList.remove("selected"));
  state.currentTrigger?.focus();
}

async function loadCatalogStatus() {
  const liveState = $("#liveState");
  try {
    // Listing sources initializes the catalogue on a cold start; status is read
    // afterwards so the coverage ledger cannot display a stale zero snapshot.
    const sources = await fetchJson("/api/ticaret/sources");
    const status = await fetchJson("/api/ticaret/status");
    state.sourceData = sources;
    liveState.classList.add("online");
    liveState.querySelector("span").textContent = status.syncing ? "Katalog yenileniyor" : "Kaynaklar güncel";
    $("#syncBadge").textContent = status.syncing ? "eşitleniyor" : "canlı";
    $("#sourceCount").textContent = numberFormat.format(status.source_count);
    $("#pageCount").textContent = numberFormat.format(status.pages_scanned);
    $("#documentCount").textContent = numberFormat.format(status.document_count);
    const priorityHours = Math.max(1, Math.round(status.sync_interval_seconds / 3600));
    const fullHours = Math.max(priorityHours, Math.round((status.full_sync_interval_seconds || status.sync_interval_seconds) / 3600));
    $("#refreshCadence").textContent = `${priorityHours} sa. / tam ${fullHours} sa.`;
    $("#latestGazetteDate").textContent = status.latest_official_gazette_date
      ? formatDate(status.latest_official_gazette_date)
      : "—";
    $("#lastSynced").textContent = status.last_synced_at ? `Son eşitleme: ${formatDate(status.last_synced_at, true)}` : "İlk eşitleme sürüyor.";
    $("#allCount").textContent = `${numberFormat.format(status.document_count)} canlı kayıt`;

    const sourceFilter = $("#sourceFilter");
    const selectedSource = sourceFilter.value;
    sourceFilter.replaceChildren(new Option("Tüm kaynaklar", ""));
    sources.sources.forEach((source) => {
      const option = document.createElement("option");
      option.value = source.id;
      option.textContent = `${sourceLabels[source.id] || source.id} (${numberFormat.format(source.documents)})`;
      sourceFilter.append(option);
    });
    if ([...sourceFilter.options].some((option) => option.value === selectedSource)) sourceFilter.value = selectedSource;
    $$(".source-link[data-kind]").forEach((button) => {
      const kind = button.dataset.kind;
      if (!kind || !sources.layers[kind]) return;
      const small = button.querySelector("small");
      small.dataset.base ||= small.textContent;
      small.textContent = `${small.dataset.base} · ${numberFormat.format(sources.layers[kind].documents)}`;
    });
    if (status.syncing) window.setTimeout(loadCatalogStatus, 30000);
  } catch (error) {
    liveState.classList.add("error");
    liveState.querySelector("span").textContent = "Katalog hazırlanıyor";
    $("#syncBadge").textContent = "bekleniyor";
  }
}

async function loadAuthState() {
  try {
    const response = await fetch("/api/auth/me", { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const auth = await response.json();
    state.auth = auth;
    if (!auth.authenticated || !auth.user) return;
    const firstName = String(auth.user.name || auth.user.email || "Hesabım").split(" ")[0];
    $("#appLogin").hidden = true;
    $("#appAccountButton").hidden = false;
    $("#appAccountButton").textContent = `${firstName} · Hesabım`;
    refreshConsultationBadge();
    renderWatchList();
    const params = new URLSearchParams(location.search);
    if (params.get("account")) {
      // Abonelik donusu: hesap paneli /api/account'u yeniden okur, yani yeni kota
      // hemen gecerli olur. Kullaniciya sonucu da acikca soyleriz.
      const payment = params.get("payment");
      openAccount(payment ? "plans" : "summary");
      if (payment === "success") showToast("Aboneliğiniz açıldı; yeni kotanız hemen geçerli.");
      if (payment === "failed") showToast("Ödeme tamamlanamadı. Paket sekmesinden yeniden deneyebilirsiniz.");
    }
  } catch (_) {
    // The application remains usable as a guest if account state is unavailable.
  }
}

const quotaLabels = { vision: "Görsel analiz", classification: "GTİP adayı", precheck: "Ön değerlendirme", dossier: "Kanıt dosyası" };

function switchAccountTab(tab) {
  $$('[data-account-tab]').forEach((button) => button.classList.toggle("active", button.dataset.accountTab === tab));
  $$('[data-account-panel]').forEach((panel) => { panel.hidden = panel.dataset.accountPanel !== tab; });
  if (tab === "dossiers") loadDossiers();
  if (tab === "compliance") loadCompliance();
}

const complianceStatusLabels = { good: "İyi durumda", watch: "Dikkat gerektiren noktalar var", risk: "Riskli: yüksek öncelikli uyarıları ele alın" };
const complianceSeverityLabels = { high: "Yüksek", medium: "Orta", low: "Düşük" };

function complianceBand(score) {
  return score >= 85 ? "good" : score >= 60 ? "watch" : "risk";
}

function renderCompliance(report) {
  const score = Math.max(0, Math.min(100, Number(report.score) || 0));
  const ring = $("#complianceRing");
  ring.dataset.status = report.status || complianceBand(score);
  const circumference = 2 * Math.PI * 52;
  $("#complianceRingValue").style.strokeDashoffset = String(circumference * (1 - score / 100));
  $("#complianceScore").textContent = String(score);
  const dossierNote = report.dossier_count ? `${report.dossier_count} kanıt dosyası · ${report.watch_count} izlenen kod` : "Henüz kanıt dosyası yok; puan yalnızca izleme listesine dayanır.";
  $("#complianceStatus").textContent = `${complianceStatusLabels[ring.dataset.status] || ""} · ${dossierNote}${report.email_alerts ? " · Yüksek uyarılar günde en fazla bir e-postayla özetlenir." : ""}`;
  const counts = report.alert_counts || {};
  $("#complianceCounts").innerHTML = ["high", "medium", "low"].map((level) => `<li data-severity="${level}">${complianceSeverityLabels[level]}: ${counts[level] || 0}</li>`).join("");
  $("#complianceComponents").innerHTML = (report.components || []).map((item) => `<article class="compliance-component" data-band="${complianceBand(item.score)}"><div><b>${escapeHtml(item.label)}</b><small>${escapeHtml(item.detail)} · ağırlık %${item.weight}</small></div><span class="component-score">${item.score}</span><div class="component-track"><i style="width:${item.score}%"></i></div></article>`).join("");
  const alerts = report.alerts || [];
  $("#complianceAlerts").innerHTML = alerts.length
    ? alerts.map((alert) => {
      const meta = [alert.gtip ? `GTİP <code>${escapeHtml(alert.gtip)}</code>` : "", alert.due_date ? `tarih ${escapeHtml(alert.due_date)}` : "", alert.source ? `kaynak ${escapeHtml(alert.source)}` : ""].filter(Boolean).join(" · ");
      return `<article class="compliance-alert" data-severity="${escapeHtml(alert.severity)}"><span class="severity">${escapeHtml(complianceSeverityLabels[alert.severity] || alert.severity)}</span><div><b>${escapeHtml(alert.title)}</b><small>${escapeHtml(alert.detail || "")}</small>${meta ? `<div class="alert-meta">${meta}</div>` : ""}</div></article>`;
    }).join("")
    : "<p>Erken uyarı yok. Kanıt dosyalarınız ve izleme listeniz için süresi dolan önlem, yakın değişiklik veya eksik onay bulunmadı.</p>";
  if (Array.isArray(report.warnings) && report.warnings.length) {
    $("#complianceAlerts").insertAdjacentHTML("beforeend", `<p class="alert-meta">${report.warnings.map((item) => escapeHtml(item)).join("<br>")}</p>`);
  }
}

async function loadCompliance() {
  $("#complianceStatus").textContent = "Uyum raporu hesaplanıyor…";
  try {
    renderCompliance(await fetchJson("/api/account/compliance"));
  } catch (error) {
    $("#complianceStatus").textContent = error.message;
    $("#complianceAlerts").innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

function hasCapability(feature) {
  const account = state.auth?.account;
  if (!account || !Array.isArray(account.capabilities)) return true; // sunucu karar verir
  return account.capabilities.includes(feature);
}

function featureUpsellHtml(feature) {
  const labels = state.featureLabels || {};
  const label = labels[feature] || "Bu özellik";
  const plans = (state.plans || []).filter((plan) => (plan.capabilities || []).includes(feature)).map((plan) => plan.name);
  const target = plans.length ? plans.join(" / ") : "Kurumsal";
  return `<div class="feature-locked"><p><b>${escapeHtml(label)}</b> mevcut paketinizde yok. <button type="button" class="link-button" data-open-plans>${escapeHtml(target)} paketine geçin</button>.</p></div>`;
}

function renderAccount(auth) {
  const account = auth.account;
  $("#accountIdentity").textContent = `${auth.user.name || "Kullanıcı"} · ${auth.user.email}`;
  $("#currentPlanName").textContent = account.plan.name;
  $("#currentPlanStatus").textContent = `${account.period} kullanım dönemi · ${account.subscription.status}`;
  $("#adminLink").hidden = !account.is_admin;
  $("#manageBilling").hidden = !(
    account.subscription.provider === "stripe"
    && ["active", "pending", "past_due"].includes(account.subscription.status)
  );
  $("#quotaGrid").innerHTML = Object.entries(account.quotas).map(([key, quota]) => {
    const percent = quota.limit == null ? 0 : Math.min(100, Math.round((quota.used / Math.max(1, quota.limit)) * 100));
    return `<article class="quota-card"><header><b>${escapeHtml(quotaLabels[key] || key)}</b><span>${quota.limit == null ? `${quota.used} / sınırsız` : `${quota.used} / ${quota.limit}`}</span></header><div class="quota-track"><i style="width:${percent}%"></i></div></article>`;
  }).join("");
}

async function openAccount(tab = "summary") {
  if (!state.auth?.authenticated) {
    location.href = "/auth/google";
    return;
  }
  const dialog = $("#accountDialog");
  if (!dialog.open) dialog.showModal();
  switchAccountTab(tab);
  renderAccount(state.auth);
  try {
    const [account, plans] = await Promise.all([fetchJson("/api/account"), fetchJson("/api/plans")]);
    state.auth.account = account;
    state.auth.billing_enabled = plans.billing_enabled;
    state.plans = plans.plans;
    state.featureLabels = plans.features || {};
    state.salesEmail = plans.sales_email || state.salesEmail;
    renderAccount(state.auth);
    renderPlans(plans.plans, plans.billing_enabled);
  } catch (error) { showToast(error.message); }
}

function renderPlans(plans, billingEnabled) {
  $("#pricingGrid").innerHTML = plans.map((plan) => {
    const monthly = plan.monthly_price_try == null ? "Teklif" : plan.monthly_price_try === 0 ? "Ücretsiz" : `${numberFormat.format(plan.monthly_price_try)} TL`;
    const purchasable = ["expert", "team"].includes(plan.code);
    const salesHref = `mailto:${escapeHtml(state.salesEmail || "")}?subject=${encodeURIComponent(`Ticaret Bilgi Masası · ${plan.name} paketi`)}`;
    const salesLink = `<a class="price-link" href="${salesHref}">Satış ekibiyle görüşün</a>`;
    const actions = purchasable
      ? (billingEnabled
        ? `<button type="button" data-buy-plan="${escapeHtml(plan.code)}" data-buy-cycle="monthly">Aylık başlat</button><button class="yearly" type="button" data-buy-plan="${escapeHtml(plan.code)}" data-buy-cycle="yearly">Yıllık · ${numberFormat.format(plan.yearly_price_try)} TL + KDV</button>`
        : `<p class="price-soon">Çevrim içi ödeme yakında açılıyor. Yıllık: ${numberFormat.format(plan.yearly_price_try)} TL + KDV.</p>${salesLink}`)
      : plan.code === "starter" ? '<button type="button" disabled>Mevcut ücretsiz paket</button>' : salesLink;
    const current = state.auth?.account?.plan?.code === plan.code ? '<em class="price-current">Mevcut paketiniz</em>' : "";
    return `<article class="price-card${plan.code === "expert" ? " featured" : ""}"><span>${escapeHtml(plan.code)}</span><h3>${escapeHtml(plan.name)}</h3>${current}<div class="price">${escapeHtml(monthly)}${plan.monthly_price_try ? "<small> + KDV / ay</small>" : ""}</div><ul>${plan.features.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><div class="price-actions">${actions}</div></article>`;
  }).join("");
}

async function loadDossiers() {
  const target = $("#dossierList");
  target.innerHTML = "<p>Kanıt dosyaları yükleniyor…</p>";
  try {
    const data = await fetchJson("/api/dossiers");
    target.innerHTML = data.items.length ? data.items.map((item) => `<article class="dossier-item"><div><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.gtip || "GTİP yok")} · ${escapeHtml(item.origin_country || "menşe yok")} · ${escapeHtml(formatDate(item.checked_at, true))}</small></div><div class="dossier-actions"><a href="/api/dossiers/${encodeURIComponent(item.id)}?download=1">JSON indir</a><button type="button" data-delete-dossier="${escapeHtml(item.id)}">Sil</button></div></article>`).join("") : "<p>Henüz sunucuda kayıtlı kanıt dosyanız yok.</p>";
  } catch (error) { target.innerHTML = `<p>${escapeHtml(error.message)}</p>`; }
}

statusNote.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-clear-filter]");
  if (!chip) return;
  const key = chip.dataset.clearFilter;
  const clear = {
    kind: () => selectSourceKind("", { run: false, focus: false }),
    source: () => { $("#sourceFilter").value = ""; },
    type: () => { $("#documentType").value = ""; },
    year: () => { $("#yearFilter").value = ""; },
    repealed: () => { $("#includeRepealed").checked = true; },
  };
  if (key === "all") Object.values(clear).forEach((fn) => fn()); else clear[key]?.();
  runTicaretSearch();
});

$("#appAccountButton").addEventListener("click", () => openAccount());
$("#closeAccount").addEventListener("click", () => $("#accountDialog").close());
$("#accountDialog").addEventListener("click", (event) => { if (event.target === $("#accountDialog")) $("#accountDialog").close(); });
$$('[data-account-tab]').forEach((button) => button.addEventListener("click", () => switchAccountTab(button.dataset.accountTab)));
$("#refreshDossiers").addEventListener("click", loadDossiers);
$("#refreshCompliance").addEventListener("click", loadCompliance);
$("#dossierList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-dossier]");
  if (!button || !confirm("Bu kanıt dosyası kalıcı olarak silinsin mi?")) return;
  try {
    await fetchJson(`/api/dossiers/${encodeURIComponent(button.dataset.deleteDossier)}`, { method: "DELETE" });
    await loadDossiers();
    try { state.auth.account = await fetchJson("/api/account"); renderAccount(state.auth); } catch { /* kota görünümü bir sonraki açılışta yenilenir */ }
    showToast("Kanıt dosyası silindi; kanıt dosyası kotanız güncellendi.");
  } catch (error) { showToast(error.message); }
});

$("#pricingGrid").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-buy-plan]");
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const checkout = await fetchJson("/api/billing/checkout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      plan_code: button.dataset.buyPlan, billing_cycle: button.dataset.buyCycle,
    }) });
    const target = new URL(checkout.checkout_url);
    if (target.protocol !== "https:" || target.hostname !== "checkout.stripe.com") throw new Error("Stripe yönlendirme adresi doğrulanamadı.");
    location.assign(target.href);
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
$("#manageBilling").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    const result = await fetchJson("/api/billing/portal", { method: "POST" });
    const target = new URL(result.portal_url);
    if (target.protocol !== "https:" || target.hostname !== "billing.stripe.com") throw new Error("Stripe abonelik adresi doğrulanamadı.");
    location.assign(target.href);
  } catch (error) { showToast(error.message); }
  finally { event.currentTarget.disabled = false; }
});
$("#deleteAccount").addEventListener("click", async () => {
  if (!confirm("Hesabınız, abonelik kaydınız, kullanım geçmişiniz ve kanıt dosyalarınız kalıcı olarak silinsin mi?")) return;
  try { await fetchJson("/api/account", { method: "DELETE" }); location.href = "/?account=deleted"; }
  catch (error) { showToast(error.message); }
});

function switchScope(scope) {
  if (scope === state.scope) return;
  state.scope = scope;
  $$("[data-scope]").forEach((button) => button.classList.toggle("active", button.dataset.scope === scope));
  $("#ticaretFilters").hidden = scope !== "ticaret";
  $("#generalFilters").hidden = scope !== "general";
  $(".app-shell").classList.toggle("general-layout", scope === "general");
  $(".app-shell").classList.toggle("customs-layout", scope === "customs");
  $(".source-sidebar").hidden = scope !== "ticaret";
  $("#researchWorkspace").hidden = scope === "customs";
  $("#customsWorkspace").hidden = scope !== "customs";
  reader.hidden = scope === "customs";
  if (scope === "customs") {
    closeReader();
    $("#customsQuestion").focus();
    return;
  }
  reader.hidden = false;
  queryInput.placeholder = scope === "ticaret" ? "Gümrük, ithalat, ihracat, destek veya ülke raporu ara" : "Kanun adı, mevzuat numarası veya içerik ara";
  closeReader();
  runSearch(scope === "ticaret" ? { offset: 0 } : { page: 1 });
}

function selectSourceKind(kind, { run = true, focus = true } = {}) {
  state.activeKind = kind;
  const selected = $(`.source-link[data-kind="${kind}"]`) || $('.source-link[data-kind=""]');
  $$('.source-link').forEach((item) => item.classList.toggle("active", item === selected));
  $$('.quick-route').forEach((item) => {
    const active = item.dataset.quickAction === kind;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", active ? "true" : "false");
  });
  $("#sourceFilter").value = "";
  if (focus) queryInput.focus();
  if (run) runTicaretSearch({ offset: 0 });
}

$$('.quick-route').forEach((button) => button.addEventListener("click", () => {
  const action = button.dataset.quickAction;
  if (action === "customs") {
    switchScope("customs");
    window.setTimeout(() => $("#productImage")?.focus(), 0);
    return;
  }
  if (state.scope !== "ticaret") switchScope("ticaret");
  selectSourceKind(action);
}));

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch(state.scope === "ticaret" ? { offset: 0 } : { page: 1 });
});
// Filtre değiştiğinde aramayı hemen yenile; "Ara" düğmesine basmak gerekmesin.
["#sourceFilter", "#documentType", "#yearFilter", "#includeRepealed"].forEach((selector) => {
  $(selector)?.addEventListener("change", () => {
    if (state.scope === "ticaret") runTicaretSearch({ offset: 0 });
  });
});
["#generalType", "#startDate", "#endDate"].forEach((selector) => {
  $(selector)?.addEventListener("change", () => {
    if (state.scope === "general") runGeneralSearch({ page: 1 });
  });
});
$$('input[name="generalMode"]').forEach((radio) => radio.addEventListener("change", () => {
  if (state.scope === "general" && queryInput.value.trim()) runGeneralSearch({ page: 1 });
}));
$$('[data-scope]').forEach((button) => button.addEventListener("click", () => switchScope(button.dataset.scope)));
$$('.source-link').forEach((button) => button.addEventListener("click", () => {
  selectSourceKind(button.dataset.kind);
}));
prevPage.addEventListener("click", () => state.scope === "ticaret"
  ? runTicaretSearch({ offset: Math.max(0, state.offset - state.limit) })
  : runGeneralSearch({ page: Math.max(1, state.page - 1) }));
nextPage.addEventListener("click", () => state.scope === "ticaret"
  ? runTicaretSearch({ offset: state.offset + state.limit })
  : runGeneralSearch({ page: state.page + 1 }));
$$('[data-close-reader]').forEach((button) => button.addEventListener("click", closeReader));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && reader.classList.contains("open")) closeReader();
});

readerSearch.addEventListener("input", () => {
  renderReaderContent(readerSearch.value.trim());
  requestAnimationFrame(() => readerContent.querySelector("mark")?.scrollIntoView({ behavior: "smooth", block: "center" }));
});

reader.addEventListener("click", (event) => {
  const control = event.target.closest("[data-reader-anchor]");
  if (!control || !reader.contains(control)) return;
  const target = document.getElementById(control.dataset.readerAnchor);
  if (!target) return;
  const section = target.matches("details") ? target : target.closest("details.reader-section");
  if (section) section.open = true;
  target.scrollIntoView({ behavior: "smooth", block: "start" });
  target.focus({ preventScroll: true });
});

$("#expandAllSections").addEventListener("click", () => {
  $$("#readerContent details.reader-section").forEach((section) => { section.open = true; });
});

$("#collapseAllSections").addEventListener("click", () => {
  $$("#readerContent details.reader-section").forEach((section) => { section.open = false; });
});

loadMoreContent.addEventListener("click", async () => {
  if (!state.currentDocument || state.scope !== "ticaret") return;
  loadMoreContent.disabled = true;
  loadMoreContent.textContent = "Devamı alınıyor…";
  try {
    const data = await fetchJson(`/api/ticaret/document/${encodeURIComponent(state.currentDocument.id)}?offset=${state.currentOffset}`);
    setReaderContent(data.content, true);
    state.currentOffset = data.offset + data.returned_characters;
    loadMoreContent.hidden = !data.truncated;
  } catch (error) {
    showToast(error.message || "Belgenin devamı alınamadı.");
  } finally {
    loadMoreContent.disabled = false;
    loadMoreContent.textContent = "Belgenin devamını getir";
  }
});

$("#copyCitation").addEventListener("click", () => {
  if (!state.currentDocument) return;
  const doc = state.currentDocument;
  const citation = state.scope === "ticaret"
    ? `${doc.title}. ${sourceLabels[doc.source_id] || doc.source_id}, T.C. Ticaret Bakanlığı. ${doc.publication_date || doc.page_updated_at || "Tarih belirtilmemiş"}. ${doc.document_url || doc.source_page_url}`
    : `${doc.title}. ${doc.type_label || "Mevzuat"}${doc.number ? `, No ${doc.number}` : ""}. ${doc.gazette_date || ""} ${doc.source_url || "https://www.mevzuat.gov.tr"}`;
  copyText(citation, "Belge atfı kopyalandı.");
});
$("#copyMcp").addEventListener("click", () => copyText(`${window.location.origin}/mcp`, "MCP adresi kopyalandı."));

function customsSourceMap(data) {
  return new Map((data.sources || []).map((source) => [source.id, source]));
}

function citationChips(citations, sourceMap) {
  if (!citations?.length) return "";
  return `<div class="citation-chips">${citations.map((id) => {
    const source = sourceMap.get(id);
    return source ? `<a href="${safeUrl(source.url)}" target="_blank" rel="noreferrer">[${escapeHtml(id)}]</a>` : "";
  }).join("")}</div>`;
}

function renderFindings(items, sourceMap, type = "finding") {
  if (!items?.length) return '<p class="missing-list">Bu başlıkta doğrulanmış bulgu üretilemedi.</p>';
  return `<div class="finding-grid">${items.map((item) => `
    <article class="finding-card ${escapeHtml(item.status)}">
      <span>${escapeHtml(item.status)}</span>
      <b>${escapeHtml(item.name)}</b>
      ${type === "tax" && item.rate ? `<p><strong>Oran:</strong> ${escapeHtml(item.rate)}${item.basis ? ` · ${escapeHtml(item.basis)}` : ""}</p>` : ""}
      <p>${escapeHtml(item.explanation)}</p>
      ${citationChips(item.citations, sourceMap)}
    </article>`).join("")}</div>`;
}

function renderCost(cost) {
  if (!cost) return '<p class="missing-list">Fatura bedeli girilmediği için kıymet hesabı yapılmadı.</p>';
  const currency = escapeHtml(cost.currency || "");
  const amount = (value) => value == null ? "Doğrulanmış oran eksik" : `${numberFormat.format(value)} ${currency}`;
  return `<table class="cost-table"><tbody>
    <tr><td>Tahmini gümrük kıymeti</td><td>${amount(cost.customs_value_estimate)}</td></tr>
    <tr><td>Gümrük vergisi</td><td>${amount(cost.customs_duty)}</td></tr>
    <tr><td>İlave vergi</td><td>${amount(cost.additional_duty)}</td></tr>
    <tr><td>KDV matrahı tahmini</td><td>${amount(cost.vat_base_estimate)}</td></tr>
    <tr><td>KDV</td><td>${amount(cost.vat)}</td></tr>
    <tr><td>Bilinen kalemlerle toplam</td><td>${amount(cost.known_landed_total)}</td></tr>
  </tbody></table>${(cost.missing_rates || []).length ? `<div class="result-caution"><b>Toplam için eksik girdiler:</b> ${cost.missing_rates.map((item) => escapeHtml(item)).join(" · ")}</div>` : ""}<p class="rate-warning">${escapeHtml(cost.note)}</p>${renderLiraSummary(cost.try_summary)}`;
}

function renderMeasureCoverage(coverage) {
  const labels = {
    customs_duty: "Gümrük vergisi",
    additional_duty: "İGV",
    additional_financial_liability: "Ek mali yükümlülük",
    anti_dumping: "Damping / sübvansiyon",
    surveillance: "Gözetim",
    safeguard: "Korunma önlemi",
    tariff_quota: "Tarife kontenjanı",
    vat: "KDV",
    kkdf: "KKDF",
    sct: "ÖTV",
  };
  const statusLabels = {
    verified_snapshot: "Canlı tabloda doğrulandı",
    partial_snapshot: "Kısmi kaynak",
    not_integrated: "Ayrıca doğrulanmalı",
    user_confirmation_required: "Doğrulanmış giriş gerekli",
  };
  const entries = Object.entries(coverage || {});
  if (!entries.length) return "";
  return `<div class="measure-coverage">${entries.map(([key, item]) => `<article data-state="${escapeHtml(item.status)}"><b>${escapeHtml(labels[key] || key)}</b><span>${escapeHtml(statusLabels[item.status] || item.status)}</span><p>${escapeHtml(item.note)}</p></article>`).join("")}</div>`;
}

function renderExpertReviewPacket(packet) {
  if (!packet) return "";
  const labels = { BTB: "Bağlayıcı Tarife Bilgisi", "gümrük_müşaviri": "Yetkili gümrük müşaviri", "yetkili_kurum": "Yetkili kurum" };
  const hashes = [
    ...(packet.tariff_snapshot_sha256 || []),
    ...(packet.control_document_sha256 || []),
    ...(packet.classification_snapshot_sha256 || []),
  ];
  return `<section class="answer-section expert-review" data-risk="${escapeHtml(packet.risk_level)}"><h3>Uzman / BTB inceleme dosyası · ${escapeHtml(packet.risk_level)}</h3>
    <div class="expert-review-grid"><div><b>İnceleme kanalları</b><p>${(packet.review_types || []).map((item) => escapeHtml(labels[item] || item)).join(" · ") || "Ek inceleme gerekmiyor"}</p></div><div><b>Tarife yolu</b><p>${(packet.classification_path || []).map((item) => `<code>${escapeHtml(item)}</code>`).join(" › ") || "Kesinleşmedi"}</p></div></div>
    <ul class="missing-list">${(packet.reasons || ["Yüksek risk nedeni bildirilmedi."]).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    ${(packet.questions_for_reviewer || []).length ? `<details class="expert-questions"><summary>Uzman için hazırlanmış sorular</summary><ol>${packet.questions_for_reviewer.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol></details>` : ""}
    ${hashes.length ? `<p class="expert-hashes"><b>Kanıt izleri:</b> ${hashes.map((item) => `<code title="${escapeHtml(item)}">${escapeHtml(item.slice(0, 12))}…</code>`).join(" ")}</p>` : ""}
    <button class="secondary-action" id="downloadExpertPacket" type="button">Müşavir / BTB dosyasını JSON indir</button></section>`;
}

const WORKFLOW_STATUS_LABELS = { done: "Tamam", pending: "Bekliyor", blocked: "Engelli", not_applicable: "Uygulanmaz" };

function workflowSummary(steps) {
  const counts = { done: 0, pending: 0, blocked: 0, not_applicable: 0 };
  steps.forEach((step) => { if (step.status in counts) counts[step.status] += 1; });
  const applicable = steps.length - counts.not_applicable;
  return { ...counts, total: steps.length, completion_ratio: applicable > 0 ? counts.done / applicable : 1 };
}

// PRD Faz 2.4: the static guide is replaced by the workflow computed server-side from the
// precheck result. No result (or a dossier saved before the field existed) keeps the static guide.
function renderWorkflow(steps) {
  const output = $("#workflowOutput");
  const guide = $("#guideStatic");
  if (!output || !guide) return;
  if (!Array.isArray(steps) || !steps.length) {
    output.hidden = true; output.innerHTML = ""; guide.hidden = false;
    return;
  }
  const summary = workflowSummary(steps);
  const percent = Math.round(summary.completion_ratio * 100);
  output.innerHTML = `
    <div class="workflow-head">
      <h3>Hesaplanan işlem akışı · ${steps.length} adım</h3>
      <div class="workflow-meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" aria-label="Tamamlanma oranı"><i style="width:${percent}%"></i></div>
      <small>%${percent} tamamlandı (uygulanmayan adımlar hariç)</small>
      <div class="workflow-counts"><span>${summary.done} tamam</span><span>${summary.pending} bekliyor</span><span>${summary.blocked} engelli</span><span>${summary.not_applicable} uygulanmaz</span></div>
    </div>
    <ol class="workflow-list">${steps.map((step) => `
      <li class="workflow-step ${escapeHtml(step.status)}">
        <b>${Number(step.order) || ""}</b>
        <div>
          <h4>${escapeHtml(step.title)}</h4>
          <p>${escapeHtml(step.summary)}</p>
          ${step.next_action ? `<p class="workflow-next">${escapeHtml(step.next_action)}</p>` : ""}
          ${step.legal_basis ? `<small class="workflow-legal">${escapeHtml(step.legal_basis)}</small>` : ""}
          ${(step.evidence_refs || []).length ? `<small class="workflow-refs">Kaynak alanlar: ${step.evidence_refs.map((ref) => escapeHtml(ref)).join(", ")}</small>` : ""}
        </div>
        <span class="workflow-badge ${escapeHtml(step.status)}">${escapeHtml(WORKFLOW_STATUS_LABELS[step.status] || step.status)}</span>
      </li>`).join("")}</ol>
    <p class="workflow-legal-note">Adım durumları yalnızca ön değerlendirme sonucundaki alanlardan kural tabanlı türetilir; yapay zekâ yorumu içermez ve bağlayıcı idari karar değildir. GTİP forma otomatik yazılmaz.</p>`;
  guide.hidden = true;
  output.hidden = false;
}

// PRD Faz 2.3: karar soruları. Sorular sunucuda kural tabanlı üretilir; cevap yalnız
// kullanıcıdan gelir ve seçildiği anda ilgili maliyet alanına yazılır (otomatik doldurma yok).
const DECISION_FIELDS = {
  precheck: {
    vat_rate: "#vatRate", kkdf_rate: "#kkdfRate", payment_method: "#paymentMethod",
    has_surveillance_certificate: "#hasSurveillanceCertificate", atr_certificate: "#assistAtr",
    condition: "#productCondition",
  },
  tool: {
    vat_rate: "#tariffVat", kkdf_rate: "#tariffKkdf", payment_method: "#tariffPayment",
    has_surveillance_certificate: "#tariffSurveillanceCertificate", atr_certificate: "#tariffAtr",
  },
};

function decisionQuestionsHtml(questions, context) {
  const list = Array.isArray(questions) ? questions : [];
  if (!list.length) return "";
  const answered = state.decisionAnswers[context] || {};
  const rows = list.map((question) => {
    const id = String(question.id || "");
    const current = answered[id] || "";
    const options = (question.options || []).map((option) => {
      const value = String(option.value);
      const checked = current === value ? " checked" : "";
      return `<label><input type="radio" name="decision-${escapeHtml(context)}-${escapeHtml(id)}" value="${escapeHtml(value)}" data-decision-context="${escapeHtml(context)}" data-decision-id="${escapeHtml(id)}"${checked}><span>${escapeHtml(option.label)}</span></label>`;
    }).join("");
    return `<div class="decision-question-row${current ? " answered" : ""}" data-decision-row="${escapeHtml(id)}">
      <p>${escapeHtml(question.text)}</p>
      <div class="decision-question-options">${options}</div>
      <span class="decision-filled">cevabınızla dolduruldu</span>
      <small>${escapeHtml(question.reason || "")}${question.legal_basis ? ` · ${escapeHtml(question.legal_basis)}` : ""}</small>
    </div>`;
  }).join("");
  return `<section class="decision-question-panel">
    <header><div><span class="eyebrow">Oranlar yalnız cevabınızla hesaba girer</span><h4>Karar soruları</h4></div><span>${list.length} soru</span></header>
    <div class="decision-question-list" data-decision-context="${escapeHtml(context)}">${rows}</div>
    <p class="decision-question-note">Cevabınız ilgili maliyet alanına yazılır; hesabı yeniden çalıştırdığınızda cevaplar birlikte gönderilir. Hiçbir oran otomatik doldurulmaz.</p>
  </section>`;
}

function updateDecisionProgress() {
  const panel = $("#decisionQuestionPanel");
  const progress = $("#decisionQuestionProgress");
  if (!panel || !progress) return;
  const rows = panel.querySelectorAll("[data-decision-row]").length;
  const answered = panel.querySelectorAll(".decision-question-row.answered").length;
  progress.textContent = `${answered} / ${rows} cevap`;
}

function renderDecisionQuestionPanel(questions) {
  const panel = $("#decisionQuestionPanel");
  const list = $("#decisionQuestionList");
  if (!panel || !list) return;
  const items = Array.isArray(questions) ? questions : [];
  panel.hidden = items.length === 0;
  if (!items.length) {
    list.replaceChildren();
    updateDecisionProgress();
    return;
  }
  const wrapper = document.createElement("div");
  wrapper.innerHTML = decisionQuestionsHtml(items, "precheck");
  const rendered = wrapper.querySelector(".decision-question-list");
  list.innerHTML = rendered ? rendered.innerHTML : "";
  updateDecisionProgress();
}

function setDecisionField(selector, value) {
  const field = $(selector);
  if (!field || value == null) return false;
  const text = String(value);
  if (field.tagName === "SELECT") {
    const match = Array.from(field.options).find((option) => option.value === text || option.text === text);
    if (!match) return false;
    field.value = match.value;
  } else {
    field.value = text;
  }
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}

// Cevabın maliyet alanlarına eşlemesi; sunucudaki apply_decision_answers ile aynı kurallar.
function applyDecisionAnswerToFields(questionId, value, context) {
  const fields = DECISION_FIELDS[context] || {};
  const applied = [];
  const remember = (label, ok) => { if (ok) applied.push(label); };
  if (questionId === "vat_rate" || questionId === "vat_rate_confirm") {
    const rate = Number(String(value).replace(",", "."));
    if (Number.isFinite(rate) && rate >= 0 && rate <= 100) remember("KDV", setDecisionField(fields.vat_rate, rate));
  } else if (questionId === "payment_method") {
    const cash = value === "pesin";
    remember("Ödeme şekli", setDecisionField(fields.payment_method, cash ? "Peşin" : "Vadeli mal mukabili")
      || setDecisionField(fields.payment_method, cash ? "Peşin" : "Vadeli"));
    remember("KKDF", setDecisionField(fields.kkdf_rate, cash ? 0 : 6));
  } else if (questionId === "surveillance_certificate") {
    remember("Gözetim belgesi", setDecisionField(fields.has_surveillance_certificate, value));
  } else if (questionId === "atr_certificate") {
    remember("A.TR", setDecisionField(fields.atr_certificate, value));
  } else if (questionId === "used_goods") {
    remember("Ürün durumu", setDecisionField(fields.condition, value === "true" ? "used" : "new"));
  }
  return applied;
}

document.addEventListener("change", (event) => {
  const input = event.target.closest("[data-decision-id]");
  if (!input || !input.checked) return;
  const context = input.dataset.decisionContext === "tool" ? "tool" : "precheck";
  const questionId = input.dataset.decisionId;
  state.decisionAnswers[context] = { ...(state.decisionAnswers[context] || {}), [questionId]: input.value };
  const row = input.closest(".decision-question-row");
  if (row) row.classList.add("answered");
  updateDecisionProgress();
  const applied = applyDecisionAnswerToFields(questionId, input.value, context);
  if (typeof updateReadiness === "function") updateReadiness();
  showToast(applied.length
    ? `${applied.join(", ")} alanı cevabınızla dolduruldu; hesabı yeniden çalıştırın.`
    : "Cevabınız kaydedildi; hesabı yeniden çalıştırdığınızda gönderilir.");
});

function renderCustomsResult(data) {
  setFlowStep(4);
  exportStore.precheck = data;
  state.currentCustomsResult = data;
  saveLocalScenario(data);
  renderWorkflow(data.workflow);
  renderDecisionQuestionPanel(data.decision_questions);
  const sourceMap = customsSourceMap(data);
  const statusLabels = {
    preliminary: "Ön değerlendirme",
    needs_information: "Bilgi gerekli",
    insufficient_evidence: "Kanıt yetersiz",
    evidence_only: "Yalnız kanıt paketi",
  };
  const warningStatus = data.status !== "preliminary";
  const candidates = data.candidate_gtips?.length ? `
    <div class="candidate-grid">${data.candidate_gtips.map((item) => `
      <article class="candidate-card"><code>${escapeHtml(item.code)}</code><b>${escapeHtml(item.confidence)} güven</b><p>${escapeHtml(item.explanation)}</p>${citationChips(item.citations, sourceMap)}</article>`).join("")}</div>`
    : '<p class="missing-list">Kanıtla desteklenen aday kod üretilemedi. Fotoğraf tek başına kesin GTİP değildir.</p>';
  const sources = (data.sources || []).map((source) => `
    <a href="${safeUrl(source.url)}" target="_blank" rel="noreferrer">
      <code>[${escapeHtml(source.id)}]</code><span><b>${escapeHtml(source.title)}${String(source.id || "").startsWith("hyb_") ? '<em class="source-badge">anlamsal eşleşme</em>' : ""}</b><small>${escapeHtml(source.authority)}${source.fetch_warning ? ` · ${escapeHtml(source.fetch_warning)}` : ""}</small></span>
    </a>`).join("");
  $("#customsOutput").innerHTML = `
    <article class="answer-sheet">
      <header class="answer-head">
        <span class="answer-status${warningStatus ? " warning" : ""}">${escapeHtml(statusLabels[data.status] || data.status)}</span>
        <div><h2>İthalat ön değerlendirme dosyası</h2><p>${escapeHtml(data.summary)}</p></div>
        <time>${escapeHtml(formatDate(data.as_of, true))}</time>
      </header>
      <section class="answer-section"><h3>Aday GTİP / CN kodları</h3>${candidates}</section>
      ${data.tariff_lookup ? `<section class="answer-section"><h3>Resmî tarife snapshot eşleşmesi</h3>${tariffMatchSummary(data.tariff_lookup)}${renderMeasureCoverage(data.tariff_lookup.measure_coverage)}${renderTradeMeasures(data.tariff_lookup.trade_measures)}${renderExciseTax(data.tariff_lookup.excise_tax)}<table class="evidence-table"><thead><tr><th>GTİP / Önlem</th><th>Oran</th><th>Menşe sütunu</th><th>Kaynak satırı</th><th>Kanıt</th></tr></thead><tbody>${tariffRows(data.tariff_lookup.measures)}</tbody></table>${exportBar("precheck", [{ table: "measures", label: "Tarife satırları" }, ...(data.deterministic_cost ? [{ table: "cost", label: "Maliyet taslağı" }] : [])])}${applyRatesButton(data.tariff_lookup, "precheck")}${(data.tariff_lookup.warnings || []).length ? `<div class="result-caution">${data.tariff_lookup.warnings.map((item) => escapeHtml(item)).join(" · ")}</div>` : ""}</section>` : ""}
      ${data.origin_documents ? `<section class="answer-section"><h3>Menşe belgeleri · ${escapeHtml(data.origin_documents.regime_name)}</h3><ul class="missing-list">${(data.origin_documents.documents || []).map((item) => `<li><b>${escapeHtml(item.name)}</b> — ${escapeHtml(item.applicability)}${item.note ? ` <small>${escapeHtml(item.note)}</small>` : ""}</li>`).join("")}</ul><div class="result-caution">${escapeHtml((data.origin_documents.caveats || []).join(" "))}</div></section>` : ""}
      ${data.control_lookup ? `<section class="answer-section"><h3>Resmî kontrol tebliği Ek-1 eşleşmeleri</h3>${renderControlTool(data.control_lookup)}</section>` : ""}
      <section class="answer-section"><h3>Eksik veya teyit edilmesi gereken bilgiler</h3><ul class="missing-list">${(data.missing_information?.length ? data.missing_information : ["Kritik eksik alan bildirilmedi."]).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>
      <section class="answer-section"><h3>TAREKS · TSE · kimyasal · laboratuvar kontrolleri</h3>${renderFindings(data.controls, sourceMap)}</section>
      <section class="answer-section"><h3>Gerekli belge ve izinler</h3>${renderFindings(data.required_documents, sourceMap)}</section>
      <section class="answer-section"><h3>Vergi ve mali yükümlülük bulguları</h3>${renderFindings(data.taxes, sourceMap, "tax")}</section>
      <section class="answer-section"><h3>Kullanıcı oranlarıyla maliyet taslağı</h3>${renderCost(data.deterministic_cost)}</section>
      ${data.image_observation ? `<section class="answer-section"><h3>Fotoğrafta görülenler</h3><p class="missing-list">${escapeHtml(data.image_observation)}</p></section>` : ""}
      ${renderExpertReviewPacket(data.expert_review_packet)}
      <section class="answer-section"><h3>Sonraki güvenli adımlar</h3><ol class="next-list">${(data.next_steps || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol></section>
      <section class="answer-section"><h3>Resmî kanıt defteri · ${(data.sources || []).length} kaynak</h3><div class="source-ledger">${sources}</div></section>
      <div class="legal-banner"><strong>Önemli:</strong> ${escapeHtml(data.legal_notice)}</div>
      <div class="result-actions"><button class="consultant-send-result" id="sendResultToConsultant" type="button">Danışmana gönder</button><button id="saveScenario" type="button">Kanıt dosyasına kaydet</button><button id="printPrecheck" type="button">PDF olarak kaydet</button><button id="emailPrecheck" type="button">E-posta ile gönder</button></div>
    </article>
    <form class="followup-box" id="followupForm"><label class="field"><span>Bu ürün için takip sorusu</span><input id="followupQuestion" maxlength="1500" placeholder="Örn. TAREKS başvurusunda hangi teknik dosyalar hazırlanmalı?"></label><button class="analyse-button" type="submit"><span>Takip sorusunu sor</span><svg viewBox="0 0 24 24"><path d="m5 12 14 0M14 6l6 6-6 6"/></svg></button></form>`;
  $("#followupForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("#followupQuestion").value.trim();
    if (!value) return;
    $("#customsQuestion").value = value;
    $("#customsForm").requestSubmit();
  });
  $("#saveScenario")?.addEventListener("click", async () => {
    saveLocalScenario(data);
    if (!state.auth?.authenticated) {
      showToast("Ön değerlendirme bu cihaza kaydedildi. Kalıcı sunucu kaydı ve paylaşım için Google ile giriş yapabilirsiniz.");
      return;
    }
    const button = $("#saveScenario"); button.disabled = true;
    try {
      await fetchJson("/api/dossiers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        title: data.inquiry?.product_description || $("#productDescription").value.trim() || "İthalat ön değerlendirmesi",
        product_name: $("#productDescription").value.trim(),
        gtip: data.inquiry?.candidate_gtip || $("#candidateGtip").value.trim(),
        origin_country: data.inquiry?.origin_country || $("#originCountry").value.trim(),
        effective_date: data.as_of || new Date().toISOString(), result: data,
      }) });
      showToast("Kanıt dosyası kaynak hash’leriyle sunucuya kaydedildi.");
      button.textContent = "Kanıt dosyasına kaydedildi";
      const auth = await fetchJson("/api/auth/me"); state.auth = auth;
    } catch (error) { showToast(error.message); button.disabled = false; }
  });
  $("#downloadExpertPacket")?.addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(data.expert_review_packet, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `gumrukce-uzman-dosyasi-${data.inquiry?.candidate_gtip || "gtip-belirsiz"}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("#sendResultToConsultant")?.addEventListener("click", () => {
    switchCustomsView("consultants");
    $("#consultantsPanelTitle")?.scrollIntoView({ behavior: "smooth", block: "start" });
    showToast("Bir danışman seçerek kontrollü dosya devrini başlatın.");
  });
}

function updateReadiness() {
  const tariffCode = $("#candidateGtip").value.replace(/\D/g, "");
  const checks = {
    vision: !state.customsImageData || state.customsVisionStatus === "confirmed",
    description: $("#productDescription").value.trim().length >= 12,
    origin: Boolean($("#originCountry").value.trim()),
    gtip: state.customsGtipSelectionConfirmed && [6, 8, 10, 12].includes(tariffCode.length),
    cost: ["#invoiceValue", "#freight", "#insurance"].every((selector) => $(selector).value !== ""),
    payment: Boolean($("#paymentMethod").value.trim() && $("#incoterm").value.trim()),
  };
  Object.entries(checks).forEach(([key, ready]) => {
    $(`[data-check="${key}"]`)?.classList.toggle("ready", ready);
  });
}

const visionFieldSelectors = [
  "#productDescription", "#composition", "#intendedUse", "#productCategory", "#brandModel",
  "#targetUser", "#declaredProductType",
  "#dimensions", "#labelText", "#dominantColors", "#constructionForm", "#componentsAccessories",
  "#functionMechanism", "#packaging", "#visibleFeatures", "#inferredFeatures",
  "#classificationQuestions", "#requiredUserInputs", "#productCondition",
];

function setVisionState(status, message, provider = "") {
  state.customsVisionStatus = status;
  const labels = {
    idle: "Bekliyor", ready: "Analize hazır", analysing: "Analiz ediliyor", review: "Kullanıcı kontrolü", confirmed: "Onaylandı", error: "Elle doldurun",
  };
  const panel = $("#attributeReview");
  if (panel) panel.hidden = !(state.customsImageData || state.customsDocumentPages) || ["idle", "ready"].includes(status);
  const badge = $("#visionState");
  badge.dataset.state = status;
  badge.textContent = labels[status] || status;
  $("#visionMessage").textContent = message;
  $("#visionProvider").textContent = provider || "Analiz sonucu ve güven düzeyi burada gösterilir.";
  setConfirmButtons(
    status === "confirmed" ? "Evsaflar onaylandı → adayları yeniden bul" : "Evsafları onayla → aday GTİP bul",
    status === "analysing",
  );
  updateVisionAnalyseButton(status);
  updateReadiness();
}

function setConfirmButtons(label, disabled) {
  // Onay düğmesi hem evsaf kutusunun üstünde hem altında duruyor; ikisi tek eylemdir.
  $$('[data-action="confirm-attributes"]').forEach((button) => {
    button.disabled = disabled;
    const span = button.querySelector("span");
    if (span) span.textContent = label;
  });
}

function setFlowStep(step) {
  // Şerit kullanıcının gerçekte nerede olduğunu gösterir; sabit "1. adım" yanıltıcıydı.
  const flow = $("#customsFlow");
  if (!flow) return;
  const current = Math.max(1, Math.min(4, Number(step) || 1));
  flow.dataset.step = String(current);
  [...flow.querySelectorAll("[data-flow-step]")].forEach((node) => {
    node.classList.toggle("active", Number(node.dataset.flowStep) <= current);
  });
}

function updateVisionAnalyseButton(status) {
  const button = $("#analyseImageButton");
  const route = $("#visionRoute");
  if (!button || !route) return;
  const hasImage = Boolean(state.customsImageData);
  button.hidden = !hasImage;
  route.hidden = !hasImage;
  button.disabled = status === "analysing";
  button.setAttribute("aria-busy", status === "analysing" ? "true" : "false");
  const labels = {
    ready: "Ürünü Analiz Et",
    analysing: "Fotoğraf Analiz Ediliyor…",
    review: "Fotoğrafı Yeniden Analiz Et",
    confirmed: "Fotoğrafı Yeniden Analiz Et",
    error: "Analizi Tekrar Dene",
  };
  button.querySelector("span").textContent = labels[status] || "Ürünü Analiz Et";
}

function joinLines(values) {
  return Array.isArray(values) ? values.filter(Boolean).join("\n") : "";
}

function setVisionValue(selector, value, { preserveWhenEmpty = false } = {}) {
  const input = $(selector);
  if (!input || (preserveWhenEmpty && !value)) return;
  input.value = value || "";
  input.classList.toggle("vision-filled", Boolean(value));
}

function parseClassificationQuestions(value) {
  const seen = new Set();
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-•*]|\d+[.)])\s*/, "").trim())
    .filter((question) => {
      const key = question.toLocaleLowerCase("tr-TR");
      if (question.length < 3 || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 12);
}

function collectClassificationAnswers() {
  return parseClassificationQuestions($("#classificationQuestions").value).flatMap((question, index) => {
    const answer = $(`[data-classification-answer="${index}"]`)?.value?.trim()
      || state.customsClassificationAnswers[question]?.trim()
      || "";
    if (!answer) return [];
    return [{ question, answer }];
  });
}

function updateClassificationAnswerProgress() {
  const questions = parseClassificationQuestions($("#classificationQuestions").value);
  const answered = collectClassificationAnswers().length;
  $("#classificationAnswerProgress").textContent = `${answered} / ${questions.length} cevap`;
}

function renderClassificationAnswerFields({ clear = false } = {}) {
  const panel = $("#classificationAnswerPanel");
  const list = $("#classificationAnswerList");
  const questions = parseClassificationQuestions($("#classificationQuestions").value);
  if (clear) state.customsClassificationAnswers = {};
  panel.hidden = questions.length === 0;
  if (!questions.length) {
    list.replaceChildren();
    updateClassificationAnswerProgress();
    return;
  }
  list.innerHTML = questions.map((question, index) => `<label class="classification-answer-row">
    <span><b>${index + 1}</b>${escapeHtml(question)}</span>
    <textarea data-classification-answer="${index}" maxlength="1000" placeholder="Cevabınızı buraya yazın; kullanıcı istediği zaman düzeltebilir.">${escapeHtml(state.customsClassificationAnswers[question] || "")}</textarea>
  </label>`).join("");
  updateClassificationAnswerProgress();
}

function applyVisionAttributes(data) {
  const description = data.product_description || [data.product_name, data.product_category].filter(Boolean).join(" — ");
  setVisionValue("#productDescription", description);
  setVisionValue("#composition", data.composition);
  setVisionValue("#intendedUse", data.intended_use);
  setVisionValue("#productCategory", data.product_category);
  setVisionValue("#brandModel", [data.visible_brand, data.visible_model].filter(Boolean).join(" / "));
  setVisionValue("#dimensions", data.dimensions);
  setVisionValue("#labelText", data.label_text);
  setVisionValue("#dominantColors", joinLines(data.dominant_colors).replaceAll("\n", ", "));
  setVisionValue("#constructionForm", data.construction_form);
  setVisionValue("#componentsAccessories", joinLines(data.components_accessories));
  setVisionValue("#functionMechanism", data.function_mechanism);
  setVisionValue("#packaging", data.packaging);
  setVisionValue("#visibleFeatures", joinLines(data.visible_features));
  setVisionValue("#inferredFeatures", joinLines(data.inferred_features));
  setVisionValue("#classificationQuestions", joinLines(data.classification_questions));
  renderClassificationAnswerFields({ clear: true });
  setVisionValue("#requiredUserInputs", joinLines(data.required_user_inputs));
  setVisionValue("#originCountry", data.visible_origin_country, { preserveWhenEmpty: true });
  if (["new", "used"].includes(data.condition)) setVisionValue("#productCondition", data.condition);
  if (!$("#customsQuestion").value.trim()) {
    $("#customsQuestion").value = "Bu ürünün aday GTİP'i, ithalat vergileri, TAREKS/TSE kontrolleri, gerekli belgeleri ve ek maliyetleri nelerdir?";
  }
  const fileStatus = $("#productFileStatus");
  fileStatus.dataset.state = "filled";
  fileStatus.textContent = `${data.product_name || "Ürün"} evsafları ürün dosyasına eklendi. Alanları kontrol edip gerekiyorsa düzeltin.`;
  updateReadiness();
}

function classificationRequestBody() {
  return {
    product_description: $("#productDescription").value.trim(),
    product_category: $("#productCategory").value.trim(),
    composition: $("#composition").value.trim(),
    intended_use: $("#intendedUse").value.trim(),
    target_user: $("#targetUser").value.trim(),
    declared_product_type: $("#declaredProductType").value.trim(),
    construction_form: $("#constructionForm").value.trim(),
    function_mechanism: $("#functionMechanism").value.trim(),
    components_accessories: $("#componentsAccessories").value.trim(),
    label_text: $("#labelText").value.trim(),
    visible_features: $("#visibleFeatures").value.trim(),
    inferred_features: $("#inferredFeatures").value.trim(),
    classification_questions: $("#classificationQuestions").value.trim(),
    classification_answers: collectClassificationAnswers(),
    decision_answers: { ...(state.decisionAnswers.precheck || {}) },
    origin_country: $("#originCountry").value.trim(),
  };
}

function candidateRates(candidate) {
  if (candidate.rate_status === "origin_required") {
    return '<em>Vergi oranı için menşe ülke girin</em>';
  }
  if (candidate.rate_status === "ambiguous") {
    const customsVariants = candidate.rate_variants?.customs_duty || [];
    const additionalVariants = candidate.rate_variants?.additional_duty || [];
    const parts = [];
    if (customsVariants.length) parts.push(`GV ${customsVariants.map((rate) => `%${numberFormat.format(rate)}`).join("/")}`);
    if (additionalVariants.length) parts.push(`İGV ${additionalVariants.map((rate) => `%${numberFormat.format(rate)}`).join("/")}`);
    return `<em>${escapeHtml(parts.join(" · ") || "Oran alt GTİP’e göre değişiyor")} · alt kod seçilmeli</em>`;
  }
  const parts = [];
  if (candidate.customs_duty_rate != null) parts.push(`<b>GV %${escapeHtml(numberFormat.format(candidate.customs_duty_rate))}</b>`);
  if (candidate.additional_duty_rate != null) parts.push(`<b>İGV %${escapeHtml(numberFormat.format(candidate.additional_duty_rate))}</b>`);
  if (candidate.additional_financial_liability_rate != null) parts.push(`<b>EMY %${escapeHtml(numberFormat.format(candidate.additional_financial_liability_rate))}</b>`);
  return parts.join("") || '<em>Bu seviyede tek oran doğrulanamadı</em>';
}

function renderGtipSuggestions(data) {
  const panel = $("#gtipSuggestions");
  const list = $("#gtipSuggestionList");
  panel.hidden = false;
  $("#gtipSuggestionStatus").textContent = data.candidates?.length
    ? `${data.candidates.length} aday · resmî tarife cetvelinde doğrulandı`
    : "Aday bulunamadı";
  if (!data.candidates?.length) {
    list.innerHTML = `<div class="answer-error"><p>${escapeHtml(data.summary || "Aday kod üretilemedi.")}</p></div>`;
    return;
  }
  list.innerHTML = data.candidates.map((candidate, index) => {
    const evidence = candidate.classification_evidence || [];
    const references = evidence.flatMap((item) => item.regulation_references || []).slice(0, 3);
    const evidenceText = evidence.length
      ? `${evidence.length} resmî AB sınıflandırma sayfası · ${references.join(" · ") || "kod eşleşmesi"}`
      : "Bu aday için ürün-özel AB karar eşleşmesi bulunamadı";
    return `<button class="gtip-candidate-button${candidate.code === $("#candidateGtip").value.replace(/\D/g, "") && state.customsGtipSelectionConfirmed ? " selected" : ""}" type="button" data-candidate-index="${index}">
      <code>${escapeHtml(candidate.code)}<small>${escapeHtml(candidate.level)} · ${escapeHtml(candidate.confidence_score ?? 0)}/100 kanıt puanı · ${escapeHtml(candidate.model_votes ?? 1)} model oyu · ${escapeHtml(candidate.matched_gtip_count)} alt GTİP</small></code>
      <span>${escapeHtml(candidate.explanation)}<small class="gtip-candidate-evidence">${escapeHtml(evidenceText)}</small></span>
      <span class="gtip-candidate-rates">${candidateRates(candidate)}</span>
    </button>`;
  }).join("");
  $$('[data-candidate-index]').forEach((button) => button.addEventListener("click", async () => {
    const candidate = data.candidates[Number(button.dataset.candidateIndex)];
    state.customsSelectedCandidate = candidate;
    setSelectedTariffCode(candidate.code, { exact: candidate.code.length === 12 });
    $$('[data-candidate-index]').forEach((item) => item.classList.toggle("selected", item === button));
    showToast(`${candidate.code} seçildi; şimdi resmî Türk tarife alt dalları açılıyor.`);
    try {
      await loadTariffTree(candidate.code, candidate);
    } catch (error) {
      renderTariffTreeError(error.message || "Türk GTİP alt dalları getirilemedi.");
    }
  }));
}

function tariffLevelLabel(level) {
  return { HS6: "HS6", CN8: "CN8", TR10: "Türkiye 10", GTIP12: "GTİP12" }[level] || level || "Tarife";
}

function treeNodeRates(node) {
  if (node.rate_status === "origin_required") return "Menşe girildiğinde ortak oranlar doğrulanır";
  const safe = node.unambiguous_rates || {};
  const parts = [];
  if (safe.customs_duty != null) parts.push(`GV %${numberFormat.format(safe.customs_duty)}`);
  if (safe.additional_duty != null) parts.push(`İGV %${numberFormat.format(safe.additional_duty)}`);
  if (safe.additional_financial_liability != null) parts.push(`EMY %${numberFormat.format(safe.additional_financial_liability)}`);
  if (node.rate_status === "unambiguous" && parts.length) return `${parts.join(" · ")} · bütün alt satırlarda ortak`;
  const variants = Object.values(node.rate_variants || {}).flat();
  return variants.length ? `Alt satırlara göre değişiyor: ${[...new Set(variants)].map((rate) => `%${numberFormat.format(rate)}`).join(" / ")}` : "Bu dalda tek oran doğrulanamadı";
}

function setSelectedTariffCode(code, { exact = false } = {}) {
  const normalised = String(code || "").replace(/\D/g, "");
  state.customsApplyingTariffSelection = true;
  $("#candidateGtip").value = normalised;
  state.customsGtipSelectionConfirmed = [6, 8, 10, 12].includes(normalised.length);
  state.customsExactGtipConfirmed = exact && normalised.length === 12;
  if (state.customsExactGtipConfirmed) setFlowStep(4);
  state.customsAutoGtip = null;
  $("#candidateGtip").dispatchEvent(new Event("input", { bubbles: true }));
  state.customsApplyingTariffSelection = false;
  updateReadiness();
}

function tariffPathCodes(code) {
  const digits = String(code || "").replace(/\D/g, "");
  return [6, 8, 10, 12].filter((length) => length <= digits.length).map((length) => digits.slice(0, length));
}

function renderTariffTreeError(message) {
  const panel = $("#tariffTree");
  panel.hidden = false;
  $("#tariffTreeStatus").textContent = "Alt dallar alınamadı";
  $("#tariffTreeList").innerHTML = `<div class="answer-error"><p>${escapeHtml(message)}</p></div>`;
}

function renderTariffTree(tree, candidate = state.customsSelectedCandidate) {
  const panel = $("#tariffTree");
  const list = $("#tariffTreeList");
  const path = $("#tariffTreePath");
  const questions = $("#tariffTreeQuestions");
  panel.hidden = false;
  state.customsTariffTree = tree;
  path.innerHTML = tariffPathCodes(tree.prefix).map((code, index, values) => `<button type="button" data-tariff-path="${escapeHtml(code)}"${index === values.length - 1 ? ' aria-current="step"' : ""}>${escapeHtml(code)}</button>`).join('<span aria-hidden="true">›</span>');
  const decisionQuestions = [...new Set([
    ...(candidate?.decisive_missing_information || []),
    ...(state.customsClassificationResult?.missing_information || []),
  ].filter(Boolean))].slice(0, 8);
  questions.hidden = decisionQuestions.length === 0;
  questions.innerHTML = decisionQuestions.length
    ? `<b>Alt kodu seçmeden önce doğrulayın</b><ul>${decisionQuestions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : "";

  if (tree.status !== "matched") {
    $("#tariffTreeStatus").textContent = "Resmî tarife eşleşmesi yok";
    list.innerHTML = '<div class="answer-error"><p>Bu kod aktif resmî tarife tablolarında doğrulanamadı.</p></div>';
  } else if (tree.exact_gtip_selected) {
    state.customsExactGtipConfirmed = true;
    state.customsGtipSelectionConfirmed = true;
    $("#tariffTreeStatus").textContent = "12 haneli satır doğrulandı";
    list.innerHTML = `<div class="tariff-tree-final"><b>${escapeHtml(tree.prefix)}</b><span>Aktif resmî tarife tablolarında bulunan GTİP12 satırı</span></div>`;
  } else {
    state.customsExactGtipConfirmed = false;
    $("#tariffTreeStatus").textContent = `${tree.total_children} ${tariffLevelLabel(tree.next_level)} dalı · kullanıcı seçimi gerekli`;
    list.innerHTML = tree.children.map((node) => `<button type="button" class="tariff-tree-node" data-tariff-child="${escapeHtml(node.code)}" data-tariff-final="${node.final ? "true" : "false"}">
      <code>${escapeHtml(node.code)}<small>${tariffLevelLabel(node.level)} · ${escapeHtml(node.descendant_count)} GTİP12 satırı</small></code>
      <span>${escapeHtml(treeNodeRates(node))}</span>
      <b>${node.final ? "Bu GTİP12’yi doğrula" : "Alt dalları aç"}</b>
    </button>`).join("");
  }
  $("#tariffTreeNotice").textContent = tree.exact_gtip_selected
    ? "Kodun resmî tabloda bulunması, ürünün bu kodda sınıflandırıldığını tek başına kanıtlamaz. Evsaf ve gerekçe kullanıcı tarafından doğrulanmıştır."
    : "Sistem alt dalı otomatik seçmez. 6/8/10 haneli kodlarda yalnız bütün alt satırlarda ortak olan oranlar güvenle gösterilebilir; TAREKS kapsamı için GTİP12 gerekir.";
  $$('[data-tariff-path]').forEach((button) => button.addEventListener("click", () => loadTariffTree(button.dataset.tariffPath, candidate).catch((error) => renderTariffTreeError(error.message))));
  $$('[data-tariff-child]').forEach((button) => button.addEventListener("click", async () => {
    const code = button.dataset.tariffChild;
    const exact = button.dataset.tariffFinal === "true";
    setSelectedTariffCode(code, { exact });
    if (exact) prefillVerifiedRates(code);
    await loadTariffTree(code, candidate).catch((error) => renderTariffTreeError(error.message));
    showToast(exact ? `${code} GTİP12 adayı kullanıcı seçimiyle doğrulandı.` : `${code} dalı seçildi; bir alt seviye açıldı.`);
  }));

async function prefillVerifiedRates(code) {
  const origin = $("#originCountry").value.trim();
  if (!origin) return;
  try {
    const tariff = await fetchJson("/api/tariff/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gtip: code, origin_country: origin }),
    });
    const safe = tariff.unambiguous_rates || {};
    const safeAdditionalDuty = safe.additional_duty != null ? safe.additional_duty : (
      !tariff.ambiguous_measure_types?.includes("additional_duty") && tariff.measure_coverage?.additional_duty?.status === "verified_snapshot" ? 0 : null
    );
    let filled = 0;
    [
      ["#customsDutyRate", safe.customs_duty],
      ["#additionalDutyRate", safeAdditionalDuty],
      ["#additionalFinancialLiabilityRate", safe.additional_financial_liability],
    ].forEach(([selector, value]) => {
      const input = $(selector);
      if (value == null || !input || input.value.trim() !== "") return;
      input.value = String(value);
      input.classList.add("rate-suggested");
      input.title = "Resmî tarife satırından önerildi; onaylamadan veya düzeltmeden maliyet hesabı yapılmaz.";
      filled += 1;
    });
    if (filled) showToast("Vergi oranları resmî satırdan önerildi; doğrulayıp onaylayın.");
  } catch (error) { /* oran önerisi başarısız olsa da kod seçimi geçerli kalır */ }
}
  updateReadiness();
}

async function loadTariffTree(code, candidate = state.customsSelectedCandidate) {
  const panel = $("#tariffTree");
  panel.hidden = false;
  $("#tariffTreeStatus").textContent = "Resmî alt dallar hazırlanıyor…";
  $("#tariffTreeList").innerHTML = '<div class="analysis-loading"><i></i><div><b>Türk tarife ağacı taranıyor</b><span>Hiçbir alt kod otomatik seçilmiyor…</span></div></div>';
  const tree = await fetchJson("/api/tariff/tree", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gtip: code, origin_country: $("#originCountry").value.trim() }),
  });
  renderTariffTree(tree, candidate);
  return tree;
}

function resetGtipSuggestions({ clearAutoCode = false } = {}) {
  if (clearAutoCode && state.customsAutoGtip && $("#candidateGtip").value.replace(/\D/g, "") === state.customsAutoGtip) {
    $("#candidateGtip").value = "";
  }
  state.customsClassificationResult = null;
  state.customsAutoGtip = null;
  state.customsGtipSelectionConfirmed = false;
  state.customsExactGtipConfirmed = false;
  state.customsTariffTree = null;
  state.customsSelectedCandidate = null;
  $("#gtipSuggestions").hidden = true;
  $("#gtipSuggestionList").innerHTML = "";
  $("#gtipSuggestionStatus").textContent = "Evsaf onayından sonra hazırlanır.";
  $("#tariffTree").hidden = true;
  $("#tariffTreeList").innerHTML = "";
  $("#tariffTreePath").innerHTML = "";
  $("#tariffTreeQuestions").innerHTML = "";
  updateReadiness();
}

async function classifyApprovedProduct() {
  const panel = $("#gtipSuggestions");
  panel.hidden = false;
  $("#gtipSuggestionStatus").textContent = "Aday kodlar ve oranlar hazırlanıyor…";
  $("#gtipSuggestionList").innerHTML = '<div class="analysis-loading"><i></i><div><b>En yakın üç tarife aranıyor</b><span>Model adayları resmî tarife cetvelinde doğrulanıyor…</span></div></div>';
  const data = await fetchJson("/api/customs/classify-product", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(classificationRequestBody()),
  });
  state.customsClassificationResult = data;
  state.customsGtipSelectionConfirmed = false;
  state.customsExactGtipConfirmed = false;
  state.customsSelectedCandidate = null;
  $("#candidateGtip").value = "";
  $("#tariffTree").hidden = true;
  renderGtipSuggestions(data);
  return data;
}

async function refreshCandidateRates() {
  const data = state.customsClassificationResult;
  const origin = $("#originCountry").value.trim();
  if (!data?.candidates?.length || !origin) return;
  const updated = await Promise.all(data.candidates.map(async (candidate) => {
    const tariff = await fetchJson("/api/tariff/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gtip: candidate.code, origin_country: origin, dispatch_country: $("#dispatchCountry")?.value.trim() || null, atr_certificate: $("#assistAtr")?.value || null }),
    });
    const safe = tariff.unambiguous_rates || {};
    return {
      ...candidate,
      customs_duty_rate: safe.customs_duty ?? null,
      additional_duty_rate: safe.additional_duty ?? null,
      additional_financial_liability_rate: safe.additional_financial_liability ?? null,
      rate_variants: tariff.rate_variants || {},
      rate_status: tariff.ambiguous_measure_types?.length || safe.customs_duty == null ? "ambiguous" : "unambiguous",
    };
  }));
  state.customsClassificationResult = { ...data, candidates: updated };
  renderGtipSuggestions(state.customsClassificationResult);
}

async function analyseProductImage() {
  if (!state.customsImageData) return;
  setVisionState(
    "analysing",
    "Görsel yalnızca ürünün görünür evsaflarına çevriliyor; genellikle 10-40 saniye sürer. Bu aşamada GTİP, vergi veya TAREKS sorgusu yapılmaz.",
    "Yapay zekâ görsel analizi sürüyor…",
  );
  try {
    const data = await fetchJson("/api/customs/describe-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_data_url: state.customsImageData }),
      timeoutMs: AI_ANALYSIS_TIMEOUT_MS,
    });
    state.customsVisionResult = data;
    applyVisionAttributes(data);
    setVisionState(
      "review",
      `${data.warning} Alanları düzeltin; araştırma ancak onayınızdan sonra başlar.`,
      `Yapay zekâ görsel analizi · güven: ${confidenceLabel(data.confidence)}`,
    );
    // Analiz bitince kullanicinin gozu siradaki eyleme dusmeli; eskiden durum satirina
    // kayiyordu ve onay dugmesi uzun evsaf listesinin altinda kaliyordu.
    setFlowStep(2);
    ($("#attributeNextStep") || $("#productFileStatus")).scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    state.customsVisionResult = null;
    // Gercek sebep ust satirda gorunmeli: "kotaniz doldu" ile "model yanit vermedi"
    // birbirinden ayirt edilemezse kullanici da, hata arayan da yanlis yere bakar.
    const reason = describeRequestError(error, "Görsel analizi tamamlanamadı.");
    const status = $("#productFileStatus");
    status.dataset.state = "error";
    if (error && error.code === "quota_exceeded") {
      // Kota dolmasi satin alinabilir bir sinirdir: cozumu ayni yerde gosteriyoruz.
      status.innerHTML = `${escapeHtml(reason)} <button type="button" class="secondary-action" id="quotaUpgrade">Paketleri gör</button>`;
      $("#quotaUpgrade")?.addEventListener("click", () => openAccount("plans"));
    } else {
      status.textContent = reason;
    }
    setVisionState("error", `${reason} Ürün evsaflarını elle doldurup yine de açıkça onaylayabilirsiniz.`);
  }
}

async function setProductImage(file) {
  const allowed = ["image/jpeg", "image/png", "image/webp"];
  if (!file) {
    state.customsImageData = null;
    state.customsVisionResult = null;
    state.customsClassificationAnswers = {};
    renderClassificationAnswerFields({ clear: true });
    state.customsVisionStatus = "idle";
    resetGtipSuggestions({ clearAutoCode: true });
    $("#imagePreview").hidden = true;
    $("#uploadZone").classList.remove("has-image");
    $("#attributeReview").hidden = true;
    $("#uploadTitle").textContent = "Ürün fotoğrafı ekle";
    $("#uploadHint").textContent = "JPEG, PNG veya WebP · en az 80×80 piksel · en fazla 8 MB";
    $("#productFileStatus").dataset.state = "waiting";
    $("#productFileStatus").textContent = "Görsel analizi tamamlandığında temel evsaflar önce bu ürün dosyasına işlenir.";
    updateVisionAnalyseButton("idle");
    updateReadiness();
    return;
  }
  if (!allowed.includes(file.type) || file.size > 8 * 1024 * 1024) {
    $("#productImage").value = "";
    showToast("JPEG, PNG veya WebP biçiminde en fazla 8 MB görsel yükleyin.");
    return;
  }
  state.customsImageData = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  $("#imagePreview").src = state.customsImageData;
  $("#imagePreview").hidden = false;
  $("#uploadZone").classList.add("has-image");
  $("#uploadTitle").textContent = file.name;
  $("#uploadHint").textContent = `${numberFormat.format(Math.ceil(file.size / 1024))} KB · analiz siz başlatmadan gönderilmez`;
  state.customsVisionResult = null;
  state.customsClassificationAnswers = {};
  renderClassificationAnswerFields({ clear: true });
  resetGtipSuggestions({ clearAutoCode: true });
  $("#productFileStatus").dataset.state = "waiting";
  $("#productFileStatus").textContent = "Fotoğraf hazır. Analiz sonucu önce bu ürün dosyasına işlenecek.";
  setVisionState(
    "ready",
    "Fotoğraf hazır. Ürünü Analiz Et düğmesine bastığınızda yalnızca görünür evsaflar çıkarılır.",
  );
}

function customsRequestBody() {
  return {
    question: $("#customsQuestion").value.trim(),
    product_description: $("#productDescription").value.trim(),
    candidate_gtip: $("#candidateGtip").value.trim() || null,
    tariff_selection_confirmed: state.customsGtipSelectionConfirmed,
    exact_gtip_confirmed: state.customsExactGtipConfirmed,
    classification_verification_status: state.customsClassificationResult?.verification_status || null,
    classification_confidence_score: state.customsSelectedCandidate?.confidence_score ?? null,
    classification_models: (state.customsClassificationResult?.models || []).slice(0, 3),
    origin_country: $("#originCountry").value.trim() || null,
    dispatch_country: $("#dispatchCountry").value.trim() || null,
    atr_certificate: $("#assistAtr")?.value || null,
    intended_use: $("#intendedUse").value.trim() || null,
    target_user: $("#targetUser").value.trim() || null,
    declared_product_type: $("#declaredProductType").value.trim() || null,
    composition: $("#composition").value.trim() || null,
    product_category: $("#productCategory").value.trim() || null,
    brand_model: $("#brandModel").value.trim() || null,
    dimensions: $("#dimensions").value.trim() || null,
    label_text: $("#labelText").value.trim() || null,
    dominant_colors: $("#dominantColors").value.trim() || null,
    construction_form: $("#constructionForm").value.trim() || null,
    components_accessories: $("#componentsAccessories").value.trim() || null,
    function_mechanism: $("#functionMechanism").value.trim() || null,
    packaging: $("#packaging").value.trim() || null,
    visible_features: $("#visibleFeatures").value.trim() || null,
    inferred_features: $("#inferredFeatures").value.trim() || null,
    classification_questions: $("#classificationQuestions").value.trim() || null,
    classification_answers: collectClassificationAnswers(),
    required_user_inputs: $("#requiredUserInputs").value.trim() || null,
    condition: $("#productCondition").value,
    invoice_value: nullableNumber("#invoiceValue"),
    freight: nullableNumber("#freight"),
    insurance: nullableNumber("#insurance"),
    other_pre_import_costs: nullableNumber("#otherCosts"),
    quantity: nullableNumber("#quantity"),
    currency: $("#currency").value,
    incoterm: $("#incoterm").value.trim() || null,
    payment_method: $("#paymentMethod").value.trim() || null,
    customs_duty_rate: nullableNumber("#customsDutyRate"),
    additional_duty_rate: nullableNumber("#additionalDutyRate"),
    additional_financial_liability_rate: nullableNumber("#additionalFinancialLiabilityRate"),
    anti_dumping_amount: nullableNumber("#antiDumpingAmount"),
    kkdf_rate: nullableNumber("#kkdfRate"),
    vat_rate: nullableNumber("#vatRate"),
    sct_amount: nullableNumber("#sctAmount"),
    surveillance_unit_value: nullableNumber("#surveillanceUnitValue"),
    has_surveillance_certificate: $("#hasSurveillanceCertificate").value === "" ? null : $("#hasSurveillanceCertificate").value === "true",
    ...liraFields("assist"),
  };
}

function liraFields(prefix) {
  return {
    trt_bandrol_rate: nullableNumber(`#${prefix}TrtBandrolRate`),
    exchange_rate: nullableNumber(`#${prefix}ExchangeRate`),
    exchange_rate_date: $(`#${prefix}ExchangeRateDate`)?.value || null,
    stamp_duty_try: nullableNumber(`#${prefix}StampDutyTry`),
    port_storage_try: nullableNumber(`#${prefix}PortStorageTry`),
    gekap_try: nullableNumber(`#${prefix}GekapTry`),
  };
}

$("#productImage").addEventListener("change", (event) => setProductImage(event.target.files?.[0]).catch(() => showToast("Görsel okunamadı.")));
$("#analyseImageButton").addEventListener("click", analyseProductImage);
const uploadZone = $("#uploadZone");
["dragenter", "dragover"].forEach((name) => uploadZone.addEventListener(name, (event) => { event.preventDefault(); uploadZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => uploadZone.addEventListener(name, (event) => { event.preventDefault(); uploadZone.classList.remove("dragging"); }));
uploadZone.addEventListener("drop", (event) => setProductImage(event.dataTransfer.files?.[0]).catch(() => showToast("Görsel okunamadı.")));
$$('#customsForm input, #customsForm textarea, #customsForm select').forEach((input) => input.addEventListener("input", updateReadiness));
visionFieldSelectors.forEach((selector) => $(selector)?.addEventListener("input", () => {
  if (state.customsImageData && state.customsVisionStatus === "confirmed") {
    resetGtipSuggestions({ clearAutoCode: true });
    setVisionState(
      "review",
      "Onaydan sonra evsaf değişti. Güncel alanları yeniden onaylamadan resmî araştırma başlatılamaz.",
      state.customsVisionResult ? "Yapay zekâ görsel analizi · elle düzenlendi" : "Elle düzenlenen evsaf",
    );
  }
}));

$("#classificationQuestions").addEventListener("input", () => renderClassificationAnswerFields());
$("#classificationAnswerList").addEventListener("input", (event) => {
  const input = event.target.closest("[data-classification-answer]");
  if (!input) return;
  const questions = parseClassificationQuestions($("#classificationQuestions").value);
  const question = questions[Number(input.dataset.classificationAnswer)];
  if (question) state.customsClassificationAnswers[question] = input.value;
  updateClassificationAnswerProgress();
  updateReadiness();
  if (state.customsImageData && state.customsVisionStatus === "confirmed") {
    resetGtipSuggestions({ clearAutoCode: true });
    setVisionState("review", "Eksik bilgi cevabı değişti. Güncel cevaplarla aday GTİP'i yeniden bulun.", "Kullanıcı tarafından tamamlanan evsaf");
  }
});
$("#goToCostFields").addEventListener("click", () => {
  $("#shipmentDetails").open = true;
  $("#costDetails").open = true;
  $("#costDetails").scrollIntoView({ behavior: "smooth", block: "start" });
  window.setTimeout(() => $("#invoiceValue").focus({ preventScroll: true }), 450);
});

let originRateTimer = null;
$("#originCountry").addEventListener("input", () => {
  window.clearTimeout(originRateTimer);
  if (!state.customsClassificationResult?.candidates?.length) return;
  if (!$("#originCountry").value.trim()) {
    state.customsClassificationResult.candidates = state.customsClassificationResult.candidates.map((candidate) => ({
      ...candidate,
      customs_duty_rate: null,
      additional_duty_rate: null,
      additional_financial_liability_rate: null,
      rate_status: "origin_required",
    }));
    renderGtipSuggestions(state.customsClassificationResult);
    return;
  }
  const selectedCode = $("#candidateGtip").value.replace(/\D/g, "");
  originRateTimer = window.setTimeout(async () => {
    try {
      await refreshCandidateRates();
      if (state.customsGtipSelectionConfirmed && [6, 8, 10, 12].includes(selectedCode.length)) {
        await loadTariffTree(selectedCode);
      }
    } catch (error) {
      showToast(error.message || "Aday oranları yenilenemedi.");
    }
  }, 650);
});

let manualTariffTimer = null;
$("#candidateGtip").addEventListener("input", () => {
  const current = $("#candidateGtip").value.replace(/\D/g, "");
  if (state.customsAutoGtip && current !== state.customsAutoGtip) state.customsAutoGtip = null;
  if (!state.customsApplyingTariffSelection) {
    state.customsGtipSelectionConfirmed = false;
    state.customsExactGtipConfirmed = false;
    state.customsSelectedCandidate = state.customsClassificationResult?.candidates?.find((candidate) => candidate.code === current) || null;
    window.clearTimeout(manualTariffTimer);
    if ([4, 6, 8, 10, 12].includes(current.length)) {
      manualTariffTimer = window.setTimeout(async () => {
        try {
          const tree = await loadTariffTree(current, state.customsSelectedCandidate);
          if (tree.status === "matched") {
            state.customsGtipSelectionConfirmed = true;
            state.customsExactGtipConfirmed = tree.exact_gtip_selected;
            updateReadiness();
            if (state.customsPendingSubmit) {
              state.customsPendingSubmit = false;
              showToast(`${current} doğrulandı; analiz başlatılıyor.`);
              $("#customsForm").requestSubmit();
            } else {
              showToast(`${current} aktif resmî tarife ağacında doğrulandı.`);
            }
          } else if (state.customsPendingSubmit) {
            state.customsPendingSubmit = false;
            showToast("Kod resmî tarife ağacında bulunamadı; analiz başlatılmadı.");
          }
        } catch (error) {
          state.customsPendingSubmit = false;
          renderTariffTreeError(error.message || "Elle girilen kod doğrulanamadı.");
        }
      }, 650);
    } else {
      $("#tariffTree").hidden = true;
    }
  }
  $$('[data-candidate-index]').forEach((button) => {
    const candidate = state.customsClassificationResult?.candidates?.[Number(button.dataset.candidateIndex)];
    button.classList.toggle("selected", state.customsGtipSelectionConfirmed && candidate?.code === current);
  });
  updateReadiness();
});

async function confirmAttributesAndFindCandidates() {
  if (state.customsVisionStatus === "analysing") return;
  if ($("#productDescription").value.trim().length < 12) {
    showToast("Onaylamadan önce teknik ürün tanımını tamamlayın.");
    $("#productDescription").focus();
    return;
  }
  setVisionState(
    "confirmed",
    "Evsaflar onaylandı. En yakın üç HS/CN adayı resmî tarife cetvelinde doğrulanıyor.",
    state.customsVisionResult
      ? "Yapay zekâ görsel analizi · kullanıcı onaylı"
      : "Elle girilen evsaf · kullanıcı onaylı",
  );
  setConfirmButtons("Aday GTİP’ler bulunuyor…", true);
  try {
    const classification = await classifyApprovedProduct();
    if (classification.candidates?.length) {
      setVisionState(
        "confirmed",
        `${classification.candidates.length} aday kod bulundu. Sistem hiçbirini otomatik seçmedi; bir adayı seçerek Türk GTİP12 alt dallarını açın.`,
        state.customsVisionResult
          ? "Yapay zekâ görsel analizi · aday kodlar resmî tarife cetvelinde doğrulandı"
          : "Elle girilen evsaf · aday kodlar resmî tarife cetvelinde doğrulandı",
      );
      showToast(`${classification.candidates.length} aday tarife kodu bulundu.`);
      setFlowStep(3);
      $("#gtipSuggestions").scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      setVisionState("confirmed", classification.summary, "Yapay zekâ sınıflandırması · aday kod bulunamadı");
      showToast("Aday kod için ürün evsafı yetersiz kaldı.");
    }
  } catch (error) {
    $("#gtipSuggestions").hidden = false;
    $("#gtipSuggestionStatus").textContent = "Adaylar alınamadı";
    $("#gtipSuggestionList").innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message || "Aday kodlar üretilemedi.")}</p></div>`;
    setVisionState("confirmed", "Evsaflar onaylandı ancak aday GTİP üretilemedi. Tekrar deneyebilir veya kodu elle girebilirsiniz.");
  } finally {
    setConfirmButtons("Evsaflar onaylandı → adayları yeniden bul", false);
  }
}

$$('[data-action="confirm-attributes"]').forEach((button) => {
  button.addEventListener("click", confirmAttributesAndFindCandidates);
});

$("#customsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.customsImageData && state.customsVisionStatus !== "confirmed") {
    if (["idle", "ready"].includes(state.customsVisionStatus)) {
      showToast("Önce Ürünü Analiz Et düğmesine basın.");
      $("#analyseImageButton").scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      showToast("Önce fotoğraftan çıkarılan evsafları kontrol edip onaylayın.");
      $("#attributeReview").scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return;
  }
  const selectedTariffCode = $("#candidateGtip").value.replace(/\D/g, "");
  if (state.customsClassificationResult?.candidates?.length && !state.customsGtipSelectionConfirmed) {
    showToast("Önce bir tarife adayını veya doğrulanmış alt dalı seçin.");
    $("#gtipSuggestions").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (selectedTariffCode && ![4, 6, 8, 10, 12].includes(selectedTariffCode.length)) {
    showToast("Tarife kodu 4, 6, 8, 10 veya 12 haneli olmalıdır.");
    $("#candidateGtip").focus();
    return;
  }
  if (selectedTariffCode && !state.customsGtipSelectionConfirmed) {
    window.clearTimeout(manualTariffTimer);
    showToast("Kod resmî tarife ağacında doğrulanıyor…");
    try {
      const tree = await loadTariffTree(selectedTariffCode, state.customsSelectedCandidate);
      if (tree && (tree.status === "matched" || tree.status === "partial")) {
        state.customsGtipSelectionConfirmed = true;
        state.customsExactGtipConfirmed = Boolean(tree.exact_gtip_selected);
        updateReadiness();
      } else {
        showToast("Kod resmî tarife ağacında bulunamadı; lütfen geçerli bir GTİP girin veya aday seçin.");
        return;
      }
    } catch (error) {
      showToast(error.message || "Tarife doğrulanamadı.");
      return;
    }
  }
  state.customsPendingSubmit = false;
  if (selectedTariffCode && selectedTariffCode.length < 12) {
    showToast("Üst tarife koduyla yalnız ortak oranlar gösterilir; TAREKS kapsamı GTİP12 seçilmeden kesinleşmez.");
  }
  const button = $("#analyseButton");
  const result = $("#customsResult");
  const loading = $("#analysisLoading");
  result.hidden = false;
  loading.hidden = false;
  $("#customsOutput").innerHTML = "";
  button.disabled = true;
  button.querySelector("span").textContent = "Resmî kaynaklar taranıyor…";
  result.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const data = await fetchJson("/api/customs/precheck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(customsRequestBody()),
    });
    renderCustomsResult(data);
  } catch (error) {
    $("#customsOutput").innerHTML = `<div class="answer-error"><h2>Ön değerlendirme tamamlanamadı</h2><p>${escapeHtml(error.message || "Lütfen daha sonra yeniden deneyin.")}</p></div>`;
  } finally {
    loading.hidden = true;
    button.disabled = false;
    button.querySelector("span").textContent = "GTİP bul · vergi ve TAREKS'i araştır";
  }
});

function formatUnixDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "short" }).format(new Date(Number(value) * 1000));
}

const consultationStatusLabels = {
  sent: "Yanıt bekliyor", accepted: "Danışman kabul etti", declined: "Danışman kabul etmedi", closed: "Görüşme kapandı",
};

function consultantCard(item) {
  const canSend = Boolean(state.currentCustomsResult);
  return `<article class="consultant-card">
    <header><span class="consultant-monogram">${escapeHtml((item.display_name || "D").slice(0, 1).toUpperCase())}</span><div><h3>${escapeHtml(item.display_name)}</h3><p>${escapeHtml(item.title)}</p></div><b>ONAYLI PROFİL</b></header>
    <p class="consultant-bio-text">${escapeHtml(item.bio)}</p>
    <div class="consultant-meta"><span>${escapeHtml(item.city || "Konum belirtilmedi")}</span><span>${item.service_mode === "hybrid" ? "Çevrim içi + yüz yüze" : "Çevrim içi"}</span><span>${escapeHtml(item.experience_years)} yıl deneyim</span></div>
    <ul>${(item.expertise || []).map((entry) => `<li>${escapeHtml(entry)}</li>`).join("")}</ul>
    <footer><small>Mevzuat danışmanlığı · fiilî gümrük işlemi değildir</small><button type="button" data-send-consultant="${escapeHtml(item.id)}" ${canSend ? "" : "disabled"}>${canSend ? "Analiz dosyasını gönder" : "Önce ürün analizi oluştur"}</button></footer>
  </article>`;
}

async function loadConsultants() {
  const target = $("#consultantGrid");
  target.innerHTML = "<p>Danışmanlar yükleniyor…</p>";
  try {
    const publicData = await fetchJson("/api/consultants");
    state.consultants = publicData.items || [];
    const marketplaceEnabled = publicData.enabled !== false;
    state.consultantsMarketplace = marketplaceEnabled;
    document.body.classList.toggle("marketplace-enabled", marketplaceEnabled);
    target.innerHTML = publicData.items.length
      ? publicData.items.map(consultantCard).join("")
      : '<div class="consultant-empty"><b>İlk danışman başvuruları bekleniyor</b><p>Ücretsiz başvuru yapabilir; yönetici incelemesinden sonra bu dizinde yer alabilirsiniz.</p></div>';
    if (state.auth?.authenticated) {
      const [profileData] = await Promise.all([
        fetchJson("/api/consultants/me"), loadConsultationRequests(),
      ]);
      if (profileData.profile) fillConsultantApplication(profileData.profile);
    } else {
      $("#consultationRequestList").innerHTML = '<p class="consultant-login-note">Taleplerinizi görmek veya danışman olmak için <a href="/auth/google">Google ile giriş yapın</a>.</p>';
    }
  } catch (error) {
    target.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function fillConsultantApplication(profile) {
  $("#consultantName").value = profile.display_name || "";
  $("#consultantTitle").value = profile.title || "";
  $("#consultantCity").value = profile.city || "";
  $("#consultantMode").value = profile.service_mode || "online";
  $("#consultantExperience").value = profile.experience_years ?? 0;
  $("#consultantBio").value = profile.bio || "";
  $$('[name="consultantExpertise"]').forEach((input) => { input.checked = (profile.expertise || []).includes(input.value); });
  const labels = { pending: "Başvurunuz yönetici incelemesinde.", active: "Profiliniz yayında. Değişiklik yaparsanız yeniden incelemeye alınır.", suspended: "Profiliniz yayında değil; yönetici incelemesi gerekiyor." };
  $("#consultantApplicationStatus").textContent = labels[profile.status] || "";
  $("#consultantApplicationStatus").dataset.status = profile.status || "pending";
}

function requestCard(item) {
  const packet = item.packet || {};
  const party = item.direction === "incoming" ? "Başvuru sahibi" : item.consultant_name;
  const sourceLinks = (packet.official_source_urls || []).slice(0, 12).map((url, index) => `<a href="${safeUrl(url)}" target="_blank" rel="noreferrer">Resmî kaynak ${index + 1} ↗</a>`).join("");
  const messages = (item.messages || []).map((message) => `<div class="consultation-message${message.mine ? " mine" : ""}"><b>${message.mine ? "Siz" : "Karşı taraf"}</b><p>${escapeHtml(message.body)}</p><time>${escapeHtml(formatUnixDate(message.created_at))}</time></div>`).join("");
  const mayReply = item.status === "accepted";
  const statusActions = item.direction === "incoming" && item.status === "sent"
    ? `<button type="button" data-request-status="accepted">Talebi kabul et</button><button type="button" data-request-status="declined" class="decline">Kabul etme</button>`
    : !["declined", "closed"].includes(item.status) ? '<button type="button" data-request-status="closed" class="decline">Görüşmeyi kapat</button>' : "";
  return `<details class="consultation-request" data-request-id="${escapeHtml(item.id)}">
    <summary><div><span>${item.direction === "incoming" ? "GELEN" : "GÖNDERİLEN"}</span><b>${escapeHtml(item.subject)}</b><small>${escapeHtml(party || "Kullanıcı")} · ${escapeHtml(formatUnixDate(item.created_at))}</small></div><em data-status="${escapeHtml(item.status)}">${escapeHtml(consultationStatusLabels[item.status] || item.status)}</em></summary>
    <div class="consultation-request-body"><p class="initial-message">${escapeHtml(item.message)}</p>
      <div class="shared-packet"><b>Paylaşılan analiz özeti</b><p>${escapeHtml(packet.summary || "Özet bulunmuyor.")}</p><code>${escapeHtml(packet.inquiry?.candidate_gtip || "GTİP belirsiz")}</code><span>${escapeHtml(packet.inquiry?.origin_country || "Menşe belirtilmedi")}</span><nav>${sourceLinks || "Resmî kaynak URL’si bulunmuyor."}</nav></div>
      <div class="consultation-thread">${messages || "<p>Henüz ek mesaj yok.</p>"}</div>
      ${mayReply ? `<form class="consultation-reply"><label class="field"><span>Görüşme mesajı</span><textarea required minlength="2" maxlength="2000" placeholder="Yanıtınızı veya ek sorunuzu yazın."></textarea></label><button type="submit">Mesajı gönder</button></form>` : item.status === "sent" ? '<p class="reply-lock">Mesajlaşma, danışman talebi kabul ettikten sonra açılır.</p>' : ""}
      <div class="consultation-status-actions">${statusActions}</div>
    </div>
  </details>`;
}

function consultationAttentionCount(data) {
  const needsMe = (item) => {
    if (item.direction === "incoming" && item.status === "sent") return true;
    if (item.status !== "accepted") return false;
    const last = (item.messages || []).at(-1);
    return Boolean(last && !last.mine);
  };
  return [...(data.incoming || []), ...(data.outgoing || [])].filter(needsMe).length;
}

function renderConsultationBadge(count) {
  state.consultationBadge = count;
  const tab = $('[data-customs-view="consultants"]');
  if (!tab) return;
  let badge = tab.querySelector(".tab-badge");
  if (!count) { badge?.remove(); return; }
  if (!badge) { badge = document.createElement("i"); badge.className = "tab-badge"; tab.append(badge); }
  badge.textContent = String(count);
  badge.setAttribute("aria-label", `${count} bekleyen danışmanlık talebi`);
}

async function refreshConsultationBadge() {
  if (!state.auth?.authenticated) return renderConsultationBadge(0);
  try { renderConsultationBadge(consultationAttentionCount(await fetchJson("/api/consultation-requests"))); }
  catch { /* rozet bilgilendirme amaçlıdır */ }
}

async function loadConsultationRequests() {
  const target = $("#consultationRequestList");
  if (!state.auth?.authenticated) return;
  target.innerHTML = "<p>Danışmanlık talepleri yükleniyor…</p>";
  try {
    const data = await fetchJson("/api/consultation-requests");
    renderConsultationBadge(consultationAttentionCount(data));
    const items = [...(data.incoming || []), ...(data.outgoing || [])];
    target.innerHTML = items.length ? items.map(requestCard).join("") : "<p>Henüz danışmanlık talebiniz yok.</p>";
  } catch (error) { target.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`; }
}

function openConsultationDialog(consultant) {
  if (!state.auth?.authenticated) {
    showToast("Danışmana dosya göndermek için Google hesabınızla giriş yapın.");
    window.setTimeout(() => { location.href = "/auth/google"; }, 700);
    return;
  }
  if (!state.currentCustomsResult) return showToast("Önce Ürüne Sor alanında bir analiz oluşturun.");
  state.selectedConsultant = consultant;
  $("#consultationAdvisorName").textContent = `${consultant.display_name} · ${consultant.title}`;
  const inquiry = state.currentCustomsResult.inquiry || {};
  $("#consultationSubject").value = `${inquiry.candidate_gtip || "GTİP"} için sınıflandırma ve mevzuat görüşü`;
  $("#consultationMessage").value = "Analizdeki aday GTİP, ticaret önlemleri ve ürün güvenliği kapsamını resmî kaynaklarıyla değerlendirmenizi rica ederim.";
  $("#consultationConsent").checked = false;
  $("#consultationDialog").showModal();
}

function switchCustomsView(view) {
  $$('[data-customs-view]').forEach((button) => button.classList.toggle("active", button.dataset.customsView === view));
  $$('[data-customs-panel]').forEach((panel) => { panel.hidden = panel.dataset.customsPanel !== view; });
  if (view === "changes") {
    renderWatchList();
    loadChanges();
  }
  if (view === "consultants") loadConsultants();
}

$$('[data-customs-view]').forEach((button) => button.addEventListener("click", () => switchCustomsView(button.dataset.customsView)));

$("#refreshConsultants").addEventListener("click", loadConsultants);
$("#refreshConsultationRequests").addEventListener("click", () => {
  if (!state.auth?.authenticated) return showToast("Talepler için Google hesabınızla giriş yapın.");
  loadConsultationRequests();
});
$("#openConsultantApplication").addEventListener("click", () => {
  if (!state.auth?.authenticated) {
    showToast("Ücretsiz danışman kaydı için Google hesabınızla giriş yapın.");
    window.setTimeout(() => { location.href = "/auth/google"; }, 700);
    return;
  }
  $("#consultantApplication").open = true;
  $("#consultantApplication").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("#consultantGrid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-send-consultant]");
  if (!button || button.disabled) return;
  const consultant = state.consultants.find((item) => item.id === button.dataset.sendConsultant);
  if (consultant) openConsultationDialog(consultant);
});
$("#consultantApplicationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.auth?.authenticated) return showToast("Başvuru için Google hesabınızla giriş yapın.");
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const data = await fetchJson("/api/consultants/me", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        display_name: $("#consultantName").value.trim(), title: $("#consultantTitle").value,
        city: $("#consultantCity").value.trim(), service_mode: $("#consultantMode").value,
        experience_years: Number($("#consultantExperience").value || 0),
        bio: $("#consultantBio").value.trim(),
        expertise: $$('[name="consultantExpertise"]:checked').map((input) => input.value),
        advisory_only_accepted: $("#consultantTerms").checked,
      }),
    });
    fillConsultantApplication(data.profile);
    showToast("Ücretsiz danışman başvurunuz incelemeye gönderildi.");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});

function updateFieldCounter(input) {
  const hint = document.querySelector(`[data-counter-for="${input.id}"]`);
  if (!hint) return;
  const length = input.value.trim().length;
  const min = Number(input.getAttribute("minlength") || 0);
  const max = Number(input.getAttribute("maxlength") || 0);
  const short = length < min;
  hint.textContent = short ? `${length} / ${max} · en az ${min} karakter${length ? ` (${min - length} daha)` : ""}` : `${length} / ${max}`;
  hint.classList.toggle("short", short && length > 0);
}
["#consultationSubject", "#consultationMessage"].forEach((selector) => {
  const input = $(selector);
  if (!input) return;
  input.addEventListener("input", () => updateFieldCounter(input));
  updateFieldCounter(input);
});
$("#consultationForm button[type=submit]")?.addEventListener("click", (event) => {
  const form = $("#consultationForm");
  if (form.checkValidity()) return;
  event.preventDefault();
  const invalid = form.querySelector(":invalid");
  const label = invalid?.closest("label")?.querySelector("span")?.textContent?.replace("*", "").trim() || "Form";
  const min = invalid?.getAttribute("minlength");
  showToast(invalid?.type === "checkbox" ? "Paylaşım onayı kutusunu işaretleyin." : min ? `${label}: en az ${min} karakter yazın.` : `${label} alanı zorunludur.`);
  invalid?.focus();
  form.reportValidity();
});

function closeConsultationDialog() { $("#consultationDialog").close(); state.selectedConsultant = null; }
$("#closeConsultationDialog").addEventListener("click", closeConsultationDialog);
$("#cancelConsultation").addEventListener("click", closeConsultationDialog);
$("#consultationDialog").addEventListener("click", (event) => { if (event.target === $("#consultationDialog")) closeConsultationDialog(); });
$("#consultationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const consultant = state.selectedConsultant;
  if (!consultant || !state.currentCustomsResult) return showToast("Danışman veya analiz dosyası bulunamadı.");
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await fetchJson("/api/consultation-requests", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        consultant_id: consultant.id, subject: $("#consultationSubject").value.trim(),
        message: $("#consultationMessage").value.trim(), share_consent: $("#consultationConsent").checked,
        result: state.currentCustomsResult,
      }),
    });
    closeConsultationDialog();
    await loadConsultationRequests();
    $("#consultationInbox").scrollIntoView({ behavior: "smooth", block: "start" });
    showToast("Analiz özeti danışmana güvenle gönderildi.");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});

$("#consultationRequestList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-request-status]");
  if (!button) return;
  const requestNode = button.closest("[data-request-id]");
  button.disabled = true;
  try {
    await fetchJson(`/api/consultation-requests/${encodeURIComponent(requestNode.dataset.requestId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: button.dataset.requestStatus }),
    });
    await loadConsultationRequests();
    showToast("Danışmanlık talebi güncellendi.");
  } catch (error) { showToast(error.message); button.disabled = false; }
});
$("#consultationRequestList").addEventListener("submit", async (event) => {
  const form = event.target.closest(".consultation-reply");
  if (!form) return;
  event.preventDefault();
  const requestNode = form.closest("[data-request-id]");
  const textarea = form.querySelector("textarea");
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await fetchJson(`/api/consultation-requests/${encodeURIComponent(requestNode.dataset.requestId)}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body: textarea.value.trim() }),
    });
    await loadConsultationRequests();
    showToast("Mesaj görüşmeye eklendi.");
  } catch (error) { showToast(error.message); button.disabled = false; }
});

function tariffRows(items) {
  if (!items?.length) return '<tr><td colspan="5">Bu menşe sütunu için uygulanabilir satır bulunamadı.</td></tr>';
  return items.map((item) => `<tr>
    <td><code>${escapeHtml(item.gtip)}</code><br>${escapeHtml(item.measure_type)}</td>
    <td>${item.rate == null ? escapeHtml(item.rate_text) : `%${escapeHtml(item.rate)}`}${item.footnote ? `<br><small>Dipnot: ${escapeHtml(item.footnote)}</small>` : ""}</td>
    <td>${escapeHtml(item.country_group)} · ${escapeHtml(item.country_group_description)}</td>
    <td>${escapeHtml(item.source_file)}<br>${escapeHtml(item.source_sheet)} · satır ${escapeHtml(item.source_row)}</td>
    <td><a href="${safeUrl(item.source_url)}" target="_blank" rel="noreferrer">Resmî kaynak ↗</a><br><small>SHA ${escapeHtml(item.archive_sha256.slice(0, 12))}</small></td>
  </tr>`).join("");
}

const tariffMeasureLabels = {
  customs_duty: "Gümrük vergisi",
  additional_duty: "İlave gümrük vergisi",
  additional_financial_liability: "Ek mali yükümlülük",
};

function tariffMatchSummary(tariff) {
  if (tariff.match_mode !== "prefix") return "";
  const variantItems = Object.entries(tariff.rate_variants || {}).map(([type, rates]) => {
    const label = tariffMeasureLabels[type] || type;
    const values = rates?.length ? rates.map((rate) => `%${numberFormat.format(rate)}`).join(" / ") : "oran okunamadı";
    const ambiguous = (tariff.ambiguous_measure_types || []).includes(type);
    return `<li><b>${escapeHtml(label)}:</b> ${escapeHtml(values)}${ambiguous ? " · oran veya kapsam alt GTİP’e göre değişiyor" : " · bütün alt satırlarda aynı"}</li>`;
  });
  if (!tariff.rate_variants?.additional_duty && tariff.measure_coverage?.additional_duty?.status === "verified_snapshot" && !(tariff.ambiguous_measure_types || []).includes("additional_duty")) {
    variantItems.push(`<li><b>${escapeHtml(tariffMeasureLabels.additional_duty)}:</b> %0 · İGV listesinde yer almıyor (uygulanmıyor)</li>`);
  }
  if (!tariff.rate_variants?.additional_financial_liability && !(tariff.ambiguous_measure_types || []).includes("additional_financial_liability")) {
    variantItems.push(`<li><b>${escapeHtml(tariffMeasureLabels.additional_financial_liability)}:</b> %0 · EMY listesinde yer almıyor (uygulanmıyor)</li>`);
  }
  const variants = variantItems.join("");
  return `<div class="result-caution"><strong>${escapeHtml(tariff.gtip.length)} haneli kodla ön ek araması:</strong> ${escapeHtml(tariff.matched_gtip_count || 0)} adet 12 haneli Türk GTİP satırı bulundu.${variants ? `<ul>${variants}</ul>` : ""}</div>`;
}

const rateFieldByMeasure = {
  customs_duty: "#customsDutyRate",
  additional_duty: "#additionalDutyRate",
  additional_financial_liability: "#additionalFinancialLiabilityRate",
};

function applicableTariffRates(tariff) {
  if (!tariff) return {};
  const ambiguous = new Set(tariff.ambiguous_measure_types || []);
  const rates = {};
  (tariff.measures || []).forEach((item) => {
    if (item.rate == null || !rateFieldByMeasure[item.measure_type] || ambiguous.has(item.measure_type)) return;
    if (!(item.measure_type in rates)) rates[item.measure_type] = Number(item.rate);
  });
  if (!ambiguous.has("additional_duty") && !("additional_duty" in rates) && tariff.measure_coverage?.additional_duty?.status === "verified_snapshot") {
    rates.additional_duty = 0;
  }
  return rates;
}

function applyRatesButton(tariff, source) {
  const rates = applicableTariffRates(tariff);
  const keys = Object.keys(rates);
  state.tariffRateSources[source] = rates;
  if (!keys.length) return "";
  const summary = keys.map((key) => `${tariffMeasureLabels[key] || key} %${numberFormat.format(rates[key])}`).join(" · ");
  return `<div class="apply-rates"><div><b>Bulunan oranları maliyet hesabına aktar</b><small>${escapeHtml(summary)} · ${escapeHtml(tariff.gtip)}${tariff.origin_country ? ` · ${escapeHtml(tariff.origin_country)}` : ""}</small></div><button type="button" data-apply-rates="${escapeHtml(source)}">Bu oranları kullan</button></div>`;
}

// ---- Dışa aktarım (CSV) ve panoya kopyalama --------------------------------
const exportStore = {};

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return /[;"\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function csvNumber(value) {
  // Excel (tr-TR) ondalık ayırıcı olarak virgül bekler.
  return value == null || value === "" ? "" : String(value).replace(".", ",");
}

function buildCsv(headers, rows) {
  return "\ufeff" + [headers, ...rows].map((row) => row.map(csvCell).join(";")).join("\r\n");
}

function buildTsv(headers, rows) {
  return [headers, ...rows].map((row) => row.map((value) => String(value ?? "").replace(/[\t\n\r]+/g, " ")).join("\t")).join("\n");
}

function downloadTextFile(name, text, mime = "text/csv;charset=utf-8") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function fileStamp() {
  return new Date().toISOString().slice(0, 10);
}

const exportTables = {
  measures(data) {
    const tariff = data.tariff || data.tariff_lookup || data;
    const rows = [...(tariff.measures || []), ...(tariff.conditional_measures || [])].map((item) => [
      item.gtip, tariffMeasureLabels[item.measure_type] || item.measure_type, csvNumber(item.rate ?? item.rate_text),
      item.country_group, item.country_group_description, item.description || "", item.footnote || "", item.condition || "",
      item.list_name, item.source_file, item.source_sheet, item.source_row, item.valid_from, item.retrieved_at, item.archive_sha256,
    ]);
    return {
      name: `tarife-${tariff.gtip}-${(tariff.origin_country || "mense").replace(/\s+/g, "_")}-${fileStamp()}.csv`,
      headers: ["GTİP", "Önlem", "Oran %", "Menşe sütunu", "Sütun açıklaması", "Eşya tanımı", "Dipnot", "Şart", "Liste", "Kaynak dosya", "Sayfa", "Satır", "Yürürlük", "Alınma tarihi", "Arşiv SHA-256"],
      rows,
    };
  },
  cost(data) {
    const cost = data.cost || data.deterministic_cost;
    if (!cost) return null;
    const currency = cost.currency || "";
    const rows = (cost.lines || []).map((line) => [line.label, csvNumber(line.base), csvNumber(line.rate), csvNumber(line.amount), currency, line.formula || ""]);
    if (cost.total_taxes != null) rows.push(["Toplam vergi", "", "", csvNumber(cost.total_taxes), currency, ""]);
    if (cost.landed_total != null) rows.push(["Genel toplam (vergiler dahil)", "", "", csvNumber(cost.landed_total), currency, ""]);
    if (cost.unit_landed_cost != null) rows.push(["Birim maliyet", "", "", csvNumber(cost.unit_landed_cost), currency, ""]);
    (cost.missing_rates || []).forEach((item) => rows.push(["Eksik girdi", "", "", "", "", item]));
    const lira = cost.try_summary;
    if (lira) {
      (lira.lines || []).forEach((line) => rows.push([`${line.label} (TL)`, "", "", csvNumber(line.amount_try), "TRY", `kur ${lira.exchange_rate}`]));
      rows.push(["KDV matrahı (TL)", "", "", csvNumber(lira.vat_base_try), "TRY", ""]);
      rows.push(["Toplam vergi (TL)", "", "", csvNumber(lira.total_taxes_try), "TRY", ""]);
      rows.push(["Genel toplam (TL)", "", "", csvNumber(lira.landed_total_try), "TRY", ""]);
    }
    const tariff = data.tariff || data.tariff_lookup || {};
    return {
      name: `maliyet-${tariff.gtip || "gtip"}-${fileStamp()}.csv`,
      headers: ["Kalem", "Matrah", "Oran %", "Tutar", "Para birimi", "Formül / not"],
      rows,
    };
  },
  bulk(data) {
    const rows = (data.rows || []).map((row) => [
      row.line ?? row.index, row.gtip || "", row.origin_country || "", row.dispatch_country || "", row.description || "",
      row.status, row.resolved_country_group || "", csvNumber(row.rates?.customs_duty), csvNumber(row.rates?.additional_duty),
      csvNumber(row.rates?.financial_liability), csvNumber(row.rates?.kkdf), csvNumber(row.rates?.vat),
      csvNumber(row.customs_value), csvNumber(row.total_taxes), csvNumber(row.landed_total), csvNumber(row.unit_landed_cost),
      row.currency || "", csvNumber(row.total_taxes_try), csvNumber(row.landed_total_try), (row.missing_rates || []).join(" | "), row.error || (row.warnings || []).join(" | "),
    ]);
    return {
      name: `toplu-hesap-${fileStamp()}.csv`,
      headers: ["Satır", "GTİP", "Menşe", "Sevk", "Açıklama", "Durum", "Sütun", "GV %", "İGV %", "EMY %", "KKDF %", "KDV %", "Gümrük kıymeti", "Toplam vergi", "Genel toplam", "Birim maliyet", "Para birimi", "Toplam vergi (TL)", "Genel toplam (TL)", "Eksik girdiler", "Hata / uyarılar"],
      rows,
    };
  },
  scenarios(data) {
    const rows = (data.rows || []).map((row) => [
      row.origin_country, row.dispatch_country || "", row.origin_documents?.regime_name || "", row.resolved_country_group || "",
      csvNumber(row.unambiguous_rates?.customs_duty), csvNumber(row.unambiguous_rates?.additional_duty), csvNumber(row.unambiguous_rates?.additional_financial_liability),
      row.atr_free_circulation ? "evet" : "hayır", (row.origin_proof_required || []).map((key) => tariffMeasureLabels[key] || key).join(", "),
      Object.entries(row.fallback_rates || {}).map(([key, value]) => `${tariffMeasureLabels[key] || key} %${value}`).join(", "),
      (row.origin_documents?.documents || []).map((item) => item.name).join(" | "), (row.warnings || []).join(" | "),
    ]);
    return {
      name: `mense-senaryolari-${data.gtip}-${fileStamp()}.csv`,
      headers: ["Menşe", "Sevk ülkesi", "Rejim", "Sütun", "Gümrük vergisi %", "İGV %", "EMY %", "A.TR serbest dolaşım", "Menşe tevsiki gereken", "Tevsik yoksa oran", "Belgeler", "Uyarılar"],
      rows,
    };
  },
  savings(data) {
    const rows = (data.ranked || []).map((item) => [
      item.rank, item.origin_country, item.dispatch_country || "", savingsVariantLabel(item), item.resolved_country_group || "",
      csvNumber(item.rates_used?.customs_duty), csvNumber(item.rates_used?.additional_duty), csvNumber(item.rates_used?.additional_financial_liability),
      csvNumber(item.total_taxes), csvNumber(item.landed_total), csvNumber(item.landed_total_pessimistic),
      item.is_baseline ? "temel senaryo" : csvNumber(item.savings_vs_baseline), item.is_baseline ? "" : csvNumber(item.savings_pct),
      (item.conditions || []).join(" | "), (item.warnings || []).join(" | "),
    ]);
    return {
      name: `tasarruf-onerisi-${data.gtip}-${fileStamp()}.csv`,
      headers: ["Sıra", "Menşe", "Sevk ülkesi", "Varyant", "Sütun", "Gümrük vergisi %", "İGV %", "EMY %", `Vergiler toplamı ${data.currency || ""}`, `Toplam maliyet ${data.currency || ""}`, "Tevsik yoksa toplam", "Temel senaryoya göre tasarruf", "Tasarruf %", "Koşullar", "Uyarılar"],
      rows,
    };
  },
};

function exportBar(kind, tables) {
  const buttons = tables.map(({ table, label }) => `
    <button type="button" data-export="${escapeHtml(kind)}" data-table="${escapeHtml(table)}" data-format="csv">${escapeHtml(label)} · CSV</button>
    <button type="button" data-export="${escapeHtml(kind)}" data-table="${escapeHtml(table)}" data-format="copy">${escapeHtml(label)} · Kopyala</button>`).join("");
  return `<div class="export-bar" role="group" aria-label="Dışa aktar">${buttons}</div>`;
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-export]");
  if (!button) return;
  const data = exportStore[button.dataset.export];
  const builder = exportTables[button.dataset.table];
  const table = data && builder ? builder(data) : null;
  if (!table || !table.rows.length) return showToast("Dışa aktarılacak satır yok.");
  if (button.dataset.format === "copy") {
    copyText(buildTsv(table.headers, table.rows), `${table.rows.length} satır panoya kopyalandı (Excel'e yapıştırılabilir).`);
    return;
  }
  downloadTextFile(table.name, buildCsv(table.headers, table.rows));
  showToast(`${table.name} indirildi.`);
});

function renderLiraSummary(summary) {
  if (!summary) return "";
  const lira = (value) => value == null ? "—" : `${numberFormat.format(value)} TL`;
  const bilgeCodeMap = {
    duty: "401 (Gümrük Vergisi)",
    additional: "403 (İlave Gümrük Vergisi)",
    additional_financial_liability: "404 (Ek Mali Yükümlülük)",
    anti_dumping: "405 (Dampinge Karşı Vergi)",
    kkdf: "408 (Kaynak Kullanım Fonu)",
    sct: "409 (Özel Tüketim Vergisi)",
    trt_bandrol: "410 (TRT Bandrol)",
    stamp_duty: "0001 (Damga Vergisi)",
    port_storage: "— (Liman/Ardiye Tescil Öncesi)",
    vat: "407 (KDV)",
    gekap: "— (GEKAP)",
  };

  const lines = (summary.lines || []).map((line) => {
    const code = bilgeCodeMap[line.code] || "—";
    return `<tr>
      <td>${escapeHtml(line.label)}</td>
      <td><code>${escapeHtml(code)}</code></td>
      <td class="num">${lira(line.amount_try)}</td>
    </tr>`;
  }).join("");

  return `
    <div class="tax-compass-wrapper">
      <div class="tax-compass-header">
        <h3>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/></svg>
          <span>Gümrük Beyannamesi Tahakkuk Pusulası</span>
          <span class="bilge-badge">BİLGE TAHAKKUK FORMATI</span>
        </h3>
        <small>${summary.currency} TCMB Satış Kuru: <b>${summary.exchange_rate} TL</b>${summary.exchange_rate_date ? ` (${summary.exchange_rate_date})` : ""}</small>
      </div>
      <table class="tax-compass-table">
        <thead>
          <tr>
            <th>Vergi / Masraf Türü</th>
            <th>BİLGE Kodu</th>
            <th style="text-align:right">Tutar (TL)</th>
          </tr>
        </thead>
        <tbody>
          <tr class="compass-section-head">
            <td colspan="2">1. Gümrük CIF Kıymeti Matrahı</td>
            <td class="num">${lira(summary.customs_value_try)}</td>
          </tr>
          ${lines}
          <tr class="compass-section-head">
            <td colspan="2">2. KDV Matrahı (CIF + İthalat Vergileri + Ardiye + Damga)</td>
            <td class="num">${lira(summary.vat_base_try)}</td>
          </tr>
          <tr class="compass-total-row">
            <td>Gümrük İdaresine Ödenecek Toplam Vergi ve Harçlar</td>
            <td><code>401-410 Toplam</code></td>
            <td class="num">${lira(summary.total_taxes_try)}</td>
          </tr>
          <tr class="compass-total-row" style="background: color-mix(in srgb, var(--teal) 14%, var(--surface));">
            <td><strong>Genel İthalat Çıkış Maliyeti (Eşya Bedeli + Tüm Vergiler)</strong></td>
            <td><strong>Landed Cost</strong></td>
            <td class="num"><strong>${lira(summary.landed_total_try)}</strong></td>
          </tr>
          ${summary.unit_landed_cost_try != null ? `
          <tr>
            <td>Birim İthalat Maliyeti (Adet / Birim Başına)</td>
            <td>Birim</td>
            <td class="num"><strong>${lira(summary.unit_landed_cost_try)}</strong></td>
          </tr>` : ""}
        </tbody>
      </table>
      <div style="padding: 8px 14px; font-size: .72rem; color: var(--ink-2); background: var(--surface-2, #f9fafb); border-top: 1px solid var(--line);">
        ${(summary.notes || []).map((n) => `<p style="margin:2px 0;">${escapeHtml(n)}</p>`).join("")}
      </div>
    </div>
  `;
}

const TRADE_MEASURE_LABELS = { anti_dumping: "Damping / sübvansiyon", safeguard: "Korunma önlemleri", surveillance: "Gözetim tebliğleri", tariff_quota: "Tarım tarife kontenjanları", communiques: "İthalat Tebliğleri" };
function tradeSourceText(sources) {
  return Object.entries(sources || {}).map(([kind, meta]) => {
    const state = meta.origin === "synced" ? `resmî eşitleme ${String(meta.fetched_at || "").slice(0, 10)}` : meta.origin === "seed" ? "depo tohum verisi" : "yüklü değil";
    return `${TRADE_MEASURE_LABELS[kind] || kind}: ${state}`;
  }).join(" · ");
}
function renderExciseTax(excise) {
  if (!excise) return "";
  const rows = (excise.matches || []).map((hit) => {
    const values = Object.entries(hit.values || {}).map(([key, value]) => `${escapeHtml(key)}: ${escapeHtml(String(value))}`).join("<br>");
    return `<tr><td><code>${escapeHtml(hit.matched_code)}</code></td><td>${escapeHtml(hit.list_label)}</td><td>${escapeHtml(hit.description || "")}</td><td>${values || "—"}</td></tr>`;
  });
  const related = (excise.related_positions || []).map((hit) => `<li><code>${escapeHtml(hit.matched_code)}</code> · ${escapeHtml(hit.list_label)} ${escapeHtml(hit.description || "")}</li>`);
  const empty = related.length
    ? `<p class="missing-list">Bu GTİP ekli listelerde yok; aynı pozisyonda ÖTV satırları var:</p><ul class="missing-list">${related.join("")}</ul>`
    : `<p class="missing-list">Bu GTİP 4760 sayılı Kanunun ekli listelerinde bulunamadı (ÖTV'ye tabi görünmüyor).</p>`;
  return `<details class="advanced-fields trade-measures" ${rows.length || related.length ? "open" : ""}><summary><span>ÖTV kapsamı</span><small>${rows.length ? `${rows.length} liste satırı` : (related.length ? "kod değişmiş olabilir" : "kapsam dışı")}</small></summary>
    ${rows.length ? `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Kod</th><th>Liste</th><th>Eşya</th><th>Oran / tutar</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : empty}
    ${(excise.warnings || []).map((item) => `<p class="rate-warning">${escapeHtml(item)}</p>`).join("")}
    <p class="rate-warning">Kaynak: ${escapeHtml(excise.legal_basis || "")}.</p></details>`;
}

// KDV önerisi (2007/13033 ekli listeler). Yalnız gösterir; alan ancak "Öneriyi kullan" ile dolar.
function renderVatSuggestion(vat) {
  if (!vat) return "";
  const basisLabel = vat.basis === "official_list" ? "resmî liste" : "sezgisel fasıl kuralı";
  const verifiedNote = vat.verified === false ? " · satır tohum verisinde doğrulanmadı" : "";
  const conditions = (vat.conditions || []).length ? ` · şart: ${vat.conditions.map((item) => escapeHtml(item)).join(", ")}` : "";
  if (vat.ambiguous) {
    const options = (vat.candidates || []).filter((item) => item.rate != null);
    const buttons = options.map((item) => `<button type="button" data-use-vat="${escapeHtml(String(item.rate))}">%${numberFormat.format(item.rate)} kullan</button>`).join("");
    const lines = options.map((item) => `%${numberFormat.format(item.rate)}${(item.conditions || []).length ? ` (${item.conditions.map((cond) => escapeHtml(cond)).join(", ")})` : ""} – ${escapeHtml(item.legal_basis || "")}`).join(" · ");
    return `<div class="apply-rates vat-suggestion"><div><b>KDV önerisi belirsiz · şartı doğrulayıp seçin</b><small>${lines}${vat.row_text ? ` · "${escapeHtml(vat.row_text)}"` : ""}</small></div><div class="vat-suggestion-actions">${buttons}</div></div>`;
  }
  if (vat.rate == null) return "";
  return `<div class="apply-rates vat-suggestion"><div><b>KDV önerisi: %${numberFormat.format(vat.rate)} · ${basisLabel} · onayınız gerekir</b><small>${escapeHtml(vat.legal_basis || "")}${vat.matched_expression ? ` · eşleşen ifade ${escapeHtml(vat.matched_expression)}` : ""}${conditions}${verifiedNote}${vat.row_text ? ` · "${escapeHtml(vat.row_text)}"` : ""}</small></div><button type="button" data-use-vat="${escapeHtml(String(vat.rate))}">Öneriyi kullan</button></div>`;
}

function renderTradeMeasures(trade) {
  if (!trade) return "";
  const statusLabel = { in_force: "yürürlükte", expired: "süresi dolmuş", unknown: "süre bilgisi yok" };
  const groups = [
    ["Damping / sübvansiyon", (trade.anti_dumping || []).filter((hit) => hit.origin_match !== false)],
    ["Korunma önlemi", (trade.safeguard || []).filter((hit) => hit.origin_match !== false)],
    ["Gözetim", trade.surveillance || []],
    ["Tarife kontenjanı", (trade.tariff_quota || []).filter((hit) => hit.origin_match !== false)],
  ];
  const rows = groups.flatMap(([label, hits]) => hits.map((hit) => `<tr><td>${escapeHtml(label)}</td><td><code>${escapeHtml(hit.matched_code)}</code> ${escapeHtml(hit.product || "")}</td><td>${escapeHtml(hit.country || "")}</td><td>${escapeHtml(hit.rate_text || "—")} ${escapeHtml(hit.unit || "")}</td><td>${escapeHtml(hit.legal_act || "")}<br><small>${escapeHtml(hit.gazette || "")}${hit.expires ? ` · bitiş ${escapeHtml(hit.expires)}` : ""}${hit.notes ? ` · ${escapeHtml(hit.notes)}` : ""}</small></td><td><span class="measure-status ${escapeHtml(hit.status || "")}">${escapeHtml(statusLabel[hit.status] || hit.status || "")}</span></td></tr>`));
  const hidden = (trade.anti_dumping || []).filter((hit) => hit.origin_match === false).length;
  const empty = `<p class="missing-list">Bu GTİP için resmî listelerde ${trade.origin_country ? "bu menşeye uygulanan " : ""}damping, korunma veya gözetim satırı bulunmadı.</p>`;
  return `<details class="advanced-fields trade-measures" ${rows.length ? "open" : ""}><summary><span>Damping, korunma, gözetim ve kontenjan kapsamı</span><small>${rows.length ? `${rows.length} resmî satır` : "eşleşen önlem yok"}</small></summary>
    ${rows.length ? `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Önlem</th><th>Kapsam</th><th>Ülke</th><th>Oran / kıymet</th><th>Dayanak</th><th>Durum</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : empty}
    ${hidden ? `<p class="rate-warning">${hidden} damping satırı başka menşe ülkelere ait olduğu için gösterilmedi.</p>` : ""}
    <p class="rate-warning">${escapeHtml(tradeSourceText(trade.sources))}. Oran ve tutarlar resmî tabloda yazıldığı gibidir; firma bazlı oranlar ve kontenjan muafiyetleri için tebliğ metnini doğrulayın.</p></details>`;
}

const VALIDITY_LABELS = {
  current: ["Bugün yürürlükte", "Aktif resmî sürüm"],
  legal: ["Yasal yürürlük aralığı", "Sürüm sınırları resmî yürürlük tarihlerinden"],
  observed: ["Gözlemlenen aralık", "Sürüm sınırı indirme tarihlerinden türetildi; o günkü resmî metni ayrıca doğrulayın"],
  unavailable: ["Sürüm yok", "Bu tarihi kapsayan onaylı sürüm arşivde yok"],
};

function validityBadgeHtml(result) {
  if (!result || !result.validity_basis) return "";
  const [label, title] = VALIDITY_LABELS[result.validity_basis] || [result.validity_basis, ""];
  const when = result.as_of_date ? ` · ${escapeHtml(result.as_of_date)}` : "";
  return ` <span class="validity-badge validity-${escapeHtml(result.validity_basis)}" title="${escapeHtml(title)}">${escapeHtml(label)}${when}</span>`;
}

function renderTariffTool(data) {
  exportStore.tool = data;
  const tariff = data.tariff || data;
  const cost = data.cost;
  const warnings = [...(tariff.warnings || []), ...(cost?.warnings || [])];
  const costLedger = cost ? `<div class="formula-ledger"><h3>Maliyet formülü · ${escapeHtml(cost.status)}</h3>
    ${(cost.lines || []).map((line) => `<div class="formula-line"><span>${escapeHtml(line.label)} <small>${escapeHtml(line.formula)}</small></span><code>${line.amount == null ? "—" : `${numberFormat.format(line.amount)} ${escapeHtml(cost.currency)}`}</code></div>`).join("")}
    <div class="formula-line"><strong>Toplam vergi</strong><code>${cost.total_taxes == null ? "Oran eksik" : `${numberFormat.format(cost.total_taxes)} ${escapeHtml(cost.currency)}`}</code></div>
    <div class="formula-line"><strong>Genel toplam (vergiler dahil)</strong><code>${cost.landed_total == null ? "Oran eksik" : `${numberFormat.format(cost.landed_total)} ${escapeHtml(cost.currency)}`}</code></div>
    ${cost.unit_landed_cost != null ? `<div class="formula-line"><strong>Birim maliyet</strong><code>${numberFormat.format(cost.unit_landed_cost)} ${escapeHtml(cost.currency)}</code></div>` : ""}
    ${(cost.missing_rates || []).length ? `<div class="result-caution"><b>Toplam için eksik girdiler:</b> ${cost.missing_rates.map((item) => escapeHtml(item)).join(" · ")}</div>` : ""}
    <p class="rate-warning">Kredili/vadeli ödemede KKDF eklenir, peşin ödemede bu kalem %0'dır. Beyanname damga vergisi ve TL giderler kur girildiğinde TL özetinde gösterilir. Kesin tutar için beyan öncesi gümrük müşaviri teyidi alın.</p></div>${renderLiraSummary(cost.try_summary)}` : "";
  return `<div class="answer-head"><span class="answer-status${tariff.status === "matched" ? "" : " warning"}">${escapeHtml(tariff.status)}</span><div><h2>${escapeHtml(tariff.gtip)} · ${escapeHtml(tariff.origin_country || "menşe seçilmedi")}</h2><p>Ülke grubu: ${escapeHtml(tariff.resolved_country_group || "çözümlenmedi")} · ${escapeHtml(tariff.as_of)}${validityBadgeHtml(tariff)}</p></div></div>
    ${tariffMatchSummary(tariff)}
    ${renderTradeMeasures(tariff.trade_measures)}
    ${renderExciseTax(tariff.excise_tax)}
    ${renderVatSuggestion(tariff.vat_rate)}
    <table class="evidence-table"><thead><tr><th>GTİP / Önlem</th><th>Oran</th><th>Menşe sütunu</th><th>Kaynak satırı</th><th>Kanıt</th></tr></thead><tbody>${tariffRows(tariff.measures)}</tbody></table>
    ${exportBar("tool", [{ table: "measures", label: "Tarife satırları" }, ...(cost ? [{ table: "cost", label: "Maliyet defteri" }] : [])])}
    ${applyRatesButton(tariff, "tool")}
    ${tariff.conditional_measures?.length ? `<details class="advanced-fields"><summary><span>Şarta bağlı askıya alma / nihai kullanım satırları</span><small>${tariff.conditional_measures.length} kayıt</small></summary><table class="evidence-table"><tbody>${tariffRows(tariff.conditional_measures)}</tbody></table></details>` : ""}
    ${costLedger}
    ${decisionQuestionsHtml(data.decision_questions, "tool")}
    ${warnings.length ? `<div class="result-caution">${warnings.map((item) => escapeHtml(item)).join(" · ")}</div>` : ""}
    ${data.legal_notice ? `<div class="legal-banner"><strong>Önemli:</strong> ${escapeHtml(data.legal_notice)}</div>` : ""}`;
}

// KDV önerisi yalnız bu düğmeyle alana yazılır; sayfa hiçbir zaman otomatik doldurmaz.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-use-vat]");
  if (!button) return;
  const rate = Number(String(button.dataset.useVat).replace(",", "."));
  if (!Number.isFinite(rate) || rate < 0 || rate > 100) return showToast("Geçersiz KDV oranı.");
  const targets = [$("#tariffVat"), $("#vatRate")].filter(Boolean);
  const filled = [];
  targets.forEach((field, index) => {
    if (index > 0 && field.value.trim()) return; // yardımcı formdaki dolu değeri ezme
    field.value = String(rate);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    filled.push(field.id);
  });
  if (!filled.length) return;
  if (typeof updateReadiness === "function") updateReadiness();
  showToast(`KDV %${numberFormat.format(rate)} maliyet alanına aktarıldı; analizi yeniden çalıştırın.`);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-apply-rates]");
  if (!button) return;
  const rates = state.tariffRateSources[button.dataset.applyRates] || {};
  const applied = [];
  Object.entries(rates).forEach(([key, value]) => {
    const field = $(rateFieldByMeasure[key]);
    if (!field) return;
    field.value = String(value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    applied.push(tariffMeasureLabels[key] || key);
  });
  if (!applied.length) return showToast("Aktarılabilir doğrulanmış oran bulunamadı.");
  if (button.dataset.applyRates === "tool") {
    const code = $("#tariffGtip").value.trim();
    const origin = $("#tariffOrigin").value.trim();
    if (code && !$("#candidateGtip").value.trim()) { $("#candidateGtip").value = code; $("#candidateGtip").dispatchEvent(new Event("input", { bubbles: true })); }
    if (origin && !$("#originCountry").value.trim()) $("#originCountry").value = origin;
    const dispatch = $("#tariffDispatch")?.value.trim();
    if (dispatch && !$("#dispatchCountry").value.trim()) $("#dispatchCountry").value = dispatch;
    const invoice = $("#tariffInvoice").value.trim();
    if (invoice && !$("#invoiceValue").value.trim()) { $("#invoiceValue").value = invoice; $("#freight").value = $("#tariffFreight").value || $("#freight").value; $("#insurance").value = $("#tariffInsurance").value || $("#insurance").value; $("#currency").value = $("#tariffCurrency").value; }
    switchCustomsView("assistant");
  }
  const costDetails = $("#costDetails");
  if (costDetails && "open" in costDetails) costDetails.open = true;
  $("#customsDutyRate")?.scrollIntoView({ behavior: "smooth", block: "center" });
  updateReadiness();
  showToast(`${applied.join(", ")} oranı maliyet alanlarına aktarıldı; analizi yeniden çalıştırın.`);
});

$("#tariffForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const output = $("#tariffOutput");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Resmî tarife satırları alınıyor</b><span>Menşe grubu, dipnot ve snapshot kanıtı denetleniyor…</span></div></div>';
  const common = {
    gtip: $("#tariffGtip").value.trim(),
    origin_country: $("#tariffOrigin").value.trim(),
    dispatch_country: $("#tariffDispatch")?.value.trim() || null,
    atr_certificate: $("#tariffAtr")?.value || null,
    as_of: $("#tariffAsOf")?.value || null,
  };
  if (common.as_of && common.as_of < new Date().toISOString().slice(0, 10) && !hasCapability("temporal_query")) {
    output.innerHTML = featureUpsellHtml("temporal_query");
    button.disabled = false;
    return;
  }
  try {
    const invoice = nullableNumber("#tariffInvoice");
    const data = invoice == null
      ? await fetchJson("/api/tariff/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(common) })
      : await fetchJson("/api/tariff/cost", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...common, ...collectTariffCostInputs(invoice), decision_answers: { ...(state.decisionAnswers.tool || {}) } }) });
    output.innerHTML = renderTariffTool(data);
    const scenarioBox = $("#scenarioBox");
    if (scenarioBox) {
      scenarioBox.hidden = false;
      const input = $("#scenarioOrigins");
      if (input && !input.value.trim()) {
        const base = $("#tariffOrigin").value.trim();
        const candidates = ["Çin", "Almanya", "Güney Kore", "İtalya"]
          .filter((item) => item.toLocaleLowerCase("tr") !== base.toLocaleLowerCase("tr"));
        input.value = [base, ...candidates.slice(0, 2)].filter(Boolean).join(", ");
      }
    }
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><h2>Tarife sorgusu tamamlanamadı</h2><p>${escapeHtml(error.message)}</p></div>`;
  } finally { button.disabled = false; }
});

function collectTariffCostInputs(invoice) {
  // Tarife & Maliyet formundaki maliyet kalemleri; tek satır hesabı ve tasarruf önerisi aynı gövdeyi kullanır.
  return {
    invoice_value: invoice, freight: nullableNumber("#tariffFreight") || 0,
    insurance: nullableNumber("#tariffInsurance") || 0, other_costs: nullableNumber("#tariffOtherCosts") || 0,
    quantity: nullableNumber("#tariffQuantity"), currency: $("#tariffCurrency").value,
    vat_rate: nullableNumber("#tariffVat"), payment_method: $("#tariffPayment").value || null,
    additional_financial_liability_rate: nullableNumber("#tariffEmy"), kkdf_rate: nullableNumber("#tariffKkdf"),
    anti_dumping_amount: nullableNumber("#tariffAntiDumping"), sct_amount: nullableNumber("#tariffSct"),
    surveillance_unit_value: nullableNumber("#tariffSurveillance"),
    has_surveillance_certificate: $("#tariffSurveillanceCertificate").value === "" ? null : $("#tariffSurveillanceCertificate").value === "true",
    ...liraFields("tariff"),
  };
}

function savingsVariantLabel(item) {
  if (item.variant === "atr") return "A.TR ile";
  return item.atr_available || item.atr_certificate === true ? "A.TR'siz" : "—";
}

function renderSavingsResult(data) {
  exportStore.savings = data;
  const currency = data.currency || "";
  const money = (value) => value == null ? "—" : `${numberFormat.format(value)} ${escapeHtml(currency)}`;
  const ranked = data.ranked || [];
  const rows = ranked.map((item) => {
    const saving = item.is_baseline
      ? "<em>temel senaryo</em>"
      : item.savings_vs_baseline == null ? "—"
        : `<span class="${item.savings_vs_baseline < 0 ? "savings-negative" : ""}">${item.savings_vs_baseline >= 0 ? "" : "−"}${numberFormat.format(Math.abs(item.savings_vs_baseline))} ${escapeHtml(currency)}${item.savings_pct != null ? ` <small>(%${numberFormat.format(Math.abs(item.savings_pct))})</small>` : ""}</span>`;
    const pessimistic = item.landed_total_pessimistic != null
      ? `<small>tevsik yoksa ${numberFormat.format(item.landed_total_pessimistic)} ${escapeHtml(currency)}</small>` : "";
    const conditions = (item.conditions || []).length ? `<ul class="savings-list">${item.conditions.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>` : "—";
    const warnings = (item.warnings || []).slice(0, 3).map((text) => escapeHtml(text)).join(" · ") || "—";
    return `<tr class="${item.rank === 1 ? "savings-best" : ""}${item.is_baseline ? " savings-baseline" : ""}">
      <td>${item.rank}</td>
      <td><b>${escapeHtml(item.origin_country)}</b>${item.dispatch_country ? `<small>sevk: ${escapeHtml(item.dispatch_country)}</small>` : ""}${item.resolved_country_group ? `<small>sütun ${escapeHtml(item.resolved_country_group)}</small>` : ""}</td>
      <td>${escapeHtml(savingsVariantLabel(item))}</td>
      <td>${money(item.total_taxes)}</td>
      <td>${money(item.landed_total)}${pessimistic}</td>
      <td>${saving}</td>
      <td>${conditions}</td>
      <td>${warnings}</td>
    </tr>`;
  }).join("");
  const notComparable = (data.not_comparable || []).map((item) => `<li><b>${escapeHtml(item.origin_country)}</b>${item.variant === "atr" ? " (A.TR ile)" : ""}: ${escapeHtml((item.reasons || []).join(" ") || "karşılaştırılamadı")}</li>`).join("");
  const best = data.best;
  const head = best
    ? `<div class="answer-head"><span class="answer-status">${ranked.length} senaryo</span><div><h2>En düşük toplam maliyet: ${escapeHtml(best.origin_country)} (${escapeHtml(savingsVariantLabel(best))})</h2><p>${best.is_baseline ? "Temel senaryo zaten en düşük maliyetli görünüyor." : `Temel senaryoya göre ${numberFormat.format(best.savings_vs_baseline ?? 0)} ${escapeHtml(currency)} fark; koşullar sağlanmadan tasarruf gerçekleşmez.`}${data.baseline_note ? ` ${escapeHtml(data.baseline_note)}` : ""}</p></div></div>`
    : `<p class="missing-list">Karşılaştırılabilir senaryo yok; aşağıdaki sebepleri giderip yeniden deneyin.</p>`;
  return `${head}
  ${ranked.length ? `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Sıra</th><th>Menşe</th><th>Varyant</th><th>Vergiler toplamı</th><th>Toplam maliyet</th><th>Temel senaryoya göre tasarruf</th><th>Koşullar (belge)</th><th>Uyarılar</th></tr></thead><tbody>${rows}</tbody></table></div>
  ${exportBar("savings", [{ table: "savings", label: "Tasarruf tablosu" }])}` : ""}
  ${notComparable ? `<h4>Karşılaştırılamayan senaryolar</h4><ul class="savings-list">${notComparable}</ul>` : ""}
  ${data.legal_notice ? `<div class="legal-banner"><strong>Önemli:</strong> ${escapeHtml(data.legal_notice)}</div>` : ""}`;
}

function renderScenarioRows(data) {
  exportStore.scenarios = data;
  const fmt = (value) => value == null ? "kod başına değişiyor" : `%${numberFormat.format(value)}`;
  return `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Menşe</th><th>Sütun</th><th>Gümrük vergisi</th><th>İGV / ek vergi</th><th>Tercih belgesi</th><th>Not</th></tr></thead><tbody>${(data.rows || []).map((row) => {
    const docs = row.origin_documents;
    const docText = docs ? (docs.documents || []).map((item) => item.name).join(", ") : "—";
    const notes = [];
    if (row.origin_recognised === false) notes.push("Menşe ülke tanınmadı; 'Diğer Ülkeler' varsayıldı");
    if (row.atr_free_circulation) notes.push("GV: A.TR ile serbest dolaşım sütunu; İGV/EMY menşeye göre");
    else if (row.atr_available) notes.push("A.TR teyit edilirse GV serbest dolaşım sütunundan hesaplanır");
    if ((row.origin_proof_required || []).length) {
      const fallback = Object.entries(row.fallback_rates || {}).map(([key, value]) => `${tariffMeasureLabels[key] || key} %${numberFormat.format(value)}`).join(", ");
      notes.push(`İGV/EMY tercihi menşe tevsikine bağlı${fallback ? ` (tevsik yoksa ${fallback})` : ""}`);
    }
    notes.push(...(row.warnings || []).filter((item) => !item.includes("kapsam matrisi")).slice(0, 1));
    if (row.unambiguous_rates?.customs_duty == null) notes.push("Oran bütün alt GTİP12 satırlarında ortak değil");
    return `<tr><td><b>${escapeHtml(row.origin_country)}</b><small>${escapeHtml(docs?.regime_name || "")}</small></td><td>${escapeHtml(row.resolved_country_group || "—")}</td><td>${escapeHtml(fmt(row.unambiguous_rates?.customs_duty))}</td><td>${escapeHtml(fmt(row.unambiguous_rates?.additional_duty))}</td><td>${escapeHtml(docText)}</td><td>${escapeHtml(notes.join(" · ") || "—")}</td></tr>`;
  }).join("")}</tbody></table></div>
  ${exportBar("scenarios", [{ table: "scenarios", label: "Senaryo tablosu" }])}
  <p class="rate-warning">Senaryo satırları resmî tarife arşivinin güncel snapshot'ından ve belge kural tablosundan üretilir; bağlayıcı tarife bilgisi değildir.</p>`;
}

function renderBulkResult(data) {
  exportStore.bulk = data;
  const money = (value, currency) => value == null ? "—" : `${numberFormat.format(value)} ${escapeHtml(currency || "")}`;
  const pct = (value) => value == null ? "—" : `%${numberFormat.format(value)}`;
  const statusLabel = { complete: "tam", partial: "eksik girdi", error: "hata" };
  const rows = (data.rows || []).map((row) => `<tr class="bulk-${escapeHtml(row.status)}">
      <td>${escapeHtml(row.line ?? row.index)}</td>
      <td><code>${escapeHtml(row.gtip || "—")}</code>${row.description ? `<small>${escapeHtml(row.description)}</small>` : ""}</td>
      <td>${escapeHtml(row.origin_country || "—")}${row.dispatch_country ? `<small>sevk: ${escapeHtml(row.dispatch_country)}</small>` : ""}${row.origin_recognised === false ? "<small>menşe tanınmadı</small>" : ""}</td>
      <td>${escapeHtml(statusLabel[row.status] || row.status)}</td>
      <td>${pct(row.rates?.customs_duty)} / ${pct(row.rates?.additional_duty)}</td>
      <td>${money(row.customs_value, row.currency)}</td>
      <td>${money(row.total_taxes, row.currency)}</td>
      <td>${money(row.landed_total, row.currency)}${row.landed_total_try != null ? `<small>${numberFormat.format(row.landed_total_try)} TL</small>` : ""}</td>
      <td>${escapeHtml(row.error || (row.missing_rates || []).join(", ") || (row.warnings || [])[0] || "")}</td>
    </tr>`).join("");
  const totals = (data.totals || []).map((item) => `<div class="formula-line"><span><strong>${escapeHtml(item.currency)}</strong> <small>${item.complete_rows}/${item.rows} satır tam</small></span><code>kıymet ${numberFormat.format(item.customs_value)} · vergi ${numberFormat.format(item.total_taxes)} · toplam ${numberFormat.format(item.landed_total)}</code></div>`).join("");
  const summary = data.summary || {};
  return `<div class="answer-head"><span class="answer-status${summary.errors ? " warning" : ""}">${summary.rows || 0} satır</span><div><h2>Toplu hesap</h2><p>${summary.complete || 0} tam · ${summary.partial || 0} eksik girdi · ${summary.errors || 0} hata</p></div></div>
    <div class="scenario-table-wrap"><table class="evidence-table bulk-table"><thead><tr><th>Satır</th><th>GTİP</th><th>Menşe</th><th>Durum</th><th>GV / İGV</th><th>Gümrük kıymeti</th><th>Toplam vergi</th><th>Genel toplam</th><th>Not</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${exportBar("bulk", [{ table: "bulk", label: "Toplu hesap" }])}
    ${totals ? `<div class="formula-ledger"><h3>Para birimi bazında toplam (yalnız tam satırlar)</h3>${totals}</div>` : ""}
    ${data.legal_notice ? `<div class="legal-banner"><strong>Önemli:</strong> ${escapeHtml(data.legal_notice)}</div>` : ""}`;
}

$("#bulkCalculate")?.addEventListener("click", async () => {
  const output = $("#bulkOutput");
  if (!hasCapability("bulk_costing")) { output.innerHTML = featureUpsellHtml("bulk_costing"); return; }
  const file = $("#bulkFile")?.files?.[0];
  if (!file) return showToast("Önce bir CSV veya XLSX dosyası seçin.");
  if (file.size > 2 * 1024 * 1024) return showToast("Dosya 2 MB sınırını aşıyor.");
  output.innerHTML = '<div class="answer-loading"><strong>Satırlar hesaplanıyor</strong><span>Her satır için resmî tarife satırı ve maliyet defteri üretiliyor…</span></div>';
  try {
    const fileDataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Dosya okunamadı."));
      reader.readAsDataURL(file);
    });
    const data = await fetchJson("/api/tariff/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_name: file.name, file_data_url: fileDataUrl }),
    });
    output.innerHTML = renderBulkResult(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("#scenarioCompare").addEventListener("click", async () => {
  const output = $("#scenarioOutput");
  if (!hasCapability("scenario_compare")) { output.innerHTML = featureUpsellHtml("scenario_compare"); return; }
  const gtip = $("#tariffGtip").value.trim();
  const origins = $("#scenarioOrigins").value.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 6);
  if (!gtip || origins.length < 2) {
    output.innerHTML = '<p class="missing-list">Karşılaştırma için tarife kodu ve en az iki menşe ülke gerekir.</p>';
    return;
  }
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Senaryolar karşılaştırılıyor</b><span>Her menşe için resmî sütun ve belge kuralı denetleniyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/tariff/scenarios", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gtip, origins, dispatch_country: $("#tariffDispatch")?.value.trim() || null, atr_certificate: $("#tariffAtr")?.value || null }),
    });
    output.innerHTML = renderScenarioRows(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("#scenarioSavings")?.addEventListener("click", async () => {
  const output = $("#savingsOutput");
  if (!hasCapability("scenario_compare")) { output.innerHTML = featureUpsellHtml("scenario_compare"); return; }
  const gtip = $("#tariffGtip").value.trim();
  const origins = $("#scenarioOrigins").value.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 6);
  const invoice = nullableNumber("#tariffInvoice");
  if (!gtip || origins.length < 2) {
    output.innerHTML = '<p class="missing-list">Tasarruf önerisi için tarife kodu ve en az iki menşe ülke gerekir.</p>';
    return;
  }
  if (invoice == null) {
    output.innerHTML = '<p class="missing-list">Tasarruf önerisi için Tarife & Maliyet formuna fatura bedelini ve doğrulanmış maliyet kalemlerini girin.</p>';
    return;
  }
  const baseOrigin = $("#tariffOrigin").value.trim();
  const baseline = origins.find((item) => item.toLocaleLowerCase("tr") === baseOrigin.toLocaleLowerCase("tr")) || origins[0];
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Senaryolar maliyete göre sıralanıyor</b><span>Her menşe (ve A.TR varyantı) için maliyet defteri üretiliyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/tariff/savings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gtip, origins, dispatch_country: $("#tariffDispatch")?.value.trim() || null, atr_certificate: $("#tariffAtr")?.value || null,
        baseline_origin: baseline, cost: collectTariffCostInputs(invoice),
      }),
    });
    output.innerHTML = renderSavingsResult(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

const FOREIGN_MEASURE_LABELS = {
  third_country_duty: "Üçüncü ülke gümrük vergisi", preference: "Tercihli tarife", customs_union_duty: "Gümrük birliği vergisi",
  suspension: "Vergi askıya alma", quota: "Tarife kontenjanı", anti_dumping: "Damping önlemi", countervailing: "Telafi edici vergi",
  safeguard: "Korunma önlemi", prohibition: "İthalat yasağı", restriction: "İthalat kontrolü / kısıtlama", vat: "KDV",
  excise: "Özel tüketim", supplementary_unit: "Tamamlayıcı ölçü birimi", other: "Diğer önlem",
};

function foreignLinksHtml(links) {
  if (!links || !links.length) return "";
  return `<ul class="savings-list">${links.map((item) => `<li><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>${item.note ? ` <small>${escapeHtml(item.note)}</small>` : ""}</li>`).join("")}</ul>`;
}

function renderForeignTariff(data) {
  const cards = (data.results || []).map((item) => {
    const rows = (item.measures || []).slice(0, 12).map((measure) => `<tr>
      <td>${escapeHtml(FOREIGN_MEASURE_LABELS[measure.kind] || measure.measure_type || "—")}</td>
      <td>${escapeHtml(measure.geographical_area || measure.geographical_area_id || "—")}</td>
      <td>${escapeHtml(measure.duty_expression || "—")}</td>
    </tr>`).join("");
    const candidates = (item.candidates || []).slice(0, 8).map((c) => `<li><b>${escapeHtml(c.code)}</b> — ${escapeHtml(c.description)}</li>`).join("");
    const rateBlock = item.data_kind === "nomenclature"
      ? `${item.matched_code ? `<p><b>Tarife numarası:</b> ${escapeHtml(item.matched_code)} — ${escapeHtml(item.description || "")}</p>` : ""}
         ${candidates ? `<ul class="savings-list">${candidates}</ul>` : ""}
         <p class="missing-list">Bu ülke resmî açık verisinde <b>vergi oranı yayımlamıyor</b>; oran için aşağıdaki resmî sorgu ekranını kullanın.</p>`
      : item.data_kind === "api"
      ? `<p><b>Üçüncü ülke vergisi:</b> ${escapeHtml(item.third_country_duty || "—")}${item.origin_preference ? ` &nbsp;|&nbsp; <b>Tercihli oran (${escapeHtml(item.origin_preference.geographical_area || item.origin_preference.geographical_area_id || "")}):</b> ${escapeHtml(item.origin_preference.duty_expression || "—")}` : ""}</p>
         ${item.matched_code ? `<p><small>Eşleşen kod: <b>${escapeHtml(item.matched_code)}</b> — ${escapeHtml(item.description || "")} (${item.match_quality === "exact_hs6" ? "HS-6 tam eşleşme" : "yalnız pozisyon düzeyinde"})</small></p>` : ""}
         ${rows ? `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Önlem</th><th>Ülke / grup</th><th>Oran</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}`
      : `<p class="missing-list">Bu ülke resmî açık veri yayımlamadığı için oran otomatik alınamaz; aşağıdaki resmî sorgu bağlantılarını kullanın.</p>`;
    const notes = (item.notes || []).filter(Boolean).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
    return `<section class="answer-section">
      <h4>${escapeHtml(item.label)} <small>${escapeHtml(item.authority || "")}</small></h4>
      ${rateBlock}
      ${foreignLinksHtml(item.links)}
      ${item.bti_url ? `<p><small><a href="${escapeHtml(item.bti_url)}" target="_blank" rel="noopener noreferrer">Bağlayıcı tarife kararı bilgisi</a></small></p>` : ""}
      ${notes ? `<ul class="savings-list">${notes}</ul>` : ""}
    </section>`;
  }).join("");
  const warnings = (data.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<div class="answer-head"><span class="answer-status">HS-6: ${escapeHtml(data.hs6 || "")}</span><div><h2>Yurt dışı tarife karşılaştırma</h2><p>${escapeHtml(data.comparability_note || "")}</p></div></div>
    ${warnings ? `<ul class="savings-list">${warnings}</ul>` : ""}
    ${cards}
    <p class="rate-warning">${escapeHtml(data.calculation_note || "")}</p>`;
}

function renderEbtiResults(data) {
  const hits = data.hits || [];
  if (!hits.length) {
    return `<p class="missing-list">Bu kod için AB Bağlayıcı Tarife Bilgisi kararı bulunamadı.${(data.warnings || []).length ? ` ${escapeHtml(data.warnings.join(" "))}` : ""}</p>`;
  }
  const rows = hits.map((hit) => {
    const validity = [hit.valid_from, hit.valid_to].filter(Boolean).join(" – ") || "—";
    const invalid = String(hit.status || "").toUpperCase() !== "VALID";
    return `<tr${invalid ? ' class="savings-negative"' : ""}>
      <td><a href="${escapeHtml(hit.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(hit.reference)}</a><br><small>${escapeHtml(hit.issuing_country)} · ${escapeHtml(hit.language || "")}${invalid ? " · geçersiz" : ""}</small></td>
      <td><b>${escapeHtml(hit.code)}</b><br><small>${escapeHtml(validity)}</small></td>
      <td>${escapeHtml(String(hit.description || "").slice(0, 300))}</td>
      <td><small>${escapeHtml(String(hit.justification || "").slice(0, 300))}</small></td>
    </tr>`;
  }).join("");
  return `<div class="answer-head"><span class="answer-status">${hits.length} karar</span><div><h2>AB Bağlayıcı Tarife Bilgisi kararları</h2><p>${escapeHtml(data.binding_note || "")}</p></div></div>
    <div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Karar</th><th>Kod / geçerlilik</th><th>Eşya tanımı</th><th>Sınıflandırma gerekçesi</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

const EU_TARIC_KIND_LABELS = {
  third_country_duty: "Üçüncü ülke gümrük vergisi", customs_union_duty: "Gümrük birliği vergisi",
  preference: "Tercihli tarife", suspension: "Vergi askıya alma", quota: "Tarife kontenjanı",
  anti_dumping: "Damping önlemi", countervailing: "Telafi edici vergi", safeguard: "Korunma önlemi",
  additional_duty: "Ek vergi", agricultural_component: "Tarım bileşeni", prohibition: "İthalat yasağı",
  restriction: "İthalat kontrolü / kısıtlama", surveillance: "Gözetim", other: "Diğer önlem",
};

function renderEuTaric(data) {
  if (data.status === "disabled") {
    return `<p class="missing-list">${escapeHtml((data.warnings || []).join(" ") || "AB TARIC sorgusu şu anda kapalı.")}</p>`;
  }
  if (data.status !== "ok") {
    return `<p class="missing-list">${escapeHtml((data.warnings || []).join(" ") || "AB TARIC verisi alınamadı.")}</p>`;
  }
  const s = data.summary || {};
  const rows = (s.measures || []).slice(0, 15).map((m) => `<tr>
    <td>${escapeHtml(EU_TARIC_KIND_LABELS[m.kind] || m.measure_type || "—")}</td>
    <td>${escapeHtml(m.partner_area || m.partner_area_code || "—")}</td>
    <td>${escapeHtml(m.duty_text || "—")}</td>
    <td><small>${escapeHtml([...(m.documents || []), ...(m.additional_codes || [])].join(", ") || "—")}</small></td>
  </tr>`).join("");
  const extras = (s.additional_duties || []).map((m) => `<li><b>${escapeHtml(EU_TARIC_KIND_LABELS[m.kind] || m.measure_type)}</b> — ${escapeHtml(m.partner_area || "")} ${escapeHtml(m.duty_text || "")}</li>`).join("");
  const docs = (s.required_documents || []).map((d) => escapeHtml(d)).join(", ");
  return `<div class="answer-head"><span class="answer-status">${escapeHtml(s.snapshot_month || "")} anlık görüntüsü</span><div><h2>AB TARIC önlemleri — ${escapeHtml(data.goods_code)}</h2><p>${escapeHtml(s.goods_description || "")}</p></div></div>
    <section class="answer-section">
      <p><b>Üçüncü ülke vergisi:</b> ${escapeHtml(s.mfn_rate || "—")}
         &nbsp;|&nbsp; <b>${escapeHtml(data.partner_country || "")} için oran:</b> ${escapeHtml(s.partner_rate || "—")}${s.partner_rate_kind ? ` <small>(${escapeHtml(EU_TARIC_KIND_LABELS[s.partner_rate_kind] || s.partner_rate_kind)})</small>` : ""}</p>
      ${docs ? `<p><b>Gereken belge:</b> ${docs}</p>` : ""}
      ${extras ? `<h4>Ek vergiler</h4><ul class="savings-list">${extras}</ul>` : ""}
      ${rows ? `<div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>Önlem</th><th>Ülke / grup</th><th>Oran</th><th>Belge / ek kod</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <p class="rate-warning">${escapeHtml(data.conditional_note || "")}</p>
      <p class="rate-warning">${escapeHtml(data.customs_union_note || "")}</p>
      <p><small>${escapeHtml(data.source_note || "")}${data.from_archive ? ` (arşivden, son alınma: ${escapeHtml(String(data.fetched_at || "").slice(0, 10))})` : ""}</small></p>
      ${(data.warnings || []).length ? `<ul class="savings-list">${data.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
    </section>`;
}

$("#euTaricLookup")?.addEventListener("click", async () => {
  const output = $("#euTaricOutput");
  if (!hasCapability("foreign_tariff")) { output.innerHTML = featureUpsellHtml("foreign_tariff"); return; }
  const gtip = $("#tariffGtip").value.trim();
  if (!gtip || gtip.replace(/\D/g, "").length < 6) {
    output.innerHTML = '<p class="missing-list">AB sorgusu için en az 6 haneli bir GTİP girin.</p>';
    return;
  }
  const params = new URLSearchParams({ gtip });
  const origin = $("#tariffOrigin")?.value.trim();
  if (origin) params.set("origin", origin);
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>AB TARIC sorgulanıyor</b><span>Komisyonun resmî aylık ham verisinden önlemler çözümleniyor…</span></div></div>';
  try {
    const data = await fetchJson(`/api/foreign/eu-taric?${params.toString()}`);
    output.innerHTML = renderEuTaric(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("#ebtiSearch")?.addEventListener("click", async () => {
  const output = $("#ebtiOutput");
  if (!hasCapability("foreign_tariff")) { output.innerHTML = featureUpsellHtml("foreign_tariff"); return; }
  const gtip = $("#tariffGtip").value.trim();
  if (!gtip || gtip.replace(/\D/g, "").length < 4) {
    output.innerHTML = '<p class="missing-list">AB karar araması için en az 4 haneli bir kod girin.</p>';
    return;
  }
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>AB kararları aranıyor</b><span>Komisyonun günlük BTB yayınından süzülüyor…</span></div></div>';
  try {
    const data = await fetchJson(`/api/foreign/ebti?gtip=${encodeURIComponent(gtip)}`);
    output.innerHTML = renderEbtiResults(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

$("#foreignCompare")?.addEventListener("click", async () => {
  const output = $("#foreignOutput");
  if (!hasCapability("foreign_tariff")) { output.innerHTML = featureUpsellHtml("foreign_tariff"); return; }
  const gtip = $("#tariffGtip").value.trim();
  if (!gtip || gtip.replace(/\D/g, "").length < 6) {
    output.innerHTML = '<p class="missing-list">Karşılaştırma için en az 6 haneli bir GTİP/HS kodu girin.</p>';
    return;
  }
  const params = new URLSearchParams({ gtip });
  const origin = $("#tariffOrigin")?.value.trim();
  if (origin) params.set("origin", origin);
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Yurt dışı tarifeler sorgulanıyor</b><span>Birleşik Krallık resmî API\'si okunuyor, AB ve İsviçre için resmî sorgu bağlantıları hazırlanıyor…</span></div></div>';
  try {
    const data = await fetchJson(`/api/foreign/tariff?${params.toString()}`);
    output.innerHTML = renderForeignTariff(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-open-plans]")) openAccount("plans");
});

function printPrecheckLocally() {
  document.body.classList.add("print-dossier");
  const closedDetails = document.querySelectorAll(".answer-sheet details:not([open])");
  closedDetails.forEach((el) => el.setAttribute("open", "true"));
  const cleanup = () => {
    document.body.classList.remove("print-dossier");
    closedDetails.forEach((el) => el.removeAttribute("open"));
    window.removeEventListener("afterprint", cleanup);
  };
  window.addEventListener("afterprint", cleanup);
  window.print();
}

async function ensurePlanCatalog() {
  if (state.plans && state.featureLabels) return;
  try {
    const plans = await fetchJson("/api/plans");
    state.plans = plans.plans || [];
    state.featureLabels = plans.features || {};
  } catch (_) {
    // Etiketler yüklenemezse genel "Bu özellik" metniyle devam edilir.
  }
}

async function showPdfReportUpsell(button) {
  const sheet = button.closest(".answer-sheet");
  if (!sheet || sheet.parentElement?.querySelector('.feature-locked[data-feature="pdf_report"]')) return;
  await ensurePlanCatalog();
  const wrapper = document.createElement("div");
  wrapper.innerHTML = featureUpsellHtml("pdf_report");
  const node = wrapper.firstElementChild;
  if (!node) return;
  node.dataset.feature = "pdf_report";
  node.insertAdjacentHTML("beforeend", "<p>Tarayıcı yazdırma penceresi açılıyor; sunucu tarafı rapor her sayfada zorunlu yasal alt bilgi ve kaynak defteriyle üretilir.</p>");
  sheet.insertAdjacentElement("afterend", node);
}

async function downloadPrecheckPdf(button) {
  const result = state.currentCustomsResult;
  if (!result) { showToast("İndirilecek ön değerlendirme dosyası bulunamadı."); return; }
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "PDF hazırlanıyor…";
  try {
    let response;
    try {
      response = await fetch("/api/customs/report.pdf", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/pdf" },
        body: JSON.stringify({ result }),
      });
    } catch (_) {
      throw new Error("Sunucuya ulaşılamadı. Bağlantınızı kontrol edip tekrar deneyin.");
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(data.error || "PDF raporu oluşturulamadı.");
      error.code = data.code;
      throw error;
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = match ? match[1] : "gumruksor-on-degerlendirme.pdf";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    showToast("PDF raporu zorunlu yasal alt bilgiyle indirildi.");
  } catch (error) {
    showToast(error.message || "PDF raporu oluşturulamadı.");
    if (error.code === "feature_required" || error.code === "authentication_required") {
      if (error.code === "feature_required") await showPdfReportUpsell(button);
      printPrecheckLocally();
    }
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("#printPrecheck");
  if (!button) return;
  if (state.auth?.authenticated && hasCapability("pdf_report")) {
    await downloadPrecheckPdf(button);
    return;
  }
  if (state.auth?.authenticated) await showPdfReportUpsell(button);
  printPrecheckLocally();
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("#emailPrecheck");
  if (!button) return;
  const result = state.currentCustomsResult;
  if (!result) { showToast("Gönderilecek ön değerlendirme dosyası bulunamadı."); return; }
  if (!state.auth?.authenticated) { showToast("E-posta ile göndermek için Google ile giriş yapın; dosya kendi adresinize gider."); return; }
  if (state.auth?.email_enabled === false) { showToast("E-posta gönderimi sunucuda henüz yapılandırılmadı; dosyayı PDF olarak kaydedebilirsiniz."); return; }
  button.disabled = true;
  try {
    const data = await fetchJson("/api/email/precheck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result),
    });
    showToast(`${data.recipient} adresine gönderildi; spam klasörünü de kontrol edin.`);
  } catch (error) {
    showToast(error.message || "E-posta gönderilemedi.");
  } finally {
    button.disabled = false;
  }
});

const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const documentMimeByExtension = { pdf: "application/pdf", docx: DOCX_MIME, jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

function documentMimeType(file) {
  const extension = (file.name || "").split(".").pop().toLowerCase();
  const byExtension = documentMimeByExtension[extension];
  // Tarayıcılar .docx için bazen boş ya da octet-stream tür bildirir; uzantıya göre düzelt.
  if (!file.type || file.type === "application/octet-stream") return byExtension || "";
  return file.type;
}

function readFileAsDataUrl(file, mimeType) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      resolve(mimeType ? result.replace(/^data:[^;]*;base64,/, `data:${mimeType};base64,`) : result);
    };
    reader.onerror = () => reject(new Error("Dosya okunamadı."));
    reader.readAsDataURL(file);
  });
}

$("#ingestSource")?.addEventListener("click", async () => {
  const output = $("#ingestOutput");
  const url = $("#ingestUrl").value.trim();
  const file = $("#ingestPdf")?.files?.[0];
  if (!url && !file) { output.innerHTML = '<p class="missing-list">Belge adresi girin veya PDF / Word (.docx) dosyası seçin.</p>'; return; }
  if (url && file) { output.innerHTML = '<p class="missing-list">Yalnızca bir kaynak belirtin: adres veya dosya.</p>'; return; }
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Belge okunuyor</b><span>Ürün metni çıkarılıyor, onayınıza sunulacak…</span></div></div>';
  try {
    let body;
    if (file) {
      if (file.size > 10 * 1024 * 1024) throw new Error("Belge en fazla 10 MB olabilir.");
      const mimeType = documentMimeType(file);
      if (!["application/pdf", DOCX_MIME].includes(mimeType)) throw new Error("Yalnızca PDF veya Word (.docx) dosyası yüklenebilir; eski .doc dosyasını Word'de .docx olarak kaydedin.");
      body = { pdf_data_url: await readFileAsDataUrl(file, mimeType) };
    } else {
      body = { url };
    }
    const data = await fetchJson("/api/customs/ingest-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const structured = data.structured || {};
    const structuredNote = data.extraction === "structured"
      ? `<p class="missing-list">Sayfadan ürün verisi okundu: <b>${escapeHtml([structured.brand, structured.name].filter(Boolean).join(" · ") || data.title || "")}</b>${structured.attributes?.length ? ` · ${structured.attributes.length} özellik` : ""}</p>`
      : (data.source_type === "url" ? '<p class="missing-list">Sayfada yapılandırılmış ürün verisi bulunamadı; görünen metin alındı. Gereksiz kısımları silip yalnızca ürünle ilgili bölümü bırakın.</p>' : "");
    const pagesBadge = data.source_kind === "pdf_pages"
      ? `<p><span class="source-badge" data-kind="pdf_pages">${escapeHtml(data.badge || "Taranmış/çizim PDF'i sayfa görseli olarak analiz edildi")} · ${data.pages_used || 0}/${data.page_count || data.pages_used || 0} sayfa</span></p>`
      : "";
    const attributeAction = data.source_kind === "pdf_pages" && data.attributes
      ? '<button type="button" class="secondary-action" id="ingestApplyAttributes">Evsafları forma aktar</button>'
      : "";
    output.innerHTML = `
      ${pagesBadge}
      <p class="missing-list">${escapeHtml(data.warning || "")}</p>
      ${structuredNote}
      <textarea class="ingest-textarea" id="ingestText">${escapeHtml(data.text)}</textarea>
      <div class="result-actions"><button type="button" id="ingestAppend">Ürün tanımına ekle</button>${attributeAction}</div>
      ${data.truncated ? '<p class="missing-list">Belge uzun olduğu için metin kısaltıldı.</p>' : ""}`;
    $("#ingestAppend")?.addEventListener("click", () => {
      const target = $("#productDescription");
      const text = ($("#ingestText")?.value || "").trim();
      if (!target || !text) return;
      target.value = `${target.value ? `${target.value.trimEnd()} ` : ""}${text}`.slice(0, 2000);
      target.dispatchEvent(new Event("input", { bubbles: true }));
      showToast("Belge metni ürün tanımına eklendi; gözden geçirip onaylayın.");
    });
    $("#ingestApplyAttributes")?.addEventListener("click", () => {
      // Sayfa görsellerinden çıkarılan evsaflar, fotoğraf analiziyle aynı inceleme
      // satırlarına gider; GTİP alanına hiçbir şey yazılmaz.
      state.customsDocumentPages = { pages_used: data.pages_used || 0 };
      state.customsVisionResult = data.attributes;
      applyVisionAttributes(data.attributes);
      setVisionState(
        "review",
        `${data.warning || ""} Alanları düzeltin; araştırma ancak onayınızdan sonra başlar.`,
        `PDF sayfa analizi (${data.pages_used || 0} sayfa) · güven: ${confidenceLabel(data.attributes.confidence)}`,
      );
      $("#attributeReview")?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(describeRequestError(error, "Belge okunamadı."))}</p></div>`;
  }
});

$("#verifyBrandModel")?.addEventListener("click", async () => {
  const output = $("#brandModelOutput");
  const brand = ($("#brandInput")?.value || "").trim();
  const model = ($("#modelInput")?.value || "").trim();
  const url = ($("#ingestUrl")?.value || "").trim();
  if (!brand && !model) { output.innerHTML = '<p class="missing-list">Doğrulanacak marka ve/veya model girin.</p>'; return; }
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Sayfa okunuyor</b><span>Marka/model metni sayfayla karşılaştırılıyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/customs/brand-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand, model, url: url || undefined }),
    });
    if (data.status === "source_required") {
      output.innerHTML = `<p class="missing-list">${escapeHtml(data.message || "")}</p><ul class="missing-list">${(data.suggestions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
      return;
    }
    const match = data.match || {};
    const verdictLabel = { match: "Eşleşti", partial: "Kısmen eşleşti", no_match: "Eşleşmedi" }[match.verdict] || "Belirsiz";
    output.innerHTML = `
      <p><span class="match-score" data-verdict="${escapeHtml(match.verdict || "")}">${verdictLabel} · ${match.score ?? 0}/100</span> <small>${escapeHtml(data.title || data.url || "")}</small></p>
      <ul class="missing-list">${(match.evidence || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
      <p class="missing-list">${escapeHtml(data.warning || "")}</p>
      <div class="result-actions"><button type="button" class="secondary-action" id="brandModelApply">Marka/modeli evsaf alanına yaz</button></div>`;
    $("#brandModelApply")?.addEventListener("click", () => {
      const target = $("#brandModel");
      if (!target) return;
      target.value = [brand, model].filter(Boolean).join(" / ").slice(0, 300);
      target.dispatchEvent(new Event("input", { bubbles: true }));
      showToast("Marka/model evsaf alanına yazıldı; GTİP seçimi size aittir.");
    });
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  }
});

const shippingReviewFields = [
  ["document_number", "Belge numarası"],
  ["document_date", "Belge tarihi"],
  ["shipper", "Gönderici (shipper)"],
  ["consignee", "Alıcı (consignee)"],
  ["notify_party", "İhbar adresi (notify)"],
  ["carrier", "Taşıyıcı"],
  ["vessel_or_flight", "Gemi / uçuş"],
  ["port_of_loading", "Yükleme limanı"],
  ["port_of_discharge", "Boşaltma limanı"],
  ["place_of_delivery", "Teslim yeri"],
  ["country_of_origin", "Menşe ülke (belgede yazılıysa)"],
  ["country_of_dispatch", "Sevk ülkesi"],
  ["packages_count", "Kap adedi"],
  ["package_type", "Kap türü"],
  ["gross_weight_kg", "Brüt ağırlık (kg)"],
  ["net_weight_kg", "Net ağırlık (kg)"],
  ["volume_cbm", "Hacim (m³)"],
  ["incoterm", "Teslim şekli (Incoterm)"],
  ["freight_terms", "Navlun ödeme (prepaid/collect)"],
  ["invoice_total", "Fatura tutarı"],
  ["currency", "Fatura para birimi"],
  ["freight_amount", "Navlun tutarı"],
  ["freight_currency", "Navlun para birimi (belgede ayrıysa)"],
  ["insurance_amount", "Sigorta tutarı"],
  ["insurance_currency", "Sigorta para birimi (belgede ayrıysa)"],
  ["payment_terms", "Ödeme şekli (belgedeki ifade)"],
  ["payment_method", "Ödeme şekli (KKDF için normalize)"],
  ["quantity", "Miktar"],
  ["quantity_unit", "Miktar birimi"],
];

// Sunucudaki PAYMENT_METHOD_LABELS ile aynı: normalize anahtar → formdaki Türkçe ödeme şekli.
const SHIPPING_PAYMENT_METHOD_LABELS = {
  cash_in_advance: "Peşin",
  cash_against_goods: "Mal mukabili",
  cash_against_documents: "Vesaik mukabili",
  letter_of_credit: "Akreditif",
  acceptance_credit: "Kabul kredili",
};

function shippingFieldValue(key) {
  return ($(`[data-shipping-field="${key}"]`)?.value || "").trim();
}

function fillIfEmpty(selector, value) {
  const input = $(selector);
  if (!input || !value || input.value.trim()) return false;
  input.value = value;
  input.classList.add("vision-filled");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
}

function fillNumberIfEmpty(selector, value) {
  const number = parseLocalizedNumber(String(value ?? ""));
  if (number == null) return false;
  return fillIfEmpty(selector, localizedNumberFormat.format(number));
}

// "Belgeden alındı, doğrulayın" rozeti: alan belgeden dolduruldu; kullanıcı dokununca rozet kalkar.
function markFromDocument(selector) {
  const input = $(selector);
  if (!input || input.dataset.fromDocument) return;
  input.dataset.fromDocument = "1";
  const label = input.closest("label.field");
  if (label && !label.querySelector(".doc-badge")) {
    const badge = document.createElement("small");
    badge.className = "doc-badge";
    badge.textContent = "belgeden alındı, doğrulayın";
    badge.title = "Bu değer yüklenen sevkiyat belgesinden okundu; belgeyle karşılaştırıp onaylayın.";
    label.appendChild(badge);
  }
  const clear = () => {
    delete input.dataset.fromDocument;
    label?.querySelector(".doc-badge")?.remove();
    input.removeEventListener("input", clear);
    input.removeEventListener("change", clear);
  };
  input.addEventListener("input", clear);
  input.addEventListener("change", clear);
}

function fillFromDocument(selector, value, numeric = false) {
  const done = numeric ? fillNumberIfEmpty(selector, value) : fillIfEmpty(selector, value);
  if (done) markFromDocument(selector);
  return done;
}

function applyShippingDocument() {
  const filled = [];
  const skipped = [];
  const goods = shippingFieldValue("goods_description");
  const description = $("#productDescription");
  if (goods && description) {
    const prefix = description.value.trim() ? `${description.value.trimEnd()}\nBelgedeki eşya tanımı: ` : "";
    description.value = `${prefix}${goods}`.slice(0, 2000);
    description.classList.add("vision-filled");
    description.dispatchEvent(new Event("input", { bubbles: true }));
    filled.push("ürün tanımı");
  }
  if (fillFromDocument("#originCountry", shippingFieldValue("country_of_origin"))) filled.push("menşe ülke");
  if (fillFromDocument("#dispatchCountry", shippingFieldValue("country_of_dispatch"))) filled.push("sevk ülkesi");
  if (fillFromDocument("#incoterm", shippingFieldValue("incoterm").toUpperCase())) filled.push("teslim şekli");
  // Para birimi: seçim kutusunun her zaman bir değeri vardır; yalnız fatura bedeli de boşken (henüz
  // maliyet girilmemişken) belgedeki fatura para birimine çekilir, kullanıcının seçimi ezilmez.
  const invoiceWasEmpty = fillFromDocument("#invoiceValue", shippingFieldValue("invoice_total"), true);
  if (invoiceWasEmpty) filled.push("fatura bedeli");
  const currency = shippingFieldValue("currency").toUpperCase();
  const currencySelect = $("#currency");
  const currencyKnown = Boolean(currency && currencySelect && Array.from(currencySelect.options).some((option) => option.value === currency));
  if (currencyKnown && invoiceWasEmpty && currencySelect.value !== currency) {
    currencySelect.value = currency;
    currencySelect.dispatchEvent(new Event("change", { bubbles: true }));
    markFromDocument("#currency");
    filled.push("para birimi");
  } else if (currency && !currencyKnown) {
    skipped.push(`para birimi ${currency} formda seçilemiyor`);
  }
  // Navlun / sigorta: belgede ayrı para birimi yazıyorsa ve fatura para biriminden farklıysa
  // toplanamaz; alan boş bırakılır ve kullanıcıya bildirilir.
  const formCurrency = currencySelect?.value || currency;
  for (const [amountKey, currencyKey, selector, label] of [
    ["freight_amount", "freight_currency", "#freight", "navlun"],
    ["insurance_amount", "insurance_currency", "#insurance", "sigorta"],
  ]) {
    const itemCurrency = shippingFieldValue(currencyKey).toUpperCase();
    const amount = shippingFieldValue(amountKey);
    if (amount && itemCurrency && formCurrency && itemCurrency !== formCurrency) {
      skipped.push(`${label} ${itemCurrency} cinsinden, form para birimi ${formCurrency}`);
      continue;
    }
    if (fillFromDocument(selector, amount, true)) filled.push(label);
  }
  if (fillFromDocument("#quantity", shippingFieldValue("quantity"), true)) filled.push("miktar");
  // Ödeme şekli: yalnız normalize anahtar tanındıysa Türkçe etiketi yazılır (KKDF önerisi maliyet
  // motorunda bu etiketten türetilir; KKDF oranı forma yazılmaz, kullanıcı doğrular).
  // Navlun ödeme şekli (prepaid/collect) ödeme yöntemi değildir; #paymentMethod'a yazılmaz.
  const paymentLabel = SHIPPING_PAYMENT_METHOD_LABELS[shippingFieldValue("payment_method")] || "";
  if (fillFromDocument("#paymentMethod", paymentLabel)) filled.push("ödeme şekli");
  if (!$("#customsQuestion").value.trim()) {
    $("#customsQuestion").value = "Bu sevkiyattaki eşyanın aday GTİP'i, ithalat vergileri, TAREKS/TSE kontrolleri, gerekli belgeleri ve toplam maliyeti nelerdir?";
  }
  updateReadiness();
  const skippedNote = skipped.length ? ` Aktarılmadı: ${skipped.join("; ")}.` : "";
  showToast(filled.length
    ? `Forma aktarıldı: ${filled.join(", ")}. Boş olmayan alanlar korundu; "belgeden alındı" rozetli alanları doğrulayın.${skippedNote}`
    : `Aktarılacak yeni alan bulunamadı; form alanları zaten dolu.${skippedNote}`);
}

function renderShippingReview(data) {
  const output = $("#shippingOutput");
  const rows = shippingReviewFields.map(([key, label]) => {
    const value = data[key];
    const shown = value == null ? "" : String(value);
    if (key === "payment_method") {
      const options = [["", "Belgede yazmıyor / tanınmadı"], ...Object.entries(SHIPPING_PAYMENT_METHOD_LABELS)]
        .map(([code, text]) => `<option value="${escapeHtml(code)}"${code === shown ? " selected" : ""}>${escapeHtml(text)}</option>`)
        .join("");
      return `<label class="field"><span>${escapeHtml(label)}</span><select data-shipping-field="${key}">${options}</select></label>`;
    }
    return `<label class="field"><span>${escapeHtml(label)}</span><input data-shipping-field="${key}" maxlength="300" value="${escapeHtml(shown)}"></label>`;
  }).join("");
  const hsCodes = Array.isArray(data.hs_codes) ? data.hs_codes : [];
  const containers = Array.isArray(data.containers) ? data.containers : [];
  const unreadable = Array.isArray(data.unreadable_fields) ? data.unreadable_fields : [];
  output.innerHTML = `
    <p class="missing-list"><b>${escapeHtml(data.document_type_label || "Sevkiyat belgesi")}</b> · yapay zekâ okuması · güven: ${escapeHtml(confidenceLabel(data.confidence))}</p>
    <p class="missing-list">${escapeHtml(data.warning || "")}</p>
    <label class="field"><span>Eşya tanımı (belgedeki haliyle)</span><textarea data-shipping-field="goods_description" maxlength="2000" class="ingest-textarea">${escapeHtml(data.goods_description || "")}</textarea></label>
    <div class="shipping-review-grid">${rows}</div>
    ${containers.length ? `<p class="missing-list">Konteynerler: ${escapeHtml(containers.join(", "))}</p>` : ""}
    ${data.marks_and_numbers ? `<p class="missing-list">Marka / numara: ${escapeHtml(data.marks_and_numbers)}</p>` : ""}
    ${hsCodes.length ? `<p class="rate-warning">Belgede yazan HS kodları (yalnızca öneri, forma yazılmadı): ${escapeHtml(hsCodes.join(", "))}. Doğruysa "Seçilen HS / CN / Türk GTİP" alanına elle girin.</p>` : ""}
    ${unreadable.length ? `<p class="missing-list">Belgede okunamayan / bulunmayan alanlar: ${escapeHtml(unreadable.join(", "))}</p>` : ""}
    <div class="result-actions"><button type="button" id="shippingApply">Ürün dosyasına aktar</button></div>`;
  $("#shippingApply")?.addEventListener("click", applyShippingDocument);
}

$("#shippingRead")?.addEventListener("click", async () => {
  const output = $("#shippingOutput");
  const button = $("#shippingRead");
  const file = $("#shippingFile")?.files?.[0];
  if (!file) { output.innerHTML = '<p class="missing-list">Konşimento, fatura veya çeki listesi dosyası seçin.</p>'; return; }
  if (file.size > 10 * 1024 * 1024) { output.innerHTML = '<p class="missing-list">Belge en fazla 10 MB olabilir.</p>'; return; }
  const allowed = ["application/pdf", DOCX_MIME, "image/jpeg", "image/png", "image/webp"];
  const mimeType = documentMimeType(file);
  if (!allowed.includes(mimeType)) { output.innerHTML = '<p class="missing-list">Yalnızca PDF, Word (.docx), JPEG, PNG veya WebP yüklenebilir; eski .doc dosyasını Word\'de .docx olarak kaydedin.</p>'; return; }
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Belge okunuyor</b><span>Gönderici, alıcı, eşya, kap/ağırlık ve fatura alanları çıkarılıyor…</span></div></div>';
  button.disabled = true;
  try {
    const documentDataUrl = await readFileAsDataUrl(file, mimeType);
    const data = await fetchJson("/api/customs/ingest-shipping-document", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_data_url: documentDataUrl }),
      timeoutMs: AI_ANALYSIS_TIMEOUT_MS,
    });
    renderShippingReview(data);
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(describeRequestError(error, "Sevkiyat belgesi okunamadı."))}</p></div>`;
  } finally {
    button.disabled = false;
  }
});

function renderRuleDocuments(rule) {
  const documents = rule.required_documents || [];
  const exemptions = rule.exemptions || [];
  if (!documents.length && !exemptions.length && !rule.required_documents_excerpt) return "";
  const documentList = documents.length
    ? `<ol class="document-list">${documents.map((item) => `<li class="document-${escapeHtml(item.kind)}">${escapeHtml(item.text)}${item.kind === "conditional" ? " <small>şarta bağlı</small>" : ""}</li>`).join("")}</ol>`
    : (rule.required_documents_excerpt ? `<p>${escapeHtml(rule.required_documents_excerpt)}</p>` : "");
  const exemptionList = exemptions.length
    ? `<details class="advanced-fields"><summary><span>Muafiyet / istisna / kapsam dışı hükümleri</span><small>${exemptions.length} cümle · resmî metinden</small></summary><ul class="missing-list">${exemptions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul><p class="rate-warning">Bu cümleler ürünün kapsam dışı olduğunu kanıtlamaz; ürün, ithalatçı ve rejim şartları resmî metinden doğrulanmalıdır.</p></details>`
    : "";
  return `<details class="advanced-fields"><summary><span>Yüklenmesi gereken belgeler</span><small>${documents.length ? `${documents.length} satır` : "resmî metinden"}</small></summary>${documentList}</details>${exemptionList}`;
}

function renderControlTool(data) {
  const cards = (data.matches || []).map((match) => {
    const rule = match.rule;
    return `<article class="control-card"><header><code>${escapeHtml(rule.code)}</code><b>${escapeHtml(rule.title)}</b><a href="${safeUrl(rule.source_url)}" target="_blank" rel="noreferrer">Resmî metin ↗</a></header>
      <dl><div><dt>Ek-1 eşleşmesi</dt><dd>${escapeHtml(match.matched_scope.gtip_prefix)} · ${escapeHtml(match.match_type)}</dd></div><div><dt>Sistem</dt><dd>${escapeHtml(rule.system)}</dd></div><div><dt>Fiilî denetim</dt><dd>${rule.risk_based ? "Risk analizine bağlı" : "Yetkili kurum kararı"}</dd></div><div><dt>Laboratuvar</dt><dd>${rule.laboratory_test_possible ? "Mümkün; otomatik değil" : "Metinde tespit edilmedi"}</dd></div></dl>
      <p><strong>Kapsam satırı:</strong> ${escapeHtml(match.matched_scope.source_line)}<br>${escapeHtml(match.assessment)}</p>
      ${renderRuleDocuments(rule)}
      <div class="result-caution">${match.cautions.map((item) => escapeHtml(item)).join(" · ")}</div></article>`;
  }).join("");
  return `<div class="answer-head"><span class="answer-status${data.status === "matched" ? "" : " warning"}">${escapeHtml(data.status)}</span><div><h2>${escapeHtml(data.gtip)} kontrol dosyası</h2><p>${escapeHtml(data.as_of)} itibarıyla indekslenmiş resmî tebliğ ekleri · kapsam: ${escapeHtml(data.scope_determination || "belirsiz")} · fiilî denetim sonucu bu sistemde belirlenmez</p></div></div>
    ${cards || '<p class="missing-list">İndekslenen güncel Ek-1 listelerinde eşleşme bulunamadı.</p>'}
    ${(data.warnings || []).length ? `<div class="result-caution">${data.warnings.map((item) => escapeHtml(item)).join(" · ")}</div>` : ""}`;
}

function renderControlSearchResults(data) {
  const results = data.results || [];
  if (!results.length) {
    return `<div class="answer-head"><span class="answer-status warning">0 sonuç</span><div><h2>“${escapeHtml(data.query || "")}” için denetim kapsamı bulunamadı</h2><p>İndekslenen 2026 ÜGD Ek-1 listelerinde bu kelimeye veya GTİP'e ait kayıt eşleşmedi. GTİP kodunu 4, 6 veya 12 hane olarak yazabilir ya da Tebliğ Fihristinden inceleyebilirsiniz.</p></div></div>`;
  }
  const cards = results.map((item) => {
    const rule = item.rule || {};
    return `<article class="control-card">
      <header>
        <code>${escapeHtml(rule.code || "ÜGD")}</code>
        <b>${escapeHtml(rule.title || "")}</b>
        ${rule.source_url ? `<a href="${safeUrl(rule.source_url)}" target="_blank" rel="noreferrer">Resmî metin ↗</a>` : ""}
      </header>
      <dl>
        <div><dt>Eşleşen GTİP / Kapsam</dt><dd><b>${escapeHtml(item.gtip || "")}</b></dd></div>
        <div><dt>Sistem</dt><dd>${escapeHtml(rule.system || "TAREKS")}</dd></div>
        <div><dt>Eşya Tanımı</dt><dd>${escapeHtml(item.commodity_name || "—")}</dd></div>
        <div><dt>Denetim Şekli</dt><dd>${rule.risk_based ? "Risk analizine bağlı fiilî denetim" : "Belge kontrolü / Heyet"}</dd></div>
        <div><dt>Laboratuvar</dt><dd>${rule.laboratory_test_possible ? "Mümkün; otomatik değil" : "Metinde tespit edilmedi"}</dd></div>
      </dl>
      ${renderRuleDocuments(rule)}
    </article>`;
  }).join("");

  return `<div class="answer-head">
    <span class="answer-status">${results.length} eşleşme</span>
    <div>
      <h2>“${escapeHtml(data.query || "")}” arama sonuçları</h2>
      <p>2026 Ürün Güvenliği ve Denetimi tebliğlerinde bulunan ürün ve GTİP kapsamları</p>
    </div>
  </div>
  ${cards}`;
}

$("#controlsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const output = $("#controlsOutput");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Kontrol tebliğleri taranıyor</b><span>Ek-1 kapsamı ile risk sonucu taranıyor…</span></div></div>';
  const query = $("#controlsGtip").value.trim();
  const digits = query.replace(/\D/g, "");
  try {
    let data;
    if (digits.length >= 4 && digits.length <= 12 && digits.length === query.replace(/[\s.]/g, "").length) {
      data = await fetchJson("/api/controls/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gtip: digits }) });
      output.innerHTML = renderControlTool(data);
    } else {
      const searchRes = await fetchJson(`/api/controls/search?q=${encodeURIComponent(query)}`);
      output.innerHTML = renderControlSearchResults(searchRes);
    }
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><h2>Kontrol sorgusu tamamlanamadı</h2><p>${escapeHtml(error.message)}</p></div>`;
  } finally { button.disabled = false; }
});

function saveLocalScenario(data) {
  if (!data) return;
  try {
    const items = savedScenarios();
    const title = data.inquiry?.product_description || $("#productDescription")?.value?.trim() || "İthalat ön değerlendirmesi";
    const gtip = data.inquiry?.candidate_gtip || $("#candidateGtip")?.value?.trim() || "";
    const origin = data.inquiry?.origin_country || $("#originCountry")?.value?.trim() || "";
    const newEntry = {
      title,
      gtip,
      origin,
      savedAt: new Date().toISOString(),
      result: data,
    };
    const filtered = items.filter((x) => !(x.gtip === gtip && x.title === title));
    filtered.unshift(newEntry);
    safeStorage.setItem("gumrukce-scenarios", JSON.stringify(filtered.slice(0, 25)));
    renderWatchList();
  } catch (_) {}
}

function savedWatchItems() {
  try { return JSON.parse(safeStorage.getItem("gumrukce-watchlist") || "[]"); } catch (_) { return []; }
}

function savedScenarios() {
  try { return JSON.parse(safeStorage.getItem("gumrukce-scenarios") || "[]"); } catch (_) { return []; }
}

function watchlistOnServer() {
  return Boolean(state.auth?.authenticated);
}

function renderWatchChanges(item) {
  const changes = item.changes || [];
  if (!changes.length) return "";
  const rows = changes.slice(0, 20).map((change) => `<tr><td><code>${escapeHtml(change.gtip)}</code></td><td>${escapeHtml(change.measure_label || change.measure_type)}</td><td>${escapeHtml(change.country_group)}</td><td>${escapeHtml(change.before ?? "—")}</td><td>${escapeHtml(change.after ?? "— (satır kaldırıldı)")}</td></tr>`).join("");
  return `<details class="advanced-fields watch-changes"><summary><span>${escapeHtml(item.change_count || changes.length)} resmî satır değişikliği</span><small>${escapeHtml(changes[0].source_title || "")} · yeni sürüm ${escapeHtml(changes[0].new_snapshot || "")}</small></summary>
    <div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>GTİP</th><th>Önlem</th><th>Sütun</th><th>Önce</th><th>Sonra</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

function renderWatchItems(items, { server }) {
  const target = $("#watchList");
  if (!items.length) {
    target.innerHTML = `<p class="missing-list">${server ? "Hesabınızda izlenen GTİP yok." : "Bu cihazda izlenen GTİP yok. Google ile giriş yaparsanız liste hesabınızda saklanır ve resmî satır değiştiğinde e-posta alırsınız."}</p>`;
    return;
  }
  target.innerHTML = items.map((item, index) => `<div class="watch-item${item.change_count ? " has-changes" : ""}"><code>${escapeHtml(item.gtip)}</code><span>${escapeHtml(item.label || "Adsız ürün")}${item.change_count ? `<small class="watch-badge">${escapeHtml(item.change_count)} değişiklik</small>` : ""}${item.origin_country ? `<small>menşe: ${escapeHtml(item.origin_country)}</small>` : ""}</span><button type="button" data-watch-query="${index}">Kontrol et</button><button type="button" data-watch-remove="${index}" data-watch-id="${escapeHtml(item.id || "")}">Kaldır</button>${renderWatchChanges(item)}</div>`).join("");
}

async function migrateLocalWatchItems() {
  const local = savedWatchItems();
  if (!local.length || !watchlistOnServer()) return;
  try {
    await fetchJson("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: local.map((item) => ({ gtip: item.gtip, label: item.label || "" })) }) });
    safeStorage.removeItem("gumrukce-watchlist");
    showToast(`${local.length} izlenen GTİP hesabınıza taşındı.`);
  } catch (_) {
    // Local copy stays until the server accepts it.
  }
}

async function renderWatchList() {
  const scenarios = savedScenarios();
  $("#scenarioList").innerHTML = scenarios.length ? scenarios.map((item, index) => `<div class="watch-item"><code>${escapeHtml(item.gtip || "GTİP yok")}</code><span>${escapeHtml(item.title)}<small>${escapeHtml(formatDate(item.savedAt, true))} · ${escapeHtml(item.origin || "menşe yok")}</small></span><button type="button" data-scenario-open="${index}">Aç</button><button type="button" data-scenario-remove="${index}">Kaldır</button></div>`).join("") : '<p class="missing-list">Henüz kayıtlı ön değerlendirme yok.</p>';
  if (!watchlistOnServer()) {
    state.watchItems = savedWatchItems();
    renderWatchItems(state.watchItems, { server: false });
    return;
  }
  try {
    await migrateLocalWatchItems();
    const data = await fetchJson("/api/watchlist");
    state.watchItems = data.items || [];
    renderWatchItems(state.watchItems, { server: true });
    const note = $("#watchNotifyNote");
    if (note) note.textContent = data.email_enabled
      ? "İzlenen GTİP'in resmî tarife satırı yeni sürümde değişirse e-posta adresinize bildirim gönderilir."
      : "E-posta bildirimi sunucuda henüz yapılandırılmadı; değişiklikler bu listede görünür.";
  } catch (error) {
    state.watchItems = savedWatchItems();
    renderWatchItems(state.watchItems, { server: false });
  }
}

$("#addWatch").addEventListener("click", async () => {
  const gtip = $("#watchGtip").value.replace(/\D/g, "");
  if (![4, 6, 8, 10, 12].includes(gtip.length)) return showToast("İzleme için 4, 6, 8, 10 veya 12 haneli GTİP girin.");
  const label = $("#watchLabel").value.trim();
  if (watchlistOnServer()) {
    try {
      await fetchJson("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gtip, label }) });
      showToast("GTİP hesabınızdaki izleme listesine eklendi.");
      renderWatchList();
    } catch (error) { showToast(error.message); }
    return;
  }
  const items = savedWatchItems();
  if (!items.some((item) => item.gtip === gtip)) items.push({ gtip, label, addedAt: new Date().toISOString() });
  safeStorage.setItem("gumrukce-watchlist", JSON.stringify(items.slice(-100)));
  renderWatchList();
  showToast("GTİP bu cihazdaki izleme listesine eklendi.");
});

$("#watchList").addEventListener("click", async (event) => {
  const remove = event.target.closest("[data-watch-remove]");
  const query = event.target.closest("[data-watch-query]");
  if (remove) {
    if (watchlistOnServer() && remove.dataset.watchId) {
      try { await fetchJson(`/api/watchlist/${encodeURIComponent(remove.dataset.watchId)}`, { method: "DELETE" }); } catch (error) { showToast(error.message); }
    } else {
      const items = savedWatchItems(); items.splice(Number(remove.dataset.watchRemove), 1);
      safeStorage.setItem("gumrukce-watchlist", JSON.stringify(items));
    }
    renderWatchList();
  }
  if (query) {
    const item = (state.watchItems || [])[Number(query.dataset.watchQuery)];
    if (!item) return;
    const digits = String(item.gtip).replace(/\D/g, "");
    if (digits.length === 12) { $("#controlsGtip").value = digits; switchCustomsView("controls"); $("#controlsForm").requestSubmit(); return; }
    $("#tariffGtip").value = digits; if (item.origin_country) $("#tariffOrigin").value = item.origin_country; switchCustomsView("tariff"); $("#tariffForm").requestSubmit();
  }
});

$("#scenarioList").addEventListener("click", (event) => {
  const open = event.target.closest("[data-scenario-open]");
  const remove = event.target.closest("[data-scenario-remove]");
  const items = savedScenarios();
  if (remove) {
    items.splice(Number(remove.dataset.scenarioRemove), 1);
    safeStorage.setItem("gumrukce-scenarios", JSON.stringify(items)); renderWatchList();
  }
  if (open) {
    const item = items[Number(open.dataset.scenarioOpen)];
    if (!item?.result) return;
    switchCustomsView("assistant");
    $("#customsResult").hidden = false;
    renderCustomsResult(item.result);
    $("#customsResult").scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

async function loadChanges() {
  const output = $("#changesOutput");
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Sürüm defteri okunuyor</b><span>Tarife ve kontrol snapshot’ları karşılaştırılıyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/changes");
    const controlRows = data.controls || [];
    const changeSourceLabels = { import_regime: "İthalat Rejimi Kararı", additional_duty: "İlave Gümrük Vergisi (İGV) Kararı" };
    const changeStatusLabels = { no_previous_snapshot: "İlk sürüm arşivlendi", compared: "İki sürüm karşılaştırıldı" };
    const tariffSummaries = Object.entries(data.tariff || {}).map(([name, value]) => {
      const sourceLabel = changeSourceLabels[name] || name;
      const statusText = changeStatusLabels[value.status] || value.status || (value.changes?.length ? "değişiklik var" : "tek sürüm");
      const total = value.total_changes ?? value.changes?.length ?? 0;
      const note = value.status === "no_previous_snapshot"
        ? "Karşılaştırılacak ikinci resmî sürüm henüz arşivlenmedi; sonraki sürüm güncellemesinden sonra satır farkları burada listelenir."
        : `${total} satır farkı${value.new_snapshot ? ` · yeni sürüm ${value.new_snapshot}` : ""}`;
      const rows = (value.changes || []).slice(0, 50).map((change) => `<tr><td><code>${escapeHtml(change.gtip)}</code></td><td>${escapeHtml(tariffMeasureLabels[change.measure_type] || change.measure_type)}</td><td>${escapeHtml(change.country_group)}</td><td>${escapeHtml(change.before ?? "—")}</td><td>${escapeHtml(change.after ?? "— (satır kaldırıldı)")}</td></tr>`).join("");
      const table = rows ? `<details class="advanced-fields"><summary><span>Satır farkları</span><small>${Math.min(total, 50)} / ${total}</small></summary><div class="scenario-table-wrap"><table class="evidence-table"><thead><tr><th>GTİP</th><th>Önlem</th><th>Sütun</th><th>Önce</th><th>Sonra</th></tr></thead><tbody>${rows}</tbody></table></div></details>` : "";
      return `<article class="candidate-card"><code>${escapeHtml(sourceLabel)}</code><b>${escapeHtml(statusText)}</b><p>${escapeHtml(note)}</p>${table}</article>`;
    }).join("");
    const tradeStatus = data.trade_measure_status || {};
    const tradeDatasets = Object.entries(tradeStatus.datasets || {}).map(([kind, meta]) => `<div class="formula-line"><span><strong>${escapeHtml(meta.label || TRADE_MEASURE_LABELS[kind] || kind)}</strong><br><small>${escapeHtml(meta.origin === "synced" ? `resmî eşitleme ${String(meta.fetched_at || "").slice(0, 16).replace("T", " ")}` : meta.origin === "seed" ? "depo tohum verisi (ilk eşitleme bekleniyor)" : "yüklü değil")}</small></span><code>${escapeHtml(meta.item_count ?? 0)} satır</code></div>`).join("");
    const lastSync = tradeStatus.last_sync?.finished_at ? `Son eşitleme ${escapeHtml(String(tradeStatus.last_sync.finished_at).slice(0, 16).replace("T", " "))}` : "Eşitleme henüz çalışmadı";
    const tradeRows = (data.trade_measures || []).map((change) => {
      const detail = change.detail || {};
      const items = [...(detail.added || []).map((item) => `<li><b>Yeni:</b> ${escapeHtml(item.summary)}</li>`), ...(detail.removed || []).map((item) => `<li><b>Kaldırıldı:</b> ${escapeHtml(item.summary)}</li>`), ...(detail.modified || []).map((item) => `<li><b>Değişti:</b> ${escapeHtml(item.before)} → ${escapeHtml(item.after)}</li>`)].slice(0, 40).join("");
      return `<details class="advanced-fields"><summary><span>${escapeHtml(detail.label || TRADE_MEASURE_LABELS[change.kind] || change.kind)}</span><small>${escapeHtml(String(change.changed_at || "").slice(0, 10))} · +${escapeHtml(change.added)} / −${escapeHtml(change.removed)} / ~${escapeHtml(change.modified)}</small></summary><ul class="missing-list">${items}</ul></details>`;
    }).join("");
    const tradeLedger = `<div class="formula-ledger"><h3>Damping, korunma, gözetim ve İthalat Tebliğleri (günlük eşitleme)</h3>${tradeDatasets}<p class="rate-warning">${lastSync}. Her gün Ticaret Bakanlığı ve mevzuat.gov.tr'den yeniden okunur; farklar aşağıda ve izleme listesi bildirimlerinde görünür.</p>${tradeRows || '<p class="missing-list">Önlem listelerinde henüz kaydedilmiş fark yok.</p>'}</div>`;
    const review = data.review || {};
    const reviewStrip = Number(review.pending_count || 0) > 0
      ? `<p class="rate-warning review-strip">${escapeHtml(review.pending_count)} yeni resmî veri sürümü editör incelemesinde; onaylanana kadar sorgular önceki onaylı sürümle yanıtlanır.</p>`
      : "";
    output.innerHTML = `${reviewStrip}<div class="candidate-grid">${tariffSummaries || '<p class="missing-list">Tarife sürümü henüz yok.</p>'}</div>${tradeLedger}
      <div class="formula-ledger"><h3>Kontrol tebliği değişiklikleri</h3>${controlRows.length ? controlRows.map((item) => `<div class="formula-line"><span><strong>${escapeHtml(item.code)}</strong> · ${escapeHtml(item.title)}<br><small>${escapeHtml(item.changed_at)}</small></span><code>${item.scope_count_delta > 0 ? "+" : ""}${escapeHtml(item.scope_count_delta)}</code></div>`).join("") : '<p class="missing-list">Karşılaştırılabilir ikinci tebliğ sürümü henüz oluşmadı.</p>'}</div>`;
  } catch (error) { output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`; }
}

$("#refreshChanges").addEventListener("click", loadChanges);
renderWatchList();

const savedTheme = safeStorage.getItem("ticaret-bilgi-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("#themeToggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  safeStorage.setItem("ticaret-bilgi-theme", next);
});

loadCatalogStatus();
loadAuthState();
loadCountryList();
async function fetchCustomsRate(prefix) {
  const currencyEl = $(prefix === "tariff" ? "#tariffCurrency" : "#currency");
  const dateEl = $(`#${prefix}ExchangeRateDate`);
  const rateEl = $(`#${prefix}ExchangeRate`);
  const note = $(`#${prefix}FxNote`);
  const button = document.querySelector(`.fx-fetch[data-prefix="${prefix}"]`);
  const currency = currencyEl?.value || "USD";
  const show = (message) => { if (note) { note.hidden = false; note.textContent = message; } };
  if (currency === "TRY") {
    rateEl.value = "1";
    show("Fatura Türk lirası ise kur 1 alınır.");
    return;
  }
  const params = new URLSearchParams({ currency });
  if (dateEl?.value) params.set("date", dateEl.value);
  if (button) button.disabled = true;
  try {
    const data = await fetchJson(`/api/tariff/exchange-rate?${params}`);
    rateEl.value = String(data.rate).replace(".", ",");
    rateEl.classList.remove("input-invalid");
    if (dateEl && !dateEl.value) dateEl.value = data.registration_date;
    show(`${data.note} Dayanak: ${data.legal_basis}.`);
  } catch (error) {
    show(`Kur alınamadı: ${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}
document.querySelectorAll(".fx-fetch").forEach((button) => {
  button.addEventListener("click", () => fetchCustomsRate(button.dataset.prefix));
});

async function loadDeclarationStatus() {
  const hint = $("#declarationHint");
  if (!hint) return;
  try {
    const data = await fetchJson("/api/customs/declaration/status");
    hint.textContent = data.configured ? (state.auth?.authenticated ? "Hazır" : "Giriş gerekir") : "Sunucuda Eylemio hesabı tanımlı değil";
  } catch { hint.textContent = "Durum alınamadı"; }
}
async function queryDeclaration() {
  const output = $("#declarationOutput");
  const button = $("#declarationQuery");
  const number = ($("#declarationNo")?.value || "").trim();
  if (!number) { output.innerHTML = '<div class="answer-error"><p>Beyanname numarasını girin.</p></div>'; return; }
  button.disabled = true;
  output.innerHTML = '<div class="analysis-loading"><i></i><div><b>Beyanname sorgulanıyor</b><span>Eylemio üzerinden Ticaret Bakanlığı servisine bağlanılıyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/customs/declaration", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ declaration_no: number }) });
    const rows = (data.summary || []).map((pair) => `<div class="formula-line"><span>${escapeHtml(pair.label)}</span><code>${escapeHtml(pair.value)}</code></div>`).join("");
    output.innerHTML = `<div class="formula-ledger"><h3>${escapeHtml(data.declaration_no)} · ${escapeHtml(data.message || "sorgu tamamlandı")}</h3>${rows || '<p class="missing-list">Konektör alan döndürmedi.</p>'}<details class="advanced-fields"><summary><span>Ham yanıt</span><small>${escapeHtml(data.account?.name || "")}</small></summary><pre class="raw-json">${escapeHtml(JSON.stringify(data.data || {}, null, 2))}</pre></details><p class="rate-warning">${escapeHtml(data.source || "")}</p></div>`;
  } catch (error) {
    output.innerHTML = `<div class="answer-error"><p>${escapeHtml(error.message)}</p></div>`;
  } finally { button.disabled = false; }
}
$("#declarationQuery")?.addEventListener("click", queryDeclaration);
$("#declarationNo")?.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); queryDeclaration(); } });
async function initMarketplaceStatus() {
  try {
    const data = await fetchJson("/api/consultants");
    const enabled = data.enabled !== false;
    state.consultantsMarketplace = enabled;
    document.body.classList.toggle("marketplace-enabled", enabled);
  } catch (_) {}
}

loadDeclarationStatus();
initMarketplaceStatus();

// Autocomplete logic
function setupAutocomplete(inputEl, dropdownEl, onSelect) {
  if (!inputEl || !dropdownEl) return;
  let debounceTimer = null;
  let activeIndex = -1;
  let currentItems = [];

  const closeDropdown = () => {
    dropdownEl.hidden = true;
    dropdownEl.innerHTML = "";
    activeIndex = -1;
    currentItems = [];
  };

  inputEl.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const val = inputEl.value.trim();
    if (val.length < 2) {
      closeDropdown();
      return;
    }
    debounceTimer = setTimeout(async () => {
      try {
        const res = await fetchJson(`/api/tariff/autocomplete?q=${encodeURIComponent(val)}`);
        const items = res.results || [];
        currentItems = items;
        if (!items.length) {
          closeDropdown();
          return;
        }
        dropdownEl.innerHTML = items.map((item, idx) => `
          <div class="autocomplete-item" data-idx="${idx}">
            <span class="ac-code">${escapeHtml(item.code)}</span>
            <span class="ac-name">${escapeHtml(item.name)}</span>
            <span class="ac-badge">${escapeHtml(item.category || (item.chapter ? `Fasıl ${item.chapter}` : ""))}</span>
          </div>
        `).join("");
        dropdownEl.hidden = false;
        activeIndex = -1;

        dropdownEl.querySelectorAll(".autocomplete-item").forEach((el) => {
          el.addEventListener("click", () => {
            const idx = Number(el.dataset.idx);
            const selected = currentItems[idx];
            if (selected) {
              inputEl.value = selected.code;
              inputEl.dispatchEvent(new Event("input", { bubbles: true }));
              closeDropdown();
              if (onSelect) onSelect(selected);
            }
          });
        });
      } catch (_) {
        closeDropdown();
      }
    }, 220);
  });

  inputEl.addEventListener("keydown", (e) => {
    if (dropdownEl.hidden || !currentItems.length) return;
    const items = dropdownEl.querySelectorAll(".autocomplete-item");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % items.length;
      items.forEach((item, i) => item.classList.toggle("active", i === activeIndex));
      items[activeIndex]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + items.length) % items.length;
      items.forEach((item, i) => item.classList.toggle("active", i === activeIndex));
      items[activeIndex]?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      items[activeIndex]?.click();
    } else if (e.key === "Escape") {
      closeDropdown();
    }
  });

  document.addEventListener("click", (e) => {
    if (!inputEl.contains(e.target) && !dropdownEl.contains(e.target)) {
      closeDropdown();
    }
  });
}

setupAutocomplete($("#candidateGtip"), $("#candidateGtipDropdown"), (item) => {
  showToast(`${item.code} seçildi.`);
});

setupAutocomplete($("#tariffGtip"), $("#tariffGtipDropdown"), (item) => {
  showToast(`${item.code} seçildi.`);
});

setupAutocomplete($("#controlsGtip"), $("#controlsGtipDropdown"), (item) => {
  showToast(`${item.code} seçildi.`);
});

// Direct classify button for text-only inquiry in Gümrükçe'ye Sor
$("#directClassifyButton")?.addEventListener("click", async () => {
  const desc = $("#productDescription").value.trim();
  if (desc.length < 5) {
    showToast("Lütfen önce ürün tanımını (ürün adı, malzemesi, işlevi vb.) yazın.");
    $("#productDescription").focus();
    return;
  }
  const btn = $("#directClassifyButton");
  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span>Aday GTİP’ler taranıyor…</span>';
  try {
    setVisionState(
      "confirmed",
      "Ürün tanım evsafları doğrulanıyor. En yakın GTİP adayları ve vergi oranları taranıyor...",
      "Metin tabanlı sınıflandırma"
    );
    const classification = await classifyApprovedProduct();
    if (classification.candidates?.length) {
      showToast(`${classification.candidates.length} aday tarife kodu bulundu.`);
      setFlowStep(3);
      $("#gtipSuggestions").scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      showToast("Aday kod için ürün tanımı biraz daha detaylandırılmalı.");
    }
  } catch (error) {
    showToast(error.message || "Sınıflandırma gerçekleştirilemedi.");
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
});

// Quick pills in search
$$(".quick-pill").forEach((button) => {
  button.addEventListener("click", () => {
    const q = button.dataset.query;
    if (q) {
      queryInput.value = q;
      form.requestSubmit();
    }
  });
});

// Controls mode switch & communiques catalog
$("#controlsModeSearch")?.addEventListener("click", () => {
  $("#controlsModeSearch").classList.add("active");
  $("#controlsModeCatalog")?.classList.remove("active");
  $("#controlsForm").hidden = false;
  $("#controlsOutput").hidden = false;
  $("#communiquesCatalog").hidden = true;
});

$("#controlsModeCatalog")?.addEventListener("click", async () => {
  $("#controlsModeCatalog").classList.add("active");
  $("#controlsModeSearch")?.classList.remove("active");
  $("#controlsForm").hidden = true;
  $("#controlsOutput").hidden = true;
  $("#communiquesCatalog").hidden = false;
  await loadCommuniquesCatalog();
});

async function loadCommuniquesCatalog() {
  const container = $("#communiquesCatalog");
  if (!container) return;
  if (container.dataset.loaded === "true") return;
  container.innerHTML = '<div class="analysis-loading"><i></i><div><b>2026 ÜGD Tebliğ Fihristi yükleniyor</b><span>24 tebliğ, TAREKS ve TSE kapsamları getiriliyor…</span></div></div>';
  try {
    const data = await fetchJson("/api/controls/communiques");
    container.dataset.loaded = "true";
    const communiques = data.communiques || [];
    container.innerHTML = `
      <div class="answer-head">
        <span class="answer-status">${communiques.length} tebliğ</span>
        <div>
          <h2>2026 Yılı İthalatta Ürün Güvenliği ve Denetimi (ÜGD) Tebliğleri</h2>
          <p>Ticaret Bakanlığı koordinasyonunda TAREKS, TSE, Tarım ve Çevre denetim sistemlerine tabi tüm resmi tebliğler</p>
        </div>
      </div>
      <div class="communique-catalog-grid">
        ${communiques.map((c) => {
          const sys = (c.system || "").toLowerCase();
          const sysClass = sys.includes("tse") ? "tse" : (sys.includes("tar") ? "tarim" : "tareks");
          const docs = (c.required_documents || []).slice(0, 3);
          return `
            <div class="communique-card">
              <div class="communique-header">
                <span class="communique-no">${escapeHtml(c.code)}</span>
                <span class="communique-system-badge ${sysClass}">${escapeHtml(c.system || "TAREKS")}</span>
              </div>
              <div class="communique-title">${escapeHtml(c.title)}</div>
              <div class="communique-stats">
                <strong>${c.scope_count}</strong> denetime tabi GTİP kapsamı
                ${c.official_gazette_date ? ` · RG: ${escapeHtml(c.official_gazette_date)}` : ""}
              </div>
              ${docs.length ? `
                <div class="communique-docs">
                  <b>Zorunlu / İbraz Edilecek Belgeler:</b>
                  <ul>${docs.map((d) => `<li>${escapeHtml(d.text)}</li>`).join("")}</ul>
                </div>
              ` : ""}
              <div class="card-actions" style="margin-top:auto; display:flex; gap:6px;">
                <button type="button" class="secondary-action" style="padding:4px 8px; font-size:.7rem;" data-filter-communique="${escapeHtml(c.code)}">Bu Tebliğde Ara</button>
                <a href="${safeUrl(c.source_url)}" target="_blank" rel="noreferrer" style="margin-left:auto; font-size:.72rem; color:var(--teal);">Resmî Gazete ↗</a>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;

    container.querySelectorAll("[data-filter-communique]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const code = btn.dataset.filterCommunique;
        $("#controlsModeSearch")?.click();
        $("#controlsGtip").value = code;
        $("#controlsForm")?.requestSubmit();
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="answer-error"><p>${escapeHtml(err.message || "Tebliğler yüklenemedi.")}</p></div>`;
  }
}

bindLocalizedNumberInputs();
if (new URLSearchParams(location.search).get("scope") === "customs" || location.hash === "#customs") switchScope("customs");
runTicaretSearch({ offset: 0 });

/* ---------------------------------------------------------------- Araç çağıran asistan (PRD Faz 3.3) */

const ASSISTANT_MAX_HISTORY = 6;

function assistantSourceBadges(ids) {
  if (!Array.isArray(ids) || !ids.length) return "";
  return ids.map((id) => `<span class="source-badge">${escapeHtml(id)}</span>`).join("");
}

function renderAssistantAnswer(data) {
  const claims = Array.isArray(data.claims) ? data.claims : [];
  const rates = Array.isArray(data.rates) ? data.rates : [];
  const candidates = Array.isArray(data.gtip_candidates) ? data.gtip_candidates : [];
  const steps = Array.isArray(data.next_steps) ? data.next_steps : [];
  const unverified = Array.isArray(data.unverified) ? data.unverified : [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const toolCalls = Array.isArray(data.tool_calls) ? data.tool_calls : [];
  const parts = [
    '<span class="assistant-role">Gümrükçe asistanı</span>',
    `<p>${escapeHtml(data.answer || "Yanıt üretilemedi.")}</p>`,
  ];
  if (claims.length) {
    parts.push(`<ul class="assistant-claims">${claims.map((item) =>
      `<li>${escapeHtml(item.text || "")}${assistantSourceBadges(item.source_ids)}</li>`).join("")}</ul>`);
  }
  if (candidates.length) {
    parts.push(`<ul class="assistant-claims">${candidates.map((item) =>
      `<li><b>${escapeHtml(item.code || "")}</b> — ${escapeHtml(item.explanation || "")}${assistantSourceBadges(item.source_ids)}</li>`).join("")}</ul>`);
  }
  if (rates.length) {
    parts.push(`<table class="assistant-rates"><thead><tr><th>Kalem</th><th>Değer</th><th>Kaynak</th></tr></thead><tbody>${rates.map((item) =>
      `<tr><td>${escapeHtml(item.name || "")}</td><td>${escapeHtml(item.value || "")}</td><td>${assistantSourceBadges(item.source_ids) || "—"}</td></tr>`).join("")}</tbody></table>`);
  }
  if (steps.length) {
    parts.push(`<ul class="assistant-steps">${steps.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`);
  }
  if (unverified.length) {
    parts.push(`<div class="assistant-unverified"><b>Doğrulanamadı ve yanıttan düşürüldü:</b><ul class="assistant-claims">${unverified.map((item) =>
      `<li>${escapeHtml(item.value || "")} — ${escapeHtml(item.reason || "")}</li>`).join("")}</ul></div>`);
  }
  if (warnings.length) {
    parts.push(`<div class="assistant-unverified">${warnings.map((item) => escapeHtml(item)).join("<br>")}</div>`);
  }
  if (toolCalls.length) {
    parts.push(`<details class="assistant-tools"><summary>${toolCalls.length} araç çağrısı</summary>${toolCalls.map((call) =>
      `<div class="assistant-tool"><b>${escapeHtml(call.id || "")} · ${escapeHtml(call.name || "")}</b>
        <pre>${escapeHtml(JSON.stringify(call.args || {}, null, 1))}</pre>
        <pre>${escapeHtml(call.summary || "")}</pre></div>`).join("")}</details>`);
  }
  if (data.legal_notice) parts.push(`<p class="assistant-notice">${escapeHtml(data.legal_notice)}</p>`);
  return parts.join("");
}

function appendAssistantMessage(kind, html) {
  const thread = $("#assistantThread");
  if (!thread) return null;
  const node = document.createElement("div");
  node.className = `assistant-msg ${kind}`;
  node.innerHTML = html;
  thread.appendChild(node);
  thread.scrollTop = thread.scrollHeight;
  return node;
}

function assistantContext() {
  const gtip = ($("#candidateGtip")?.value || "").replace(/\D/g, "");
  const origin = ($("#originCountry")?.value || "").trim();
  const asOf = ($("#tariffAsOf")?.value || "").trim();
  const body = { question: "", history: state.assistantHistory.slice(-ASSISTANT_MAX_HISTORY) };
  if (gtip) body.gtip = gtip;
  if (origin) body.origin_country = origin;
  if (asOf) body.as_of = asOf;
  return body;
}

$("#assistantForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#assistantQuestion");
  const button = $("#assistantSend");
  const question = (input.value || "").trim();
  if (!question) return;
  const body = assistantContext();
  body.question = question;
  appendAssistantMessage("user", `<span class="assistant-role">Siz</span><p>${escapeHtml(question)}</p>`);
  input.value = "";
  button.disabled = true;
  const pending = appendAssistantMessage("assistant", '<span class="assistant-role">Gümrükçe asistanı</span><p>Resmî araçlar sorgulanıyor…</p>');
  try {
    const data = await fetchJson("/api/customs/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: AI_ANALYSIS_TIMEOUT_MS,
    });
    if (pending) pending.innerHTML = renderAssistantAnswer(data);
    state.assistantHistory.push({ role: "user", content: question.slice(0, 2000) });
    state.assistantHistory.push({ role: "assistant", content: String(data.answer || "").slice(0, 2000) });
    state.assistantHistory = state.assistantHistory.slice(-ASSISTANT_MAX_HISTORY);
  } catch (error) {
    const message = error.message || "Asistan yanıtı alınamadı.";
    if (pending) {
      pending.className = "assistant-msg error";
      pending.innerHTML = `<span class="assistant-role">Hata</span><p>${escapeHtml(message)}</p>`;
    }
    showToast(message);
  } finally {
    button.disabled = false;
  }
});

$("#assistantClear")?.addEventListener("click", () => {
  state.assistantHistory = [];
  const thread = $("#assistantThread");
  if (thread) thread.innerHTML = "";
});
