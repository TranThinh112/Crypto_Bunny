const state = {
  autoTimer: null,
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
  return date.toLocaleString();
}

function intervalLabel(seconds) {
  const total = Math.max(60, Number(seconds || 60));
  const minutes = Math.max(1, Math.round(total / 60));
  return minutes === 1 ? "1 phut" : `${minutes} phut`;
}

function setBusy(isBusy) {
  state.running = isBusy;
  refs.analyzeBtn.disabled = isBusy;
  refs.refreshBtn.disabled = isBusy;
  refs.analyzeBtn.textContent = isBusy ? "Dang chay" : "Phan tich";
}

function setStatus(text) {
  refs.statusLine.textContent = text;
}

function setLeverageStatus(text, kind = "") {
  if (!refs.leverageStatus) return;
  refs.leverageStatus.textContent = text;
  refs.leverageStatus.className = kind;
}

function setOrderMarginStatus(text, kind = "") {
  if (!refs.orderMarginStatus) return;
  refs.orderMarginStatus.textContent = text;
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
    setLeverageStatus(`Toi da ${MAX_LEVERAGE}x`, "warn");
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
    setLeverageStatus(`Dang dung ${safeLeverage}x`, "ok");
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
  return String(value)
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
    BTC: { name: "Bitcoin", className: "btc", glyph: "₿" },
    ETH: { name: "Ethereum", className: "eth", glyph: "Ξ" },
    SOL: { name: "Solana", className: "sol", glyph: "SOL" },
    BNB: { name: "BNB", className: "bnb", glyph: "BNB" },
    XRP: { name: "XRP", className: "xrp", glyph: "XRP" },
    DOGE: { name: "Dogecoin", className: "doge", glyph: "Ð" },
    ADA: { name: "Cardano", className: "ada", glyph: "₳" },
    LINK: { name: "Chainlink", className: "link", glyph: "LINK" },
    AVAX: { name: "Avalanche", className: "avax", glyph: "A" },
    LTC: { name: "Litecoin", className: "ltc", glyph: "Ł" },
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
  if (state.selectedViewSymbol) return "DANG XEM";
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
    .replace(/No recent symbol-specific news confirmed the setup/, "Chua co tin gan day xac nhan setup nay")
    .replace(/Spread ([0-9.]+)% exceeds maximum ([0-9.]+)%/, "Spread $1% vuot muc toi da $2%")
    .replace(/Stop distance ([0-9.]+)% is below minimum ([0-9.]+)%/, "Khoang cach SL $1% thap hon muc toi thieu $2%")
    .replace(/Stop distance ([0-9.]+)% exceeds maximum ([0-9.]+)%/, "Khoang cach SL $1% vuot muc toi da $2%")
    .replace(/Private OKX checks skipped because API credentials are unavailable or mode is dry_run/, "Bo qua kiem tra private OKX vi chua co API key hoac dang o dry_run");
}

function reasonVi(text) {
  if (!text) return "";
  return text
    .replace("Price is above EMA50", "Gia dang nam tren EMA50")
    .replace("Price is below EMA50", "Gia dang nam duoi EMA50")
    .replace("EMA20 is above EMA50", "EMA20 dang nam tren EMA50")
    .replace("EMA20 is below EMA50", "EMA20 dang nam duoi EMA50")
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
    ctx.fillText("Chua co tin hieu", 24, 42);
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

  const direction = candidate.side === "short" ? "Co loi khi gia giam" : "Co loi khi gia tang";
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
  refs.selectedLabel.textContent = isPositionView ? "Vi the dang xem" : isViewing ? "Cap dang xem" : selected ? "Lenh duoc chon" : "Ung vien manh nhat";
  refs.selectedValue.innerHTML = candidate ? pairCellHtml(candidate.symbol) : "-";
  refs.sideValue.textContent = candidate ? `${sideLabel(candidate.side)}${isPositionView ? " - OKX demo" : isViewing ? " - dang xem" : ""}` : "-";

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

  refs.riskValue.textContent = isPositionView ? "Dang mo" : riskPassed ? "Dat" : isViewing ? "Dang xem" : "Bi chan";
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
    refs.planTag.textContent = "Chua co tin hieu";
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
  refs.planTag.textContent = isPositionView ? `${sideLabel(candidate.side)} / VI THE OKX` : isRealSelected ? sideLabel(candidate.side) : `${sideLabel(candidate.side)} / DANG XEM`;
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
        `Vi the OKX demo dang mo: ${sideLabel(candidate.side)} ${candidate.symbol}`,
        `Don bay: ${candidate.leverage ? `${fmt(candidate.leverage, 2)}x` : "-"}`,
        `Ky quy uoc tinh: ${candidate.margin_usdt ? `${fmt(candidate.margin_usdt, 2)} USDT` : "-"}`,
        `PnL chua chot: ${candidate.unrealized_pnl === null ? "-" : `${signed(candidate.unrealized_pnl, 4)} USDT`}`,
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
      loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`));
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
  if (status === "OPEN") return "DANG MO";
  if (status === "CLOSED") return "DA DONG";
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
      const message = data.enabled === false && data.message ? data.message : "Chua co vi the mo tren OKX demo";
      row.innerHTML = `<td colspan="8" class="empty-cell">${escapeHtml(message)}</td>`;
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
        row.innerHTML = `
          <td>${pairCellHtml(position.symbol)}</td>
          <td><span class="side ${sideClass}">${positionSideLabel(position.side)}</span></td>
          <td>${fmt(position.contracts, 6)}</td>
          <td>${fmt(position.entry_price, 4)}</td>
          <td>${fmt(position.mark_price, 4)}</td>
          <td>${position.leverage ? `${fmt(position.leverage, 2)}x` : "-"}</td>
          <td class="${Number(position.unrealized_pnl) >= 0 ? "pnl-up" : "pnl-down"}">${signed(position.unrealized_pnl, 4)}</td>
          <td>${position.margin_mode || "-"}</td>
        `;
        row.addEventListener("click", () => {
          state.selectedViewSymbol = position.symbol;
          state.selectedViewSource = "position";
          if (state.currentDecision) renderDecision(state.currentDecision);
          renderOkxPositions(state.currentPositionsPayload);
          loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`));
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
    refs.ordersStatus.textContent = `${orders.length} lenh cho`;
  }
  if (refs.orderRows) {
    refs.orderRows.innerHTML = "";
    if (!orders.length) {
      const row = document.createElement("tr");
      const message = data.enabled === false && data.message ? data.message : "Chua co lenh cho tren OKX demo";
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
    refs.demoStatus.textContent = `OKX demo: dang tat (${data.mode})`;
  } else if (Array.isArray(data.missing_env) && data.missing_env.length) {
    refs.demoStatus.textContent = `OKX demo: thieu ${data.missing_env.join(", ")}`;
  } else {
    refs.demoStatus.textContent = data.message || "OKX demo: chua ket noi";
  }
}

function renderAutomationStatus(status) {
  if (!refs.paperStatus || !status) return;
  const result = status.last_result || "not_started";
  const next = status.next_scan_at ? ` | lan toi ${timeLabel(status.next_scan_at)}` : "";
  if (!status.enabled) {
    refs.paperStatus.textContent = "Auto server: dang tat";
  } else if (result === "order_submitted") {
    refs.paperStatus.textContent = `Auto server: da gui lenh demo ${status.order_id || ""}${next}`;
  } else if (result === "no_order") {
    const reason = (status.risk_reasons || [])[0];
    refs.paperStatus.textContent = reason ? `Auto server: chua vao lenh - ${riskReasonVi(reason)}${next}` : `Auto server: da scan, chua co lenh${next}`;
  } else if (result === "skipped_busy") {
    refs.paperStatus.textContent = `Auto server: bo qua vi dang co scan khac${next}`;
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
    refs.pulseMove.textContent = "Dang dung gia gan nhat vi OKX tam thoi loi";
    if (state.currentDecision) renderSelected(state.currentDecision);
    return;
  }

  const previous = state.lastPrices.get(focus.symbol);
  refs.pulseSymbol.textContent = `${focus.symbol} ${fmt(current, 2)}`;
  if (previous) {
    const delta = current - previous;
    const deltaPct = previous ? (delta / previous) * 100 : 0;
    refs.pulseMove.className = movementClass(delta);
    refs.pulseMove.textContent = `${movementText(delta)} ${signed(delta, 2)} (${signed(deltaPct, 3)}%) trong 1 phut`;
    appendPulseLog({ symbol: focus.symbol, last: current, previous, createdAt: payload.created_at });
  } else {
    refs.pulseMove.className = "flat";
    refs.pulseMove.textContent = `Moc ban dau ${fmt(current, 2)}. Se so sanh sau 1 phut.`;
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
    setStatus("Chua co bao cao");
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
  setLeverageStatus("Dang luu...", "");
  try {
    const res = await fetch("/api/config/leverage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ leverage }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.detail || `HTTP ${res.status}`);
    renderConfig(payload);
    setLeverageStatus(`Da luu ${leverage}x`, "ok");
    setStatus(`Don bay moi: ${leverage}x. Ap dung tu lenh mo sau.`);
    loadDecision().catch((err) => setStatus(`Loi: ${err.message}`));
  } catch (err) {
    setLeverageStatus("Luu loi", "warn");
    setStatus(`Loi luu don bay: ${err.message}`);
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
    loadDecision().catch((err) => setStatus(`Loi: ${err.message}`));
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
  setStatus("Dang phan tich");
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
    setStatus(`Da cap nhat: ${payload.report_path}`);
    loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`));
  } catch (err) {
    setStatus(`Loi: ${err.message}`);
  } finally {
    setBusy(false);
  }
}

function startPricePulse() {
  if (state.priceTimer) clearInterval(state.priceTimer);
  loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`));
  state.priceTimer = setInterval(() => loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`)), 60000);
}

function startPaperAutoScan() {
  if (state.paperTimer) clearInterval(state.paperTimer);
  const seconds = Math.max(60, Number(state.paperIntervalSeconds || 600));
  const refresh = () => {
    loadDecision().catch((err) => setStatus(`Loi: ${err.message}`));
    loadAutomationStatus().catch((err) => setStatus(`Loi auto server: ${err.message}`));
    loadOkxPositions().catch((err) => setStatus(`Loi vi the OKX: ${err.message}`));
    loadPrices().catch((err) => setStatus(`Loi gia: ${err.message}`));
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

refs.refreshBtn.addEventListener("click", () => loadDecision().catch((err) => setStatus(`Loi: ${err.message}`)));
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
  setLeverageStatus(`Loi: ${err.message}`, "warn");
  setOrderMarginStatus(`Lỗi: ${err.message}`, "warn");
});
loadDecision()
  .then((exists) => {
    if (!exists) runAnalysis();
    loadOkxDemoStatus()
      .catch((err) => setStatus(`Loi OKX demo: ${err.message}`))
      .finally(() => {
        loadAutomationStatus().catch((err) => setStatus(`Loi auto server: ${err.message}`));
        loadOkxPositions().catch((err) => setStatus(`Loi vi the OKX: ${err.message}`));
        startPaperAutoScan();
      });
  })
  .catch((err) => setStatus(`Loi: ${err.message}`));
startPricePulse();
