"""Small HTML helpers: escaping and a whitelist sanitiser for doctor-entered rich text."""
from __future__ import annotations

from html import escape as _escape
from html.parser import HTMLParser

ALLOWED_TAGS = {"p", "b", "strong", "i", "em", "u", "br", "ul", "ol", "li", "table", "thead",
                "tbody", "tr", "th", "td", "small", "span", "div"}
VOID = {"br"}


def e(value) -> str:
    return "" if value is None else _escape(str(value), quote=True)


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            return
        self.out.append(f"<{tag}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(_escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitise(html: str) -> str:
    """Strip every tag and attribute not in the whitelist (no scripts, styles, links)."""
    p = _Sanitiser()
    p.feed(html or "")
    p.close()
    return p.result()
