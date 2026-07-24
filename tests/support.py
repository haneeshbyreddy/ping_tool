from wisp.egress.notifiers import NotifyResult

class RecordingNotifier:
    channel = "ntfy"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict] = []

    def send(self, recipient, title, body, priority, *, whatsapp=(),
             facts=None) -> NotifyResult:
        # `whatsapp`/`facts` are the additive WhatsApp fan-out args (2026-07-23);
        # recorded so tests can assert the per-account numbers a page reaches,
        # while the existing `recipient` (ntfy topic string) assertions stand.
        self.sent.append({"recipient": recipient, "title": title,
                          "body": body, "priority": priority,
                          "whatsapp": list(whatsapp), "facts": facts})
        return NotifyResult(self.ok)
