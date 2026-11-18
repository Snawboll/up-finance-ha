# Plain multi-arch Python image: no Home Assistant base image needed because the
# app uses only the Python standard library.
FROM python:3.12-alpine

RUN apk add --no-cache tzdata

WORKDIR /opt/up_finance
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

CMD ["python", "-m", "app.main"]
