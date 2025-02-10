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
INITIAL_CAPITAL = 300  # 初始資金
TRADE_FEE = 0.00075  # 交易手續費
SLIPPAGE_TOLERANCE = 0.002  # 滑點容忍度
MIN_PROFIT_THRESHOLD = 0.01  # 最小利潤閾值
MIN_TRADE_AMOUNT = 10  # 最小交易金額(USDT)
MAX_TRADE_AMOUNT = 1000  # 最大交易金額(USDT)

# ✅ 高流動性幣的交易路徑設置
TRADE_PATHS = [
    ['USDT', 'BTC', 'ETH', 'USDT'],
    ['USDT', 'ETH', 'BTC', 'USDT'],
    ['USDT', 'BNB', 'BTC', 'USDT'],
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

# ✅ Telegram 日誌處理器
class TelegramLoggingHandler(logging.Handler):
    def __init__(self, token, chat_id):
        super().__init__()
        self.token = token
        self.chat_id = chat_id
        
    def emit(self, record):
        log_message = f"🔔 {record.levelname}\n{self.format(record)}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        self.send_telegram_message(log_message)

    def send_telegram_message(self, message):
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=5)
        except requests.exceptions.RequestException as e:
            print(f"Telegram發送失敗: {e}")

# ✅ 初始化系統
try:
    check_env_vars()
    
    app = Flask(__name__)

    client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"), testnet=True)

    # 獲取可用交易對並檢查所需的交易對是否存在
    exchange_info = client.get_exchange_info()
    symbols = [s['symbol'] for s in exchange_info['symbols']]
    logging.info("可用的交易對: %s", symbols)

    required_symbols = ['USDTBTC', 'BTCETH', 'ETHUSDT', 'USDTBNB', 'BNBBTC']
    missing_symbols = [symbol for symbol in required_symbols if symbol not in symbols]
    if missing_symbols:
        raise ValueError(f"缺少必要的交易對: {', '.join(missing_symbols)}")

    creds_info = json.loads(os.getenv('GOOGLE_CREDENTIALS_JSON'))
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gsheet = gspread.authorize(creds).open_by_key(os.getenv("GOOGLE_SHEET_ID")).sheet1

    telegram_handler = TelegramLoggingHandler(os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID'))
    telegram_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(telegram_handler)

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
    symbols = ["btcusdt", "ethusdt", "bnbusdt"]
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
    amount = INITIAL_CAPITAL
    initial_amount = amount

    for i in range(len(path) - 1):
        symbol = f"{path[i]}{path[i+1]}".lower()
        price = prices.get(symbol)

        if not price:
            logging.warning(f"⚠️ 缺少 {symbol} 的價格")
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
        return True
    else:
        logging.warning("⚠️ 沒有套利機會")
        return False

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
