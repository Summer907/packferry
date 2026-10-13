FROM python:3.12.12-slim AS builder
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages
COPY clients ./clients
RUN python -m pip install --no-cache-dir uv==0.11.29 \
    && uv sync --locked --no-dev --no-editable

FROM python:3.12.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/packferry/.venv/bin:$PATH
WORKDIR /opt/packferry
COPY --from=builder /src/.venv ./.venv
COPY apps ./apps
COPY packages ./packages
COPY clients ./clients
COPY frontend/importer/dist ./frontend/importer/dist
RUN groupadd --system packferry && useradd --system --gid packferry --home-dir /var/lib/packferry packferry \
    && mkdir -p /var/lib/packferry && chown packferry:packferry /var/lib/packferry
USER packferry
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn apps.importer.server:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"$PACKFERRY_TRUSTED_PROXY_IPS\""]
