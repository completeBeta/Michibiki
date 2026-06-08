FROM python:3.11-slim

WORKDIR /app

# Install protoc for compiling .proto at build time
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY templates/ ./templates/
COPY entrypoint.sh .

# Compile protobuf schema
RUN python -m grpc_tools.protoc -I src --python_out=src src/mihon_backup.proto

EXPOSE 5001

CMD ["./entrypoint.sh"]
