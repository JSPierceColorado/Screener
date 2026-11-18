FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY marketwide_common.py marketwide_screener.py marketwide_buyer.py marketwide_service.py ./
CMD ["python", "-u", "marketwide_service.py"]
