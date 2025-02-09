import os
import threading
import time
import logging
import gspread
import json
from binance.client import Client
from flask import Flask, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests
import websocket
from datetime import datetime
import traceback

app = Flask(__name__)

# 用於保存機器人運行狀況的變數
is_bot_running = True  # 假設目前機器人已啟動，如果機器人有停止或崩潰，則可更改為 False


# ✅ 常量定義
TRADE_FEE = 0.00075  # 交易手續費
SLIPPAGE_TOLERANCE = 0.002  # 滑點容忍度
MIN_PROFIT_THRESHOLD = 0.0001  # 調整最小利潤閾值，降低觸發條件
MIN_TRADE_AMOUNT = 10  # 最小交易金額(USDT)
MAX_TRADE_AMOUNT = 1000  # 最大交易金額(USDT)
WEBSOCKET_PING_INTERVAL = 30  # WebSocket心跳間隔
PRICE_CHANGE_THRESHOLD = 0.001  # 價格變動閾值 (0.1%)
PRICE_CHANGE_MONITOR_INTERVAL = 60  # 價格變動檢測間隔 (秒)

# ✅ 交易路徑設置
TRADE_PATHS = [
    ['USDT', 'BNB', 'ETH', 'USDT'],
    ['USDT', 'BTC', 'BNB', 'USDT'],
    ['USDT', 'BTC', 'ETH', 'USDT'],
]

# ✅ 初始化日誌處理
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ✅ 檢查環境變數
def check_env_vars():
    required_vars = [
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "GOOGLE_SHEET_ID",
        "GOOGLE_CREDENTIALS_JSON", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
    ]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        raise EnvironmentError(f"缺少環境變數: {', '.join(missing_vars)}")

# ✅ 初始化系統
try:
    check_env_vars()
    
    app = Flask(__name__)

    client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"), testnet=True)

    # 檢查 Binance 支持的交易對
    exchange_info = client.get_exchange_info()
    available_symbols = {s['symbol'].lower() for s in exchange_info['symbols']}
    
    required_symbols = {'bnbusdt', 'btcusdt', 'ethusdt', 'ethbnb'}
    missing_symbols = required_symbols - available_symbols

    # 允許替代交易對
    alternative_pairs = {
        "usdtbnb": "bnbusdt",
        "usdtbtc": "btcusdt",
        "usdteth": "ethusdt",  # 新增替代交易對
        "ethbnb": "bnbeth"  # 更新 ethbnb 為有效交易對名稱
    }

    for pair in list(missing_symbols):
        if pair in alternative_pairs and alternative_pairs[pair] in available_symbols:
            print(f"⚠️ 找不到 {pair}，將使用 {alternative_pairs[pair]} 代替")
            missing_symbols.remove(pair)

    if missing_symbols:
        raise ValueError(f"缺少必要的交易對: {', '.join(missing_symbols)}")

    # Google Sheets 連接
    creds_info = json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gsheet = gspread.authorize(creds).open_by_key(os.getenv("GOOGLE_SHEET_ID")).sheet1

    logging.info("✅ 系統初始化成功")

except Exception as e:
    error_msg = f"❌ 初始化失敗: {str(e)}\n{traceback.format_exc()}"
    print(error_msg)
    raise

# ✅ 賬戶餘額檢查與購買BNB
def check_balance_and_buy_bnb():
    try:
        # 查詢賬戶餘額
        account_info = client.get_account()
        usdt_balance = 0
        for asset in account_info['balances']:
            if asset['asset'] == 'USDT':
                usdt_balance = float(asset['free'])
        
        if usdt_balance < MIN_TRADE_AMOUNT:
            logging.warning(f"⚠️ 賬戶USDT餘額不足，無法進行交易 (USDT餘額: {usdt_balance})")
            return

        # 計算20%的USDT餘額來購買BNB
        buy_amount_usdt = usdt_balance * 0.2  # 購買20%的USDT餘額
        bnb_price = prices.get('bnbusdt')

        if not bnb_price:
            logging.warning("⚠️ 無法獲取BNB價格，無法進行購買")
            return

        # 計算購買的BNB數量
        bnb_quantity = buy_amount_usdt / bnb_price
        bnb_quantity = round(bnb_quantity, 2)  # 保留兩位小數
        
        if bnb_quantity < 0.01:
            logging.warning("⚠️ 計算出的BNB數量過少，無法進行購買")
            return

        # 發送購買BNB的訂單
        logging.info(f"🚀 嘗試購買 {bnb_quantity} BNB，總價: {buy_amount_usdt} USDT")
        order = client.order_market_buy(symbol='bnbusdt', quantity=bnb_quantity)
        logging.info(f"✅ 成功購買 {bnb_quantity} BNB，訂單詳細信息: {order}")

    except Exception as e:
        logging.error(f"查詢餘額或購買BNB時發生錯誤: {e}")

# ✅ 定時檢查賬戶餘額並進行BNB購買
def monitor_and_buy_bnb():
    while True:
        check_balance_and_buy_bnb()
        time.sleep(3600)  # 每小時檢查一次餘額並購買BNB

# ✅ 啟動購買BNB監控
threading.Thread(target=monitor_and_buy_bnb, daemon=True).start()

# ✅ WebSocket 監聽價格
prices = {}
last_prices = {}
last_logged_time = time.time()

def on_message(ws, message):
    global last_logged_time
    try:
        data = json.loads(message)
        if 's' in data and 'c' in data:
            symbol = data['s'].lower()
            price = float(data['c'])
            prices[symbol] = price
            
            # 設置每 30 秒記錄一次價格
            current_time = time.time()
            if current_time - last_logged_time >= 30:  # 每 30 秒記錄一次
                logging.info(f"📈 {symbol.upper()} 最新價格: {price}")
                last_logged_time = current_time

            # 價格變動檢測
            if symbol in last_prices:
                last_price = last_prices[symbol]
                price_change = abs(price - last_price) / last_price
                if price_change >= PRICE_CHANGE_THRESHOLD:
                    logging.info(f"📉 {symbol.upper()} 價格變動超過 {PRICE_CHANGE_THRESHOLD * 100}%: {last_price} → {price}")
                    # 可以加入額外條件來觸發套利計算，例如進行套利計算
                    for path in TRADE_PATHS:
                        if path[0] == symbol.split('usdt')[0].upper():
                            logging.info(f"📊 開始執行套利計算: {' → '.join(path)}")
                            execute_trade(path)
            last_prices[symbol] = price
        else:
            logging.warning(f"⚠️ 無法解析 WebSocket 數據: {data}")
    except json.JSONDecodeError:
        logging.error("⚠️ 收到無法解析的訊息，無法轉換為 JSON 格式")
    except Exception as e:
        logging.error(f"WebSocket 處理錯誤: {str(e)}")

def on_error(ws, error):
    logging.error(f"WebSocket 錯誤: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.warning("WebSocket 連線關閉，嘗試重連...")
    time.sleep(5)
    start_websocket()

def on_open(ws):
    symbols = ["bnbusdt", "btcusdt", "ethusdt", "ethbnb"]  # ✅ 訂閱所有套利交易對
    payload = {
        "method": "SUBSCRIBE",
        "params": [f"{symbol}@ticker" for symbol in symbols],
        "id": 1
    }
    ws.send(json.dumps(payload))
    logging.info("✅ WebSocket 已連接，監聽市場價格")

def start_websocket():
    ws = websocket.WebSocketApp("wss://stream.binance.com:9443/ws",
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.on_open = on_open
    ws.run_forever()

threading.Thread(target=start_websocket, daemon=True).start()

# ✅ 價格變動檢測
def monitor_price_changes():
    global last_prices
    while True:
        for symbol, current_price in prices.items():
            if symbol in last_prices:
                last_price = last_prices[symbol]
                price_change = abs(current_price - last_price) / last_price
                if price_change >= PRICE_CHANGE_THRESHOLD:
                    logging.info(f"📉 {symbol.upper()} 價格變動超過 {PRICE_CHANGE_THRESHOLD * 100}%: {last_price} → {current_price}")
                    # 可以加入額外條件來觸發某些操作，例如進行套利檢查
                    for path in TRADE_PATHS:
                        if path[0] == symbol.split('usdt')[0].upper():
                            logging.info(f"📊 開始執行套利計算: {' → '.join(path)}")
                            execute_trade(path)
            last_prices[symbol] = current_price
        time.sleep(PRICE_CHANGE_MONITOR_INTERVAL)

# ✅ 計算套利利潤
def execute_trade(path):
    logging.info(f"🚀 嘗試執行套利: {' → '.join(path)}")
    profit = calculate_profit(path)

    # 確保交易金額符合限制
    if profit < MIN_PROFIT_THRESHOLD:
        logging.info(f"❌ 無利潤套利，跳過此次交易")
        return

    # 計算交易金額（根據最大交易金額和可用資金進行調整）
    trade_amount = min(MAX_TRADE_AMOUNT, profit)
    if trade_amount < MIN_TRADE_AMOUNT:
        logging.info(f"❌ 交易金額低於最小限制，跳過此次交易")
        return

    logging.info(f"💰 套利成功，預計利潤: {profit:.2f} USDT")

    # 自動記錄套利交易到 Google Sheets
    record_trade(path, profit)
        
    # 透過 Telegram 通知
    send_telegram_message(f"🚀 套利成功! 路徑: {' → '.join(path)}, 預計利潤: {profit:.2f} USDT")


# ✅ 啟動價格變動檢測
threading.Thread(target=monitor_price_changes, daemon=True).start()

@app.route("/health")
def health():
    return jsonify({"status": "ok", "message": "套利機器人正在運行中"})

@app.route("/status")
def status():
    # 可以根據具體情況進行調整，這裡假設用變數 `is_bot_running` 來表示機器人狀態
    bot_status = "運行中" if is_bot_running else "未啟動"
    
    # 根據實際需求，可以將這些訊息存儲在外部系統中，或者自動更新
    return jsonify({
        "bot_status": bot_status,
        "uptime": "已運行 24 小時",  # 示例，可根據實際情況自動計算
        "message": "機器人運行狀態查詢"
    })

# ✅ 啟動 Flask 應用
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
