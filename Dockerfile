FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

RUN groupadd -r exporter && useradd -r -g exporter exporter

COPY --from=builder /install /usr/local
COPY copilot_premium_exporter.py /app/copilot_premium_exporter.py
COPY config.json /app/config.json

WORKDIR /app
USER exporter

EXPOSE 9185

ENTRYPOINT ["python", "copilot_premium_exporter.py"]
