FROM python:3.13-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "/app/notifier.py"]
