"""(exportit) saves the conversation as the model would see it; (exportit: both)
also saves it as the tool sent it."""

import asyncio
import json

import petsitter.tricks.exportit as ex
from petsitter.proxy import ProxyHandler
from petsitter.trick import Trick
from petsitter.tricks.exportit import ExportItTrick
from petsitter.tricks.logger import LoggerTrick


class Shout(Trick):
    def system_prompt(self, to_add):
        return "be brief"

    def pre_hook(self, context, params):
        for m in context:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m["content"] = m["content"].upper()
        return context


def _run(tmp_path, monkeypatch, text):
    monkeypatch.setattr(ex, "EXPORT_DIR", str(tmp_path / "out"))
    log = LoggerTrick(path=str(tmp_path / "log"))
    handler = ProxyHandler(model_url="http://never-called", model_name="m",
                           tricks=[log, Shout(), ExportItTrick()])
    result = asyncio.run(handler.chat_completions({"model": "m", "messages": [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": text},
    ]}))
    files = {p.name: json.loads(p.read_text()) for p in (tmp_path / "out").iterdir()}
    return result["choices"][0]["message"]["content"], files, log


def test_default_is_after_the_extensions(tmp_path, monkeypatch):
    reply, files, _ = _run(tmp_path, monkeypatch, "(exportit)")
    [(name, convo)] = files.items()
    assert not name.endswith(("-before.json", "-after.json"))
    assert convo[0] == {"role": "system", "content": "be brief"}
    assert convo[1]["content"] == "HELLO THERE"
    assert name in reply


def test_both_writes_before_and_after(tmp_path, monkeypatch):
    reply, files, log = _run(tmp_path, monkeypatch, "(exportit: both)")
    before = next(v for k, v in files.items() if k.endswith("-before.json"))
    after = next(v for k, v in files.items() if k.endswith("-after.json"))
    assert before[0]["content"] == "hello there" and before[0]["role"] == "user"
    assert after[1]["content"] == "HELLO THERE"
    assert "Before:" in reply and "After:" in reply
    # the preview ran no watcher: the Traffic Logger recorded nothing
    assert not (tmp_path / "log").exists()


def test_other_text_is_still_a_note(tmp_path, monkeypatch):
    reply, files, _ = _run(tmp_path, monkeypatch, "(exportit: backup before refactor)")
    assert len(files) == 1 and reply.endswith("Note: backup before refactor")
