FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py __init__.py ./cloud/
COPY site ./cloud/site
ENV CLOUD_DB=/data/cloud.db
VOLUME /data
EXPOSE 8100
CMD ["uvicorn", "cloud.app:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers", "--forwarded-allow-ips", "*"]
