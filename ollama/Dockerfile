# Ollama with the Qwen 2.5 model baked in as an image layer: no model download at
# startup (fast, offline cold start). Serves the Ollama API on port 11434.
FROM ollama/ollama:0.12.3

ARG OLLAMA_MODEL=qwen2.5:1.5b-instruct
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_KEEP_ALIVE=30m \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1

RUN ollama serve & pid=$!; \
    for i in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done; \
    ollama pull "$OLLAMA_MODEL" && kill $pid

EXPOSE 11434
