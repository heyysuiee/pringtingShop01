FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRINTSHOP_ENVIRONMENT=production

WORKDIR /app

COPY backend/app/requirements.txt backend/app/requirements.txt
RUN pip install --no-cache-dir -r backend/app/requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY main.py ./main.py

RUN groupadd --system printshop \
    && useradd --system --gid printshop --home-dir /app printshop \
    && mkdir -p /data \
    && chown -R printshop:printshop /data

VOLUME ["/data"]
USER printshop
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
