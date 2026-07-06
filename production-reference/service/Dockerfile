# rooted-api — the automation layer over the Rooted brain.
# The image carries only the service; the brain (the repo's slice/) is
# bind-mounted by docker-compose at /app/slice, and ROOTED_SLICE_DIR points
# there. Same repo, same commit — nothing is vendored into the image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    ROOTED_SLICE_DIR=/app/slice

WORKDIR /app

# Dependencies first, for layer caching.
COPY service/requirements.txt /app/service/requirements.txt
RUN pip install --no-cache-dir -r /app/service/requirements.txt

COPY service/ /app/service/

EXPOSE 8000

# workers=1 is LOAD-BEARING: the in-process threading.Lock that serialises every
# queue/decision/flag mutation is only authoritative within a single worker.
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
