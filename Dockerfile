FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e ".[postgres]"
EXPOSE 8789
CMD ["modelkeyguard", "gateway", "--host", "0.0.0.0", "--port", "8789"]
