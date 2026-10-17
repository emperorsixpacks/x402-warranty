FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --no-dev

# Copy application code
COPY server ./server

ENV PYTHONPATH=/app/server

# Run the warranty server
CMD ["uv", "run", "python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
