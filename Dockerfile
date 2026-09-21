FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOT_MODE=research STATE_DIR=/app/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml ./
COPY truetrade ./truetrade
COPY scripts ./scripts
COPY main.py ./
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
CMD ["python", "-m", "truetrade.worker.mt5"]
