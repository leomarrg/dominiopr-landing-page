# DOMINIO: imagen para DESARROLLO LOCAL (docker compose up).
# Produccion no usa Docker: corre en Lightsail con gunicorn + nginx (ver DEPLOYMENT.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # SQLite vive en un volumen, fuera del codigo montado.
    DJANGO_DB_PATH=/data/db.sqlite3 \
    DJANGO_DEBUG=True

WORKDIR /app

# Usuario sin privilegios. /data es el volumen de la base de datos.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data

# Dependencias primero: esta capa se reutiliza mientras requirements.txt no cambie.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .

USER app
EXPOSE 8000

# Migra, crea la tabla de cache (los limites de uso viven ahi) y arranca en 0.0.0.0
# para aceptar conexiones desde otras maquinas de la red.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py createcachetable && python manage.py runserver 0.0.0.0:8000"]
