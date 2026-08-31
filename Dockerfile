FROM python:3.13-slim

WORKDIR /app

# uv, and the lockfile as the single source of truth. The dependency list used
# to be hand-pinned here, which was tolerable for four packages and is not for
# Cognee's tree (an LLM stack, an embedded graph database, a vector store).
# pyproject.toml plus uv.lock now say what goes in, in one place.
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra cognee

COPY manifest.py store.py cognee_store.py main.py ./

ENV CASSETTE_NAME=memory \
    PATH="/app/.venv/bin:$PATH" \
    # Cognee writes its databases and ingested files here. A volume is mounted
    # over it (compose.yaml): left on the container filesystem, every memory
    # would go away with the next redeploy.
    COGNEE_STORAGE_DIR=/var/lib/cognee

EXPOSE 9998

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9998"]
