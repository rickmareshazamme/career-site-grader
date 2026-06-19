# Playwright base image ships Chromium + all system deps, so headless rendering
# (renderer.py, gated by ENABLE_HEADLESS=1) works without extra apt steps.
# Headless is OFF by default — without ENABLE_HEADLESS the app behaves exactly
# like the nixpacks build, just from a container.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind to Railway's $PORT. 2 workers keeps Chromium memory bounded when headless
# is enabled; gthread handles the I/O-bound grading work.
CMD gunicorn server:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --worker-class gthread --threads 8 --timeout 180
