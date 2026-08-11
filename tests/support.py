from wisp.egress.notifiers import NotifyResult

class RecordingNotifier:
    channel = "whatsapp"

    def __init__(self, ok: bool = True, free_ok: bool = True) -> None:
        self.ok = ok
        self.free_ok = free_ok
        self.sent: list[dict] = []
        self.texts: list[dict] = []
        self.buttons: list[dict] = []

    def send(self, title, body, priority=3, *, whatsapp=(),
             facts=None) -> NotifyResult:
        self.sent.append({"title": title, "body": body, "priority": priority,
                          "whatsapp": list(whatsapp), "facts": facts})
        return NotifyResult(self.ok)


    def send_text(self, to, body) -> NotifyResult:
        self.texts.append({"to": to, "body": body})
        return NotifyResult(self.free_ok)

    def send_buttons(self, to, body, buttons) -> NotifyResult:
        self.buttons.append({"to": to, "body": body,
                             "buttons": [tuple(b) for b in buttons]})
        return NotifyResult(self.free_ok)
