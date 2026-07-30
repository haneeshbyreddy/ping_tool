from wisp.egress.notifiers import NotifyResult

class RecordingNotifier:
    channel = "whatsapp"

    def __init__(self, ok: bool = True, free_ok: bool = True) -> None:
        self.ok = ok
        # Whether a free-form reply is accepted — i.e. whether the recipient's
        # 24h window is open. Its own flag so a test can shut the window
        # (`free_ok=False`) and prove the template fallback still lands.
        self.free_ok = free_ok
        self.sent: list[dict] = []
        self.texts: list[dict] = []
        self.buttons: list[dict] = []

    def send(self, title, body, priority=3, *, whatsapp=(),
             facts=None) -> NotifyResult:
        # WhatsApp is the only channel (ntfy removed 2026-07-24): a page carries a
        # title/body/priority, the list of E.164 numbers it reaches (`whatsapp`),
        # and the structured `facts`. All recorded so tests can assert them.
        self.sent.append({"title": title, "body": body, "priority": priority,
                          "whatsapp": list(whatsapp), "facts": facts})
        return NotifyResult(self.ok)

    # Free-form replies (only legal inside a recipient's 24h window, so callers
    # try these FIRST and fall back to the template `send` above when they fail).
    # Recorded separately: a test asserting "the assignee got a button" must not
    # be satisfiable by a template page that carries none.

    def send_text(self, to, body) -> NotifyResult:
        self.texts.append({"to": to, "body": body})
        return NotifyResult(self.free_ok)

    def send_buttons(self, to, body, buttons) -> NotifyResult:
        self.buttons.append({"to": to, "body": body,
                             "buttons": [tuple(b) for b in buttons]})
        return NotifyResult(self.free_ok)
