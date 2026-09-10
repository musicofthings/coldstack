FROM python:3.11-slim

# psycopg[binary] ships its own libpq, so no build toolchain is needed here.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY coldstack ./coldstack
COPY pytest.ini .

# Never run as root in a container that holds other people's API keys.
RUN useradd --create-home --uid 10001 coldstack && chown -R coldstack /app
USER coldstack

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "coldstack.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
