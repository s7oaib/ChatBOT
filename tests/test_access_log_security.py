"""The production entrypoint must not log GET webhook credentials in URLs."""

import runpy
import threading
from pathlib import Path

import uvicorn


def test_production_entrypoint_disables_full_url_access_logs(monkeypatch):
    options = {}

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", NoStartThread)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: options.update(kwargs))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "main.py"), run_name="__main__")

    assert options["access_log"] is False
