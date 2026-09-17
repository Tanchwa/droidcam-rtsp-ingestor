"""Long-lived web frontend for cellphone camera -> llm-d VLM inference.

Serves the UI, holds the browser WebSocket, and turns a "go" click into a
streaming request against the llm-d router gateway. Frame capture lives in the
separate handler workload, which the gateway hook provisions per session.
"""
