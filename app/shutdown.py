"""Shared orderly radio shutdown for both frontends."""

import time


def stop_receiver(receiver, before_disconnect=None):
    """Cancel RF work, request RX, then wait for transport logout/cleanup.

    The transport owns sockets and its final repeated PTT-off/logout sequence.
    Never let a UI timeout cut that daemon thread short when the process exits.
    """
    try:
        receiver.stop_tx()
        state = receiver.radio_state
        if (
            getattr(receiver, "tx_enabled", False) is True
            or state.get("ptt") is True
            or state.get("ptt_pending") is True
            or state.get("tuner") == 2
        ):
            receiver.command("force_rx")
            receiver.command("tuner_stop")
        deadline = time.monotonic() + 1.2
        while receiver.busy() and time.monotonic() < deadline:
            state = receiver.radio_state
            if (
                state.get("ptt") is False
                and not state.get("ptt_pending")
                and state.get("tuner") != 2
            ):
                break
            if not getattr(receiver, "tx_enabled", False) and not (
                state.get("ptt") or state.get("ptt_pending") or state.get("tuner") == 2
            ):
                break
            time.sleep(0.03)
        if before_disconnect:
            before_disconnect()
    finally:
        receiver.stop.set()
        if receiver.thread:
            receiver.thread.join()
