# Образ Playwright уже містить Chromium і всі системні бібліотеки — інакше
# збір з OLX не працює, бо цей сайт віддає дані лише справжньому браузеру.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_URL=sqlite:////data/realty.db \
    OPS_DB_URL=sqlite:////data/ops.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Каталог даних монтується постійним диском: без цього база зникає при
# кожному редеплої, а разом із нею — уся історія цін.
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["python", "cli.py", "serve", "--host", "0.0.0.0"]
