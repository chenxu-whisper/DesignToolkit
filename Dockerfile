FROM node:22-alpine AS frontend-builder

WORKDIR /app/web/frontend

COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ ./
ARG VITE_HOSTED_MODE=true
ENV VITE_HOSTED_MODE=${VITE_HOSTED_MODE}
RUN npm run build


FROM ghcr.io/astral-sh/uv:0.8.2 AS uv


FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

COPY --from=frontend-builder /app/web/frontend/dist/ ./web/frontend/dist/

RUN groupadd --system designtoolkit \
    && useradd --system --gid designtoolkit --home-dir /app designtoolkit \
    && mkdir -p /data \
    && chown -R designtoolkit:designtoolkit /app /data

USER designtoolkit

EXPOSE 8765

CMD ["designtoolkit", "web", "--host", "0.0.0.0", "--port", "8765"]
