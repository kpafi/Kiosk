FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Eigene uid/gid nur für Kiosk: 1000 gehört auf der Box schon einem anderen Dienst.
# docker-compose.yml setzt dieselbe als user: (gilt dann auch für ältere Images beim Zurückrollen).
RUN groupadd -g 10100 kiosk && useradd -u 10100 -g kiosk -m kiosk

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
COPY sources.yaml .

# Build-Zeitstempel (deploy.sh: --build-arg KIOSK_BUILD=...), erscheint in /api/status als "build".
# Erst nach den COPY-Schritten, damit er den Cache der Abhängigkeiten nicht ungültig macht.
ARG KIOSK_BUILD=""
ENV KIOSK_BUILD=$KIOSK_BUILD

RUN mkdir -p /data && chown kiosk:kiosk /data
USER kiosk
ENV KIOSK_DATA_DIR=/data HOME=/tmp
EXPOSE 8100
# start-period: Eine Schema-Migration beim Start kann dauern (ca. 2 ms pro Artikel).
# Hängt der Sammler, beendet ein Wachhund in der App den Prozess (restart: unless-stopped).
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=4).status==200 else 1)"
# Genau EIN uvicorn-Worker: Login-Bremse im Speicher, Sammler-Schleife und die eine
# SQLite-Verbindung setzen einen einzigen Prozess voraus. --workers nicht erhöhen.
# Proxy-Header nur von Caddy akzeptieren: Der kommt über docker-proxy vom Gateway des fest
# vorgegebenen Compose-Netzes herein (docker-compose.yml: 172.19.0.0/16, Gateway 172.19.0.1).
# Ohne Server-Kopfzeile (Version) und ohne Zugriffsprotokoll: Caddy verwirft seine Logs bewusst, das
# Protokoll von uvicorn landete mit allen Suchbegriffen (?q=...) und Artikel-IDs in docker logs.
# Fehler und Meldungen der App (logging) bleiben.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.19.0.1", \
     "--no-server-header", "--no-access-log"]
