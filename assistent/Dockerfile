# Assistent-API (Phase 5). Gebaut von ~/docker/llm-stack/compose.yml (build: ./assistent).
# Abhaengigkeiten exakt aus uv.lock (--frozen), keine Dev-Gruppe (pytest) im Image.
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /app
# Erst nur die Abhaengigkeiten: Layer bleibt bei Code-Aenderungen im Cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY assistent ./assistent
RUN uv sync --frozen --no-dev && rm /usr/local/bin/uv
# Zustand der Kalender-Vorschlaege (ASSISTENT_ZUSTAND). compose mountet hier ein
# Named Volume; ein neues Volume uebernimmt Besitzer 1993 von diesem Verzeichnis.
RUN mkdir /zustand && chown 1993:1993 /zustand
# uid/gid 1993 = Besitzer von /mnt/data/llm/vault (deno in livesync-bridge).
# Nur so darf der Assistent schreiben; compose setzt user: zusaetzlich.
USER 1993:1993
EXPOSE 8100
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/gesund', timeout=4).status == 200 else 1)"]
CMD ["assistent", "api", "--host", "0.0.0.0", "--port", "8100"]
