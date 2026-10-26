FROM python:3.10-slim

# Установка системных зависимостей, необходимых для работы Chromium в Playwright
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 wget curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и устанавливаем зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем браузер Playwright
RUN playwright install chromium

# Копируем весь остальной код бота
COPY . .

# Команда запуска бота
CMD ["python", "bot.py"]
