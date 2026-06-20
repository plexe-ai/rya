# Rya runtime image — the single-worker server (control plane + data plane).
# OSS self-host: `docker compose up` (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir '.[api,postgres,llm]'

# The agent project (rya.agent.yaml + src/agent.py) is mounted at /project.
WORKDIR /project
EXPOSE 8787

# Serves the control-plane API + webhook trigger for the mounted agent.
CMD ["rya", "serve", "--host", "0.0.0.0", "--port", "8787"]
