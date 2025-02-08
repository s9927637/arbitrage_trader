import os
import threading
import time
import logging
import gspread
import json
import numpy as np
from datetime import datetime
from binance.client import Client
from flask import Flask, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from sklearn.preprocessing import MinMaxScaler
from google.oauth2.service_account import Credentials
import requests

# 設置日誌
logging.basicConfig(filename='arbitrage_bot.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# 初始化 Flask API
app = Flask(__name__)

# 初始化套利狀態
arbitrage_is_running = False

# 設定 Binance API
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET, testnet=True)

# Google Sheets設定
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
credentials_info = json.loads(credentials_json)
scopes = ['https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_info(credentials_info, scopes=scopes)
gsheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
service = build('sheets', 'v4', credentials=creds)


# 從環境變數中獲取 Telegram Bot Token 和 Chat ID
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram_notification(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message
    }
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        logging.info("Telegram 通知已發送")
    else:
        logging.error("Telegram 通知發送失敗")

# 交易參數
TRADE_FEE = 0.00075
SLIPPAGE_TOLERANCE = 0.002
SEQ_LEN = 60  # LSTM使用60筆資料
scaler = MinMaxScaler(feature_range=(0, 1))

# 📌 取得帳戶資金
def get_account_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance["free"]) if balance else 0
    except Exception as e:
        logging.error(f"取得 {asset} 餘額失敗: {e}")
        return 0

# 📌 計算交易資金
def get_trade_amount():
    usdt_balance = get_account_balance("USDT")
    return usdt_balance * 0.8

# 📌 購買 BNB 作為手續費
def buy_bnb_for_gas():
    try:
        usdt_balance = get_account_balance("USDT")
        bnb_balance = get_account_balance("BNB")
        if bnb_balance < 0.05:  # 確保 BNB 足夠支付 Gas
            buy_amount = usdt_balance * 0.2  # 使用 20% USDT 購 BNB
            client.order_market_buy(symbol="BNBUSDT", quoteOrderQty=buy_amount)
            logging.info(f"✅ 購買 {buy_amount} USDT 的 BNB 作為手續費")
            send_telegram_notification(f"購買 {buy_amount} USDT 的 BNB 作為手續費")
    except Exception as e:
        logging.error(f"購買 BNB 失敗: {e}")
        send_telegram_notification(f"購買 BNB 失敗: {e}")

# 📌 計算套利收益
def calculate_arbitrage_profit(path):
    amount = get_trade_amount()
    for i in range(len(path) - 1):
        symbol = f"{path[i]}{path[i+1]}"
        ticker = client.get_symbol_ticker(symbol=symbol)
        if ticker:
            price = float(ticker["price"])
            amount = amount * price * (1 - TRADE_FEE)
    return amount - get_trade_amount()

# 📌 記錄交易到 Google Sheets
def log_to_google_sheets(timestamp, path, trade_amount, cost, expected_profit, actual_profit, status):
    try:
        gsheet.append_row([timestamp, " → ".join(path), trade_amount, cost, expected_profit, actual_profit, status])
        logging.info(f"✅ 交易已記錄至 Google Sheets: {timestamp}")
        send_telegram_notification(f"交易已記錄至 Google Sheets: {timestamp}")
    except Exception as e:
        logging.error(f"記錄交易到 Google Sheets 失敗: {e}")
        send_telegram_notification(f"記錄交易到 Google Sheets 失敗: {e}")

# 📌 執行套利交易
def execute_trade(path):
    trade_amount = get_trade_amount()
    expected_profit = calculate_arbitrage_profit(path)
    cost = trade_amount * TRADE_FEE
    actual_profit = 0

    try:
        for symbol in path:
            client.order_market_buy(symbol=symbol, quoteOrderQty=trade_amount)
            logging.info(f"🟢 交易完成: {symbol} ({trade_amount} USDT）")
        
        actual_profit = calculate_arbitrage_profit(path)
        status = "成功"
        send_telegram_notification(f"套利交易成功，實際獲利: {actual_profit} USDT")
    except Exception as e:
        logging.error(f"❌ 交易失敗: {e}")
        send_telegram_notification(f"套利交易失敗: {e}")
        status = "失敗"

    log_to_google_sheets(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        path,
        trade_amount,
        cost,
        expected_profit,
        actual_profit,
        status
    )

    logging.info(f"✅ 三角套利完成，實際獲利: {actual_profit} USDT")

# 📌 自動執行套利
def arbitrage():
    buy_bnb_for_gas()
    best_path, best_profit = select_best_arbitrage_path()

    if best_profit > 1:
        logging.info(f"✅ 最佳套利路徑: {' → '.join(best_path)}，預期獲利 {best_profit:.2f} USDT")
        send_telegram_notification(f"最佳套利路徑: {' → '.join(best_path)}，預期獲利 {best_profit:.2f} USDT")
        execute_trade(best_path)
    else:
        logging.info("❌ 無套利機會")
        send_telegram_notification("無套利機會")

# ✅ 讓套利交易在背景執行
def run_arbitrage():
    while True:
        arbitrage()
        time.sleep(5)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok"}), 200

# 更新套利狀態並啟動套利
@app.route('/start', methods=['GET'])
def start_arbitrage():
    global arbitrage_is_running
    arbitrage_is_running = True
    thread = threading.Thread(target=run_arbitrage, daemon=True)
    thread.start()
    
    send_telegram_notification("套利機器人已啟動")
    return jsonify({"status": "套利機器人已啟動"}), 200

# 停止套利並通知 Telegram
@app.route('/stop', methods=['GET'])
def stop_arbitrage():
    global arbitrage_is_running
    arbitrage_is_running = False
    
    send_telegram_notification("套利機器人已停止")
    return jsonify({"status": "套利機器人已停止"}), 200

# 查詢套利機器人狀態
@app.route('/status', methods=['GET'])
def get_arbitrage_status():
    if arbitrage_is_running:
        return jsonify({"status": "running", "message": "套利機器人正在運行中"}), 200
    else:
        return jsonify({"status": "idle", "message": "套利機器人閒置"}), 200

# 假設的套利運行函數
def run_arbitrage():
    while arbitrage_is_running:
        # 執行套利邏輯
        pass  # 在這裡加入你的套利邏輯
        # 模擬延遲
        time.sleep(5)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 80)))
