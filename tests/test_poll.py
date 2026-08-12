"""The `--wait` engine, driven by a fake clock and fake responses. No network."""

from __future__ import annotations

import json

import pytest

from unstract_cli.core.errors import ExitCode
from unstract_cli.core.poll import (
    CLIError,
    PollSpec,
    extract_handle,
    extract_status,
    persist,
    preflight,
    wait_for_completion,
)

SPEC = PollSpec(
    handle_field="whisper_hash",
    terminal_success=("processed",),
    terminal_failure=("error",),
    status_field=("status", "execution_status"),
)


class Clock:
    """Monotonic clock that only advances when the engine sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def responses(*payloads):
    """A poll callable returning each payload in turn, then repeating the last."""
    queue = list(payloads)
    calls: list[str] = []

    def poll(handle: str):
        calls.append(handle)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    poll.calls = calls
    return poll


def test_polls_until_terminal_success():
    clock = Clock()
    poll = responses(
        {"status": "processing"},
        {"status": "processing"},
        {"status": "processed", "n": 1},
    )
    out = wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=poll,
        interval=3,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert out == {"status": "processed", "n": 1}
    assert poll.calls == ["h1", "h1", "h1"]
    assert clock.slept == [3, 3]


def test_terminal_state_comes_from_the_body_not_the_http_status():
    # The deployment API returns HTTP 422 while still executing; only the body's
    # status decides, so this reaches COMPLETED without any status-code input.
    spec = PollSpec(
        handle_field="execution_id",
        terminal_success=("COMPLETED",),
        terminal_failure=("ERROR",),
        status_field=("status", "execution_status"),
    )
    clock = Clock()
    out = wait_for_completion(
        initial={"message": {"execution_id": "e1", "execution_status": "PENDING"}},
        spec=spec,
        poll=responses({"status": "EXECUTING"}, {"status": "COMPLETED"}),
        sleep=clock.sleep,
        now=clock.now,
    )
    assert out == {"status": "COMPLETED"}


def test_terminal_failure_raises_with_the_handle_attached():
    clock = Clock()
    with pytest.raises(CLIError) as excinfo:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=responses({"status": "error", "detail": "bad page"}),
            sleep=clock.sleep,
            now=clock.now,
        )
    err = excinfo.value
    assert err.exit_code is ExitCode.VALIDATION
    assert err.to_dict()["whisper_hash"] == "h1"
    assert err.to_dict()["details"]["detail"] == "bad page"


def test_timeout_carries_the_handle_so_work_is_resumable():
    clock = Clock()
    with pytest.raises(CLIError) as excinfo:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=responses({"status": "processing"}),
            interval=5,
            timeout=12,
            sleep=clock.sleep,
            now=clock.now,
        )
    err = excinfo.value
    assert err.exit_code is ExitCode.TIMEOUT
    payload = err.to_dict()
    assert payload["whisper_hash"] == "h1"
    assert payload["last_status"] == "processing"
    assert "Resume" in payload["hint"]
    # The last sleep is clipped so the wait lasts exactly as long as asked.
    assert clock.slept == [5, 5, 2]
    assert clock.now() == 12


def test_missing_handle_returns_the_initial_response_unpolled():
    poll = responses({"status": "processed"})
    out = wait_for_completion(
        initial={"no_handle_here": True}, spec=SPEC, poll=poll, sleep=Clock().sleep
    )
    assert out == {"no_handle_here": True}
    assert poll.calls == []


def test_status_changes_are_reported_once_each():
    clock = Clock()
    seen: list[str | None] = []
    wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=responses(
            {"status": "accepted"},
            {"status": "processing"},
            {"status": "processing"},
            {"status": "processed"},
        ),
        on_status=seen.append,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert seen == ["accepted", "processing", "processed"]


def test_retrieve_step_runs_after_terminal_success():
    clock = Clock()
    out = wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=responses({"status": "processed"}),
        retrieve=lambda handle: {"result_for": handle},
        sleep=clock.sleep,
        now=clock.now,
    )
    assert out == {"result_for": "h1"}


def test_save_persists_the_retrieved_result_before_returning(tmp_path):
    target = tmp_path / "out" / "result.json"
    on_disk: list[bool] = []

    def retrieve(handle):
        return {"text": "extracted"}

    out = wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=responses({"status": "processed"}),
        retrieve=retrieve,
        save=target,
        # Observed from inside the engine, before the caller is handed anything:
        # asserting after the return passes for either ordering.
        on_saved=lambda path: on_disk.append(path.exists()),
        sleep=Clock().sleep,
    )
    assert on_disk == [True]
    assert json.loads(target.read_text()) == out


def test_an_unwritable_save_target_is_refused_before_anything_is_read(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    with pytest.raises(CLIError) as caught:
        preflight(blocker / "result.json")

    assert caught.value.exit_code is ExitCode.USAGE
    assert "nothing is lost" in (caught.value.hint or "")


def test_a_failed_save_carries_the_result_it_could_not_write(tmp_path):
    """By this point the service has served the result and will not again, so
    the payload has to leave through the error."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    with pytest.raises(CLIError) as caught:
        persist(blocker / "result.json", {"result_text": "IRREPLACEABLE"})

    assert caught.value.exit_code is ExitCode.SAVE_FAILED
    assert caught.value.details == {"result_text": "IRREPLACEABLE"}


def test_a_save_leaves_no_temporary_file_behind(tmp_path):
    target = persist(tmp_path / "out.json", {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_persist_writes_text_payloads_unwrapped(tmp_path):
    target = persist(tmp_path / "a.txt", "plain extracted text")
    assert target.read_text() == "plain extracted text"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "processed"},
        {"message": {"status": "processed"}},
        {"data": {"status": "processed"}},
        {"result": {"status": "processed"}},
    ],
)
def test_status_is_found_one_level_into_the_common_envelopes(payload):
    assert extract_status(payload) == "processed"


def test_handle_is_found_one_level_in_too():
    assert extract_handle({"message": {"execution_id": "e1"}}, "execution_id") == "e1"
    assert extract_handle({"nothing": 1}, "execution_id") is None
