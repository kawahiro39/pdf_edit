FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        libnss3 \
        libnspr4 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxtst6 \
        libglib2.0-0 \
        libgtk-3-0 \
        libdrm2 \
        libgbm1 \
        libatspi2.0-0 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        fonts-liberation \
        fonts-ubuntu \
        fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
