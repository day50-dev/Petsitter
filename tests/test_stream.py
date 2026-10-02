"""The dashboard's one shared event stream (/api/stream)."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, PET_CONFIG_DIR=str(tmp_path), PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.Popen([sys.executable, str(ROOT / "petsitter"), "-l", f"127.0.0.1:{port}", "--no-browser"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(base + "/api/info", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _events(lines):
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_one_stream_carries_every_topic(server):
    tid = next(t["id"] for t in httpx.get(server + "/api/tricks").json() if t["name"] == "ContextMonitorTrick")
    with httpx.stream("GET", server + "/api/stream", timeout=10) as r:
        events = _events(r.iter_lines())
        sid = next(events)["sid"]
        reply = httpx.post(f"{server}/api/stream/{sid}",
                           json={"subscribe": ["pause", "logs", f"live:{tid}", "nonsense"]}).json()
        assert reply["topics"] == sorted(["pause", "logs", f"live:{tid}"])

        assert next(events) == {"topic": "pause", "data": {"paused": False}}
        httpx.post(server + "/api/pause", json={"paused": True})
        assert next(e for e in events if e["topic"] == "pause") == {"topic": "pause", "data": {"paused": True}}

        # unsubscribing stops a topic
        httpx.post(f"{server}/api/stream/{sid}", json={"unsubscribe": ["pause"]})
        httpx.post(server + "/api/pause", json={"paused": False})
        httpx.post(f"{server}/api/stream/{sid}", json={"subscribe": ["pause"]})
        assert next(e for e in events if e["topic"] == "pause") == {"topic": "pause", "data": {"paused": False}}


def test_unknown_stream_is_a_404(server):
    assert httpx.post(server + "/api/stream/nope", json={"subscribe": ["logs"]}).status_code == 404


def test_pages_use_the_shared_stream(server):
    assert '<script src="/static/stream.js?v=' in httpx.get(server + "/").text
    tid = next(t["id"] for t in httpx.get(server + "/api/tricks").json() if t["name"] == "ContextMonitorTrick")
    page = httpx.get(f"{server}/api/tricks/ui/{tid}/").text
    assert '<script src="/static/stream.js?v=' in page and 'EventSource("events")' in page
    for gone in ("/api/logs", "/api/pause/stream"):
        assert httpx.get(server + gone, timeout=3).status_code in (404, 405)
