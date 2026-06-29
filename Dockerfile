FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY cogs ./cogs
COPY PooperScooper.py ./

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e ".[bingo,palworld]"

COPY server_configs ./server_configs
COPY config.example.json ./config.example.json
COPY palworld_config.example.json ./palworld_config.example.json
COPY data ./data

ENV PYTHONUNBUFFERED=1

CMD ["python", "PooperScooper.py"]