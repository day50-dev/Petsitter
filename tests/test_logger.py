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

    def test_default_path_used_when_unconfigured(self):
        trick = LoggerTrick()
        assert trick._log_path() == DEFAULT_LOGGER_PATH

    def test_pre_hook_records_request(self, tmp_path):
        logfile = tmp_path / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        context = [{"role": "user", "content": "hello there"}]
        params = {"model": "my-model", "temperature": 0.5, "tools": []}

        result = trick.pre_hook(context, params)

        assert result is context
        rec = _records(logfile)[0]
        assert rec["event"] == "request"
        assert rec["direction"] == "out"
        assert rec["messages"] == context
        assert rec["payload"] == params
        assert rec["timestamp"]
        assert rec["model"] == "my-model"

    def test_post_hook_records_response(self, tmp_path):
        logfile = tmp_path / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        context = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi!"},
        ]

        result = trick.post_hook(context)

        assert result is context
        rec = _records(logfile)[0]
        assert rec["event"] == "response"
        assert rec["direction"] == "in"
        assert rec["messages"] == context
        assert rec["answer"] == context[-1]
        assert rec["timestamp"]

    def test_request_and_response_both_land_in_same_file(self, tmp_path):
        logfile = tmp_path / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        trick.pre_hook([{"role": "user", "content": "go"}], {})
        trick.post_hook([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "done"},
        ])

        recs = _records(logfile)
        assert len(recs) == 2
        assert [r["event"] for r in recs] == ["request", "response"]

    def test_directory_path_writes_traffic_jsonl_inside(self, tmp_path):
        target = tmp_path / "somewhere"
        trick = LoggerTrick(path=str(target / "traffic.jsonl"))
        trick.configure({"path": str(target)})

        assert trick._log_path() == target / "traffic.jsonl"

    def test_parent_dirs_are_created(self, tmp_path):
        logfile = tmp_path / "deep" / "nest" / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        trick.pre_hook([], {})
        assert logfile.exists()

    def test_lines_are_valid_jsonl(self, tmp_path):
        logfile = tmp_path / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        trick.pre_hook([{"role": "user", "content": "hi"}], {"tools": []})
        trick.post_hook([{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}])
        for rec in _records(logfile):
            assert "timestamp" in rec
            assert rec["trick"] == "LoggerTrick"

    def test_empty_context_post_hook_no_record(self, tmp_path):
        logfile = tmp_path / "traffic.jsonl"
        trick = LoggerTrick(path=str(logfile))
        assert trick.post_hook([]) == []
        assert not logfile.exists()