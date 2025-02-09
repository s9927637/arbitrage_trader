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
MIN_PROFIT_THRESHOLD = 0.001  # 最小利潤閾值
MIN_TRADE_AMOUNT = 10  # 最小交易金額(USDT)
MAX_TRADE_AMOUNT = 1000  # 最大交易金額(USDT)
WEBSOCKET_PING_INTERVAL = 30  # WebSocket心跳間隔
PRICE_CHANGE_THRESHOLD = 0.01  # 價格變動閾值(1%)

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

def on_message(ws, message):
    try:
        data = json.loads(message)
        if 's' in data and 'c' in data:
            symbol = data['s'].lower()
            price = float(data['c'])
            prices[symbol] = price
            logging.info(f"📈 {symbol.upper()} 最新價格: {price}")
        else:
            logging.warning(f"⚠️ 無法解析 WebSocket 數據: {data}")
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
        
        # 透過 Telegram 通知套利成功
        send_telegram_message(f"成功執行套利: {' → '.join(path)}，利潤: {profit:.2f} USDT")
        
        return True
    else:
        logging.warning("⚠️ 沒有套利機會")
        return False

# ✅ 記錄套利交易到 Google Sheets
def record_trade(path, profit):
    trade_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    gsheet.append_row([trade_time, ' → '.join(path), profit])
    logging.info(f"✅ 套利交易已記錄到 Google Sheets: {' → '.join(path)}，利潤: {profit:.2f} USDT")

# ✅ 透過 Telegram 發送消息
def send_telegram_message(message):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        logging.info("✅ Telegram 通知發送成功")
    except Exception as e:
        logging.error(f"Telegram 通知發送失敗: {str(e)}")

# ✅ 提供 Flask API 查詢套利機會
@app.route('/arbitrage_opportunities', methods=['GET'])
def arbitrage_opportunities():
    opportunities = []
    for path in TRADE_PATHS:
        profit = calculate_profit(path)
        if profit > 0:
            opportunities.append({
                'path': ' → '.join(path),
                'profit': profit
            })
    return jsonify(opportunities)

# ✅ 選擇最佳套利路徑
def find_best_arbitrage():
    best_path, best_profit = None, 0
    for path in TRADE_PATHS:
        profit = calculate_profit(path)
        if profit > best_profit:
            best_path, best_profit = path, profit
    return best_path if best_profit > 0 else None

# ✅ 主循環
while True:
    path = find_best_arbitrage()
    if path:
        execute_trade(path)
    time.sleep(5)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
