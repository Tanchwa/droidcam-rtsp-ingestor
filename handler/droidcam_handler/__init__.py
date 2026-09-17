"""Short-lived DroidCam RTSP handler.

Created by the llm-d router's gateway hook when a session starts, reads its
whole configuration from the environment, feeds frames into the assigned
inference pool, and exits. It never talks to the frontend.
"""
