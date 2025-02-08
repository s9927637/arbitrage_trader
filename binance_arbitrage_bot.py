import os
import sys
import threading
import time
import logging
import gspread
import json
import jwt
from datetime import datetime
from binance.client import Client
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from functools import wraps, lru_cache
from concurrent.futures import ThreadPoolExecutor
import requests
import traceback

# 自定義日誌處理器
class TelegramLogHandler(logging.Handler):
    def __init__(self, bot_token, chat_id):
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id

    def emit(self, record):
        log_entry = self.format(record)
        self.send_telegram_message(log_entry)

    def send_telegram_message(self, message):
        url = f'https://api.telegram.org/bot{self.bot_token}/sendMessage'
        try:
            # 添加表情符號
            level_emojis = {
                'DEBUG': '🔍',
                'INFO': 'ℹ️',
                'WARNING': '⚠️',
                'ERROR': '❌',
                'CRITICAL': '🚨'
            }
            
            # 格式化消息
            formatted_message = (
                f"{level_emojis.get(record.levelname, '')} "
                f"<b>{record.levelname}</b>\n"
                f"{message}\n"
                f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            payload = {
                'chat_id': self.chat_id,
                'text': formatted_message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                print(f"Telegram發送失敗: {response.text}")
                
        except Exception as e:
            print(f"Telegram發送異常: {e}")

# 速率限制器
class RateLimiter:
    def __init__(self, max_requests, time_window):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []
        self.lock = threading.Lock()
        
    def can_make_request(self):
        with self.lock:
            now = time.time()
            self.requests = [req for req in self.requests if now - req < self.time_window]
            
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False

# 環境變數檢查
def check_environment_variables():
    required_env_vars = [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "GOOGLE_SHEET_ID",
        "GOOGLE_CREDENTIALS_JSON",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "JWT_SECRET"
    ]
    
    missing_vars = []
    for var in required_env_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        error_msg = f"❌ 缺少必要的環境變數: {', '.join(missing_vars)}"
        raise EnvironmentError(error_msg)

# 設置日誌系統
def setup_logging():
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

    formatter = logging.Formatter(
        '%(message)s\n'
        '位置: %(pathname)s:%(lineno)d\n'
        '函數: %(funcName)s'
    )

    telegram_handler = TelegramLogHandler(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    telegram_handler.setFormatter(formatter)
    telegram_handler.setLevel(logging.INFO)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(telegram_handler)

    return logger

# 通知函數
def notify_system_status():
    try:
        status = {
            "binance_api": bool(client.ping()),
            "google_sheets": bool(gsheet.get_all_values()),
            "usdt_balance": get_account_balance("USDT"),
            "bnb_balance": get_account_balance("BNB")
        }
        
        message = (
            "🔄 系統狀態報告\n"
            f"Binance API: {'✅' if status['binance_api'] else '❌'}\n"
            f"Google Sheets: {'✅' if status['google_sheets'] else '❌'}\n"
            f"USDT 餘額: {status['usdt_balance']:.2f}\n"
            f"BNB 餘額: {status['bnb_balance']:.2f}"
        )
        
        logging.info(message)
        
    except Exception as e:
        logging.error(f"系統狀態檢查失敗: {e}")

def notify_trade_execution(path, initial_amount, final_amount, status="成功"):
    try:
        profit = final_amount - initial_amount
        profit_percentage = (profit / initial_amount) * 100
        
        message = (
            f"{'✅' if status == '成功' else '❌'} 交易執行 {status}\n"
            f"路徑: {' → '.join(path)}\n"
            f"初始金額: {initial_amount:.2f} USDT\n"
            f"最終金額: {final_amount:.2f} USDT\n"
            f"利潤: {profit:.2f} USDT ({profit_percentage:.2f}%)"
        )
        
        logging.info(message)
        
    except Exception as e:
        logging.error(f"交易通知失敗: {e}")

def notify_risk_warning(message, details=None):
    warning_msg = f"⚠️ 風險警告: {message}"
    if details:
        warning_msg += f"\n詳細信息: {details}"
    logging.warning(warning_msg)

def notify_error(error_type, error_message, error_details=None):
    error_msg = (
        f"❌ {error_type} 錯誤\n"
        f"錯誤信息: {error_message}"
    )
    if error_details:
        error_msg += f"\n詳細信息: {error_details}"
    logging.error(error_msg)

# 錯誤處理裝飾器
def error_handler(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_msg = (
                f"❌ 錯誤發生在 {func.__name__}\n"
                f"錯誤信息: {str(e)}\n"
                f"詳細追蹤:\n{traceback.format_exc()}"
            )
            logging.error(error_msg)
            return None
    return wrapper

# 認證裝飾器
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'message': '缺少認證令牌'}), 401
        try:
            jwt.decode(token, os.getenv('JWT_SECRET'), algorithms=['HS256'])
        except:
            return jsonify({'message': '無效的認證令牌'}), 401
        return f(*args, **kwargs)
    return decorated

# API安全調用
def safe_api_call(func, *args, **kwargs):
    while not weight_limiter.can_make_request():
        time.sleep(0.1)
        
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_msg = (
                f"❌ API調用失敗: {func.__name__}\n"
                f"參數: args={args}, kwargs={kwargs}\n"
                f"嘗試次數: {attempt + 1}/{max_retries}\n"
                f"錯誤: {str(e)}"
            )
            logging.warning(error_msg)
            
            if attempt == max_retries - 1:
                logging.error(f"❌ API最終調用失敗: {error_msg}")
                raise
            
            wait_time = 2 ** attempt
            logging.info(f"等待 {wait_time} 秒後重試...")
            time.sleep(wait_time)

# 交易相關函數
@error_handler
def get_account_balance(asset):
    balance = safe_api_call(client.get_asset_balance, asset=asset)
    if balance:
        logging.info(f"💰 {asset} 餘額: {balance['free']}")
        return float(balance['free'])
    return 0

@lru_cache(maxsize=100)
def get_current_prices():
    pairs = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ETHBTC', 'BNBBTC', 'BNBETH']
    prices = {}
    
    for pair in pairs:
        try:
            ticker = safe_api_call(client.get_symbol_ticker, symbol=pair)
            prices[pair] = float(ticker['price'])
        except Exception as e:
            logging.error(f"❌ 獲取 {pair} 價格失敗: {e}")
            
    return prices

@error_handler
def check_market_conditions():
    try:
        # 檢查市場波動性
        btc_klines = safe_api_call(
            client.get_klines, 
            symbol='BTCUSDT',
            interval=Client.KLINE_INTERVAL_1HOUR,
            limit=24
        )
        
        prices = [float(k[4]) for k in btc_klines]
        volatility = (max(prices) - min(prices)) / min(prices)
        
        if volatility > MAX_VOLATILITY:
            notify_risk_warning(f"市場波動性過高: {volatility*100:.2f}%")
            return False
            
        return True
        
    except Exception as e:
        notify_error("市場條件檢查", str(e))
        return False

@error_handler
def check_risk_management():
    try:
        # 檢查賬戶餘額
        usdt_balance = get_account_balance("USDT")
        if usdt_balance < MIN_USDT_BALANCE:
            notify_risk_warning(f"USDT餘額過低: {usdt_balance}")
            return False
            
        # 檢查24小時交易量
        trades = safe_api_call(client.get_my_trades, symbol='BTCUSDT')
        daily_volume = sum(float(trade['quoteQty']) for trade in trades 
                         if int(trade['time']) > (time.time() - 86400) * 1000)
        
        if daily_volume > MAX_DAILY_VOLUME:
            notify_risk_warning(f"達到每日交易限額: {daily_volume}")
            return False
            
        # 檢查市場條件
        if not check_market_conditions():
            return False
            
        return True
        
    except Exception as e:
        notify_error("風險檢查", str(e))
        return False

@error_handler
def calculate_path_profit(path, prices):
    amount = 1.0
    for i in range(len(path)-1):
        pair = f"{path[i]}{path[i+1]}"
        if pair in prices:
            amount *= prices[pair] * (1 - TRADE_FEE)
        else:
            pair = f"{path[i+1]}{path[i]}"
            if pair in prices:
                amount *= (1/prices[pair]) * (1 - TRADE_FEE)
            else:
                return 0
    return amount - 1.0

@error_handler
def execute_trade(path):
    try:
        # 檢查餘額
        usdt_balance = get_account_balance("USDT")
        if usdt_balance < MIN_USDT_BALANCE:
            raise ValueError("USDT餘額不足")
            
        # 檢查交易對
        for i in range(len(path)-1):
            symbol = f"{path[i]}{path[i+1]}"
            ticker = safe_api_call(client.get_symbol_ticker, symbol=symbol)
            if not ticker:
                raise ValueError(f"無效的交易對: {symbol}")
        
        # 計算交易金額
        trade_amount = usdt_balance * 0.8
        current_amount = trade_amount
        
        # 執行交易
        while not order_limiter.can_make_request():
            time.sleep(0.1)
            
        for i in range(len(path)-1):
            symbol = f"{path[i]}{path[i+1]}"
            order = safe_api_call(
                client.order_market_buy,
                symbol=symbol,
                quoteOrderQty=current_amount
            )
            current_amount = float(order['executedQty'])
            logging.info(f"✅ 交易完成: {symbol}")
        
        # 記錄交易
        log_trade(path, trade_amount, current_amount)
        notify_trade_execution(path, trade_amount, current_amount)
        
    except Exception as e:
        notify_error("交易執行", str(e))
        raise

@error_handler
def log_trade(path, initial_amount, final_amount):
    try:
        profit = final_amount - initial_amount
        profit_percentage = (profit / initial_amount) * 100
        
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "->".join(path),
            f"{initial_amount:.8f}",
            f"{final_amount:.8f}",
            f"{profit:.8f}",
            f"{profit_percentage:.2f}%"
        ]
        
        gsheet.append_row(row)
        logging.info(f"📝 交易記錄已保存到Google Sheets")
        
    except Exception as e:
        notify_error("交易記錄", str(e))

  # 初始化
try:
    # 1. 檢查環境變數
    check_environment_variables()
    
    # 2. 設置日誌系統
    logger = setup_logging()
    
    # 3. 初始化 Flask
    app = Flask(__name__)
    executor = ThreadPoolExecutor(max_workers=3)
    
    # 4. 初始化速率限制器
    weight_limiter = RateLimiter(max_requests=1200, time_window=60)
    order_limiter = RateLimiter(max_requests=10, time_window=1)
    
    # 5. 初始化 Binance 客戶端
    API_KEY = os.getenv("BINANCE_API_KEY")
    API_SECRET = os.getenv("BINANCE_API_SECRET")
    client = Client(API_KEY, API_SECRET, testnet=True)
    
    # 6. 初始化 Google Sheets
    SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
    credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    credentials_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(
        credentials_info, 
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gsheet = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
    
    # 7. 設置全局變數
    arbitrage_is_running = False
    TRADE_FEE = 0.00075
    MIN_PROFIT = 0.001
    MIN_USDT_BALANCE = 100
    MAX_DAILY_VOLUME = 1000
    MAX_VOLATILITY = 0.1
    
    logging.info("✅ 系統初始化成功")
    
except Exception as e:
    error_msg = f"❌ 初始化失敗: {e}"
    print(error_msg)
    logging.critical(error_msg)
    sys.exit(1)

# API端點
@app.route('/health', methods=['GET'])
@error_handler
def health_check():
    status = {
        "service": "arbitrage_bot",
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "checks": {
            "binance_api": bool(client.ping()),
            "google_sheets": bool(gsheet.get_all_values()),
            "telegram": True,
            "risk_management": check_risk_management()
        }
    }
    logging.info(f"🏥 健康檢查: {status}")
    return jsonify(status), 200

@app.route('/start', methods=['GET'])
@require_auth
@error_handler
def start_arbitrage():
    global arbitrage_is_running
    
    if not check_risk_management():
        msg = "啟動失敗: 風險管理檢查未通過"
        notify_risk_warning(msg)
        return jsonify({"status": "error", "message": msg}), 400
        
    arbitrage_is_running = True
    thread = threading.Thread(target=run_arbitrage, daemon=True)
    thread.start()
    
    msg = "🚀 套利機器人已啟動"
    logging.info(msg)
    notify_system_status()
    
    return jsonify({"status": msg}), 200

@app.route('/stop', methods=['GET'])
@require_auth
@error_handler
def stop_arbitrage():
    global arbitrage_is_running
    arbitrage_is_running = False
    
    msg = "🛑 套利機器人已停止"
    logging.info(msg)
    return jsonify({"status": msg}), 200

@app.route('/status', methods=['GET'])
@error_handler
def get_arbitrage_status():
    status = {
        "is_running": arbitrage_is_running,
        "last_check": datetime.now().isoformat(),
        "balances": {
            "USDT": get_account_balance("USDT"),
            "BNB": get_account_balance("BNB")
        },
        "risk_status": check_risk_management(),
        "market_conditions": check_market_conditions()
    }
    
    logging.info(f"📊 系統狀態: {status}")
    return jsonify(status), 200

# 主要執行邏輯
def calculate_arbitrage_opportunity():
    prices = get_current_prices()
    paths = [
        ['BTC', 'ETH', 'BNB', 'BTC'],
        ['ETH', 'BNB', 'BTC', 'ETH'],
        ['BNB', 'BTC', 'ETH', 'BNB']
    ]
    
    best_path = None
    best_profit = 0
    
    for path in paths:
        profit = calculate_path_profit(path, prices)
        if profit > best_profit:
            best_profit = profit
            best_path = path
            
    return best_path, best_profit

def run_arbitrage():
    while arbitrage_is_running:
        try:
            # 每小時檢查系統狀態
            if datetime.now().minute == 0:
                notify_system_status()
            
            if not check_risk_management():
                notify_risk_warning("風險管理檢查未通過，暫停交易")
                time.sleep(300)  # 等待5分鐘
                continue
                
            logging.info("🔄 開始新一輪套利檢查...")
            best_path, best_profit = calculate_arbitrage_opportunity()
            
            if best_profit > MIN_PROFIT:
                msg = f"💰 發現套利機會: {' → '.join(best_path)}, 預期利潤: {best_profit*100:.2f}%"
                logging.info(msg)
                
                try:
                    initial_amount = get_account_balance("USDT")
                    execute_trade(best_path)
                    final_amount = get_account_balance("USDT")
                    notify_trade_execution(best_path, initial_amount, final_amount)
                except Exception as e:
                    notify_trade_execution(best_path, initial_amount, initial_amount, "失敗")
                    notify_error("交易執行", str(e), traceback.format_exc())
            else:
                logging.info("😴 無套利機會，等待下次檢查...")
            
            time.sleep(5)
            
        except Exception as e:
            notify_error("套利執行", str(e), traceback.format_exc())
            time.sleep(30)

if __name__ == '__main__':
    try:
        logging.info("🔄 系統啟動中...")
        notify_system_status()
        app.run(debug=False, host='0.0.0.0', port=5000)
    except Exception as e:
        notify_error("系統啟動", str(e), traceback.format_exc())
        sys.exit(1)
