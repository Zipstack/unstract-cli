"""The `--wait` engine, driven by a fake clock and fake responses. No network."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys

import pytest

from unstract_cli.core import poll as poll_module
from unstract_cli.core.errors import REDACTED, ExitCode
from unstract_cli.core.poll import (
    MAX_TRANSIENT_POLLS,
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
    # The job is still running, so this is the failure a caller is meant to
    # come back from rather than the one that ends the attempt.
    assert payload["retryable"] is True
    # The last sleep is clipped so the wait lasts exactly as long as asked.
    assert clock.slept == [5, 5, 2]
    assert clock.now() == 12


def test_a_finished_response_with_no_handle_is_still_saved(tmp_path):
    """A deployment can answer the submit with the finished result. Returning it
    unpolled is right; returning it without honouring --save loses it."""
    target = tmp_path / "result.json"
    poll = responses()
    out = wait_for_completion(
        initial={"status": "processed", "result_text": "done"},
        spec=SPEC,
        poll=poll,
        save=target,
        sleep=Clock().sleep,
    )
    assert out == {"status": "processed", "result_text": "done"}
    assert poll.calls == []
    assert json.loads(target.read_text()) == out


def test_no_handle_and_no_result_fails_rather_than_reporting_success():
    """Nothing to poll and nothing finished is a broken response, not an answer
    the caller should see reported as ok."""
    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"no_handle_here": True},
            spec=SPEC,
            poll=responses(),
            sleep=Clock().sleep,
        )
    assert caught.value.exit_code is ExitCode.SERVER_ERROR
    assert caught.value.details == {"no_handle_here": True}


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


@pytest.mark.skipif(sys.platform == "win32", reason="no directory handles")
def test_a_saved_result_is_synced_through_the_directory_entry(tmp_path, monkeypatch):
    """Syncing the file leaves the rename itself in cache, so a crash can take
    back the only copy of a result the service will not serve again."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        order.append("fsync-dir" if os.fstat(fd).st_mode & stat.S_IFDIR else "fsync")
        real_fsync(fd)

    monkeypatch.setattr(poll_module.os, "fsync", fsync)
    monkeypatch.setattr(
        poll_module.os,
        "replace",
        lambda src, dst: (order.append("replace"), real_replace(src, dst))[1],
    )

    persist(tmp_path / "out.json", {"a": 1})

    assert order == ["fsync", "replace", "fsync-dir"]


@pytest.mark.skipif(sys.platform == "win32", reason="no directory handles")
def test_a_result_that_cannot_be_confirmed_on_disk_is_not_reported_as_saved(
    tmp_path, monkeypatch
):
    """Unlike the config, which can be written again, this is the last copy:
    reporting success would invite the caller to drop it."""
    real_fsync = os.fsync

    def fsync(fd):
        if os.fstat(fd).st_mode & stat.S_IFDIR:
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(poll_module.os, "fsync", fsync)

    with pytest.raises(CLIError) as caught:
        persist(tmp_path / "out.json", {"result_text": "IRREPLACEABLE"})

    assert caught.value.exit_code is ExitCode.SAVE_FAILED
    assert caught.value.details == {"result_text": "IRREPLACEABLE"}


def test_a_save_leaves_no_temporary_file_behind(tmp_path):
    target = persist(tmp_path / "out.json", {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_planted_temporary_file_is_not_written_through(tmp_path):
    """A save directory another user can write is a directory they can plant a
    symlink in, and following it would truncate whatever it points at."""
    victim = tmp_path / "victim"
    victim.write_text("do not touch")
    (tmp_path / "out.json.tmp").symlink_to(victim)

    persist(tmp_path / "out.json", {"a": 1})

    assert victim.read_text() == "do not touch"


def test_a_symlinked_save_target_is_refused_before_anything_is_read(tmp_path):
    """Saving over the link would turn it into a regular file and leave what it
    stood for behind, so it is rejected while the result can still be re-read."""
    real = tmp_path / "results.json"
    real.write_text("previous")
    link = tmp_path / "latest.json"
    link.symlink_to(real)

    with pytest.raises(CLIError) as caught:
        preflight(link)

    assert caught.value.exit_code is ExitCode.USAGE
    assert "symlink" in str(caught.value)
    assert link.is_symlink()
    assert real.read_text() == "previous"


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


# --------------------------------------------------------------------------- #
# Transient failures, and what may not be retried
# --------------------------------------------------------------------------- #


def test_a_transient_poll_failure_does_not_end_the_wait():
    """A 500 mid-poll says nothing about the job, and giving up on it throws
    away a document that has already been paid for."""
    clock = Clock()
    calls: list[int] = []

    def poll(handle):
        calls.append(1)
        if len(calls) < 3:
            raise CLIError("upstream", ExitCode.SERVER_ERROR, retryable=True)
        return {"status": "processed"}

    out = wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=poll,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert out == {"status": "processed"}
    assert len(calls) == 3


def test_a_non_retryable_poll_failure_ends_the_wait_at_once():
    def poll(handle):
        raise CLIError("gone", ExitCode.NOT_FOUND)

    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=poll,
            sleep=Clock().sleep,
            now=Clock().now,
        )
    assert caught.value.exit_code is ExitCode.NOT_FOUND


def test_a_failed_retrieve_is_not_reported_as_retryable():
    """The retrieve is the acknowledging read: calling it retryable invites a
    second attempt at a result the service will not serve twice."""

    def retrieve(handle):
        raise RuntimeError("connection reset")

    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=responses({"status": "processed"}),
            retrieve=retrieve,
            sleep=Clock().sleep,
            now=Clock().now,
        )
    assert caught.value.retryable is False


def test_a_refused_retrieve_stays_retryable():
    """A refusal never served the request, so the one-shot read is still there
    to collect -- and the envelope must not tell the caller otherwise."""

    def retrieve(handle):
        raise CLIError(
            "rate limited", ExitCode.RATE_LIMITED, http_status=429, retryable=True
        )

    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=responses({"status": "processed"}),
            retrieve=retrieve,
            sleep=Clock().sleep,
            now=Clock().now,
        )
    assert caught.value.exit_code is ExitCode.RATE_LIMITED
    assert caught.value.message == "rate limited"
    assert caught.value.retryable is True


def test_transient_poll_failures_stop_at_the_cap():
    """Otherwise a hard-down service is retried for the whole timeout."""
    clock = Clock()
    calls: list[int] = []

    def poll(handle):
        calls.append(1)
        raise CLIError("upstream", ExitCode.SERVER_ERROR, retryable=True)

    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=poll,
            sleep=clock.sleep,
            now=clock.now,
            timeout=10_000,
        )
    assert caught.value.message == "upstream"
    assert len(calls) == MAX_TRANSIENT_POLLS + 1


def test_the_retry_backoff_never_sleeps_past_the_deadline():
    """`--timeout 30` that returns at 35s has lied, backoff or not."""
    clock = Clock()

    def poll(handle):
        raise CLIError("upstream", ExitCode.SERVER_ERROR, retryable=True)

    with pytest.raises(CLIError):
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=poll,
            sleep=clock.sleep,
            now=clock.now,
            interval=3.0,
            timeout=10.0,
        )
    assert sum(clock.slept) <= 10.0


def test_a_terminal_failure_without_a_handle_is_not_saved(tmp_path):
    """A response that is finished and failed is not a result, so --save must
    not write it and report success."""
    target = tmp_path / "out.json"
    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"status": "error", "message": "bad page"},
            spec=SPEC,
            poll=responses({"status": "processed"}),
            save=target,
            sleep=Clock().sleep,
            now=Clock().now,
        )
    assert caught.value.exit_code is ExitCode.VALIDATION
    assert caught.value.details == {"status": "error", "message": "bad page"}
    assert not target.exists()


def test_persist_refuses_a_symlink_and_keeps_the_result(tmp_path):
    """preflight checks this before the read; the link can be planted after it,
    and a caller can reach persist without a preflight at all."""
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)

    with pytest.raises(CLIError) as caught:
        persist(link, {"result_text": "IRREPLACEABLE"})

    assert caught.value.exit_code is ExitCode.SAVE_FAILED
    assert caught.value.details == {"result_text": "IRREPLACEABLE"}
    assert real.read_text(encoding="utf-8") == "{}"


def test_a_spec_cannot_name_one_status_as_both_outcomes():
    """`classify` tests failure first, so an overlap would report a success as
    an error. Case-folded on both sides, the way `classify` reads them."""
    with pytest.raises(CLIError) as caught:
        PollSpec(
            handle_field="h",
            terminal_success=("Done",),
            terminal_failure=("done",),
        )
    assert caught.value.exit_code is ExitCode.GENERIC


def flaky_then(payload, failures=1):
    """A poll callable that raises a retryable failure before answering."""
    remaining = [failures]

    def poll(handle: str):
        if remaining[0]:
            remaining[0] -= 1
            raise CLIError("upstream is busy", ExitCode.SERVER_ERROR, retryable=True)
        return payload

    return poll


def test_a_retried_failure_is_reported_without_being_dressed_as_a_status():
    seen: list[str] = []
    retries: list[CLIError] = []
    clock = Clock()
    wait_for_completion(
        initial={"whisper_hash": "h1"},
        spec=SPEC,
        poll=flaky_then({"status": "processed"}),
        sleep=clock.sleep,
        now=clock.now,
        on_status=seen.append,
        on_retry=retries.append,
    )
    assert [exc.message for exc in retries] == ["upstream is busy"]
    assert not any("upstream is busy" in status for status in seen)


def test_a_rescued_result_survives_field_name_redaction(tmp_path):
    """A failed save leaves `details` as the only copy of a result the service
    will not serve again, so collapsing a field for being named like a
    credential destroys what the caller is being handed it to recover."""
    result = {"extraction": {"license_key": "AB-123456", "name": "Ada"}}
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    with pytest.raises(CLIError) as caught:
        persist(blocker / "out.json", result)
    assert caught.value.exit_code is ExitCode.SAVE_FAILED
    assert caught.value.to_dict()["details"] == result


def test_an_ordinary_failure_still_redacts_by_field_name():
    error = CLIError("nope", ExitCode.VALIDATION, details={"api_key": "AB-123456"})
    assert error.to_dict()["details"] == {"api_key": REDACTED}


def test_a_zero_interval_still_backs_off_between_retries():
    """The flags refuse a zero interval, but nothing stops a caller reaching
    this loop directly -- and doubling zero never grows it, so a rate limit
    would be answered as fast as the loop can issue calls."""
    slept: list[float] = []
    clock = Clock()

    def failing(_handle):
        raise CLIError("busy", ExitCode.RATE_LIMITED, http_status=429, retryable=True)

    def record(seconds: float) -> None:
        slept.append(seconds)
        clock.sleep(seconds)

    with pytest.raises(CLIError):
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=failing,
            interval=0,
            timeout=600,
            sleep=record,
            now=clock.now,
        )
    assert slept and all(seconds > 0 for seconds in slept)


def test_preflight_refuses_a_writable_file_in_a_directory_it_cannot_write(tmp_path):
    """The result is written to a temporary sibling and moved over the target,
    so the directory is what has to be writable. Checking the file alone passes
    here and fails after the read `--save` exists to protect."""
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "out.json"
    target.write_text("", encoding="utf-8")
    locked.chmod(0o500)
    try:
        with pytest.raises(CLIError) as caught:
            preflight(target)
    finally:
        locked.chmod(0o700)
    assert caught.value.exit_code is ExitCode.USAGE
    assert "nothing is lost" in (caught.value.hint or "")


def test_preflight_accepts_a_path_whose_directory_does_not_exist_yet(tmp_path):
    """`persist` creates the parents, so refusing here would refuse a path that
    works."""
    assert preflight(tmp_path / "new" / "deeper" / "out.json")


def test_a_fault_on_this_side_is_not_retried_as_a_server_failure():
    """Everything the service raises on purpose is a CLIError by the time it
    reaches the loop, so anything else is this CLI's own bug -- repeating it
    spends the retry budget and reports someone else's fault."""
    calls: list[str] = []

    def broken(handle):
        calls.append(handle)
        raise AttributeError("'NoneType' object has no attribute 'get'")

    clock = Clock()
    with pytest.raises(CLIError) as caught:
        wait_for_completion(
            initial={"whisper_hash": "h1"},
            spec=SPEC,
            poll=broken,
            timeout=600,
            sleep=clock.sleep,
            now=clock.now,
        )
    assert caught.value.exit_code is ExitCode.GENERIC
    assert caught.value.retryable is False
    assert len(calls) == 1
