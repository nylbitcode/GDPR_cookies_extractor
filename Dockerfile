FROM ollama/ollama:latest AS ollama_builder

FROM python:3.12-slim

COPY --from=ollama_builder /bin/ollama /usr/local/bin/ollama

RUN apt-get update && apt-get install -y curl && \
    rm -rf /var/lib/apt/lists/*
    
RUN pip install poetry
RUN poetry config virtualenvs.create false
WORKDIR /app

ENV PYTHONPATH=/app/src

COPY pyproject.toml poetry.lock* ./
RUN poetry install --only main --no-root && \
    poetry run playwright install --with-deps

COPY ./src ./src
COPY config.json .
COPY sites.csv .

RUN (ollama serve > /dev/null 2>&1 &) ; sleep 5 ; echo "--- Pulling llama3 model... ---" ; ollama pull llama3 ; pkill ollama

RUN mkdir -p /app/output /app/logs
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["poetry", "run", "main"]