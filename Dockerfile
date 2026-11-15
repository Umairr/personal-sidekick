# Personal Sidekick: FastAPI + LangGraph agent + headless Playwright browser
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MCP_BIN_DIR=/opt/mcp/node_modules/.bin \
    SIDEKICK_HOST=0.0.0.0 \
    SIDEKICK_PORT=8765 \
    SIDEKICK_HEADLESS=true \
    SIDEKICK_BROWSER=chromium \
    SIDEKICK_NO_SANDBOX=true

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

# Root certificates: Node ships its own, but Python needs the system bundle for HTTPS
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# MCP servers + Chromium (with its system libraries), pinned to the versions the app expects
WORKDIR /opt/mcp
RUN npm init -y >/dev/null \
 && npm install --omit=dev --no-audit --no-fund \
      @playwright/mcp@0.0.85 @modelcontextprotocol/server-filesystem@2.3.0 \
 && npx playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.npm

# Python dependencies first, so code changes don't reinstall them
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Run as the image's built-in non-root "node" user (uid 1000, same as the default
# user on most Linux servers, so the mounted sandbox folder stays writable)
RUN mkdir -p sandbox && chown -R node:node /app/sandbox /ms-playwright
USER node

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD node -e "fetch('http://127.0.0.1:8765/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["/app/.venv/bin/python", "server.py"]
