FROM python:3.12-slim

# No system ffmpeg needed — imageio-ffmpeg (requirements.txt) bundles its
# own static binary with everything this project uses: libx264, libopus,
# aac, and the xstack/tpad/amix/apad filters. See userbot/recorder.py.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p storage/recordings \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# docker-compose.yml overrides this per service (run_bot.py / run_userbot.py)
CMD ["python", "run_bot.py"]
