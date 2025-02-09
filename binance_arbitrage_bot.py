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

# 常量定義
TRADE_FEE = 0.00075  # 交易手續費
SLIPPAGE_TOLERANCE = 0.002  # 滑點容忍度
MIN_PROFIT_THRESHOLD = 0.001  # 最小利潤閾值
MIN_TRADE_AMOUNT = 10  # 最小交易金額(USDT)
MAX_TRADE_AMOUNT = 1000  # 最大交易金額(USDT)
INITIAL_BALANCE = 100  # 初始資金
RETRY_TIMES = 3  # API調用重試次數
WEBSOCKET_PING_INTERVAL = 30  # WebSocket心跳間隔
PRICE_CHANGE_THRESHOLD = 0.01  # 價格變動閾值(1%)

TRADE_PATHS = [
    ['USDT', 'BNB', 'ETH', 'USDT'],
    ['USDT', 'BTC', 'BNB', 'USDT'],
    ['USDT', 'BTC', 'ETH', 'USDT'],
]

# Telegram日誌處理器
class TelegramLoggingHandler(logging.Handler):
    def __init__(self, token, chat_id):
        super().__init__()
        self.token = token
        self.chat_id = chat_id
        
    def emit(self, record):
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
            log_message = (
                f"{level_emojis.get(record.levelname, '')} "
                f"<b>{record.levelname}</b>\n"
                f"{self.format(record)}\n"
                f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            self.send_telegram_message(log_message)
        except Exception as e:
            print(f"Telegram發送失敗: {e}")

    def send_telegram_message(self, message):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            response = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }, timeout=10)
            
            if response.status_code != 200:
                print(f"Telegram API錯誤: {response.text}")
                
        except requests.exceptions.Timeout:
            print("Telegram請求超時")
        except requests.exceptions.RequestException as e:
            print(f"Telegram請求異常: {e}")

# 環境變數檢查
def check_environment_variables():
    required_vars = {
        "BINANCE_API_KEY": "Binance API金鑰",
        "BINANCE_API_SECRET": "Binance API密鑰",
        "GOOGLE_SHEET_ID": "Google Sheet ID",
        "GOOGLE_CREDENTIALS_JSON": "Google認證信息",
        "TELEGRAM_BOT_TOKEN": "Telegram Bot Token",
        "TELEGRAM_CHAT_ID": "Telegram Chat ID"
    }
    
    missing_vars = []
    for var, desc in required_vars.items():
        if not os.getenv(var):
            missing_vars.append(f"{desc} ({var})")
    
    if missing_vars:
        error_msg = f"❌ 缺少必要的環境變數:\n" + "\n".join(missing_vars)
        raise EnvironmentError(error_msg)

# 初始化
try:
    # 檢查環境變數
    check_environment_variables()
    
    # 初始化 Flask
    app = Flask(__name__)
    
    # 初始化 Binance 客戶端
    client = Client(
        os.getenv("BINANCE_API_KEY"),
        os.getenv("BINANCE_API_SECRET"),
        testnet=True
    )
    
    # 初始化 Google Sheets
    credentials_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
    credentials_info = json.loads(credentials_json)
    creds = service_account.Credentials.from_service_account_info(
        credentials_info, 
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gsheet = gspread.authorize(creds).open_by_key(os.getenv("GOOGLE_SHEET_ID")).sheet1
    
    # 設置日誌
    telegram_handler = TelegramLoggingHandler(
        os.getenv('TELEGRAM_BOT_TOKEN'),
        os.getenv('TELEGRAM_CHAT_ID')
    )
    telegram_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    telegram_handler.setFormatter(formatter)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(telegram_handler)
    
    # 全局變數
    arbitrage_is_running = False
    last_prices = {}  # 用於存儲上一次的價格
    
    logging.info("✅ 系統初始化成功")
    
except Exception as e:
    error_msg = f"❌ 初始化失敗: {str(e)}\n{traceback.format_exc()}"
    print(error_msg)
    raise

# 工具函數
def retry_on_error(func):
    """重試裝飾器"""
    def wrapper(*args, **kwargs):
        for attempt in range(RETRY_TIMES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == RETRY_TIMES - 1:
                    logging.error(f"❌ {func.__name__} 最終失敗: {e}")
                    raise
                logging.warning(f"⚠️ {func.__name__} 重試 {attempt + 1}/{RETRY_TIMES}")
                time.sleep(2 ** attempt)
    return wrapper

def send_telegram_message(message):
    """發送Telegram消息"""
    logging.info(message)

@retry_on_error
def get_latest_balance():
    """獲取最新餘額"""
    try:
        records = gsheet.get_all_values()
        if len(records) > 1 and records[-1][7]:
            return float(records[-1][7])
        return INITIAL_BALANCE
    except Exception as e:
        logging.error(f"❌ 無法獲取餘額: {e}")
        return INITIAL_BALANCE

@retry_on_error
def get_account_balance(asset):
    """獲取賬戶餘額"""
    try:
        balance = client.get_asset_balance(asset=asset)
        if balance:
            free_balance = float(balance['free'])
            logging.info(f"💰 {asset} 餘額: {free_balance}")
            return free_balance
        return 0
    except Exception as e:
        logging.error(f"❌ 獲取{asset}餘額失敗: {e}")
        return 0

@retry_on_error
def get_price(symbol):
    """獲取交易對價格"""
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        if ticker and 'price' in ticker:
            price = float(ticker['price'])
            
            # 檢查價格變動
            if symbol in last_prices:
                old_price = last_prices[symbol]
                change = abs(price - old_price) / old_price
                if change > PRICE_CHANGE_THRESHOLD:
                    logging.warning(
                        f"⚠️ {symbol} 價格變動較大\n"
                        f"原價: {old_price:.8f}\n"
                        f"新價: {price:.8f}\n"
                        f"變動: {change*100:.2f}%"
                    )
            
            last_prices[symbol] = price
            return price
        return None
    except Exception as e:
        logging.error(f"❌ 獲取{symbol}價格失敗: {e}")
        return None

@retry_on_error
def is_pair_tradable(pair):
    """檢查交易對是否可交易"""
    try:
        exchange_info = client.get_exchange_info()
        symbols = [s['symbol'] for s in exchange_info['symbols']]
        return pair in symbols
    except Exception as e:
        logging.error(f"❌ 檢查交易對{pair}失敗: {e}")
        return False

def calculate_profit(path):
    """計算套利利潤"""
    try:
        amount = get_latest_balance() * 0.8  # 使用80%資金
        
        if amount < MIN_TRADE_AMOUNT:
            logging.warning(f"⚠️ 交易金額{amount}低於最小限額{MIN_TRADE_AMOUNT}")
            return 0
            
        if amount > MAX_TRADE_AMOUNT:
            amount = MAX_TRADE_AMOUNT
            logging.info(f"ℹ️ 交易金額已限制在{MAX_TRADE_AMOUNT}")
        
        initial_amount = amount
        
        # 計算整個路徑的利潤
        for i in range(len(path) - 1):
            symbol = f"{path[i]}{path[i+1]}"
            
            if not is_pair_tradable(symbol):
                logging.warning(f"⚠️ 交易對{symbol}不可用")
                return 0
                
            price = get_price(symbol)
            if not price:
                logging.warning(f"⚠️ 無法獲取{symbol}價格")
                return 0
                
            # 計算滑點
            depth = client.get_order_book(symbol=symbol)
            best_price = float(depth['asks'][0][0])
            slippage = abs(best_price - price) / price
            
            if slippage > SLIPPAGE_TOLERANCE:
                logging.warning(f"⚠️ {symbol}滑點過大: {slippage*100:.2f}%")
                return 0
                
            amount = amount * price * (1 - TRADE_FEE)
            
        profit = amount - initial_amount
        profit_percentage = (profit / initial_amount) * 100
        
        if profit > 0:
            logging.info(
                f"💰 發現套利機會:\n"
                f"路徑: {' → '.join(path)}\n"
                f"預期利潤: {profit:.2f} USDT ({profit_percentage:.2f}%)"
            )
            
        return profit
        
    except Exception as e:
        logging.error(f"❌ 計算利潤失敗: {e}")
        return 0

def select_best_arbitrage_path():
    """選擇最佳套利路徑"""
    try:
        best_path = None
        best_profit = 0
        
        for path in TRADE_PATHS:
            profit = calculate_profit(path)
            if profit > best_profit:
                best_profit = profit
                best_path = path
                
        if best_path:
            logging.info(
                f"✨ 最佳套利路徑:\n"
                f"路徑: {' → '.join(best_path)}\n"
                f"預期利潤: {best_profit:.2f} USDT"
            )
            
        return best_path, best_profit
        
    except Exception as e:
        logging.error(f"❌ 選擇套利路徑失敗: {e}")
        return None, 0

@retry_on_error
def log_to_google_sheets(timestamp, path, trade_amount, cost, expected_profit, actual_profit, status):
    """記錄交易到Google Sheets"""
    try:
        final_balance = get_latest_balance() + actual_profit
        row = [
            timestamp,
            " → ".join(path),
            f"{trade_amount:.8f}",
            f"{cost:.8f}",
            f"{expected_profit:.8f}",
            f"{actual_profit:.8f}",
            status,
            f"{final_balance:.8f}"
        ]
        gsheet.append_row(row)
        logging.info("📝 交易記錄已保存")
    except Exception as e:
        logging.error(f"❌ 記錄交易失敗: {e}")

  # 交易執行
@retry_on_error
def execute_trade(path):
    """執行套利交易"""
    trade_id = f"TRADE_{int(time.time())}"
    trade_amount = get_latest_balance() * 0.8
    expected_profit = calculate_profit(path)
    cost = trade_amount * TRADE_FEE * len(path)
    actual_profit = 0

    try:
        # 交易前檢查
        if trade_amount < MIN_TRADE_AMOUNT:
            raise ValueError(f"交易金額 {trade_amount} USDT 低於最小限額 {MIN_TRADE_AMOUNT} USDT")
            
        if trade_amount > MAX_TRADE_AMOUNT:
            trade_amount = MAX_TRADE_AMOUNT
            logging.info(f"交易金額已限制在 {MAX_TRADE_AMOUNT} USDT")

        # 檢查BNB餘額
        bnb_balance = get_account_balance("BNB")
        if bnb_balance < 0.1:  # 如果BNB餘額低於0.1
            fee_amount = trade_amount * 0.2
            bnb_symbol = "BNBUSDT"
            if is_pair_tradable(bnb_symbol):
                order = client.order_market_buy(
                    symbol=bnb_symbol,
                    quoteOrderQty=fee_amount
                )
                logging.info(f"✅ 已購買 BNB 支付手續費，金額: {fee_amount} USDT")

        # 執行交易路徑
        current_amount = trade_amount
        orders = []
        
        for i in range(len(path) - 1):
            symbol = f"{path[i]}{path[i+1]}"
            
            # 檢查交易對
            if not is_pair_tradable(symbol):
                raise ValueError(f"交易對 {symbol} 不可用")
                
            # 獲取市場深度
            depth = client.get_order_book(symbol=symbol)
            best_ask = float(depth['asks'][0][0])
            
            # 檢查滑點
            current_price = get_price(symbol)
            if current_price:
                slippage = abs(best_ask - current_price) / current_price
                if slippage > SLIPPAGE_TOLERANCE:
                    raise ValueError(f"交易對 {symbol} 滑點過大: {slippage*100:.2f}%")

            # 執行訂單
            if current_amount > 0:
                order = client.order_market_buy(
                    symbol=symbol,
                    quoteOrderQty=current_amount
                )
                orders.append(order)
                current_amount = float(order['executedQty'])
                
                logging.info(
                    f"✅ {symbol} 交易成功\n"
                    f"數量: {current_amount}\n"
                    f"價格: {order['price']}"
                )
            else:
                raise ValueError(f"交易金額為0，無法執行交易")

        # 計算實際利潤
        final_amount = get_account_balance(path[-1])
        actual_profit = final_amount - trade_amount
        profit_percentage = (actual_profit / trade_amount) * 100

        # 記錄交易
        log_to_google_sheets(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            path,
            trade_amount,
            cost,
            expected_profit,
            actual_profit,
            "成功"
        )

        # 發送成功通知
        success_msg = (
            f"✅ 套利交易成功 #{trade_id}\n"
            f"路徑: {' → '.join(path)}\n"
            f"投入: {trade_amount:.2f} USDT\n"
            f"獲得: {final_amount:.2f} USDT\n"
            f"利潤: {actual_profit:.2f} USDT ({profit_percentage:.2f}%)\n"
            f"手續費: {cost:.2f} USDT"
        )
        logging.info(success_msg)

    except Exception as e:
        error_msg = (
            f"❌ 套利交易失敗 #{trade_id}\n"
            f"路徑: {' → '.join(path)}\n"
            f"錯誤: {str(e)}"
        )
        logging.error(error_msg)
        
        # 記錄失敗交易
        log_to_google_sheets(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            path,
            trade_amount,
            cost,
            expected_profit,
            0,
            "失敗"
        )
        raise

# WebSocket監控
class PriceMonitor:
    def __init__(self):
        self.ws = None
        self.last_prices = {}
        self.reconnect_count = 0
        self.max_reconnects = 5

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            symbol = data['s']
            price = float(data['p'])
            
            # 檢查價格變動
            if symbol in self.last_prices:
                old_price = self.last_prices[symbol]
                change = abs(price - old_price) / old_price
                if change > PRICE_CHANGE_THRESHOLD:
                    logging.warning(
                        f"⚠️ {symbol} 價格變動較大\n"
                        f"原價: {old_price:.8f}\n"
                        f"新價: {price:.8f}\n"
                        f"變動: {change*100:.2f}%"
                    )
            
            self.last_prices[symbol] = price
            
        except Exception as e:
            logging.error(f"處理WebSocket消息失敗: {e}")

    def on_error(self, ws, error):
        logging.error(f"WebSocket錯誤: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logging.warning(
            f"WebSocket連接關閉\n"
            f"狀態碼: {close_status_code}\n"
            f"消息: {close_msg}"
        )
        self.reconnect()

    def on_open(self, ws):
        logging.info("WebSocket連接已開啟")
        # 訂閱交易對
        subscribe_message = {
            "method": "SUBSCRIBE",
            "params": [
                "btcusdt@trade",
                "ethusdt@trade",
                "bnbusdt@trade"
            ],
            "id": 1
        }
        ws.send(json.dumps(subscribe_message))

    def reconnect(self):
        """重新連接WebSocket"""
        if self.reconnect_count < self.max_reconnects:
            self.reconnect_count += 1
            logging.info(f"嘗試重新連接 WebSocket ({self.reconnect_count}/{self.max_reconnects})")
            time.sleep(5)  # 等待5秒後重連
            self.start()
        else:
            logging.error("WebSocket重連次數超過上限，停止重連")

    def start(self):
        """啟動WebSocket監控"""
        websocket.enableTrace(True)
        self.ws = websocket.WebSocketApp(
            "wss://stream.binance.com:9443/ws",
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        
        self.ws.run_forever(
            ping_interval=WEBSOCKET_PING_INTERVAL,
            ping_timeout=10
        )

    def stop(self):
        """停止WebSocket監控"""
        if self.ws:
            self.ws.close()

      # 主要執行邏輯
def arbitrage_loop():
    """套利主循環"""
    global arbitrage_is_running
    
    while arbitrage_is_running:
        try:
            # 檢查賬戶狀態
            usdt_balance = get_account_balance("USDT")
            if usdt_balance < MIN_TRADE_AMOUNT:
                logging.warning(f"⚠️ USDT餘額不足: {usdt_balance}")
                time.sleep(60)
                continue

            # 獲取最佳套利路徑
            best_path, best_profit = select_best_arbitrage_path()
            
            if best_profit > MIN_PROFIT_THRESHOLD:
                try:
                    execute_trade(best_path)
                except Exception as e:
                    logging.error(f"執行交易失敗: {e}")
            else:
                logging.info("😴 無套利機會，等待下次檢查...")
            
            time.sleep(10)  # 避免頻繁請求
            
        except Exception as e:
            logging.error(f"套利循環出錯: {e}")
            time.sleep(30)  # 出錯後等待較長時間

# API端點
@app.route('/health', methods=['GET'])
def health_check():
    """健康檢查"""
    try:
        # 檢查各項服務狀態
        binance_status = bool(client.ping())
        sheets_status = bool(gsheet.get_all_values())
        
        status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "services": {
                "binance_api": "✅ 正常" if binance_status else "❌ 異常",
                "google_sheets": "✅ 正常" if sheets_status else "❌ 異常",
                "bot_status": "🟢 運行中" if arbitrage_is_running else "⭕ 已停止"
            },
            "balances": {
                "USDT": get_account_balance("USDT"),
                "BNB": get_account_balance("BNB")
            }
        }
        
        logging.info(f"系統狀態檢查: {json.dumps(status, ensure_ascii=False, indent=2)}")
        return jsonify(status), 200
        
    except Exception as e:
        error_status = {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }
        logging.error(f"健康檢查失敗: {e}")
        return jsonify(error_status), 500

@app.route('/start', methods=['GET'])
def start_bot():
    """啟動機器人"""
    global arbitrage_is_running
    
    try:
        if arbitrage_is_running:
            return jsonify({"message": "機器人已在運行中"}), 400
            
        # 檢查初始條件
        usdt_balance = get_account_balance("USDT")
        if usdt_balance < MIN_TRADE_AMOUNT:
            return jsonify({
                "message": f"USDT餘額不足，最少需要 {MIN_TRADE_AMOUNT} USDT",
                "current_balance": usdt_balance
            }), 400
        
        # 啟動機器人
        arbitrage_is_running = True
        
        # 啟動套利循環
        threading.Thread(target=arbitrage_loop, daemon=True).start()
        
        # 啟動價格監控
        price_monitor = PriceMonitor()
        threading.Thread(target=price_monitor.start, daemon=True).start()
        
        msg = "🚀 套利機器人已啟動"
        logging.info(msg)
        return jsonify({"message": msg}), 200
        
    except Exception as e:
        error_msg = f"啟動失敗: {e}"
        logging.error(error_msg)
        return jsonify({"error": error_msg}), 500

@app.route('/stop', methods=['GET'])
def stop_bot():
    """停止機器人"""
    global arbitrage_is_running
    
    try:
        if not arbitrage_is_running:
            return jsonify({"message": "機器人已經停止"}), 400
            
        arbitrage_is_running = False
        msg = "🛑 套利機器人已停止"
        logging.info(msg)
        return jsonify({"message": msg}), 200
        
    except Exception as e:
        error_msg = f"停止失敗: {e}"
        logging.error(error_msg)
        return jsonify({"error": error_msg}), 500

@app.route('/status', methods=['GET'])
def get_status():
    """獲取機器人狀態"""
    try:
        status = {
            "is_running": arbitrage_is_running,
            "last_check": datetime.now().isoformat(),
            "balances": {
                "USDT": get_account_balance("USDT"),
                "BNB": get_account_balance("BNB")
            },
            "trade_stats": {
                "min_trade_amount": MIN_TRADE_AMOUNT,
                "max_trade_amount": MAX_TRADE_AMOUNT,
                "profit_threshold": MIN_PROFIT_THRESHOLD
            }
        }
        
        return jsonify(status), 200
        
    except Exception as e:
        error_msg = f"獲取狀態失敗: {e}"
        logging.error(error_msg)
        return jsonify({"error": error_msg}), 500

# 主程序入口
if __name__ == '__main__':
    try:
        logging.info("🔄 系統啟動中...")
        
        # 設置日誌
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # 啟動API服務器
        port = int(os.getenv('PORT', 80))
        app.run(
            host='0.0.0.0',
            port=port,
            debug=False
        )
        
    except Exception as e:
        logging.critical(f"❌ 系統啟動失敗: {e}")
        sys.exit(1)
