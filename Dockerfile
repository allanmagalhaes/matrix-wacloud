FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy ./deploy
RUN pip install --no-cache-dir .

ENV MWC_DATA_DIR=/data
VOLUME /data
EXPOSE 29330
CMD ["matrix-wacloud", "run", "-c", "/data/config.yaml"]
