import os

# Tests never need a model server or the network: force the offline LLM and live data
# before the app module is imported.
os.environ["LLM_MODE"] = "mock"
os.environ["MOCK_LATENCY_MS"] = "0"
os.environ["LIVE_DATA"] = "mock"
