FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY member_stream.py reminders.csv ./

ENV PORT=8088
EXPOSE 8088
CMD ["python", "member_stream.py"]
