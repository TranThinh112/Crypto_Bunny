# Railway 24/7 + Telegram setup

This repo is prepared so the bot can run 24/7 on Railway. Do not commit real keys. Put secrets only in Railway Variables.

## Files already prepared

- `railway.json` starts the FastAPI UI with `0.0.0.0:$PORT` and checks `/healthz`.
- `config.railway.yaml` runs demo mode, scans every 5 minutes, keeps max 5 active trades, and stores state under `/data`.
- `.env.railway.example` lists the variables to copy into Railway.
- `crypto_trader/notifier.py` sends Telegram messages for startup, scans, submitted orders, and errors.
- `python -m crypto_trader deploy-check --config config.railway.yaml` validates the deploy environment without printing secrets.

## Railway steps

1. Push this folder to a private GitHub repo.
2. In Railway, create a new project from that GitHub repo.
3. Keep a single service/replica only. Do not scale horizontally, because two bot instances can submit duplicate orders.
4. Railway will read `railway.json` and run:

```bash
python -m crypto_trader ui --config config.railway.yaml --host 0.0.0.0 --port $PORT
```

5. Add a Railway Volume and mount it at:

```text
/data
```

This keeps `bot_state.sqlite`, `trades.jsonl`, and `latest_decision.json` across redeploys.

6. Add Railway Variables from `.env.railway.example`:

```dotenv
OKX_API_KEY=
OKX_SECRET=
OKX_PASSPHRASE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFY_SCANS=true
TELEGRAM_MESSAGE_THREAD_ID=
```

Leave `TELEGRAM_MESSAGE_THREAD_ID` empty unless sending into a Telegram forum topic.

Do not set `PORT`; Railway provides it.

7. Generate a public Railway domain, then open:

```text
https://your-service.up.railway.app/healthz
```

The response should include `"ok": true`.

## Telegram steps

1. Open Telegram and chat with `@BotFather`.
2. Create a bot with `/newbot`.
3. Copy the token into `TELEGRAM_BOT_TOKEN`.
4. Send one message to your new bot from your Telegram account.
5. Open this URL in a browser, replacing the token:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

6. Find `chat.id` and put it into `TELEGRAM_CHAT_ID`.

For a group, add the bot to the group, send a message in the group, then use the same `getUpdates` URL and copy the group `chat.id`.

## OKX key settings

Create the API key in OKX Demo Trading first.

Recommended permissions:

- Read
- Trade

Do not enable withdrawal permission.

Use demo keys while `config.railway.yaml` has:

```yaml
mode: demo
execution:
  enable_live: false
```

## Preflight checks

Before keys are available, this should pass the setup check and warn only about missing runtime paths/PORT:

```powershell
python -m crypto_trader deploy-check --config config.railway.yaml --allow-missing-secrets
```

After keys are added in Railway Variables, run the same check in Railway shell without `--allow-missing-secrets`:

```bash
python -m crypto_trader deploy-check --config config.railway.yaml
```

## Important operating rule

Run only one active bot instance:

- Use Railway for 24/7 operation.
- Stop the local UI when Railway is live.
- Keep Railway replicas at 1.

This avoids duplicate orders from two scanners running at the same time.

## Notification noise

To receive every scan result:

```dotenv
TELEGRAM_NOTIFY_SCANS=true
```

To receive only startup/order/error style messages:

```dotenv
TELEGRAM_NOTIFY_SCANS=false
```

## Telegram message codes

The bot sends compact trading journal messages:

- `SC #STT dd/mm/yyyy`: one scan report. `STT` resets every local day.
- `VT #STT dd/mm/yyyy`: a real/demo position was opened. `STT` never resets.
- `LC #STT dd/mm/yyyy`: a pending limit order was created. `STT` never resets.
- Local LC stays in bot memory for up to 6 hours. If it is still valid and all 5 position slots are full, the bot sends it to OKX as a limit order for 1.5 days.
- If an LC exists and fewer than 5 positions are open, the bot checks the trend again, cancels the OKX pending order if needed, and opens a direct market VT when the setup still passes.
- `LC #... da duoc gui len OKX`: a local LC was submitted to OKX as a limit order.
- `LC #... da duoc chuyen thanh VT #...`: a pending order filled and became a position.
- `LC #... da duoc huy`: a pending order was canceled because it expired, disappeared on OKX, or the setup no longer passes the latest scan.
- `PNL/SD`: every 5 hours by default, reports open position PNL and account balance.
- `Tổng kết ngày`: gửi sau khi qua ngày mới theo giờ Việt Nam, gồm SD đầu ngày, PNL lệnh đã đóng, SD cuối ngày, vị thế đang mở và lệnh chờ.

The 5-hour account report interval is configured in YAML:

```yaml
notifications:
  telegram:
    account_report_interval_seconds: 18000
    daily_summary_enabled: true
    trade_memory_limit: 100
```

## Telegram buttons

Bot messages include quick buttons:

- `📲 Dashboard`: mở màn hình chính ngay trong Telegram.
- `🔎 Scan ngay`: chạy một lần phân tích thủ công, không tự gửi lệnh.
- `📌 VT`: xem các vị thế đang mở ngay lúc bấm.
- `💵 SD`: xem số dư tài khoản, số VT đang mở và số LC đang chờ.
- `🟡 LC`: xem các lệnh chờ đang mở.
- `📊 PNL/SD`: xem báo cáo PNL/SD ngay, không cần chờ chu kỳ 5 tiếng.
- `🛡 Guard`: xem trạng thái Market Guard.
- `💰 Set USDT` và `⚙️ Đòn bẩy`: chỉnh cấu hình lệnh sau.

The scheduled scan/report messages still run as configured. Buttons are handled by Telegram polling, so no webhook is required.

Text commands also work:

```text
/ui
/dashboard
/scan
/guard
/vt
/sd
/lc
/pnl
/menu
```

## Telegram sizing controls

The bot also has Telegram buttons for order sizing:

- `💰 Set USDT`: choose the base margin USDT for future orders. This updates `position_sizing.base_margin_usdt`.
- `⚙️ Đòn bẩy`: choose leverage for future orders. Only `5x` to `25x` is accepted.

Text commands:

```text
/usdt
/usdt 5
/lev
/lev 15
/leverage 15
```

`/usdt` without a number opens the margin picker. `/lev` or `/leverage` without a number opens the leverage picker. Both apply only to new orders opened after the change.

## Win-rate gate

The sample configs set:

```yaml
strategy:
  min_win_probability_pct: 80
```

The bot still analyzes every configured symbol, but only a candidate with estimated win probability at or above this threshold can pass the risk gate. This is a strict filter, not a guarantee that trades will win 80% of the time.

## Dynamic universe and LC_OKX

The sample configs can scan up to the top 50 OKX USDT swap pairs by 24h volume:

```yaml
strategy:
  universe:
    enabled: true
    mode: top_volume_24h
    quote: USDT
    max_symbols: 50
```

Local LC orders stay internal for `pending_orders.local_max_age_hours` (default 6 hours). After that, if all checks still pass and active slots remain full, the bot submits the LC to OKX and marks it as `LC_OKX`. Telegram notifies when `LC_OKX` becomes VT, and also when an internal LC is released directly to VT. Before a new VT is submitted, the bot runs a final `pre_entry_check`.

## AI router

The bot now has two AI roles:

```yaml
ai:
  enabled: true
  internal:
    provider: openai
    model: gpt-5.4-mini
    api_key_env: OPENAI_API_KEY_INTERNAL
    market_scan_interval_seconds: 14400
    market_scan_max_symbols: 3
    market_scan_to_pending: true
    market_scan_pending_limit: 3
  okx:
    provider: openai
    model: gpt-5.5
    api_key_env: OPENAI_API_KEY_OKX
    approval_enabled: true
    ask_internal_before_entry: true
```

The bot continuously scans top-volume crypto USDT swaps on 1m/5m/1h, stores indicators, candlestick patterns, and scores in SQLite, then lets the internal role summarize the market every 4 hours. `gpt-5.4-mini` can save only 1-3 best setups as local LC. `gpt-5.5` is called only when a pending LC is about to reach OKX or become VT, and code validators still run after the model approval. LC memory is ranked by hard policy as `LC_OKX` first, then local LC, then new mini setups.
