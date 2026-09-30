FROM node:22-alpine AS maplibre-assets

WORKDIR /build
RUN npm pack maplibre-gl@5.6.0 \
    && tar -xzf maplibre-gl-5.6.0.tgz


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
EXPOSE 8000 8100
ENV MAPLIBRE_STATIC_ROOT=/opt/maplibre/assets

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends unixodbc \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libfontconfig1 \
        libfreetype6 \
        libgbm1 \
        libglib2.0-0 \
        libjpeg62-turbo \
        libnspr4 \
        libnss3 \
        libopenjp2-7 \
        libpango-1.0-0 \
        libpng16-16 \
        libtiff6 \
        libwebp7 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=maplibre-assets /build/package/dist /opt/maplibre/assets
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[dev]"
RUN python -m playwright install chromium
COPY migrations ./migrations
COPY alembic.ini ./

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
