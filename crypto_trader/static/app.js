const state = {
  autoTimer: null,
  lcPipelineTimer: null,
  priceTimer: null,
  paperTimer: null,
  okxPositionsTimer: null,
  okxPositionsInFlight: null,
  systemChecklistTimer: null,
  realtimeModuleTimer: null,
  systemChecklistInFlight: null,
  lcPipelineInFlight: null,
  pricesInFlight: null,
  decisionInFlight: null,
  automationStatusInFlight: null,
  paperIntervalSeconds: 60,
  maxBaseMarginUsdt: 20,
  selectedViewSymbol: null,
  selectedViewSource: null,
  okxDemoReady: false,
  lastPrices: new Map(),
  currentPrices: new Map(),
  currentDecision: null,
  currentPositions: [],
  currentPositionsPayload: null,
  lastSystemChecklistPayload: null,
  systemChecklistPayloadByRange: {},
  systemChecklistRequestSeq: 0,
  previousSystemChecklistPayload: null,
  systemChecklistRefreshInFlight: false,
  lastSystemChecklistRefreshMs: 0,
  selectedSystemModuleKey: null,
  selectedPositionManagementDetailTab: null,
  selectedTradeExecutionTabKey: null,
  selectedMarketRegimeView: null,
  systemModuleAiRange: "current",
  running: false,
};

const el = (id) => document.getElementById(id);
const MIN_LEVERAGE = 5;
const MAX_LEVERAGE = 25;
const MIN_BASE_MARGIN_USDT = 1;
const SYSTEM_CHECKLIST_REFRESH_MS = 60 * 1000;
const LC_PIPELINE_REFRESH_MS = 3 * 60 * 1000;
const PRICE_REFRESH_MS = 30 * 1000;
const OKX_POSITIONS_REFRESH_MS = 15 * 1000;
const REALTIME_MODULE_REFRESH_MS = 60 * 1000;

function dashboardIsVisible() {
  return document.visibilityState !== "hidden";
}

const refs = {
  statusLine: el("statusLine"),
  autoRun: el("autoRun"),
  darkModeToggle: el("darkModeToggle"),
  intervalInput: el("intervalInput"),
  orderMarginInput: el("orderMarginInput"),
  saveOrderMarginBtn: el("saveOrderMarginBtn"),
  orderMarginStatus: el("orderMarginStatus"),
  leverageInput: el("leverageInput"),
  saveLeverageBtn: el("saveLeverageBtn"),
  leverageStatus: el("leverageStatus"),
  refreshBtn: el("refreshBtn"),
  analyzeBtn: el("analyzeBtn"),
  actionValue: el("actionValue"),
  modeValue: el("modeValue"),
  selectedValue: el("selectedValue"),
  selectedLabel: el("selectedLabel"),
  sideValue: el("sideValue"),
  confidenceValue: el("confidenceValue"),
  confidenceBar: el("confidenceBar"),
  winRateValue: el("winRateValue"),
  winRateDetail: el("winRateDetail"),
  riskValue: el("riskValue"),
  riskDetail: el("riskDetail"),
  pulseUpdated: el("pulseUpdated"),
  pulseSymbol: el("pulseSymbol"),
  pulseMove: el("pulseMove"),
  pulseFeed: el("pulseFeed"),
  paperStatus: el("paperStatus"),
  demoStatus: el("demoStatus"),
  paperNextScan: el("paperNextScan"),
  positionsStatus: el("positionsStatus"),
  positionRows: el("positionRows"),
  ordersStatus: el("ordersStatus"),
  orderRows: el("orderRows"),
  systemChecklistStatus: el("systemChecklistStatus"),
  systemChecklistToggle: el("systemChecklistToggle"),
  systemChecklistBody: el("systemChecklistBody"),
  systemChecklistGrid: el("systemChecklistGrid"),
  systemChecklistDate: el("systemChecklistDate"),
  systemChecklistHistoryBtn: el("systemChecklistHistoryBtn"),
  systemChecklistTodayBtn: el("systemChecklistTodayBtn"),
  systemSummaryWeekBtn: el("systemSummaryWeekBtn"),
  systemSummaryMonthBtn: el("systemSummaryMonthBtn"),
  systemSummaryYearBtn: el("systemSummaryYearBtn"),
  systemSummaryChart: el("systemSummaryChart"),
  systemModuleStatus: el("systemModuleStatus"),
  systemModuleGrid: el("systemModuleGrid"),
  systemModuleOverlay: el("systemModuleOverlay"),
  systemModuleBackdrop: el("systemModuleBackdrop"),
  systemModuleDetail: el("systemModuleDetail"),
  storageSummary: el("storageSummary"),
  scanMemorySummary: el("scanMemorySummary"),
  lcPipelineSummary: el("lcPipelineSummary"),
  lcPipelineRows: el("lcPipelineRows"),
  lcInternalSummary: el("lcInternalSummary"),
  lcInternalRows: el("lcInternalRows"),
  storageMaintenanceBtn: el("storageMaintenanceBtn"),
  planTag: el("planTag"),
  planCanvas: el("planCanvas"),
  entryValue: el("entryValue"),
  currentValue: el("currentValue"),
  stopValue: el("stopValue"),
  targetValue: el("targetValue"),
  rrValue: el("rrValue"),
  qtyValue: el("qtyValue"),
  spreadValue: el("spreadValue"),
  tpPctValue: el("tpPctValue"),
  slPctValue: el("slPctValue"),
  updatedValue: el("updatedValue"),
  reasonList: el("reasonList"),
  riskBlocks: el("riskBlocks"),
  candidateRows: el("candidateRows"),
  candidateCount: el("candidateCount"),
  newsList: el("newsList"),
  newsCount: el("newsCount"),
};

const THEME_STORAGE_KEY = "cryptoSignalTheme";

function applyTheme(isDark) {
  document.body.classList.toggle("dark-mode", Boolean(isDark));
  if (refs.darkModeToggle) refs.darkModeToggle.checked = Boolean(isDark);
}

function initTheme() {
  const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
  applyTheme(savedTheme === "dark");
}

function setThemeFromToggle() {
  const isDark = Boolean(refs.darkModeToggle?.checked);
  localStorage.setItem(THEME_STORAGE_KEY, isDark ? "dark" : "light");
  applyTheme(isDark);
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  if (Math.abs(n) >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: digits });
  if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { maximumFractionDigits: digits + 2 });
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function formatFixed2(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function pct(value) {
  if (value === null || value === undefined) return "-";
  return `${fmt(value, 4)}%`;
}

function signed(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${fmt(n, digits)}`;
}

function timeLabel(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("vi-VN", {
    timeZone: "Asia/Ho_Chi_Minh",
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function timeOnlyLabel(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    const text = String(value || "").trim();
    const timeMatch = text.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
    return timeMatch ? timeMatch[1] : text || "-";
  }
  return date.toLocaleTimeString("vi-VN", {
    timeZone: "Asia/Ho_Chi_Minh",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function dateOnlyLabel(value) {
  const text = String(value ?? "").trim();
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return text || "-";
  return `${match[3]}/${match[2]}/${match[1]}`;
}

function isTimestampLabel(label) {
  const key = viLabel(label || "");
  return [
    "cap nhat luc",
    "updatedat",
    "updated_at",
    "createdat",
    "created_at",
    "ngay doc",
    "paper trade moi nhat",
  ].includes(key);
}

function isDateOnlyLabel(label) {
  const key = viLabel(label || "");
  return key === "ngay kiem tra";
}

function formatCardValue(label, value) {
  if (value === null || value === undefined || value === "") return "-";
  const text = String(value).trim();
  if (!text || text === "-") return "-";
  if (isDateOnlyLabel(label) || /^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return dateOnlyLabel(text);
  }
  if (isTimestampLabel(label) || /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text)) {
    return timeLabel(text);
  }
  return text;
}

function moduleUpdatedRow(module) {
  const rows = Array.isArray(module?.stats) ? module.stats : [];
  const preferredKeys = ["updatedat", "updated_at", "createdat", "created_at", "cap nhat luc"];
  for (const key of preferredKeys) {
    const row = rows.find((item) => viLabel(item?.label || "") === key && item?.value !== null && item?.value !== undefined && String(item.value).trim() && String(item.value).trim() !== "-");
    if (row) return row;
  }
  return null;
}

function moduleUpdatedLabel(module) {
  const row = moduleUpdatedRow(module);
  if (row) return formatCardValue(row.label, row.value);
  if (module?.file?.updated_at) return timeLabel(module.file.updated_at);
  return "-";
}

function intervalLabel(seconds) {
  const total = Math.max(60, Number(seconds || 60));
  const minutes = Math.max(1, Math.round(total / 60));
  return minutes === 1 ? "1 phút" : `${minutes} phút`;
}

function systemDataUpdateSeconds() {
  const payloadSeconds = Number(state.lastSystemChecklistPayload?.automation?.interval_seconds);
  if (Number.isFinite(payloadSeconds) && payloadSeconds > 0) return payloadSeconds;
  const paperSeconds = Number(state.paperIntervalSeconds);
  if (Number.isFinite(paperSeconds) && paperSeconds > 0) return paperSeconds;
  return 300;
}

function systemDataUpdateIntervalLabel() {
  return `${intervalLabel(systemDataUpdateSeconds())}/l\u1ea7n`;
}

function systemDataUpdateScheduleText() {
  return `Chu k\u1ef3 c\u1eadp nh\u1eadt data h\u1ec7 th\u1ed1ng: ${systemDataUpdateIntervalLabel()}`;
}

function normalizeVietnameseIntervalLabel(value) {
  return String(value || "")
    .replace(/phÃºt/gi, "ph\u00fat")
    .replace(/\bphut\b/gi, "ph\u00fat")
    .replace(/lÃ¡ÂºÂ§n/gi, "l\u1ea7n")
    .replace(/láº§n/gi, "l\u1ea7n")
    .replace(/\blan\b/gi, "l\u1ea7n");
}

function setBusy(isBusy) {
  state.running = isBusy;
  refs.analyzeBtn.disabled = isBusy;
  refs.refreshBtn.disabled = isBusy;
  refs.analyzeBtn.textContent = isBusy ? "Đang chạy" : "Phân tích";
}

function setStatus(text) {
  refs.statusLine.textContent = viText(text);
}

function setLeverageStatus(text, kind = "") {
  if (!refs.leverageStatus) return;
  refs.leverageStatus.textContent = viText(text);
  refs.leverageStatus.className = kind;
}

function setOrderMarginStatus(text, kind = "") {
  if (!refs.orderMarginStatus) return;
  refs.orderMarginStatus.textContent = viText(text);
  refs.orderMarginStatus.className = kind;
}

function controlNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return Number.isInteger(number) ? String(number) : String(Math.round(number * 10000) / 10000);
}

function clampLeverage(value) {
  const leverage = Math.trunc(Number(value));
  if (!Number.isFinite(leverage)) return null;
  return Math.max(MIN_LEVERAGE, Math.min(MAX_LEVERAGE, leverage));
}

function maxBaseMarginUsdt() {
  return Math.max(MIN_BASE_MARGIN_USDT, Number(state.maxBaseMarginUsdt || 20));
}

function clampOrderMargin(value) {
  const margin = Number(String(value).replace(",", "."));
  if (!Number.isFinite(margin)) return null;
  return Math.max(MIN_BASE_MARGIN_USDT, Math.min(maxBaseMarginUsdt(), margin));
}

function nextLeverageValue(input, event) {
  if (!event.data) return null;
  let start = input.value.length;
  let end = input.value.length;
  try {
    start = input.selectionStart ?? input.value.length;
    end = input.selectionEnd ?? input.value.length;
  } catch {
    start = input.value.length;
    end = input.value.length;
  }
  return `${input.value.slice(0, start)}${event.data}${input.value.slice(end)}`;
}

function blockInvalidLeverageInput(event) {
  if (!refs.leverageInput) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.inputType && event.inputType.startsWith("delete")) return;
  const nextValue = nextLeverageValue(refs.leverageInput, event);
  if (nextValue === null || nextValue === "") return;
  if (!/^\d+$/.test(nextValue) || Number(nextValue) > MAX_LEVERAGE) {
    event.preventDefault();
    setLeverageStatus(`Chi nhap ${MIN_LEVERAGE}-${MAX_LEVERAGE}x`, "warn");
  }
}

function sanitizeLeverageInput() {
  if (!refs.leverageInput || refs.leverageInput.value === "") return;
  const digits = refs.leverageInput.value.replace(/[^\d]/g, "");
  if (digits === "") {
    refs.leverageInput.value = "";
    setLeverageStatus(`Chi nhap ${MIN_LEVERAGE}-${MAX_LEVERAGE}x`, "warn");
    return;
  }
  const leverage = Number(digits);
  if (leverage > MAX_LEVERAGE) {
    refs.leverageInput.value = String(MAX_LEVERAGE);
    setLeverageStatus(`Tối đa ${MAX_LEVERAGE}x`, "warn");
  } else if (digits !== refs.leverageInput.value) {
    refs.leverageInput.value = digits;
  }
}

function normalizeLeverageInput() {
  if (!refs.leverageInput || refs.leverageInput.value === "") return;
  const leverage = clampLeverage(refs.leverageInput.value);
  if (leverage === null) {
    refs.leverageInput.value = "";
    setLeverageStatus(`Chi nhap ${MIN_LEVERAGE}-${MAX_LEVERAGE}x`, "warn");
    return;
  }
  refs.leverageInput.value = String(leverage);
}

function sanitizeOrderMarginInput() {
  if (!refs.orderMarginInput || refs.orderMarginInput.value === "") return;
  const cleaned = refs.orderMarginInput.value.replace(",", ".").replace(/[^\d.]/g, "");
  const parts = cleaned.split(".");
  const normalized = parts.length > 1 ? `${parts.shift()}.${parts.join("")}` : cleaned;
  if (normalized !== refs.orderMarginInput.value) refs.orderMarginInput.value = normalized;
}

function normalizeOrderMarginInput() {
  if (!refs.orderMarginInput || refs.orderMarginInput.value === "") return;
  const margin = clampOrderMargin(refs.orderMarginInput.value);
  if (margin === null) {
    refs.orderMarginInput.value = "";
    setOrderMarginStatus(`Nhập ${MIN_BASE_MARGIN_USDT}-${controlNumber(maxBaseMarginUsdt())} USDT`, "warn");
    return;
  }
  refs.orderMarginInput.value = controlNumber(margin);
}

function renderConfig(config) {
  const leverage = Number(config?.exchange?.leverage);
  if (refs.leverageInput && Number.isFinite(leverage)) {
    const safeLeverage = clampLeverage(leverage);
    refs.leverageInput.value = String(safeLeverage);
    setLeverageStatus(`Đang dùng ${safeLeverage}x`, "ok");
  }

  const sizing = config?.position_sizing || {};
  const maxMargin = Number(sizing.max_margin_usdt);
  if (Number.isFinite(maxMargin)) state.maxBaseMarginUsdt = Math.max(MIN_BASE_MARGIN_USDT, maxMargin);
  if (refs.orderMarginInput) {
    refs.orderMarginInput.max = controlNumber(maxBaseMarginUsdt());
    const margin = Number(config?.order_margin_usdt ?? sizing.base_margin_usdt);
    if (Number.isFinite(margin)) {
      refs.orderMarginInput.value = controlNumber(margin);
      setOrderMarginStatus(`Đang dùng ${controlNumber(margin)} USDT`, "ok");
    }
  }
}

function escapeHtml(value) {
  return viText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function baseFromSymbol(symbol) {
  return String(symbol || "").split("/")[0].split(":")[0].toUpperCase();
}

function pairDisplayParts(symbol) {
  const text = String(symbol || "-");
  const parts = text.split(":");
  return {
    market: parts[0] || text,
    suffix: parts.length > 1 ? `:${parts.slice(1).join(":")}` : "",
    full: text,
  };
}

function pairMeta(symbol) {
  const base = baseFromSymbol(symbol);
  const data = {
    BTC: { name: "Bitcoin", className: "btc", glyph: "BTC" },
    ETH: { name: "Ethereum", className: "eth", glyph: "ETH" },
    SOL: { name: "Solana", className: "sol", glyph: "SOL" },
    BNB: { name: "BNB", className: "bnb", glyph: "BNB" },
    XRP: { name: "XRP", className: "xrp", glyph: "XRP" },
    DOGE: { name: "Dogecoin", className: "doge", glyph: "DOGE" },
    ADA: { name: "Cardano", className: "ada", glyph: "ADA" },
    LINK: { name: "Chainlink", className: "link", glyph: "LINK" },
    AVAX: { name: "Avalanche", className: "avax", glyph: "A" },
    LTC: { name: "Litecoin", className: "ltc", glyph: "LTC" },
  };
  return data[base] || { name: base || "Unknown", className: "generic", glyph: base.slice(0, 3) };
}

function coinLogoHtml(meta) {
  const className = escapeHtml(meta.className);
  const glyph = escapeHtml(meta.glyph || "");
  if (meta.className === "eth") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <path class="eth-top" d="M18 5 9 19l9 5 9-5L18 5Z"></path>
          <path class="eth-bottom" d="M18 25 9 20l9 11 9-11-9 5Z"></path>
        </svg>
      </span>
    `;
  }
  if (meta.className === "sol") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <path d="M10 10h18l-4 5H6l4-5Z"></path>
          <path d="M8 16h18l-4 5H4l4-5Z"></path>
          <path d="M12 22h18l-4 5H8l4-5Z"></path>
        </svg>
      </span>
    `;
  }
  if (meta.className === "bnb") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <path d="M18 5 25 12 18 19 11 12 18 5Z"></path>
          <path d="M8 15 14 21 8 27 2 21 8 15Z"></path>
          <path d="M28 15 34 21 28 27 22 21 28 15Z"></path>
          <path d="M18 17 24 23 18 29 12 23 18 17Z"></path>
        </svg>
      </span>
    `;
  }
  if (meta.className === "ada") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <circle cx="18" cy="18" r="4"></circle>
          <circle cx="18" cy="7" r="2.4"></circle>
          <circle cx="18" cy="29" r="2.4"></circle>
          <circle cx="7" cy="18" r="2.4"></circle>
          <circle cx="29" cy="18" r="2.4"></circle>
          <circle cx="10" cy="10" r="1.7"></circle>
          <circle cx="26" cy="10" r="1.7"></circle>
          <circle cx="10" cy="26" r="1.7"></circle>
          <circle cx="26" cy="26" r="1.7"></circle>
        </svg>
      </span>
    `;
  }
  if (meta.className === "link") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <path d="M18 5 29 11.5v13L18 31 7 24.5v-13L18 5Z"></path>
          <path class="logo-hole" d="M18 11 24 14.5v7L18 25l-6-3.5v-7L18 11Z"></path>
        </svg>
      </span>
    `;
  }
  if (meta.className === "avax") {
    return `
      <span class="coin-logo ${className}" aria-hidden="true">
        <svg viewBox="0 0 36 36" focusable="false">
          <path d="M18 6 31 29h-8l-5-9-5 9H5L18 6Z"></path>
        </svg>
      </span>
    `;
  }
  return `<span class="coin-logo ${className}" aria-hidden="true">${glyph}</span>`;
}

function pairCellHtml(symbol) {
  const meta = pairMeta(symbol);
  const pair = pairDisplayParts(symbol);
  const detail = pair.suffix ? `${meta.name} · ${pair.suffix}` : meta.name;
  return `
    <div class="pair-cell">
      ${coinLogoHtml(meta)}
      <span class="pair-name">
        <strong title="${escapeHtml(pair.full)}">${escapeHtml(pair.market)}</strong>
        <small>${escapeHtml(detail)}</small>
      </span>
    </div>
  `;
}

function sideLabel(side) {
  if (side === "long") return "LONG";
  if (side === "short") return "SHORT";
  return "-";
}

function candidateForSymbol(decision, symbol) {
  if (!symbol) return null;
  return (decision.candidates || []).find((candidate) => candidate.symbol === symbol) || null;
}

function selectedOpenPosition() {
  if (!state.selectedViewSymbol) return null;
  return state.currentPositions.find((position) => position.symbol === state.selectedViewSymbol) || null;
}

function normalizedPositionSide(side) {
  const normalized = String(side || "").toLowerCase();
  if (normalized === "long" || normalized === "buy") return "long";
  if (normalized === "short" || normalized === "sell") return "short";
  return "";
}

function liveOkxPositionForTradeItem(item) {
  const symbol = String(item?.symbol || "");
  const side = normalizedPositionSide(item?.side);
  if (!symbol || !Array.isArray(state.currentPositions)) return null;
  return state.currentPositions.find((position) => {
    if (String(position?.symbol || "") !== symbol) return false;
    const positionSide = normalizedPositionSide(position?.side);
    return !side || !positionSide || positionSide === side;
  }) || null;
}

function tradeItemWithLiveOkxPosition(item) {
  const position = liveOkxPositionForTradeItem(item);
  if (!position) return item;
  const contracts = nullableNumber(position.contracts);
  const markPrice = nullableNumber(position.mark_price);
  const entryPrice = nullableNumber(position.entry_price);
  const unrealizedPnl = nullableNumber(position.unrealized_pnl);
  const stopLoss = nullableNumber(position.stop_loss);
  const takeProfit = nullableNumber(position.take_profit);
  const levels = item?.profit_protection_levels && typeof item.profit_protection_levels === "object"
    ? item.profit_protection_levels
    : {};
  return {
    ...item,
    quantity: contracts ?? item?.quantity,
    entry_price: entryPrice ?? item?.entry_price,
    live_entry_price: entryPrice ?? item?.live_entry_price ?? item?.entry_price,
    mark_price: markPrice ?? item?.mark_price,
    pnl: unrealizedPnl ?? item?.pnl,
    stop_loss: stopLoss ?? item?.stop_loss,
    take_profit: takeProfit ?? item?.take_profit,
    profit_protection_levels: {
      ...levels,
      live_entry_price: entryPrice ?? levels.live_entry_price ?? item?.live_entry_price ?? item?.entry_price,
      current_stop_loss: stopLoss ?? levels.current_stop_loss ?? item?.stop_loss,
      current_take_profit: takeProfit ?? levels.current_take_profit ?? item?.take_profit,
      quantity: contracts ?? levels.quantity,
      current_amount: contracts ?? levels.current_amount,
      current_sl: {
        ...(levels.current_sl || {}),
        price: stopLoss ?? levels.current_sl?.price,
      },
      current_tp: {
        ...(levels.current_tp || {}),
        price: takeProfit ?? levels.current_tp?.price,
      },
    },
  };
}

function tradeExecutionLiveEntry(item) {
  return item?.live_entry_price ?? item?.entry_price ?? item?.initial_entry_price;
}

function positionAsCandidate(position, fallback = null) {
  const entry = nullableNumber(position.entry_price) ?? nullableNumber(fallback?.entry);
  const mark = nullableNumber(position.mark_price);
  const leverage = nullableNumber(position.leverage) ?? nullableNumber(fallback?.leverage) ?? 1;
  const side = normalizedPositionSide(position.side) || fallback?.side || "long";
  const takeProfitPct = nullableNumber(fallback?.take_profit_pct) ?? 75;
  const stopLossPct = nullableNumber(fallback?.stop_loss_pct) ?? 50;
  const priceTakeProfitPct = nullableNumber(fallback?.price_take_profit_pct) ?? takeProfitPct / Math.max(leverage, 1);
  const priceStopLossPct = nullableNumber(fallback?.price_stop_loss_pct) ?? stopLossPct / Math.max(leverage, 1);
  let takeProfit = nullableNumber(position.take_profit) ?? nullableNumber(fallback?.take_profit);
  let stopLoss = nullableNumber(position.stop_loss) ?? nullableNumber(fallback?.stop_loss);

  if (entry !== null) {
    if (takeProfit === null) {
      takeProfit = side === "short" ? entry * (1 - priceTakeProfitPct / 100) : entry * (1 + priceTakeProfitPct / 100);
    }
    if (stopLoss === null) {
      stopLoss = side === "short" ? entry * (1 + priceStopLossPct / 100) : entry * (1 - priceStopLossPct / 100);
    }
  }

  const notional = nullableNumber(position.notional) ?? nullableNumber(fallback?.order_usdt);
  const margin = notional !== null && leverage > 0 ? Math.abs(notional) / leverage : nullableNumber(fallback?.margin_usdt);

  return {
    ...(fallback || {}),
    symbol: position.symbol || fallback?.symbol || "-",
    side,
    entry,
    stop_loss: stopLoss,
    take_profit: takeProfit,
    risk_reward: nullableNumber(fallback?.risk_reward) ?? takeProfitPct / Math.max(stopLossPct, 1),
    quantity: nullableNumber(position.contracts) ?? nullableNumber(fallback?.quantity),
    confidence: nullableNumber(fallback?.confidence) ?? 0,
    win_probability_pct: nullableNumber(fallback?.win_probability_pct),
    spread_pct: nullableNumber(fallback?.spread_pct),
    target_mode: fallback?.target_mode || "roi_percent",
    take_profit_pct: takeProfitPct,
    stop_loss_pct: stopLossPct,
    price_take_profit_pct: priceTakeProfitPct,
    price_stop_loss_pct: priceStopLossPct,
    mark_price: mark,
    leverage,
    margin_usdt: margin,
    order_usdt: notional,
    unrealized_pnl: nullableNumber(position.unrealized_pnl) ?? nullableNumber(position.pnl_usdt),
    margin_mode: position.margin_mode,
    is_open_position: true,
    reasons: fallback?.reasons || [],
  };
}

function selectedCandidate(decision) {
  const candidates = decision.candidates || [];
  if (state.selectedViewSymbol) {
    const chosen = candidateForSymbol(decision, state.selectedViewSymbol);
    const position = selectedOpenPosition();
    if (state.selectedViewSource === "position" && position) return positionAsCandidate(position, chosen);
    if (chosen) return chosen;
    if (position) return positionAsCandidate(position, chosen);
  }
  return decision.selected || candidates[0] || null;
}

function actionLabel(decision) {
  if (state.selectedViewSymbol) return "ĐANG XEM";
  if (!decision.selected) return "DUNG NGOAI";
  return sideLabel(decision.selected.side);
}

function targetModeLabel(mode) {
  if (mode === "roi_percent") return "ROI";
  if (mode === "price_percent") return "Gia";
  if (mode === "risk_reward" || mode === "rr" || mode === "rr_percent") return "RR";
  return "ATR/RR";
}

function scanSourceLabel(source) {
  if (source === "new_and_old_rescan") return "Moi + cu";
  if (source === "old_rescan") return "Cu quet lai";
  if (source === "new_scan") return "Moi";
  return "-";
}

function scanSourceClass(source) {
  if (source === "new_and_old_rescan") return "both";
  if (source === "old_rescan") return "old";
  return "new";
}

function nullableNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function deltaClass(value) {
  const n = nullableNumber(value);
  if (n === null) return "delta neutral";
  if (n > 0) return "delta up";
  if (n < 0) return "delta down";
  return "delta neutral";
}

function deltaText(value) {
  const n = nullableNumber(value);
  if (n === null) return "Lan dau";
  return `${signed(n, 2)}%`;
}

function riskReasonVi(text) {
  if (!text) return "";
  return text
    .replace(/Confidence ([0-9.]+) is below minimum ([0-9.]+)/, "Do tin cay $1 thap hon nguong toi thieu $2")
    .replace(/Risk\/reward ([0-9.]+) is below minimum ([0-9.]+)/, "R:R $1 thap hon nguong toi thieu $2")
    .replace(/No recent symbol-specific news confirmed the setup/, "Chưa có tin gần đây xác nhận setup này")
    .replace(/Spread ([0-9.]+)% exceeds maximum ([0-9.]+)%/, "Spread $1% vượt mức tối đa $2%")
    .replace(/Stop distance ([0-9.]+)% is below minimum ([0-9.]+)%/, "Khoảng cách SL $1% thap hon muc toi thieu $2%")
    .replace(/Stop distance ([0-9.]+)% exceeds maximum ([0-9.]+)%/, "Khoảng cách SL $1% vượt mức tối đa $2%")
    .replace(/Private OKX checks skipped because API credentials are unavailable or mode is dry_run/, "Bỏ qua kiểm tra private OKX vì chưa có API key hoặc đang ở dry_run");
}

function reasonVi(text) {
  if (!text) return "";
  return text
    .replace("Price is above EMA50", "Giá đang nằm trên EMA50")
    .replace("Price is below EMA50", "Giá đang nằm dưới EMA50")
    .replace("EMA20 is above EMA50", "EMA20 đang nằm trên EMA50")
    .replace("EMA20 is below EMA50", "EMA20 đang nằm dưới EMA50")
    .replace(/RSI is constructive at ([0-9.]+)/, "RSI tich cuc o muc $1")
    .replace(/RSI is weak at ([0-9.]+)/, "RSI yeu o muc $1")
    .replace(/RSI is extended at ([0-9.]+)/, "RSI qua cao o muc $1")
    .replace(/RSI is deeply oversold at ([0-9.]+)/, "RSI qua ban manh o muc $1")
    .replace(/Volume is ([0-9.]+)x recent average with price above EMA20/, "Volume bang $1 lan trung binh gan day, gia tren EMA20")
    .replace(/Volume is ([0-9.]+)x recent average with price below EMA20/, "Volume bang $1 lan trung binh gan day, gia duoi EMA20")
    .replace("Price is pressing recent resistance", "Gia ap sat khang cu gan day")
    .replace("Price is pressing recent support", "Gia ap sat ho tro gan day")
    .replace(/News sentiment is bullish \(([+-]?[0-9.]+), ([0-9]+) item\(s\)\)/, "Tin tuc nghieng ve tang ($1, $2 bai)")
    .replace(/News sentiment is bearish \(([+-]?[0-9.]+), ([0-9]+) item\(s\)\)/, "Tin tuc nghieng ve giam ($1, $2 bai)")
    .replace(/([0-9]+) related news item\(s\), but sentiment is neutral/, "$1 bai lien quan, nhung sentiment trung lap")
    .replace(/TP\/SL target: TP ([0-9]+)%, SL ([0-9]+)% \(roi_percent, ([0-9]+)x\)/, "Muc tieu TP/SL: TP $1%, SL $2% theo ROI, don bay $3x")
    .replace(/TP\/SL target: TP ([0-9]+)%, SL ([0-9]+)% \(price_percent, ([0-9]+)x\)/, "Muc tieu TP/SL: TP $1%, SL $2% theo bien dong gia")
    .replace(/TP\/SL target: RR ([0-9.]+):1, SL ([0-9]+)% \(roi_percent, ([0-9]+)x\)/, "Muc tieu TP/SL: RR $1:1, SL $2% theo ROI, don bay $3x")
    .replace(/TP\/SL target: RR ([0-9.]+):1, SL ([0-9]+)% \(price_percent, ([0-9]+)x\)/, "Muc tieu TP/SL: RR $1:1, SL $2% theo bien dong gia");
}

function currentPriceFor(symbol) {
  const value = state.currentPrices.get(symbol);
  return Number.isFinite(value) ? value : null;
}

function activePriceFor(candidate) {
  const livePrice = currentPriceFor(candidate.symbol);
  if (livePrice !== null) return livePrice;
  const mark = nullableNumber(candidate.mark_price);
  return mark !== null ? mark : null;
}

function planChartTheme() {
  const dark = document.body.classList.contains("dark-mode");
  return dark
    ? {
        background: "#132229",
        grid: "#30464f",
        axis: "#d6e3e7",
        muted: "#a8bac4",
        entry: "#20a6a9",
        target: "#39b878",
        stop: "#e05b50",
        current: "#6b94d8",
        currentStroke: "#132229",
      }
    : {
        background: "#fbfcfc",
        grid: "#d9e1e5",
        axis: "#172026",
        muted: "#66727c",
        entry: "#147a7e",
        target: "#1f8a5b",
        stop: "#bd3f32",
        current: "#315f9f",
        currentStroke: "#ffffff",
      };
}

function renderPlan(candidate) {
  const canvas = refs.planCanvas;
  const ctx = canvas.getContext("2d");
  const theme = planChartTheme();
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 760;
  const height = canvas.clientHeight || 240;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = theme.background;
  ctx.fillRect(0, 0, width, height);

  if (!candidate) {
    ctx.fillStyle = theme.muted;
    ctx.font = "700 16px system-ui";
    ctx.fillText("Chưa có tín hiệu", 24, 42);
    return;
  }

  const entry = nullableNumber(candidate.entry);
  const stop = nullableNumber(candidate.stop_loss);
  const target = nullableNumber(candidate.take_profit);
  const currentPrice = activePriceFor(candidate);
  const prices = [entry, stop, target].filter((value) => value !== null);
  if (prices.length < 3 || entry === null || stop === null || target === null) {
    ctx.fillStyle = theme.muted;
    ctx.font = "700 16px system-ui";
    ctx.fillText("Thieu du lieu gia de ve ke hoach", 24, 42);
    return;
  }
  if (currentPrice !== null) prices.push(currentPrice);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const pad = Math.max((max - min) * 0.18, Math.abs(entry || 1) * 0.001);
  const lo = min - pad;
  const hi = max + pad;
  const left = 32;
  const right = width - 32;
  const top = 24;
  const bottom = height - 44;
  const x = (price) => left + ((hi - price) / (hi - lo)) * (right - left);
  const y = (level) => top + level * (bottom - top);

  ctx.strokeStyle = theme.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i < 5; i += 1) {
    const yy = y(i / 4);
    ctx.beginPath();
    ctx.moveTo(left, yy);
    ctx.lineTo(right, yy);
    ctx.stroke();
  }

  const entryX = x(entry);
  const stopX = x(stop);
  const targetX = x(target);
  const currentX = currentPrice !== null ? x(currentPrice) : null;
  const lineY = y(0.52);

  ctx.strokeStyle = theme.axis;
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(Math.min(stopX, targetX), lineY);
  ctx.lineTo(Math.max(stopX, targetX), lineY);
  ctx.stroke();

  if (currentX !== null) drawProgress(ctx, entryX, currentX, lineY, theme);

  const tpLabel = candidate.take_profit_pct ? `TP ${fmt(candidate.take_profit_pct, 0)}%` : "TP";
  const slLabel = candidate.stop_loss_pct ? `SL ${fmt(candidate.stop_loss_pct, 0)}%` : "SL";
  drawMarker(ctx, targetX, lineY, theme.target, tpLabel, target, theme);
  drawMarker(ctx, entryX, lineY, theme.entry, "Vao", entry, theme);
  drawMarker(ctx, stopX, lineY, theme.stop, slLabel, stop, theme);
  if (currentX !== null) drawCurrentMarker(ctx, currentX, lineY, currentPrice, theme);

  const direction = candidate.side === "short" ? "Có lợi khi giá giảm" : "Có lợi khi giá tăng";
  ctx.fillStyle = theme.muted;
  ctx.font = "700 13px system-ui";
  ctx.fillText(direction, left, height - 18);
}

function drawProgress(ctx, entryX, currentX, y, theme) {
  ctx.strokeStyle = theme.current;
  ctx.lineWidth = 5;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(entryX, y + 11);
  ctx.lineTo(currentX, y + 11);
  ctx.stroke();
  ctx.lineCap = "butt";
}

function drawMarker(ctx, x, y, color, label, value, theme) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, 7, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = theme.axis;
  ctx.font = "800 12px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(label, x, y - 18);
  ctx.font = "700 12px system-ui";
  ctx.fillStyle = theme.muted;
  ctx.fillText(fmt(value, 2), x, y + 27);
  ctx.textAlign = "start";
}

function drawCurrentMarker(ctx, x, y, value, theme) {
  ctx.save();
  ctx.strokeStyle = theme.current;
  ctx.setLineDash([5, 5]);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x, y - 84);
  ctx.lineTo(x, y + 88);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = theme.current;
  ctx.beginPath();
  ctx.arc(x, y + 11, 8, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = theme.currentStroke;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fillStyle = theme.current;
  ctx.font = "800 12px system-ui";
  ctx.textAlign = "center";
  ctx.fillText("Hien tai", x, y + 45);
  ctx.font = "700 12px system-ui";
  ctx.fillText(fmt(value, 2), x, y + 63);
  ctx.restore();
}

function renderSummary(decision) {
  const candidate = selectedCandidate(decision);
  const selected = decision.selected;
  const isViewing = Boolean(state.selectedViewSymbol);
  const isPositionView = Boolean(candidate?.is_open_position);
  const riskPassed = !isViewing && decision.risk_check && decision.risk_check.passed;
  refs.actionValue.textContent = actionLabel(decision);
  refs.modeValue.textContent = `Che do: ${decision.mode || "-"}`;
  refs.selectedLabel.textContent = isPositionView ? "Vị thế đang xem" : isViewing ? "Cặp đang xem" : selected ? "Lệnh được chọn" : "Ứng viên mạnh nhất";
  refs.selectedValue.innerHTML = candidate ? pairCellHtml(candidate.symbol) : "-";
  refs.sideValue.textContent = candidate ? `${sideLabel(candidate.side)}${isPositionView ? " - OKX demo" : isViewing ? " - đang xem" : ""}` : "-";

  const confidence = candidate ? Number(candidate.confidence) : 0;
  refs.confidenceValue.textContent = candidate ? fmt(confidence, 2) : "-";
  refs.confidenceBar.style.width = `${Math.max(0, Math.min(100, confidence))}%`;
  refs.confidenceBar.style.background = confidence >= 75 ? "#147a7e" : "#b27611";

  if (candidate && candidate.win_probability_pct !== null && candidate.win_probability_pct !== undefined) {
    refs.winRateValue.textContent = `${fmt(candidate.win_probability_pct, 2)}%`;
    const breakEven = 100 / (1 + Number(candidate.risk_reward || 0));
    const delta = nullableNumber(candidate.win_delta_pct);
    const deltaNote = delta === null ? "" : ` | Chu ky truoc ${deltaText(delta)}`;
    refs.winRateDetail.textContent = `Hoa von ${fmt(breakEven, 1)}% voi R:R ${fmt(candidate.risk_reward, 2)}${deltaNote}`;
  } else {
    refs.winRateValue.textContent = "-";
    refs.winRateDetail.textContent = "-";
  }

  refs.riskValue.textContent = isPositionView ? "Đang mở" : riskPassed ? "Đạt" : isViewing ? "Đang xem" : "Bị chặn";
  refs.riskValue.style.color = isPositionView ? "#315f9f" : riskPassed ? "#1f8a5b" : isViewing ? "#315f9f" : "#bd3f32";
  const blocks = (decision.risk_check && decision.risk_check.reasons) || [];
  refs.riskDetail.textContent = isPositionView
    ? "Du lieu truc tiep tu vi the OKX demo"
    : isViewing
      ? "Click cap khac de doi UI"
      : blocks.length
        ? riskReasonVi(blocks[0])
        : "Khong co chan";
  if (refs.updatedValue) refs.updatedValue.textContent = timeLabel(decision.created_at);
}

function renderSelected(decision) {
  const candidate = selectedCandidate(decision);
  refs.planTag.className = "tag";
  if (!candidate) {
    refs.planTag.textContent = "Chưa có tín hiệu";
    refs.entryValue.textContent = "-";
    refs.currentValue.textContent = "-";
    refs.stopValue.textContent = "-";
    refs.targetValue.textContent = "-";
    refs.rrValue.textContent = "-";
    refs.qtyValue.textContent = "-";
    refs.spreadValue.textContent = "-";
    refs.tpPctValue.textContent = "-";
    refs.slPctValue.textContent = "-";
    if (refs.reasonList) refs.reasonList.innerHTML = "";
    if (refs.riskBlocks) refs.riskBlocks.innerHTML = "";
    renderPlan(null);
    return;
  }

  const isPositionView = Boolean(candidate.is_open_position);
  const isRealSelected = decision.selected && decision.selected.symbol === candidate.symbol && !state.selectedViewSymbol;
  refs.planTag.textContent = isPositionView ? `${sideLabel(candidate.side)} / VỊ THẾ OKX` : isRealSelected ? sideLabel(candidate.side) : `${sideLabel(candidate.side)} / ĐANG XEM`;
  refs.planTag.classList.add(candidate.side);
  refs.entryValue.textContent = fmt(candidate.entry, 2);
  const currentPrice = activePriceFor(candidate);
  refs.currentValue.textContent = currentPrice === null ? "-" : fmt(currentPrice, 2);
  refs.stopValue.textContent = fmt(candidate.stop_loss, 2);
  refs.targetValue.textContent = fmt(candidate.take_profit, 2);
  refs.rrValue.textContent = fmt(candidate.risk_reward, 2);
  refs.qtyValue.textContent = fmt(candidate.quantity, 4);
  refs.spreadValue.textContent = pct(candidate.spread_pct);

  if (candidate.take_profit_pct && candidate.stop_loss_pct) {
    const targetMode = targetModeLabel(candidate.target_mode);
    refs.tpPctValue.textContent = `${fmt(candidate.take_profit_pct, 0)}% ${targetMode}`;
    refs.slPctValue.textContent = `${fmt(candidate.stop_loss_pct, 0)}% ${targetMode}`;
  } else {
    refs.tpPctValue.textContent = candidate.price_take_profit_pct ? pct(candidate.price_take_profit_pct) : "-";
    refs.slPctValue.textContent = candidate.price_stop_loss_pct ? pct(candidate.price_stop_loss_pct) : "-";
  }

  if (refs.reasonList) {
    refs.reasonList.innerHTML = "";
    if (isPositionView) {
      [
        `Vị thế OKX demo đang mở: ${sideLabel(candidate.side)} ${candidate.symbol}`,
        `Don bay: ${candidate.leverage ? `${fmt(candidate.leverage, 2)}x` : "-"}`,
        `Ky quy uoc tinh: ${candidate.margin_usdt ? `${fmt(candidate.margin_usdt, 2)} USDT` : "-"}`,
        `PnL chưa chốt: ${candidate.unrealized_pnl === null ? "-" : `${signed(candidate.unrealized_pnl, 4)} USDT`}`,
      ].forEach((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        refs.reasonList.appendChild(li);
      });
    }
    (candidate.reasons || []).forEach((reason) => {
      const li = document.createElement("li");
      li.textContent = reasonVi(reason);
      refs.reasonList.appendChild(li);
    });
  }

  if (refs.riskBlocks) {
    refs.riskBlocks.innerHTML = "";
  }
  if (refs.riskBlocks && !state.selectedViewSymbol) {
    const blocks = (decision.risk_check && decision.risk_check.reasons) || [];
    blocks.forEach((reason) => {
      const div = document.createElement("div");
      div.className = "risk-item";
      div.textContent = riskReasonVi(reason);
      refs.riskBlocks.appendChild(div);
    });
  }

  renderPlan(candidate);
}

function renderCandidates(decision) {
  const candidates = decision.candidates || [];
  const visibleSymbol = selectedCandidate(decision)?.symbol || "";
  const scan = decision.scan_comparison || {};
  refs.candidateCount.textContent = scan.enabled ? `${candidates.length} cap - giu top win giua 2 chu ky` : `${candidates.length} cap`;
  refs.candidateRows.innerHTML = "";
  candidates.forEach((candidate) => {
    const row = document.createElement("tr");
    row.className = candidate.symbol === visibleSymbol ? "selected-row candidate-row" : "candidate-row";
    row.dataset.symbol = candidate.symbol;
    row.tabIndex = 0;
    row.title = `Xem UI cua ${candidate.symbol}`;
    row.innerHTML = `
      <td>${pairCellHtml(candidate.symbol)}</td>
      <td><span class="side ${candidate.side}">${sideLabel(candidate.side)}</span></td>
      <td>${fmt(candidate.confidence, 2)}</td>
      <td>${candidate.win_probability_pct ? `${fmt(candidate.win_probability_pct, 2)}%` : "-"}</td>
      <td>${fmt(candidate.entry, 2)}</td>
      <td>${fmt(candidate.stop_loss, 2)}</td>
      <td>${fmt(candidate.take_profit, 2)}</td>
      <td>${candidate.take_profit_pct ? `${fmt(candidate.take_profit_pct, 0)} / ${fmt(candidate.stop_loss_pct, 0)}` : "-"}</td>
      <td>${fmt(candidate.news_score, 2)} / ${candidate.news_count || 0}</td>
      <td>${candidate.margin_usdt ? `${fmt(candidate.margin_usdt, 2)} USDT` : "-"}</td>
      <td>${candidate.recovery_margin_usdt ? `+${fmt(candidate.recovery_margin_usdt, 2)} USDT` : "-"}</td>
      <td><span class="source-pill ${scanSourceClass(candidate.scan_source)}">${scanSourceLabel(candidate.scan_source)}</span></td>
      <td><span class="${deltaClass(candidate.win_delta_pct)}">${deltaText(candidate.win_delta_pct)}</span></td>
      <td>${pct(candidate.spread_pct)}</td>
    `;
    row.addEventListener("click", () => {
      state.selectedViewSymbol = candidate.symbol;
      state.selectedViewSource = "candidate";
      renderDecision(decision);
      loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`));
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      row.click();
    });
    refs.candidateRows.appendChild(row);
  });
}

function tradeStatusLabel(status) {
  if (status === "OPEN") return "ĐANG MỞ";
  if (status === "CLOSED") return "ĐÃ ĐÓNG";
  return status || "-";
}

function positionSideLabel(side) {
  const normalized = String(side || "").toLowerCase();
  if (normalized === "long" || normalized === "buy") return "LONG";
  if (normalized === "short" || normalized === "sell") return "SHORT";
  return side || "-";
}

function positionSideClass(side) {
  const normalized = String(side || "").toLowerCase();
  if (normalized === "long" || normalized === "buy") return "long";
  if (normalized === "short" || normalized === "sell") return "short";
  return "";
}

function renderPaperState(paperState, paperResult = null) {
  const stateData = paperState || {};
  state.paperIntervalSeconds = Number(stateData.scan_interval_seconds || state.paperIntervalSeconds || 600);
  if (refs.paperNextScan) {
    refs.paperNextScan.textContent = `Server auto: ${intervalLabel(state.paperIntervalSeconds)}`;
  }
}

function bytesLabel(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes)) return "-";
  if (bytes >= 1024 * 1024 * 1024) return `${fmt(bytes / 1024 / 1024 / 1024, 2)} GB`;
  if (bytes >= 1024 * 1024) return `${fmt(bytes / 1024 / 1024, 2)} MB`;
  if (bytes >= 1024) return `${fmt(bytes / 1024, 2)} KB`;
  return `${bytes} B`;
}

function statusLabel(status) {
  if (status === "ok") return "OK";
  if (status === "warn") return "WARN";
  return "FAIL";
}

function renderEvidenceLines(evidence) {
  const rows = Array.isArray(evidence) ? evidence : [];
  if (!rows.length) return '<div class="health-evidence-empty">Chưa có dẫn chứng.</div>';
  return rows.map((row) => `
    <div class="health-evidence-row">
      <span>${escapeHtml(row.label || "-")}</span>
      <strong>${escapeHtml(formatCardValue(row.label, row.value))}</strong>
    </div>
  `).join("");
}

function moduleStatusLabel(status) {
  if (status === "ok") return "OK";
  if (status === "warn") return "CẦN CHÚ Ý";
  return "LỖI";
}

function systemModuleKey(module) {
  if (!module) return "";
  if (module.capital_management_group) return "capital-management::combined";
  return `${String(module.number ?? "").trim()}::${String(module.name || "").trim()}`;
}

function normalizeSystemModuleAiRange(value) {
  return String(value || "").toLowerCase() === "all" ? "all" : "current";
}

function systemModuleAiRangeLabel(value) {
  return normalizeSystemModuleAiRange(value) === "all" ? "All" : "Hiện tại";
}

function renderSystemModuleAiRangeToggle(module) {
  if (Number(module?.number || 0) !== 1) return "";
  const activeRange = normalizeSystemModuleAiRange(module?.ai_range || state.systemModuleAiRange);
  return `
    <div class="module-ai-range-toggle" role="group" aria-label="Phạm vi dữ liệu AI">
      ${["current", "all"].map((range) => `
        <button
          type="button"
          class="module-ai-range-btn ${activeRange === range ? "active" : ""}"
          data-ai-range="${range}"
          aria-pressed="${activeRange === range ? "true" : "false"}"
        >${escapeHtml(systemModuleAiRangeLabel(range))}</button>
      `).join("")}
    </div>
  `;
}

function updateSystemModuleAiRangeUi(nextRange, { loading = false } = {}) {
  const range = normalizeSystemModuleAiRange(nextRange);
  if (!refs.systemModuleDetail) return;
  refs.systemModuleDetail.classList.toggle("module-ai-range-loading", Boolean(loading));
  refs.systemModuleDetail.querySelectorAll(".module-ai-range-btn").forEach((button) => {
    const isActive = normalizeSystemModuleAiRange(button.getAttribute("data-ai-range")) === range;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
    button.disabled = Boolean(loading);
  });
}

function moduleDetailScrollTop() {
  return refs.systemModuleDetail?.querySelector(".module-chart-scroll")?.scrollTop || 0;
}

function moduleDetailActiveChartIndex() {
  return refs.systemModuleDetail?.querySelector(".module-chart-legend-item.active")?.getAttribute("data-chart-index") || null;
}

function moduleGroupCollapsedKeys() {
  const keys = new Set();
  if (!refs.systemModuleGrid) return keys;
  refs.systemModuleGrid.querySelectorAll(".module-group").forEach((group) => {
    const key = group.getAttribute("data-group-key");
    const toggle = group.querySelector(".module-group-toggle");
    const body = group.querySelector(".module-group-body");
    const collapsed = toggle?.getAttribute("aria-expanded") === "false" || Boolean(body?.hidden);
    if (key && collapsed) keys.add(key);
  });
  return keys;
}

function closeSystemModuleDetail() {
  if (refs.systemModuleOverlay) refs.systemModuleOverlay.hidden = true;
  state.selectedSystemModuleKey = null;
  if (refs.systemModuleDetail) {
    refs.systemModuleDetail.classList.remove("module-detail-chart-scroll", "market-regime-detail");
    refs.systemModuleDetail.innerHTML = "";
  }
  if (refs.systemModuleGrid) {
    refs.systemModuleGrid.querySelectorAll(".module-item").forEach((item) => item.classList.remove("selected"));
  }
  if (refs.systemChecklistGrid) {
    refs.systemChecklistGrid.querySelectorAll(".health-item").forEach((item) => item.classList.remove("selected"));
  }
}

function renderHealthCriterionDetail(item) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !item) return;
  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  state.selectedSystemModuleKey = null;
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.remove("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head">
      <div>
        <span class="module-number">Tiêu chí hệ thống</span>
        <h3 id="systemModuleTitle">${escapeHtml(item.name || "-")}</h3>
        <p>${escapeHtml(item.detail || "-")}</p>
      </div>
      <span class="status-pill ${item.status === "ok" ? "ok" : "warn"}">${statusLabel(item.status)}</span>
    </div>
    <div class="module-meta">
      <div><span>Mục tiêu</span><strong>${escapeHtml(item.target || "✓")}</strong></div>
      <div><span>Bắt buộc</span><strong>${item.required ? "Có" : "Không"}</strong></div>
      <div><span class="module-meta-inline-label">Trạng thái</span><strong>${statusLabel(item.status)}</strong></div>
    </div>
    <div class="module-stat-table health-stat-table">
      <div class="module-stat-head">Biến / bằng chứng</div>
      <div class="module-stat-head">Giá trị hiện tại</div>
      <div class="module-stat-head">Ý nghĩa cần kiểm tra</div>
      ${evidence.map((row) => `
        <div>${escapeHtml(row.label || "-")}</div>
        <div>${escapeHtml(formatCardValue(row.label, row.value))}</div>
        <div>${escapeHtml(healthEvidenceMeaning(row.label, item.name))}</div>
      `).join("") || `
        <div>-</div>
        <div>-</div>
        <div>Chưa có bằng chứng chi tiết cho tiêu chí này.</div>
      `}
    </div>
  `;
  const closeBtn = refs.systemModuleDetail.querySelector(".module-close");
  if (closeBtn) closeBtn.addEventListener("click", closeSystemModuleDetail);
}

function healthEvidenceMeaning(label, criterionName) {
  const text = String(label || "").toLowerCase();
  if (text.includes("mode")) return "Chế độ chạy hiện tại; cần chú ý khi chuyển sang demo/live.";
  if (text.includes("automation")) return "Cho biết server có tự chạy chu kỳ scan/lệnh hay không.";
  if (text.includes("interval")) return "Khoảng cách giữa hai lần scan tự động.";
  if (text.includes("ai")) return "Trạng thái policy AI; biến này cần đặc biệt chú ý để tránh gọi AI ngoài setup.";
  if (text.includes("kill") || text.includes("guard")) return "Lớp chặn an toàn khi thị trường hoặc cấu hình không đạt điều kiện.";
  if (text.includes("storage") || text.includes("atlas") || text.includes("disk")) return "Sức khỏe lưu trữ và dung lượng còn trống.";
  if (text.includes("rows") || text.includes("bytes")) return "Số dòng/kích thước dữ liệu đang được lưu.";
  if (criterionName) return `Bằng chứng dùng để đánh giá tiêu chí "${criterionName}".`;
  return "Bằng chứng dùng để đánh giá sức khỏe hệ thống.";
}

const MODULE_CHART_COLORS = ["#147a7e", "#315f9f", "#1f8a5b", "#bd3f32", "#b7791f", "#6f4fb3", "#c0568a", "#4a7c59", "#8a5a44", "#4d6b7c"];
const AI_DECISION_COLOR_BY_KEY = new Map([
  ["long_count", "#16c784"],
  ["short_count", "#ea3943"],
  ["long_percent", "#16c784"],
  ["short_percent", "#ea3943"],
]);

function viText(value) {
  const original = String(value ?? "");
  const looksBroken = (text) => /(?:�.|�.|�.|ƒ|��|“|�|—|–|\uFFFD)/.test(text);
  const mojibakeScore = (text) => (text.match(/(?:�.|�.|�.|ƒ|��|“|�|—|–|\uFFFD)/g) || []).length;
  const cp1252ReverseMap = new Map([
    [0x20AC, 0x80],
    [0x201A, 0x82],
    [0x0192, 0x83],
    [0x201E, 0x84],
    [0x2026, 0x85],
    [0x2020, 0x86],
    [0x2021, 0x87],
    [0x02C6, 0x88],
    [0x2030, 0x89],
    [0x0160, 0x8A],
    [0x2039, 0x8B],
    [0x0152, 0x8C],
    [0x017D, 0x8E],
    [0x2018, 0x91],
    [0x2019, 0x92],
    [0x201C, 0x93],
    [0x201D, 0x94],
    [0x2022, 0x95],
    [0x2013, 0x96],
    [0x2014, 0x97],
    [0x02DC, 0x98],
    [0x2122, 0x99],
    [0x0161, 0x9A],
    [0x203A, 0x9B],
    [0x0153, 0x9C],
    [0x017E, 0x9E],
    [0x0178, 0x9F],
  ]);
  const decodeUtf8FromWindows1252 = (text) => {
    try {
      const bytes = Uint8Array.from(Array.from(text), (char) => {
        const codePoint = char.codePointAt(0) || 0;
        if (codePoint <= 0xff) return codePoint;
        if (cp1252ReverseMap.has(codePoint)) return cp1252ReverseMap.get(codePoint);
        return 0x3f;
      });
      return new TextDecoder("utf-8").decode(bytes);
    } catch {
      return text;
    }
  };
  let text = original;
  for (let i = 0; i < 4; i += 1) {
    if (!looksBroken(text)) break;
    const decoded = decodeUtf8FromWindows1252(text);
    if (!decoded || decoded === text) break;
    if (mojibakeScore(decoded) > mojibakeScore(text)) break;
    text = decoded;
  }
  return text;
}

function viLabel(value) {
  return viText(value).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function moduleComparableValue(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const text = String(value ?? "").trim();
  if (!text || text === "-") return null;
  const normalized = text.replace(/,/g, "");
  if (normalized.toLowerCase() === "true") return 1;
  if (normalized.toLowerCase() === "false") return 0;
  if (!/^-?\d+(\.\d+)?(%|x)?$/i.test(normalized)) return null;
  const parsed = Number(normalized.replace(/(%|x)$/i, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function moduleNumericValue(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return Math.abs(value);
  const text = String(value ?? "").trim();
  if (!text || text === "-") return null;
  const normalized = text.replace(/,/g, "");
  if (normalized.toLowerCase() === "true") return 1;
  if (normalized.toLowerCase() === "false") return 0;
  if (!/^-?\d+(\.\d+)?(%|x)?$/i.test(normalized)) return null;
  const parsed = Number(normalized.replace(/(%|x)$/i, ""));
  return Number.isFinite(parsed) ? Math.abs(parsed) : null;
}

function moduleSignedNumericValue(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const text = String(value ?? "").trim();
  if (!text || text === "-") return null;
  const normalized = text.replace(/,/g, "");
  if (normalized.toLowerCase() === "true") return 1;
  if (normalized.toLowerCase() === "false") return 0;
  if (!/^-?\d+(\.\d+)?(%|x)?$/i.test(normalized)) return null;
  const parsed = Number(normalized.replace(/(%|x)$/i, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function moduleChartRows(rows) {
  return (Array.isArray(rows) ? rows : [])
    .filter((row) => !isTimestampLabel(row?.label || "") && !isDateOnlyLabel(row?.label || ""))
    .map((row, index) => {
      const rawValue = moduleNumericValue(row.value);
      if (rawValue === null) return null;
      return {
        ...row,
        rawNumericValue: rawValue,
        chartValue: rawValue > 0 ? rawValue : 0.2,
        color: MODULE_CHART_COLORS[index % MODULE_CHART_COLORS.length],
      };
    })
    .filter(Boolean)
    .slice(0, 10);
}

function moduleLegendCurrentValue(row) {
  const numeric = moduleNumericValue(row?.value);
  if (numeric !== null) {
    const key = String(row?.aiDecisionKey || "");
    const unitKey = viLabel(aiDecisionUnit(row));
    if (["total_decisions", "no_trade_count", "mini_no_trade_count", "ai_other_count", "long_count", "short_count"].includes(key) || unitKey === "lan" || unitKey === "lenh" || unitKey === "quyet dinh") {
      return Math.round(numeric).toLocaleString("en-US", { maximumFractionDigits: 0 });
    }
    return formatFixed2(numeric);
  }
  return formatCardValue(row?.label || "", viText(row?.value ?? "-"));
}

function aiDecisionUnit(row) {
  const key = String(row?.aiDecisionKey || "");
  if (key === "total_decisions") return "Mini + 5.5";
  if (key === "total_decisions") return "lần";
  if (key === "long_count" || key === "short_count") return "lệnh";
  if (key === "no_trade_count" || key === "mini_no_trade_count") return "lần";
  if (key.includes("percent") || key.includes("winrate")) return "%";
  if (key.includes("confidence")) return "điểm";
  if (key.includes("profit_factor")) return "hệ số";
  if (key === "bias_warning") return "trạng thái";
  return String(row?.unit || "");
}

function moduleDisplayLabel(row) {
  const label = String(row?.label || "-");
  const unit = aiDecisionUnit(row);
  return unit ? `${label} (${unit})` : label;
}

function moduleLegendShare(chartRows, row) {
  const totalRaw = chartRows.reduce((sum, item) => sum + Math.max(0, Number(item.rawNumericValue || 0)), 0);
  if (!totalRaw) return "0.00%";
  return `${formatFixed2(Math.max(0, Number(row.rawNumericValue || 0)) / totalRaw * 100)}%`;
}
function moduleAxisTickLabel(value) {
  return formatFixed2(value);
}

function moduleDeltaValueLabel(row, value) {
  const unitKey = viLabel(aiDecisionUnit(row));
  if (unitKey === "lan" || unitKey === "lenh" || unitKey === "quyet dinh") {
    return Math.round(Number(value || 0)).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  return moduleAxisTickLabel(value);
}

function moduleDeltaSuffix(row) {
  return String(row?.value ?? "").includes("%") ? "%" : "";
}

function modulePreviousStatRow(module, row) {
  const modules = Array.isArray(state.previousSystemChecklistPayload?.modules)
    ? state.previousSystemChecklistPayload.modules
    : [];
  const moduleKey = String(module?.number ?? "").trim();
  const labelKey = viLabel(row?.label || "");
  const rawKey = String(row?.riskKey || row?.aiDecisionKey || "").trim();
  for (const item of modules) {
    if (String(item?.number ?? "").trim() !== moduleKey) continue;
    const stats = Array.isArray(item?.stats) ? item.stats : [];
    if (rawKey) {
      const rawMatch = stats.find((entry) => String(entry?.label || "").trim() === rawKey);
      if (rawMatch) return rawMatch;
    }
    const match = stats.find((entry) => viLabel(entry?.label || "") === labelKey);
    if (match) return match;
  }
  return null;
}

function moduleDeltaInfo(module, row) {
  const currentValue = moduleComparableValue(row?.value);
  const previousValue = moduleComparableValue(modulePreviousStatRow(module, row)?.value);
  const suffix = moduleDeltaSuffix(row);
  if (currentValue === null || previousValue === null) {
    return { state: "flat", text: "0" };
  }
  const delta = currentValue - previousValue;
  if (Math.abs(delta) < 1e-9) {
    return { state: "flat", text: "0" };
  }
  return {
    state: delta > 0 ? "up" : "down",
    text: `${delta > 0 ? "+" : "-"}${moduleDeltaValueLabel(row, Math.abs(delta))}${suffix}`,
  };
}

function moduleChangedVariableCount(module, rows = null) {
  const sourceRows = Array.isArray(rows)
    ? rows
    : moduleDisplayRows(module, Array.isArray(module?.stats) ? module.stats : []);
  return sourceRows.reduce((count, row) => {
    const delta = moduleDeltaInfo(module, row);
    return delta.state === "up" || delta.state === "down" ? count + 1 : count;
  }, 0);
}

const AI_DECISION_ROW_CONFIG = [
  ["total_decisions", "Tổng lần AI được gọi", "Tổng số lần Mini và 5.5 được gọi thật theo phạm vi đang chọn; không tính record scan nội bộ.", "AI được gọi nhiều hơn trong phạm vi.", "AI được gọi ít hơn trong phạm vi."],
  ["long_count", "Mini chọn LONG", "Số lần Mini gọi thật và chọn setup LONG.", "Mini chọn LONG nhiều hơn.", "Mini chọn LONG ít hơn."],
  ["short_count", "Mini chọn SHORT", "Số lần Mini gọi thật và chọn setup SHORT.", "Mini chọn SHORT nhiều hơn.", "Mini chọn SHORT ít hơn."],
  ["mini_no_trade_count", "Mini không chọn lệnh", "Số lần Mini được gọi nhưng trả NO_TRADE hoặc không chọn cặp nào.", "Mini từ chối nhiều hơn.", "Mini từ chối ít hơn."],
  ["no_trade_count", "5.5 từ chối/xóa setup", "Số lần GPT-5.5 từ chối vào lệnh hoặc xóa setup; không tính các lần giữ setup.", "5.5 từ chối/xóa setup nhiều hơn.", "5.5 từ chối/xóa setup ít hơn."],
  ["long_percent", "Tỷ lệ LONG", "Tỷ lệ quyết định LONG trên tổng quyết định AI thực.", "AI đang thiên về LONG.", "AI giảm xu hướng LONG."],
  ["short_percent", "Tỷ lệ SHORT", "Tỷ lệ quyết định SHORT trên tổng quyết định AI thực.", "AI đang thiên về SHORT.", "AI giảm xu hướng SHORT."],
  ["winrate_long", "Tỷ lệ thắng LONG", "Tỷ lệ thắng của các lệnh LONG.", "LONG đang hoạt động hiệu quả hơn.", "Hiệu quả LONG giảm."],
  ["winrate_short", "Tỷ lệ thắng SHORT", "Tỷ lệ thắng của các lệnh SHORT.", "SHORT đang hoạt động hiệu quả hơn.", "Hiệu quả SHORT giảm."],
  ["profit_factor_long", "Hệ số lợi nhuận LONG", "Hiệu quả sinh lời của các lệnh LONG. Giá trị lớn hơn 1 cho thấy LONG đang có lợi nhuận.", "Hiệu quả LONG tăng.", "Hiệu quả LONG giảm."],
  ["profit_factor_short", "Hệ số lợi nhuận SHORT", "Hiệu quả sinh lời của các lệnh SHORT. Giá trị lớn hơn 1 cho thấy SHORT đang có lợi nhuận.", "Hiệu quả SHORT tăng.", "Hiệu quả SHORT giảm."],
  ["avg_confidence_long", "Độ tin cậy LONG", "Mức độ tự tin trung bình của AI khi quyết định LONG.", "AI tự tin hơn với LONG.", "AI ít tự tin hơn với LONG."],
  ["avg_confidence_short", "Độ tin cậy SHORT", "Mức độ tự tin trung bình của AI khi quyết định SHORT.", "AI tự tin hơn với SHORT.", "AI ít tự tin hơn với SHORT."],
  ["bias_warning", "Cảnh báo lệch hướng", "Cảnh báo khi AI có xu hướng thiên quá nhiều về LONG hoặc SHORT.", "AI đang lệch hướng mạnh hơn.", "AI giảm mức lệch hướng.", true],
].map(([key, label, meaning, up, down, isBiasWarning], index) => ({
  key,
  label,
  meaning,
  up,
  down,
  isBiasWarning: Boolean(isBiasWarning),
  order: index,
}));

AI_DECISION_ROW_CONFIG.splice(5, 0, {
  key: "ai_other_count",
  label: "AI khác / Chưa phân loại",
  meaning: "Số lần AI đã gọi nhưng không thuộc Mini không chọn, Mini chọn LONG/SHORT hoặc 5.5 từ chối/xóa setup.",
  up: "AI khác / Chưa phân loại tăng.",
  down: "AI khác / Chưa phân loại giảm.",
  isBiasWarning: false,
  order: 5,
});

const AI_DECISION_ROW_KEYS = new Set(AI_DECISION_ROW_CONFIG.map((item) => item.key));

function moduleAiRowKey(row) {
  return String(row?.label || "").trim();
}

function moduleAiDecisionRows(rows) {
  const byKey = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => byKey.set(moduleAiRowKey(row), row));
  return AI_DECISION_ROW_CONFIG
    .map((config) => {
      const row = byKey.get(config.key);
      if (!row) return null;
      return {
        ...row,
        label: config.label,
        meaning: config.meaning,
        trendUp: config.up,
        trendDown: config.down,
        aiDecisionKey: config.key,
        aiDecisionOrder: config.order,
        isBiasWarning: config.isBiasWarning,
      };
    })
    .filter(Boolean);
}

function moduleDisplayRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  if (moduleNumber !== 1) return Array.isArray(rows) ? rows : [];
  const aiRows = moduleAiDecisionRows(rows);
  if (aiRows.length) return aiRows;
  return (Array.isArray(rows) ? rows : []).filter((row) => {
    const label = String(row?.label || "").toLowerCase();
    return !label.includes("ngày kiểm tra") && !label.includes("cập nhật lúc");
  });
}

function moduleAuxRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  if (moduleNumber !== 1) return [];
  return (Array.isArray(rows) ? rows : []).filter((row) => {
    const label = String(row?.label || "").toLowerCase();
    return label.includes("ngày kiểm tra") || label.includes("cập nhật lúc");
  });
}

function moduleAuxRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  if (moduleNumber !== 1) return [];
  return (Array.isArray(rows) ? rows : []).filter((row) => {
    const label = String(row?.label || "").toLowerCase();
    return !AI_DECISION_ROW_KEYS.has(moduleAiRowKey(row))
      || label.includes("ngÃ y kiá»ƒm tra")
      || label.includes("cáº­p nháº­t lÃºc");
  });
}

function moduleToneClass(row) {
  const rawText = `${row?.label || ""} ${row?.value || ""}`.toLowerCase();
  if (rawText.includes("hard recovery") || rawText.includes("pause")) return "danger";
  if (rawText.includes("soft recovery") || rawText.includes("recovery mode")) return "warning";
  const text = viLabel(rawText);
  if (text.includes("hard recovery") || text.includes("pause") || text.includes("tam dung")) return "danger";
  if (text.includes("soft recovery") || text.includes("recovery") || text.includes("can chu y")) return "warning";
  if (text.includes("loss") || text.includes("drawdown") || text.includes("loi")) return "danger";
  if (text.includes("ok") || text.includes("tat") || text.includes("normal") || text.includes("binh thuong")) return "success";
  return row?.attention ? "warning" : "neutral";
}

function polarPoint(cx, cy, radius, angle) {
  const radians = (angle - 90) * Math.PI / 180;
  return { x: cx + radius * Math.cos(radians), y: cy + radius * Math.sin(radians) };
}

function donutSegmentPath(cx, cy, outerRadius, innerRadius, startAngle, endAngle) {
  const outerStart = polarPoint(cx, cy, outerRadius, endAngle);
  const outerEnd = polarPoint(cx, cy, outerRadius, startAngle);
  const innerStart = polarPoint(cx, cy, innerRadius, startAngle);
  const innerEnd = polarPoint(cx, cy, innerRadius, endAngle);
  const largeArc = endAngle - startAngle > 180 ? 1 : 0;
  return [
    `M ${outerStart.x} ${outerStart.y}`,
    `A ${outerRadius} ${outerRadius} 0 ${largeArc} 0 ${outerEnd.x} ${outerEnd.y}`,
    `L ${innerStart.x} ${innerStart.y}`,
    `A ${innerRadius} ${innerRadius} 0 ${largeArc} 1 ${innerEnd.x} ${innerEnd.y}`,
    "Z",
  ].join(" ");
}

function moduleTrendMeaning(row) {
  const label = String(row.label || "").toLowerCase();
  if (label.includes("enabled") || label.startsWith("is") || label.includes("allow") || label.includes("block")) {
    return {
      up: "Tăng/bật nghĩa là cơ chế này đang tham gia mạnh hơn vào vận hành.",
      down: "Giảm/tắt nghĩa là cơ chế này ít tác động hơn hoặc đang bị vô hiệu.",
    };
  }
  if (label.includes("loss") || label.includes("drawdown") || label.includes("error") || label.includes("spread") || label.includes("drift")) {
    return {
      up: "Tăng nghĩa là rủi ro hoặc độ lệch đang lớn hơn, cần kiểm tra kỹ.",
      down: "Giảm nghĩa là rủi ro/độ lệch đang hạ nhiệt.",
    };
  }
  if (label.includes("win") || label.includes("profit") || label.includes("confidence") || label.includes("score")) {
    return {
      up: "Tăng nghĩa là chất lượng tín hiệu hoặc sức khỏe chiến lược tốt hơn.",
      down: "Giảm nghĩa là chất lượng tín hiệu yếu đi, cần xem lại điều kiện vào lệnh.",
    };
  }
  if (label.includes("count") || label.includes("active") || label.includes("calls") || label.includes("replay")) {
    return {
      up: "Tăng nghĩa là tần suất/khối lượng hoạt động của biến này lớn hơn.",
      down: "Giảm nghĩa là biến này ít phát sinh hơn hoặc dữ liệu đang ít đi.",
    };
  }
  if (label.includes("max") || label.includes("threshold") || label.includes("limit")) {
    return {
      up: "Tăng nghĩa là ngưỡng cho phép rộng hơn hoặc mức chịu đựng cao hơn.",
      down: "Giảm nghĩa là hệ thống siết điều kiện hoặc giảm mức chịu đựng.",
    };
  }
  return {
    up: "Tăng nghĩa là tỷ trọng của biến này trong module lớn hơn.",
    down: "Giảm nghĩa là tỷ trọng của biến này trong module nhỏ hơn.",
  };
}

function renderModuleVariableRows(module, chartRows, showShare = false) {
  return chartRows.map((row, index) => {
    const trend = moduleTrendMeaning(row);
    const share = moduleLegendShare(chartRows, row);
    const currentValue = moduleLegendCurrentValue(row);
    const delta = moduleDeltaInfo(module, row);
    return `
      <button class="module-chart-legend-item ${row.attention ? "attention" : ""} ${moduleToneClass(row)}" type="button" data-chart-index="${index}" title="${escapeHtml(row.label || "-")}: ${escapeHtml(currentValue)}">
        <span class="module-chart-swatch" style="background:${row.color}"></span>
        <div>
          <strong>${escapeHtml(row.label || "-")}</strong>
          <small class="module-chart-value-line"><span>Giá trị hiện tại: ${escapeHtml(currentValue)}</span><span class="module-chart-delta ${delta.state}">${escapeHtml(delta.text)}</span></small>
          ${showShare ? `<small class="module-chart-share-line">Tỷ trọng trên biểu đồ: ${escapeHtml(share)}${row.attention ? " · cần chú ý" : ""}</small>` : ""}
          <p>${escapeHtml(row.meaning || "Biến dùng để theo dõi trạng thái module.")}</p>
          <p><b>Tăng:</b> ${escapeHtml(trend.up)}</p>
          <p><b>Giảm:</b> ${escapeHtml(trend.down)}</p>
        </div>
      </button>
    `;
  }).join("");
}

function formatBiasWarningValue(value) {
  const text = viText(value || "Normal");
  const key = viLabel(text);
  if (key.includes("long")) return "🟡 LONG Bias";
  if (key.includes("short")) return "🟠 SHORT Bias";
  return "🟢 Normal";
}

function renderModuleVariableRows(module, chartRows, showShare = false) {
  return chartRows.map((row, index) => {
    const chartIndex = row.chartIndex ?? index;
    const aiKey = row.aiDecisionKey ? String(row.aiDecisionKey) : "";
    const trend = {
      ...moduleTrendMeaning(row),
      ...(row.trendUp ? { up: row.trendUp } : {}),
      ...(row.trendDown ? { down: row.trendDown } : {}),
    };
    const share = moduleLegendShare(chartRows, row);
    const currentValue = moduleLegendCurrentValue(row);
    const displayLabel = moduleDisplayLabel(row);
    const helpText = moduleHelpText(row);
    const labelHtml = helpText
      ? `<span class="module-label-with-help">${escapeHtml(displayLabel)}${renderHelpBadge(helpText)}</span>`
      : escapeHtml(displayLabel);
    const delta = moduleDeltaInfo(module, row);
    const valueLine = row.isBiasWarning
      ? `<small class="module-chart-value-line"><span>Hiển thị:</span><span class="module-chart-delta flat">${escapeHtml(formatBiasWarningValue(row.value))}</span></small>`
      : `<small class="module-chart-value-line"><span>Giá trị hiện tại: ${escapeHtml(currentValue)}</span><span class="module-chart-delta ${delta.state}">${escapeHtml(delta.text)}</span></small>`;
    return `
      <button class="module-chart-legend-item ${row.attention ? "attention" : ""} ${moduleToneClass(row)}" type="button" data-chart-index="${chartIndex}" ${aiKey ? `data-ai-key="${escapeHtml(aiKey)}"` : ""} title="${escapeHtml(displayLabel)}: ${escapeHtml(currentValue)}">
        <span class="module-chart-swatch" style="background:${row.color}"></span>
        <div>
          <strong>${labelHtml}</strong>
          ${valueLine}
          ${showShare ? `<small class="module-chart-share-line">Tỷ trọng trên biểu đồ: ${escapeHtml(share)}${row.attention ? " · cần chú ý" : ""}</small>` : ""}
          <p>${escapeHtml(row.meaning || "Biến dùng để theo dõi trạng thái module.")}</p>
          <p><b>Tăng:</b> ${escapeHtml(trend.up)}</p>
          <p><b>Giảm:</b> ${escapeHtml(trend.down)}</p>
        </div>
      </button>
    `;
  }).join("");
}

const PROFIT_FACTOR_HELP_TEXT = [
  "Hệ số lợi nhuận = tổng PnL lời / tổng giá trị tuyệt đối của PnL lỗ.",
  "Ý nghĩa: cho biết hướng LONG/SHORT lời gấp bao nhiêu lần phần lỗ của chính hướng đó.",
  "Mốc 1.0 là hòa vốn; >1 là lời nhiều hơn lỗ, <1 là lỗ nhiều hơn lời.",
  "Dùng để so sánh LONG và SHORT: hướng nào hệ số cao hơn thì đang kiếm tiền hiệu quả hơn.",
].join(" ");

function moduleHelpText(row) {
  const key = String(row?.aiDecisionKey || "");
  if (key === "profit_factor_long" || key === "profit_factor_short") {
    return PROFIT_FACTOR_HELP_TEXT;
  }
  if (key === "ai_other_count") {
    return "Số lượt AI còn lại sau khi trừ Mini không chọn, LONG, SHORT và 5.5 từ chối/xóa setup. Gồm các trạng thái giữ setup, lỗi review, trung gian hoặc log thiếu dữ liệu.";
  }
  return "";
}

function renderHelpBadge(helpText) {
  if (!helpText) return "";
  return `
    <span class="module-help" aria-label="${escapeHtml(helpText)}">
      ?
      <span class="module-help-tooltip" role="tooltip">${escapeHtml(helpText)}</span>
    </span>
  `;
}

function moduleBarPercentValue(row, maxRawValue) {
  const raw = Math.max(0, Number(row?.rawNumericValue || 0));
  const label = viLabel(row?.label || "");
  const valueText = String(row?.value ?? "");
  const looksPercent = aiDecisionUnit(row) === "%"
    || valueText.includes("%")
    || label.includes("percent")
    || label.includes("ty le")
    || label.includes("rate")
    || label.includes("ratio")
    || label.includes("confidence")
    || label.includes("score");
  if (looksPercent) return Math.max(0, Math.min(100, raw));
  if (!maxRawValue) return 0;
  return Math.max(0, Math.min(100, raw / maxRawValue * 100));
}

function niceAutoPercentAxisMax(maxValue) {
  const raw = Math.max(0, Number(maxValue || 0));
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  if (raw > 80) return 100;
  const target = raw * 1.2;
  const exponent = Math.floor(Math.log10(target));
  const base = 10 ** exponent;
  const mantissas = [1, 2, 4, 5, 8, 10];
  let best = 100;
  let bestDistance = Infinity;
  mantissas.forEach((mantissa) => {
    const candidate = mantissa * base;
    if (candidate < raw) return;
    const distance = Math.abs(candidate - target);
    if (distance < bestDistance || (Math.abs(distance - bestDistance) < 1e-9 && candidate > best)) {
      best = candidate;
      bestDistance = distance;
    }
  });
  if (best === 100 && raw <= 80 && target < 100) return 80;
  return Math.min(100, best);
}

function formatAxisPercentLabel(value) {
  const numeric = Number(value || 0);
  if (Math.abs(numeric) < 1e-9) return "0%";
  return `${numeric.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })}%`;
}

function niceAutoFactorAxisMax(maxValue) {
  const raw = Math.max(0, Number(maxValue || 0));
  const target = Math.max(raw, 1) * 1.2;
  const exponent = Math.floor(Math.log10(target));
  const base = 10 ** exponent;
  const mantissas = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 8, 10];
  for (const mantissa of mantissas) {
    const candidate = mantissa * base;
    if (candidate >= target) return candidate;
  }
  return 10 * base;
}

function formatAxisFactorLabel(value) {
  const numeric = Number(value || 0);
  if (Math.abs(numeric) < 1e-9) return "0";
  return numeric.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: numeric < 10 ? 2 : 1,
  });
}

function aiDecisionRow(rows, key) {
  return (Array.isArray(rows) ? rows : []).find((row) => row.aiDecisionKey === key) || null;
}

function aiDecisionDirectionLabel(row) {
  const key = String(row?.aiDecisionKey || "");
  if (key === "long_count" || key === "long_percent") return "LONG";
  if (key === "short_count" || key === "short_percent") return "SHORT";
  return moduleDisplayLabel(row);
}

function aiDecisionChartRow(rows, key, chartIndex, colorIndex, tooltipValue = null) {
  const row = aiDecisionRow(rows, key);
  if (!row) return null;
  const rawValue = moduleNumericValue(row.value);
  if (rawValue === null) return null;
  return {
    ...row,
    rawNumericValue: rawValue,
    chartValue: rawValue > 0 ? rawValue : 0.2,
    chartIndex,
    color: AI_DECISION_COLOR_BY_KEY.get(key) || MODULE_CHART_COLORS[colorIndex % MODULE_CHART_COLORS.length],
    tooltipValue,
  };
}

const AI_DECISION_CHART_META = new Map([
  ["long_count", { chartIndex: 1, colorIndex: 1 }],
  ["short_count", { chartIndex: 2, colorIndex: 2 }],
  ["long_percent", { chartIndex: 4, colorIndex: 4 }],
  ["short_percent", { chartIndex: 5, colorIndex: 5 }],
  ["winrate_long", { chartIndex: 6, colorIndex: 6 }],
  ["winrate_short", { chartIndex: 7, colorIndex: 7 }],
  ["avg_confidence_long", { chartIndex: 8, colorIndex: 8 }],
  ["avg_confidence_short", { chartIndex: 9, colorIndex: 9 }],
  ["profit_factor_long", { chartIndex: 10, colorIndex: 10 }],
  ["profit_factor_short", { chartIndex: 11, colorIndex: 11 }],
]);

function aiDecisionLegendRows(rows) {
  const hiddenKeys = new Set([
    "total_decisions",
    "long_count",
    "short_count",
    "mini_no_trade_count",
    "no_trade_count",
    "ai_other_count",
    "bias_warning",
  ]);
  const hiddenLabelPrefixes = [
    "mini chon long",
    "mini chon short",
    "mini khong chon lenh",
  ];
  return (Array.isArray(rows) ? rows : []).map((row, index) => {
    const numeric = moduleNumericValue(row.value);
    const key = String(row?.aiDecisionKey || "");
    const chartMeta = AI_DECISION_CHART_META.get(key);
    const chartIndex = chartMeta?.chartIndex ?? index;
    const colorIndex = chartMeta?.colorIndex ?? index;
    return {
      ...row,
      rawNumericValue: numeric === null ? 0 : numeric,
      chartValue: numeric && numeric > 0 ? numeric : 0.2,
      chartIndex,
      color: AI_DECISION_COLOR_BY_KEY.get(key) || MODULE_CHART_COLORS[colorIndex % MODULE_CHART_COLORS.length],
    };
  }).filter((row) => {
    const key = String(row?.aiDecisionKey || "");
    const label = viLabel(moduleDisplayLabel(row));
    return !hiddenKeys.has(key) && !hiddenLabelPrefixes.some((prefix) => label.startsWith(prefix));
  });
}

function renderAiDecisionBiasCard(module, row) {
  if (!row) return "";
  const trend = {
    ...moduleTrendMeaning(row),
    ...(row.trendUp ? { up: row.trendUp } : {}),
    ...(row.trendDown ? { down: row.trendDown } : {}),
  };
  const currentValue = moduleLegendCurrentValue(row);
  const displayLabel = moduleDisplayLabel(row);
  return `
    <div class="module-chart-legend-item module-chart-status-item module-ai-bias-card" role="status" title="${escapeHtml(displayLabel)}: ${escapeHtml(currentValue)}">
      <span class="module-chart-swatch" style="background:${MODULE_CHART_COLORS[2]}"></span>
      <div>
        <strong>${escapeHtml(displayLabel)}</strong>
        <small class="module-chart-value-line"><span>Hiển thị:</span><span class="module-chart-delta flat">${escapeHtml(formatBiasWarningValue(row.value))}</span></small>
        <p>${escapeHtml(row.meaning || "Biến dùng để theo dõi trạng thái module.")}</p>
        <p><b>Tăng:</b> ${escapeHtml(trend.up)}</p>
        <p><b>Giảm:</b> ${escapeHtml(trend.down)}</p>
      </div>
    </div>
  `;
}

function renderAiDecisionKpi(module, row) {
  if (!row) return "";
  const totalTarget = row.chartTotalTarget ? String(row.chartTotalTarget) : "";
  const delta = moduleDeltaInfo(module, row);
  return `
    <div class="module-chart-meta" ${totalTarget ? `data-chart-total-target="${escapeHtml(totalTarget)}"` : ""}>
      <div class="module-total-anchor">
        <span class="module-chart-delta ${delta.state}">${escapeHtml(delta.text)}</span>
        <span>${escapeHtml(moduleDisplayLabel(row))}</span>
        <strong>${escapeHtml(moduleLegendCurrentValue(row))}</strong>
      </div>
    </div>
  `;
}

function renderAiDecisionKpiGroup(module, rows) {
  const items = (Array.isArray(rows) ? rows : []).filter(Boolean);
  if (!items.length) return "";
  return `
    <div class="module-chart-meta module-ai-kpi-row">
      ${items.map((row) => {
        const delta = moduleDeltaInfo(module, row);
        const label = moduleDisplayLabel(row);
        const helpText = moduleHelpText(row);
        const labelHtml = helpText
          ? `<span class="module-label-with-help">${escapeHtml(label)}${renderHelpBadge(helpText)}</span>`
          : escapeHtml(label);
        return `
          <div class="module-total-anchor">
            <span class="module-chart-delta ${delta.state}">${escapeHtml(delta.text)}</span>
            <span>${labelHtml}</span>
            <strong>${escapeHtml(moduleLegendCurrentValue(row))}</strong>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderChartTitle(title, subtitle, helpText = "") {
  return `
    <div class="module-chart-title">
      <strong>${escapeHtml(title)}${renderHelpBadge(helpText)}</strong>
      ${subtitle ? `<small>${escapeHtml(subtitle)}</small>` : ""}
    </div>
  `;
}

function chartAxisLabelLines(row) {
  const label = moduleDisplayLabel(row);
  const unitMatch = label.match(/^(.*)\s+(\([^)]*\))$/);
  if (unitMatch) return [unitMatch[1], unitMatch[2]];
  const words = label.split(/\s+/).filter(Boolean);
  if (words.length <= 2) return [label];
  const midpoint = Math.ceil(words.length / 2);
  return [words.slice(0, midpoint).join(" "), words.slice(midpoint).join(" ")];
}

function renderChartAxisLabel(row, x, y) {
  const lines = chartAxisLabelLines(row).slice(0, 2);
  return `
    <text x="${x}" y="${y}" text-anchor="middle" class="module-bar-index">
      ${lines.map((line, index) => `<tspan x="${x}" dy="${index ? 14 : 0}">${escapeHtml(line)}</tspan>`).join("")}
    </text>
  `;
}

function renderAiDecisionDonut(rows, title, subtitle, centerLabel, caption) {
  const chartRows = rows.filter(Boolean);
  if (!chartRows.length) {
    return '<div class="module-chart-empty">Chưa có dữ liệu Decision Distribution.</div>';
  }
  const total = chartRows.reduce((sum, row) => sum + row.chartValue, 0) || 1;
  let cursor = 180;
  const segments = chartRows.map((row) => {
    const angle = row.chartValue / total * 360;
    const startAngle = cursor;
    const endAngle = cursor + angle;
    const midAngle = startAngle + angle / 2;
    const path = donutSegmentPath(100, 100, 82, 45, startAngle, endAngle);
    cursor += angle;
    const tooltip = row.tooltipValue || `${moduleDisplayLabel(row)}: ${row.value ?? "-"}`;
    return `<path class="module-chart-segment module-donut-segment" data-chart-index="${row.chartIndex}" data-mid-angle="${midAngle}" d="${path}" fill="${row.color}"><title>${escapeHtml(tooltip)}</title></path>`;
  }).join("");
  return `
    <div>
      ${renderChartTitle(title, subtitle)}
      <div class="module-chart-wrap">
        <svg class="module-donut" data-chart-total-target="ai-entry-total" viewBox="0 0 200 200" role="img" aria-label="${escapeHtml(title)}">
          ${segments}
          <circle cx="100" cy="100" r="39" fill="#fbfcfc"></circle>
          <text x="100" y="96" text-anchor="middle" class="module-donut-total">${escapeHtml(centerLabel)}</text>
          <text x="100" y="115" text-anchor="middle" class="module-donut-caption">${escapeHtml(caption)}</text>
          <g class="module-chart-callout" hidden>
            <line class="module-chart-callout-line" x1="0" y1="0" x2="0" y2="0"></line>
            <rect class="module-chart-callout-box" x="0" y="0" width="0" height="0" rx="6"></rect>
            <text class="module-chart-callout-text" x="0" y="0"></text>
          </g>
        </svg>
      </div>
      <div class="module-donut-keys">
        ${chartRows.map((row) => `<span><i style="background:${row.color}"></i>${escapeHtml(aiDecisionDirectionLabel(row))}: ${escapeHtml(moduleLegendCurrentValue(row))}</span>`).join("")}
      </div>
    </div>
  `;
}

function renderAiDecisionBarSvg(rows, title, subtitle, chartId) {
  const chartRows = rows.filter(Boolean);
  if (!chartRows.length) {
    return `<div class="module-chart-empty">Chưa có dữ liệu ${escapeHtml(title)}.</div>`;
  }
  const rawValues = chartRows.map((row) => Math.max(0, Number(row.rawNumericValue || 0)));
  const maxChartValue = Math.max(...rawValues, 0);
  const maxRawValue = Math.max(maxChartValue, 1);
  const chartLeft = 62;
  const chartRight = 398;
  const chartTop = 34;
  const chartBaseline = 196;
  const chartHeight = chartBaseline - chartTop;
  const barWidth = (chartRight - chartLeft - 10) / chartRows.length;
  const isProfitFactorChart = chartId === "ai-profit";
  const autoScaleYAxis = ["ai-entry-percent", "ai-winrate"].includes(chartId);
  const barPercentValues = chartRows.map((row) => moduleBarPercentValue(row, maxRawValue));
  const barAxisValues = isProfitFactorChart ? rawValues : barPercentValues;
  const maxAxisValue = Math.max(...barAxisValues, 0);
  const yAxisMax = isProfitFactorChart
    ? niceAutoFactorAxisMax(maxAxisValue)
    : autoScaleYAxis
      ? niceAutoPercentAxisMax(maxAxisValue)
      : 100;
  const tickValues = (autoScaleYAxis || isProfitFactorChart)
    ? [0, 1, 2, 3, 4].map((step) => yAxisMax * step / 4)
    : [25, 50, 75, 100];
  const yTicks = tickValues.map((tickValue) => {
    const y = chartBaseline - (tickValue / yAxisMax) * chartHeight;
    const tickLabel = isProfitFactorChart
      ? formatAxisFactorLabel(tickValue)
      : autoScaleYAxis
        ? formatAxisPercentLabel(tickValue)
        : `${formatFixed2(tickValue)}%`;
    const gridLine = Math.abs(tickValue) < 1e-9
      ? ""
      : `<line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="module-bar-grid"></line>`;
    return `
      <g>
        ${gridLine}
        <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="module-axis-label">${tickLabel}</text>
      </g>
    `;
  }).join("");
  const profitFactorMarker = isProfitFactorChart && yAxisMax >= 1
    ? (() => {
      const markerY = chartBaseline - (1 / yAxisMax) * chartHeight;
      return `
        <g>
          <line x1="${chartLeft}" y1="${markerY}" x2="${chartRight}" y2="${markerY}" class="module-bar-grid"></line>
          <text x="${chartRight}" y="${markerY - 7}" text-anchor="end" class="module-axis-label">mốc 1.0</text>
        </g>
      `;
    })()
    : "";
  const bars = chartRows.map((row, index) => {
    const axisValue = barAxisValues[index] ?? 0;
    const scaledPercent = Math.max(0, Math.min(100, axisValue / yAxisMax * 100));
    const height = Math.max(8, scaledPercent / 100 * chartHeight);
    const x = chartLeft + index * barWidth + barWidth * 0.18;
    const y = chartBaseline - height;
    const width = Math.max(28, barWidth * 0.64);
    const xLabel = moduleLegendCurrentValue(row);
    return `
      <g>
        <rect class="module-chart-segment module-bar-segment" data-chart-index="${row.chartIndex}" x="${x}" y="${y}" width="${width}" height="${height}" rx="4" fill="${row.color}">
          <title>${escapeHtml(moduleDisplayLabel(row))}: ${escapeHtml(xLabel)}</title>
        </rect>
        ${renderChartAxisLabel(row, x + width / 2, 214)}
      </g>
    `;
  }).join("");
  const markerId = `module-y-arrow-${chartId}`;
  const chartHelpText = isProfitFactorChart ? PROFIT_FACTOR_HELP_TEXT : "";
  return `
    <div>
      ${renderChartTitle(title, subtitle, chartHelpText)}
      <div class="module-chart-wrap">
        <svg class="module-bar-chart" viewBox="0 0 430 264" role="img" aria-label="${escapeHtml(title)}">
          <defs>
            <marker id="${markerId}" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M 0 8 L 4 0 L 8 8 Z" class="module-axis-arrow-head"></path>
            </marker>
          </defs>
          ${yTicks}
          ${profitFactorMarker}
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartRight}" y2="${chartBaseline}" class="module-bar-axis"></line>
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartLeft}" y2="${chartTop - 12}" class="module-bar-axis module-y-axis" marker-end="url(#${markerId})"></line>
          <text x="${chartLeft - 12}" y="${chartTop - 16}" text-anchor="middle" class="module-axis-percent">${isProfitFactorChart ? "hệ số" : "%"}</text>
          ${bars}
          <g class="module-chart-callout" hidden>
            <line class="module-chart-callout-line" x1="0" y1="0" x2="0" y2="0"></line>
            <rect class="module-chart-callout-box" x="0" y="0" width="0" height="0" rx="6"></rect>
            <text class="module-chart-callout-text" x="0" y="0"></text>
          </g>
        </svg>
      </div>
    </div>
  `;
}

function renderAiDecisionModuleChart(module, rows) {
  const totalDecisionRow = aiDecisionRow(rows, "total_decisions");
  const miniNoTradeRow = aiDecisionRow(rows, "mini_no_trade_count");
  const noTradeRow = aiDecisionRow(rows, "no_trade_count");
  const otherAiRow = aiDecisionRow(rows, "ai_other_count");
  const biasWarningRow = aiDecisionRow(rows, "bias_warning");
  const longPercent = aiDecisionRow(rows, "long_percent");
  const shortPercent = aiDecisionRow(rows, "short_percent");
  const longOrderRow = aiDecisionChartRow(rows, "long_count", 1, 1);
  const shortOrderRow = aiDecisionChartRow(rows, "short_count", 2, 2);
  const entryTotal = [longOrderRow, shortOrderRow].filter(Boolean).reduce((sum, row) => sum + Number(row.rawNumericValue || 0), 0);
  const entryDirectionRows = [
    longOrderRow ? { ...longOrderRow, tooltipValue: `Số LONG: ${moduleLegendCurrentValue(longOrderRow)} | Tỷ lệ LONG: ${longPercent?.value ?? "-"}` } : null,
    shortOrderRow ? { ...shortOrderRow, tooltipValue: `Số SHORT: ${moduleLegendCurrentValue(shortOrderRow)} | Tỷ lệ SHORT: ${shortPercent?.value ?? "-"}` } : null,
  ];
  const totalKpiRow = totalDecisionRow ? { ...totalDecisionRow } : null;
  const directionPercentRows = [
    aiDecisionChartRow(rows, "long_percent", 4, 4),
    aiDecisionChartRow(rows, "short_percent", 5, 5),
  ];
  const winrateRows = [
    aiDecisionChartRow(rows, "winrate_long", 6, 6),
    aiDecisionChartRow(rows, "winrate_short", 7, 7),
  ];
  const profitRows = [
    aiDecisionChartRow(rows, "profit_factor_long", 10, 10),
    aiDecisionChartRow(rows, "profit_factor_short", 11, 11),
  ];
  const confidenceRows = [
    aiDecisionChartRow(rows, "avg_confidence_long", 8, 8),
    aiDecisionChartRow(rows, "avg_confidence_short", 9, 9),
  ];
  return `
    <section class="module-chart-panel module-chart-panel-compact module-ai-decision-panel">
      <div class="module-chart-legend module-ai-chart-stack">
        ${renderAiDecisionKpiGroup(module, [totalKpiRow, noTradeRow, miniNoTradeRow, otherAiRow])}
        ${renderAiDecisionBiasCard(module, biasWarningRow)}
        ${renderAiDecisionDonut(entryDirectionRows, "Mini chọn hướng vào lệnh", "", entryTotal ? String(entryTotal) : "0", "lệnh")}
        ${renderAiDecisionBarSvg(directionPercentRows, "Hướng vào lệnh · Tỷ lệ", "Tỷ lệ LONG/SHORT trên tổng quyết định", "ai-entry-percent")}
        ${renderAiDecisionBarSvg(winrateRows, "Tỷ lệ thắng", "Hiệu suất quyết định LONG/SHORT", "ai-winrate")}
        ${renderAiDecisionBarSvg(profitRows, "Hệ số lợi nhuận", "Hiệu suất sinh lời LONG/SHORT", "ai-profit")}
        ${renderAiDecisionBarSvg(confidenceRows, "Độ tin cậy của AI", "Mức tự tin trung bình theo LONG/SHORT", "ai-confidence")}
      </div>
      <div class="module-chart-legend compact module-ai-variable-list">${renderModuleVariableRows(module, aiDecisionLegendRows(rows), false)}</div>
    </section>
  `;
}

const BUNNY_MINIMIZE_ROW_CONFIG = [
  ["recoveryMode", "Recovery mode", "trạng thái", "Mode vận hành hiện tại của Bunny Minimize Losses.", "Mode đang siết hơn bình thường.", "Mode đang nới hoặc về normal."],
  ["recoveryBand", "Dải recovery", "trạng thái", "Dải recovery nội bộ đang dùng để tính mốc hard/soft/normal.", "Dải recovery đang siết hơn.", "Dải recovery đang nới hơn."],
  ["isRecoveryMode", "Recovery mode", "trạng thái", "Bật khi chuỗi thua toàn hệ thống chạm ngưỡng recovery.", "Recovery vừa bật hoặc đang tác động mạnh hơn.", "Recovery đã tắt hoặc giảm tác động."],
  ["isPaused", "Tạm dừng giao dịch", "trạng thái", "Bật khi chuỗi thua chạm ngưỡng pause, bot không mở lệnh mới.", "Bot đang bị pause, cần chú ý.", "Bot đã thoát pause hoặc ít bị chặn hơn."],
  ["globalLossStreak", "Chuỗi thua toàn hệ thống", "lần", "Số lệnh LOSS liên tiếp gần nhất trên toàn hệ thống.", "Chuỗi thua tăng, rủi ro hệ thống cao hơn.", "Chuỗi thua giảm hoặc đã reset."],
  ["globalLossStreakThreshold", "Ngưỡng bật recovery", "lần", "Số lệnh thua liên tiếp cần đạt để bật recovery mode.", "Ngưỡng recovery nới rộng hơn.", "Ngưỡng recovery siết chặt hơn."],
  ["pauseTradingLossStreak", "Ngưỡng pause", "lần", "Số lệnh thua liên tiếp cần đạt để tạm dừng giao dịch.", "Ngưỡng pause nới rộng hơn.", "Ngưỡng pause siết chặt hơn."],
  ["openPositionsCount", "Vị thế đang mở", "slot", "Số vị thế OPEN thực tế đang chiếm slot.", "Số slot đang dùng tăng.", "Số slot đang dùng giảm."],
  ["maxConcurrentPositions", "Slot tối đa", "slot", "Số vị thế tối đa được phép chạy song song.", "Sức chứa vị thế được nới rộng.", "Sức chứa vị thế bị thu hẹp."],
  ["slotUtilizationPercent", "Tỷ lệ dùng slot", "%", "Phần trăm slot đang được sử dụng.", "Bot đang dùng nhiều slot hơn.", "Bot đang dùng ít slot hơn."],
  ["pausedMinutesRemaining", "Thời gian pause còn lại", "phút", "Số phút còn lại trước khi bot tự thoát pause.", "Thời gian bị pause tăng.", "Thời gian pause còn lại giảm."],
  ["normalRiskPercent", "Rủi ro Normal", "%", "Tỷ lệ rủi ro áp dụng khi hệ thống chạy normal mode.", "Normal mode dùng rủi ro cao hơn.", "Normal mode dùng rủi ro thấp hơn."],
  ["softRecoveryRiskPercent", "Rủi ro Soft Recovery", "%", "Tỷ lệ rủi ro áp dụng khi hệ thống ở soft recovery.", "Soft recovery dùng rủi ro cao hơn.", "Soft recovery dùng rủi ro thấp hơn."],
  ["recoveryModeRiskPercent", "Rủi ro Recovery", "%", "Tỷ lệ rủi ro áp dụng khi hệ thống đang recovery mode.", "Recovery mode dùng rủi ro cao hơn.", "Recovery mode dùng rủi ro thấp hơn."],
  ["currentNormalMinRuleScore", "Rule score hiện hành", "điểm", "Ngưỡng rule score hiện hành sau adaptive threshold.", "Bot lọc setup chặt hơn theo rule score.", "Bot lọc setup thoáng hơn theo rule score."],
  ["currentNormalMinGptConfidence", "GPT confidence hiện hành", "điểm", "Ngưỡng GPT confidence hiện hành sau adaptive threshold.", "Bot yêu cầu AI tự tin hơn.", "Bot yêu cầu AI ít chặt hơn."],
  ["normalMinRiskReward", "RR tối thiểu Normal", "hệ số", "Risk reward tối thiểu khi chạy normal mode.", "Bot yêu cầu RR cao hơn.", "Bot chấp nhận RR thấp hơn."],
  ["softRecoveryMinRuleScore", "Rule score Soft", "điểm", "Ngưỡng rule score tối thiểu khi soft recovery.", "Soft recovery lọc chặt hơn.", "Soft recovery lọc thoáng hơn."],
  ["softRecoveryMinGptConfidence", "GPT confidence Soft", "điểm", "Ngưỡng GPT confidence tối thiểu khi soft recovery.", "Soft recovery yêu cầu AI tự tin hơn.", "Soft recovery yêu cầu AI ít chặt hơn."],
  ["softRecoveryMinRiskReward", "RR tối thiểu Soft", "hệ số", "Risk reward tối thiểu khi soft recovery.", "Soft recovery yêu cầu RR cao hơn.", "Soft recovery chấp nhận RR thấp hơn."],
  ["recoveryMinRuleScore", "Rule score Recovery", "điểm", "Ngưỡng rule score tối thiểu khi recovery mode.", "Recovery lọc chặt hơn theo rule score.", "Recovery lọc thoáng hơn theo rule score."],
  ["recoveryMinGptConfidence", "GPT confidence Recovery", "điểm", "Ngưỡng GPT confidence tối thiểu khi recovery mode.", "Recovery yêu cầu AI tự tin hơn.", "Recovery yêu cầu AI ít chặt hơn."],
  ["recoveryMinRiskReward", "RR tối thiểu Recovery", "hệ số", "Risk reward tối thiểu khi recovery mode.", "Recovery yêu cầu RR cao hơn.", "Recovery chấp nhận RR thấp hơn."],
  ["strongSetupRuleScore", "Rule score Strong", "điểm", "Ngưỡng rule score để gắn nhãn STRONG setup.", "Strong setup khó đạt hơn theo rule score.", "Strong setup dễ đạt hơn theo rule score."],
  ["strongSetupGptConfidence", "GPT confidence Strong", "điểm", "Ngưỡng GPT confidence để gắn nhãn STRONG setup.", "Strong setup khó đạt hơn theo confidence.", "Strong setup dễ đạt hơn theo confidence."],
  ["strongSetupMinRiskReward", "RR tối thiểu Strong", "hệ số", "Risk reward tối thiểu để gắn nhãn STRONG setup.", "Strong setup yêu cầu RR cao hơn.", "Strong setup chấp nhận RR thấp hơn."],
  ["enableAdaptiveThreshold", "Adaptive threshold", "trạng thái", "Bật nghĩa là ngưỡng Normal tự điều chỉnh theo số lệnh đóng 7 ngày.", "Adaptive threshold vừa bật hoặc tăng tác động.", "Adaptive threshold vừa tắt hoặc giảm tác động."],
  ["weeklyTargetMinTrades", "Mục tiêu lệnh tối thiểu", "lệnh", "Số lệnh đóng tối thiểu mục tiêu trong 7 ngày.", "Mục tiêu tối thiểu tăng.", "Mục tiêu tối thiểu giảm."],
  ["weeklyTargetMaxTrades", "Mục tiêu lệnh tối đa", "lệnh", "Số lệnh đóng tối đa mục tiêu trong 7 ngày.", "Mục tiêu tối đa tăng.", "Mục tiêu tối đa giảm."],
  ["adaptiveScoreStep", "Bước chỉnh rule score", "điểm", "Mức tăng/giảm rule score khi adaptive threshold điều chỉnh.", "Adaptive chỉnh rule score mạnh hơn.", "Adaptive chỉnh rule score nhẹ hơn."],
  ["adaptiveConfidenceStep", "Bước chỉnh confidence", "điểm", "Mức tăng/giảm GPT confidence khi adaptive threshold điều chỉnh.", "Adaptive chỉnh confidence mạnh hơn.", "Adaptive chỉnh confidence nhẹ hơn."],
  ["recoveryCyclePnlUsdt", "Cycle PnL", "u", "Cycle PnL dung de phan biet Soft Recovery va Normal.", "Cycle PnL tang.", "Cycle PnL giam."],
  ["recoveryTargetProfitUsdt", "Mốc Normal", "u", "Cycle PnL cần đạt để quay về Normal.", "Mốc Normal cao hơn.", "Mốc Normal thấp hơn."],
  ["hardRecoveryPeakLossUsdt", "Đáy Hard", "u", "Đáy âm sâu nhất kể từ khi vào Hard Recovery.", "Đáy Hard âm sâu hơn.", "Đáy Hard đã cải thiện."],
  ["hardRecoverySoftExitThresholdUsdt", "Mốc về Soft", "u", "Mốc hồi 50% từ đáy Hard để quay về Soft Recovery.", "Mốc về Soft cao hơn.", "Mốc về Soft thấp hơn."],
  ["needToSoftUsdt", "Cần về Soft", "u", "Số USDT cần gỡ thêm để từ Hard Recovery quay về Soft Recovery theo mốc soft exit hiện tại.", "Cần gỡ nhiều hơn.", "Cần gỡ ít hơn."],
  ["needToNormalUsdt", "Cần về Normal", "u", "Số USDT cần gỡ thêm để Cycle PnL đạt mục tiêu normal hiện tại.", "Cần gỡ nhiều hơn.", "Cần gỡ ít hơn."],
].map(([key, label, unit, meaning, up, down], index) => ({
  key,
  label,
  unit,
  meaning,
  trendUp: up,
  trendDown: down,
  order: index,
}));

const BUNNY_MINIMIZE_ROW_KEYS = new Set(BUNNY_MINIMIZE_ROW_CONFIG.map((item) => item.key));

function bunnyMinimizeRows(rows) {
  const byKey = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => byKey.set(String(row?.label || "").trim(), row));
  return BUNNY_MINIMIZE_ROW_CONFIG
    .map((config) => {
      const row = byKey.get(config.key);
      if (!row) return null;
      return {
        ...row,
        label: config.label,
        unit: config.unit,
        meaning: config.meaning,
        trendUp: config.trendUp,
        trendDown: config.trendDown,
        riskKey: config.key,
        riskOrder: config.order,
      };
    })
    .filter(Boolean);
}

function bunnyRiskRow(rows, key) {
  return (Array.isArray(rows) ? rows : []).find((row) => (row.riskKey || row.label) === key) || null;
}

function bunnyRiskValue(row) {
  const numeric = moduleNumericValue(row?.value);
  const key = String(row?.riskKey || row?.label || "");
  const unit = viLabel(row?.unit || "");
  if (key === "recoveryMode") {
    const mode = String(row?.value || "").toUpperCase();
    if (mode === "HARD_RECOVERY") return "Hard Recovery";
    if (mode === "SOFT_RECOVERY") return "Soft Recovery";
    return "Normal";
  }
  if (["isRecoveryMode", "isPaused", "enableAdaptiveThreshold"].includes(key)) {
    return numeric !== null && numeric >= 50 ? "Bật" : "Tắt";
  }
  if (numeric === null) return formatCardValue(row?.label || "", viText(row?.value ?? "-"));
  if (unit === "%") return `${formatAxisPercentLabel(numeric)}`;
  if (unit === "lan" || unit === "lenh" || unit === "slot" || unit === "phut") {
    return Math.round(numeric).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  if (unit === "he so") return formatAxisFactorLabel(numeric);
  return formatFixed2(numeric);
}

function formatBunnyCyclePnl(value) {
  const numeric = moduleSignedNumericValue(value);
  if (numeric === null) return "-";
  const fixed = numeric.toFixed(2);
  return `${Number(fixed) > 0 ? "+" : ""}${Number(fixed).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}u`;
}

function bunnyRiskChartRow(rows, key, chartIndex, colorIndex) {
  const row = bunnyRiskRow(rows, key);
  if (!row) return null;
  const rawValue = moduleNumericValue(row.value);
  if (rawValue === null) return null;
  return {
    ...row,
    rawNumericValue: rawValue,
    chartValue: rawValue > 0 ? rawValue : 0,
    chartIndex,
    color: MODULE_CHART_COLORS[colorIndex % MODULE_CHART_COLORS.length],
  };
}

function niceAutoNumberAxisMax(maxValue) {
  const raw = Math.max(0, Number(maxValue || 0));
  if (!Number.isFinite(raw) || raw <= 0) return 4;
  return Math.max(4, Math.ceil((raw * 1.2) / 4) * 4);
}

function formatRiskAxisLabel(value, unit) {
  const normalizedUnit = viLabel(unit || "");
  if (normalizedUnit === "%") return formatAxisPercentLabel(value);
  if (normalizedUnit === "he so") return formatAxisFactorLabel(value);
  if (normalizedUnit === "diem" || normalizedUnit === "lan" || normalizedUnit === "slot" || normalizedUnit === "lenh" || normalizedUnit === "phut") {
    return Number(value || 0).toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  return formatFixed2(value);
}

function riskYAxisMax(rows, unit) {
  const values = (Array.isArray(rows) ? rows : []).map((row) => Math.max(0, Number(row?.rawNumericValue || 0)));
  const maxValue = Math.max(...values, 0);
  const normalizedUnit = viLabel(unit || "");
  if (normalizedUnit === "%") return niceAutoPercentAxisMax(maxValue);
  if (normalizedUnit === "he so") return niceAutoFactorAxisMax(maxValue);
  if (normalizedUnit === "diem") return 100;
  return niceAutoNumberAxisMax(maxValue);
}

function renderBunnyRiskKpis(module, rows) {
  const cyclePnlRow = bunnyRiskRow(rows, "recoveryCyclePnlUsdt");
  const items = [
    bunnyRiskRow(rows, "recoveryMode"),
    bunnyRiskRow(rows, "isPaused"),
    bunnyRiskRow(rows, "globalLossStreak"),
    bunnyRiskRow(rows, "slotUtilizationPercent"),
    bunnyRiskRow(rows, "openPositionsCount"),
    bunnyRiskRow(rows, "maxConcurrentPositions"),
    bunnyRiskRow(rows, "needToSoftUsdt"),
    bunnyRiskRow(rows, "needToNormalUsdt"),
  ].filter(Boolean);
  if (!items.length) return "";
  return `
    <div class="module-chart-meta module-ai-kpi-row">
      ${items.map((row) => {
        const label = row.unit ? `${row.label} (${row.unit})` : row.label;
        const tone = bunnyRiskKpiTone(row);
        const rowKey = String(row?.riskKey || row?.label || "");
        const hideDelta = Number(module?.number || 0) === 2
          && ["recoveryMode", "isPaused", "needToSoftUsdt", "needToNormalUsdt"].includes(rowKey);
        const isRecoveryMode = rowKey === "recoveryMode";
        const cyclePnlValue = moduleSignedNumericValue(cyclePnlRow?.value);
        const valueHtml = isRecoveryMode && cyclePnlRow
          ? `<strong class="bunny-recovery-kpi-value"><span>${escapeHtml(bunnyRiskValue(row))}</span><em class="${cyclePnlValue < 0 ? "negative" : "positive"}">${escapeHtml(formatBunnyCyclePnl(cyclePnlRow.value))}</em></strong>`
          : `<strong>${escapeHtml(bunnyRiskValue(row))}</strong>`;
        return `
          <div class="module-total-anchor ${tone ? `state-${tone}` : ""}">
            ${hideDelta ? "" : `<span class="module-chart-delta flat">${escapeHtml(bunnyRiskValue(row))}</span>`}
            <span>${escapeHtml(label || "-")}</span>
            ${valueHtml}
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderBunnyRiskBarSvg(rows, title, subtitle, chartId, unit) {
  const chartRows = rows.filter(Boolean);
  if (!chartRows.length) {
    return `<div class="module-chart-empty">Chưa có dữ liệu ${escapeHtml(title)}.</div>`;
  }
  const yAxisMax = riskYAxisMax(chartRows, unit);
  const chartLeft = 62;
  const slotWidth = 150;
  const chartRight = chartLeft + Math.max(360, chartRows.length * slotWidth);
  const chartTop = 34;
  const chartBaseline = 214;
  const chartHeight = chartBaseline - chartTop;
  const barWidth = (chartRight - chartLeft - 10) / chartRows.length;
  const chartWidth = chartRight + 44;
  const chartHeightTotal = 310;
  const tickValues = [0, 1, 2, 3, 4].map((step) => yAxisMax * step / 4);
  const yTicks = tickValues.map((tickValue) => {
    const y = chartBaseline - (tickValue / yAxisMax) * chartHeight;
    const gridLine = Math.abs(tickValue) < 1e-9 ? "" : `<line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="module-bar-grid"></line>`;
    return `
      <g>
        ${gridLine}
        <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="module-axis-label">${escapeHtml(formatRiskAxisLabel(tickValue, unit))}</text>
      </g>
    `;
  }).join("");
  const bars = chartRows.map((row, index) => {
    const axisValue = Math.max(0, Number(row.rawNumericValue || 0));
    const scaledPercent = yAxisMax > 0 ? Math.max(0, Math.min(100, axisValue / yAxisMax * 100)) : 0;
    const height = axisValue > 0 ? Math.max(8, scaledPercent / 100 * chartHeight) : 0;
    const x = chartLeft + index * barWidth + barWidth * 0.18;
    const y = chartBaseline - height;
    const width = Math.max(24, barWidth * 0.64);
    return `
      <g>
        <rect class="module-chart-segment module-bar-segment" data-chart-index="${row.chartIndex}" data-risk-key="${escapeHtml(row.riskKey || "")}" x="${x}" y="${y}" width="${width}" height="${height}" rx="4" fill="${row.color}">
          <title>${escapeHtml(row.label || "-")}: ${escapeHtml(bunnyRiskValue(row))}</title>
        </rect>
        ${renderBunnyRiskAxisLabel(row, x + width / 2, 236)}
      </g>
    `;
  }).join("");
  const markerId = `module-risk-y-arrow-${chartId}`;
  return `
    <div>
      ${renderChartTitle(title, subtitle)}
      <div class="module-chart-wrap">
        <svg class="module-bar-chart module-risk-bar-chart" viewBox="0 0 ${chartWidth} ${chartHeightTotal}" role="img" aria-label="${escapeHtml(title)}">
          <defs>
            <marker id="${markerId}" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M 0 8 L 4 0 L 8 8 Z" class="module-axis-arrow-head"></path>
            </marker>
          </defs>
          ${yTicks}
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartRight}" y2="${chartBaseline}" class="module-bar-axis"></line>
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartLeft}" y2="${chartTop - 12}" class="module-bar-axis module-y-axis" marker-end="url(#${markerId})"></line>
          <text x="${chartLeft - 12}" y="${chartTop - 16}" text-anchor="middle" class="module-axis-percent">${escapeHtml(unit)}</text>
          ${bars}
          <g class="module-chart-callout" hidden>
            <line class="module-chart-callout-line" x1="0" y1="0" x2="0" y2="0"></line>
            <rect class="module-chart-callout-box" x="0" y="0" width="0" height="0" rx="6"></rect>
            <text class="module-chart-callout-text" x="0" y="0"></text>
          </g>
        </svg>
      </div>
    </div>
  `;
}

function bunnyRiskAxisLabelLines(row) {
  const key = String(row?.riskKey || "");
  const labels = {
    openPositionsCount: ["Vị thế", "đang mở", "(slot)"],
    maxConcurrentPositions: ["Slot", "tối đa", "(slot)"],
    slotUtilizationPercent: ["Tỷ lệ", "dùng slot", "(%)"],
    globalLossStreak: ["Chuỗi thua", "hệ thống", "(lần)"],
    globalLossStreakThreshold: ["Ngưỡng", "recovery", "(lần)"],
    pauseTradingLossStreak: ["Ngưỡng", "pause", "(lần)"],
    currentNormalMinRuleScore: ["Rule score", "hiện hành", "(điểm)"],
    currentNormalMinGptConfidence: ["GPT conf.", "hiện hành", "(điểm)"],
    recoveryMinRuleScore: ["Rule score", "Recovery", "(điểm)"],
    recoveryMinGptConfidence: ["GPT conf.", "Recovery", "(điểm)"],
    strongSetupRuleScore: ["Rule score", "Strong", "(điểm)"],
    strongSetupGptConfidence: ["GPT conf.", "Strong", "(điểm)"],
    normalMinRiskReward: ["RR tối thiểu", "Normal", "(hệ số)"],
    recoveryMinRiskReward: ["RR tối thiểu", "Recovery", "(hệ số)"],
    strongSetupMinRiskReward: ["RR tối thiểu", "Strong", "(hệ số)"],
    normalRiskPercent: ["Rủi ro", "Normal", "(%)"],
    recoveryModeRiskPercent: ["Rủi ro", "Recovery", "(%)"],
  };
  return labels[key] || chartAxisLabelLines(row).slice(0, 3);
}

function renderBunnyRiskAxisLabel(row, x, y) {
  const lines = bunnyRiskAxisLabelLines(row).slice(0, 3);
  return `
    <text x="${x}" y="${y}" text-anchor="middle" class="module-bar-index module-risk-axis-index">
      ${lines.map((line, index) => `<tspan x="${x}" dy="${index ? 15 : 0}">${escapeHtml(line)}</tspan>`).join("")}
    </text>
  `;
}

function bunnyRiskKpiTone(row) {
  const key = String(row?.riskKey || "");
  const rawValue = String(row?.value || "");
  const value = rawValue.toUpperCase();
  const normalizedValue = viLabel(rawValue).toLowerCase();
  if (key === "recoveryMode") {
    if (value.includes("HARD") || normalizedValue.includes("hard")) return "red";
    if (value.includes("SOFT") || normalizedValue.includes("soft") || normalizedValue.includes("recovery")) return "orange";
    return "green";
  }
  if (key === "isPaused") {
    if (value === "BẬT" || value === "BAT") return "red";
    return "green";
  }
  if (key === "globalLossStreak") {
    const numeric = moduleNumericValue(row?.value);
    if (numeric !== null && numeric >= 2) return "red";
    if (numeric !== null && numeric > 0) return "orange";
  }
  if (key === "slotUtilizationPercent") {
    const numeric = moduleNumericValue(row?.value);
    if (numeric !== null && numeric >= 90) return "red";
    if (numeric !== null && numeric >= 70) return "orange";
    if (numeric !== null) return "green";
  }
  return "";
}

const BUNNY_RISK_CHART_ORDER = [
  ["openPositionsCount", { chartIndex: 10, colorIndex: 1 }],
  ["maxConcurrentPositions", { chartIndex: 11, colorIndex: 2 }],
  ["slotUtilizationPercent", { chartIndex: 12, colorIndex: 3 }],
  ["globalLossStreakThreshold", { chartIndex: 21, colorIndex: 4 }],
  ["pauseTradingLossStreak", { chartIndex: 22, colorIndex: 6 }],
  ["currentNormalMinRuleScore", { chartIndex: 30, colorIndex: 0 }],
  ["currentNormalMinGptConfidence", { chartIndex: 31, colorIndex: 1 }],
  ["softRecoveryMinRuleScore", { chartIndex: 32, colorIndex: 4 }],
  ["softRecoveryMinGptConfidence", { chartIndex: 33, colorIndex: 5 }],
  ["recoveryMinRuleScore", { chartIndex: 34, colorIndex: 8 }],
  ["recoveryMinGptConfidence", { chartIndex: 35, colorIndex: 9 }],
  ["strongSetupRuleScore", { chartIndex: 36, colorIndex: 8 }],
  ["strongSetupGptConfidence", { chartIndex: 37, colorIndex: 9 }],
  ["normalMinRiskReward", { chartIndex: 40, colorIndex: 0 }],
  ["softRecoveryMinRiskReward", { chartIndex: 41, colorIndex: 4 }],
  ["recoveryMinRiskReward", { chartIndex: 42, colorIndex: 4 }],
  ["strongSetupMinRiskReward", { chartIndex: 43, colorIndex: 8 }],
  ["normalRiskPercent", { chartIndex: 50, colorIndex: 2 }],
  ["softRecoveryRiskPercent", { chartIndex: 51, colorIndex: 4 }],
  ["recoveryModeRiskPercent", { chartIndex: 52, colorIndex: 6 }],
];

function renderBunnyRiskVariableRows(module, rows) {
  const chartMeta = new Map(BUNNY_RISK_CHART_ORDER);
  const chartOrder = new Map(BUNNY_RISK_CHART_ORDER.map(([key], index) => [key, index]));
  const hiddenKeys = Number(module?.number || 0) === 2
    ? new Set(["recoveryMode", "isRecoveryMode", "isPaused", "globalLossStreak", "recoveryCyclePnlUsdt", "openPositionsCount", "maxConcurrentPositions", "slotUtilizationPercent", "needToSoftUsdt", "needToNormalUsdt"])
    : null;
  const chartRows = (Array.isArray(rows) ? rows : [])
    .filter((row) => !hiddenKeys || !hiddenKeys.has(String(row?.riskKey || "")))
    .map((row, index) => ({
      ...row,
      rawNumericValue: moduleNumericValue(row.value) ?? 0,
      chartValue: moduleNumericValue(row.value) || 0,
      chartIndex: chartMeta.get(String(row?.riskKey || ""))?.chartIndex ?? row.chartIndex ?? index,
      color: row.color || MODULE_CHART_COLORS[(chartMeta.get(String(row?.riskKey || ""))?.colorIndex ?? index) % MODULE_CHART_COLORS.length],
    }))
    .sort((left, right) => {
      const leftOrder = chartOrder.get(String(left?.riskKey || "")) ?? Number.MAX_SAFE_INTEGER;
      const rightOrder = chartOrder.get(String(right?.riskKey || "")) ?? Number.MAX_SAFE_INTEGER;
      return leftOrder - rightOrder || (left.riskOrder ?? 0) - (right.riskOrder ?? 0);
    });
  return chartRows.map((row) => {
    const delta = moduleDeltaInfo(module, row);
    const trend = moduleTrendMeaning(row);
    const label = row.unit ? `${row.label} (${row.unit})` : row.label;
    const deltaHtml = Number(module?.number || 0) === 2 ? "" : `<span class="module-chart-delta ${delta.state}">${escapeHtml(delta.text)}</span>`;
    return `
      <button class="module-chart-legend-item ${row.attention ? "attention" : ""}" type="button" data-chart-index="${row.chartIndex}" data-risk-key="${escapeHtml(row.riskKey || "")}" title="${escapeHtml(label)}: ${escapeHtml(bunnyRiskValue(row))}">
        <span class="module-chart-swatch" style="background:${row.color}"></span>
        <div>
          <strong>${escapeHtml(label || "-")}</strong>
          <small class="module-chart-value-line"><span>Giá trị hiện tại: ${escapeHtml(bunnyRiskValue(row))}</span>${deltaHtml}</small>
          <p>${escapeHtml(row.meaning || "Biến dùng để theo dõi trạng thái Bunny Minimize Losses.")}</p>
          <p><b>Tăng:</b> ${escapeHtml(trend.up)}</p>
          <p><b>Giảm:</b> ${escapeHtml(trend.down)}</p>
        </div>
      </button>
    `;
  }).join("");
}
function renderBunnyMinimizeModuleChart(module, rows) {
  const lossRows = [
    bunnyRiskChartRow(rows, "globalLossStreakThreshold", 21, 4),
    bunnyRiskChartRow(rows, "pauseTradingLossStreak", 22, 6),
  ];
  const thresholdRows = [
    bunnyRiskChartRow(rows, "currentNormalMinRuleScore", 30, 0),
    bunnyRiskChartRow(rows, "currentNormalMinGptConfidence", 31, 1),
    bunnyRiskChartRow(rows, "softRecoveryMinRuleScore", 32, 4),
    bunnyRiskChartRow(rows, "softRecoveryMinGptConfidence", 33, 5),
    bunnyRiskChartRow(rows, "recoveryMinRuleScore", 34, 8),
    bunnyRiskChartRow(rows, "recoveryMinGptConfidence", 35, 9),
    bunnyRiskChartRow(rows, "strongSetupRuleScore", 36, 8),
    bunnyRiskChartRow(rows, "strongSetupGptConfidence", 37, 9),
  ];
  const rrRows = [
    bunnyRiskChartRow(rows, "normalMinRiskReward", 40, 0),
    bunnyRiskChartRow(rows, "softRecoveryMinRiskReward", 41, 4),
    bunnyRiskChartRow(rows, "recoveryMinRiskReward", 42, 4),
    bunnyRiskChartRow(rows, "strongSetupMinRiskReward", 43, 8),
  ];
  const riskRows = [
    bunnyRiskChartRow(rows, "normalRiskPercent", 50, 2),
    bunnyRiskChartRow(rows, "softRecoveryRiskPercent", 51, 4),
    bunnyRiskChartRow(rows, "recoveryModeRiskPercent", 52, 6),
  ];
  return `
    <section class="module-chart-panel module-chart-panel-compact module-risk-panel">
      <div class="module-chart-legend module-ai-chart-stack module-risk-chart-stack">
        ${renderBunnyRiskKpis(module, rows)}
        ${renderBunnyRiskBarSvg(lossRows, "Chuỗi thua & ngưỡng bảo vệ", "Recovery và pause dựa trên loss streak toàn hệ thống", "loss-streak", "lần")}
        ${renderBunnyRiskBarSvg(thresholdRows, "Ngưỡng điểm lọc", "Normal, Soft Recovery và Hard Recovery", "threshold-score", "điểm")}
        ${renderBunnyRiskBarSvg(rrRows, "Ngưỡng Risk Reward", "RR tối thiểu theo từng chế độ lọc", "threshold-rr", "hệ số")}
        ${renderBunnyRiskBarSvg(riskRows, "Rủi ro theo chế độ", "Risk percent dùng cho Normal, Soft và Hard Recovery", "risk-percent", "%")}
      </div>
      <div class="module-chart-legend compact module-ai-variable-list module-risk-variable-list">${renderBunnyRiskVariableRows(module, rows)}</div>
    </section>
  `;
}

function renderModuleBarChart(module, rows) {
  const chartRows = moduleChartRows(rows);
  if (!chartRows.length) {
    return '<section class="module-chart-panel"><div class="module-chart-empty">Chưa có biến số đủ điều kiện để vẽ biểu đồ cột.</div></section>';
  }
  const maxRawValue = Math.max(...chartRows.map((row) => Math.max(0, Number(row.rawNumericValue || 0))), 1);
  const chartLeft = 62;
  const chartRight = 398;
  const chartTop = 34;
  const chartBaseline = 196;
  const chartHeight = chartBaseline - chartTop;
  const barWidth = (chartRight - chartLeft - 10) / chartRows.length;
  const yTicks = [25, 50, 75, 100].map((tickValue) => {
    const y = chartBaseline - (tickValue / 100) * chartHeight;
    return `
      <g>
        <line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="module-bar-grid"></line>
        <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="module-axis-label">${formatFixed2(tickValue)}%</text>
      </g>
    `;
  }).join("");
  const bars = chartRows.map((row, index) => {
    const percentValue = moduleBarPercentValue(row, maxRawValue);
    const height = Math.max(8, percentValue / 100 * chartHeight);
    const x = chartLeft + index * barWidth + barWidth * 0.18;
    const y = chartBaseline - height;
    const width = Math.max(14, barWidth * 0.64);
    const xLabel = moduleLegendCurrentValue(row);
    return `
      <g>
        <rect class="module-chart-segment module-bar-segment" data-chart-index="${index}" x="${x}" y="${y}" width="${width}" height="${height}" rx="4" fill="${row.color}">
          <title>${escapeHtml(row.label || "-")}: X=${escapeHtml(xLabel)}, Y=${formatFixed2(percentValue)}%</title>
        </rect>
        <text x="${x + width / 2}" y="216" text-anchor="middle" class="module-bar-index">${escapeHtml(xLabel)}</text>
      </g>
    `;
  }).join("");
  return `
    <section class="module-chart-panel module-chart-panel-compact">
      <div class="module-chart-wrap">
        <svg class="module-bar-chart" viewBox="0 0 430 244" role="img" aria-label="Biểu đồ cột module: trục Y là phần trăm, trục X là giá trị hiện tại">
          <defs>
            <marker id="module-y-arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M 0 8 L 4 0 L 8 8 Z" class="module-axis-arrow-head"></path>
            </marker>
          </defs>
          ${yTicks}
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartRight}" y2="${chartBaseline}" class="module-bar-axis"></line>
          <line x1="${chartLeft}" y1="${chartBaseline}" x2="${chartLeft}" y2="${chartTop - 12}" class="module-bar-axis module-y-axis" marker-end="url(#module-y-arrow)"></line>
          <text x="${chartLeft - 12}" y="${chartTop - 16}" text-anchor="middle" class="module-axis-percent">%</text>
          ${bars}
          <g class="module-chart-callout" hidden>
            <line class="module-chart-callout-line" x1="0" y1="0" x2="0" y2="0"></line>
            <rect class="module-chart-callout-box" x="0" y="0" width="0" height="0" rx="6"></rect>
            <text class="module-chart-callout-text" x="0" y="0"></text>
          </g>
        </svg>
      </div>
      <div class="module-chart-legend compact">${renderModuleVariableRows(module, chartRows, false)}</div>
    </section>
  `;
}

function renderModuleDonut(module, rows) {
  const chartRows = moduleChartRows(rows);
  if (!chartRows.length) {
    return '<section class="module-chart-panel"><div class="module-chart-empty">Chưa có biến số đủ điều kiện để vẽ biểu đồ tròn.</div></section>';
  }
  const total = chartRows.reduce((sum, row) => sum + row.chartValue, 0) || 1;
  let cursor = 0;
  const segments = chartRows.map((row, index) => {
    const angle = row.chartValue / total * 360;
    const startAngle = cursor;
    const endAngle = cursor + angle;
    const midAngle = startAngle + angle / 2;
    const path = donutSegmentPath(100, 100, 82, 45, startAngle, endAngle);
    cursor += angle;
    return `<path class="module-chart-segment module-donut-segment" data-chart-index="${index}" data-mid-angle="${midAngle}" d="${path}" fill="${row.color}"><title>${escapeHtml(row.label)}: ${escapeHtml(row.value ?? "-")}</title></path>`;
  }).join("");
  return `
    <section class="module-chart-panel module-chart-panel-compact">
      <div class="module-chart-wrap">
        <svg class="module-donut" viewBox="0 0 200 200" role="img" aria-label="Biểu đồ tròn biến module">
          ${segments}
          <circle cx="100" cy="100" r="39" fill="#fbfcfc"></circle>
          <text x="100" y="96" text-anchor="middle" class="module-donut-total">${chartRows.length}</text>
          <text x="100" y="115" text-anchor="middle" class="module-donut-caption">biến</text>
          <g class="module-chart-callout" hidden>
            <line class="module-chart-callout-line" x1="0" y1="0" x2="0" y2="0"></line>
            <rect class="module-chart-callout-box" x="0" y="0" width="0" height="0" rx="6"></rect>
            <text class="module-chart-callout-text" x="0" y="0"></text>
          </g>
        </svg>
      </div>
      <div class="module-chart-legend compact">${renderModuleVariableRows(module, chartRows, true)}</div>
    </section>
  `;
}

function renderModuleChart(module, rows) {
  if (Number(module?.number) === 1) return renderAiDecisionModuleChart(module, rows);
  if (Number(module?.number) === 2) return renderBunnyMinimizeModuleChart(module, rows);
  const barChartModules = new Set([1, 2, 3, 4, 5, 8]);
  return barChartModules.has(Number(module?.number)) ? renderModuleBarChart(module, rows) : renderModuleDonut(module, rows);
}

function bindModuleChartInteractions() {
  if (!refs.systemModuleDetail) return;
  const detail = refs.systemModuleDetail;
  const callout = detail.querySelector(".module-chart-callout");
  const calloutLine = detail.querySelector(".module-chart-callout-line");
  const calloutBox = detail.querySelector(".module-chart-callout-box");
  const calloutText = detail.querySelector(".module-chart-callout-text");
  const showCallout = (segment, label) => {
    const svg = segment.ownerSVGElement;
    if (!svg) return;
    const callout = svg.querySelector(".module-chart-callout");
    const calloutLine = svg.querySelector(".module-chart-callout-line");
    const calloutBox = svg.querySelector(".module-chart-callout-box");
    const calloutText = svg.querySelector(".module-chart-callout-text");
    if (!callout || !calloutLine || !calloutBox || !calloutText) return;
    const tag = segment.tagName.toLowerCase();
    let anchorX = 100;
    let anchorY = 100;
    if (tag === "rect") {
      const x = Number(segment.getAttribute("x") || 0);
      const y = Number(segment.getAttribute("y") || 0);
      const width = Number(segment.getAttribute("width") || 0);
      anchorX = x + width / 2;
      anchorY = y - 6;
    } else {
      const box = segment.getBBox();
      const midAngle = Number(segment.getAttribute("data-mid-angle") || 0);
      const anchor = polarPoint(100, 100, 88, midAngle);
      anchorX = anchor.x;
      anchorY = anchor.y;
      const isUpper = midAngle >= 315 || midAngle <= 45 || (midAngle >= 45 && midAngle < 180);
      const isRightHalf = midAngle >= 180 && midAngle < 360;
      const preferTop = midAngle < 180 ? true : !isRightHalf;
      const preferBottom = midAngle >= 180 ? true : isRightHalf;
      const calloutY = preferTop ? Math.max(8, anchorY - 44) : Math.min((svg.viewBox.baseVal.height || 200) - 30, anchorY + 16);
      const boxWidth = Math.max(74, String(label).length * 6.6);
      const targetX = Math.min((svg.viewBox.baseVal.width || 200) - boxWidth - 8, Math.max(8, anchorX - boxWidth / 2));
      const targetY = calloutY;
      callout.hidden = false;
      calloutLine.setAttribute("x1", String(anchorX));
      calloutLine.setAttribute("y1", String(anchorY));
      calloutLine.setAttribute("x2", String(targetX + boxWidth / 2));
      calloutLine.setAttribute("y2", String(preferTop ? targetY + 22 : targetY));
      calloutBox.setAttribute("x", String(targetX));
      calloutBox.setAttribute("y", String(targetY));
      calloutBox.setAttribute("width", String(boxWidth));
      calloutBox.setAttribute("height", String(22));
      calloutText.textContent = label;
      calloutText.setAttribute("x", String(targetX + 8));
      calloutText.setAttribute("y", String(targetY + 15));
      return;
    }
    const boxWidth = Math.max(74, String(label).length * 6.6);
    const boxHeight = 22;
    const viewWidth = svg.viewBox.baseVal.width || 200;
    const targetX = tag === "rect"
      ? Math.min(viewWidth - boxWidth - 8, Math.max(8, anchorX + 10))
      : Math.min(viewWidth - boxWidth - 8, Math.max(8, anchorX - boxWidth / 2));
    const targetY = Math.max(8, tag === "rect" ? anchorY - 28 : anchorY - 18);
    callout.hidden = false;
    calloutLine.setAttribute("x1", String(anchorX));
    calloutLine.setAttribute("y1", String(anchorY));
    calloutLine.setAttribute("x2", String(targetX));
    calloutLine.setAttribute("y2", String(targetY + boxHeight / 2));
    calloutBox.setAttribute("x", String(targetX));
    calloutBox.setAttribute("y", String(targetY));
    calloutBox.setAttribute("width", String(boxWidth));
    calloutBox.setAttribute("height", String(boxHeight));
    calloutText.textContent = label;
    calloutText.setAttribute("x", String(targetX + 8));
    calloutText.setAttribute("y", String(targetY + 15));
  };
  const showTotalCallout = (item) => {
    const target = item.getAttribute("data-chart-total-target");
    if (!target) return false;
    const svg = detail.querySelector(`.module-donut[data-chart-total-target="${target}"]`);
    if (!svg) return false;
    const callout = svg.querySelector(".module-chart-callout");
    const calloutLine = svg.querySelector(".module-chart-callout-line");
    const calloutBox = svg.querySelector(".module-chart-callout-box");
    const calloutText = svg.querySelector(".module-chart-callout-text");
    if (!callout || !calloutLine || !calloutBox || !calloutText) return false;
    const label = item.querySelector("strong")?.textContent || item.getAttribute("title") || "Total";
    const viewWidth = svg.viewBox.baseVal.width || 200;
    const boxWidth = Math.min(viewWidth - 16, Math.max(102, String(label).length * 6.8 + 16));
    const targetX = Math.max(8, (viewWidth - boxWidth) / 2);
    const targetY = 10;
    detail.querySelectorAll(`.module-chart-segment`).forEach((segment) => {
      if (segment.ownerSVGElement === svg) segment.classList.add("active");
    });
    detail.querySelectorAll(`.module-chart-meta[data-chart-total-target="${target}"]`).forEach((node) => {
      node.classList.add("active");
    });
    callout.hidden = false;
    calloutLine.setAttribute("x1", "100");
    calloutLine.setAttribute("y1", "58");
    calloutLine.setAttribute("x2", String(targetX + boxWidth / 2));
    calloutLine.setAttribute("y2", String(targetY + 22));
    calloutBox.setAttribute("x", String(targetX));
    calloutBox.setAttribute("y", String(targetY));
    calloutBox.setAttribute("width", String(boxWidth));
    calloutBox.setAttribute("height", "22");
    calloutText.textContent = label;
    calloutText.setAttribute("x", String(targetX + 8));
    calloutText.setAttribute("y", String(targetY + 15));
    return true;
  };
  const clearActive = () => {
    detail.querySelectorAll(".module-chart-segment.active, .module-chart-legend-item.active, .module-chart-meta.active").forEach((node) => {
      node.classList.remove("active");
    });
    detail.querySelectorAll(".module-chart-callout").forEach((node) => {
      node.hidden = true;
    });
  };
  detail.querySelectorAll(".module-chart-legend-item").forEach((item) => {
    item.addEventListener("click", () => {
      const index = item.getAttribute("data-chart-index");
      const riskKey = item.getAttribute("data-risk-key");
      if (index === null) return;
      const alreadyActive = item.classList.contains("active");
      clearActive();
      if (alreadyActive) return;
      item.classList.add("active");
      if (showTotalCallout(item)) return;
      const segmentSelector = riskKey
        ? `.module-chart-segment[data-risk-key="${CSS.escape(riskKey)}"]`
        : `.module-chart-segment[data-chart-index="${CSS.escape(index)}"]`;
      detail.querySelectorAll(segmentSelector).forEach((segment) => {
        segment.classList.add("active");
        const label = item.getAttribute("title") || "";
        showCallout(segment, label);
      });
    });
  });
  detail.querySelectorAll(".module-chart-segment").forEach((segment) => {
    segment.addEventListener("click", () => {
      const index = segment.getAttribute("data-chart-index");
      const riskKey = segment.getAttribute("data-risk-key");
      const item = riskKey
        ? detail.querySelector(`.module-chart-legend-item[data-risk-key="${CSS.escape(riskKey)}"]`)
        : detail.querySelector(`.module-chart-legend-item[data-chart-index="${CSS.escape(index || "")}"]`);
      if (item) item.click();
    });
  });
}

const MARKET_REGIME_LABELS = {
  BULL: "Thị trường tăng",
  BEAR: "Thị trường giảm",
  SIDEWAY: "Thị trường đi ngang",
  HIGH_VOLATILITY: "Biến động cao",
  LOW_VOLATILITY: "Biến động thấp",
  UNKNOWN: "Chưa xác định",
};

function isMarketRegimeModule(module) {
  return Number(module?.number || 0) === 5 && viLabel(module?.name || "").includes("market regime");
}

function isMarketPatternEngineModule(module) {
  const name = viLabel(module?.name || "");
  return Number(module?.number || 0) === 14 || name.includes("market structure") || name.includes("pattern engine");
}

function isTradeExecutionModule(module) {
  const name = viLabel(module?.name || "");
  return Number(module?.number || 0) === 15 || name.includes("trade execution") || name.includes("position management");
}

function isPositionManagementSection(module) {
  return Boolean(module?.position_management_section);
}

function isRealtimeSystemModule(module) {
  if (!module) return false;
  const moduleNumber = Number(module.number || 0);
  if (moduleNumber === 2 || moduleNumber === 5 || moduleNumber === 8) return true;
  return isPositionManagementSection(module) || isTradeExecutionModule(module);
}

function positionManagementDetailTabGroup(section) {
  if (section === "overview") return ["overview", "pending_orders"];
  if (section === "profit_protection") return ["profit_protection", "sync_journal"];
  return [];
}

function positionManagementActiveDetailTab(section) {
  const tabs = positionManagementDetailTabGroup(section);
  if (!tabs.length) return null;
  return tabs.includes(state.selectedPositionManagementDetailTab)
    ? state.selectedPositionManagementDetailTab
    : tabs[0];
}

function renderPositionManagementDetailTabButton(key, label, activeKey) {
  const active = key === activeKey;
  return `<button class="trade-execution-tab ${active ? "active" : ""}" type="button" role="tab" aria-selected="${active ? "true" : "false"}" data-position-detail-tab="${escapeHtml(key)}">${escapeHtml(label)}</button>`;
}

function marketRegimeModuleName(module) {
  if (isPositionManagementSection(module)) return String(module.display_name || module.name || "-");
  return isMarketRegimeModule(module) ? "Market Regime Detector" : String(module?.name || "-");
}

function marketRegimeModuleSubtitle(module) {
  if (isPositionManagementSection(module)) {
    if (module.position_management_section === "profit_protection") {
      return `${String(module.display_subtitle || "")}\nNhật ký`;
    }
    if (module.position_management_section === "overview") {
      return `V\u1ecb th\u1ebf \u0111ang m\u1edf, TP/SL v\u00e0 l\u1ec7nh \u0111\u00f3ng g\u1ea7n \u0111\u00e2y\nOrder`;
    }
    return String(module.display_subtitle || "");
  }
  return isMarketRegimeModule(module) ? "Bộ nhận diện trạng thái thị trường" : "";
}

function marketRegimeFiniteValue(value) {
  const number = Number(value);
  return value !== null && value !== undefined && value !== "" && Number.isFinite(number) ? number : null;
}

function formatMarketRegimeNumber(value, maximumFractionDigits = 4) {
  const number = marketRegimeFiniteValue(value);
  if (number === null) return "-";
  const absNumber = Math.abs(number);
  let digits = maximumFractionDigits;
  if (maximumFractionDigits === 4 && absNumber > 0 && absNumber < 1) {
    const leadingZeros = Math.max(0, Math.ceil(-Math.log10(absNumber)) - 1);
    digits = Math.max(maximumFractionDigits, Math.min(12, leadingZeros + 5));
  }
  return number.toLocaleString("en-US", {
    maximumFractionDigits: digits,
  });
}

function marketRegimeStat(module, label) {
  const key = viLabel(label);
  return (Array.isArray(module?.stats) ? module.stats : []).find((row) => viLabel(row?.label || "") === key) || null;
}

function marketRegimeLabel(value) {
  const key = String(value || "UNKNOWN").trim().toUpperCase();
  return MARKET_REGIME_LABELS[key] || MARKET_REGIME_LABELS.UNKNOWN;
}

function marketRegimeTone(value) {
  const key = String(value || "UNKNOWN").trim().toUpperCase();
  if (key === "BULL") return "bull";
  if (key === "BEAR") return "bear";
  if (key === "SIDEWAY") return "sideway";
  if (key === "HIGH_VOLATILITY") return "high-volatility";
  if (key === "LOW_VOLATILITY") return "low-volatility";
  return "unknown";
}

function marketRegimeReasonRows(reason) {
  if (Array.isArray(reason)) {
    return reason.filter((item) => item !== null && item !== undefined && String(item).trim());
  }
  if (reason !== null && reason !== undefined && String(reason).trim()) return [reason];
  return [];
}

function renderMarketRegimeKpiCard({ title, subtitle, value, unitText, createdAt, stateText = "" }) {
  const numericValue = marketRegimeFiniteValue(value);
  return `
    <article class="market-regime-kpi ${numericValue === null ? "empty" : ""}">
      <div class="market-regime-kpi-head">
        <div>
          <strong>${escapeHtml(title)}</strong>
          <small>${escapeHtml(subtitle)}</small>
        </div>
        ${stateText ? `<span class="market-regime-kpi-state">${escapeHtml(stateText)}</span>` : ""}
      </div>
      <div class="market-regime-kpi-value">${numericValue === null ? "Chưa có dữ liệu" : escapeHtml(formatMarketRegimeNumber(numericValue))}</div>
      <div class="market-regime-kpi-meta">
        <span>${escapeHtml(unitText || "Chưa có thông tin đơn vị")}</span>
        <time>${escapeHtml(timeLabel(createdAt))}</time>
      </div>
    </article>
  `;
}

function renderMarketRegimeIndicatorStrip(indicators) {
  const rows = [
    ["last", "Giá hiện tại"],
    ["bid", "Giá mua"],
    ["ask", "Giá bán"],
    ["ema200", "EMA200"],
    ["vwap", "VWAP"],
    ["spread_pct", "Spread (%)"],
    ["volume_ratio", "Tỷ lệ khối lượng"],
    ["support", "Hỗ trợ"],
    ["resistance", "Kháng cự"],
  ].map(([key, label]) => ({ key, label, value: marketRegimeFiniteValue(indicators?.[key]) }))
    .filter((row) => row.value !== null);
  if (!rows.length) {
    return '<div class="market-regime-empty compact">Chưa có chỉ báo bổ sung trong snapshot hiện tại.</div>';
  }
  return rows.map((row) => `
    <div class="market-regime-indicator">
      <span>${escapeHtml(row.label)}</span>
      <strong>${escapeHtml(formatMarketRegimeNumber(row.value))}</strong>
    </div>
  `).join("");
}

function marketRegimeChartAxis(value) {
  const number = marketRegimeFiniteValue(value);
  if (number === null) return "-";
  const clean = Math.abs(number) < 1e-10 ? 0 : number;
  return clean.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function renderMarketRegimeLineChart({
  id,
  title,
  subtitle,
  createdAt,
  series,
  axisLabel = "Giá trị gốc",
  missing = [],
  supplemental = [],
  error = "",
}) {
  const availableSeries = (Array.isArray(series) ? series : [])
    .map((item) => ({ ...item, value: marketRegimeFiniteValue(item.value) }))
    .filter((item) => item.value !== null);
  const missingText = (Array.isArray(missing) ? missing : []).filter(Boolean).join(", ");
  const note = missingText ? `<p class="market-regime-chart-note">Chưa có dữ liệu: ${escapeHtml(missingText)}.</p>` : "";
  const extraRows = (Array.isArray(supplemental) ? supplemental : [])
    .map((item) => ({ ...item, value: marketRegimeFiniteValue(item.value) }))
    .filter((item) => item.value !== null);
  const supplementalMarkup = extraRows.length ? `
    <div class="market-regime-chart-supplemental">
      ${extraRows.map((item) => `
        <div><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(formatMarketRegimeNumber(item.value))}</strong></div>
      `).join("")}
      <p>${escapeHtml(extraRows[0].note || "Hiển thị riêng vì payload không xác nhận cùng đơn vị với series của biểu đồ.")}</p>
    </div>
  ` : "";
  if (error || !availableSeries.length) {
    return `
      <article class="market-regime-chart-card" data-regime-chart="${escapeHtml(id)}">
        <header>
          <div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></div>
        </header>
        <div class="market-regime-empty">${escapeHtml(error || "Chưa có dữ liệu phù hợp để vẽ biểu đồ này.")}</div>
        ${supplementalMarkup}
        ${note}
      </article>
    `;
  }

  const values = availableSeries.map((item) => item.value);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const rawSpan = rawMax - rawMin;
  const padding = rawSpan > 0 ? rawSpan * 0.16 : Math.max(Math.abs(rawMax) * 0.1, 1);
  const axisMin = rawMin - padding;
  const axisMax = rawMax + padding;
  const axisSpan = axisMax - axisMin || 1;
  const chartLeft = 62;
  const chartRight = 496;
  const chartTop = 26;
  const chartBottom = 184;
  const chartHeight = chartBottom - chartTop;
  const pointX = (chartLeft + chartRight) / 2;
  const pointY = (value) => chartBottom - ((value - axisMin) / axisSpan) * chartHeight;
  const ticks = Array.from({ length: 5 }, (_, index) => axisMin + (axisSpan * index) / 4);
  const chartSeries = availableSeries.map((item) => {
    const y = pointY(item.value);
    const target = `${id}-${item.key}`;
    return `
      <g class="market-regime-series" data-regime-series="${escapeHtml(target)}">
        <path d="M ${pointX} ${y}" fill="none" stroke="${item.color}" stroke-width="2.5"></path>
        <circle cx="${pointX}" cy="${y}" r="5.5" fill="${item.color}" stroke="var(--panel)" stroke-width="2">
          <title>${escapeHtml(timeLabel(createdAt))} | ${escapeHtml(item.label)}: ${escapeHtml(formatMarketRegimeNumber(item.value))}</title>
        </circle>
      </g>
    `;
  }).join("");
  const legend = availableSeries.map((item) => {
    const target = `${id}-${item.key}`;
    return `
      <button class="market-regime-legend-btn" type="button" data-regime-target="${escapeHtml(target)}" aria-pressed="true">
        <i style="background:${item.color}"></i>
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(formatMarketRegimeNumber(item.value))}</strong>
      </button>
    `;
  }).join("");
  return `
    <article class="market-regime-chart-card" data-regime-chart="${escapeHtml(id)}">
      <header>
        <div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></div>
        <span>${escapeHtml(axisLabel)}</span>
      </header>
      <div class="market-regime-chart-canvas">
        <svg viewBox="0 0 540 224" role="img" aria-label="${escapeHtml(title)}">
          ${ticks.map((value, index) => {
            const y = chartBottom - (index / 4) * chartHeight;
            return `
              <g>
                <line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="market-regime-grid-line"></line>
                <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="market-regime-axis-text">${escapeHtml(marketRegimeChartAxis(value))}</text>
              </g>
            `;
          }).join("")}
          <line x1="${chartLeft}" y1="${chartBottom}" x2="${chartRight}" y2="${chartBottom}" class="market-regime-axis-line"></line>
          ${chartSeries}
          <text x="${pointX}" y="210" text-anchor="middle" class="market-regime-axis-text">${escapeHtml(timeOnlyLabel(createdAt))}</text>
        </svg>
      </div>
      <div class="market-regime-chart-legend">${legend}</div>
      ${supplementalMarkup}
      ${note}
    </article>
  `;
}

function marketRegimeSnapshotCreatedAt(snapshot) {
  return snapshot?.created_at || snapshot?.createdAt || null;
}

function marketRegimeSameSnapshot(left, right) {
  const leftCreatedAt = marketRegimeSnapshotCreatedAt(left);
  const rightCreatedAt = marketRegimeSnapshotCreatedAt(right);
  if (leftCreatedAt === rightCreatedAt) return true;
  const leftTime = Date.parse(leftCreatedAt);
  const rightTime = Date.parse(rightCreatedAt);
  return Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime === rightTime;
}

function marketRegimeChartSnapshots(currentSnapshot, history) {
  const rows = (Array.isArray(history) ? history : [])
    .filter((snapshot) => snapshot && typeof snapshot === "object" && marketRegimeSnapshotCreatedAt(snapshot));
  const currentCreatedAt = marketRegimeSnapshotCreatedAt(currentSnapshot);
  if (!rows.length) return currentCreatedAt ? [currentSnapshot] : [];
  if (currentCreatedAt && !rows.some((snapshot) => marketRegimeSameSnapshot(snapshot, currentSnapshot))) {
    rows.push(currentSnapshot);
  }
  return rows.sort((left, right) => {
    const leftCreatedAt = marketRegimeSnapshotCreatedAt(left);
    const rightCreatedAt = marketRegimeSnapshotCreatedAt(right);
    const leftTime = Date.parse(leftCreatedAt);
    const rightTime = Date.parse(rightCreatedAt);
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime)) return leftTime - rightTime;
    return String(leftCreatedAt).localeCompare(String(rightCreatedAt));
  });
}

const MARKET_REGIME_VIEW_MARKET = "MARKET";
const MARKET_REGIME_DEFAULT_TOP_SYMBOLS = ["BTC/USDT:USDT", "XAU/USDT:USDT", "ETH/USDT:USDT"];

function marketRegimeSnapshotScope(snapshot) {
  const indicators = snapshot?.indicators && typeof snapshot.indicators === "object" ? snapshot.indicators : {};
  return String(snapshot?.scope || indicators.scope || "").trim().toLowerCase();
}

function marketRegimeSnapshotSymbol(snapshot) {
  const indicators = snapshot?.indicators && typeof snapshot.indicators === "object" ? snapshot.indicators : {};
  return String(snapshot?.symbol || indicators.symbol || "").trim();
}

function marketRegimeSymbolLabel(symbol) {
  if (symbol === MARKET_REGIME_VIEW_MARKET) return "Market";
  if (String(symbol || "").startsWith("XAU/USDT")) return "XAU/USDT";
  return String(symbol || "").split("/", 1)[0] || String(symbol || "-");
}

function marketRegimeCompactSymbolList(symbols, limit = 10) {
  const rows = (Array.isArray(symbols) ? symbols : [])
    .map(marketRegimeSymbolLabel)
    .filter(Boolean);
  if (!rows.length) return "-";
  const visible = rows.slice(0, limit).join(", ");
  const hiddenCount = rows.length - limit;
  return hiddenCount > 0 ? `${visible} +${hiddenCount}` : visible;
}

function marketRegimeNormalizeHistoryPayload(module, regime) {
  const raw = module?.market_regime_history
    || regime?.history
    || state.lastSystemChecklistPayload?.market_regime_history
    || [];
  if (Array.isArray(raw)) {
    return {
      items: raw,
      aggregate: { label: "Thị trường", items: [], count: 0, latest_created_at: null },
      top_symbols: MARKET_REGIME_DEFAULT_TOP_SYMBOLS,
      detail_symbols: MARKET_REGIME_DEFAULT_TOP_SYMBOLS,
      aggregate_limit: 40,
      market_symbols: [],
      by_symbol: {},
      coverage: {},
    };
  }
  if (!raw || typeof raw !== "object") {
    return {
      items: [],
      aggregate: { label: "Thị trường", items: [], count: 0, latest_created_at: null },
      top_symbols: MARKET_REGIME_DEFAULT_TOP_SYMBOLS,
      detail_symbols: MARKET_REGIME_DEFAULT_TOP_SYMBOLS,
      aggregate_limit: 40,
      market_symbols: [],
      by_symbol: {},
      coverage: {},
    };
  }
  return {
    items: Array.isArray(raw.items) ? raw.items : [],
    aggregate: raw.aggregate && typeof raw.aggregate === "object" ? raw.aggregate : { label: "Thị trường", items: [], count: 0, latest_created_at: null },
    top_symbols: Array.isArray(raw.top_symbols) && raw.top_symbols.length ? raw.top_symbols : MARKET_REGIME_DEFAULT_TOP_SYMBOLS,
    detail_symbols: Array.isArray(raw.detail_symbols) && raw.detail_symbols.length ? raw.detail_symbols : (Array.isArray(raw.top_symbols) && raw.top_symbols.length ? raw.top_symbols : MARKET_REGIME_DEFAULT_TOP_SYMBOLS),
    aggregate_limit: marketRegimeFiniteValue(raw.aggregate_limit) || 40,
    market_symbols: Array.isArray(raw.market_symbols) ? raw.market_symbols : [],
    by_symbol: raw.by_symbol && typeof raw.by_symbol === "object" ? raw.by_symbol : {},
    coverage: raw.coverage && typeof raw.coverage === "object" ? raw.coverage : {},
  };
}

function marketRegimeHistoryItemsForView(payload, viewKey) {
  if (viewKey === MARKET_REGIME_VIEW_MARKET) {
    const aggregateItems = Array.isArray(payload?.aggregate?.items) ? payload.aggregate.items : [];
    return aggregateItems.length ? aggregateItems : [];
  }
  const bucket = payload?.by_symbol?.[viewKey];
  if (Array.isArray(bucket)) return bucket;
  if (bucket && typeof bucket === "object" && Array.isArray(bucket.items)) return bucket.items;
  return [];
}

function marketRegimeSnapshotForView(regime, payload, viewKey) {
  if (viewKey === MARKET_REGIME_VIEW_MARKET) {
    if (marketRegimeSnapshotScope(regime) === "aggregate" || marketRegimeSnapshotSymbol(regime) === MARKET_REGIME_VIEW_MARKET) return regime;
    const aggregateItems = marketRegimeHistoryItemsForView(payload, viewKey);
    return aggregateItems[0] || null;
  }
  if (marketRegimeSnapshotSymbol(regime) === viewKey) return regime;
  const symbolItems = marketRegimeHistoryItemsForView(payload, viewKey);
  return symbolItems[0] || null;
}

function marketRegimeViewOptions(payload) {
  const detailSymbols = Array.isArray(payload?.detail_symbols) && payload.detail_symbols.length
    ? payload.detail_symbols
    : Array.isArray(payload?.top_symbols) && payload.top_symbols.length
      ? payload.top_symbols
    : MARKET_REGIME_DEFAULT_TOP_SYMBOLS;
  const aggregateItems = marketRegimeHistoryItemsForView(payload, MARKET_REGIME_VIEW_MARKET);
  const coverage = payload?.coverage && typeof payload.coverage === "object" ? payload.coverage : {};
  return [
    {
      key: MARKET_REGIME_VIEW_MARKET,
      label: "Market",
      subtitle: coverage.coverage_count !== undefined && coverage.target_count
        ? `${coverage.coverage_count}/${coverage.target_count} cặp`
        : `${aggregateItems.length} snapshot`,
      count: aggregateItems.length,
    },
    ...detailSymbols.map((symbol) => {
      const items = marketRegimeHistoryItemsForView(payload, symbol);
      return {
        key: symbol,
        label: marketRegimeSymbolLabel(symbol),
        subtitle: `${items.length} snapshot`,
        count: items.length,
      };
    }),
  ];
}

function marketRegimeSelectedView(payload) {
  const options = marketRegimeViewOptions(payload);
  if (state.selectedMarketRegimeView && options.some((item) => item.key === state.selectedMarketRegimeView)) return state.selectedMarketRegimeView;
  const marketOption = options.find((item) => item.key === MARKET_REGIME_VIEW_MARKET && item.count > 0);
  if (marketOption) return marketOption.key;
  const firstWithData = options.find((item) => item.count > 0);
  return firstWithData?.key || MARKET_REGIME_VIEW_MARKET;
}

function renderMarketRegimeViewSelector(options, selectedView) {
  return `
    <div class="market-regime-view-selector" role="tablist" aria-label="Chọn phạm vi Market Regime">
      ${options.map((option) => `
        <button
          class="market-regime-view-btn ${option.key === selectedView ? "active" : ""}"
          type="button"
          role="tab"
          aria-selected="${option.key === selectedView ? "true" : "false"}"
          data-market-regime-view="${escapeHtml(option.key)}"
        >
          <strong>${escapeHtml(option.label)}</strong>
          <span>${escapeHtml(option.subtitle)}</span>
        </button>
      `).join("")}
    </div>
  `;
}

function renderMarketRegimeCoverage(payload, selectedView) {
  const coverage = payload?.coverage && typeof payload.coverage === "object" ? payload.coverage : {};
  const covered = Array.isArray(coverage.covered_symbols) ? coverage.covered_symbols : [];
  const missing = Array.isArray(coverage.missing_symbols) ? coverage.missing_symbols : [];
  const targetCount = marketRegimeFiniteValue(coverage.target_count) || marketRegimeFiniteValue(payload?.aggregate_limit);
  const coverageCount = marketRegimeFiniteValue(coverage.coverage_count);
  const coverageText = coverageCount !== null && targetCount !== null
    ? `${formatMarketRegimeNumber(coverageCount, 0)}/${formatMarketRegimeNumber(targetCount, 0)}`
    : "-";
  const scopeLabel = targetCount ? `top ${formatMarketRegimeNumber(targetCount, 0)}` : "top volume";
  return `
    <div class="market-regime-coverage-card">
      <div>
        <span>Phạm vi đang xem</span>
        <strong>${escapeHtml(selectedView === MARKET_REGIME_VIEW_MARKET ? `Tổng hợp thị trường ${scopeLabel}` : marketRegimeSymbolLabel(selectedView))}</strong>
      </div>
      <div>
        <span>Độ phủ ${escapeHtml(scopeLabel)}</span>
        <strong>${escapeHtml(coverageText)}</strong>
      </div>
      <p>
        Có dữ liệu: ${escapeHtml(marketRegimeCompactSymbolList(covered))}
        ${missing.length ? ` · Chưa có: ${escapeHtml(marketRegimeCompactSymbolList(missing, 6))}` : ""}
      </p>
    </div>
  `;
}

function marketRegimeIndicatorValue(indicators, keys) {
  const source = indicators && typeof indicators === "object" ? indicators : {};
  const candidateKeys = Array.isArray(keys) ? keys : [keys];
  for (const key of candidateKeys) {
    const value = marketRegimeFiniteValue(source?.[key]);
    if (value !== null) return value;
  }
  return null;
}

function renderMarketRegimeSnapshotChart({
  id,
  title,
  subtitle,
  snapshots,
  series,
  axisLabel = "Giá trị gốc",
  fixedYMin = null,
  fixedYMax = null,
  error = "",
}) {
  const rows = Array.isArray(snapshots) ? snapshots : [];
  const latestSnapshot = rows.at(-1) || null;
  const latestIndicators = latestSnapshot?.indicators && typeof latestSnapshot.indicators === "object"
    ? latestSnapshot.indicators
    : {};
  const normalizedSeries = (Array.isArray(series) ? series : []).map((item) => ({
    ...item,
    currentValue: marketRegimeIndicatorValue(latestIndicators, item.keys || item.key),
    points: rows.map((snapshot, index) => ({
      index,
      createdAt: marketRegimeSnapshotCreatedAt(snapshot),
      value: marketRegimeIndicatorValue(snapshot?.indicators, item.keys || item.key),
    })),
  })).map((item) => {
    const latestPointValue = [...item.points].reverse().find((point) => point.value !== null)?.value ?? null;
    return {
      ...item,
      hasData: item.points.some((point) => point.value !== null),
      displayValue: item.currentValue !== null ? item.currentValue : latestPointValue,
    };
  });
  const availableSeries = normalizedSeries.filter((item) => item.hasData);
  const missingText = normalizedSeries
    .filter((item) => !item.hasData)
    .map((item) => item.label)
    .join(", ");
  const missingNote = missingText
    ? `<p class="market-regime-chart-note">Chưa có dữ liệu: ${escapeHtml(missingText)}.</p>`
    : "";
  if (error || !rows.length || !availableSeries.length) {
    return `
      <article class="market-regime-chart-card market-regime-snapshot-chart" data-regime-chart="${escapeHtml(id)}">
        <header>
          <div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></div>
        </header>
        <div class="market-regime-empty">${escapeHtml(error || "Chưa có dữ liệu phù hợp để vẽ biểu đồ này.")}</div>
        ${missingNote}
      </article>
    `;
  }

  const values = availableSeries.flatMap((item) => item.points
    .filter((point) => point.value !== null)
    .map((point) => point.value));
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const rawSpan = rawMax - rawMin;
  const valueReference = Math.max(Math.abs(rawMin), Math.abs(rawMax), Number.EPSILON);
  const padding = rows.length === 1
    ? Math.max(rawSpan * 0.2, valueReference * 0.04, Number.EPSILON)
    : rawSpan > 0
      ? rawSpan * 0.16
      : Math.max(valueReference * 0.1, Number.EPSILON);
  const fixedMin = marketRegimeFiniteValue(fixedYMin);
  const fixedMax = marketRegimeFiniteValue(fixedYMax);
  const axisMin = fixedMin !== null ? fixedMin : rawMin - padding;
  const axisMax = fixedMax !== null ? fixedMax : rawMax + padding;
  const axisSpan = axisMax - axisMin || 1;
  const chartLeft = 62;
  const chartRight = 496;
  const chartTop = 26;
  const chartBottom = 184;
  const chartWidth = chartRight - chartLeft;
  const chartHeight = chartBottom - chartTop;
  const pointX = (index) => rows.length === 1
    ? chartLeft + chartWidth / 2
    : chartLeft + (index / (rows.length - 1)) * chartWidth;
  const pointY = (value) => chartBottom - ((value - axisMin) / axisSpan) * chartHeight;
  const ticks = Array.from({ length: 5 }, (_, index) => axisMin + (axisSpan * index) / 4);
  const xLabelStep = rows.length <= 5 ? 1 : Math.ceil((rows.length - 1) / 4);
  const singleBarWidth = Math.min(54, Math.max(22, chartWidth / Math.max(availableSeries.length * 1.7, 1)));
  const singleBarGap = Math.max(8, singleBarWidth * 0.38);
  const singleGroupWidth = availableSeries.length * singleBarWidth + Math.max(0, availableSeries.length - 1) * singleBarGap;
  const singleGroupLeft = chartLeft + (chartWidth - singleGroupWidth) / 2;
  const chartSeries = availableSeries.map((item, seriesIndex) => {
    const target = `${id}-${item.key}`;
    if (rows.length === 1) {
      const point = item.points.find((candidate) => candidate.value !== null);
      if (!point) return "";
      const x = singleGroupLeft + seriesIndex * (singleBarWidth + singleBarGap);
      const y = pointY(point.value);
      const height = Math.max(3, chartBottom - y);
      return `
        <g class="market-regime-series" data-regime-series="${escapeHtml(target)}">
          <rect x="${x}" y="${y}" width="${singleBarWidth}" height="${height}" rx="4" fill="${item.color}" opacity="0.92">
            <title>${escapeHtml(timeLabel(point.createdAt))} | ${escapeHtml(item.label)}: ${escapeHtml(formatMarketRegimeNumber(point.value))}</title>
          </rect>
          <circle cx="${x + singleBarWidth / 2}" cy="${y}" r="4.5" fill="${item.color}" stroke="var(--panel)" stroke-width="2">
            <title>${escapeHtml(timeLabel(point.createdAt))} | ${escapeHtml(item.label)}: ${escapeHtml(formatMarketRegimeNumber(point.value))}</title>
          </circle>
        </g>
      `;
    }
    const segments = [];
    let activeSegment = [];
    item.points.forEach((point) => {
      if (point.value === null) {
        if (activeSegment.length >= 2) segments.push(activeSegment);
        activeSegment = [];
        return;
      }
      activeSegment.push(point);
    });
    if (activeSegment.length >= 2) segments.push(activeSegment);
    const paths = rows.length >= 2 ? segments.map((segment) => {
      const path = segment.map((point, index) => {
        const command = index === 0 ? "M" : "L";
        return `${command} ${pointX(point.index)} ${pointY(point.value)}`;
      }).join(" ");
      return `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>`;
    }).join("") : "";
    const markers = item.points.filter((point) => point.value !== null).map((point) => `
      <circle cx="${pointX(point.index)}" cy="${pointY(point.value)}" r="4.5" fill="${item.color}" stroke="var(--panel)" stroke-width="2">
        <title>${escapeHtml(timeLabel(point.createdAt))} | ${escapeHtml(item.label)}: ${escapeHtml(formatMarketRegimeNumber(point.value))}</title>
      </circle>
    `).join("");
    return `
      <g class="market-regime-series" data-regime-series="${escapeHtml(target)}">
        ${paths}
        ${markers}
      </g>
    `;
  }).join("");
  const legend = availableSeries.map((item) => {
    const target = `${id}-${item.key}`;
    return `
      <button class="market-regime-legend-btn" type="button" data-regime-target="${escapeHtml(target)}" aria-pressed="true">
        <i style="background:${item.color}"></i>
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(formatMarketRegimeNumber(item.displayValue))}</strong>
      </button>
    `;
  }).join("");
  const snapshotHint = rows.length === 1
    ? '<p class="market-regime-snapshot-hint">Đang vẽ snapshot hiện tại; line xu hướng sẽ hiện khi có từ 2 snapshot trở lên.</p>'
    : "";
  return `
    <article class="market-regime-chart-card market-regime-snapshot-chart" data-regime-chart="${escapeHtml(id)}" data-snapshot-count="${rows.length}">
      <header>
        <div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></div>
        <span>${escapeHtml(rows.length === 1 ? "Snapshot hiện tại" : axisLabel)}</span>
      </header>
      <div class="market-regime-chart-canvas">
        <svg viewBox="0 0 540 224" role="img" aria-label="${escapeHtml(title)}">
          ${ticks.map((value, index) => {
            const y = chartBottom - (index / 4) * chartHeight;
            return `
              <g>
                <line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="market-regime-grid-line"></line>
                <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="market-regime-axis-text">${escapeHtml(marketRegimeChartAxis(value))}</text>
              </g>
            `;
          }).join("")}
          <line x1="${chartLeft}" y1="${chartBottom}" x2="${chartRight}" y2="${chartBottom}" class="market-regime-axis-line"></line>
          ${chartSeries}
          ${rows.map((snapshot, index) => (
            index % xLabelStep === 0 || index === rows.length - 1
              ? `<text x="${pointX(index)}" y="210" text-anchor="middle" class="market-regime-axis-text">${escapeHtml(timeOnlyLabel(marketRegimeSnapshotCreatedAt(snapshot)))}</text>`
              : ""
          )).join("")}
        </svg>
      </div>
      ${snapshotHint}
      <div class="market-regime-chart-legend">${legend}</div>
      ${missingNote}
    </article>
  `;
}

function bindMarketRegimeChartInteractions() {
  if (!refs.systemModuleDetail) return;
  refs.systemModuleDetail.querySelectorAll(".market-regime-legend-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.getAttribute("data-regime-target");
      if (!target) return;
      const isVisible = button.getAttribute("aria-pressed") !== "false";
      button.setAttribute("aria-pressed", isVisible ? "false" : "true");
      button.classList.toggle("muted", isVisible);
      refs.systemModuleDetail.querySelectorAll(".market-regime-series").forEach((series) => {
        if (series.getAttribute("data-regime-series") === target) series.classList.toggle("is-hidden", isVisible);
      });
    });
  });
}

function renderMarketRegimeLoadingSkeleton() {
  return `
    <section class="market-regime-loading" aria-label="Đang tải Market Regime">
      <div class="market-regime-skeleton status"></div>
      <div class="market-regime-skeleton-kpis">
        ${Array.from({ length: 3 }, () => '<div class="market-regime-skeleton kpi"></div>').join("")}
      </div>
      <div class="market-regime-skeleton-charts">
        ${Array.from({ length: 4 }, () => '<div class="market-regime-skeleton chart"></div>').join("")}
      </div>
      <div class="market-regime-skeleton table"></div>
    </section>
  `;
}

function renderMarketRegimeLoadError(message) {
  return `
    <div class="market-regime-load-error" role="alert">
      <strong>Không thể tải dữ liệu Market Regime.</strong>
      <span>${escapeHtml(message || "Lỗi không xác định")}</span>
    </div>
  `;
}

function renderMarketRegimeDetail(module, options = {}) {
  const regime = module?.market_regime && typeof module.market_regime === "object" ? module.market_regime : {};
  const historyPayload = marketRegimeNormalizeHistoryPayload(module, regime);
  const viewOptions = marketRegimeViewOptions(historyPayload);
  const selectedView = marketRegimeSelectedView(historyPayload);
  state.selectedMarketRegimeView = selectedView;
  const selectedSnapshot = marketRegimeSnapshotForView(regime, historyPayload, selectedView) || {};
  const selectedHistory = marketRegimeHistoryItemsForView(historyPayload, selectedView);
  const chartSnapshots = marketRegimeChartSnapshots(selectedSnapshot, selectedHistory);
  const latestChartSnapshot = chartSnapshots.at(-1) || selectedSnapshot || {};
  const indicators = latestChartSnapshot.indicators && typeof latestChartSnapshot.indicators === "object" ? latestChartSnapshot.indicators : {};
  const createdAt = latestChartSnapshot.created_at || marketRegimeStat(module, "updatedAt")?.value || null;
  const confidence = marketRegimeFiniteValue(latestChartSnapshot.confidence);
  const confidenceWidth = confidence === null ? 0 : Math.max(0, Math.min(100, confidence));
  const reasons = marketRegimeReasonRows(latestChartSnapshot.reason);
  const regimeCode = String(latestChartSnapshot.regime || "UNKNOWN").trim().toUpperCase();
  const error = String(latestChartSnapshot.error || regime.error || "").trim();
  const historySamples = selectedHistory.length;
  const isAggregateView = selectedView === MARKET_REGIME_VIEW_MARKET;
  const aggregateTargetCount = marketRegimeFiniteValue(indicators.target_count)
    || marketRegimeFiniteValue(historyPayload?.coverage?.target_count)
    || marketRegimeFiniteValue(historyPayload?.aggregate_limit)
    || 40;
  const aggregateScopeText = `top ${formatMarketRegimeNumber(aggregateTargetCount, 0)}`;
  const macd = marketRegimeFiniteValue(indicators.macd);
  const macdState = macd === null ? "" : macd > 0 ? "Dương" : macd < 0 ? "Âm" : "Trung tính";
  const trendSeries = isAggregateView
    ? [
        { key: "trend_score", label: "Trend Score", color: "#147a7e" },
        { key: "price_above_ema20_pct", label: "Giá > EMA20", color: "#315f9f" },
        { key: "ema20_above_ema50_pct", label: "EMA20 > EMA50", color: "#1f8a5b" },
        { key: "price_above_ema200_pct", label: "Giá > EMA200", color: "#7b61ff" },
        { key: "price_above_vwap_pct", label: "Giá > VWAP", color: "#b7791f" },
      ]
    : [
        { key: "ema_fast", label: "EMA20", color: "#147a7e" },
        { key: "ema_slow", label: "EMA50", color: "#315f9f" },
        { key: "ema200", label: "EMA200", color: "#1f8a5b" },
        { key: "vwap", label: "VWAP", color: "#b7791f" },
      ];
  const strengthSeries = [
    { key: "adx", label: "ADX", color: "#147a7e" },
    { key: "rsi", label: "RSI", color: "#315f9f" },
    { key: "fear_greed", label: "Fear & Greed", color: "#b7791f" },
    { key: "news_score", label: "News Score", color: "#bd3f32" },
  ];
  const volatilityFundingSeries = [
    {
      key: isAggregateView ? "median_atr_pct" : "atr_pct",
      keys: isAggregateView ? ["median_atr_pct", "atr_pct", "volatility"] : ["atr_pct", "volatility"],
      label: "ATR%",
      color: "#bd3f32",
    },
    {
      key: "funding_rate",
      keys: ["funding_rate", "funding", "fundingRate"],
      label: "Funding",
      color: "#315f9f",
    },
  ];
  const marketFlowSeries = [
    {
      key: isAggregateView ? "median_volume_ratio" : "volume_ratio",
      keys: isAggregateView ? ["median_volume_ratio", "volume_ratio", "volume"] : ["volume_ratio", "volume"],
      label: "Volume Ratio",
      color: "#147a7e",
    },
    {
      key: "open_interest",
      keys: ["open_interest", "open_interest_change", "openInterest", "openInterestChange"],
      label: "Open Interest",
      color: "#315f9f",
    },
  ];
  const kpiCards = isAggregateView
    ? [
        renderMarketRegimeKpiCard({ title: "Trend Score", subtitle: `Điểm xu hướng ${aggregateScopeText}`, value: indicators.trend_score, unitText: "0-100", createdAt }),
        renderMarketRegimeKpiCard({ title: `Độ phủ ${aggregateScopeText}`, subtitle: "Số cặp có snapshot thật", value: indicators.coverage_count, unitText: `${formatMarketRegimeNumber(aggregateTargetCount, 0)} cặp mục tiêu`, createdAt }),
        renderMarketRegimeKpiCard({ title: "RSI trung vị", subtitle: "Median RSI của nhóm đang có dữ liệu", value: indicators.median_rsi ?? indicators.rsi, unitText: "0-100", createdAt }),
      ]
    : [
        renderMarketRegimeKpiCard({ title: "ATR", subtitle: "Biên độ thực trung bình", value: indicators.atr, unitText: "Chưa có thông tin đơn vị", createdAt }),
        renderMarketRegimeKpiCard({ title: "MACD", subtitle: "Đường trung bình hội tụ phân kỳ", value: indicators.macd, unitText: "Chưa có thông tin đơn vị", createdAt, stateText: macdState }),
        renderMarketRegimeKpiCard({ title: "BTC Dominance", subtitle: "Tỷ lệ thống trị Bitcoin", value: indicators.btc_dominance, unitText: "Chưa có thông tin đơn vị", createdAt }),
      ];

  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "5")}</span>
        <h3 id="systemModuleTitle">Market Regime Detector</h3>
        <p>Bộ nhận diện trạng thái thị trường</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${error ? `<div class="market-regime-load-error" role="alert"><strong>Dữ liệu Market Regime đang lỗi.</strong><span>${escapeHtml(error)}</span></div>` : ""}
      ${renderMarketRegimeViewSelector(viewOptions, selectedView)}
      ${renderMarketRegimeCoverage(historyPayload, selectedView)}
      <section class="market-regime-status-card">
        <div class="market-regime-status-main">
          <div>
            <span>${escapeHtml(isAggregateView ? `Trạng thái tổng hợp thị trường ${aggregateScopeText}` : `Trạng thái ${marketRegimeSymbolLabel(selectedView)}`)}</span>
            <strong>${escapeHtml(marketRegimeLabel(regimeCode))}</strong>
            <small>${escapeHtml(regimeCode)}</small>
          </div>
          <span class="market-regime-badge ${escapeHtml(marketRegimeTone(regimeCode))}">${escapeHtml(marketRegimeLabel(regimeCode))}</span>
        </div>
        <div class="market-regime-confidence">
          <div><span>Độ tin cậy</span><strong>${confidence === null ? "-" : `${escapeHtml(formatMarketRegimeNumber(confidence, 2))}%`}</strong></div>
          <div class="market-regime-progress" role="progressbar" aria-label="Độ tin cậy" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${confidence === null ? "0" : confidenceWidth}">
            <span style="width:${confidenceWidth}%"></span>
          </div>
        </div>
        <div class="market-regime-status-meta">
          <div><span>Thời điểm ghi nhận</span><strong>${escapeHtml(timeLabel(createdAt))}</strong></div>
          <div><span>Snapshot phạm vi này</span><strong>${escapeHtml(formatMarketRegimeNumber(historySamples, 0))}</strong></div>
        </div>
        <div class="market-regime-reasons">
          <span>Lý do phân loại</span>
          ${reasons.length ? `<ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : '<p>Chưa có lý do phân loại trong dữ liệu hiện tại.</p>'}
        </div>
      </section>

      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Chỉ báo chính</strong><small>Snapshot hiện tại</small></div></div>
        <div class="market-regime-kpi-grid">${kpiCards.join("")}</div>
        <div class="market-regime-indicator-strip">${renderMarketRegimeIndicatorStrip(indicators)}</div>
      </section>

      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Bốn biểu đồ chính</strong><small>Snapshot hiện tại</small></div></div>
        <div class="market-regime-chart-grid">
          ${renderMarketRegimeSnapshotChart({
            id: "trend-structure",
            title: "Cấu trúc xu hướng",
            subtitle: isAggregateView ? `Trend Score và tỷ lệ ${aggregateScopeText} theo EMA/VWAP` : "EMA20, EMA50, EMA200 và VWAP",
            snapshots: chartSnapshots,
            series: trendSeries,
            axisLabel: isAggregateView ? "Tỷ lệ / điểm 0-100" : "Giá trị giá",
            fixedYMin: isAggregateView ? 0 : null,
            fixedYMax: isAggregateView ? 100 : null,
            error,
          })}
          ${renderMarketRegimeSnapshotChart({
            id: "strength-sentiment",
            title: "Sức mạnh xu hướng và tâm lý thị trường",
            subtitle: "ADX, RSI, Fear & Greed và News Score",
            snapshots: chartSnapshots,
            series: strengthSeries,
            axisLabel: isAggregateView ? "Thang 0-100 / điểm" : "Giá trị gốc",
            fixedYMin: isAggregateView ? 0 : null,
            fixedYMax: isAggregateView ? 100 : null,
            error,
          })}
          ${renderMarketRegimeSnapshotChart({
            id: "volatility-funding",
            title: "Độ biến động và Funding",
            subtitle: "ATR% và Funding",
            createdAt,
            snapshots: chartSnapshots,
            series: volatilityFundingSeries,
            axisLabel: "ATR% / Funding",
            error,
          })}
          ${renderMarketRegimeSnapshotChart({
            id: "market-flow",
            title: "Dòng tiền và mức tham gia thị trường",
            subtitle: "Volume Ratio và Open Interest",
            createdAt,
            snapshots: chartSnapshots,
            series: marketFlowSeries,
            axisLabel: "Volume Ratio / Open Interest",
            error,
          })}
        </div>
      </section>

      <section class="market-regime-history">
        <div class="market-regime-section-head"><div><strong>Lịch sử Market Regime</strong><small>Các bản ghi được cung cấp trong luồng frontend hiện tại</small></div></div>
        <div class="market-regime-empty">Phạm vi ${escapeHtml(isAggregateView ? "Market" : marketRegimeSymbolLabel(selectedView))} đang có ${escapeHtml(formatMarketRegimeNumber(historySamples, 0))} snapshot thật. Chart sẽ tự chuyển sang line khi có từ 2 snapshot trở lên.</div>
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  refs.systemModuleDetail.querySelectorAll(".market-regime-view-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const nextView = button.getAttribute("data-market-regime-view") || MARKET_REGIME_VIEW_MARKET;
      if (nextView === state.selectedMarketRegimeView) return;
      const scrollTop = moduleDetailScrollTop();
      state.selectedMarketRegimeView = nextView;
      renderMarketRegimeDetail(module, { scrollTop });
    });
  });
  bindMarketRegimeChartInteractions();
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function marketPatternLatest(module) {
  const payload = module?.market_pattern_engine && typeof module.market_pattern_engine === "object"
    ? module.market_pattern_engine
    : {};
  return payload.latest && typeof payload.latest === "object" ? payload.latest : null;
}

function marketPatternCount(module, key) {
  const counts = module?.market_pattern_engine?.counts && typeof module.market_pattern_engine.counts === "object"
    ? module.market_pattern_engine.counts
    : {};
  return Number(counts[key] || 0);
}

function renderMarketPatternPill(label, value) {
  return `
    <div class="market-regime-kpi">
      <div class="market-regime-kpi-head">
        <div>
          <strong>${escapeHtml(label)}</strong>
          <small>MongoDB</small>
        </div>
      </div>
      <div class="market-regime-kpi-value">${escapeHtml(value ?? "-")}</div>
      <div class="market-regime-kpi-meta">
        <span>Market Pattern Engine</span>
      </div>
    </div>
  `;
}

function renderMarketPatternMetric(label, value, unit = "") {
  return `
    <div>
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? "-")}${unit ? ` ${escapeHtml(unit)}` : ""}</strong>
    </div>
  `;
}

function marketPatternStatusState(item) {
  if (!item || typeof item !== "object") return null;
  const rawStatus = item.status ?? item.active ?? item.is_active ?? item.enabled;
  if (rawStatus === undefined || rawStatus === null || rawStatus === "") return null;
  if (typeof rawStatus === "boolean") return rawStatus ? "active" : "inactive";
  const normalized = String(rawStatus).trim().toLowerCase();
  if (["active", "enabled", "true", "1", "open", "valid"].includes(normalized)) return "active";
  return "inactive";
}

function renderMarketPatternStatusDot(item) {
  const state = marketPatternStatusState(item);
  if (!state) return escapeHtml(item?.direction || "-");
  const label = state === "active" ? "Active" : "Inactive";
  return `<span class="market-pattern-status-dot ${state}" title="${label}" aria-label="${label}" role="img"></span>`;
}

function renderMarketPatternList(title, rows, emptyText) {
  const items = Array.isArray(rows) ? rows : [];
  return `
    <section class="market-regime-section market-pattern-list-card">
      <div class="market-regime-section-head"><div><strong>${escapeHtml(title)}</strong></div></div>
      ${items.length
        ? `<div class="market-pattern-list">${items.map((item) => `
            <div class="market-pattern-list-row">
              <span class="market-pattern-list-name">${escapeHtml(item.pattern_type || item.pattern || item.type || "-")}</span>
              <span class="market-pattern-list-state">${renderMarketPatternStatusDot(item)}</span>
              <strong class="market-pattern-list-score">${escapeHtml(formatMarketRegimeNumber(item.confidence ?? item.strength_score ?? item.confluence_score))}</strong>
            </div>
          `).join("")}</div>`
        : `<div class="market-regime-empty">${escapeHtml(emptyText)}</div>`}
    </section>
  `;
}

function renderMarketPatternDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const payload = module.market_pattern_engine && typeof module.market_pattern_engine === "object" ? module.market_pattern_engine : {};
  const latest = marketPatternLatest(module);
  const structure = latest?.market_structure && typeof latest.market_structure === "object" ? latest.market_structure : {};
  const confluence = latest?.confluence && typeof latest.confluence === "object" ? latest.confluence : {};
  const feature = latest?.feature_vector && typeof latest.feature_vector === "object" ? latest.feature_vector : {};
  const support = Array.isArray(latest?.support_zones) ? latest.support_zones : [];
  const resistance = Array.isArray(latest?.resistance_zones) ? latest.resistance_zones : [];
  const candles = Array.isArray(latest?.candlestick_patterns) ? latest.candlestick_patterns : [];
  const charts = Array.isArray(latest?.chart_patterns) ? latest.chart_patterns : [];
  const smartMoney = Array.isArray(latest?.smart_money) ? latest.smart_money : [];
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "14")}</span>
        <h3 id="systemModuleTitle">Market Structure & Pattern Engine</h3>
        <p>Bộ máy nhận diện cấu trúc thị trường và mô hình giá</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${payload.error ? `<div class="market-regime-load-error" role="alert"><strong>Market Pattern Engine đang lỗi.</strong><span>${escapeHtml(payload.error)}</span></div>` : ""}
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Trạng thái engine</strong><small>Rule-based, không tự đặt lệnh</small></div></div>
        <div class="market-regime-kpi-grid">
          ${renderMarketPatternPill("Bản phân tích", formatMarketRegimeNumber(marketPatternCount(module, "market_analysis_snapshots"), 0))}
          ${renderMarketPatternPill("Mô hình", formatMarketRegimeNumber(marketPatternCount(module, "pattern_detections"), 0))}
          ${renderMarketPatternPill("Vùng giá", formatMarketRegimeNumber(marketPatternCount(module, "support_resistance_zones"), 0))}
          ${renderMarketPatternPill("Sự kiện cấu trúc", formatMarketRegimeNumber(marketPatternCount(module, "market_structure_events"), 0))}
        </div>
      </section>
      ${latest ? `
        <section class="market-regime-status-card">
          <div class="market-regime-status-main">
            <div>
              <span>${escapeHtml(`${latest.symbol || "-"} · ${latest.timeframe || "-"}`)}</span>
              <strong>${escapeHtml(structure.trend_regime || "range")}</strong>
              <small>${escapeHtml(latest.analysis_mode || "-")} · ${escapeHtml(timeLabel(latest.candle_close_time || latest.created_at))}</small>
            </div>
            <span class="market-regime-badge ${escapeHtml(String(confluence.bias || "neutral").toLowerCase())}">${escapeHtml(confluence.bias || "neutral")}</span>
          </div>
          <div class="market-regime-status-meta">
            ${renderMarketPatternMetric("Cấu trúc", structure.structure_state || "-")}
            ${renderMarketPatternMetric("Đồng thuận kỹ thuật", formatMarketRegimeNumber(confluence.confluence_score))}
            ${renderMarketPatternMetric("Chất lượng dữ liệu", formatMarketRegimeNumber(feature.data_quality_score ?? latest.data_quality?.score))}
          </div>
        </section>
        ${renderMarketPatternList("Mô hình nến", candles, "Chưa có mô hình nến được xác nhận.")}
        ${renderMarketPatternList("Mô hình giá", charts, "Chưa có chart pattern phù hợp.")}
        ${renderMarketPatternList("Smart Money", smartMoney, "Chưa có FVG, liquidity sweep hoặc order block heuristic.")}
        ${renderMarketPatternList("Vùng hỗ trợ", support, "Chưa có vùng support đủ điều kiện.")}
        ${renderMarketPatternList("Vùng kháng cự", resistance, "Chưa có vùng resistance đủ điều kiện.")}
      ` : `<div class="market-regime-empty">Chưa có snapshot. Engine sẽ có dữ liệu sau khi Opportunity Scanner hoặc Final Re-check gọi endpoint analyze/recheck.</div>`}
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function marketPatternItems(module) {
  const payload = module?.market_pattern_engine && typeof module.market_pattern_engine === "object"
    ? module.market_pattern_engine
    : {};
  return Array.isArray(payload.latest_items) ? payload.latest_items : [];
}

function marketPatternTone(value) {
  const clean = String(value || "").toLowerCase();
  if (clean.includes("bull")) return "bull";
  if (clean.includes("bear")) return "bear";
  if (clean.includes("range") || clean.includes("neutral") || clean.includes("mixed")) return "sideway";
  return "unknown";
}

function marketPatternBarAxis(value) {
  const number = marketRegimeFiniteValue(value);
  if (number === null) return "-";
  if (Math.abs(number) >= 10) return formatMarketRegimeNumber(number, 0);
  return formatMarketRegimeNumber(number, 2);
}

function marketPatternNiceMax(values, fallback = 1) {
  const maxValue = Math.max(...values.map((value) => Math.max(0, Number(value) || 0)), 0);
  if (maxValue <= 0) return fallback;
  if (maxValue <= 1) return 1;
  const padded = maxValue * 1.18;
  const magnitude = 10 ** Math.floor(Math.log10(padded));
  const normalized = padded / magnitude;
  const step = normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function renderMarketPatternBarChart({ id, title, subtitle, axisLabel, rows, fixedMax = null, emptyText = "Chưa có dữ liệu phù hợp để vẽ biểu đồ này." }) {
  const chartRows = (Array.isArray(rows) ? rows : [])
    .map((row, index) => ({
      ...row,
      value: marketRegimeFiniteValue(row.value),
      color: row.color || MODULE_CHART_COLORS[index % MODULE_CHART_COLORS.length],
    }))
    .filter((row) => row.value !== null);
  const hasPositiveValue = chartRows.some((row) => Number(row.value) > 0);
  if (!chartRows.length) {
    return "";
  }
  const chartLeft = 58;
  const chartRight = 504;
  const chartTop = 24;
  const chartBottom = 184;
  const chartHeight = chartBottom - chartTop;
  const axisMax = fixedMax !== null ? Number(fixedMax) || 1 : marketPatternNiceMax(chartRows.map((row) => row.value));
  const axisSpan = axisMax || 1;
  const ticks = Array.from({ length: 5 }, (_, index) => (axisSpan * index) / 4);
  const gap = 16;
  const slot = (chartRight - chartLeft) / chartRows.length;
  const width = Math.max(22, Math.min(54, slot - gap));
  const bars = chartRows.map((row, index) => {
    const safeValue = Math.max(0, Number(row.value) || 0);
    const height = safeValue > 0
      ? Math.max(6, Math.min(chartHeight, (safeValue / axisSpan) * chartHeight))
      : 4;
    const x = chartLeft + slot * index + (slot - width) / 2;
    const y = chartBottom - height;
    const label = row.shortLabel || row.label;
    return `
      <g>
        <rect x="${x}" y="${y}" width="${width}" height="${height}" rx="4" fill="${row.color}" class="market-pattern-bar" opacity="${hasPositiveValue ? "1" : "0.42"}">
          <title>${escapeHtml(row.label)}: ${escapeHtml(marketPatternBarAxis(row.value))}${row.unit ? ` ${escapeHtml(row.unit)}` : ""}</title>
        </rect>
        <text x="${x + width / 2}" y="${Math.max(chartTop + 10, y - 7)}" text-anchor="middle" class="market-regime-axis-text">${escapeHtml(marketPatternBarAxis(row.value))}</text>
        <text x="${x + width / 2}" y="207" text-anchor="middle" class="market-regime-axis-text">${escapeHtml(label)}</text>
      </g>
    `;
  }).join("");
  const legend = chartRows.map((row) => `
    <div class="market-pattern-legend-item">
      <i style="background:${row.color}"></i>
      <span>${escapeHtml(row.label)}</span>
      <strong>${escapeHtml(marketPatternBarAxis(row.value))}${row.unit ? ` ${escapeHtml(row.unit)}` : ""}</strong>
    </div>
  `).join("");
  return `
    <article class="market-regime-chart-card market-pattern-chart-card" data-pattern-chart="${escapeHtml(id)}">
      <header>
        <div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(subtitle)}</small></div>
        <span>${escapeHtml(axisLabel)}</span>
      </header>
      <div class="market-regime-chart-canvas">
        <svg viewBox="0 0 540 224" role="img" aria-label="${escapeHtml(title)}">
          ${ticks.map((value, index) => {
            const y = chartBottom - (index / 4) * chartHeight;
            return `
              <g>
                <line x1="${chartLeft}" y1="${y}" x2="${chartRight}" y2="${y}" class="market-regime-grid-line"></line>
                <text x="${chartLeft - 8}" y="${y + 4}" text-anchor="end" class="market-regime-axis-text">${escapeHtml(marketPatternBarAxis(value))}</text>
              </g>
            `;
          }).join("")}
          <line x1="${chartLeft}" y1="${chartBottom}" x2="${chartRight}" y2="${chartBottom}" class="market-regime-axis-line"></line>
          ${bars}
        </svg>
      </div>
      <div class="market-pattern-chart-legend">${legend}</div>
    </article>
  `;
}

function renderMarketPatternSnapshotStrip(latest, structure, confluence, feature) {
  const createdAt = latest?.updated_at || latest?.created_at || latest?.candle_close_time;
  const rows = [
    { title: "Snapshot mới nhất", value: `${latest?.symbol || "-"} · ${latest?.timeframe || "-"}`, meta: timeLabel(createdAt) },
    { title: "Regime", value: structure?.trend_regime || "-", meta: structure?.structure_state || "-" },
    { title: "Bias kỹ thuật", value: confluence?.bias || "-", meta: `Score ${formatMarketRegimeNumber(confluence?.confluence_score)}` },
    { title: "Dữ liệu", value: formatMarketRegimeNumber(feature?.data_quality_score ?? latest?.data_quality?.score), meta: "Thang 0-1" },
  ];
  return `
    <div class="market-pattern-summary-grid">
      ${rows.map((row) => `
        <article class="market-pattern-summary-card">
          <span>${escapeHtml(row.title)}</span>
          <strong>${escapeHtml(row.value ?? "-")}</strong>
          <small>${escapeHtml(row.meta || "-")}</small>
        </article>
      `).join("")}
    </div>
  `;
}

function renderMarketPatternDetailV2(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const payload = module.market_pattern_engine && typeof module.market_pattern_engine === "object" ? module.market_pattern_engine : {};
  const latest = marketPatternLatest(module);
  const structure = latest?.market_structure && typeof latest.market_structure === "object" ? latest.market_structure : {};
  const confluence = latest?.confluence && typeof latest.confluence === "object" ? latest.confluence : {};
  const feature = latest?.feature_vector && typeof latest.feature_vector === "object" ? latest.feature_vector : {};
  const support = Array.isArray(latest?.support_zones) ? latest.support_zones : [];
  const resistance = Array.isArray(latest?.resistance_zones) ? latest.resistance_zones : [];
  const candles = Array.isArray(latest?.candlestick_patterns) ? latest.candlestick_patterns : [];
  const charts = Array.isArray(latest?.chart_patterns) ? latest.chart_patterns : [];
  const smartMoney = Array.isArray(latest?.smart_money) ? latest.smart_money : [];
  const latestItems = marketPatternItems(module);
  const latestSampleText = latestItems.length ? `${formatMarketRegimeNumber(latestItems.length, 0)} snapshot gần nhất` : "Snapshot hiện tại";
  const recordRows = [
    { label: "Bản phân tích", shortLabel: "Phân tích", value: marketPatternCount(module, "market_analysis_snapshots"), unit: "bản ghi", color: MODULE_CHART_COLORS[0] },
    { label: "Mô hình đã lưu", shortLabel: "Mô hình", value: marketPatternCount(module, "pattern_detections"), unit: "bản ghi", color: MODULE_CHART_COLORS[1] },
    { label: "Vùng giá đã lưu", shortLabel: "Vùng giá", value: marketPatternCount(module, "support_resistance_zones"), unit: "bản ghi", color: MODULE_CHART_COLORS[2] },
    { label: "Sự kiện cấu trúc", shortLabel: "Cấu trúc", value: marketPatternCount(module, "market_structure_events"), unit: "bản ghi", color: MODULE_CHART_COLORS[3] },
  ];
  const latestDetectionRows = [
    { label: "Mô hình nến", shortLabel: "Nến", value: candles.length, unit: "mục", color: MODULE_CHART_COLORS[0] },
    { label: "Mô hình giá", shortLabel: "Giá", value: charts.length, unit: "mục", color: MODULE_CHART_COLORS[1] },
    { label: "Smart Money", shortLabel: "SMC", value: smartMoney.length, unit: "mục", color: MODULE_CHART_COLORS[2] },
    { label: "Vùng hỗ trợ", shortLabel: "Hỗ trợ", value: support.length, unit: "mục", color: MODULE_CHART_COLORS[4] },
    { label: "Vùng kháng cự", shortLabel: "Kháng cự", value: resistance.length, unit: "mục", color: MODULE_CHART_COLORS[5] },
  ];
  const scoreRows = [
    { label: "Đồng thuận kỹ thuật", shortLabel: "Đồng thuận", value: confluence.confluence_score, unit: "điểm", color: MODULE_CHART_COLORS[0] },
    { label: "Sức mạnh xu hướng", shortLabel: "Xu hướng", value: structure.trend_strength, unit: "điểm", color: MODULE_CHART_COLORS[1] },
    { label: "Chất lượng dữ liệu", shortLabel: "Dữ liệu", value: feature.data_quality_score ?? latest?.data_quality?.score, unit: "điểm", color: MODULE_CHART_COLORS[2] },
  ];
  const overviewMarkup = latest ? renderMarketPatternSnapshotStrip(latest, structure, confluence, feature) : `
    <div class="market-pattern-summary-grid market-pattern-empty-overview">
      <article class="market-pattern-summary-card">
        <span>Snapshot mới nhất</span>
        <strong>Chưa có</strong>
        <small>Chờ Mini/5.5 gửi OHLCV vào engine</small>
      </article>
      <article class="market-pattern-summary-card">
        <span>Bản phân tích</span>
        <strong>${escapeHtml(marketPatternBarAxis(recordRows[0]?.value || 0))}</strong>
        <small>MongoDB market_analysis_snapshots</small>
      </article>
      <article class="market-pattern-summary-card">
        <span>Mô hình đã lưu</span>
        <strong>${escapeHtml(marketPatternBarAxis(recordRows[1]?.value || 0))}</strong>
        <small>Candle, chart pattern, Smart Money</small>
      </article>
      <article class="market-pattern-summary-card">
        <span>Vùng giá / cấu trúc</span>
        <strong>${escapeHtml(marketPatternBarAxis((Number(recordRows[2]?.value) || 0) + (Number(recordRows[3]?.value) || 0)))}</strong>
        <small>Support, resistance, BOS/CHoCH</small>
      </article>
    </div>
  `;
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "14")}</span>
        <h3 id="systemModuleTitle">Market Structure & Pattern Engine</h3>
        <p>Bộ máy nhận diện cấu trúc thị trường, mô hình nến, mô hình giá và vùng giá cho Mini/5.5.</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${payload.error ? `<div class="market-regime-load-error" role="alert"><strong>Market Pattern Engine đang lỗi.</strong><span>${escapeHtml(payload.error)}</span></div>` : ""}
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tổng quan engine</strong><small>Rule-based, chỉ phân tích và xuất feature cho AI</small></div></div>
        ${overviewMarkup}
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Biểu đồ tổng hợp</strong><small>Gom biến cùng đơn vị tính và cùng mục đích</small></div></div>
        <div class="market-regime-chart-grid market-pattern-chart-grid">
          ${renderMarketPatternBarChart({ id: "market-pattern-records", title: "Dữ liệu đã lưu trong MongoDB", subtitle: "Các collection cùng đơn vị bản ghi", axisLabel: "Bản ghi", rows: recordRows })}
          ${renderMarketPatternBarChart({ id: "market-pattern-latest-detections", title: "Kết quả nhận diện snapshot mới nhất", subtitle: "Các nhóm mô hình/vùng giá cùng đơn vị mục", axisLabel: "Mục", rows: latestDetectionRows })}
          ${renderMarketPatternBarChart({ id: "market-pattern-scores", title: "Điểm chất lượng và đồng thuận", subtitle: "Các biến cùng thang đo 0-1", axisLabel: "Điểm 0-1", rows: scoreRows, fixedMax: 1 })}
          <article class="market-regime-status-card market-pattern-status-card">
            <div class="market-regime-status-main">
              <div>
                <span>${escapeHtml(latest ? `${latest.symbol || "-"} · ${latest.timeframe || "-"}` : "Chưa có snapshot")}</span>
                <strong>${escapeHtml(structure.trend_regime || "Chưa có dữ liệu")}</strong>
                <small>${escapeHtml(latest ? `${latestSampleText} · ${timeLabel(latest.candle_close_time || latest.created_at)}` : "Engine sẽ có dữ liệu sau pool Mini/recheck.")}</small>
              </div>
              <span class="market-regime-badge ${escapeHtml(marketPatternTone(confluence.bias || structure.trend_regime))}">${escapeHtml(confluence.bias || "neutral")}</span>
            </div>
            <div class="market-regime-status-meta">
              ${renderMarketPatternMetric("Cấu trúc", structure.structure_state || "-")}
              ${renderMarketPatternMetric("BOS", structure.bos?.detected ? (structure.bos.direction || "detected") : "Không")}
              ${renderMarketPatternMetric("CHoCH", structure.choch?.detected ? (structure.choch.direction || "detected") : "Không")}
              ${renderMarketPatternMetric("Cập nhật", timeLabel(latest?.updated_at || latest?.created_at) || "-")}
            </div>
          </article>
        </div>
      </section>
      ${latest ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Chi tiết snapshot mới nhất</strong><small>Danh sách pattern và vùng giá đã nhận diện</small></div></div>
          <div class="market-pattern-detail-grid">
            ${renderMarketPatternList("Mô hình nến", candles, "Chưa có mô hình nến được xác nhận.")}
            ${renderMarketPatternList("Mô hình giá", charts, "Chưa có chart pattern phù hợp.")}
            ${renderMarketPatternList("Smart Money", smartMoney, "Chưa có FVG, liquidity sweep hoặc order block heuristic.")}
            ${renderMarketPatternList("Vùng hỗ trợ", support, "Chưa có vùng support đủ điều kiện.")}
            ${renderMarketPatternList("Vùng kháng cự", resistance, "Chưa có vùng resistance đủ điều kiện.")}
          </div>
        </section>
      ` : ""}
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function tradeExecutionItems(module) {
  const payload = module?.trade_execution && typeof module.trade_execution === "object" ? module.trade_execution : {};
  return Array.isArray(payload.open_items) ? payload.open_items : [];
}

function tradeExecutionConfig(module) {
  const payload = module?.trade_execution && typeof module.trade_execution === "object" ? module.trade_execution : {};
  return payload.trailing_config && typeof payload.trailing_config === "object" ? payload.trailing_config : {};
}

function tradeExecutionClosedItems(module) {
  const payload = module?.trade_execution && typeof module.trade_execution === "object" ? module.trade_execution : {};
  return Array.isArray(payload.recent_closed) ? payload.recent_closed : [];
}

function tradeExecutionPayload(module) {
  return module?.trade_execution && typeof module.trade_execution === "object" ? module.trade_execution : {};
}

function positionManagementBaseModule(modules) {
  const rows = Array.isArray(modules) ? modules : [];
  return rows.find((module) => isTradeExecutionModule(module)) || null;
}

function countTradeExecutionClosedByReason(items, reason) {
  return (Array.isArray(items) ? items : []).filter((item) => String(item?.close_reason || "").toLowerCase() === reason).length;
}

function sumTradeExecutionPnl(items) {
  return (Array.isArray(items) ? items : []).reduce((sum, item) => {
    const value = Number(item?.pnl);
    return Number.isFinite(value) ? sum + value : sum;
  }, 0);
}

function positionManagementStats(baseModule, sectionKey) {
  const payload = tradeExecutionPayload(baseModule);
  const openItems = tradeExecutionItems(baseModule);
  const closedItems = tradeExecutionClosedItems(baseModule);
  const config = tradeExecutionConfig(baseModule);
  const pendingTotal = Number(payload.pending_total ?? payload.pending_count ?? 0) || 0;
  const closedPnl = sumTradeExecutionPnl(closedItems);
  const tpClosed = countTradeExecutionClosedByReason(closedItems, "take_profit");
  const slClosed = countTradeExecutionClosedByReason(closedItems, "stop_loss");
  const trailingEnabled = Boolean(config.enabled);
  const partialEnabled = Boolean(config.partial_enabled);
  const mismatchCount = Number(payload.reconciliation_issue_count ?? payload.mismatch_count ?? 0) || 0;
  const common = {
    openItems,
    closedItems,
    openCount: Number(payload.open_count ?? openItems.length) || 0,
    closedCount: Number(payload.closed_count ?? closedItems.length) || 0,
    pendingTotal,
    partialDone: Number(payload.partial_done_count ?? 0) || 0,
    waitingPartial: Number(payload.waiting_partial_count ?? 0) || 0,
    tpClosed,
    slClosed,
    closedPnl,
    trailingEnabled,
    partialEnabled,
    mismatchCount,
    hasError: Boolean(payload.error),
  };
  const statsBySection = {
    overview: [
      { label: "Vị thế mở", value: common.openCount },
      { label: "Lệnh đóng gần đây", value: common.closedCount },
      { label: "Đang chờ chốt 1 phần", value: common.waitingPartial },
      { label: "Trailing Stop", value: trailingEnabled ? "Bật" : "Tắt" },
    ],
    pending_orders: [
      { label: "Pending hiện có", value: common.pendingTotal },
      { label: "Vị thế mở", value: common.openCount },
      { label: "Lệnh đóng gần đây", value: common.closedCount },
      { label: "Lỗi module", value: common.hasError ? 1 : 0 },
    ],
    profit_protection: [
      { label: "Partial TP", value: partialEnabled ? "Bật" : "Tắt" },
      { label: "Đã partial", value: common.partialDone },
      { label: "Trailing Stop", value: trailingEnabled ? "Bật" : "Tắt" },
      { label: "Closed PnL", value: formatMarketRegimeNumber(closedPnl) },
    ],
    profit_unused: [
      { label: "Partial TP", value: partialEnabled ? "Bật" : "Tắt" },
      { label: "Đã partial", value: common.partialDone },
      { label: "Closed PnL", value: formatMarketRegimeNumber(closedPnl) },
      { label: "Đóng do TP", value: tpClosed },
    ],
    sync_journal: [
      { label: "Lệch đồng bộ", value: mismatchCount },
      { label: "Nguồn close OKX", value: closedItems.filter((item) => item.exchange_close_source).length },
      { label: "Lỗi module", value: common.hasError ? 1 : 0 },
      { label: "Lệnh đóng gần đây", value: common.closedCount },
    ],
  };
  return { ...common, stats: statsBySection[sectionKey] || statsBySection.overview };
}

function positionManagementSections(baseModule) {
  if (!baseModule) return [];
  const payload = tradeExecutionPayload(baseModule);
  const status = payload.error ? "warn" : (baseModule.status || "ok");
  const sections = [
    {
      key: "overview",
      name: "Tổng quan vị thế",
      display_name: "Tổng quan vị thế",
      display_subtitle: "",
      purpose: "Theo dõi vị thế đang mở, biểu đồ tiến độ TP, TP/SL và lệnh đóng gần đây.",
    },
    {
      key: "pending_orders",
      name: "Pending & Orders",
      display_name: "Pending & Orders",
      display_subtitle: "Setup chờ, lệnh chờ và trạng thái OKX",
      purpose: "Theo dõi setup chờ, lệnh đã gửi, fill, cancel, reject và expired.",
    },
    {
      key: "profit_protection",
      name: "Bảo vệ vị thế",
      display_name: "Bảo vệ vị thế",
      display_subtitle: "SL, break-even, trailing stop, liquidation warning",
      purpose: "Theo dõi các lớp bảo vệ sau khi vị thế đã mở.",
    },
    {
      key: "profit",
      name: "Chốt lời",
      display_name: "Chốt lời",
      display_subtitle: "TP, partial TP, remaining size, closed PnL",
      purpose: "Theo dõi kế hoạch chốt lời, partial TP và PnL đã đóng.",
    },
    {
      key: "sync_journal",
      name: "Đồng bộ & Nhật ký",
      display_name: "Đồng bộ & Nhật ký",
      display_subtitle: "OKX reconciliation, journal và event quan trọng",
      purpose: "Theo dõi dữ liệu bot có khớp OKX không và các sự kiện vòng đời vị thế.",
    },
  ];
  return sections.filter((section) => !["profit", "sync_journal", "pending_orders"].includes(section.key)).map((section, index) => {
    const mergedSection = section.key === "profit_protection"
      ? {
          ...section,
          name: "Ch\u1ed1t l\u1eddi & B\u1ea3o v\u1ec7",
          display_name: "Ch\u1ed1t l\u1eddi & B\u1ea3o v\u1ec7",
          display_subtitle: "Partial TP, TP m\u1edbi, SL d\u01b0\u01a1ng v\u00e0 trailing stop",
          purpose: "Theo d\u00f5i ch\u1ed1t 30%, ph\u1ea7n c\u00f2n l\u1ea1i \u0111ang g\u1ed3ng, TP m\u1edbi v\u00e0 SL b\u1ea3o v\u1ec7 theo b\u1ed1 c\u1ee5c 50/50.",
        }
      : section;
    const summary = positionManagementStats(baseModule, mergedSection.key);
    const warn = mergedSection.key === "sync_journal" ? summary.mismatchCount > 0 || summary.hasError : summary.hasError;
    return {
      ...baseModule,
      number: `15.${index + 1}`,
      name: mergedSection.name,
      display_name: mergedSection.display_name,
      display_subtitle: mergedSection.display_subtitle,
      purpose: mergedSection.purpose,
      status: warn ? "warn" : status,
      position_management_section: mergedSection.key,
      source_module_number: baseModule.number || 15,
      stats: summary.stats.map((item) => ({ ...item, attention: warn })),
    };
  });
}

function tradeExecutionPartialBadge(item) {
  const levels = item?.profit_protection_levels && typeof item.profit_protection_levels === "object"
    ? item.profit_protection_levels
    : {};
  const lossGuard = levels.loss_guard && typeof levels.loss_guard === "object" ? levels.loss_guard : null;
  const lossGuardDone = Boolean(lossGuard?.executed || item?.loss_guard_partial_done);
  if (lossGuardDone) {
    return { label: "Đã chốt lỗ 25%", tone: "bear" };
  }
  const profitPartialDone = Boolean(levels.partial_30?.executed);
  if (profitPartialDone) {
    return { label: "Đã chốt lời 30%", tone: "bull" };
  }
  return { label: "Chờ chốt 1 phần", tone: "unknown" };
}

function renderTradeExecutionPositionCard(item) {
  const symbol = item.symbol || "-";
  const side = String(item.side || "-").toUpperCase();
  const badge = tradeExecutionPartialBadge(item);
  const entry = tradeExecutionLiveEntry(item);
  return `
    <article class="trade-execution-position">
      <header>
        <div>
          <span>${escapeHtml(side)}</span>
          <strong>${escapeHtml(symbol)}</strong>
          <small>Entry: ${escapeHtml(formatMarketRegimeNumber(entry))}</small>
        </div>
        <span class="market-regime-badge ${badge.tone}">${escapeHtml(badge.label)}</span>
      </header>
    </article>
  `;
}

function renderTradeExecutionClosedList(items) {
  const rows = Array.isArray(items) ? items.slice(0, 8) : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Chưa có lệnh đóng gần đây.</div>`;
  return `
    <div class="trade-execution-closed-list">
      ${rows.map((item) => `
        <div class="trade-execution-closed-row">
          <span>${escapeHtml(item.symbol || "-")}</span>
          <strong>${escapeHtml(item.status || "-")}</strong>
          <span>${escapeHtml(item.close_reason || "-")}</span>
          <b>${escapeHtml(formatMarketRegimeNumber(item.pnl))}</b>
        </div>
      `).join("")}
    </div>
  `;
}


function tradeExecutionPositionTabLabel(item, index) {
  const symbol = String(item?.symbol || `#${index + 1}`);
  const side = String(item?.side || "").toUpperCase();
  return side ? `${symbol} ${side}` : symbol;
}

function tradeExecutionPositionTabKey(item, index) {
  const symbol = String(item?.symbol || `#${index + 1}`);
  const side = String(item?.side || "").toUpperCase();
  return `${symbol}|${side}`;
}

function tradeExecutionCloseBadges(item) {
  const events = Array.isArray(item?.trade_event_history) ? item.trade_event_history : [];
  const hasLossGuard = Boolean(item?.loss_guard_partial_done)
    || events.some((event) => String(event?.type || "") === "loss_guard_partial_close");
  const hasProfitPartial = Boolean(item?.partial_take_profit_done)
    || events.some((event) => {
      if (String(event?.type || "") !== "partial_close") return false;
      const pnl = nullableNumber(event?.manual_partial_history?.pnl ?? event?.partial_pnl ?? event?.pnl);
      return pnl === null || pnl > 0;
    });
  const badges = [];
  if (hasProfitPartial && !hasLossGuard) badges.push({ label: "Chốt lời 30%", tone: "take-profit" });
  if (hasLossGuard) badges.push({ label: "Chốt lỗ 25%", tone: "stop-loss" });
  return badges;
}

function tradeExecutionChartRowsForItem(item) {
  const progress = Math.max(0, Math.min(100, Number(item?.tp_progress_pct) || 0));
  const levels = item?.profit_protection_levels && typeof item.profit_protection_levels === "object"
    ? item.profit_protection_levels
    : {};
  return {
    progressRows: [
      { label: item?.symbol || "Vi the", shortLabel: "TP", value: progress, unit: "%", color: item?.partial_take_profit_done ? MODULE_CHART_COLORS[2] : MODULE_CHART_COLORS[0] },
      { label: "Moc partial", shortLabel: "70%", value: 70, unit: "%", color: MODULE_CHART_COLORS[4] },
    ],
    targetRows: [
      { label: "Entry live", shortLabel: "Entry", value: tradeExecutionLiveEntry(item), unit: "gia", color: MODULE_CHART_COLORS[0] },
      { label: "SL live", shortLabel: "SL", value: levels.current_stop_loss ?? item?.stop_loss, unit: "gia", color: MODULE_CHART_COLORS[3] },
      { label: "TP live", shortLabel: "TP", value: levels.current_take_profit ?? item?.take_profit, unit: "gia", color: MODULE_CHART_COLORS[2] },
      { label: "Mark", shortLabel: "Mark", value: item?.current_price ?? item?.mark_price, unit: "gia", color: MODULE_CHART_COLORS[1] },
    ],
  };
}

function renderTradeExecutionPositionTabs(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Chua co vi the mo de ve chart.</div>`;
  return `
    <div class="trade-execution-tabs" role="tablist">
      ${rows.map((item, index) => `
        <button class="trade-execution-tab ${index === 0 ? "active" : ""}" type="button" role="tab" aria-selected="${index === 0 ? "true" : "false"}" data-trade-tab="${index}">
          ${escapeHtml(tradeExecutionPositionTabLabel(item, index))}
        </button>
      `).join("")}
    </div>
    ${rows.map((item, index) => {
      const chartRows = tradeExecutionChartRowsForItem(item);
      return `
        <div class="trade-execution-tab-panel" role="tabpanel" data-trade-panel="${index}" ${index === 0 ? "" : "hidden"}>
          <div class="market-regime-chart-grid market-pattern-chart-grid">
            ${renderMarketPatternBarChart({ id: `trade-execution-progress-${index}`, title: "Tien do toi TP", subtitle: "Moc partial mac dinh 70%", axisLabel: "%", rows: chartRows.progressRows, fixedMax: 100, emptyText: "Chua co vi the mo." })}
            ${renderMarketPatternBarChart({ id: `trade-execution-targets-${index}`, title: "Entry / SL / TP / Mark", subtitle: "Cac moc gia dang duoc OKX/Atlas theo doi", axisLabel: "Gia", rows: chartRows.targetRows, emptyText: "Chua co moc gia de ve." })}
          </div>
        </div>
      `;
    }).join("")}
  `;
}

function formatTradeExecutionPnl(value) {
  const numeric = nullableNumber(value);
  if (numeric === null) return "-";
  const sign = numeric > 0 ? "+" : "";
  return `${sign}${numeric.toFixed(2)} USDT`;
}

function formatPositionQuantityUsdt(contracts, price, contractSize) {
  const contractCount = nullableNumber(contracts);
  const priceValue = nullableNumber(price);
  const sizeValue = nullableNumber(contractSize) ?? 1;
  if (contractCount === null) return "-";
  const contractText = `${formatMarketRegimeNumber(contractCount)} contract`;
  if (priceValue === null || sizeValue <= 0) return contractText;
  const usdtValue = contractCount * sizeValue * priceValue;
  return `${formatMarketRegimeNumber(usdtValue, 2)} USDT (${contractText})`;
}

function calculateRuntimePositionPnl(side, entryPrice, targetPrice, contracts, contractSize) {
  const entry = nullableNumber(entryPrice);
  const target = nullableNumber(targetPrice);
  const amount = nullableNumber(contracts);
  const size = nullableNumber(contractSize) ?? 1;
  if (entry === null || target === null || amount === null || size <= 0) return null;
  const direction = normalizedPositionSide(side) === "short" ? -1 : 1;
  return (target - entry) * amount * size * direction;
}

function profitProtectionLevelWithRuntimePnl(level, item, amount, contractSize) {
  if (!level || typeof level !== "object") return level;
  const runtimePnl = calculateRuntimePositionPnl(
    item?.side,
    tradeExecutionLiveEntry(item),
    level.price,
    amount,
    contractSize
  );
  if (runtimePnl === null) return level;
  return {
    ...level,
    pnl: runtimePnl,
  };
}

function renderProfitProtectionLevel(label, level, tone = "", options = {}) {
  const price = level && typeof level === "object" ? level.price : null;
  const pnl = level && typeof level === "object" ? level.pnl : null;
  const triggerPrice = level && typeof level === "object" ? level.trigger_price : null;
  const extraRows = Array.isArray(options.extraRows) ? options.extraRows : [];
  const initialAmount = level && typeof level === "object" ? nullableNumber(level.initial_amount) : null;
  const showInitialAmount = options.showInitialAmount !== false;
  const displayExtraRows = !showInitialAmount || initialAmount === null
    ? extraRows
    : [...extraRows, `Khối lượng ban đầu: ${formatMarketRegimeNumber(initialAmount)} contract`];
  const showTrigger = options.showTrigger !== false;
  const achieved = Boolean(options.achieved);
  const disabled = Boolean(options.disabled);
  return `
    <div class="profit-protection-level ${tone ? `level-${tone}` : ""} ${achieved ? "level-achieved" : ""} ${disabled ? "level-disabled" : ""}">
      <span>${escapeHtml(label)}${achieved ? `<i class="profit-level-check" aria-label="Da dat">✓</i>` : ""}</span>
      <strong>${escapeHtml(formatMarketRegimeNumber(price))}</strong>
      <b class="${nullableNumber(pnl) < 0 ? "negative" : nullableNumber(pnl) > 0 ? "positive" : ""}">${escapeHtml(formatTradeExecutionPnl(pnl))}</b>
      ${showTrigger && nullableNumber(triggerPrice) !== null ? `<small>Kích hoạt: ${escapeHtml(formatMarketRegimeNumber(triggerPrice))}</small>` : ""}
      ${displayExtraRows.map((row) => `<small>${escapeHtml(row)}</small>`).join("")}
    </div>
  `;
}

function profitProtectionPriceReached(side, livePrice, targetPrice, tolerance = 1e-8) {
  const live = nullableNumber(livePrice);
  const target = nullableNumber(targetPrice);
  if (live === null || target === null) return false;
  const direction = normalizedPositionSide(side) === "short" ? -1 : 1;
  return direction > 0 ? live + tolerance >= target : live - tolerance <= target;
}

function profitProtectionStepReached(item, level, livePrice) {
  if (!level || typeof level !== "object") return false;
  return profitProtectionPriceReached(item?.side, livePrice, level.price);
}

function renderLossGuardLevel(lossGuard) {
  if (!lossGuard || typeof lossGuard !== "object") return "";
  const closePct = Math.round((Number(lossGuard.partial_close_fraction) || 0) * 100);
  const rrValue = nullableNumber(lossGuard.partial_close_r);
  const rrText = rrValue === null ? "-" : `${Number(rrValue).toFixed(2)}R`;
  return renderProfitProtectionLevel(`Chốt lỗ ${closePct}%`, {
    price: lossGuard.partial_close_price,
    pnl: lossGuard.partial_close_pnl,
    trigger_price: lossGuard.partial_close_price,
  }, "loss", {
    extraRows: [
      `Tỉ lệ RR: ${rrText}`,
      `Lỗ dự kiến: ${formatTradeExecutionPnl(lossGuard.partial_close_pnl)}`,
    ],
  });
}

function renderLossGuardLevelActual(lossGuard, levels = {}) {
  if (!lossGuard || typeof lossGuard !== "object") return "";
  const closePct = Math.round((Number(lossGuard.partial_close_fraction) || 0) * 100);
  const rrValue = nullableNumber(lossGuard.partial_close_r);
  const rrText = rrValue === null ? "-" : `${Number(rrValue).toFixed(2)}R`;
  const executed = Boolean(lossGuard.executed);
  const displayPrice = executed && nullableNumber(lossGuard.actual_partial_price) !== null
    ? lossGuard.actual_partial_price
    : lossGuard.partial_close_price;
  const displayPnl = executed && nullableNumber(lossGuard.actual_partial_pnl) !== null
    ? lossGuard.actual_partial_pnl
    : lossGuard.partial_close_pnl;
  const displayAmount = executed && nullableNumber(lossGuard.actual_partial_amount) !== null
    ? lossGuard.actual_partial_amount
    : lossGuard.partial_close_amount;
  const contractSize = nullableNumber(levels?.contract_size) ?? nullableNumber(levels?.current_sl?.contract_size) ?? 1;
  const initialAmount = nullableNumber(levels?.current_sl?.initial_amount);
  const remainingAfterLoss = executed && initialAmount !== null && nullableNumber(displayAmount) !== null
    ? Math.max(0, initialAmount - Number(displayAmount))
    : nullableNumber(levels?.current_amount ?? levels?.remaining_amount);
  const disabledByProfitProtection = Boolean(levels?.partial_30?.executed) && !executed;
  return renderProfitProtectionLevel(`${executed ? "Đã chốt lỗ" : "Chốt lỗ"} ${closePct}%`, {
    price: displayPrice,
    pnl: displayPnl,
    trigger_price: lossGuard.actual_partial_trigger_price || lossGuard.partial_close_price,
  }, "loss", {
    achieved: executed,
    disabled: disabledByProfitProtection,
    showTrigger: false,
    extraRows: [
      `${executed ? "Khối lượng đã chốt" : "Khối lượng chốt"} ${closePct}%: ${formatPositionQuantityUsdt(displayAmount, displayPrice, contractSize)}`,
      `KL còn lại: ${formatPositionQuantityUsdt(remainingAfterLoss, displayPrice, contractSize)}`,
      lossGuard.applies === false ? "Áp dụng: hiển thị mốc, chưa tự động đóng" : "",
      `Thời gian: ${executed && lossGuard.executed_at ? timeLabel(lossGuard.executed_at) : "-"}`,
    ].filter(Boolean),
  });
}

renderLossGuardLevel = renderLossGuardLevelActual;

function profitProtectionExtraRows(rows) {
  return rows
    .filter((row) => row && row.value !== null && row.value !== undefined && String(row.value).trim() && String(row.value).trim() !== "-")
    .map((row) => `${row.label}: ${row.value}`);
}

function renderProfitProtectionPositionPanel(item) {
  item = tradeItemWithLiveOkxPosition(item);
  const levels = item?.profit_protection_levels || {};
  const tpSteps = Array.isArray(levels.tp_steps) ? levels.tp_steps : [];
  const slSteps = Array.isArray(levels.sl_steps) ? levels.sl_steps : [];
  const levelByStep = (rows, step) => rows.find((level) => Number(level?.step) === step) || null;
  const currentPnlLevel = { price: item?.mark_price, pnl: item?.pnl };
  const partialDone = Boolean(levels.partial_30?.executed);
  const extensionStep = Number(item?.profit_extension_step);
  const liveReachedStep = partialDone && Number.isFinite(extensionStep) ? Math.max(0, Math.min(3, extensionStep)) + 1 : null;
  const lossGuard = levels.loss_guard && typeof levels.loss_guard === "object" ? levels.loss_guard : null;
  const contractSize = nullableNumber(levels.contract_size ?? item?.contract_size) ?? 1;
  const rawCurrentSlLevel = levels.current_sl || levelByStep(slSteps, 1);
  const rawCurrentTpLevel = levels.current_tp || levelByStep(tpSteps, 1);
  const liveEntry = tradeExecutionLiveEntry(item);
  const initialEntry = nullableNumber(item?.initial_entry_price);
  const showInitialEntry = nullableNumber(liveEntry) !== null
    && initialEntry !== null
    && Math.abs(Number(liveEntry) - Number(initialEntry)) > 1e-12;
  const currentSlPrice = levels.current_stop_loss ?? rawCurrentSlLevel?.price ?? item?.stop_loss;
  const currentTpPrice = levels.current_take_profit ?? rawCurrentTpLevel?.price ?? item?.take_profit;
  const liveStopIsProtected = profitProtectionPriceReached(item?.side, currentSlPrice, liveEntry);
  const useLiveActiveStepOverride = !levels.manual_targets_are_initial && liveStopIsProtected;
  const liveLevelForStep = (level, step, livePrice) => {
    if (!useLiveActiveStepOverride) return level;
    if (liveReachedStep !== step) return level;
    if (!level || typeof level !== "object") return level;
    return { ...level, price: livePrice };
  };
  const slStepReached = (level) => partialDone && profitProtectionStepReached(item, level, currentSlPrice);
  const tpStepReached = (level) => partialDone && profitProtectionStepReached(item, level, currentTpPrice);
  const partialPrice = levels.partial_30?.price ?? item?.partial_take_profit_price;
  const currentAmountForRuntime = nullableNumber(levels.current_amount ?? item?.quantity);
  const runtimeLevel = (level) => profitProtectionLevelWithRuntimePnl(level, item, currentAmountForRuntime, contractSize);
  const currentSlLevel = runtimeLevel({ ...(rawCurrentSlLevel || {}), price: currentSlPrice });
  const currentTpLevel = runtimeLevel({ ...(rawCurrentTpLevel || {}), price: currentTpPrice });
  const initialExtraRows = profitProtectionExtraRows([
    { label: "Khối lượng ban đầu", value: formatPositionQuantityUsdt(levels.current_sl?.initial_amount ?? item?.initial_quantity, currentSlPrice, contractSize) },
  ]);
  const currentExtraRows = profitProtectionExtraRows([
    { label: "Mark", value: formatMarketRegimeNumber(item?.mark_price) },
    { label: "KL hi\u1ec7n t\u1ea1i", value: formatPositionQuantityUsdt(currentAmountForRuntime, item?.mark_price, contractSize) },
  ]);
  const partialBaseAmount = currentAmountForRuntime;
  const partialAmount = nullableNumber(item?.partial_take_profit_amount);
  const plannedPartialAmount = partialBaseAmount === null
    ? null
    : partialBaseAmount * 0.3;
  const afterProfitPartialAmount = partialBaseAmount === null
    ? null
    : partialDone
      ? currentAmountForRuntime
      : Math.max(0, partialBaseAmount - plannedPartialAmount);
  const partialLevel = profitProtectionLevelWithRuntimePnl(
    levels.partial_30,
    item,
    partialDone && partialAmount !== null ? partialAmount : plannedPartialAmount,
    contractSize
  );
  const partialTimeLabel = levels.partial_30?.executed_at ? timeLabel(levels.partial_30.executed_at) : "-";
  const badge = tradeExecutionPartialBadge(item);
  const partialExtraRows = [
    ...profitProtectionExtraRows([
    { label: "Khối lượng chốt 30%", value: formatPositionQuantityUsdt(partialDone && partialAmount !== null ? partialAmount : plannedPartialAmount, partialPrice, contractSize) },
    { label: "KL sau khi chốt lời 30%", value: formatPositionQuantityUsdt(afterProfitPartialAmount, partialPrice, contractSize) },
    ]),
    `Thời gian: ${partialTimeLabel}`,
  ];
  return `
    <article class="trade-execution-position profit-protection-position">
      <header>
        <div>
          <span>${escapeHtml(String(item?.side || "-").toUpperCase())}</span>
          <strong>${escapeHtml(item?.symbol || "-")}</strong>
          <small>Entry live: ${escapeHtml(formatMarketRegimeNumber(liveEntry))}</small>
          ${showInitialEntry ? `<small>Entry ban đầu: ${escapeHtml(formatMarketRegimeNumber(initialEntry))}</small>` : ""}
        </div>
        <span class="market-regime-badge ${badge.tone}">${escapeHtml(badge.label)}</span>
      </header>
      <div class="profit-protection-level-grid">
        ${renderProfitProtectionLevel("SL live", currentSlLevel, "base", { extraRows: initialExtraRows, showInitialAmount: false })}
        ${renderProfitProtectionLevel("TP live", currentTpLevel, "base")}
        ${renderProfitProtectionLevel("PnL hi\u1ec7n t\u1ea1i", currentPnlLevel, "current", { extraRows: currentExtraRows })}
        ${renderProfitProtectionLevel("Ch\u1ed1t l\u1eddi 30%", partialLevel, "partial", { achieved: partialDone, extraRows: partialExtraRows, showTrigger: false })}
        ${renderLossGuardLevelActual(lossGuard, levels)}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(slSteps, 2) || levels.sl2, 2, currentSlPrice)); return renderProfitProtectionLevel("SL2", level, "step2", { achieved: slStepReached(level) }); })()}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(tpSteps, 2) || levels.tp2, 2, currentTpPrice)); return renderProfitProtectionLevel("TP2", level, "step2", { showTrigger: false, achieved: tpStepReached(level) }); })()}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(slSteps, 3) || levels.sl3, 3, currentSlPrice)); return renderProfitProtectionLevel("SL3", level, "step3", { achieved: slStepReached(level) }); })()}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(tpSteps, 3), 3, currentTpPrice)); return renderProfitProtectionLevel("TP3", level, "step3", { showTrigger: false, achieved: tpStepReached(level) }); })()}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(slSteps, 4), 4, currentSlPrice)); return renderProfitProtectionLevel("SL4", level, "step4", { achieved: slStepReached(level) }); })()}
        ${(() => { const level = runtimeLevel(liveLevelForStep(levelByStep(tpSteps, 4), 4, currentTpPrice)); return renderProfitProtectionLevel("TP4", level, "step4", { showTrigger: false, achieved: tpStepReached(level) }); })()}
      </div>
    </article>
  `;
}

function renderProfitProtectionPositionTabs(items) {
  const rows = Array.isArray(items) ? items.map(tradeItemWithLiveOkxPosition) : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Ch\u01b0a c\u00f3 v\u1ecb th\u1ebf \u0111ang g\u1ed3ng.</div>`;
  const activeKey = rows.some((item, index) => tradeExecutionPositionTabKey(item, index) === state.selectedTradeExecutionTabKey)
    ? state.selectedTradeExecutionTabKey
    : tradeExecutionPositionTabKey(rows[0], 0);
  return `
    <div class="trade-execution-tabs" role="tablist">
      ${rows.map((item, index) => {
        const tabKey = tradeExecutionPositionTabKey(item, index);
        const active = tabKey === activeKey;
        return `
        <button class="trade-execution-tab ${active ? "active" : ""}" type="button" role="tab" aria-selected="${active ? "true" : "false"}" data-trade-tab="${index}" data-trade-tab-key="${escapeHtml(tabKey)}">
          ${escapeHtml(tradeExecutionPositionTabLabel(item, index))}
        </button>
      `;
      }).join("")}
    </div>
    ${rows.map((item, index) => {
      const active = tradeExecutionPositionTabKey(item, index) === activeKey;
      return `
      <div class="trade-execution-tab-panel" role="tabpanel" data-trade-panel="${index}" ${active ? "" : "hidden"}>
        ${renderProfitProtectionPositionPanel(item)}
      </div>
    `;
    }).join("")}
  `;
}

function findSystemModuleByKey(targetKey) {
  const modules = Array.isArray(state.lastSystemChecklistPayload?.modules)
    ? state.lastSystemChecklistPayload.modules
    : [];
  return groupedSystemModules(modules)
    .flatMap((group) => Array.isArray(group.items) ? group.items : [])
    .find((module) => systemModuleKey(module) === targetKey) || null;
}

function refreshOpenPositionManagementDetailFromLiveOkx() {
  if (!state.selectedSystemModuleKey || !refs.systemModuleOverlay || refs.systemModuleOverlay.hidden) return;
  const module = findSystemModuleByKey(state.selectedSystemModuleKey);
  if (!module || !isPositionManagementSection(module)) return;
  renderModuleDetail(module, {
    scrollTop: moduleDetailScrollTop(),
    activeChartIndex: moduleDetailActiveChartIndex(),
  });
}

function bindTradeExecutionTabs() {
  const root = refs.systemModuleDetail;
  if (!root) return;
  const tabs = Array.from(root.querySelectorAll("[data-trade-tab]"));
  const panels = Array.from(root.querySelectorAll("[data-trade-panel]"));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const selected = tab.getAttribute("data-trade-tab");
      state.selectedTradeExecutionTabKey = tab.getAttribute("data-trade-tab-key") || selected;
      tabs.forEach((node) => {
        const active = node.getAttribute("data-trade-tab") === selected;
        node.classList.toggle("active", active);
        node.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach((panel) => {
        panel.hidden = panel.getAttribute("data-trade-panel") !== selected;
      });
    });
  });
}

function bindPositionManagementDetailTabs() {
  const root = refs.systemModuleDetail;
  if (!root) return;
  const tabs = Array.from(root.querySelectorAll("[data-position-detail-tab]"));
  const panels = Array.from(root.querySelectorAll("[data-position-detail-panel]"));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const selected = tab.getAttribute("data-position-detail-tab");
      state.selectedPositionManagementDetailTab = selected;
      tabs.forEach((node) => {
        const active = node.getAttribute("data-position-detail-tab") === selected;
        node.classList.toggle("active", active);
        node.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach((panel) => {
        panel.hidden = panel.getAttribute("data-position-detail-panel") !== selected;
      });
    });
  });
}

function renderPositionManagementSectionDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const section = String(module.position_management_section || "overview");
  const payload = tradeExecutionPayload(module);
  const items = tradeExecutionItems(module);
  const closedItems = tradeExecutionClosedItems(module);
  const config = tradeExecutionConfig(module);
  const stats = positionManagementStats(module, section);
  const headingBySection = {
    overview: {
      title: "Tổng quan vị thế",
      subtitle: "",
    },
    pending_orders: {
      title: "Pending & Orders",
      subtitle: "Theo dõi setup chờ, lệnh chờ, lệnh đã gửi OKX và trạng thái fill/cancel/reject.",
    },
    protection: {
      title: "Bảo vệ vị thế",
      subtitle: "Theo dõi SL, break-even, trailing stop và các cảnh báo bảo vệ vị thế.",
    },
    profit: {
      title: "Chốt lời",
      subtitle: "Theo dõi TP, partial TP, remaining size và closed PnL.",
    },
    profit_protection: {
      title: "Ch\u1ed1t l\u1eddi & B\u1ea3o v\u1ec7",
      subtitle: "Theo d\u00f5i ch\u1ed1t 30%, TP m\u1edbi, SL d\u01b0\u01a1ng v\u00e0 trailing stop theo b\u1ed1 c\u1ee5c 50/50.",
    },
    sync_journal: {
      title: "Đồng bộ & Nhật ký",
      subtitle: "Theo dõi OKX reconciliation, nguồn dữ liệu close và event quan trọng.",
    },
  };
  const heading = headingBySection[section] || headingBySection.overview;
  const summaryCards = stats.stats.map((row) => `
    <article class="market-pattern-summary-card">
      <span>${escapeHtml(row.label || "-")}</span>
      <strong>${escapeHtml(row.value ?? "-")}</strong>
      ${row.detail || module.display_subtitle ? `<small>${escapeHtml(row.detail || module.display_subtitle)}</small>` : ""}
    </article>
  `).join("");
  const showTopSummary = section !== "profit_protection";
  const protectionRows = [
    { label: "Trailing Stop", value: config.enabled ? "Bật" : "Tắt" },
    { label: "ATR timeframe", value: config.atr_timeframe || "-" },
    { label: "ATR multiplier", value: config.atr_multiplier ?? "-" },
    { label: "Activation R", value: config.activation_r_multiple ?? "-" },
  ];
  const profitRows = [
    { label: "Partial TP", value: config.partial_enabled ? "Bật" : "Tắt" },
    { label: "Mốc kích hoạt", value: config.partial_trigger_tp_progress === null || config.partial_trigger_tp_progress === undefined ? "-" : `${formatMarketRegimeNumber(Number(config.partial_trigger_tp_progress) * 100)}% tới TP` },
    { label: "Tỷ lệ đóng", value: config.partial_close_fraction === null || config.partial_close_fraction === undefined ? "-" : `${formatMarketRegimeNumber(Number(config.partial_close_fraction) * 100)}%` },
    { label: "Closed PnL", value: formatMarketRegimeNumber(stats.closedPnl) },
  ];
  const activeDetailTab = positionManagementActiveDetailTab(section);
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "15")}</span>
        <div class="position-management-title-row">
          ${["overview", "profit_protection"].includes(section) ? "" : `<h3 id="systemModuleTitle">${escapeHtml(heading.title)}</h3>`}
          ${section === "overview" ? `
            <div class="position-management-detail-tabs" role="tablist" aria-label="Tổng quan vị thế và pending orders">
              ${renderPositionManagementDetailTabButton("overview", "Tổng quan vị thế", activeDetailTab)}
              ${renderPositionManagementDetailTabButton("pending_orders", "Pending & Orders", activeDetailTab)}
            </div>
          ` : ""}
          ${section === "profit_protection" ? `
            <div class="position-management-detail-tabs" role="tablist" aria-label="Chốt lời và nhật ký">
              ${renderPositionManagementDetailTabButton("profit_protection", "Chốt lời & Bảo vệ", activeDetailTab)}
              ${renderPositionManagementDetailTabButton("sync_journal", "Nhật ký", activeDetailTab)}
            </div>
          ` : ""}
        </div>
        ${heading.subtitle ? `<p class="${section === "profit_protection" ? "position-management-profit-subtitle" : ""}">${escapeHtml(heading.subtitle)}</p>` : ""}
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${payload.error ? `<div class="market-regime-load-error" role="alert"><strong>Quản lý vị thế đang lỗi.</strong><span>${escapeHtml(payload.error)}</span></div>` : ""}
      ${showTopSummary ? `<section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tóm tắt</strong>${module.display_subtitle ? `<small>${escapeHtml(module.display_subtitle)}</small>` : ""}</div></div>
        <div class="market-pattern-summary-grid">${summaryCards}</div>
      </section>` : ""}
      ${section === "overview" ? `
        <div data-position-detail-panel="overview" ${activeDetailTab === "overview" ? "" : "hidden"}>
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Biểu đồ vị thế</strong><small>Tiến độ tới TP và các mốc giá đang quản lý</small></div></div>
          ${renderTradeExecutionPositionTabs(items)}
        </section>
        ${items.length ? `<section class="market-regime-section"><div class="market-regime-section-head"><div><strong>Vị thế đang mở</strong><small>Partial chỉ chạy một lần, sau đó trailing SL</small></div></div><div class="trade-execution-position-grid">${items.map(renderTradeExecutionPositionCard).join("")}</div></section>` : `<div class="market-regime-empty">Chưa có vị thế live đang mở.</div>`}
        </div>
        <div data-position-detail-panel="pending_orders" ${activeDetailTab === "pending_orders" ? "" : "hidden"}>
          <section class="market-regime-section">
            <div class="market-regime-section-head"><div><strong>Lu\u1ed3ng l\u1ec7nh</strong><small>AI candidate -> pending setup -> pending order -> OKX order -> position</small></div></div>
            <div class="position-management-flow">
              <span>Pending setup</span><span>Pending order</span><span>Submitted</span><span>Filled</span><span>Position opened</span>
            </div>
            <div class="market-regime-empty compact">Hi\u1ec7n UI \u0111ang d\u00f9ng t\u1ed5ng pending t\u1eeb storage. Chi ti\u1ebft t\u1eebng pending/order s\u1ebd \u0111\u01b0\u1ee3c n\u1ed1i v\u00e0o khi backend expose danh s\u00e1ch order \u0111\u1ea7y \u0111\u1ee7.</div>
          </section>
        </div>
      ` : ""}
      ${section === "pending_orders" ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Luồng lệnh</strong><small>AI candidate → pending setup → pending order → OKX order → position</small></div></div>
          <div class="position-management-flow">
            <span>Pending setup</span><span>Pending order</span><span>Submitted</span><span>Filled</span><span>Position opened</span>
          </div>
          <div class="market-regime-empty compact">Hiện UI đang dùng tổng pending từ storage. Chi tiết từng pending/order sẽ được nối vào khi backend expose danh sách order đầy đủ.</div>
        </section>
      ` : ""}
      ${section === "profit_protection" ? `
        <div data-position-detail-panel="profit_protection" ${activeDetailTab === "profit_protection" ? "" : "hidden"}>
          <section class="market-regime-section position-management-rule-section">
            <div class="position-management-split position-management-split-compact">
              <article class="position-management-half">
                <div class="market-regime-section-head"><div><strong>Ch\u1ed1t l\u1eddi</strong><small>Partial TP, TP m\u1edbi v\u00e0 PnL \u0111\u00e3 \u0111\u00f3ng</small></div></div>
                <div class="market-regime-status-meta">${profitRows.map((row) => renderMarketPatternMetric(row.label, row.value)).join("")}</div>
                <div class="position-management-note">Telegram partial TP s\u1ebd hi\u1ec3n th\u1ecb l\u00e3i \u0111\u00e3 \u0103n v\u00e0 PnL d\u1ef1 ki\u1ebfn n\u1ebfu ch\u1ea1m TP m\u1edbi.</div>
              </article>
              <article class="position-management-half">
                <div class="market-regime-section-head"><div><strong>B\u1ea3o v\u1ec7</strong><small>SL d\u01b0\u01a1ng, break-even v\u00e0 trailing stop</small></div></div>
                <div class="market-regime-status-meta">${protectionRows.map((row) => renderMarketPatternMetric(row.label, row.value)).join("")}</div>
                <div class="position-management-note">Telegram partial TP s\u1ebd hi\u1ec3n th\u1ecb m\u1ee9c SL m\u1edbi \u0111ang b\u1ea3o v\u1ec7 bao nhi\u00eau USDT.</div>
              </article>
            </div>
          </section>
          <section class="market-regime-section profit-protection-live-section">
            <div class="market-regime-section-head"><div><strong>V\u1ecb th\u1ebf \u0111ang g\u1ed3ng</strong><small>M\u1ed7i tab l\u00e0 1 c\u1eb7p: SL/TP hi\u1ec7n t\u1ea1i, ch\u1ed1t 30%, TP2, SL2, SL3</small></div></div>
            ${renderProfitProtectionPositionTabs(items)}
          </section>
        </div>
        <div data-position-detail-panel="sync_journal" ${activeDetailTab === "sync_journal" ? "" : "hidden"}>
          <section class="market-regime-section">
            <div class="market-regime-section-head"><div><strong>Nhật ký close gần đây</strong><small>TP, SL, trailing stop hoặc reconciled close</small></div></div>
            ${renderTradeExecutionClosedList(closedItems)}
          </section>
        </div>
      ` : ""}
      ${section === "protection" ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Thông số bảo vệ</strong><small>SL, break-even và trailing stop</small></div></div>
          <div class="market-regime-status-meta">${protectionRows.map((row) => renderMarketPatternMetric(row.label, row.value)).join("")}</div>
        </section>
        ${items.length ? `<section class="market-regime-section"><div class="market-regime-section-head"><div><strong>SL theo vị thế</strong><small>Entry, SL, TP và R hiện tại</small></div></div><div class="trade-execution-position-grid">${items.map(renderTradeExecutionPositionCard).join("")}</div></section>` : ""}
      ` : ""}
      ${section === "profit" ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Kế hoạch chốt lời</strong><small>TP, partial TP và PnL đã đóng</small></div></div>
          <div class="market-regime-status-meta">${profitRows.map((row) => renderMarketPatternMetric(row.label, row.value)).join("")}</div>
        </section>
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Lệnh đóng gần nhất</strong><small>Màu xanh là TP, màu đỏ là SL</small></div></div>
          ${renderTradeExecutionClosedList(closedItems)}
        </section>
      ` : ""}
      ${section === "sync_journal" ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Nhật ký close gần đây</strong><small>TP, SL, trailing stop hoặc reconciled close</small></div></div>
          ${renderTradeExecutionClosedList(closedItems)}
        </section>
      ` : ""}
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  bindPositionManagementDetailTabs();
  bindTradeExecutionTabs();
  bindTradeExecutionClosedPagination();
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function renderTradeExecutionClosedList(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Chưa có lệnh đóng gần đây.</div>`;
  const pageSize = 5;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  return `
    <div class="trade-execution-closed-list" data-closed-list>
      ${rows.map((item, index) => {
        const reason = String(item.close_reason || "-");
        const reasonTone = reason === "take_profit" ? "take-profit" : reason === "stop_loss" ? "stop-loss" : "neutral";
        const status = String(item.status || "-").toUpperCase();
        const rowTone = status === "WIN" ? "win" : status === "LOSS" ? "loss" : "neutral";
        const badges = tradeExecutionCloseBadges(item);
        const page = Math.floor(index / pageSize);
        return `
          <div class="trade-execution-closed-row ${rowTone}" data-closed-page="${page}" ${page === 0 ? "" : "hidden"}>
            <span>${escapeHtml(item.symbol || "-")}</span>
            <strong>${escapeHtml(status)}</strong>
            <span class="trade-execution-close-badges">
              <i class="trade-execution-close-reason ${reasonTone}">${escapeHtml(reason)}</i>
              ${badges.map((badge) => `<i class="trade-execution-close-reason ${badge.tone}">${escapeHtml(badge.label)}</i>`).join("")}
            </span>
            <b>${escapeHtml(formatMarketRegimeNumber(item.pnl))}</b>
          </div>
        `;
      }).join("")}
      ${pageCount > 1 ? `
        <div class="trade-execution-pager" data-closed-pager data-page="0" data-page-count="${pageCount}">
          <button type="button" data-closed-page-prev disabled>Trước</button>
          <span>Trang <b data-closed-page-current>1</b>/${pageCount}</span>
          <button type="button" data-closed-page-next>Sau</button>
        </div>
      ` : ""}
    </div>
  `;
}

function bindTradeExecutionClosedPagination() {
  const root = refs.systemModuleDetail;
  if (!root) return;
  root.querySelectorAll("[data-closed-list]").forEach((list) => {
    const pager = list.querySelector("[data-closed-pager]");
    if (!pager) return;
    const pageCount = Number(pager.getAttribute("data-page-count")) || 1;
    const currentLabel = pager.querySelector("[data-closed-page-current]");
    const prev = pager.querySelector("[data-closed-page-prev]");
    const next = pager.querySelector("[data-closed-page-next]");
    const showPage = (page) => {
      const normalized = Math.max(0, Math.min(pageCount - 1, Number(page) || 0));
      pager.setAttribute("data-page", String(normalized));
      list.querySelectorAll("[data-closed-page]").forEach((row) => {
        row.hidden = Number(row.getAttribute("data-closed-page")) !== normalized;
      });
      if (currentLabel) currentLabel.textContent = String(normalized + 1);
      if (prev) prev.disabled = normalized <= 0;
      if (next) next.disabled = normalized >= pageCount - 1;
    };
    prev?.addEventListener("click", () => showPage((Number(pager.getAttribute("data-page")) || 0) - 1));
    next?.addEventListener("click", () => showPage((Number(pager.getAttribute("data-page")) || 0) + 1));
    showPage(0);
  });
}

function tradeExecutionChartRowsForItem(item) {
  const progress = Math.max(0, Math.min(100, Number(item?.tp_progress_pct) || 0));
  const levels = item?.profit_protection_levels && typeof item.profit_protection_levels === "object"
    ? item.profit_protection_levels
    : {};
  return {
    progressRows: [
      { label: item?.symbol || "Vị thế", shortLabel: "TP", value: progress, unit: "%", color: item?.partial_take_profit_done ? MODULE_CHART_COLORS[2] : MODULE_CHART_COLORS[0] },
      { label: "Mốc partial", shortLabel: "70%", value: 70, unit: "%", color: MODULE_CHART_COLORS[4] },
    ],
    targetRows: [
      { label: "Entry live", shortLabel: "Entry", value: tradeExecutionLiveEntry(item), unit: "giá", color: MODULE_CHART_COLORS[0] },
      { label: "SL live", shortLabel: "SL", value: levels.current_stop_loss ?? item?.stop_loss, unit: "giá", color: MODULE_CHART_COLORS[3] },
      { label: "TP live", shortLabel: "TP", value: levels.current_take_profit ?? item?.take_profit, unit: "giá", color: MODULE_CHART_COLORS[2] },
      { label: "Mark", shortLabel: "Mark", value: item?.current_price ?? item?.mark_price, unit: "giá", color: MODULE_CHART_COLORS[1] },
    ],
  };
}

function renderTradeExecutionPositionTabs(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Chưa có vị thế mở để vẽ chart.</div>`;
  return `
    <div class="trade-execution-tabs" role="tablist">
      ${rows.map((item, index) => `
        <button class="trade-execution-tab ${index === 0 ? "active" : ""}" type="button" role="tab" aria-selected="${index === 0 ? "true" : "false"}" data-trade-tab="${index}">
          ${escapeHtml(tradeExecutionPositionTabLabel(item, index))}
        </button>
      `).join("")}
    </div>
    ${rows.map((item, index) => {
      const chartRows = tradeExecutionChartRowsForItem(item);
      return `
        <div class="trade-execution-tab-panel" role="tabpanel" data-trade-panel="${index}" ${index === 0 ? "" : "hidden"}>
          <div class="market-regime-chart-grid market-pattern-chart-grid">
            ${renderMarketPatternBarChart({ id: `trade-execution-progress-${index}`, title: "Tiến độ tới TP", subtitle: "Mốc partial mặc định 70%", axisLabel: "%", rows: chartRows.progressRows, fixedMax: 100, emptyText: "Chưa có vị thế mở." })}
            ${renderMarketPatternBarChart({ id: `trade-execution-targets-${index}`, title: "Entry / SL / TP / Mark", subtitle: "Các mốc giá đang được OKX/Atlas theo dõi", axisLabel: "Giá", rows: chartRows.targetRows, emptyText: "Chưa có mốc giá để vẽ." })}
          </div>
        </div>
      `;
    }).join("")}
  `;
}

function renderTradeExecutionDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const payload = module.trade_execution && typeof module.trade_execution === "object" ? module.trade_execution : {};
  const items = tradeExecutionItems(module);
  const closedItems = tradeExecutionClosedItems(module);
  const config = tradeExecutionConfig(module);
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "15")}</span>
      <h3 id="systemModuleTitle">Thực thi giao dịch & Quản lý vị thế</h3>
        <p>Thực thi giao dịch, partial take-profit, trailing SL và gồng lãi cho vị thế đang mở.</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${payload.error ? `<div class="market-regime-load-error" role="alert"><strong>Trade Execution đang lỗi.</strong><span>${escapeHtml(payload.error)}</span></div>` : ""}
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tổng quan thực thi</strong><small>Vị thế live, partial TP và trailing SL</small></div></div>
        <div class="market-pattern-summary-grid">
          <article class="market-pattern-summary-card"><span>Vị thế mở</span><strong>${escapeHtml(payload.open_count ?? 0)}</strong><small>trade_executions OPEN</small></article>
          <article class="market-pattern-summary-card"><span>Đã chốt 30%</span><strong>${escapeHtml(payload.partial_done_count ?? 0)}</strong><small>partial_take_profit_done</small></article>
          <article class="market-pattern-summary-card"><span>Đang chờ chốt 1 phần</span><strong>${escapeHtml(payload.waiting_partial_count ?? 0)}</strong><small>${escapeHtml(formatMarketRegimeNumber((Number(config.partial_trigger_tp_progress) || 0) * 100))}% tới TP</small></article>
          <article class="market-pattern-summary-card"><span>Trailing</span><strong>${config.enabled ? "Bật" : "Tắt"}</strong><small>ATR ${escapeHtml(config.atr_timeframe || "-")} × ${escapeHtml(config.atr_multiplier ?? "-")}</small></article>
        </div>
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Biểu đồ vị thế</strong><small>Tiến độ tới TP và các mốc giá đang quản lý</small></div></div>
        ${renderTradeExecutionPositionTabs(items)}
      </section>
      ${items.length ? `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Vị thế đang mở</strong><small>Partial chỉ chạy một lần, sau đó chỉ trailing SL</small></div></div>
          <div class="trade-execution-position-grid">
            ${items.map(renderTradeExecutionPositionCard).join("")}
          </div>
        </section>
      ` : `<div class="market-regime-empty">Chưa có vị thế live đang mở.</div>`}
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Lệnh đóng gần nhất</strong><small>TP, SL, trailing stop hoặc reconciled close</small></div></div>
        ${renderTradeExecutionClosedList(closedItems)}
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  bindTradeExecutionTabs();
  bindTradeExecutionClosedPagination();
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function isCapitalManagementModule(module) {
  return Boolean(module?.capital_management && typeof module.capital_management === "object");
}

function capitalManagementPayload(module) {
  return module?.capital_management && typeof module.capital_management === "object" ? module.capital_management : {};
}

function capitalManagementSummaryRows(module, payload) {
  const section = String(payload.section || "");
  const snapshot = payload.snapshot || {};
  const reserve = payload.reserve_state || {};
  const sizing = payload.position_size || {};
  const impact = payload.configuration_impact || {};
  const allocation = payload.allocation_check || {};
  if (section === "capital_sync") {
    return [
      { title: "Nguồn vốn", value: snapshot.source || "OKX", meta: snapshot.quote_currency || "USDT" },
      { title: "Wallet balance", value: `${formatMarketRegimeNumber(snapshot.wallet_balance)}u`, meta: "OKX account" },
      { title: "PnL thả nổi", value: `${formatMarketRegimeNumber(snapshot.unrealized_pnl)}u`, meta: "Không cộng vào vốn thật" },
      { title: "Realized capital", value: `${formatMarketRegimeNumber(snapshot.realized_capital)}u`, meta: "Wallet - unrealized PnL" },
    ];
  }
  if (section === "capital_reserve") {
    return [
      { title: "Mode vốn", value: reserve.mode || "-", meta: allocation.allowed ? "Được cấp vốn" : "Đang chặn/cảnh báo" },
      { title: "Reserve", value: `${formatMarketRegimeNumber(reserve.reserve_amount)}u`, meta: `${formatMarketRegimeNumber(reserve.reserve_percent)}%` },
      { title: "Trading capital", value: `${formatMarketRegimeNumber(reserve.trading_capital)}u`, meta: "Sau khi giữ quỹ dự phòng" },
      { title: "Available", value: `${formatMarketRegimeNumber(reserve.available_trading_capital)}u`, meta: reserve.reason || allocation.reason || "-" },
    ];
  }
  if (section === "position_sizing") {
    return [
      { title: "Mode sizing", value: sizing.mode || "-", meta: `${formatMarketRegimeNumber(sizing.risk_percent)}% risk` },
      { title: "Risk amount", value: `${formatMarketRegimeNumber(sizing.risk_amount)}u`, meta: "Rủi ro tối đa/lệnh" },
      { title: "Suggested size", value: `${formatMarketRegimeNumber(sizing.suggested_order_size)}u`, meta: `${formatMarketRegimeNumber(sizing.required_margin)}u margin` },
      { title: "Kết quả", value: sizing.allowed ? "Được phép" : "Không đủ điều kiện", meta: sizing.reason || "-" },
    ];
  }
  return [
    { title: "Risk level", value: impact.risk_level || "-", meta: impact.is_safe ? "Có thể áp dụng" : "Cần kiểm tra" },
    { title: "Safety score", value: formatMarketRegimeNumber(impact.safety_score), meta: "Thang 0-100" },
    { title: "Max safe size", value: `${formatMarketRegimeNumber(impact.max_safe_order_size)}u`, meta: "Theo vốn hiện tại" },
    { title: "Suggested size", value: `${formatMarketRegimeNumber(impact.suggested_order_size)}u`, meta: "Sau phân tích" },
  ];
}

function renderCapitalManagementSummary(module, payload) {
  const rows = capitalManagementSummaryRows(module, payload);
  return `
    <div class="market-pattern-summary-grid">
      ${rows.map((row) => `
        <article class="market-pattern-summary-card">
          <span>${escapeHtml(row.title)}</span>
          <strong>${escapeHtml(row.value ?? "-")}</strong>
          <small>${escapeHtml(row.meta || "-")}</small>
        </article>
      `).join("")}
    </div>
  `;
}

function capitalManagementChartGroups(payload) {
  const section = String(payload.section || "");
  const snapshot = payload.snapshot || {};
  const reserve = payload.reserve_state || {};
  const sizing = payload.position_size || {};
  const impact = payload.configuration_impact || {};
  if (section === "capital_sync") {
    return [
      {
        id: "capital-sync-balance",
        title: "Vốn từ OKX",
        subtitle: "Realized capital không cộng PnL thả nổi",
        axisLabel: "USDT",
        rows: [
          { label: "Wallet balance", shortLabel: "Wallet", value: snapshot.wallet_balance, unit: "u", color: MODULE_CHART_COLORS[0] },
          { label: "Unrealized PnL", shortLabel: "uPnL", value: Math.abs(Number(snapshot.unrealized_pnl) || 0), unit: "u", color: MODULE_CHART_COLORS[3] },
          { label: "Realized capital", shortLabel: "Realized", value: snapshot.realized_capital, unit: "u", color: MODULE_CHART_COLORS[2] },
          { label: "Available balance", shortLabel: "Avail", value: snapshot.available_balance, unit: "u", color: MODULE_CHART_COLORS[1] },
        ],
      },
    ];
  }
  if (section === "capital_reserve") {
    return [
      {
        id: "capital-reserve-split",
        title: "Phân bổ vốn",
        subtitle: "Reserve capital, trading capital và phần còn trống",
        axisLabel: "USDT",
        rows: [
          { label: "Realized capital", shortLabel: "Realized", value: reserve.realized_capital, unit: "u", color: MODULE_CHART_COLORS[0] },
          { label: "Reserve capital", shortLabel: "Reserve", value: reserve.reserve_amount, unit: "u", color: MODULE_CHART_COLORS[4] },
          { label: "Trading capital", shortLabel: "Trading", value: reserve.trading_capital, unit: "u", color: MODULE_CHART_COLORS[2] },
          { label: "Used trading", shortLabel: "Used", value: reserve.used_trading_capital, unit: "u", color: MODULE_CHART_COLORS[3] },
          { label: "Available", shortLabel: "Avail", value: reserve.available_trading_capital, unit: "u", color: MODULE_CHART_COLORS[1] },
        ],
      },
      {
        id: "capital-reserve-mode",
        title: "Trạng thái cấp vốn",
        subtitle: "Reserve percent và allocation gate hiện tại",
        axisLabel: "%",
        fixedMax: 100,
        rows: [
          { label: "Reserve percent", shortLabel: "Reserve", value: reserve.reserve_percent, unit: "%", color: MODULE_CHART_COLORS[4] },
          { label: "Allocation allowed", shortLabel: "Allowed", value: payload.allocation_check?.allowed ? 100 : 0, unit: "%", color: payload.allocation_check?.allowed ? MODULE_CHART_COLORS[2] : MODULE_CHART_COLORS[3] },
          { label: "Reserve usage", shortLabel: "Use reserve", value: reserve.allow_reserve_usage ? 100 : 0, unit: "%", color: reserve.allow_reserve_usage ? MODULE_CHART_COLORS[3] : MODULE_CHART_COLORS[2] },
        ],
      },
    ];
  }
  if (section === "position_sizing") {
    return [
      {
        id: "position-sizing-cap",
        title: "Giới hạn sizing",
        subtitle: "So sánh theo risk, theo vốn và size đề xuất",
        axisLabel: "USDT",
        rows: [
          { label: "Max by risk", shortLabel: "Risk", value: sizing.max_order_size_by_risk, unit: "u", color: MODULE_CHART_COLORS[0] },
          { label: "Max by capital", shortLabel: "Capital", value: sizing.max_order_size_by_capital, unit: "u", color: MODULE_CHART_COLORS[1] },
          { label: "Suggested order", shortLabel: "Size", value: sizing.suggested_order_size, unit: "u", color: MODULE_CHART_COLORS[2] },
          { label: "Required margin", shortLabel: "Margin", value: sizing.required_margin, unit: "u", color: MODULE_CHART_COLORS[4] },
        ],
      },
      {
        id: "position-sizing-risk",
        title: "Lãi/lỗ mô phỏng",
        subtitle: "Theo SL/TP percent của sizing hiện tại",
        axisLabel: "USDT",
        rows: [
          { label: "Risk amount", shortLabel: "Risk", value: sizing.risk_amount, unit: "u", color: MODULE_CHART_COLORS[3] },
          { label: "Estimated loss", shortLabel: "Loss", value: sizing.estimated_loss, unit: "u", color: MODULE_CHART_COLORS[3] },
          { label: "Estimated profit", shortLabel: "Profit", value: sizing.estimated_profit, unit: "u", color: MODULE_CHART_COLORS[2] },
        ],
      },
    ];
  }
  return [
    {
      id: "configuration-impact-capital",
      title: "Tác động vốn",
      subtitle: "Trước và sau cấu hình đề xuất",
      axisLabel: "USDT",
      rows: [
        { label: "Trading before", shortLabel: "Trade trước", value: impact.trading_capital_before, unit: "u", color: MODULE_CHART_COLORS[0] },
        { label: "Trading after", shortLabel: "Trade sau", value: impact.trading_capital_after, unit: "u", color: MODULE_CHART_COLORS[2] },
        { label: "Required before", shortLabel: "Cần trước", value: impact.required_capital_before, unit: "u", color: MODULE_CHART_COLORS[4] },
        { label: "Required after", shortLabel: "Cần sau", value: impact.required_capital_after, unit: "u", color: MODULE_CHART_COLORS[3] },
      ],
    },
    {
      id: "configuration-impact-score",
      title: "Điểm an toàn",
      subtitle: "Safety score và order size an toàn",
      axisLabel: "Điểm / USDT",
      rows: [
        { label: "Safety score", shortLabel: "Score", value: impact.safety_score, unit: "điểm", color: MODULE_CHART_COLORS[2] },
        { label: "Max safe order", shortLabel: "Max safe", value: impact.max_safe_order_size, unit: "u", color: MODULE_CHART_COLORS[1] },
        { label: "Suggested order", shortLabel: "Suggest", value: impact.suggested_order_size, unit: "u", color: MODULE_CHART_COLORS[0] },
      ],
    },
  ];
}

function renderCapitalSyncSummaryCards(snapshot) {
  const cards = [
    { title: "Số dư ví", value: `${formatMarketRegimeNumber(snapshot.wallet_balance)}u`, meta: "OKX account", tone: "neutral" },
    { title: "PnL thả nổi", value: `${formatMarketRegimeNumber(snapshot.unrealized_pnl)}u`, meta: "Không dùng để tăng vốn", tone: Number(snapshot.unrealized_pnl || 0) < 0 ? "danger" : "success" },
    { title: "Vốn đã chốt", value: `${formatMarketRegimeNumber(snapshot.realized_capital)}u`, meta: "Số dư ví - PnL thả nổi", tone: "success" },
    { title: "Khả dụng", value: `${formatMarketRegimeNumber(snapshot.available_balance)}u`, meta: "Có thể mở lệnh", tone: "neutral" },
  ];
  return `
    <div class="market-pattern-summary-grid capital-sync-summary-grid">
      ${cards.map((card) => `
        <article class="market-pattern-summary-card capital-summary-${card.tone}">
          <span>${escapeHtml(card.title)}</span>
          <strong>${escapeHtml(card.value)}</strong>
          <small>${escapeHtml(card.meta)}</small>
        </article>
      `).join("")}
    </div>
  `;
}

function renderCapitalSyncDetail(module, payload, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const snapshot = payload.snapshot || {};
  const detailRows = [
    { label: "Số dư ví", value: `${formatMarketRegimeNumber(snapshot.wallet_balance)}u`, note: "Số dư OKX hiện tại.", tone: "neutral" },
    { label: "PnL thả nổi", value: `${formatMarketRegimeNumber(snapshot.unrealized_pnl)}u`, note: "Lãi/lỗ của vị thế còn mở, chưa được tính là vốn thật.", tone: Number(snapshot.unrealized_pnl || 0) < 0 ? "danger" : "success" },
    { label: "Vốn đã chốt", value: `${formatMarketRegimeNumber(snapshot.realized_capital)}u`, note: "Dùng làm nền vốn cho các quyết định giao dịch.", tone: "success" },
    { label: "Khả dụng", value: `${formatMarketRegimeNumber(snapshot.available_balance)}u`, note: "Số dư có thể dùng để mở lệnh futures/cross theo OKX.", tone: "neutral" },
  ];
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "-")}</span>
        <h3 id="systemModuleTitle">${escapeHtml(module.name || "-")}</h3>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll capital-sync-clean">
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tổng quan</strong><small>Vốn thật được đồng bộ từ OKX</small></div></div>
        ${renderCapitalSyncSummaryCards(snapshot)}
        <div class="capital-sync-formula">
          <strong>Vốn đã chốt = Số dư ví - PnL thả nổi.</strong>
          <span>PnL chưa đóng không dùng để tăng vốn giao dịch.</span>
        </div>
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Chi tiết vốn</strong><small>Bảng vận hành ngắn gọn</small></div></div>
        <div class="capital-sync-table" role="table" aria-label="Chi tiết Capital Sync">
          <div class="capital-sync-table-head" role="row">
            <span>Chỉ số</span>
            <span>Giá trị</span>
            <span>Ghi chú</span>
          </div>
          ${detailRows.map((row) => `
            <div class="capital-sync-table-row capital-row-${row.tone}" role="row">
              <span>${escapeHtml(row.label)}</span>
              <strong>${escapeHtml(row.value)}</strong>
              <small>${escapeHtml(row.note)}</small>
            </div>
          `).join("")}
        </div>
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function capitalGroupModuleBySection(module, section) {
  return (Array.isArray(module?.modules) ? module.modules : []).find((item) => (
    String(item?.capital_management?.section || "") === section
  )) || null;
}

function renderCapitalGroupAllocation(module) {
  const reserveModule = capitalGroupModuleBySection(module, "capital_reserve");
  const sizingModule = capitalGroupModuleBySection(module, "position_sizing");
  const blocks = [
    reserveModule ? {
      title: "Phân bổ vốn",
      subtitle: "Reserve, trading capital và phần còn dùng được",
      html: renderCapitalManagementSummary(reserveModule, capitalManagementPayload(reserveModule)),
    } : null,
    sizingModule ? {
      title: "Size lệnh kế tiếp",
      subtitle: "Suggested size, margin cần dùng và trạng thái cấp vốn",
      html: renderCapitalManagementSummary(sizingModule, capitalManagementPayload(sizingModule)),
    } : null,
  ].filter(Boolean);
  return blocks.map((block) => `
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>${escapeHtml(block.title)}</strong><small>${escapeHtml(block.subtitle)}</small></div></div>
      ${block.html}
    </section>
  `).join("");
}

function renderCapitalGroupImpact(module) {
  const impactModule = capitalGroupModuleBySection(module, "configuration_impact");
  if (!impactModule) {
    return `
      <section class="market-regime-section">
        <div class="module-chart-empty">Chưa có dữ liệu Tác động cấu hình.</div>
      </section>
    `;
  }
  const payload = capitalManagementPayload(impactModule);
  const charts = capitalManagementChartGroups(payload);
  return `
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>Tác động cấu hình</strong><small>So sánh trước/sau và mức an toàn vốn</small></div></div>
      ${renderCapitalManagementSummary(impactModule, payload)}
    </section>
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>Biểu đồ tác động</strong><small>Chỉ dùng khi cần kiểm tra cấu hình</small></div></div>
      <div class="market-regime-chart-grid market-pattern-chart-grid">
        ${charts.map((chart) => renderMarketPatternBarChart(chart)).join("")}
      </div>
    </section>
  `;
}

function renderCapitalGroupImpactV2(module) {
  const impactModule = capitalGroupModuleBySection(module, "configuration_impact");
  if (!impactModule) {
    return `
      <section class="market-regime-section">
        <div class="module-chart-empty">Chưa có dữ liệu Tác động cấu hình.</div>
      </section>
    `;
  }
  const payload = capitalManagementPayload(impactModule);
  const impact = payload.configuration_impact || {};
  const charts = capitalManagementChartGroups(payload);
  const riskLevel = String(impact.risk_level || "-").toUpperCase();
  const tradingBefore = Number(impact.trading_capital_before || 0);
  const tradingAfter = Number(impact.trading_capital_after || 0);
  const requiredBefore = Number(impact.required_capital_before || 0);
  const requiredAfter = Number(impact.required_capital_after || 0);
  const safetyScore = Number(impact.safety_score || 0);
  const isCritical = riskLevel === "CRITICAL" || requiredAfter > Math.max(tradingAfter, 0);
  const conclusion = isCritical
    ? "Cấu hình đề xuất không an toàn với vốn hiện tại."
    : "Cấu hình đề xuất nằm trong vùng có thể kiểm soát.";
  const reason = requiredAfter > Math.max(tradingAfter, 0)
    ? `Required capital ${formatMarketRegimeNumber(requiredAfter)}u lớn hơn trading capital ${formatMarketRegimeNumber(tradingAfter)}u.`
    : `Safety score hiện tại là ${formatMarketRegimeNumber(safetyScore)}/100.`;
  const recommendation = isCritical
    ? `Không tăng size/cấu hình lúc này. Chỉ giữ size an toàn ${formatMarketRegimeNumber(impact.max_safe_order_size)}u.`
    : `Có thể dùng suggested size ${formatMarketRegimeNumber(impact.suggested_order_size)}u nếu các risk gate khác đều OK.`;
  const rows = [
    ["Trading capital", `${formatMarketRegimeNumber(tradingBefore)}u`, `${formatMarketRegimeNumber(tradingAfter)}u`, "Vốn có thể dùng sau reserve"],
    ["Required capital", `${formatMarketRegimeNumber(requiredBefore)}u`, `${formatMarketRegimeNumber(requiredAfter)}u`, "Vốn cấu hình cần để chạy"],
    ["Safety score", `${formatMarketRegimeNumber(safetyScore)}/100`, `${formatMarketRegimeNumber(safetyScore)}/100`, "Điểm an toàn vốn"],
    ["Max safe size", "-", `${formatMarketRegimeNumber(impact.max_safe_order_size)}u`, "Size tối đa an toàn"],
    ["Suggested size", "-", `${formatMarketRegimeNumber(impact.suggested_order_size)}u`, "Size đề xuất sau phân tích"],
  ];
  return `
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>Kết luận</strong><small>Đọc nhanh trước khi xem biểu đồ</small></div></div>
      <div class="capital-impact-decision ${isCritical ? "critical" : "safe"}">
        <div>
          <span>${escapeHtml(riskLevel)}</span>
          <strong>${escapeHtml(conclusion)}</strong>
          <small>${escapeHtml(reason)}</small>
        </div>
        <p>${escapeHtml(recommendation)}</p>
      </div>
    </section>
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>Trước / Sau</strong><small>Các số chính cần so sánh</small></div></div>
      <div class="capital-impact-table" role="table" aria-label="Tác động cấu hình">
        <div class="capital-impact-table-head" role="row">
          <span>Chỉ số</span>
          <span>Trước</span>
          <span>Sau</span>
          <span>Ý nghĩa</span>
        </div>
        ${rows.map((row) => `
          <div class="capital-impact-table-row ${row[0] === "Required capital" && requiredAfter > tradingAfter ? "danger" : ""}" role="row">
            <span>${escapeHtml(row[0])}</span>
            <strong>${escapeHtml(row[1])}</strong>
            <strong>${escapeHtml(row[2])}</strong>
            <small>${escapeHtml(row[3])}</small>
          </div>
        `).join("")}
      </div>
    </section>
    <section class="market-regime-section">
      <div class="market-regime-section-head"><div><strong>Biểu đồ tác động</strong><small>Phần phụ để kiểm tra trực quan</small></div></div>
      <div class="market-regime-chart-grid market-pattern-chart-grid">
        ${charts.map((chart) => renderMarketPatternBarChart(chart)).join("")}
      </div>
    </section>
  `;
}

function renderCapitalManagementGroupDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const syncModule = capitalGroupModuleBySection(module, "capital_sync");
  const syncPayload = capitalManagementPayload(syncModule || {});
  const snapshot = syncPayload.snapshot || {};
  const activeTab = options.activeTab || state.selectedCapitalManagementTab || "capital";
  state.selectedCapitalManagementTab = activeTab === "impact" ? "impact" : "capital";
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  const tab = state.selectedCapitalManagementTab;
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Capital</span>
        <h3 id="systemModuleTitle">Capital Management</h3>
        <p>Quản lý vốn OKX, phân bổ vốn, size lệnh kế tiếp và tác động cấu hình.</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll capital-sync-clean">
      <div class="trade-execution-tabs capital-management-tabs" role="tablist" aria-label="Capital Management">
        <button type="button" class="trade-execution-tab ${tab === "capital" ? "active" : ""}" data-capital-tab="capital">Vốn</button>
        <button type="button" class="trade-execution-tab ${tab === "impact" ? "active" : ""}" data-capital-tab="impact">Tác động</button>
      </div>
      ${tab === "impact" ? renderCapitalGroupImpactV2(module) : `
        <section class="market-regime-section">
          <div class="market-regime-section-head"><div><strong>Vốn OKX</strong><small>Vốn thật được đồng bộ từ OKX</small></div></div>
          ${renderCapitalSyncSummaryCards(snapshot)}
          <div class="capital-sync-formula">
            <strong>Vốn đã chốt = Số dư ví - PnL thả nổi.</strong>
            <span>PnL chưa đóng không dùng để tăng vốn giao dịch.</span>
          </div>
        </section>
        ${renderCapitalGroupAllocation(module)}
      `}
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  refs.systemModuleDetail.querySelectorAll("[data-capital-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedCapitalManagementTab = button.getAttribute("data-capital-tab") || "capital";
      renderCapitalManagementGroupDetail(module, { activeTab: state.selectedCapitalManagementTab });
    });
  });
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function renderCapitalManagementDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  if (module.capital_management_group) {
    renderCapitalManagementGroupDetail(module, options);
    return;
  }
  const payload = capitalManagementPayload(module);
  if (String(payload.section || "") === "capital_sync") {
    renderCapitalSyncDetail(module, payload, options);
    return;
  }
  const charts = capitalManagementChartGroups(payload);
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "-")}</span>
        <h3 id="systemModuleTitle">${escapeHtml(module.name || "-")}</h3>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tổng quan</strong><small>Biến chính của module quản lý vốn</small></div></div>
        ${renderCapitalManagementSummary(module, payload)}
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Biểu đồ biến</strong><small>Theo thứ tự vận hành của module</small></div></div>
        <div class="market-regime-chart-grid market-pattern-chart-grid">
          ${charts.map((chart) => renderMarketPatternBarChart(chart)).join("")}
        </div>
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Chi tiết biến</strong><small>Dữ liệu backend đang trả cho dashboard</small></div></div>
        ${renderModuleChart(module, moduleDisplayRows(module, Array.isArray(module.stats) ? module.stats : []))}
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function isActivePositionManagerModule(module) {
  return Number(module?.number) === 16 || Boolean(module?.active_position_manager);
}

function activePositionActionLabel(action) {
  const key = String(action || "");
  return {
    HOLD: "Giữ",
    BAD_CUT: "Cắt lỗ chủ động",
    DCA_REVIEW: "Xem xét DCA",
    SCALE_IN_REVIEW: "Xem xét thêm KL",
    GOOD_EXIT_REVIEW: "Chốt lời ngắn",
    BAD_CUT_REMAINDER: "Đóng phần còn lại",
    HOLD_AFTER_PARTIAL: "Giữ sau chốt 30%",
    HOLD_AFTER_LOSS_CUT: "Giữ sau chốt lỗ 25%",
    PROTECT_PROFIT: "Bảo vệ lợi nhuận",
  }[key] || key || "-";
}

function activePositionTone(action) {
  const key = String(action || "");
  if (["BAD_CUT", "BAD_CUT_REMAINDER"].includes(key)) return "bear";
  if (["DCA_REVIEW", "SCALE_IN_REVIEW", "GOOD_EXIT_REVIEW"].includes(key)) return "warn";
  if (key === "PROTECT_PROFIT") return "bull";
  return "unknown";
}

function compactSymbolLabel(symbol) {
  const text = String(symbol || "-").trim();
  if (!text || text === "-") return "-";
  return text.split("/")[0].replace(/[^A-Z0-9]/gi, "") || text;
}

function activePositionExecutionMeta(execution) {
  const reason = String(execution?.reason || "").trim();
  if (execution?.submitted) return { label: "Live", tone: "bull", title: "Đã gửi lệnh OKX" };
  if (execution?.error) return { label: "Lỗi", tone: "bear", title: String(execution.error || "Lỗi OKX") };
  if (reason === "read_only_preview") return { label: "Preview", tone: "warn", title: "Chỉ xem trước, chưa gửi lệnh" };
  if (reason === "not_applied_to_existing_position") return { label: "Skipped", tone: "unknown", title: "Không áp dụng vào vị thế hiện tại" };
  if (reason === "execution_disabled_for_action") return { label: "Off", tone: "unknown", title: "Thực thi đang tắt cho hành động này" };
  return { label: reason || "-", tone: "unknown", title: reason || "-" };
}

function activePositionReasonText(item) {
  const reasons = Array.isArray(item?.reasons) ? item.reasons.filter(Boolean).map(String) : [];
  const full = reasons.join("; ") || "-";
  if (!reasons.length) return { short: "-", full };
  let short = reasons[0];
  short = short
    .replace(/^Lệnh đang âm\s+/i, "Âm ")
    .replace(/trong vùng DCA cho phép/i, "trong vùng DCA")
    .replace(/vượt ngưỡng cắt chủ động/i, "vượt ngưỡng cắt")
    .replace(/Vị thế đã chốt lời 30%;\s*/i, "Đã chốt 30%; ");
  if (short.length > 72) short = `${short.slice(0, 69).trim()}...`;
  return { short, full };
}

function activePositionScanLabel(item) {
  const createdAt = item?.created_at;
  if (!createdAt) return "-";
  const time = timeOnlyLabel(createdAt);
  return time || "-";
}

function renderActivePositionRows(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return `<div class="market-regime-empty compact">Chưa có vị thế mở để review.</div>`;
  return `
    <div class="table-wrap positions-table-wrap active-position-table compact-active-position-table">
      <table>
        <thead>
          <tr>
            <th>Cặp</th>
            <th>Hướng</th>
            <th>Hành động</th>
            <th>R</th>
            <th>PnL</th>
            <th>KL</th>
            <th>Mode</th>
            <th>Lý do</th>
            <th>Scan</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((item) => {
            const symbol = item.symbol || "-";
            const execution = activePositionExecutionMeta(item.execution || {});
            const reason = activePositionReasonText(item);
            const scanLabel = activePositionScanLabel(item);
            return `
            <tr title="${escapeHtml(`${symbol} | ${reason.full}`)}">
              <td><strong title="${escapeHtml(symbol)}">${escapeHtml(compactSymbolLabel(symbol))}</strong><small>ID ${escapeHtml(item.trade_execution_id || "-")}</small></td>
              <td>${escapeHtml(item.side || "-")}</td>
              <td><span class="market-regime-badge ${activePositionTone(item.action)}">${escapeHtml(activePositionActionLabel(item.action))}</span></td>
              <td>${escapeHtml(formatMarketRegimeNumber(item.r_multiple, 2))}R</td>
              <td>${escapeHtml(formatTradeExecutionPnl(item.pnl))}</td>
              <td>${escapeHtml(item.amount == null ? "-" : `${formatMarketRegimeNumber(item.amount)} (${formatMarketRegimeNumber(Number(item.fraction || 0) * 100, 0)}%)`)}</td>
              <td><span class="market-regime-badge ${execution.tone}" title="${escapeHtml(execution.title)}">${escapeHtml(execution.label)}</span></td>
              <td class="active-position-reason" title="${escapeHtml(reason.full)}">${escapeHtml(reason.short)}</td>
              <td title="${escapeHtml(timeLabel(item.created_at))}">${escapeHtml(scanLabel)}</td>
            </tr>
          `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderActivePositionManagerDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const payload = module.active_position_manager && typeof module.active_position_manager === "object" ? module.active_position_manager : {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const recent = Array.isArray(payload.recent_decisions) ? payload.recent_decisions : [];
  const counts = payload.action_counts && typeof payload.action_counts === "object" ? payload.action_counts : {};
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "market-regime-detail");
  refs.systemModuleDetail.innerHTML = `
    <button class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head market-regime-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "16")}</span>
        <h3 id="systemModuleTitle">${escapeHtml(module.name || "Quản lý vị thế chủ động")}</h3>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-chart-scroll market-regime-scroll">
      ${payload.error ? `<div class="market-regime-load-error" role="alert"><strong>Module đang lỗi.</strong><span>${escapeHtml(payload.error)}</span></div>` : ""}
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Tổng quan</strong><small>Shadow mode: ghi log/Telegram, chưa tự can thiệp thật</small></div></div>
        <div class="market-pattern-summary-grid">
          <article class="market-pattern-summary-card"><span>Vị thế review</span><strong>${escapeHtml(payload.open_count ?? 0)}</strong><small>OPEN</small></article>
          <article class="market-pattern-summary-card"><span>Shadow mode</span><strong>${payload.shadow_mode ? "Bật" : "Tắt"}</strong><small>Auto execute: ${payload.auto_execute_enabled ? "Bật" : "Tắt"}</small></article>
          <article class="market-pattern-summary-card"><span>Cắt lỗ/DCA</span><strong>${escapeHtml((counts.BAD_CUT || 0) + (counts.DCA_REVIEW || 0))}</strong><small>Can thiệp rủi ro</small></article>
          <article class="market-pattern-summary-card"><span>Chốt/Scale</span><strong>${escapeHtml((counts.GOOD_EXIT_REVIEW || 0) + (counts.SCALE_IN_REVIEW || 0) + (counts.PROTECT_PROFIT || 0))}</strong><small>Can thiệp lợi nhuận</small></article>
        </div>
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Review hiện tại</strong><small>Mọi hành động đều có lý do, tiền, thời gian</small></div></div>
        ${renderActivePositionRows(items)}
      </section>
      <section class="market-regime-section">
        <div class="market-regime-section-head"><div><strong>Log gần nhất</strong><small>Dùng để phân tích sau khi lệnh thắng/thua</small></div></div>
        ${renderActivePositionRows(recent)}
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function trendHoldCount(payload, key) {
  const value = Number(payload?.[key]);
  return Number.isFinite(value) ? value : 0;
}

function trendHoldItemRows(items, emptyText) {
  const rows = Array.isArray(items) ? items.filter(Boolean).slice(0, 12) : [];
  if (!rows.length) {
    return `<div class="trend-hold-empty">${escapeHtml(emptyText)}</div>`;
  }
  return rows.map((item) => {
    const symbol = item.symbol || item.pair || item.market || "-";
    const side = String(item.side || item.direction || "-").toUpperCase();
    const status = item.status || "-";
    const reason = item.block_reason || item.reason || item.last_block_reason || "-";
    const expires = item.expires_at || item.expire_at || item.ttl_until || item.watchlist_expires_at || "-";
    const cleared = Array.isArray(item.cleared_reasons) ? item.cleared_reasons.join(", ") : (item.block_cleared || item.cleared || "");
    return `
      <article class="trend-hold-row">
        <div>
          <strong>${escapeHtml(symbol)}</strong>
          <span>${escapeHtml(side)} · ${escapeHtml(status)}</span>
        </div>
        <div>
          <small>Block</small>
          <span>${escapeHtml(reason)}</span>
        </div>
        <div>
          <small>Block đã gỡ</small>
          <span>${escapeHtml(cleared || "-")}</span>
        </div>
        <div>
          <small>Hết hạn</small>
          <span>${escapeHtml(timeLabel(expires) || String(expires || "-"))}</span>
        </div>
      </article>
    `;
  }).join("");
}

function renderTrendApprovedHoldDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const payload = module.trend_approved_hold || {};
  const queueCount = trendHoldCount(payload, "queue_count");
  const readyCount = trendHoldCount(payload, "ready_count");
  const priorityCount = trendHoldCount(payload, "priority_rewatch_count");
  const total = queueCount + readyCount + priorityCount;
  const summary = total
    ? `${queueCount} setup đang giữ · ${readyCount} sẵn sàng · ${priorityCount} quay lại watchlist ưu tiên`
    : "Không có setup nào đang bị giữ trong queue";
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.remove("market-regime-detail");
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll", "trend-hold-detail");
  state.selectedSystemModuleKey = systemModuleKey(module);
  refs.systemModuleDetail.innerHTML = `
    <button id="systemModuleClose" class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "-")}</span>
        <div class="module-title-line">
          <h3 id="systemModuleTitle">${escapeHtml(module.name || "-")}</h3>
        </div>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <div class="module-head-actions">
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="trend-hold-summary">
      <div class="trend-hold-main-status">
        <span>Trạng thái hiện tại</span>
        <strong>${escapeHtml(summary)}</strong>
        <small>Nguồn dữ liệu: trend_approved_hold_queue_state</small>
      </div>
      <div class="trend-hold-kpis">
        <article><span>In Queue</span><strong>${queueCount}</strong><small>Đang chờ gỡ block</small></article>
        <article><span>Ready</span><strong>${readyCount}</strong><small>Đã clear block</small></article>
        <article><span>Priority Rewatch</span><strong>${priorityCount}</strong><small>Quay lại watchlist ưu tiên</small></article>
      </div>
      <div class="trend-hold-config">
        <div><span>TTL queue</span><strong>30p</strong></div>
        <div><span>Re-check</span><strong>2p/lần</strong></div>
        <div><span>Gia hạn watchlist</span><strong>+30p</strong></div>
      </div>
    </div>
    <div class="module-chart-scroll trend-hold-scroll">
      <section class="trend-hold-section">
        <div class="market-regime-section-head"><div><strong>Setup đang giữ</strong><small>Cặp đã được Mini approve nhưng Risk/Capital/System còn block</small></div></div>
        ${trendHoldItemRows(payload.items, "Chưa có setup nào trong Approved Hold Queue.")}
      </section>
      <section class="trend-hold-section">
        <div class="market-regime-section-head"><div><strong>Priority rewatch</strong><small>Cặp từ queue quay lại watchlist ưu tiên</small></div></div>
        ${trendHoldItemRows(payload.priority_rewatch_items, "Chưa có setup nào quay lại watchlist ưu tiên.")}
      </section>
    </div>
  `;
  refs.systemModuleDetail.querySelector(".module-close")?.addEventListener("click", closeSystemModuleDetail);
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function renderModuleDetail(module, options = {}) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  if (module.name === "Trend Approved Hold Queue") {
    renderTrendApprovedHoldDetail(module, options);
    return;
  }
  if (module.capital_management_group) {
    renderCapitalManagementGroupDetail(module, options);
    return;
  }
  if (isActivePositionManagerModule(module)) {
    renderActivePositionManagerDetail(module, options);
    return;
  }
  if (isPositionManagementSection(module)) {
    renderPositionManagementSectionDetail(module, options);
    return;
  }
  if (isCapitalManagementModule(module)) {
    renderCapitalManagementDetail(module, options);
    return;
  }
  if (isTradeExecutionModule(module)) {
    renderTradeExecutionDetail(module, options);
    return;
  }
  if (isMarketPatternEngineModule(module)) {
    renderMarketPatternDetailV2(module, options);
    return;
  }
  if (isMarketRegimeModule(module)) {
    renderMarketRegimeDetail(module, options);
    return;
  }
  refs.systemModuleDetail.classList.remove("market-regime-detail");
  state.selectedSystemModuleKey = systemModuleKey(module);
  const allRows = Array.isArray(module.stats) ? module.stats : [];
  const rows = moduleDisplayRows(module, allRows);
  const auxRows = moduleAuxRows(module, allRows);
  const file = module.file || {};
  const fileLabel = file.relative_path || file.file_name || "Chưa có file";
  const runtimeUpdatedLabel = moduleUpdatedLabel(module);
  const changedVariableCount = moduleChangedVariableCount(module, rows);
  const aiRangeToggle = renderSystemModuleAiRangeToggle(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.classList.add("module-detail-chart-scroll");
  refs.systemModuleDetail.innerHTML = `
    <button id="systemModuleClose" class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "-")}</span>
        <div class="module-title-line">
          <h3 id="systemModuleTitle">${escapeHtml(module.name || "-")}</h3>
          ${changedVariableCount > 0 ? `<span class="module-change-count" title="${escapeHtml(String(changedVariableCount))} biáº¿n Ä‘ang thay Ä‘á»•i">${escapeHtml(String(changedVariableCount))}</span>` : ""}
        </div>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <div class="module-head-actions">
        ${aiRangeToggle}
        <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
      </div>
    </div>
    <div class="module-meta">
      <div><span>Ngày đọc</span><strong>${escapeHtml(timeLabel(file.updated_at) || "-")}</strong></div>
      <div><span>File module</span><strong>${escapeHtml(fileLabel)}</strong></div>
      <div><span>Kích thước</span><strong>${file.size_bytes === undefined || file.size_bytes === null ? "-" : bytesLabel(file.size_bytes)}</strong></div>
    </div>
    <div class="module-chart-scroll">
      ${renderModuleChart(module, rows)}
      ${auxRows.length ? `
        <div class="module-chart-meta">
          ${auxRows.map((row) => `<div class="${moduleToneClass(row)}"><span>${escapeHtml(row.label || "-")}</span><strong>${escapeHtml(formatCardValue(row.label, row.value))}</strong></div>`).join("")}
        </div>
      ` : ""}
    </div>
  `;
  const metaBlocks = refs.systemModuleDetail.querySelectorAll(".module-meta > div");
  if (metaBlocks[0]) {
    const metaLabel = metaBlocks[0].querySelector("span");
    const metaValue = metaBlocks[0].querySelector("strong");
    if (metaLabel) metaLabel.textContent = "Cập nhật module";
    if (metaValue) metaValue.textContent = runtimeUpdatedLabel || "-";
  }
  const closeBtn = refs.systemModuleDetail.querySelector(".module-close");
  if (closeBtn) closeBtn.addEventListener("click", closeSystemModuleDetail);
  refs.systemModuleDetail.querySelectorAll(".module-ai-range-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const nextRange = normalizeSystemModuleAiRange(button.getAttribute("data-ai-range"));
      if (nextRange === normalizeSystemModuleAiRange(state.systemModuleAiRange)) return;
      const scrollTop = moduleDetailScrollTop();
      const activeChartIndex = moduleDetailActiveChartIndex();
      state.systemModuleAiRange = nextRange;
      updateSystemModuleAiRangeUi(nextRange, { loading: true });
      const cachedPayload = state.systemChecklistPayloadByRange?.[nextRange];
      if (cachedPayload) {
        renderSystemChecklist(cachedPayload);
        const cachedModule = (cachedPayload.modules || []).find((item) => systemModuleKey(item) === state.selectedSystemModuleKey);
        if (cachedModule) renderModuleDetail(cachedModule, { scrollTop, activeChartIndex });
        updateSystemModuleAiRangeUi(nextRange, { loading: true });
      }
      loadSystemChecklist("", { aiRange: nextRange })
        .catch((err) => setStatus(`Lỗi tải phạm vi AI: ${err.message}`))
        .finally(() => updateSystemModuleAiRangeUi(state.systemModuleAiRange, { loading: false }));
    });
  });
  bindModuleChartInteractions();
  if (options.activeChartIndex !== null && options.activeChartIndex !== undefined) {
    const activeItem = refs.systemModuleDetail.querySelector(`.module-chart-legend-item[data-chart-index="${options.activeChartIndex}"]`);
    if (activeItem) activeItem.click();
  }
  if (Number.isFinite(Number(options.scrollTop))) {
    const scrollNode = refs.systemModuleDetail.querySelector(".module-chart-scroll");
    if (scrollNode) requestAnimationFrame(() => {
      scrollNode.scrollTop = Number(options.scrollTop);
    });
  }
}

function groupedSystemModules(modules) {
  const sourceRows = Array.isArray(modules) ? modules : [];
  const dataUpdateInterval = systemDataUpdateIntervalLabel();
  const dataUpdateSchedule = systemDataUpdateScheduleText();
  const eventScheduleByName = {
    "Bộ nhớ quyết định AI": { event: "Ghi nhớ sau mỗi quyết định", schedule: "Sau mỗi lệnh đóng", interval: "lệnh đóng" },
    "Market Regime": { event: "Theo snapshot data hệ thống", schedule: dataUpdateSchedule, interval: dataUpdateInterval },
    "Market Structure & Pattern Engine": { event: "Khi scanner hoặc final re-check gửi OHLCV", schedule: "Theo request analyze/recheck", interval: "sự kiện" },
    "Strategy Versioning": { event: "Ghi nhớ sau mỗi quyết định", schedule: "6h sáng", interval: "6h sáng" },
    "Replay Engine": { event: "Ghi nhớ sau mỗi quyết định", schedule: "6h sáng", interval: "6h sáng" },
    "Bunny Minimize Losses": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
    "Trade Execution & Position Management": { event: "Sau khi mở lệnh, partial close hoặc amend SL/TP", schedule: "Theo runtime/trailing stop", interval: "1 phút" },
    "Thực thi giao dịch & Quản lý vị thế": { event: "Sau khi mở lệnh, partial close hoặc amend SL/TP", schedule: "Theo runtime/trailing stop", interval: "1 phút" },
    "Bunny Health Monitor": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
    "Recovery Chain Manager": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
    "Trend Approved Hold Queue": { event: "Khi setup được AI duyệt nhưng còn bị block", schedule: "Re-check 2 phút/lần", interval: "2 phút" },
    "Prompt Caching": { event: "Ghi token mỗi request", schedule: "Tổng hợp 6h sáng", interval: "6h sáng" },
  };
  const realModules = new Map(
    sourceRows.map((module) => {
      const name = String(module.name || "").trim();
      const schedule = eventScheduleByName[name] || {};
      return [
        name,
        {
          ...module,
          update_event: module.update_event || schedule.event || "Event-driven",
          update_schedule: module.update_schedule || schedule.schedule || "5 phút/lần",
          update_interval: module.update_interval || schedule.interval || "5 phút",
        },
      ];
    }),
  );
  const capitalModules = [
    realModules.get("Capital Sync"),
    realModules.get("Capital Reserve"),
    realModules.get("Position Sizing"),
    realModules.get("Configuration Impact"),
  ].filter(Boolean);
  const capitalManagementModule = capitalModules.length ? {
    number: "Capital",
    name: "Capital Management",
    purpose: "Quản lý vốn OKX, phân bổ vốn, size lệnh kế tiếp và tác động cấu hình.",
    status: capitalModules.some((module) => module.status !== "ok") ? "warn" : "ok",
    update_event: "Sau nạp/rút, sau lệnh đóng",
    update_schedule: "5 phút/lần đồng bộ OKX",
    update_interval: "5 phút",
    capital_management_group: true,
    modules: capitalModules,
    stats: capitalModules.flatMap((module) => Array.isArray(module.stats) ? module.stats : []),
  } : null;
  const tradeExecutionModule = realModules.get("Thá»±c thi giao dá»‹ch & Quáº£n lÃ½ vá»‹ tháº¿") || realModules.get("Trade Execution & Position Management") || positionManagementBaseModule(sourceRows);
  const activePositionManagerModule = sourceRows.find((module) => isActivePositionManagerModule(module));
  const positionManagementModules = [
    ...positionManagementSections(tradeExecutionModule),
    activePositionManagerModule,
    realModules.get("Trend Approved Hold Queue"),
  ].filter(Boolean);
  return [
    {
      key: "ai-decision",
      icon: "🧠",
      title: "AI Decision",
      subtitle_vi: "Ra quyết định AI",
      event_text: "Ghi nhớ sau mỗi quyết định",
      schedule_text: `Market Regime ${dataUpdateInterval}, Replay & Strategy lúc 6h sáng`,
      items: [
        realModules.get("Bộ nhớ quyết định AI"),
        realModules.get("Market Regime"),
        realModules.get("Strategy Versioning"),
        realModules.get("Replay Engine"),
      ].filter(Boolean),
    },
    {
      key: "market-structure",
      icon: "📈",
      title: "Market Structure",
      subtitle_vi: "Cấu trúc thị trường & mô hình giá",
      event_text: "Khi scanner hoặc final re-check gửi OHLCV",
      schedule_text: "Theo request analyze/recheck",
      items: [
        realModules.get("Market Structure & Pattern Engine"),
      ].filter(Boolean),
    },
    {
      key: "risk-management",
      icon: "🛡",
      title: "Risk Management",
      subtitle_vi: "Quản lý rủi ro",
      event_text: "Ngay khi lệnh đóng",
      schedule_text: "5 phút/lần để đối chiếu",
      items: [
        realModules.get("Bunny Minimize Losses"),
        realModules.get("Bunny Health Monitor"),
        realModules.get("Recovery Chain Manager"),
      ].filter(Boolean),
    },
    {
      key: "position-management",
      icon: "📌",
      title: "Quản lý vị thế",
      subtitle_vi: "Thực thi lệnh và vòng đời vị thế",
      event_text: "Sau khi tạo setup, gửi lệnh, fill, amend TP/SL hoặc đóng vị thế",
      schedule_text: "Theo runtime/trailing stop và đồng bộ OKX",
      items: positionManagementModules,
    },
    {
      key: "capital-management",
      icon: "💰",
      title: "Capital Management",
      subtitle_vi: "Quản lý vốn",
      event_text: "Sau nạp/rút, sau lệnh đóng",
      schedule_text: "5 phút/lần đồng bộ OKX",
      items: [
        capitalManagementModule,
      ].filter(Boolean),
    },
    {
      key: "ai-optimization",
      icon: "⚙️",
      title: "AI Optimization",
      subtitle_vi: "Tối ưu AI",
      event_text: "Ghi token mỗi request",
      schedule_text: "Tổng hợp 6h sáng",
      items: [
        realModules.get("Prompt Caching"),
      ].filter(Boolean),
    },
  ].map((group) => ({
    ...group,
    items: group.items.map((item, index) => ({
      ...item,
      group_title: group.title,
      group_icon: group.icon,
      group_order: index + 1,
    })),
  }));
}

function renderSystemModules(modules) {
  const groups = groupedSystemModules(modules);
  const totalModules = groups.reduce((sum, group) => sum + group.items.length, 0);
  if (refs.systemModuleStatus) refs.systemModuleStatus.textContent = `${totalModules} module theo nhóm`;
  if (!refs.systemModuleGrid) return;
  const collapsedGroupKeys = moduleGroupCollapsedKeys();
  const selectedModuleKey = state.selectedSystemModuleKey;
  const detailWasOpen = Boolean(selectedModuleKey && refs.systemModuleOverlay && !refs.systemModuleOverlay.hidden);
  const detailScrollTop = detailWasOpen ? moduleDetailScrollTop() : null;
  const activeChartIndex = detailWasOpen ? moduleDetailActiveChartIndex() : null;
  let refreshedSelectedModule = null;
  const fragment = document.createDocumentFragment();
  groups.forEach((group) => {
    const section = document.createElement("section");
    section.className = "module-group module-group-card";
    section.dataset.groupKey = group.key;
    const isCollapsed = collapsedGroupKeys.has(group.key);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "module-group-toggle";
    toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
    toggle.innerHTML = `
      <div class="module-group-head">
        <div class="module-group-title-block">
          <strong>${escapeHtml(group.icon)} ${escapeHtml(group.title)}</strong>
          <small class="module-group-subtitle">${escapeHtml(group.subtitle_vi || "")}</small>
        </div>
        <span>${group.items.length}</span>
      </div>
      <div class="module-group-schedule">
        <span><b>Khi có sự kiện:</b> ${escapeHtml(group.event_text || "-")}</span>
        <span><b>Theo lịch:</b> ${escapeHtml(group.schedule_text || "-")}</span>
      </div>
    `;

    const body = document.createElement("div");
    body.className = "module-group-body module-card-body";
    body.hidden = isCollapsed;

    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", expanded ? "false" : "true");
      body.hidden = expanded;
    });

    group.items.forEach((module) => {
      const updateIntervalLabel = normalizeVietnameseIntervalLabel(module.update_interval || module.update_schedule || module.update_event || "-");
      const runtimeUpdatedLabel = moduleUpdatedLabel(module);
      const displayName = marketRegimeModuleName(module);
      const displaySubtitle = marketRegimeModuleSubtitle(module);
      const displaySubtitleHtml = displaySubtitle
        ? displaySubtitle.split("\n").filter(Boolean).map((line) => {
            const isJournalLabel = isPositionManagementSection(module)
              && module.position_management_section === "profit_protection"
              && line.trim() === "Nhật ký";
            const isOrderLabel = isPositionManagementSection(module)
              && module.position_management_section === "overview"
              && line.trim() === "Order";
            return `<small class="module-localized-name ${isJournalLabel || isOrderLabel ? "module-secondary-label" : ""}">${escapeHtml(line)}</small>`;
          }).join("")
        : "";
      const attentionRows = Array.isArray(module.stats) ? module.stats.filter((row) => row && row.attention) : [];
      const displayRows = moduleDisplayRows(module, Array.isArray(module.stats) ? module.stats : []);
      const changedVariableCount = moduleChangedVariableCount(module, displayRows);
      const attentionLabel = attentionRows.length
        ? `${attentionRows.length} biến cần chú ý`
        : module.status === "warn" ? "Cần kiểm tra" : "Ổn";
      const card = document.createElement("button");
      card.type = "button";
      card.className = `module-item module-card ${module.status || "warn"}`;
      const cardModuleKey = systemModuleKey(module);
      if (cardModuleKey === selectedModuleKey) {
        card.classList.add("selected");
        refreshedSelectedModule = module;
      }
      card.innerHTML = `
        <div class="module-item-head">
          <span class="module-number">#${escapeHtml(module.group_order || "-")}</span>
          <span class="module-card-status-group">
            <span class="module-row-attention ${attentionRows.length || module.status === "warn" ? "warn" : "ok"}">${escapeHtml(attentionLabel)}</span>
            <span class="module-item-status ${module.status || "warn"}">${moduleStatusLabel(module.status)}</span>
          </span>
        </div>
        <div class="module-item-main">
          <div class="module-title-line">
            <strong>${escapeHtml(displayName)}</strong>
            ${changedVariableCount > 0 ? `<span class="module-change-count" title="${escapeHtml(String(changedVariableCount))} biáº¿n Ä‘ang thay Ä‘á»•i">${escapeHtml(String(changedVariableCount))}</span>` : ""}
          </div>
          ${displaySubtitleHtml}
          <small>${escapeHtml(module.purpose || "-")}</small>
          <span class="module-update-note"><b>Thời gian cập nhật:</b> <span class="module-update-value">${escapeHtml(runtimeUpdatedLabel || "-")}</span><em class="module-update-badge">mỗi ${escapeHtml(updateIntervalLabel)}/1 lần</em></span>
        </div>
      `;
      card.addEventListener("click", () => {
        refs.systemModuleGrid.querySelectorAll(".module-item").forEach((item) => item.classList.remove("selected"));
        card.classList.add("selected");
        state.selectedSystemModuleKey = cardModuleKey;
        renderModuleDetail(module);
      });
      body.appendChild(card);
    });

    section.appendChild(toggle);
    section.appendChild(body);
    fragment.appendChild(section);
  });
  refs.systemModuleGrid.replaceChildren(fragment);
  if (detailWasOpen) {
    if (refreshedSelectedModule) {
      renderModuleDetail(refreshedSelectedModule, { scrollTop: detailScrollTop, activeChartIndex });
    } else {
      closeSystemModuleDetail();
    }
  } else {
    closeSystemModuleDetail();
  }
}

function renderSystemChecklist(payload) {
  const data = payload || {};
  const items = data.criteria || data.items || [];
  if (refs.systemChecklistStatus) {
    refs.systemChecklistStatus.textContent = `${data.ok_count || 0}/${data.total || items.length} OK - ${data.date || "-"}`;
  }
  if (refs.systemChecklistGrid) {
    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `health-item ${item.status || "fail"}`;
      card.setAttribute("aria-expanded", "false");
      card.innerHTML = `
        <div class="health-item-head">
          <strong>${escapeHtml(item.name || "-")}</strong>
          <span>${statusLabel(item.status)}</span>
        </div>
        <small>${escapeHtml(item.detail || "-")}</small>
        <div class="health-target">Mục tiêu: ${escapeHtml(item.target || "✅")}</div>
        <div class="health-evidence" hidden>
          ${renderEvidenceLines(item.evidence)}
        </div>
      `;
      card.addEventListener("click", () => {
        refs.systemChecklistGrid.querySelectorAll(".health-item").forEach((node) => node.classList.remove("selected"));
        card.classList.add("selected");
        renderHealthCriterionDetail(item);
      });
      fragment.appendChild(card);
    });
    refs.systemChecklistGrid.replaceChildren(fragment);
  }
  const modules = (Array.isArray(data.modules) ? data.modules : []).map((module) => (
    isMarketRegimeModule(module)
      ? { ...module, market_regime: data.market_regime && typeof data.market_regime === "object" ? data.market_regime : {} }
      : module
  ));
  renderSystemModules(modules);
  const storage = data.storage || {};
  const disk = storage.disk || {};
  const files = storage.files || {};
  const counts = storage.row_counts || {};
  const payloadBytes = storage.payload_bytes || {};
  if (refs.storageSummary) {
    refs.storageSummary.innerHTML = `
      <div>Disk free: <strong>${fmt(disk.free_percent, 2)}%</strong> (${bytesLabel(disk.free_bytes)})</div>
      <div>DB: <strong>${bytesLabel(files.db?.bytes)}</strong> | WAL: <strong>${bytesLabel(files.wal?.bytes)}</strong></div>
      <div>market_scan_observations: <strong>${fmt(counts.market_scan_observations, 0)}</strong> (${bytesLabel(payloadBytes.market_scan_observations)})</div>
      <div>decisions: <strong>${fmt(counts.decisions, 0)}</strong> (${bytesLabel(payloadBytes.decisions)})</div>
      <div>pending_orders: <strong>${fmt(counts.pending_orders, 0)}</strong> | trade_executions: <strong>${fmt(counts.trade_executions, 0)}</strong></div>
    `;
  }
  if (refs.scanMemorySummary) {
    const rows = storage.market_scan_by_timeframe || [];
    refs.scanMemorySummary.innerHTML = rows.length
      ? rows.map((row) => `<div><strong>${escapeHtml(row.timeframe || "-")}</strong>: ${fmt(row.rows, 0)} dòng / ${fmt(row.symbols, 0)} cặp, mới nhất ${timeLabel(row.latest_at)}</div>`).join("")
      : "Chưa có scan memory.";
  }
}

function sideBadgeClass(side) {
  const value = String(side || "").toLowerCase();
  if (value === "long") return "long";
  if (value === "short") return "short";
  return "";
}

function lcSourceLabel(row) {
  if (!row) return "-";
  if (row.revived_at) return row.revived_label ? `HS ${row.revived_label}` : "HS";
  const slot = String(row.source_slot || row.state || "-");
  const index = row.source_index;
  let sourceClock = "";
  if (row.source_label) {
    const parts = String(row.source_label).trim().split(" ");
    sourceClock = parts.length ? parts[parts.length - 1] : "";
  }
  if (!sourceClock && row.source_time) {
    const stamp = timeLabel(row.source_time);
    sourceClock = stamp ? stamp.split(" ").pop() : "";
  }
  return index ? `${slot} #${index}${sourceClock ? ` (${sourceClock})` : ""}` : slot;
}

function lcWinText(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${fmt(value, 2)}%`;
}

function lcRowMeta(row) {
  const scanTime = timeLabel(row?.source_time || row?.last_seen_at || row?.first_seen_at);
  const source = lcSourceLabel(row);
  const age = row?.age_label || "-";
  return `${scanTime} | ${source} | sống ${escapeHtml(age)}`;
}

function lcCompactSection(title, rows, emptyText = "Chưa có dữ liệu.") {
  return `
    <div class="lc-pipeline-section">
      <div class="lc-pipeline-title">${escapeHtml(title)} <span>${fmt(rows.length, 0)}</span></div>
      ${rows.length ? rows.map((row, index) => `
        <div class="lc-pipeline-row">
          <strong>${index + 1}. ${escapeHtml(row.symbol || "-")}</strong>
          <span class="side ${sideBadgeClass(row.side)}">${escapeHtml(String(row.side || "-").toUpperCase())}</span>
          <span>Win ${lcWinText(row.win_probability_pct)}</span>
          <small>${lcRowMeta(row)}</small>
        </div>
      `).join("") : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`}
    </div>
  `;
}

function lcTelegramSection(rows, emptyText = "Chưa có LC nội bộ.") {
  return rows.length ? rows.map((row, index) => `
    <div class="lc-telegram-row">
      <strong>${index + 1}. ${escapeHtml(row.symbol || "-")} | ${escapeHtml(String(row.side || "-").toUpperCase())} | Win ${lcWinText(row.win_probability_pct)}</strong>
      <small>${lcRowMeta(row)}</small>
    </div>
  `).join("") : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`;
}

function renderLcPipeline(payload) {
  const data = payload || {};
  const counts = data.counts || {};
  const settings = data.settings || {};
  if (refs.lcPipelineSummary) {
    refs.lcPipelineSummary.innerHTML = `
      <div>Chưa Duyệt: <strong>${fmt(counts.undecided, 0)}</strong></div>
      <div>Cắt lọc: bỏ ${fmt(settings.undecided_prune_drop, 0)} win rate thấp nhất khi vượt ${fmt(settings.undecided_prune_floor, 0)}</div>
      <div>LC nội bộ: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Recheck: đúng <strong>mốc cố định ${fmt(settings.recheck_interval_minutes, 0)} phút</strong> · không sớm/trễ · Promote sau <strong>${fmt(settings.promote_after_hours, 1)}h</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }
  if (!refs.lcPipelineRows) return;
  const undecided = Array.isArray(data.undecided) ? data.undecided : [];
  const internalLc = Array.isArray(data.internal_lc) ? data.internal_lc : [];
  const section = (title, rows) => `
    <div class="lc-pipeline-section">
      <div class="lc-pipeline-title">${escapeHtml(title)} <span>${fmt(rows.length, 0)}</span></div>
      ${rows.length ? rows.slice(0, 6).map((row, index) => `
        <div class="lc-pipeline-row">
          <strong>${index + 1}. ${escapeHtml(row.symbol || "-")}</strong>
          <span class="side ${sideBadgeClass(row.side)}">${escapeHtml(String(row.side || "-").toUpperCase())}</span>
          <span>Win ${row.win_probability_pct === null || row.win_probability_pct === undefined ? "-" : `${fmt(row.win_probability_pct, 2)}%`}</span>
          <small>${timeLabel(row.last_seen_at || row.first_seen_at)} · sống ${escapeHtml(row.age_label || "-")}</small>
        </div>
      `).join("") : '<div class="lc-pipeline-empty">Chưa có dữ liệu.</div>'}
    </div>
  `;
  refs.lcPipelineRows.innerHTML = section("Chưa Duyệt", undecided) + section("LC nội bộ", internalLc);
  normalizeVietnameseUi(refs.lcPipelineRows);
}

function renderLcPipelineEnhanced(payload) {
  const data = payload || {};
  const counts = data.counts || {};
  const settings = data.settings || {};
  const undecided = (Array.isArray(data.undecided) ? data.undecided : []).slice(0, Math.max(1, Number(settings.undecided_max || 6)));
  const internalLc = Array.isArray(data.internal_lc) ? data.internal_lc : [];

  if (refs.lcPipelineSummary) {
    refs.lcPipelineSummary.innerHTML = `
      <div>Chưa Duyệt: <strong>${fmt(counts.undecided, 0)}</strong> / tối đa ${fmt(settings.undecided_max, 0)}</div>
      <div>Cắt lọc: bỏ ${fmt(settings.undecided_prune_drop, 0)} win rate thấp nhất khi vượt ${fmt(settings.undecided_prune_floor, 0)}</div>
      <div>LC nội bộ: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Recheck: đúng <strong>mốc cố định ${fmt(settings.recheck_interval_minutes, 0)} phút</strong> · không sớm/trễ · Promote sau <strong>${fmt(settings.promote_after_hours, 1)}h</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcPipelineRows) {
    refs.lcPipelineRows.innerHTML = lcCompactSection("Chưa Duyệt", undecided, "Chưa có cặp Chưa Duyệt.");
    normalizeVietnameseUi(refs.lcPipelineRows);
  }

  if (refs.lcInternalSummary) {
    refs.lcInternalSummary.innerHTML = `
      <div>Tổng LC: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcInternalRows) {
    refs.lcInternalRows.innerHTML = lcTelegramSection(internalLc);
    normalizeVietnameseUi(refs.lcInternalRows);
  }
}

// Override text rendering with clean Vietnamese strings for LC and pulse feed.
function lcRowMeta(row) {
  const scanTime = timeLabel(row?.source_time || row?.last_seen_at || row?.first_seen_at);
  const source = lcSourceLabel(row);
  const age = row?.age_label || "-";
  return `${scanTime} | ${source} | sống ${escapeHtml(age)}`;
}

function lcCompactSection(title, rows, emptyText = "Chưa có dữ liệu.") {
  return `
    <div class="lc-pipeline-section">
      <div class="lc-pipeline-title">${escapeHtml(title)} <span>${fmt(rows.length, 0)}</span></div>
      ${rows.length ? rows.map((row, index) => `
        <div class="lc-pipeline-row">
          <strong>${index + 1}. ${escapeHtml(row.symbol || "-")}</strong>
          <span class="side ${sideBadgeClass(row.side)}">${escapeHtml(String(row.side || "-").toUpperCase())}</span>
          <span>Win ${lcWinText(row.win_probability_pct)}</span>
          <small>${lcRowMeta(row)}</small>
        </div>
      `).join("") : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`}
    </div>
  `;
}

function lcTelegramSection(rows, emptyText = "Chưa có LC nội bộ.") {
  return rows.length ? rows.map((row, index) => `
    <div class="lc-telegram-row">
      <strong>${index + 1}. ${escapeHtml(row.symbol || "-")} | ${escapeHtml(String(row.side || "-").toUpperCase())} | Win ${lcWinText(row.win_probability_pct)}</strong>
      <small>${lcRowMeta(row)}</small>
    </div>
  `).join("") : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`;
}

function lcAiWatchSection(rows) {
  return `
    <div class="lc-pipeline-section">
      <div class="lc-pipeline-title">AI review / NO_TRADE <span>${fmt(rows.length, 0)}</span></div>
      ${rows.length ? `
        <div class="lc-pipeline-scroll">
          ${rows.map((row, index) => {
            const result = String(row.result || "-").toUpperCase();
            const flow = String(row.flow || "-").toUpperCase();
            const side = row.side ? String(row.side).toUpperCase() : "-";
            const metrics = [
              row.win_probability_pct === null || row.win_probability_pct === undefined ? null : `Win ${lcWinText(row.win_probability_pct)}`,
              row.confidence === null || row.confidence === undefined ? null : `Conf ${fmt(row.confidence, 2)}`,
            ].filter(Boolean).join(" | ");
            return `
              <div class="lc-pipeline-row">
                <strong>${index + 1}. ${escapeHtml(row.symbol || "-")}</strong>
                <span class="side ${sideBadgeClass(row.side)}">${escapeHtml(side)}</span>
                <span>${escapeHtml(result)} · ${escapeHtml(flow)}</span>
                <small>${timeLabel(row.created_at)}${metrics ? ` | ${escapeHtml(metrics)}` : ""} | ${escapeHtml(row.reason || "-")}</small>
              </div>
            `;
          }).join("")}
        </div>
      ` : `<div class="lc-pipeline-empty">Chưa có cặp AI review hoặc NO_TRADE.</div>`}
    </div>
  `;
}

function renderLcPipelineEnhanced(payload) {
  const data = payload || {};
  const counts = data.counts || {};
  const settings = data.settings || {};
  const undecided = (Array.isArray(data.undecided) ? data.undecided : []).slice(0, Math.max(1, Number(settings.undecided_max || 6)));
  const internalLc = Array.isArray(data.internal_lc) ? data.internal_lc : [];
  const aiWatch = Array.isArray(data.ai_watch) ? data.ai_watch : [];

  if (refs.lcPipelineSummary) {
    refs.lcPipelineSummary.innerHTML = `
      <div>Chưa Duyệt: <strong>${fmt(counts.undecided, 0)}</strong> / tối đa ${fmt(settings.undecided_max, 0)}</div>
      <div>Cắt lọc: bỏ ${fmt(settings.undecided_prune_drop, 0)} win rate thấp nhất khi vượt ${fmt(settings.undecided_prune_floor, 0)}</div>
      <div>LC nội bộ: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Recheck: đúng <strong>mốc cố định ${fmt(settings.recheck_interval_minutes, 0)} phút</strong> · không sớm/trễ · Promote sau <strong>${fmt(settings.promote_after_hours, 1)}h</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcPipelineRows) {
    refs.lcPipelineRows.innerHTML = lcCompactSection("Chưa Duyệt", undecided, "Chưa có cặp Chưa Duyệt.") + lcAiWatchSection(aiWatch);
    normalizeVietnameseUi(refs.lcPipelineRows);
  }

  if (refs.lcInternalSummary) {
    refs.lcInternalSummary.innerHTML = `
      <div>Tổng LC: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcInternalRows) {
    refs.lcInternalRows.innerHTML = lcTelegramSection(internalLc);
    normalizeVietnameseUi(refs.lcInternalRows);
  }
}

// Final LC UI override: keep exactly 3 visible rows, rest scroll below.
function lcRowMeta(row) {
  const scanTime = timeLabel(row?.source_time || row?.last_seen_at || row?.first_seen_at);
  const source = lcSourceLabel(row);
  const age = row?.age_label || "-";
  return `${scanTime} | ${source} | sống ${escapeHtml(age)}`;
}

function lcCompactSection(title, rows, emptyText = "Chưa có dữ liệu.") {
  return `
    <div class="lc-pipeline-section">
      <div class="lc-pipeline-title">${escapeHtml(title)} <span>${fmt(rows.length, 0)}</span></div>
      ${rows.length ? `
        <div class="lc-pipeline-scroll">
          ${rows.map((row, index) => `
            <div class="lc-pipeline-row">
              <strong>${index + 1}. ${escapeHtml(row.symbol || "-")}</strong>
              <span class="side ${sideBadgeClass(row.side)}">${escapeHtml(String(row.side || "-").toUpperCase())}</span>
              <span>Win ${lcWinText(row.win_probability_pct)}</span>
              <small>${lcRowMeta(row)}</small>
            </div>
          `).join("")}
        </div>
      ` : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`}
    </div>
  `;
}

function lcTelegramSection(rows, emptyText = "Chưa có LC nội bộ.") {
  return rows.length ? rows.map((row, index) => `
    <div class="lc-telegram-row">
      <strong>${index + 1}. ${escapeHtml(row.symbol || "-")} | ${escapeHtml(String(row.side || "-").toUpperCase())} | Win ${lcWinText(row.win_probability_pct)}</strong>
      <small>${lcRowMeta(row)}</small>
    </div>
  `).join("") : `<div class="lc-pipeline-empty">${escapeHtml(emptyText)}</div>`;
}

function renderLcPipelineEnhanced(payload) {
  const data = payload || {};
  const counts = data.counts || {};
  const settings = data.settings || {};
  const undecided = (Array.isArray(data.undecided) ? data.undecided : []).slice(0, Math.max(1, Number(settings.undecided_max || 6)));
  const internalLc = Array.isArray(data.internal_lc) ? data.internal_lc : [];
  const aiWatch = Array.isArray(data.ai_watch) ? data.ai_watch : [];

  if (refs.lcPipelineSummary) {
    refs.lcPipelineSummary.innerHTML = `
      <div>Chưa Duyệt: <strong>${fmt(counts.undecided, 0)}</strong> / tối đa ${fmt(settings.undecided_max, 0)}</div>
      <div>Cắt lọc: bỏ ${fmt(settings.undecided_prune_drop, 0)} win rate thấp nhất khi vượt ${fmt(settings.undecided_prune_floor, 0)}</div>
      <div>LC nội bộ: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Recheck: đúng <strong>mốc cố định ${fmt(settings.recheck_interval_minutes, 0)} phút</strong> · không sớm/trễ · Promote sau <strong>${fmt(settings.promote_after_hours, 1)}h</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcPipelineRows) {
    refs.lcPipelineRows.innerHTML = lcCompactSection("Chưa Duyệt", undecided, "Chưa có cặp Chưa Duyệt.") + lcAiWatchSection(aiWatch);
    normalizeVietnameseUi(refs.lcPipelineRows);
  }

  if (refs.lcInternalSummary) {
    refs.lcInternalSummary.innerHTML = `
      <div>Tổng LC: <strong>${fmt(counts.internal_lc, 0)}</strong> / tối đa ${fmt(settings.internal_lc_max, 0)}</div>
      <div>Tổng hợp 2h hôm nay: <strong>${fmt(data.daily_two_hour_counter, 0)}</strong></div>
      <div>Lần 2h gần nhất: ${timeLabel(data.last_two_hour_slot)}</div>
    `;
  }

  if (refs.lcInternalRows) {
    refs.lcInternalRows.innerHTML = lcTelegramSection(internalLc);
    normalizeVietnameseUi(refs.lcInternalRows);
  }
}

function movementText(delta) {
  if (delta > 0) return "TĂNG";
  if (delta < 0) return "GIẢM";
  return "KHÔNG ĐỔI";
}

function appendPulseLog({ symbol, last, previous, createdAt }) {
  if (!previous || !Number.isFinite(last) || !Number.isFinite(previous)) return;
  const delta = last - previous;
  const deltaPct = previous ? (delta / previous) * 100 : 0;
  const cls = movementClass(delta);
  const node = document.createElement("div");
  node.className = `pulse-feed-item ${cls}`;
  node.innerHTML = `
    <span>${new Date(createdAt).toLocaleTimeString("en-US")}</span>
    <div>${escapeHtml(symbol)} ${movementText(delta)} từ ${fmt(previous, 2)} đến ${fmt(last, 2)}</div>
    <strong>${signed(delta, 2)} (${signed(deltaPct, 3)}%)</strong>
  `;
  refs.pulseFeed.prepend(node);
  while (refs.pulseFeed.children.length > 12) refs.pulseFeed.removeChild(refs.pulseFeed.lastElementChild);
}

function renderPricePulse(payload) {
  const prices = payload.prices || [];
  prices.forEach((item) => {
    const last = nullableNumber(item.last);
    if (last !== null) state.currentPrices.set(item.symbol, last);
  });

  const viewCandidate = state.currentDecision ? selectedCandidate(state.currentDecision) : null;
  const focusSymbol = viewCandidate?.symbol || payload.focus?.symbol || prices[0]?.symbol;
  const focus = prices.find((item) => item.symbol === focusSymbol) || prices[0];
  const warnings = payload.warnings || [];
  const sourceLabel = payload.cached ? "Cache" : warnings.length ? "Cảnh báo giá" : "Mới 60 giây";
  refs.pulseUpdated.textContent = `${sourceLabel} - ${timeLabel(payload.created_at)}`;

  if (!focus) {
    refs.pulseSymbol.textContent = "-";
    refs.pulseMove.textContent = warnings[0] || "Không có dữ liệu giá";
    refs.pulseMove.className = "flat";
    return;
  }

  const current = nullableNumber(focus.last);
  if (current === null) {
    refs.pulseSymbol.textContent = focus.symbol || "-";
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = focus.error || warnings[0] || "Không có dữ liệu giá";
    return;
  }

  if (focus.stale) {
    refs.pulseSymbol.textContent = `${focus.symbol} ${fmt(current, 2)}`;
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = "Đang dùng giá gần nhất vì OKX tạm thời lỗi";
    if (state.currentDecision) renderSelected(state.currentDecision);
    return;
  }

  const previous = state.lastPrices.get(focus.symbol);
  refs.pulseSymbol.textContent = `${focus.symbol} ${fmt(current, 2)}`;
  if (previous) {
    const delta = current - previous;
    const deltaPct = previous ? (delta / previous) * 100 : 0;
    refs.pulseMove.className = movementClass(delta);
    refs.pulseMove.textContent = `${movementText(delta)} ${signed(delta, 2)} (${signed(deltaPct, 3)}%) trong 1 phút`;
    appendPulseLog({ symbol: focus.symbol, last: current, previous, createdAt: payload.created_at });
  } else {
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = `Mốc ban đầu ${fmt(current, 2)}. Sẽ so sánh sau 1 phút.`;
  }

  prices.forEach((item) => {
    const last = nullableNumber(item.last);
    if (last !== null && !item.stale) state.lastPrices.set(item.symbol, last);
  });

  if (state.currentDecision) renderSelected(state.currentDecision);
}

function renderSystemSummaryChart(payload) {
  if (!refs.systemSummaryChart) return;
  const data = payload || {};
  const statusCounts = data.status_counts || {};
  const moduleCounts = data.module_status_counts || {};
  const rows = [
    { label: "Tiêu chí OK", value: Number(statusCounts.ok || 0), color: "#1f8a5b" },
    { label: "Tiêu chí cần chú ý", value: Number(statusCounts.warn || 0), color: "#b7791f" },
    { label: "Tiêu chí lỗi", value: Number(statusCounts.fail || 0), color: "#bd3f32" },
    { label: "Module OK", value: Number(moduleCounts.ok || 0), color: "#147a7e" },
    { label: "Module cần chú ý", value: Number(moduleCounts.warn || 0), color: "#315f9f" },
    { label: "Module lỗi", value: Number(moduleCounts.fail || 0), color: "#c0568a" },
  ].filter((row) => row.value > 0);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  if (!total) {
    refs.systemSummaryChart.hidden = false;
    refs.systemSummaryChart.innerHTML = '<div class="module-chart-empty">Chưa có snapshot đủ để tổng hợp biểu đồ tròn.</div>';
    return;
  }
  let cursor = 0;
  const segments = rows.map((row) => {
    const angle = row.value / total * 360;
    const path = donutSegmentPath(100, 100, 82, 45, cursor, cursor + angle);
    cursor += angle;
    return `<path d="${path}" fill="${row.color}"><title>${escapeHtml(row.label)}: ${fmt(row.value, 0)}</title></path>`;
  }).join("");
  refs.systemSummaryChart.hidden = false;
  refs.systemSummaryChart.innerHTML = `
    <div class="system-summary-head">
      <strong>Tổng hợp ${escapeHtml(data.period || "-")} · ${escapeHtml(data.start || "-")} đến ${escapeHtml(data.end || "-")}</strong>
      <span>${fmt(data.snapshot_count || 0, 0)} ngày dữ liệu</span>
    </div>
    <section class="module-chart-panel">
      <div class="module-chart-wrap">
        <svg class="module-donut" viewBox="0 0 200 200" role="img" aria-label="Biểu đồ tròn tổng hợp hệ thống">
          ${segments}
          <circle cx="100" cy="100" r="39" fill="#fbfcfc"></circle>
          <text x="100" y="96" text-anchor="middle" class="module-donut-total">${fmt(total, 0)}</text>
          <text x="100" y="115" text-anchor="middle" class="module-donut-caption">mục</text>
        </svg>
      </div>
      <div class="module-chart-legend">
        ${rows.map((row) => `
          <div class="module-chart-legend-item">
            <span class="module-chart-swatch" style="background:${row.color}"></span>
            <div>
              <strong>${escapeHtml(row.label)}</strong>
              <small>${fmt(row.value, 0)} · ${fmt(row.value / total * 100, 1)}%</small>
            </div>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function normalizeVietnameseUi(root = document.body) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  let node;
  while ((node = walker.nextNode())) textNodes.push(node);
  textNodes.forEach((textNode) => {
    const fixed = viText(textNode.nodeValue || "");
    if (fixed !== textNode.nodeValue) textNode.nodeValue = fixed;
  });
  root.querySelectorAll?.("[title],[aria-label]").forEach((element) => {
    if (element.hasAttribute("title")) {
      const title = element.getAttribute("title") || "";
      element.setAttribute("title", viText(title));
    }
    if (element.hasAttribute("aria-label")) {
      const label = element.getAttribute("aria-label") || "";
      element.setAttribute("aria-label", viText(label));
    }
  });
}

function moduleDisplayRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  const sourceRows = Array.isArray(rows) ? rows : [];
  if (moduleNumber === 3) {
    const keep = new Set(["winrate", "maxdrawdownpercent", "riskmultiplierpercent", "scoreadjustment", "confidenceadjustment", "ispaused"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber === 2) {
    const riskRows = bunnyMinimizeRows(sourceRows);
    if (riskRows.length) return riskRows;
    return sourceRows.filter((row) => viLabel(row?.label || "") !== "updatedat");
  }
  if (moduleNumber === 5) {
    const keep = new Set(["historysamples", "currentconfidence", "bullpercent", "bearpercent", "sidewaypercent", "highvolatilitypercent", "lowvolatilitypercent"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber === 8) {
    const keep = new Set(["recovery_step", "recovery_band", "cycle_pnl_usdt", "hard_peak_loss_usdt", "soft_return_pnl_usdt", "next_margin_usdt", "blocked", "processed_keys_count", "last_realized_net_pnl", "last_loss_recorded"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber !== 1) return sourceRows;
  const aiRows = moduleAiDecisionRows(sourceRows);
  if (aiRows.length) return aiRows;
  return sourceRows.filter((row) => {
    const label = viLabel(row?.label || "");
    return !label.includes("ngay kiem tra") && !label.includes("cap nhat luc");
  });
}

function moduleAuxRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  const sourceRows = Array.isArray(rows) ? rows : [];
  if (moduleNumber === 2) {
    const keep = new Set(["updatedat", "updated_at", "createdat", "created_at"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")) || !BUNNY_MINIMIZE_ROW_KEYS.has(String(row?.label || "").trim()));
  }
  if (moduleNumber === 3) {
    const keep = new Set(["totaltrades", "wincount", "losscount", "breakevencount", "profitfactor", "totalpnl", "updatedat", "reason"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber === 5) {
    const keep = new Set(["unknownpercent", "currentregime", "updatedat", "reason"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber === 8) {
    const keep = new Set(["updated_at", "block_reason", "last_loss_symbol", "last_loss_side"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber !== 1) return [];
  return sourceRows.filter((row) => {
    const label = viLabel(row?.label || "");
    return !AI_DECISION_ROW_KEYS.has(moduleAiRowKey(row))
      || label.includes("ngay kiem tra")
      || label.includes("cap nhat luc");
  });
}

function moduleTrendMeaning(row) {
  if (row?.trendUp || row?.trendDown) {
    return {
      up: row.trendUp || "",
      down: row.trendDown || "",
    };
  }
  const label = viLabel(row?.label || "");
  if (label.includes("enabled") || label.startsWith("is") || label.includes("allow") || label.includes("block")) {
    return {
      up: "Tăng hoặc bật nghĩa là cơ chế này đang tham gia mạnh hơn vào vận hành.",
      down: "Giảm hoặc tắt nghĩa là cơ chế này ít tác động hơn hoặc đang bị vô hiệu.",
    };
  }
  if (label.includes("loss") || label.includes("drawdown") || label.includes("error") || label.includes("spread") || label.includes("drift")) {
    return {
      up: "Tăng nghĩa là rủi ro hoặc độ lệch đang lớn hơn, cần kiểm tra kỹ.",
      down: "Giảm nghĩa là rủi ro hoặc độ lệch đang hạ nhiệt.",
    };
  }
  if (label.includes("win") || label.includes("profit") || label.includes("confidence") || label.includes("score")) {
    return {
      up: "Tăng nghĩa là chất lượng tín hiệu hoặc sức khỏe chiến lược tốt hơn.",
      down: "Giảm nghĩa là chất lượng tín hiệu yếu đi, cần xem lại điều kiện vào lệnh.",
    };
  }
  if (label.includes("count") || label.includes("active") || label.includes("calls") || label.includes("replay")) {
    return {
      up: "Tăng nghĩa là tần suất hoặc khối lượng hoạt động của biến này lớn hơn.",
      down: "Giảm nghĩa là biến này ít phát sinh hơn hoặc dữ liệu đang ít đi.",
    };
  }
  if (label.includes("max") || label.includes("threshold") || label.includes("limit")) {
    return {
      up: "Tăng nghĩa là ngưỡng cho phép rộng hơn hoặc mức chịu đựng cao hơn.",
      down: "Giảm nghĩa là hệ thống siết điều kiện hoặc giảm mức chịu đựng.",
    };
  }
  return {
    up: "Tăng nghĩa là tỷ trọng của biến này trong module lớn hơn.",
    down: "Giảm nghĩa là tỷ trọng của biến này trong module nhỏ hơn.",
  };
}


const renderHealthCriterionDetailOriginal = renderHealthCriterionDetail;
renderHealthCriterionDetail = function renderHealthCriterionDetailPatched(item) {
  renderHealthCriterionDetailOriginal(item);
  normalizeVietnameseUi(refs.systemModuleDetail || document.body);
};

const renderModuleDetailOriginal = renderModuleDetail;
renderModuleDetail = function renderModuleDetailPatched(module, options = {}) {
  renderModuleDetailOriginal(module, options);
  normalizeVietnameseUi(refs.systemModuleDetail || document.body);
};

const renderSystemModulesOriginal = renderSystemModules;
renderSystemModules = function renderSystemModulesPatched(modules) {
  renderSystemModulesOriginal(modules);
  normalizeVietnameseUi(refs.systemModuleGrid || document.body);
};

const renderSystemChecklistOriginal = renderSystemChecklist;
renderSystemChecklist = function renderSystemChecklistPatched(payload) {
  renderSystemChecklistOriginal(payload);
  normalizeVietnameseUi(document.body);
};

const renderSystemSummaryChartOriginal = renderSystemSummaryChart;
renderSystemSummaryChart = function renderSystemSummaryChartPatched(payload) {
  renderSystemSummaryChartOriginal(payload);
  normalizeVietnameseUi(refs.systemSummaryChart || document.body);
};

async function loadSystemChecklist(date = "", options = {}) {
  const canReuseInFlight = !date && !options.forceRefresh;
  if (canReuseInFlight && state.systemChecklistInFlight) return state.systemChecklistInFlight;
  const hasExistingPayload = Boolean(state.lastSystemChecklistPayload);
  if (!date && !hasExistingPayload && refs.systemChecklistStatus) refs.systemChecklistStatus.textContent = "?ang t?i...";
  if (!date && !hasExistingPayload && refs.systemModuleStatus) refs.systemModuleStatus.textContent = "?ang t?i...";
  if (!date && !hasExistingPayload && refs.systemModuleGrid) refs.systemModuleGrid.innerHTML = renderMarketRegimeLoadingSkeleton();
  const aiRange = date ? "current" : normalizeSystemModuleAiRange(options.aiRange || state.systemModuleAiRange);
  if (!date) state.systemModuleAiRange = aiRange;
  const requestSeq = date ? null : ++state.systemChecklistRequestSeq;
  const forceParam = options.forceRefresh ? "&force_refresh=true" : "";
  const fastParam = !date && !options.forceRefresh ? "&fast=true" : "";
  const aiRangeParam = !date ? `&ai_range=${encodeURIComponent(aiRange)}` : "";
  const selectedModuleKey = !date
    ? (options.selectedModuleKey || state.selectedSystemModuleKey || "")
    : "";
  const selectedModuleParam = selectedModuleKey ? `&selected_module_key=${encodeURIComponent(selectedModuleKey)}` : "";
  const url = date
    ? `/api/system-checklist?date=${encodeURIComponent(date)}&_=${Date.now()}`
    : `/api/system-checklist?_=${Date.now()}${forceParam}${fastParam}${aiRangeParam}${selectedModuleParam}`;
  const request = (async () => {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  })();
  if (canReuseInFlight) state.systemChecklistInFlight = request;
  let payload;
  try {
    payload = await request;
  } catch (err) {
    if (!date && !hasExistingPayload && refs.systemModuleGrid) {
      refs.systemModuleGrid.innerHTML = renderMarketRegimeLoadError(err?.message || String(err));
    }
    throw err;
  } finally {
    if (canReuseInFlight && state.systemChecklistInFlight === request) state.systemChecklistInFlight = null;
  }
  if (!date && requestSeq !== state.systemChecklistRequestSeq) return;
  const backendPreviousPayload = payload && typeof payload.previous_snapshot === "object" ? payload.previous_snapshot : null;
  const lastPayload = state.lastSystemChecklistPayload;
  const sameDateAsLast = Boolean(lastPayload && payload?.date && lastPayload.date === payload.date);
  const lastRange = normalizeSystemModuleAiRange(lastPayload?.ai_range || "current");
  const payloadRange = normalizeSystemModuleAiRange(payload?.ai_range || aiRange);
  const sameRangeAsLast = Boolean(lastPayload && lastRange === payloadRange);
  state.previousSystemChecklistPayload = sameDateAsLast && sameRangeAsLast && lastPayload
    ? lastPayload
    : (sameRangeAsLast
      ? (backendPreviousPayload || lastPayload)
      : (lastPayload && payloadRange === "all" ? null : backendPreviousPayload));
  state.lastSystemChecklistPayload = payload;
  if (!date) {
    state.systemChecklistPayloadByRange[payloadRange] = payload;
  }
  renderSystemChecklist(payload);
  if (refs.systemChecklistDate && payload.date) refs.systemChecklistDate.value = payload.date;
}

function startSystemChecklistRefresh() {
  if (state.systemChecklistTimer) clearInterval(state.systemChecklistTimer);
  state.systemChecklistTimer = setInterval(() => {
    if (!dashboardIsVisible()) return;
    loadSystemChecklist("", { aiRange: "current", selectedModuleKey: state.selectedSystemModuleKey }).catch((err) => setStatus(`Lá»i system health: ${err.message}`));
  }, SYSTEM_CHECKLIST_REFRESH_MS);
}

function startRealtimeModuleRefresh() {
  if (state.realtimeModuleTimer) clearInterval(state.realtimeModuleTimer);
  state.realtimeModuleTimer = setInterval(() => {
    if (!dashboardIsVisible()) return;
    if (!state.selectedSystemModuleKey || !refs.systemModuleOverlay || refs.systemModuleOverlay.hidden) return;
    const module = findSystemModuleByKey(state.selectedSystemModuleKey);
    if (!isRealtimeSystemModule(module)) return;
    loadSystemChecklist("", { forceRefresh: true, aiRange: "current", selectedModuleKey: state.selectedSystemModuleKey }).catch((err) => setStatus(`Loi realtime module: ${err.message}`));
  }, REALTIME_MODULE_REFRESH_MS);
}

async function loadLcPipeline() {
  if (state.lcPipelineInFlight) return state.lcPipelineInFlight;
  state.lcPipelineInFlight = (async () => {
    const res = await fetch(`/api/lc-pipeline?_=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderLcPipelineEnhanced(await res.json());
  })();
  try {
    return await state.lcPipelineInFlight;
  } finally {
    state.lcPipelineInFlight = null;
  }
}

function startLcPipelineRefresh() {
  if (state.lcPipelineTimer) clearInterval(state.lcPipelineTimer);
  loadLcPipeline().catch((err) => setStatus(`Lá»i LC pipeline: ${err.message}`));
  state.lcPipelineTimer = setInterval(
    () => {
      if (!dashboardIsVisible()) return;
      loadLcPipeline().catch((err) => setStatus(`Lá»i LC pipeline: ${err.message}`));
    },
    LC_PIPELINE_REFRESH_MS,
  );
}

async function loadSystemSummary(period) {
  const res = await fetch(`/api/system-checklist/summary?period=${encodeURIComponent(period)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderSystemSummaryChart(await res.json());
}

async function runStorageMaintenance() {
  if (!refs.storageMaintenanceBtn) return;
  refs.storageMaintenanceBtn.disabled = true;
  setStatus("Đang bảo trì storage");
  try {
    const res = await fetch("/api/storage/maintenance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vacuum: false }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || `HTTP ${res.status}`);
    renderSystemChecklist({ storage: payload.stats, items: [], ok_count: 0, total: 0, date: payload.created_at });
    await loadSystemChecklist();
    setStatus("Đã bảo trì storage");
  } catch (err) {
    setStatus(`Lỗi bao tri storage: ${err.message}`);
  } finally {
    refs.storageMaintenanceBtn.disabled = false;
  }
}

function renderOkxPositions(payload) {
  const data = payload || {};
  const positions = data.positions || [];
  const orders = data.open_orders || [];
  state.currentPositions = positions;
  state.currentPositionsPayload = data;
  const selectedPositionStillOpen = state.selectedViewSymbol && positions.some((position) => position.symbol === state.selectedViewSymbol);
  if (state.selectedViewSource === "position" && state.selectedViewSymbol && !selectedPositionStillOpen) {
    state.selectedViewSymbol = null;
    state.selectedViewSource = null;
    if (state.currentDecision) renderDecision(state.currentDecision);
  }
  if (refs.positionsStatus) {
    refs.positionsStatus.textContent = data.created_at ? `${positions.length} vi the - ${timeLabel(data.created_at)}` : data.message || "-";
  }
  if (refs.positionRows) {
    refs.positionRows.innerHTML = "";
    if (!positions.length) {
      const row = document.createElement("tr");
      const message = data.enabled === false && data.message ? data.message : "Chưa có vị thế mở trên OKX demo";
      row.innerHTML = `<td colspan="11" class="empty-cell">${escapeHtml(message)}</td>`;
      refs.positionRows.appendChild(row);
    } else {
      positions.forEach((position) => {
        const row = document.createElement("tr");
        const sideClass = positionSideClass(position.side);
        const isSelected = state.selectedViewSource === "position" && state.selectedViewSymbol === position.symbol;
        row.className = isSelected ? "selected-row candidate-row position-row" : "candidate-row position-row";
        row.dataset.symbol = position.symbol;
        row.tabIndex = 0;
        row.title = `Xem ket qua giao dich cua ${position.symbol}`;
        const protectionOk = position.tp_sl_status === "ok";
        row.innerHTML = `
          <td>${pairCellHtml(position.symbol)}</td>
          <td><span class="side ${sideClass}">${positionSideLabel(position.side)}</span></td>
          <td>${fmt(position.contracts, 6)}</td>
          <td>${fmt(position.entry_price, 4)}</td>
          <td>${fmt(position.mark_price, 4)}</td>
          <td>${position.leverage ? `${fmt(position.leverage, 2)}x` : "-"}</td>
          <td>${position.stop_loss === null || position.stop_loss === undefined ? "MISSING" : fmt(position.stop_loss, 4)}</td>
          <td>${position.take_profit === null || position.take_profit === undefined ? "MISSING" : fmt(position.take_profit, 4)}</td>
          <td><span class="status-pill ${protectionOk ? "ok" : "warn"}">${protectionOk ? "OK" : "MISSING"}</span></td>
          <td class="${Number(position.unrealized_pnl) >= 0 ? "pnl-up" : "pnl-down"}">${signed(position.unrealized_pnl, 4)}</td>
          <td>${position.margin_mode || "-"}</td>
        `;
        row.addEventListener("click", () => {
          state.selectedViewSymbol = position.symbol;
          state.selectedViewSource = "position";
          if (state.currentDecision) renderDecision(state.currentDecision);
          renderOkxPositions(state.currentPositionsPayload);
          loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`));
          const panel = refs.planCanvas?.closest(".plan-panel");
          const top = panel ? panel.getBoundingClientRect().top + window.scrollY - 12 : 0;
          window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
        });
        row.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          row.click();
        });
        refs.positionRows.appendChild(row);
      });
    }
  }
  if (refs.ordersStatus) {
    refs.ordersStatus.textContent = `${orders.length} lệnh chờ`;
  }
  if (refs.orderRows) {
    refs.orderRows.innerHTML = "";
    if (!orders.length) {
      const row = document.createElement("tr");
      const message = data.enabled === false && data.message ? data.message : "Chua co lệnh chờ tren OKX demo";
      row.innerHTML = `<td colspan="8" class="empty-cell">${escapeHtml(message)}</td>`;
      refs.orderRows.appendChild(row);
    } else {
      orders.forEach((order) => {
        const row = document.createElement("tr");
        const sideClass = positionSideClass(order.side);
        row.innerHTML = `
          <td>${pairCellHtml(order.symbol)}</td>
          <td><span class="side ${sideClass}">${positionSideLabel(order.side)}</span></td>
          <td>${order.type || "-"}</td>
          <td>${fmt(order.price, 4)}</td>
          <td>${fmt(order.amount, 6)}</td>
          <td>${fmt(order.filled, 6)}</td>
          <td>${fmt(order.remaining, 6)}</td>
          <td>${order.status || "-"}</td>
        `;
        refs.orderRows.appendChild(row);
      });
    }
  }
  if (state.currentDecision && state.selectedViewSource === "position" && state.selectedViewSymbol) {
    renderSummary(state.currentDecision);
    renderSelected(state.currentDecision);
    renderCandidates(state.currentDecision);
  }
  refreshOpenPositionManagementDetailFromLiveOkx();
}

function renderOkxDemoStatus(status) {
  const data = status || {};
  state.okxDemoReady = Boolean(data.ready);
  if (!refs.demoStatus) return;
  refs.demoStatus.className = state.okxDemoReady ? "status-pill ok" : "status-pill warn";
  if (state.okxDemoReady) {
    refs.demoStatus.textContent = "OKX demo: san sang";
  } else if (data.mode && data.mode !== "demo") {
    refs.demoStatus.textContent = `OKX demo: đang tắt (${data.mode})`;
  } else if (Array.isArray(data.missing_env) && data.missing_env.length) {
    refs.demoStatus.textContent = `OKX demo: thieu ${data.missing_env.join(", ")}`;
  } else {
    refs.demoStatus.textContent = data.message || "OKX demo: chưa kết nối";
  }
}

function renderAutomationStatus(status) {
  if (!refs.paperStatus || !status) return;
  const result = status.last_result || "not_started";
  const next = status.next_scan_at ? ` | lan toi ${timeLabel(status.next_scan_at)}` : "";
  if (!status.enabled) {
    refs.paperStatus.textContent = "Auto server: đang tắt";
  } else if (result === "order_submitted") {
    refs.paperStatus.textContent = `Auto server: đã gửi lệnh demo ${status.order_id || ""}${next}`;
  } else if (result === "no_order") {
    const reason = (status.risk_reasons || [])[0];
    refs.paperStatus.textContent = reason ? `Auto server: chưa vào lệnh - ${riskReasonVi(reason)}${next}` : `Auto server: đã scan, chưa có lệnh${next}`;
  } else if (result === "skipped_busy") {
    refs.paperStatus.textContent = `Auto server: bỏ qua vì đang có scan khác${next}`;
  } else if (result === "error") {
    refs.paperStatus.textContent = `Auto server loi: ${status.error || "khong ro"}`;
  } else {
    refs.paperStatus.textContent = `Auto server: cho vong scan dau tien${next}`;
  }
}

function renderNews(decision) {
  const items = (decision.news_items || []).slice(0, 12);
  refs.newsCount.textContent = `${decision.news_items ? decision.news_items.length : 0} bai`;
  refs.newsList.innerHTML = "";
  items.forEach((item) => {
    const node = document.createElement("article");
    node.className = "news-item";
    const sentiment = item.sentiment_label || "neutral";
    node.innerHTML = `
      <a href="${item.url}" target="_blank" rel="noreferrer">${item.title}</a>
      <div class="news-meta">
        <span>${item.source || "-"}</span>
        <span class="sentiment ${sentiment}">${sentiment}</span>
      </div>
      <div class="news-meta">
        <span>${(item.symbols || []).join(", ") || "market"}</span>
        <span>${fmt(item.sentiment_score, 2)}</span>
      </div>
    `;
    refs.newsList.appendChild(node);
  });
}

function renderDecision(decision) {
  state.currentDecision = decision;
  const hasCandidate = state.selectedViewSymbol && (decision.candidates || []).some((candidate) => candidate.symbol === state.selectedViewSymbol);
  const hasPosition = state.selectedViewSymbol && state.currentPositions.some((position) => position.symbol === state.selectedViewSymbol);
  if (state.selectedViewSymbol && !hasCandidate && !hasPosition) {
    state.selectedViewSymbol = null;
    state.selectedViewSource = null;
  }
  renderSummary(decision);
  renderSelected(decision);
  renderCandidates(decision);
  renderNews(decision);
}

function movementClass(delta) {
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "flat";
}

function movementText(delta) {
  if (delta > 0) return "TANG";
  if (delta < 0) return "GIAM";
  return "KHONG DOI";
}

function appendPulseLog({ symbol, last, previous, createdAt }) {
  if (!previous || !Number.isFinite(last) || !Number.isFinite(previous)) return;
  const delta = last - previous;
  const deltaPct = previous ? (delta / previous) * 100 : 0;
  const cls = movementClass(delta);
  const node = document.createElement("div");
  node.className = `pulse-feed-item ${cls}`;
  node.innerHTML = `
    <span>${new Date(createdAt).toLocaleTimeString()}</span>
    <div>${symbol} ${movementText(delta)} tu ${fmt(previous, 2)} den ${fmt(last, 2)}</div>
    <strong>${signed(delta, 2)} (${signed(deltaPct, 3)}%)</strong>
  `;
  refs.pulseFeed.prepend(node);
  while (refs.pulseFeed.children.length > 12) refs.pulseFeed.removeChild(refs.pulseFeed.lastElementChild);
}

function renderPricePulse(payload) {
  const prices = payload.prices || [];
  prices.forEach((item) => {
    const last = nullableNumber(item.last);
    if (last !== null) state.currentPrices.set(item.symbol, last);
  });

  const viewCandidate = state.currentDecision ? selectedCandidate(state.currentDecision) : null;
  const focusSymbol = viewCandidate?.symbol || payload.focus?.symbol || prices[0]?.symbol;
  const focus = prices.find((item) => item.symbol === focusSymbol) || prices[0];
  const warnings = payload.warnings || [];
  const sourceLabel = payload.cached ? "Cache" : warnings.length ? "Canh bao gia" : "Moi 60 giay";
  refs.pulseUpdated.textContent = `${sourceLabel} - ${timeLabel(payload.created_at)}`;

  if (!focus) {
    refs.pulseSymbol.textContent = "-";
    refs.pulseMove.textContent = warnings[0] || "Khong co du lieu gia";
    refs.pulseMove.className = "flat";
    return;
  }

  const current = nullableNumber(focus.last);
  if (current === null) {
    refs.pulseSymbol.textContent = focus.symbol || "-";
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = focus.error || warnings[0] || "Khong co du lieu gia";
    return;
  }

  if (focus.stale) {
    refs.pulseSymbol.textContent = `${focus.symbol} ${fmt(current, 2)}`;
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = "Đang dùng gia gan nhat vi OKX tam thoi loi";
    if (state.currentDecision) renderSelected(state.currentDecision);
    return;
  }

  const previous = state.lastPrices.get(focus.symbol);
  refs.pulseSymbol.textContent = `${focus.symbol} ${fmt(current, 2)}`;
  if (previous) {
    const delta = current - previous;
    const deltaPct = previous ? (delta / previous) * 100 : 0;
    refs.pulseMove.className = movementClass(delta);
    refs.pulseMove.textContent = `${movementText(delta)} ${signed(delta, 2)} (${signed(deltaPct, 3)}%) trong 1 phút`;
    appendPulseLog({ symbol: focus.symbol, last: current, previous, createdAt: payload.created_at });
  } else {
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = `Mốc ban đầu ${fmt(current, 2)}. Sẽ so sánh sau 1 phút.`;
  }

  prices.forEach((item) => {
    const last = nullableNumber(item.last);
    if (last !== null && !item.stale) state.lastPrices.set(item.symbol, last);
  });

  if (state.currentDecision) renderSelected(state.currentDecision);
}

async function loadPrices() {
  if (state.pricesInFlight) return state.pricesInFlight;
  state.pricesInFlight = (async () => {
    const res = await fetch("/api/prices");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderPricePulse(await res.json());
  })();
  try {
    return await state.pricesInFlight;
  } finally {
    state.pricesInFlight = null;
  }
}

async function loadDecision() {
  if (state.decisionInFlight) return state.decisionInFlight;
  state.decisionInFlight = (async () => {
    const res = await fetch("/api/decision");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (!payload.report_exists || !payload.decision) {
      setStatus("Ch?a c? b?o c?o");
      renderPaperState(payload.paper_state);
      return false;
    }
    renderDecision(payload.decision);
    renderPaperState(payload.paper_state);
    setStatus(`B?o c?o: ${payload.report_path}`);
    return true;
  })();
  try {
    return await state.decisionInFlight;
  } finally {
    state.decisionInFlight = null;
  }
}

async function loadOkxDemoStatus() {
  const res = await fetch("/api/okx-demo-status");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderOkxDemoStatus(await res.json());
}

async function loadAutomationStatus() {
  if (state.automationStatusInFlight) return state.automationStatusInFlight;
  state.automationStatusInFlight = (async () => {
    const res = await fetch("/api/automation-status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderAutomationStatus(await res.json());
  })();
  try {
    return await state.automationStatusInFlight;
  } finally {
    state.automationStatusInFlight = null;
  }
}

async function loadConfigSummary() {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderConfig(await res.json());
}

async function saveLeverage() {
  if (!refs.leverageInput || !refs.saveLeverageBtn) return;
  normalizeLeverageInput();
  const leverage = Number(refs.leverageInput.value);
  if (!Number.isInteger(leverage) || leverage < MIN_LEVERAGE || leverage > MAX_LEVERAGE) {
    setLeverageStatus(`Nhap ${MIN_LEVERAGE}-${MAX_LEVERAGE}x`, "warn");
    return;
  }
  refs.saveLeverageBtn.disabled = true;
  setLeverageStatus("Đang lưu...", "");
  try {
    const res = await fetch("/api/config/leverage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ leverage }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || `HTTP ${res.status}`);
    renderConfig(payload);
    setLeverageStatus(`Đã lưu ${leverage}x`, "ok");
    setStatus(`Đòn bẩy mới: ${leverage}x. Áp dụng từ lệnh mở sau.`);
    loadDecision().catch((err) => setStatus(`Lỗi: ${err.message}`));
  } catch (err) {
    setLeverageStatus("Lưu lỗi", "warn");
    setStatus(`Lỗi luu don bay: ${err.message}`);
  } finally {
    refs.saveLeverageBtn.disabled = false;
  }
}

async function saveOrderMargin() {
  if (!refs.orderMarginInput || !refs.saveOrderMarginBtn) return;
  normalizeOrderMarginInput();
  const margin = Number(refs.orderMarginInput.value);
  const maxMargin = maxBaseMarginUsdt();
  if (!Number.isFinite(margin) || margin < MIN_BASE_MARGIN_USDT || margin > maxMargin) {
    setOrderMarginStatus(`Nhập ${MIN_BASE_MARGIN_USDT}-${controlNumber(maxMargin)} USDT`, "warn");
    return;
  }
  refs.saveOrderMarginBtn.disabled = true;
  setOrderMarginStatus("Đang lưu...", "");
  try {
    const res = await fetch("/api/config/order-usdt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ margin_usdt: margin }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || `HTTP ${res.status}`);
    renderConfig(payload);
    setOrderMarginStatus(`Đã lưu ${controlNumber(margin)} USDT`, "ok");
    const notional = Number(payload.estimated_notional_usdt);
    const suffix = Number.isFinite(notional) ? `, vị thế khoảng ${controlNumber(notional)} USDT` : "";
    setStatus(`USDT/lệnh mới: ${controlNumber(margin)} USDT${suffix}.`);
    loadDecision().catch((err) => setStatus(`Lỗi: ${err.message}`));
  } catch (err) {
    setOrderMarginStatus("Lưu lỗi", "warn");
    setStatus(`Lỗi lưu USDT/lệnh: ${err.message}`);
  } finally {
    refs.saveOrderMarginBtn.disabled = false;
  }
}

async function loadOkxPositions({ force = false } = {}) {
  if (state.okxPositionsInFlight && !force) return state.okxPositionsInFlight;
  state.okxPositionsInFlight = (async () => {
    const res = await fetch(`/api/okx-positions?_=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderOkxPositions(await res.json());
  })();
  try {
    return await state.okxPositionsInFlight;
  } finally {
    state.okxPositionsInFlight = null;
  }
}

function startOkxPositionsRefresh() {
  if (state.okxPositionsTimer) clearInterval(state.okxPositionsTimer);
  loadOkxPositions({ force: true }).catch((err) => setStatus(`Lỗi vị thế OKX: ${err.message}`));
  state.okxPositionsTimer = setInterval(
    () => {
      if (!dashboardIsVisible()) return;
      loadOkxPositions().catch((err) => setStatus(`Lỗi vị thế OKX: ${err.message}`));
    },
    OKX_POSITIONS_REFRESH_MS,
  );
}

async function runAnalysis() {
  if (state.running) return;
  setBusy(true);
  setStatus("Đang phân tích");
  try {
    const res = await fetch("/api/analyze", { method: "POST" });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${res.status}`);
    }
    const payload = await res.json();
    state.selectedViewSymbol = null;
    state.selectedViewSource = null;
    renderDecision(payload.decision);
    renderPaperState(payload.paper_state);
    setStatus(`Đã cập nhật: ${payload.report_path}`);
    loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`));
  } catch (err) {
    setStatus(`Lỗi: ${err.message}`);
  } finally {
    setBusy(false);
  }
}

function startPricePulse() {
  if (state.priceTimer) clearInterval(state.priceTimer);
  loadPrices().catch((err) => setStatus(`Lỗi giá: ${err.message}`));
  state.priceTimer = setInterval(() => {
    if (!dashboardIsVisible()) return;
    loadPrices().catch((err) => setStatus(`Lỗi giá: ${err.message}`));
  }, PRICE_REFRESH_MS);
}

function startPaperAutoScan() {
  if (state.paperTimer) clearInterval(state.paperTimer);
  const seconds = Math.max(60, Number(state.paperIntervalSeconds || 600));
  const refresh = () => {
    if (!dashboardIsVisible()) return;
    loadDecision().catch((err) => setStatus(`Lá»i: ${err.message}`));
    loadAutomationStatus().catch((err) => setStatus(`Lá»i auto server: ${err.message}`));
  };
  state.paperTimer = setInterval(refresh, seconds * 1000);
  if (refs.paperNextScan) refs.paperNextScan.textContent = `Server auto: ${intervalLabel(seconds)}`;
}

function resetAutoTimer() {
  if (state.autoTimer) {
    clearInterval(state.autoTimer);
    state.autoTimer = null;
  }
  if (!refs.autoRun.checked) return;
  const seconds = Math.max(30, Number(refs.intervalInput.value || 60));
  state.autoTimer = setInterval(runAnalysis, seconds * 1000);
}

refs.refreshBtn.addEventListener("click", () => {
  loadDecision().catch((err) => setStatus(`Lỗi: ${err.message}`));
  loadOkxPositions({ force: true }).catch((err) => setStatus(`Lỗi vi the OKX: ${err.message}`));
  loadLcPipeline().catch((err) => setStatus(`Lỗi LC pipeline: ${err.message}`));
  loadSystemChecklist("", { aiRange: "current", selectedModuleKey: state.selectedSystemModuleKey }).catch((err) => setStatus(`Lỗi system health: ${err.message}`));
});
refs.analyzeBtn.addEventListener("click", runAnalysis);
refs.autoRun.addEventListener("change", resetAutoTimer);
if (refs.darkModeToggle) refs.darkModeToggle.addEventListener("change", setThemeFromToggle);
refs.intervalInput.addEventListener("change", resetAutoTimer);
if (refs.saveOrderMarginBtn) refs.saveOrderMarginBtn.addEventListener("click", saveOrderMargin);
if (refs.orderMarginInput) {
  refs.orderMarginInput.addEventListener("input", sanitizeOrderMarginInput);
  refs.orderMarginInput.addEventListener("blur", normalizeOrderMarginInput);
  refs.orderMarginInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    saveOrderMargin();
  });
}
if (refs.saveLeverageBtn) refs.saveLeverageBtn.addEventListener("click", saveLeverage);
if (refs.storageMaintenanceBtn) refs.storageMaintenanceBtn.addEventListener("click", runStorageMaintenance);
if (refs.systemChecklistHistoryBtn) {
  refs.systemChecklistHistoryBtn.addEventListener("click", () => {
    const date = refs.systemChecklistDate ? refs.systemChecklistDate.value : "";
    if (!date) {
      setStatus("Chọn ngày cần xem lại");
      return;
    }
    loadSystemChecklist(date).catch((err) => setStatus(`Lỗi dữ liệu ngày cũ: ${err.message}`));
  });
}
if (refs.systemChecklistTodayBtn) {
  refs.systemChecklistTodayBtn.addEventListener("click", () => {
    if (refs.systemSummaryChart) refs.systemSummaryChart.hidden = true;
    loadSystemChecklist().catch((err) => setStatus(`Lỗi system health: ${err.message}`));
  });
}
if (refs.systemSummaryWeekBtn) refs.systemSummaryWeekBtn.addEventListener("click", () => loadSystemSummary("week").catch((err) => setStatus(`Lỗi tổng hợp tuần: ${err.message}`)));
if (refs.systemSummaryMonthBtn) refs.systemSummaryMonthBtn.addEventListener("click", () => loadSystemSummary("month").catch((err) => setStatus(`Lỗi tổng hợp tháng: ${err.message}`)));
if (refs.systemSummaryYearBtn) refs.systemSummaryYearBtn.addEventListener("click", () => loadSystemSummary("year").catch((err) => setStatus(`Lỗi tổng hợp năm: ${err.message}`)));
if (refs.systemModuleBackdrop) refs.systemModuleBackdrop.addEventListener("click", closeSystemModuleDetail);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && refs.systemModuleOverlay && !refs.systemModuleOverlay.hidden) {
    closeSystemModuleDetail();
  }
});
if (refs.systemChecklistBody) refs.systemChecklistBody.hidden = false;
if (refs.systemChecklistToggle && refs.systemChecklistBody && !refs.systemChecklistToggle.hidden) {
  refs.systemChecklistToggle.addEventListener("click", () => {
    const isHidden = refs.systemChecklistBody.hasAttribute("hidden");
    refs.systemChecklistBody.toggleAttribute("hidden", !isHidden);
    refs.systemChecklistToggle.textContent = isHidden ? "Ẩn chi tiết" : "Mở chi tiết";
  });
}
if (refs.leverageInput) {
  refs.leverageInput.addEventListener("beforeinput", blockInvalidLeverageInput);
  refs.leverageInput.addEventListener("input", sanitizeLeverageInput);
  refs.leverageInput.addEventListener("blur", normalizeLeverageInput);
  refs.leverageInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    saveLeverage();
  });
}
window.addEventListener("resize", () => {
  if (state.currentDecision) renderSelected(state.currentDecision);
});
document.addEventListener("visibilitychange", () => {
  if (!dashboardIsVisible()) return;
  loadDecision().catch((err) => setStatus(`Lỗi: ${err.message}`));
  loadOkxPositions().catch((err) => setStatus(`Lỗi vị thế OKX: ${err.message}`));
  loadPrices().catch((err) => setStatus(`Lỗi giá: ${err.message}`));
});

initTheme();

loadConfigSummary().catch((err) => {
  setLeverageStatus(`Lỗi: ${err.message}`, "warn");
  setOrderMarginStatus(`Lỗi: ${err.message}`, "warn");
});
startLcPipelineRefresh();
loadSystemChecklist().catch((err) => setStatus(`Lỗi system health: ${err.message}`));
startSystemChecklistRefresh();
startRealtimeModuleRefresh();
loadDecision()
  .then((exists) => {
    if (!exists) runAnalysis();
    loadOkxDemoStatus()
      .catch((err) => setStatus(`Lỗi OKX demo: ${err.message}`))
      .finally(() => {
        loadAutomationStatus().catch((err) => setStatus(`Lỗi auto server: ${err.message}`));
        startOkxPositionsRefresh();
        startPaperAutoScan();
      });
  })
  .catch((err) => setStatus(`Lỗi: ${err.message}`));
startPricePulse();
