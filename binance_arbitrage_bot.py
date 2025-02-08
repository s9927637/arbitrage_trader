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
from sklearn.preprocessing import MinMaxScaler
from google.oauth2.service_account import Credentials
import requests
from datetime import datetime

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

# Google Sheets 設定
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

# Telegram通知函數
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
SEQ_LEN = 60  # LSTM 使用60筆資料
scaler = MinMaxScaler(feature_range=(0, 1))

# 📌 取得帳戶資金 (初始資金設為100 USDT)
def get_account_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        send_telegram_notification(f"取得 {asset} 餘額: {balance['free']} USDT")
        return float(balance["free"]) if balance else 100  # 默認為100 USDT
    except Exception as e:
        logging.error(f"取得 {asset} 餘額失敗: {e}")
        send_telegram_notification(f"取得 {asset} 餘額失敗: {e}")
        return 100  # 默認為100 USDT

# 📌 計算交易資金
def get_trade_amount():
    usdt_balance = get_account_balance("USDT")
    trade_amount = usdt_balance * 0.8
    send_telegram_notification(f"計算的交易資金: {trade_amount} USDT")
    return trade_amount

# 📌 購買 BNB 作為手續費
def buy_bnb_for_gas():
    try:
        usdt_balance = get_account_balance("USDT")
        bnb_balance = get_account_balance("BNB")
        if bnb_balance < 0.05:  # 確保 BNB 足夠支付 Gas
            buy_amount = usdt_balance * 0.2  # 使用 20% USDT 購 BNB
            order = client.order_market_buy(symbol="BNBUSDT", quoteOrderQty=buy_amount)
            logging.info(f"✅ 購買 {buy_amount} USDT 的 BNB 作為手續費, 訂單信息: {order}")
            send_telegram_notification(f"購買 {buy_amount} USDT 的 BNB 作為手續費")
        else:
            logging.info("✅ BNB 充足，無需購買")
            send_telegram_notification("BNB 充足，無需購買")
    except Exception as e:
        logging.error(f"購買 BNB 失敗: {e}")
        send_telegram_notification(f"購買 BNB 失敗: {e}")

# 📌 獲取交易對價格
def get_price(symbol):
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        if ticker is None:
            logging.warning(f"警告: 交易對 {symbol} 無法取得價格")
            return None
        return float(ticker['price'])
    except Exception as e:
        logging.error(f"取得價格失敗: {e}")
        return None

# 📌 檢查交易對是否存在
def is_pair_tradable(pair):
    try:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols']]
        if pair in symbols:
            logging.info(f"交易對 {pair} 可用")
            return True
        else:
            logging.warning(f"交易對 {pair} 不可用")
            return False
    except Exception as e:
        logging.error(f"檢查交易對 {pair} 是否可用時出錯: {e}")
        return False

# 📌 計算路徑的利潤
def calculate_profit(path):
    amount = get_trade_amount()  # 假設使用 80% 的餘額進行交易
    initial_amount = amount  # 初始資金
    for i in range(len(path) - 1):
        symbol = f"{path[i]}{path[i+1]}"  # 交易對，例如 'USDTBNB'
        if not is_pair_tradable(symbol):  # 檢查交易對是否可用
            logging.warning(f"跳過不可用交易對: {symbol}")
            return 0  # 如果交易對不可用，返回 0 利潤
        price = get_price(symbol)
        if price is None:
            return 0  # 如果取得價格失敗，返回 0 利潤
        amount = amount * price * (1 - TRADE_FEE)  # 扣除交易費用
    profit = amount - initial_amount  # 計算套利收益
    return profit

# 📌 選擇最佳套利路徑
def select_best_arbitrage_path():
    TRADE_PATHS = [
        ['USDT', 'BNB', 'ETH', 'USDT'],  # 可能的三角套利路徑1
        ['USDT', 'BTC', 'BNB', 'USDT'],  # 可能的三角套利路徑2
        ['USDT', 'BTC', 'ETH', 'USDT'],  # 可能的三角套利路徑3
    ]
    
    best_path = None
    best_profit = 0
    for path in TRADE_PATHS:
        profit = calculate_profit(path)  # 計算路徑的利潤
        if profit > best_profit:  # 如果當前路徑的利潤較高，更新最佳路徑
            best_profit = profit
            best_path = path
    return best_path, best_profit

# 📌 記錄交易到 Google Sheets
def log_to_google_sheets(timestamp, path, trade_amount, cost, expected_profit, actual_profit, status):
    try:
        gsheet.append_row([timestamp, " → ".join(path), trade_amount, cost, expected_profit, actual_profit, status, actual_profit])
        logging.info(f"✅ 交易已記錄至 Google Sheets: {timestamp}")
        send_telegram_notification(f"交易已記錄至 Google Sheets: {timestamp}")
    except Exception as e:
        logging.error(f"記錄交易到 Google Sheets 失敗: {e}")
        send_telegram_notification(f"記錄交易到 Google Sheets 失敗: {e}")

# 📌 執行套利交易
def execute_trade(path):
    trade_amount = get_trade_amount()
    expected_profit = calculate_profit(path)
    cost = trade_amount * TRADE_FEE
    actual_profit = 0

    try:
        for symbol in path:
            if is_pair_tradable(symbol):  # 確保交易對可用
                order = client.order_market_buy(symbol=symbol, quoteOrderQty=trade_amount)
                logging.info(f"🟢 交易完成: {symbol} ({trade_amount} USDT），訂單訊息: {order}")
                send_telegram_notification(f"交易完成: {symbol} ({trade_amount} USDT）")
        
        actual_profit = calculate_profit(path)
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
    send_telegram_notification("即將執行套利交易，請耐心等待...")
    try:
        buy_bnb_for_gas()
        best_path, best_profit = select_best_arbitrage_path()

        if best_profit > 1:
            logging.info(f"✅ 最佳套利路徑: {' → '.join(best_path)}，預期獲利 {best_profit:.2f} USDT")
            send_telegram_notification(f"最佳套利路徑: {' → '.join(best_path)}，預期獲利 {best_profit:.2f} USDT")
            execute_trade(best_path)
        else:
            logging.info("❌ 無套利機會")
            send_telegram_notification("無套利機會")
    except Exception as e:
        logging.error(f"套利交易過程中出現錯誤: {e}")
        send_telegram_notification(f"套利交易過程中出現錯誤: {e}")


# ✅ 監聽 API
@app.route('/start_arbitrage', methods=['GET'])
def start_arbitrage():
    global arbitrage_is_running
    if arbitrage_is_running:
        return jsonify({"status": "正在執行套利交易中"}), 400
    arbitrage_is_running = True
    threading.Thread(target=arbitrage).start()
    return jsonify({"status": "套利交易已啟動"}), 200

@app.route('/stop_arbitrage', methods=['GET'])
def stop_arbitrage():
    global arbitrage_is_running
    arbitrage_is_running = False
    return jsonify({"status": "套利交易已停止"}), 200

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
