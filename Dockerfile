# merit-aktiva-mcp in server mode (http, sign-in with Entra or Google). See docs/server-setup.md.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 merit
USER merit
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["merit-aktiva-mcp", "--transport", "http", "--port", "8000"]
