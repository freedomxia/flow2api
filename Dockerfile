FROM python:3.11-slim

WORKDIR /app

RUN export DEBIAN_FRONTEND=noninteractive \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 -o Acquire::http::Proxy=false -o Acquire::https::Proxy=false \
    && apt-get install -y --fix-missing -o Acquire::Retries=5 -o Acquire::http::Proxy=false -o Acquire::https::Proxy=false --no-install-recommends \
    chromium \
    xvfb \
    ca-certificates \
    fonts-liberation \
    fonts-noto-cjk \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxshmfence1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY . .

ENV DISPLAY=:99 \
    CHROME_BIN=/usr/bin/chromium \
    CHROMIUM_BIN=/usr/bin/chromium

EXPOSE 8000

CMD ["sh", "-lc", "Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & exec python main.py"]
