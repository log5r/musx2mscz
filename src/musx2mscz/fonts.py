"""Font handling: Enigma text tags, font ID resolution, style mapping.

Finale text strings embed formatting commands like:
    ^fontTxt(Times New Roman,4096)^size(14)^nfx(1)Allegro ^fontMus(Kousaku,4096)^size(24)q^fontTxt(...) = 120

nfx is a bitmask: 1=bold, 2=italic, 4=underline, 32=strikeout, 64=absolute, 128=hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

NFX_BOLD = 1
NFX_ITALIC = 2

# Known Finale music fonts (glyph-encoded, not readable text).
MUSIC_FONTS = {
    "maestro", "maestro wide", "petrucci", "engraver font set", "engravertext",
    "engraver text t", "engravertextt", "jazz", "jazztext", "jazzperc", "broadway copyist",
    "kousaku", "kousaku percussion", "rentaro", "finale maestro", "finale broadway", "finale numerics",
    "finale percussion", "finale alphanotes", "finale mallets", "seville", "tamburo",
    "maestro percussion",
}

# Finale music font -> closest MuseScore-bundled SMuFL music font.
MUSIC_FONT_TO_MUSESCORE = {
    "maestro": "Finale Maestro",
    "maestro wide": "Finale Maestro",
    "petrucci": "Finale Maestro",
    "engraver font set": "Finale Maestro",
    "jazz": "Finale Broadway",
    "kousaku": "Finale Broadway",
    "broadway copyist": "Finale Broadway",
    "seville": "Finale Maestro",
}

# Text-font fallbacks for glyph fonts when text must be rendered as text.
MUSIC_TEXT_FONT_TO_MUSESCORE = {
    "finale maestro": "Finale Maestro Text",
    "finale broadway": "Finale Broadway Text",
}


def is_music_font(name: str | None) -> bool:
    return bool(name) and name.strip().lower() in MUSIC_FONTS


def musescore_music_font(name: str | None) -> str:
    if not name:
        return "Finale Maestro"
    return MUSIC_FONT_TO_MUSESCORE.get(name.strip().lower(), "Finale Maestro")


@dataclass
class FontInfo:
    family: str | None = None
    size: float | None = None
    bold: bool = False
    italic: bool = False
    music: bool = False

    def merged(self, other: "FontInfo") -> "FontInfo":
        return FontInfo(
            family=other.family or self.family,
            size=other.size if other.size is not None else self.size,
            bold=other.bold,
            italic=other.italic,
            music=other.music,
        )


@dataclass
class TextRun:
    text: str
    font: FontInfo


_TAG_RE = re.compile(r"\^(\w+)\((.*?)\)|\^\^")


def font_info_from_efx(doc, font_el) -> FontInfo:
    """FontInfo from an Enigma <font>-ish element with fontID/fontSize/efx children."""
    if font_el is None:
        return FontInfo()
    font_id = doc.get(font_el, "fontID", "0")
    size = doc.get(font_el, "fontSize")
    efx = font_el.find("efx")
    bold = efx is not None and efx.find("bold") is not None
    italic = efx is not None and efx.find("italic") is not None
    family = resolve_font_name(doc, font_id)
    return FontInfo(
        family=family,
        size=float(size) if size else None,
        bold=bold,
        italic=italic,
        music=is_music_font(family),
    )


def resolve_font_name(doc, font_id: str | None) -> str | None:
    if font_id is None:
        return None
    el = doc.other("fontName", str(font_id))
    if el is None:
        return None
    return doc.get(el, "name")


def parse_enigma_text(text: str, initial: FontInfo | None = None,
                      resolver=None) -> list[TextRun]:
    """Split an Enigma-formatted string into styled runs, dropping non-font tags.

    `resolver` optionally maps a font ID (from ^fontid tags) to a font name.
    """
    runs: list[TextRun] = []
    cur = initial or FontInfo()
    buf: list[str] = []
    pos = 0

    def flush():
        if buf:
            runs.append(TextRun("".join(buf), replace(cur)))
            buf.clear()

    for m in _TAG_RE.finditer(text):
        if m.start() > pos:
            buf.append(text[pos : m.start()])
        pos = m.end()
        if m.group(0) == "^^":
            buf.append("^")
            continue
        tag, arg = m.group(1), m.group(2)
        if tag in ("font", "fontTxt", "fontMus", "fontNum", "Font"):
            flush()
            family = arg.split(",")[0].strip()
            cur = replace(cur, family=family, music=is_music_font(family) or tag == "fontMus")
        elif tag == "fontid":
            flush()
            family = resolver(arg.split(",")[0].strip()) if resolver else None
            cur = replace(cur, family=family,
                          music=is_music_font(family) if family else False)
        elif tag == "size":
            flush()
            try:
                cur = replace(cur, size=float(arg.split(",")[0]))
            except ValueError:
                pass
        elif tag == "nfx":
            flush()
            try:
                v = int(arg.split(",")[0])
            except ValueError:
                v = 0
            cur = replace(cur, bold=bool(v & NFX_BOLD), italic=bool(v & NFX_ITALIC))
        elif tag == "flat":
            buf.append("♭")
        elif tag == "sharp":
            buf.append("♯")
        elif tag == "natural":
            buf.append("♮")
        # other tags (^baseline, ^tracking, ^page, ...) are dropped
    if pos < len(text):
        buf.append(text[pos:])
    flush()
    return [r for r in runs if r.text]


def plain_text(text: str | None) -> str:
    """Strip all Enigma tags, keeping accidentals as unicode."""
    if not text:
        return ""
    return "".join(r.text for r in parse_enigma_text(text)).strip()
