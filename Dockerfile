FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ard.py ./

RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["python", "server.py"]
CMD ["--transport", "sse"]
