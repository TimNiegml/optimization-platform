import threading
import time

from optplat.workspace import WorkspaceStore


def test_wait_user_messages_wakes_when_canvas_writes():
    store = WorkspaceStore()
    result = []

    worker = threading.Thread(
        target=lambda: result.extend(store.wait_user_messages("chat", timeout=1.0)))
    worker.start()
    time.sleep(0.03)
    store.update("chat", user_message="优化 y1")
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert [message["text"] for message in result] == ["优化 y1"]
    assert store.poll_user_messages("chat") == []  # delivered exactly once


def test_wait_user_messages_times_out_without_message():
    store = WorkspaceStore()
    started = time.monotonic()
    assert store.wait_user_messages("empty", timeout=0.03) == []
    assert time.monotonic() - started >= 0.02


def test_wait_without_mark_read_can_be_consumed_later():
    store = WorkspaceStore()
    store.update("chat", user_message="检查画布")
    peek = store.wait_user_messages("chat", timeout=0, mark_read=False)
    consumed = store.poll_user_messages("chat", mark_read=True)
    assert peek[0]["text"] == consumed[0]["text"] == "检查画布"
