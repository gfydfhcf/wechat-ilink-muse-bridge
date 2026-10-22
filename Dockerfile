# wechat-ilink-muse-bridge — standalone daemon in a container.
# The session (login credential!) lives in the /data volume; never bake
# it into the image. First run: do the QR login once (see README),
# then start the daemon.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ilink.py daemon.py ./

ENV ILINK_DATA_DIR=/data
VOLUME ["/data"]

# Default: echo demo. Override `command:` in compose (or `docker run ...
# python3 daemon.py --llm`) for the LLM example handler.
CMD ["python3", "daemon.py", "--echo"]
