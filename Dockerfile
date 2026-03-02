FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (not config/auth/tools — those are mounted as volumes)
COPY core/ core/
COPY messaging/ messaging/
COPY web_ui/ web_ui/
COPY agent.py .
COPY main.py .

# Config, auth, tools, and logs come from mounted volumes:
#   ./agent/config  → /app/config
#   ./agent/auth    → /app/auth
#   ./agent/tools   → /app/tools
#   agent_logs      → /app/logs

EXPOSE 8000

CMD ["python", "main.py"]