"""Generate a MuseScore style file (.mss) from EnigmaXML document options.

This carries global visual fidelity: music font, staff size (spatium), page
size and the text styles of expression categories (tempo bold, dynamics
italic, font sizes, ...).
"""

from __future__ import annotations

import re

from lxml.etree import Element, SubElement, ElementTree

from .enigma import EnigmaDoc
from .fonts import (FontInfo, font_info_from_efx, is_music_font,
                    musescore_music_font, resolve_font_name)

EVPU_PER_INCH = 288.0
MM_PER_INCH = 25.4
EVPU_TO_MM = MM_PER_INCH / EVPU_PER_INCH

# MuseScore renders spatium-dependent text at styleSize * spatium / 1.74978mm,
# Finale at size * effectiveScale where a space is 24 EVPU = 2.11667mm.
# Compensate so absolute printed text sizes match.
FONT_SIZE_FACTOR = 1.74978 / (24.0 * EVPU_TO_MM)


def page_metrics(doc: EnigmaDoc) -> dict:
    """Page geometry (inches/mm) and spatium (mm) derived from the document."""
    page = doc.other("pageSpec", "1")
    pct = 100.0
    w_in, h_in = 8.5, 11.0
    m_left = m_right = m_top = m_bottom = 170 / EVPU_PER_INCH
    if page is not None:
        pct = float(doc.get(page, "percent", "100") or 100)
        h, w = doc.get(page, "height"), doc.get(page, "width")
        if h and w:
            w_in, h_in = int(w) / EVPU_PER_INCH, int(h) / EVPU_PER_INCH
        m_left = abs(int(doc.get(page, "margLeft", "170") or 170)) / EVPU_PER_INCH
        m_right = abs(int(doc.get(page, "margRight", "170") or 170)) / EVPU_PER_INCH
        m_top = abs(int(doc.get(page, "margTop", "170") or 170)) / EVPU_PER_INCH
        m_bottom = abs(int(doc.get(page, "margBottom", "170") or 170)) / EVPU_PER_INCH

    sysspec = doc.other("staffSystemSpec", "1")
    sys_pct = 100.0
    staff_height_frac = 1.0
    if sysspec is not None:
        sh = doc.get(sysspec, "staffHeight")
        if sh:
            staff_height_frac = int(sh) / 6144.0
        sys_pct = float(doc.get(sysspec, "ssysPercent", "100") or 100)
    space_evpu = 24.0 * (sys_pct / 100.0) * staff_height_frac * (pct / 100.0)
    spatium_mm = max(0.4, min(3.0, space_evpu * EVPU_TO_MM))
    return {
        "page_percent": pct, "w_in": w_in, "h_in": h_in,
        "m_left": m_left, "m_right": m_right, "m_top": m_top, "m_bottom": m_bottom,
        "spatium_mm": spatium_mm,
    }

# markingsCategory categoryType -> MuseScore text style prefixes
_CATEGORY_STYLE = {
    "tempoMarks": ["tempo"],
    "tempoAlts": ["tempoChange"],
    "dynamics": ["dynamics"],
    "expressiveText": ["expression"],
    "techniqueText": ["staffText"],
    "rehearsalMarks": ["rehearsalMark"],
}


def _font_style_bits(font: FontInfo) -> int:
    bits = 0
    if font.bold:
        bits |= 1
    if font.italic:
        bits |= 2
    return bits


class StyleBuilder:
    def __init__(self, doc: EnigmaDoc):
        self.doc = doc
        self.values: dict[str, str] = {}

    def build(self) -> dict[str, str]:
        self._music_font()
        self._page_and_spatium()
        self._category_text_styles()
        self._misc_fonts()
        self._header_footer()
        return self.values

    def _header_footer(self):
        """Recreate Finale's recurring page texts (title header, page footer)."""
        doc = self.doc
        header = footer = None
        for pta in doc.other_all("pageTextAssign"):
            start = int(doc.get(pta, "startPage", "0") or 0)
            if doc.get(pta, "endPage") is not None or start < 2:
                continue  # page-range texts; only recurring ones here
            tb = doc.other("textBlock", doc.get(pta, "block"))
            raw = doc.text("blockText", doc.get(tb, "textID")) if tb is not None else None
            if not raw:
                continue
            is_bottom = doc.get(pta, "vpos") == "bottom"
            text = raw
            text = re.sub(r"\^page\((\d*)\)", "$p", text)
            for insert, meta in (("title", "$:workTitle:"), ("subtitle", "$:subtitle:"),
                                 ("composer", "$:composer:"), ("copyright", "$C")):
                text = text.replace(f"^{insert}()", meta)
            text = re.sub(r"\^\w+\([^)]*\)", "", text).strip()
            if not text:
                continue
            if is_bottom:
                footer = text
            else:
                header = text
        if header:
            self.values["showHeader"] = "1"
            self.values["headerFirstPage"] = "0"
            self.values["headerOddEven"] = "0"
            self.values["oddHeaderC"] = header
            self.values["evenHeaderC"] = header
        if footer:
            # $C = copyright (first page only), $p = page number (not on first page)
            footer = footer.replace("$p", "$C$p") if "$p" in footer else footer
            self.values["showFooter"] = "1"
            self.values["footerFirstPage"] = "1"
            self.values["footerOddEven"] = "0"
            self.values["oddFooterC"] = footer
            self.values["evenFooterC"] = footer

    # -------------------------------------------------------------- sections

    def _font_opt(self, ftype: str) -> FontInfo:
        opts = self.doc.options.get("fontOptions")
        if opts is None:
            return FontInfo()
        for f in opts.findall("font"):
            if f.get("type") == ftype:
                return font_info_from_efx(self.doc, f)
        return FontInfo()

    def _music_font(self):
        music = self._font_opt("music")
        ms_font = musescore_music_font(music.family or "Maestro")
        self.values["musicalSymbolFont"] = ms_font
        self.values["musicalTextFont"] = f"{ms_font} Text"
        self.values["dynamicsFont"] = ms_font

    def _page_and_spatium(self):
        doc = self.doc
        pm = page_metrics(doc)
        self.values["pageWidth"] = f"{pm['w_in']:.3f}"
        self.values["pageHeight"] = f"{pm['h_in']:.3f}"
        self.values["pagePrintableWidth"] = f"{pm['w_in'] - pm['m_left'] - pm['m_right']:.3f}"
        self.values["pagePrintableHeight"] = f"{pm['h_in'] - pm['m_top'] - pm['m_bottom']:.3f}"
        for k, v in (("pageOddLeftMargin", pm["m_left"]), ("pageEvenLeftMargin", pm["m_left"]),
                     ("pageOddTopMargin", pm["m_top"]), ("pageEvenTopMargin", pm["m_top"]),
                     ("pageOddBottomMargin", pm["m_bottom"]), ("pageEvenBottomMargin", pm["m_bottom"])):
            self.values[k] = f"{v:.3f}"
        self.values["pageTwosided"] = "1"
        self.values["spatium"] = f"{pm['spatium_mm']:.5g}"  # millimeters

        # staff-to-staff distance from the first system's staff positions
        dists = []
        for el in doc.other_list("instUsed", "1"):
            d = doc.get(el, "distFromTop")
            if d is not None:
                dists.append(int(d))
        if len(dists) > 1:
            deltas = [dists[i] - dists[i + 1] for i in range(len(dists) - 1)]
            avg_delta_evpu = sum(deltas) / len(deltas)  # unscaled system EVPU (space=24)
            staff_dist_sp = max(1.5, avg_delta_evpu / 24.0 - 4.0)
            self.values["staffDistance"] = f"{staff_dist_sp:.2f}"
            self.values["akkoladeDistance"] = f"{staff_dist_sp:.2f}"

        # staff optimization (hide empty staves)
        n_staves = len([el for el in doc.other_all("staffSpec") if el.get("cmper") != "32767"])
        counts = []
        for cmper, els in doc.others.get("instUsed", {}).items():
            if cmper != "0":
                counts.append(len(els))
        if counts and min(counts) < n_staves:
            self.values["hideEmptyStaves"] = "1"
            self.values["dontHideStavesInFirstSystem"] = "1"

    def _category_text_styles(self):
        for cat in self.doc.other_all("markingsCategory"):
            ctype = self.doc.get(cat, "categoryType")
            prefixes = _CATEGORY_STYLE.get(ctype or "")
            if not prefixes:
                continue
            font = font_info_from_efx(self.doc, cat.find("textFont"))
            if font.family is None and font.size is None:
                continue
            for prefix in prefixes:
                if font.family and not is_music_font(font.family):
                    self.values[f"{prefix}FontFace"] = font.family
                if font.size:
                    self.values[f"{prefix}FontSize"] = f"{font.size * FONT_SIZE_FACTOR:.4g}"
                self.values[f"{prefix}FontStyle"] = str(_font_style_bits(font))

    def _misc_fonts(self):
        # default text (Finale textBlock font) and lyrics
        tb = self._font_opt("textBlock")
        if tb.family and not is_music_font(tb.family):
            self.values["defaultFontFace"] = tb.family
            if tb.size:
                self.values["defaultFontSize"] = f"{tb.size * FONT_SIZE_FACTOR:.4g}"
        lyr = self._font_opt("lyricVerse")
        if lyr.family and not is_music_font(lyr.family):
            for prefix in ("lyricsOdd", "lyricsEven"):
                self.values[f"{prefix}FontFace"] = lyr.family
                if lyr.size:
                    self.values[f"{prefix}FontSize"] = f"{lyr.size * FONT_SIZE_FACTOR:.4g}"
        # staff/instrument names: take font of first staff fullName text block
        tup = self._font_opt("tuplet")
        if tup.family and not is_music_font(tup.family):
            self.values["tupletFontFace"] = tup.family
            if tup.size:
                self.values["tupletFontSize"] = f"{tup.size * FONT_SIZE_FACTOR:.4g}"
            self.values["tupletFontStyle"] = str(_font_style_bits(tup))
            self.values["tupletMusicalSymbolsScale"] = "1"


def build_mss(doc: EnigmaDoc) -> bytes:
    values = StyleBuilder(doc).build()
    root = Element("museScore", version="4.60")
    style = SubElement(root, "Style")
    for k, v in sorted(values.items()):
        SubElement(style, k).text = v
    from io import BytesIO
    out = BytesIO()
    ElementTree(root).write(out, pretty_print=True, encoding="UTF-8", xml_declaration=True)
    return out.getvalue()
