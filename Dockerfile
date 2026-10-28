# ue5-automation MCP stdio proxy
#
# Glama / directory introspection: this container starts the stdio MCP proxy,
# which serves the full tool catalog locally (no Unreal Editor required for
# `initialize` / `tools/list`). Tool EXECUTION requires a running Unreal Editor
# with the bridge plugin installed — without it, tools/call returns a clean
# JSON-RPC error explaining setup (never a fake success).
#
# Real-world usage does not need Docker: point a stdio client at
#   skill/ue5-automation/scripts/ue5_mcp_stdio.py
# (see README "Use with stdio clients").
FROM python:3.12-slim

WORKDIR /app

COPY skill/ue5-automation/scripts/ue5_bridge.py ./
COPY skill/ue5-automation/scripts/fake_unreal.py ./
COPY skill/ue5-automation/scripts/ue5_mcp_stdio.py ./

RUN pip install --no-cache-dir fastapi uvicorn pydantic requests

# TEST_MODE keeps the module importable outside Unreal Engine; it only affects
# which `unreal` module object is injected at import time — the proxy itself
# never executes engine commands locally (calls are relayed to the editor).
ENV UE5_BRIDGE_TEST_MODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

CMD ["python", "ue5_mcp_stdio.py"]
