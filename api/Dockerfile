# Build context is the repo root so the shared core package can be installed.
FROM python:3.12-slim

WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core /core
RUN pip install --no-cache-dir /core

# Source for the worker bundle the setup page hands out. Shipped, not
# installed — the API never runs it, it only zips it up for the GPU machine.
COPY core /srv/bundle/core
COPY worker /srv/bundle/worker

COPY api/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
