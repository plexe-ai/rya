# Rya runtime image — the single-worker server (control plane + data plane).
# OSS self-host: `docker compose up` (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

# `s3` (boto3) is not optional in practice for a multi-process deployment: the api
# and the workers are separate containers, so bundle archives have to live in a
# shared object store. Without it `rya publish` fails with E_BUNDLE_STORE.
RUN pip install --no-cache-dir '.[api,postgres,llm,mcp,s3]'

# The agent project (rya.agent.yaml + src/agent.py) is mounted at /project.
WORKDIR /project
EXPOSE 8787

# Serves the control-plane API + webhook trigger for the mounted agent.
CMD ["rya", "serve", "--host", "0.0.0.0", "--port", "8787"]
