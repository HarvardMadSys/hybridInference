FROM python:3.12-alpine

WORKDIR /app
COPY distributions/example/fixtures/fake-openai-provider/server.py ./server.py

ENTRYPOINT ["python", "/app/server.py"]
