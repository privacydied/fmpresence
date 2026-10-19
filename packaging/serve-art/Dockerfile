# fmpresence serve-art: signed Plex cover proxy. Standard library only.
# Build from the repo root:  docker build -f packaging/serve-art/Dockerfile -t fmpresence-art .
FROM python:3.13-alpine
WORKDIR /app
COPY fmpresence.py .
RUN adduser -D -H -u 10001 art
USER art
ENV ART_LISTEN=0.0.0.0:8765 \
    PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('ART_LISTEN','0.0.0.0:8765').rpartition(':')[2]; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/health', timeout=8).status == 200 else 1)"
ENTRYPOINT ["python", "fmpresence.py", "serve-art"]
