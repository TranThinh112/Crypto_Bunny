const state = {
  autoTimer: null,
  lcPipelineTimer: null,
  priceTimer: null,
  paperTimer: null,
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
  previousSystemChecklistPayload: null,
  running: false,
};

const el = (id) => document.getElementById(id);
const MIN_LEVERAGE = 5;
const MAX_LEVERAGE = 25;
const MIN_BASE_MARGIN_USDT = 1;

const refs = {
  statusLine: el("statusLine"),
  autoRun: el("autoRun"),
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

function positionAsCandidate(position, fallback = null) {
  const entry = nullableNumber(position.entry_price) ?? nullableNumber(fallback?.entry);
  const mark = nullableNumber(position.mark_price);
  const leverage = nullableNumber(position.leverage) ?? nullableNumber(fallback?.leverage) ?? 1;
  const side = normalizedPositionSide(position.side) || fallback?.side || "long";
  const takeProfitPct = nullableNumber(fallback?.take_profit_pct) ?? 75;
  const stopLossPct = nullableNumber(fallback?.stop_loss_pct) ?? 50;
  const priceTakeProfitPct = nullableNumber(fallback?.price_take_profit_pct) ?? takeProfitPct / Math.max(leverage, 1);
  const priceStopLossPct = nullableNumber(fallback?.price_stop_loss_pct) ?? stopLossPct / Math.max(leverage, 1);
  let takeProfit = nullableNumber(fallback?.take_profit);
  let stopLoss = nullableNumber(fallback?.stop_loss);

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
    unrealized_pnl: nullableNumber(position.unrealized_pnl),
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
    .replace(/TP\/SL target: TP ([0-9]+)%, SL ([0-9]+)% \(price_percent, ([0-9]+)x\)/, "Muc tieu TP/SL: TP $1%, SL $2% theo bien dong gia");
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

function renderPlan(candidate) {
  const canvas = refs.planCanvas;
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 760;
  const height = canvas.clientHeight || 240;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fbfcfc";
  ctx.fillRect(0, 0, width, height);

  if (!candidate) {
    ctx.fillStyle = "#66727c";
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
    ctx.fillStyle = "#66727c";
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

  ctx.strokeStyle = "#d9e1e5";
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

  ctx.strokeStyle = "#172026";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(Math.min(stopX, targetX), lineY);
  ctx.lineTo(Math.max(stopX, targetX), lineY);
  ctx.stroke();

  if (currentX !== null) drawProgress(ctx, entryX, currentX, lineY);

  const tpLabel = candidate.take_profit_pct ? `TP ${fmt(candidate.take_profit_pct, 0)}%` : "TP";
  const slLabel = candidate.stop_loss_pct ? `SL ${fmt(candidate.stop_loss_pct, 0)}%` : "SL";
  drawMarker(ctx, targetX, lineY, "#1f8a5b", tpLabel, target);
  drawMarker(ctx, entryX, lineY, "#147a7e", "Vao", entry);
  drawMarker(ctx, stopX, lineY, "#bd3f32", slLabel, stop);
  if (currentX !== null) drawCurrentMarker(ctx, currentX, lineY, currentPrice);

  const direction = candidate.side === "short" ? "Có lợi khi giá giảm" : "Có lợi khi giá tăng";
  ctx.fillStyle = "#66727c";
  ctx.font = "700 13px system-ui";
  ctx.fillText(direction, left, height - 18);
}

function drawProgress(ctx, entryX, currentX, y) {
  ctx.strokeStyle = "#315f9f";
  ctx.lineWidth = 5;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(entryX, y + 11);
  ctx.lineTo(currentX, y + 11);
  ctx.stroke();
  ctx.lineCap = "butt";
}

function drawMarker(ctx, x, y, color, label, value) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, 7, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#172026";
  ctx.font = "800 12px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(label, x, y - 18);
  ctx.font = "700 12px system-ui";
  ctx.fillStyle = "#66727c";
  ctx.fillText(fmt(value, 2), x, y + 27);
  ctx.textAlign = "start";
}

function drawCurrentMarker(ctx, x, y, value) {
  ctx.save();
  ctx.strokeStyle = "#315f9f";
  ctx.setLineDash([5, 5]);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x, y - 84);
  ctx.lineTo(x, y + 88);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#315f9f";
  ctx.beginPath();
  ctx.arc(x, y + 11, 8, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fillStyle = "#315f9f";
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
  const stateĐạta = paperState || {};
  state.paperIntervalSeconds = Number(stateĐạta.scan_interval_seconds || state.paperIntervalSeconds || 600);
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

function closeSystemModuleDetail() {
  if (refs.systemModuleOverlay) refs.systemModuleOverlay.hidden = true;
  if (refs.systemModuleDetail) refs.systemModuleDetail.innerHTML = "";
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
  refs.systemModuleOverlay.hidden = false;
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
      <div><span>Trạng thái</span><strong>${statusLabel(item.status)}</strong></div>
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
  if (text.toLowerCase() === "true") return 1;
  if (text.toLowerCase() === "false") return 0;
  const match = text.replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  if (!match) return null;
  const parsed = Number(match[0]);
  return Number.isFinite(parsed) ? parsed : null;
}

function moduleNumericValue(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return Math.abs(value);
  const text = String(value ?? "").trim();
  if (!text || text === "-") return null;
  if (text.toLowerCase() === "true") return 1;
  if (text.toLowerCase() === "false") return 0;
  const match = text.replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  if (!match) return null;
  const parsed = Number(match[0]);
  return Number.isFinite(parsed) ? Math.abs(parsed) : null;
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
  if (numeric !== null) return formatFixed2(numeric);
  return formatCardValue(row?.label || "", viText(row?.value ?? "-"));
}
function moduleLegendShare(chartRows, row) {
  const totalRaw = chartRows.reduce((sum, item) => sum + Math.max(0, Number(item.rawNumericValue || 0)), 0);
  if (!totalRaw) return "0.00%";
  return `${formatFixed2(Math.max(0, Number(row.rawNumericValue || 0)) / totalRaw * 100)}%`;
}
function moduleAxisTickLabel(value) {
  return formatFixed2(value);
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
  for (const item of modules) {
    if (String(item?.number ?? "").trim() !== moduleKey) continue;
    const stats = Array.isArray(item?.stats) ? item.stats : [];
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
    return { state: "flat", text: `0${suffix}` };
  }
  const delta = currentValue - previousValue;
  if (Math.abs(delta) < 1e-9) {
    return { state: "flat", text: `0${suffix}` };
  }
  return {
    state: delta > 0 ? "up" : "down",
    text: `${delta > 0 ? "+" : "-"}${moduleAxisTickLabel(Math.abs(delta))}${suffix}`,
  };
}

function moduleDisplayRows(module, rows) {
  const moduleNumber = Number(module?.number || 0);
  if (moduleNumber !== 1) return Array.isArray(rows) ? rows : [];
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
      <button class="module-chart-legend-item ${row.attention ? "attention" : ""}" type="button" data-chart-index="${index}" title="${escapeHtml(row.label || "-")}: ${escapeHtml(currentValue)}">
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

function moduleBarPercentValue(row, maxRawValue) {
  const raw = Math.max(0, Number(row?.rawNumericValue || 0));
  const label = viLabel(row?.label || "");
  const valueText = String(row?.value ?? "");
  const looksPercent = valueText.includes("%") || label.includes("percent") || label.includes("rate") || label.includes("ratio") || label.includes("confidence") || label.includes("score");
  if (looksPercent) return Math.max(0, Math.min(100, raw));
  if (!maxRawValue) return 0;
  return Math.max(0, Math.min(100, raw / maxRawValue * 100));
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
          <text x="${(chartLeft + chartRight) / 2}" y="236" text-anchor="middle" class="module-axis-caption">Trục X: giá trị hiện tại từng biến</text>
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
    if (!callout || !calloutLine || !calloutBox || !calloutText) return;
    const svg = segment.ownerSVGElement;
    if (!svg) return;
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
  const clearActive = () => {
    detail.querySelectorAll(".module-chart-segment.active, .module-chart-legend-item.active").forEach((node) => {
      node.classList.remove("active");
    });
    if (callout) callout.hidden = true;
  };
  detail.querySelectorAll(".module-chart-legend-item").forEach((item) => {
    item.addEventListener("click", () => {
      const index = item.getAttribute("data-chart-index");
      const alreadyActive = item.classList.contains("active");
      clearActive();
      if (alreadyActive) return;
      item.classList.add("active");
      detail.querySelectorAll(`.module-chart-segment[data-chart-index="${index}"]`).forEach((segment) => {
        segment.classList.add("active");
        const label = item.getAttribute("title") || "";
        showCallout(segment, label);
      });
    });
  });
  detail.querySelectorAll(".module-chart-segment").forEach((segment) => {
    segment.addEventListener("click", () => {
      const index = segment.getAttribute("data-chart-index");
      const item = detail.querySelector(`.module-chart-legend-item[data-chart-index="${index}"]`);
      if (item) item.click();
    });
  });
}

function renderModuleDetail(module) {
  if (!refs.systemModuleDetail || !refs.systemModuleOverlay || !module) return;
  const allRows = Array.isArray(module.stats) ? module.stats : [];
  const rows = moduleDisplayRows(module, allRows);
  const auxRows = moduleAuxRows(module, allRows);
  const file = module.file || {};
  const runtimeUpdatedLabel = moduleUpdatedLabel(module);
  refs.systemModuleOverlay.hidden = false;
  refs.systemModuleDetail.innerHTML = `
    <button id="systemModuleClose" class="module-close" type="button" aria-label="Đóng">×</button>
    <div class="module-detail-head">
      <div>
        <span class="module-number">Module ${escapeHtml(module.number || "-")}</span>
        <h3 id="systemModuleTitle">${escapeHtml(module.name || "-")}</h3>
        <p>${escapeHtml(module.purpose || "-")}</p>
      </div>
      <span class="status-pill ${module.status === "ok" ? "ok" : "warn"}">${moduleStatusLabel(module.status)}</span>
    </div>
    <div class="module-meta">
      <div><span>Ngày đọc</span><strong>${escapeHtml(timeLabel(file.updated_at) || "-")}</strong></div>
      <div><span>File module</span><strong>${escapeHtml(file.file_name || "Chưa có file")}</strong></div>
      <div><span>Kích thước</span><strong>${file.size_bytes === undefined || file.size_bytes === null ? "-" : bytesLabel(file.size_bytes)}</strong></div>
    </div>
    ${renderModuleChart(module, rows)}
    ${auxRows.length ? `
      <div class="module-chart-meta">
        ${auxRows.map((row) => `<div><span>${escapeHtml(row.label || "-")}</span><strong>${escapeHtml(formatCardValue(row.label, row.value))}</strong></div>`).join("")}
      </div>
    ` : ""}
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
  bindModuleChartInteractions();
}

function capitalManagementModules() {
  const now = new Date().toISOString();
  return [
    {
      number: "C1",
      name: "Capital Sync",
      purpose: "Đồng bộ số dư vốn thực từ nguồn giao dịch để tách riêng vốn sử dụng và vốn dự phòng.",
      status: "warn",
      update_event: "Sau nạp/rút, sau lệnh đóng",
      update_schedule: "5 phút/lần đồng bộ OKX",
      update_interval: "5 phút",
      has_file: false,
      file: null,
      stats: [
        { label: "Trạng thái", value: "Chưa kết nối runtime", meaning: "UI đã dành chỗ nhưng backend đồng bộ vốn chưa được nối vào dashboard hiện tại.", attention: true },
        { label: "Nguồn dữ liệu", value: "Chưa có", meaning: "Khi hoàn thiện sẽ lấy từ snapshot vốn thực tế của hệ thống." },
        { label: "Cập nhật lúc", value: now, meaning: "Thời điểm dashboard dựng trạng thái hiện tại." },
      ],
    },
    {
      number: "C2",
      name: "Capital Reserve",
      purpose: "Theo dõi quỹ dự phòng vốn, phần vốn bảo vệ và phần vốn còn được phép dùng để giao dịch.",
      status: "warn",
      update_event: "Sau nạp/rút, sau lệnh đóng",
      update_schedule: "5 phút/lần đồng bộ OKX",
      update_interval: "5 phút",
      has_file: false,
      file: null,
      stats: [
        { label: "Trạng thái", value: "Chưa kết nối runtime", meaning: "Chưa có state vốn dự phòng thực chạy trong dashboard này.", attention: true },
        { label: "Nguồn dữ liệu", value: "Chưa có", meaning: "Khi hoàn thiện sẽ lấy từ capital reserve state." },
        { label: "Cập nhật lúc", value: now, meaning: "Thời điểm dashboard dựng trạng thái hiện tại." },
      ],
    },
    {
      number: "C3",
      name: "Position Sizing",
      purpose: "Quản lý sizing và biên độ margin dùng cho lệnh mới, bao gồm mức margin cơ sở và giới hạn recovery.",
      status: "ok",
      update_event: "Sau nạp/rút, sau lệnh đóng",
      update_schedule: "5 phút/lần đồng bộ OKX",
      update_interval: "5 phút",
      has_file: false,
      file: null,
      stats: [
        { label: "Trạng thái", value: "Đã có cấu hình", meaning: "Phần sizing đang có dữ liệu cấu hình trong hệ thống." },
        { label: "Nguồn dữ liệu", value: "position_sizing", meaning: "Lấy từ cấu hình position sizing hiện hành." },
        { label: "Cập nhật lúc", value: now, meaning: "Thời điểm dashboard dựng trạng thái hiện tại." },
      ],
    },
    {
      number: "C4",
      name: "Configuration Impact",
      purpose: "Phân tích tác động của thay đổi cấu hình trước khi áp dụng vào hệ thống giao dịch.",
      status: "warn",
      update_event: "Sau nạp/rút, sau lệnh đóng",
      update_schedule: "5 phút/lần đồng bộ OKX",
      update_interval: "5 phút",
      has_file: false,
      file: null,
      stats: [
        { label: "Trạng thái", value: "Chưa kết nối runtime", meaning: "Phần phân tích tác động cấu hình chưa được nối endpoint vào dashboard này.", attention: true },
        { label: "Nguồn dữ liệu", value: "Chưa có", meaning: "Khi hoàn thiện sẽ lấy từ báo cáo phân tích cấu hình." },
        { label: "Cập nhật lúc", value: now, meaning: "Thời điểm dashboard dựng trạng thái hiện tại." },
      ],
    },
  ];
}

function groupedSystemModules(modules) {
  const sourceRows = Array.isArray(modules) ? modules : [];
  const eventScheduleByName = {
    "Bộ nhớ quyết định AI": { event: "Ghi nhớ sau mỗi quyết định", schedule: "Sau mỗi lệnh đóng", interval: "lệnh đóng" },
    "Market Regime": { event: "Ghi nhớ sau mỗi quyết định", schedule: "2 giờ/lần", interval: "2 giờ" },
    "Strategy Versioning": { event: "Ghi nhớ sau mỗi quyết định", schedule: "6h sáng", interval: "6h sáng" },
    "Replay Engine": { event: "Ghi nhớ sau mỗi quyết định", schedule: "6h sáng", interval: "6h sáng" },
    "Bunny Minimize Losses": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
    "Bunny Health Monitor": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
    "Recovery Chain Manager": { event: "Ngay khi lệnh đóng", schedule: "5 phút/lần để đối chiếu", interval: "5 phút" },
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
  const capitalModules = capitalManagementModules();
  return [
    {
      key: "ai-decision",
      icon: "🧠",
      title: "AI Decision",
      event_text: "Ghi nhớ sau mỗi quyết định",
      schedule_text: "Market Regime 2 giờ/lần, Replay & Strategy lúc 6h sáng",
      items: [
        realModules.get("Bộ nhớ quyết định AI"),
        realModules.get("Market Regime"),
        realModules.get("Strategy Versioning"),
        realModules.get("Replay Engine"),
      ].filter(Boolean),
    },
    {
      key: "risk-management",
      icon: "🛡",
      title: "Risk Management",
      event_text: "Ngay khi lệnh đóng",
      schedule_text: "5 phút/lần để đối chiếu",
      items: [
        realModules.get("Bunny Minimize Losses"),
        realModules.get("Bunny Health Monitor"),
        realModules.get("Recovery Chain Manager"),
      ].filter(Boolean),
    },
    {
      key: "capital-management",
      icon: "💰",
      title: "Capital Management",
      event_text: "Sau nạp/rút, sau lệnh đóng",
      schedule_text: "5 phút/lần đồng bộ OKX",
      items: capitalModules,
    },
    {
      key: "ai-optimization",
      icon: "⚙️",
      title: "AI Optimization",
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
  refs.systemModuleGrid.innerHTML = "";
  groups.forEach((group) => {
    const section = document.createElement("section");
    section.className = "module-group module-group-card";
    section.dataset.groupKey = group.key;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "module-group-toggle";
    toggle.setAttribute("aria-expanded", "true");
    toggle.innerHTML = `
      <div class="module-group-head">
        <strong>${escapeHtml(group.icon)} ${escapeHtml(group.title)}</strong>
        <span>${group.items.length}</span>
      </div>
      <div class="module-group-schedule">
        <span><b>Khi có sự kiện:</b> ${escapeHtml(group.event_text || "-")}</span>
        <span><b>Theo lịch:</b> ${escapeHtml(group.schedule_text || "-")}</span>
      </div>
    `;

    const body = document.createElement("div");
    body.className = "module-group-body module-card-body";

    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", expanded ? "false" : "true");
      body.hidden = expanded;
    });

    group.items.forEach((module) => {
      const configFileLabel = module.has_file ? (module.file?.file_name || "-") : "Chưa có file .txt cấu hình";
      const updateIntervalLabel = module.update_interval || module.update_schedule || module.update_event || "-";
      const runtimeUpdatedLabel = moduleUpdatedLabel(module);
      const attentionRows = Array.isArray(module.stats) ? module.stats.filter((row) => row && row.attention) : [];
      const attentionLabel = attentionRows.length
        ? `${attentionRows.length} biến cần chú ý`
        : module.status === "warn" ? "Cần kiểm tra" : "Ổn";
      const card = document.createElement("button");
      card.type = "button";
      card.className = `module-item module-card ${module.status || "warn"}`;
      card.innerHTML = `
        <div class="module-item-head">
          <span class="module-number">#${escapeHtml(module.group_order || "-")}</span>
          <span class="module-item-status ${module.status || "warn"}">${moduleStatusLabel(module.status)}</span>
        </div>
        <div class="module-item-main">
          <strong>${escapeHtml(module.name || "-")}</strong>
          <small>${escapeHtml(module.purpose || "-")}</small>
          <span class="module-update-note"><b>Thời gian cập nhật:</b> <span class="module-update-value">${escapeHtml(runtimeUpdatedLabel || "-")}</span><em class="module-update-badge">mỗi ${escapeHtml(updateIntervalLabel)}/1 lần</em></span>
          <span class="module-file"><b>File cấu hình:</b> <span class="module-file-value" title="${escapeHtml(configFileLabel)}">${escapeHtml(configFileLabel)}</span></span>
          <span class="module-row-attention ${attentionRows.length || module.status === "warn" ? "warn" : "ok"}">${escapeHtml(attentionLabel)}</span>
        </div>
      `;
      card.addEventListener("click", () => {
        refs.systemModuleGrid.querySelectorAll(".module-item").forEach((item) => item.classList.remove("selected"));
        card.classList.add("selected");
        renderModuleDetail(module);
      });
      body.appendChild(card);
    });

    section.appendChild(toggle);
    section.appendChild(body);
    refs.systemModuleGrid.appendChild(section);
  });
  closeSystemModuleDetail();
}

function renderSystemChecklist(payload) {
  const data = payload || {};
  const items = data.criteria || data.items || [];
  if (refs.systemChecklistStatus) {
    refs.systemChecklistStatus.textContent = `${data.ok_count || 0}/${data.total || items.length} OK - ${data.date || "-"}`;
  }
  if (refs.systemChecklistGrid) {
    refs.systemChecklistGrid.innerHTML = "";
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
      refs.systemChecklistGrid.appendChild(card);
    });
  }
  renderSystemModules(data.modules || []);
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
  if (moduleNumber === 5) {
    const keep = new Set(["historysamples", "currentconfidence", "bullpercent", "bearpercent", "sidewaypercent", "highvolatilitypercent", "lowvolatilitypercent"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber === 8) {
    const keep = new Set(["recovery_step", "cycle_pnl_usdt", "next_margin_usdt", "blocked", "processed_keys_count", "last_realized_net_pnl", "last_loss_recorded"]);
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
  }
  if (moduleNumber !== 1) return sourceRows;
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
    return sourceRows.filter((row) => keep.has(viLabel(row?.label || "")));
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
    return label.includes("ngay kiem tra") || label.includes("cap nhat luc");
  });
}

function moduleTrendMeaning(row) {
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
renderModuleDetail = function renderModuleDetailPatched(module) {
  renderModuleDetailOriginal(module);
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

async function loadSystemChecklist(date = "") {
  const url = date ? `/api/system-checklist?date=${encodeURIComponent(date)}` : "/api/system-checklist";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const payload = await res.json();
  state.previousSystemChecklistPayload = state.lastSystemChecklistPayload;
  state.lastSystemChecklistPayload = payload;
  renderSystemChecklist(payload);
  if (refs.systemChecklistDate && payload.date) refs.systemChecklistDate.value = payload.date;
}

async function loadLcPipeline() {
  const res = await fetch(`/api/lc-pipeline?_=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderLcPipelineEnhanced(await res.json());
}

function startLcPipelineRefresh() {
  if (state.lcPipelineTimer) clearInterval(state.lcPipelineTimer);
  loadLcPipeline().catch((err) => setStatus(`Lỗi LC pipeline: ${err.message}`));
  state.lcPipelineTimer = setInterval(
    () => loadLcPipeline().catch((err) => setStatus(`Lỗi LC pipeline: ${err.message}`)),
    60000,
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
  const res = await fetch("/api/prices");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderPricePulse(await res.json());
}

async function loadDecision() {
  const res = await fetch("/api/decision");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const payload = await res.json();
  if (!payload.report_exists || !payload.decision) {
    setStatus("Chưa có báo cáo");
    renderPaperState(payload.paper_state);
    return false;
  }
  renderDecision(payload.decision);
  renderPaperState(payload.paper_state);
  setStatus(`Bao cao: ${payload.report_path}`);
  return true;
}

async function loadOkxDemoStatus() {
  const res = await fetch("/api/okx-demo-status");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderOkxDemoStatus(await res.json());
}

async function loadAutomationStatus() {
  const res = await fetch("/api/automation-status");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderAutomationStatus(await res.json());
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

async function loadOkxPositions() {
  const res = await fetch("/api/okx-positions");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  renderOkxPositions(await res.json());
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
  loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`));
  state.priceTimer = setInterval(() => loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`)), 60000);
}

function startPaperAutoScan() {
  if (state.paperTimer) clearInterval(state.paperTimer);
  const seconds = Math.max(60, Number(state.paperIntervalSeconds || 600));
  const refresh = () => {
    loadDecision().catch((err) => setStatus(`Lỗi: ${err.message}`));
    loadAutomationStatus().catch((err) => setStatus(`Lỗi auto server: ${err.message}`));
    loadOkxPositions().catch((err) => setStatus(`Lỗi vi the OKX: ${err.message}`));
    loadLcPipeline().catch((err) => setStatus(`Lỗi LC pipeline: ${err.message}`));
    loadSystemChecklist().catch((err) => setStatus(`Lỗi system health: ${err.message}`));
    loadPrices().catch((err) => setStatus(`Lỗi gia: ${err.message}`));
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
  loadOkxPositions().catch((err) => setStatus(`Lỗi vi the OKX: ${err.message}`));
  loadLcPipeline().catch((err) => setStatus(`Lỗi LC pipeline: ${err.message}`));
  loadSystemChecklist().catch((err) => setStatus(`Lỗi system health: ${err.message}`));
});
refs.analyzeBtn.addEventListener("click", runAnalysis);
refs.autoRun.addEventListener("change", resetAutoTimer);
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
if (refs.systemChecklistToggle && refs.systemChecklistBody) {
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

loadConfigSummary().catch((err) => {
  setLeverageStatus(`Lỗi: ${err.message}`, "warn");
  setOrderMarginStatus(`Lỗi: ${err.message}`, "warn");
});
startLcPipelineRefresh();
loadDecision()
  .then((exists) => {
    if (!exists) runAnalysis();
    loadOkxDemoStatus()
      .catch((err) => setStatus(`Lỗi OKX demo: ${err.message}`))
      .finally(() => {
        loadAutomationStatus().catch((err) => setStatus(`Lỗi auto server: ${err.message}`));
        loadOkxPositions().catch((err) => setStatus(`Lỗi vi the OKX: ${err.message}`));
        loadSystemChecklist().catch((err) => setStatus(`Lỗi system health: ${err.message}`));
        startPaperAutoScan();
      });
  })
  .catch((err) => setStatus(`Lỗi: ${err.message}`));
startPricePulse();

