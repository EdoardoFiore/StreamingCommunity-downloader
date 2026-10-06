FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as root unless the compose file says `user:` (#28). Everything above is
# root-owned, so the two places the panel writes inside the image itself are
# opened to any uid: tmp/ (TMP_DIR, HLS segments while a job runs) and config/
# (where the template points DB_FILE, DATA_FILE and SCHEDULE_FILE, normally a
# volume that hides this one). Sticky like /tmp, so one uid cannot delete
# another's files. Same paths as before, so a volume already mounted on either
# keeps working.
RUN mkdir -p /app/tmp /app/config && chmod 1777 /app/tmp /app/config

EXPOSE 8000

CMD ["python", "main.py"]
