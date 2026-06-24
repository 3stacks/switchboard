# switchboard agent: AudioSocket bridge + Claude. TCP-only (no RTP), so it runs
# fine in Docker-for-Mac. Note: TTS_PROVIDER=say is macOS-host only — in a
# container use a cloud TTS (cartesia/elevenlabs) instead.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
EXPOSE 9000
CMD ["python", "bridge.py"]
