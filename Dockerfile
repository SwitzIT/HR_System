FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
# Mount a volume at /app/instance to persist the SQLite DB and resumes
CMD gunicorn app:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
