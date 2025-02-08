import numpy as np
import time
import gspread
from datetime import datetime
from binance.client import Client
from binance.enums import *
from sklearn.preprocessing import MinMaxScaler
from keras.models import Sequential
from keras.layers import LSTM
from oauth2client.service_account import ServiceAccountCredentials
import os

# ✅ 設定 Binance API - 使用 Zeabur 環境變數
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET, testnet=True)

# ✅ 設定 Google Sheets API - 使用 Zeabur 環境變數
SHEET_NAME = os.getenv("SHEET_NAME", "套利交易紀錄")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
gsheet = gspread.authorize(creds).open(SHEET_NAME).sheet1

# ✅ 交易參數
TRADE_FEE = 0.00075
SLIPPAGE_TOLERANCE = 0.002
SEQ_LEN = 60  # LSTM 使用 60 筆資料來預測價格
scaler = MinMaxScaler(feature_range=(0, 1))

# 📌 取得帳戶資金
def get_account_balance(asset):
    balance = client.get_asset_balance(asset=asset)
    return float(balance["free"]) if balance else 0

# 📌 計算交易資金（使用 80% 可用 USDT）
def get_trade_amount():
    usdt_balance = get_account_balance("USDT")
    return usdt_balance * 0.8

# 📌 購買 BNB 作為手續費
def buy_bnb_for_gas():
    usdt_balance = get_account_balance("USDT")
    bnb_balance = get_account_balance("BNB")

    if bnb_balance < 0.05:  # 確保 BNB 足夠支付 Gas
        buy_amount = usdt_balance * 0.2  # 使用 20% USDT 買 BNB
        client.order_market_buy(symbol="BNBUSDT", quoteOrderQty=buy_amount)
        print(f"✅ 購買 {buy_amount} USDT 的 BNB 作為手續費")

# 📌 取得歷史價格數據
def get_historical_data(symbol, interval="1m", limit=500):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return np.array([float(entry[4]) for entry in klines])  # 收盤價

# 📌 計算交易對的價格波動
def calculate_volatility(symbol, interval="1m", limit=500):
    prices = get_historical_data(symbol, interval, limit)
    return np.std(prices)  # 使用標準差作為波動性指標

# 📌 計算交易對的交易量
def calculate_volume(symbol, interval="1m", limit=500):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    volumes = [float(entry[5]) for entry in klines]  # 成交量
    return np.mean(volumes)  # 計算平均成交量

# 📌 根據價格波動和交易量選擇最佳交易對
def select_best_arbitrage_path():
    symbols = [s['symbol'] for s in client.get_exchange_info()['symbols']]
    best_path = None
    best_profit = 0

    for symbol in symbols:
        if "USDT" in symbol:  # 只選擇包含 USDT 的交易對
            volatility = calculate_volatility(symbol)
            volume = calculate_volume(symbol)
            
            # 這裡可以根據實際情況調整條件
            if volatility > 0.01 and volume > 100000:  # 高波動且成交量高
                profit = calculate_arbitrage_profit([symbol])
                if profit > best_profit:
                    best_profit = profit
                    best_path = [symbol]

    return best_path, best_profit

# 📌 計算套利收益
def calculate_arbitrage_profit(path):
    amount = get_trade_amount()
    for i in range(len(path) - 1):
        symbol = f"{path[i]}{path[i+1]}"
        price = get_historical_data(symbol)[-1]
        amount = amount * price * (1 - TRADE_FEE)
    return amount - get_trade_amount()

# 📌 記錄交易到 Google Sheets
def log_to_google_sheets(timestamp, path, trade_amount, cost, expected_profit, actual_profit, status):
    gsheet.append_row([timestamp, " → ".join(path), trade_amount, cost, expected_profit, actual_profit, status])

# 📌 執行套利交易
def execute_trade(path):
    trade_amount = get_trade_amount()
    expected_profit = calculate_arbitrage_profit(path)
    cost = trade_amount * TRADE_FEE
    actual_profit = 0  # 初始設定為 0

    try:
        for symbol in path:
            client.order_market_buy(symbol=symbol, quantity=trade_amount)
            print(f"🟢 交易完成: {symbol} ({trade_amount})")
        
        actual_profit = calculate_arbitrage_profit(path)
        status = "成功"
    except Exception as e:
        print(f"❌ 交易失敗: {e}")
        status = "失敗"

    # 📌 記錄套利交易
    log_to_google_sheets(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        path,
        trade_amount,
        cost,
        expected_profit,
        actual_profit,
        status
    )

    print(f"✅ 三角套利完成，實際獲利: {actual_profit} USDT")

# 📌 自動執行套利
def arbitrage():
    buy_bnb_for_gas()  # 先買 BNB 降低手續費
    best_path, best_profit = select_best_arbitrage_path()

    if best_profit > 1:
        print(f"✅ 最佳套利路徑: {' → '.join(best_path)}，預期獲利 {best_profit:.2f} USDT")
        execute_trade(best_path)
    else:
        print("❌ 無套利機會")

# 📌 自動復投機制
while True:
    arbitrage()
    time.sleep(5)  # 每 5 秒檢查套利機會
