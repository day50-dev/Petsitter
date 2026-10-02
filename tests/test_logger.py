"""Tests for the traffic logger trick."""

import json

import pytest

from petsitter.tricks.logger import DEFAULT_LOGGER_PATH, LoggerTrick


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestLoggerTrick:
    """The logger emits one JSONL record per request and per response."""

    def test_declares_path_config_field(self):
        assert LoggerTrick.config_fields
        field = LoggerTrick.config_fields[0]
        assert field["key"] == "path"
        assert field["label"]
        assert field["description"]
        assert field["default"] == str(DEFAULT_LOGGER_PATH)

    def test_default_folder_used_when_unconfigured(self):
        trick = LoggerTrick()
        assert trick._log_path("before") == DEFAULT_LOGGER_PATH / "before.jsonl"
        assert trick._log_path("after") == DEFAULT_LOGGER_PATH / "after.jsonl"

    def test_pre_hook_records_request(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path))
        logfile = tmp_path / "before.jsonl"
        context = [{"role": "user", "content": "hello there"}]
        params = {"model": "my-model", "temperature": 0.5, "tools": []}

        result = trick.pre_hook(context, params)

        assert result is context
        rec = _records(logfile)[0]
        assert rec["event"] == "request"
        assert rec["stage"] == "before"
        assert rec["messages"] == context
        assert rec["payload"] == params
        assert rec["timestamp"]
        assert rec["model"] == "my-model"

    def test_post_hook_records_response(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path))
        logfile = tmp_path / "after.jsonl"
        context = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi!"},
        ]

        result = trick.post_hook(context)

        assert result is context
        rec = _records(logfile)[0]
        assert rec["event"] == "response"
        assert rec["stage"] == "after"
        assert rec["messages"] == context
        assert rec["answer"] == context[-1]
        assert rec["timestamp"]

    def test_requests_and_replies_go_to_separate_files(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path))
        trick.pre_hook([{"role": "user", "content": "go"}], {})
        trick.post_hook([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "done"},
        ])
        assert [r["event"] for r in _records(tmp_path / "before.jsonl")] == ["request"]
        assert [r["event"] for r in _records(tmp_path / "after.jsonl")] == ["response"]

    def test_an_old_file_setting_puts_both_files_beside_it(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path / "traffic.jsonl"))
        assert trick._log_path("before") == tmp_path / "traffic.before.jsonl"
        assert trick._log_path("after") == tmp_path / "traffic.after.jsonl"

    def test_parent_dirs_are_created(self, tmp_path):
        folder = tmp_path / "deep" / "nest"
        trick = LoggerTrick(path=str(folder))
        trick.pre_hook([], {})
        assert (folder / "before.jsonl").exists()

    def test_lines_are_valid_jsonl(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path))
        trick.pre_hook([{"role": "user", "content": "hi"}], {"tools": []})
        trick.post_hook([{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}])
        for rec in _records(tmp_path / "before.jsonl") + _records(tmp_path / "after.jsonl"):
            assert "timestamp" in rec
            assert rec["trick"] == "LoggerTrick"

    def test_empty_context_post_hook_no_record(self, tmp_path):
        trick = LoggerTrick(path=str(tmp_path))
        assert trick.post_hook([]) == []
        assert not (tmp_path / "after.jsonl").exists()