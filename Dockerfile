FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RT_DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# A named volume mounted at /data inherits this ownership, so the non-root user can write to it.
RUN useradd --system --uid 10001 rt && mkdir /data && chown rt /data
USER rt
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# --proxy-headers: the only thing that can reach this port is the Caddy container, so its
# X-Forwarded-* headers (real client IP, https) are trustworthy.
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
