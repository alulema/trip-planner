import os

# Tests never hit the network: force the offline LLM before the app module is imported.
os.environ["LLM_MODE"] = "mock"
os.environ["MOCK_LATENCY_MS"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)
