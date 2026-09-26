import os

# Tests never need a model server: force the offline LLM before the app module is imported.
os.environ["LLM_MODE"] = "mock"
os.environ["MOCK_LATENCY_MS"] = "0"
