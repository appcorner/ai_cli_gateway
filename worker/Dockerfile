FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN mkdir -p /work /home/node/.codex && chown -R node:node /work /home/node/.codex /app
USER node
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /work
CMD ["python", "/app/main.py"]
