"""Short-lived cellphone camera RTSP handler.

Created by the llm-d router's gateway hook when a session starts, reads its
whole configuration from the environment, feeds frames into the assigned
inference pool, forwards each response to the caller's results callback, and
exits. The callback is the only thing it sends to the frontend, and the
frontend never contacts it.
"""
