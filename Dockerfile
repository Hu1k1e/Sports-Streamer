FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Environment settings
ENV PYTHONUNBUFFERED=1
# Replace this dynamically at runtime or in docker-compose.yml
ENV PROXY_HOST=http://127.0.0.1:7694

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright specific dependencies and xvfb for headless=False execution
RUN apt-get update && apt-get install -y xvfb python3-tk && rm -rf /var/lib/apt/lists/*
RUN playwright install chromium

# Copy app files
COPY . .

EXPOSE 5000

# Start server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
