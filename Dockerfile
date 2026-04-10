FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY georender_service/ ./georender_service/
COPY assets/ ./assets/
COPY rulesets/ ./rulesets/
COPY maps/ ./maps/

RUN mkdir -p cache

EXPOSE 8000

CMD ["uvicorn", "georender_service.app:app", "--host", "0.0.0.0", "--port", "8000"]
