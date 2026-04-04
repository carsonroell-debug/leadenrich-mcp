FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY leadenrich_mcp/ leadenrich_mcp/
COPY main.py .

RUN pip install --no-cache-dir .

EXPOSE 8300

CMD ["python", "-m", "leadenrich_mcp.server"]
