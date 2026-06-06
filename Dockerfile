FROM python:3.11-slim

# Environment settings
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright chromium and its OS dependencies
RUN playwright install chromium --with-deps

# Copy app files
COPY . .

EXPOSE 5000

# Start server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
