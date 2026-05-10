FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY streak.py dashboard.html ./

EXPOSE 8765

CMD ["python", "streak.py", "--no-browser"]
