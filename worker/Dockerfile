FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl bash && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex
ENV PATH="/home/node/.local/bin:${PATH}"
RUN mkdir -p /home/node/.local/bin && HOME=/home/node sh -c 'curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /home/node/.local/bin'
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY main.py .
RUN mkdir -p /work /home/node/.codex /home/node/.gemini /home/node/.config/gcloud && chown -R node:node /work /home/node/.codex /home/node/.gemini /home/node/.config /home/node/.local /app
USER node
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /work
CMD ["python", "/app/main.py"]
