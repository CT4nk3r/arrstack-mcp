FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ard.py config.py doctor.py discover.py wizard.py setup_ui.py setup.html ./

RUN useradd -m -u 1000 appuser && mkdir -p /config && chown -R appuser /app /config
# Settings saved from the /setup page land here; mount /config to keep them.
ENV ARRSTACK_CONFIG=/config/arrstack.toml
USER appuser

EXPOSE 8000
ENTRYPOINT ["python", "server.py"]
# Streamable HTTP at http://<host>:8000/mcp (matches the README). Override with
# `--transport sse` for legacy clients that only speak SSE (endpoint /sse).
CMD ["--transport", "streamable-http"]
