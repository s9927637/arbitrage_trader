# 使用最新的 Debian 作為基礎映像
FROM python:latest

# 設定工作目錄
WORKDIR /app

# 更新系統並安裝必要的庫
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN python --version

# 複製 requirements.txt 並安裝依賴
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 複製所有檔案到容器內
COPY . .

# 確保主程式可執行
RUN chmod +x binance_arbitrage_bot.py

# 指定執行 Python 腳本
CMD ["python3", "binance_arbitrage_bot.py"]

# 開放 8080 端口
EXPOSE 8080
