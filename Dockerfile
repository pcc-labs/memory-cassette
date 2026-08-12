FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi==0.121.2" \
    "uvicorn[standard]==0.42.0" \
    "pydantic==2.13.1"

COPY manifest.py main.py ./

ENV CASSETTE_NAME=memory
EXPOSE 9998

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9998"]
