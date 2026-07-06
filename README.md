# Crypto Signal Bot for OKX

Bot MVP này gom tin crypto, lấy dữ liệu giá OKX, chấm điểm nhiều coin, kiểm tra rủi ro, rồi chọn tối đa **1 lệnh duy nhất**. Mặc định là `dry_run`, không gửi lệnh thật.

## Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
```

Điền API key OKX vào `.env` nếu chạy `demo` hoặc `live`. API key chỉ nên có quyền `Read` + `Trade`, không cấp quyền rút tiền.

## Chạy phân tích không đặt lệnh

```powershell
python -m crypto_trader analyze --config config.yaml
```

Kết quả được ghi vào `reports/latest_decision.json`.

## Mở dashboard UI

```powershell
python -m crypto_trader ui --config config.yaml --host 127.0.0.1 --port 8000
```

Sau đó mở `http://127.0.0.1:8000`. UI có nút chạy phân tích mới và chế độ auto theo interval. UI hiện chỉ chạy phân tích, không gửi lệnh thật.

## Chạy bot một chu kỳ

```powershell
python -m crypto_trader trade --config config.yaml
```

## Ket noi OKX demo

Tao API key trong OKX Demo Trading, cap quyen `Read` + `Trade`, khong cap quyen rut tien. Dien key vao `.env`:

```dotenv
OKX_API_KEY=
OKX_SECRET=
OKX_PASSPHRASE=
```

Sau do chay UI bang config demo:

```powershell
python -m crypto_trader ui --config config.demo.yaml --host 127.0.0.1 --port 8000
```

Trong `mode: demo`, UI se hien trang thai `OKX demo: san sang` khi da co du key. Nut `Scan OKX demo` va chu ky auto 1 phut chi gui lenh khi risk gate pass; neu risk gate chan thi khong gui order.

## Chạy bot liên tục

```powershell
python -m crypto_trader run --config config.yaml --interval 900
```

Bot sẽ chạy một chu kỳ mỗi 900 giây. Dừng bằng `Ctrl+C`.

## Lưu dữ liệu và paper trading

Bot lưu trạng thái vào SQLite tại `data/bot_state.sqlite`, gồm các lần phân tích và lệnh mô phỏng. Khi mở lại UI, dữ liệu cũ sẽ được đọc lại từ `reports/latest_decision.json` hoặc SQLite nếu report JSON không còn.

UI tự gọi `paper-scan` mỗi 10 phút theo cấu hình:

```yaml
paper_trading:
  enabled: true
  auto_scan_enabled: true
  scan_interval_seconds: 600
  max_active_trades: 1
```

Mỗi lần scan, bot phân tích 5 cặp trong config, sắp xếp theo `Win Rate Est.`, chọn cặp có tỉ lệ thắng cao nhất đã qua risk gate, rồi mở một lệnh mô phỏng nếu chưa có lệnh đang mở.

Với `mode: dry_run`, bot chỉ mô phỏng. Với `mode: demo`, bot gửi lệnh demo OKX nếu có API demo hợp lệ. Với `mode: live`, bot chỉ chạy khi cả hai điều kiện cùng đúng:

- `execution.enable_live: true`
- Có file `.allow-live-trading` trong thư mục project

## Logic an toàn

Bot sẽ không vào lệnh nếu:

- Không có tín hiệu đạt `min_confidence`.
- Risk/reward thấp hơn `min_risk_reward`.
- Spread vượt `max_spread_pct`.
- Stop loss quá gần hoặc quá xa.
- Đang trong cooldown.
- Vượt giới hạn số lệnh/ngày hoặc tổng rủi ro dự kiến/ngày.
- Có position/order đang mở vượt `max_active_trades` khi kiểm tra private API khả dụng.
- Chạy live nhưng chưa bật khóa xác nhận.

## TP/SL theo phần trăm

Mặc định config mẫu đang dùng:

```yaml
exchange:
  leverage: 10

strategy:
  min_risk_reward: 1.5
  target:
    mode: roi_percent
    take_profit_pct: 75
    stop_loss_pct: 50
```

Với `mode: roi_percent`, bot hiểu TP/SL là ROI trên margin. Nếu leverage là `10x`, TP `75%` được quy đổi thành giá đi đúng hướng `7.5%`, SL `50%` thành giá đi ngược `5%`. Nếu muốn hiểu trực tiếp là phần trăm biến động giá, đổi `mode` thành `price_percent`.

Đây là công cụ hỗ trợ quyết định và tự động hóa kỹ thuật, không đảm bảo lợi nhuận. Hãy chạy demo đủ lâu trước khi bật live.

## Kiểm tra code

```powershell
python -m unittest discover -v
python -m compileall crypto_trader tests
```

## Railway 24/7 + Telegram

Repo co san `railway.json` va `config.railway.yaml`.

Railway se chay:

```bash
python -m crypto_trader ui --config config.railway.yaml --host 0.0.0.0 --port $PORT
```

Can set Railway Variables:

```dotenv
OKX_API_KEY=
OKX_SECRET=
OKX_PASSPHRASE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFY_SCANS=true
```

Nen tao Railway Volume mount vao `/data` de giu SQLite, ledger va report qua cac lan redeploy. `config.railway.yaml` dang luu vao:

```text
/data/bot_state.sqlite
/data/trades.jsonl
/data/latest_decision.json
```

Telegram se gui thong bao khi bot start, moi vong scan, khi gui order, khi bi risk gate chan, hoac khi co loi. Neu muon chi bao lenh/loi va bot bot spam, dat:

```dotenv
TELEGRAM_NOTIFY_SCANS=false
```

Tao Telegram bot bang BotFather, lay token dua vao `TELEGRAM_BOT_TOKEN`. Gui tin nhan bat ky cho bot, sau do lay chat id bang:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

Healthcheck Railway dung `/healthz`.

Huong dan day du nam o `docs/RAILWAY_TELEGRAM_SETUP.md`.

Telegram co UI bang nut inline ngay trong bot. Gui `/menu` hoac `/ui` de mo dashboard, gui `/setup` de mo nhanh phan cau hinh lenh, bam `🔎 Scan ngay` de chay mot lan phan tich thu cong, `🛡 Guard` de xem Market Guard, va cac nut `VT`, `SD`, `LC`, `PNL/SD` de xem nhanh du lieu hien tai.

Telegram co nut `💰 Set USDT` de chinh margin USDT moi lenh va nut `⚙️ Đòn bẩy` de chinh leverage cho lenh sau. Text command tuong ung:

```text
/menu
/setup
/scan
/guard
/usdt
/usdt 5
/lev
/lev 15
/leverage 15
```

Don bay chi nhan tu `5x` den `25x`. Nut `Scan ngay` chi phan tich, khong tu gui lenh; bot auto van gui lenh theo cau hinh `automation.execute_demo/live`.

Bot co bo loc `strategy.min_win_probability_pct`. Cac config mau dang dat `80`, nghia la bot van phan tich moi cap nhung chi duoc vao lenh khi ti le thang uoc tinh dat nguong. Day la bo loc risk gate, khong phai dam bao thang 80%.

Universe scan co the lay top volume 24h tren OKX:

```yaml
strategy:
  timeframe: 1m
  confirmation_timeframes:
    enabled: true
    frames: [5m, 1h]
  universe:
    enabled: true
    mode: top_volume_24h
    quote: USDT
    max_symbols: 50
    asset_class: crypto
```

LC noi bo song qua `pending_orders.local_max_age_hours` (mac dinh 6 gio) se duoc gui len OKX va doi status thanh `LC_OKX`. Khi `LC_OKX` thanh VT hoac LC noi bo duoc chuyen thang thanh VT, Telegram se thong bao ro nguon chuyen. Truoc khi gui VT, bot chay them `pre_entry_check` de doc lai VT/LC active va risk gate mot lan nua.

AI router co 2 vai tro:

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

Bot scan top 50 cap crypto USDT swap bang khung 1m/5m/1h, luu indicator, mo hinh nen va score vao SQLite. `gpt-5.4-mini` chay moi 4 tieng de tong hop thi truong va chon 1-3 setup tot nhat thanh LC noi bo. `gpt-5.5` chi duoc goi khi mot LC sap duoc dua len OKX hoac chuyen thanh VT. Sau khi 5.5 approve, code van chay validator cuoi cung truoc khi gui OKX. Bo nho LC sap xep bang policy cung: `LC_OKX -> LC noi bo -> setup mini moi`.
"# Crypto_Bunny" 
"# Crypto_Bunny" 
