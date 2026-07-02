"""Shared test doubles used across multiple integration test files.

Flat module, not a package member — mirrors the rest of the suite's no-install
convention (each test file `sys.path.insert`s `<repo>/src` itself; this just adds
`<repo>/tests` the same way) so `python -m unittest discover -s tests` and running a
single file directly both work with zero setup.
"""
from wisp.egress.notifiers import NotifyResult


class RecordingNotifier:
    """No real network — records every `send()` call for assertion."""
    channel = "ntfy"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict] = []

    def send(self, recipient, title, body, priority) -> NotifyResult:
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority})
        return NotifyResult(self.ok)
