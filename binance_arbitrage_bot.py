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
from datetime import datetime
import websocket
import json as ws_json

# 設置日誌
logging.basicConfig(filename='arbitrage_bot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# 初始化 Flask API
app = Flask(__name__)

# 設定 Binance API
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET, testnet=True)

# Google Sheets 設定
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
if not credentials_json:
    raise ValueError("⚠️ GOOGLE_CREDENTIALS_JSON 環境變數未設置！")
credentials_info = json.loads(credentials_json)
scopes = ['https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_info(credentials_info, scopes=scopes)
gsheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
service = build('sheets', 'v4', credentials=creds)

# Telegram Bot 設定
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# 交易參數
TRADE_FEE = 0.00075  # 交易手續費
SLIPPAGE_TOLERANCE = 0.002  # 滑點容忍度
TRADE_PATHS = [
    ['USDT', 'BNB', 'ETH', 'USDT'],
    ['USDT', 'BTC', 'BNB', 'USDT'],
    ['USDT', 'BTC', 'ETH', 'USDT'],
]

# 初始資金設定
INITIAL_BALANCE = 100  # 初始資金 100 USDT
arbitrage_is_running = False

# 📌 發送 Telegram 訊息
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        response = requests.post(url, data=data)
        if response.status_code != 200:
            logging.error(f"發送 Telegram 訊息失敗: {response.text}")
    except Exception as e:
        logging.error(f"Telegram 發送失敗: {e}")

# 📌 取得最新交易資金（Google Sheets 第 8 欄）
def get_latest_balance():
    try:
        records = gsheet.get_all_values()
        if len(records) > 1 and records[-1][7]:  # 確保第 8 欄有數值
            return float(records[-1][7])
        return INITIAL_BALANCE
    except Exception as e:
        logging.error(f"無法從 Google Sheets 取得資金: {e}")
        return INITIAL_BALANCE

# 📌 計算交易資金
def get_trade_amount():
    balance = get_latest_balance()
    return balance * 0.8  # 使用 80% 資金交易

# 📌 取得交易對價格
def get_price(symbol):
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        if ticker and 'price' in ticker:
            return float(ticker['price'])
        return None
    except Exception as e:
        logging.error(f"取得 {symbol} 價格失敗: {e}")
        return None

# 📌 檢查交易對是否可交易
def is_pair_tradable(pair):
    try:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols']]
        return pair in symbols
    except Exception as e:
        logging.error(f"檢查交易對 {pair} 失敗: {e}")
        return False

# 📌 計算套利利潤
def calculate_profit(path):
    amount = get_trade_amount()
    initial_amount = amount
    for i in range(len(path) - 1):
        symbol = f"{path[i]}{path[i+1]}"
        if not is_pair_tradable(symbol):
            return 0  # 交易對不可用
        price = get_price(symbol)
        if not price:
            return 0  # 無價格資訊
        amount = amount * price * (1 - TRADE_FEE)  # 扣除交易手續費
    return amount - initial_amount  # 計算利潤

# 📌 選擇最佳套利路徑
def select_best_arbitrage_path():
    best_path = None
    best_profit = 0
    for path in TRADE_PATHS:
        profit = calculate_profit(path)
        if profit > best_profit:
            best_profit = profit
            best_path = path
    return best_path, best_profit

# 📌 記錄交易到 Google Sheets
def log_to_google_sheets(timestamp, path, trade_amount, cost, expected_profit, actual_profit, status):
    try:
        final_balance = get_latest_balance() + actual_profit  # 更新資金
        gsheet.append_row([timestamp, " → ".join(path), trade_amount, cost, expected_profit, actual_profit, status, final_balance])
    except Exception as e:
        logging.error(f"記錄 Google Sheets 失敗: {e}")

# 📌 執行套利交易
def execute_trade(path):
    trade_amount = get_trade_amount()
    expected_profit = calculate_profit(path)
    cost = trade_amount * TRADE_FEE
    actual_profit = 0

    try:
        for i in range(len(path) - 1):
            symbol = f"{path[i]}{path[i+1]}"
            if is_pair_tradable(symbol):
                if trade_amount > 0:
                    client.order_market_buy(symbol=symbol, quoteOrderQty=trade_amount)
                else:
                    logging.error(f"❌ 交易金額為 0，無法執行交易: {trade_amount}")
        
        actual_profit = calculate_profit(path)
        log_to_google_sheets(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path, trade_amount, cost, expected_profit, actual_profit, "成功")
        send_telegram_message(f"✅ 套利成功!\n路徑: {' → '.join(path)}\n投入資金: {trade_amount} USDT\n預期獲利: {expected_profit:.4f} USDT\n實際獲利: {actual_profit:.4f} USDT")
    except Exception as e:
        logging.error(f"套利交易失敗: {e}")
        log_to_google_sheets(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path, trade_amount, cost, expected_profit, actual_profit, "失敗")
        send_telegram_message(f"❌ 套利失敗!\n錯誤: {e}")

# 📌 監測套利機會（持續運行）
def arbitrage_loop():
    global arbitrage_is_running
    while arbitrage_is_running:
        best_path, best_profit = select_best_arbitrage_path()
        if best_profit > 1:  # 設定套利門檻（可調整）
            execute_trade(best_path)
        else:
            logging.info("❌ 無套利機會，10 秒後重試")
            send_telegram_message("❌ 無套利機會，10 秒後重試")
        time.sleep(10)

# 📌 WebSocket 監測價格變動（實時更新）
def on_message(ws, message):
    data = ws_json.loads(message)
    if 's' in data and 'p' in data:
        symbol = data['s']
        price = float(data['p'])
        logging.info(f"接收到 {symbol} 價格更新: {price}")
        # 根據新的價格更新套利機會（如果有必要）

def on_error(ws, error):
    logging.error(f"WebSocket 錯誤: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.info("WebSocket 關閉")

def on_open(ws):
    logging.info("WebSocket 連接成功")

def start_websocket():
    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.on_open = on_open
    ws.run_forever()

# ✅ 監聽 API
@app.route('/start_arbitrage', methods=['GET'])
def start_arbitrage():
    global arbitrage_is_running
    if arbitrage_is_running:
        return jsonify({"status": "套利已在運行"}), 400
    arbitrage_is_running = True
    threading.Thread(target=arbitrage_loop).start()
    send_telegram_message("🚀 套利交易啟動!")
    return jsonify({"status": "套利交易啟動"}), 200

@app.route('/stop_arbitrage', methods=['GET'])
def stop_arbitrage():
    global arbitrage_is_running
    arbitrage_is_running = False
    send_telegram_message("🛑 套利交易已停止!")
    return jsonify({"status": "套利交易已停止"}), 200

if __name__ == '__main__':
    threading.Thread(target=start_websocket).start()
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 80)))
