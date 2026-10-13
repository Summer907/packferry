import sys
import threading
import time

from apps.exporter.adapters import ExportCancelled, _run, bind_cancel_event, unbind_cancel_event


def test_cancel_interrupts_running_tool_and_blocks_followups():
    event = threading.Event()
    outcome = []

    def work():
        token = bind_cancel_event(event)
        try:
            _run([sys.executable, "-c", "import time; time.sleep(20)"], timeout=30)
        except ExportCancelled:
            outcome.append("cancelled")
        finally:
            unbind_cancel_event(token)

    started = time.monotonic()
    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(0.4)
    event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
    assert time.monotonic() - started < 5
