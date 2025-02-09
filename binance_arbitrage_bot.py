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
def calculate_profit(path):
    amount = MIN_TRADE_AMOUNT
    initial_amount = amount

    for i in range(len(path) - 1):
        symbol = f"{path[i+1]}{path[i]}".lower()  # ✅ 修正交易對名稱
        price = prices.get(symbol)

        if not price:
            logging.warning(f"⚠️ 缺少 {symbol.upper()} 的價格")
            return 0

        amount *= price * (1 - TRADE_FEE)

    profit = amount - initial_amount
    return profit if profit > MIN_PROFIT_THRESHOLD else 0

# ✅ 執行交易
def execute_trade(path):
    logging.info(f"🚀 嘗試執行套利: {' → '.join(path)}")
    profit = calculate_profit(path)

    if profit > 0:
        logging.info(f"💰 套利成功，預計利潤: {profit:.2f} USDT")
        
        # 自動記錄套利交易到 Google Sheets
        record_trade(path, profit)
        
        # 透過 Telegram 通知
        send_telegram_message(f"🚀 套利成功! 路徑: {' → '.join(path)}, 預計利潤: {profit:.2f} USDT")
    else:
        logging.info(f"❌ 無利潤套利，跳過此次交易")

# ✅ 記錄交易至 Google Sheets
def record_trade(path, profit):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gsheet.append_row([timestamp, ' → '.join(path), profit])
    logging.info(f"📋 記錄交易到 Google Sheets: {' → '.join(path)} 利潤: {profit:.2f} USDT")

# ✅ 發送 Telegram 訊息
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"
        payload = {"chat_id": os.getenv("TELEGRAM_CHAT_ID"), "text": message}
        response = requests.post(url, data=payload)
        response.raise_for_status()
        logging.info(f"✅ 透過 Telegram 發送訊息: {message}")
    except Exception as e:
        logging.error(f"Telegram 訊息發送失敗: {e}")

# ✅ 啟動價格監控
threading.Thread(target=monitor_price_changes, daemon=True).start()

# ✅ Flask 路由設置
@app.route("/")
def home():
    return jsonify({"status": "OK", "message": "套利機器人運行中"})

# ✅ 啟動 Flask 應用
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
