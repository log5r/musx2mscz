"""Indexed access to an EnigmaXML document.

EnigmaXML stores everything in flat lists under <others>, <details>,
<entries> and <texts>. Random access via XPath is prohibitively slow for
large scores, so we build dict indexes once.
"""

from __future__ import annotations

from io import BytesIO

from lxml import etree


def _strip_namespaces(root) -> None:
    for el in root.iter():
        if isinstance(el.tag, str):
            i = el.tag.find("}")
            if i >= 0:
                el.tag = el.tag[i + 1 :]
    etree.cleanup_namespaces(root)


class EnigmaDoc:
    """Parsed EnigmaXML with fast lookup tables (score view only)."""

    def __init__(self, data: bytes):
        parser = etree.XMLParser(huge_tree=True, remove_comments=True)
        self.tree = etree.parse(BytesIO(data), parser)
        self.root = self.tree.getroot()
        _strip_namespaces(self.root)

        # others: tag -> cmper(str) -> [elements in inci order]
        self.others: dict[str, dict[str, list]] = {}
        # details keyed by entnum: tag -> entnum(str) -> [elements]
        self.details_ent: dict[str, dict[str, list]] = {}
        # details keyed by (cmper1, cmper2): tag -> (c1, c2) -> [elements]
        self.details_c12: dict[str, dict[tuple[str, str], list]] = {}
        # details keyed by single cmper (staffGroup etc.): tag -> cmper -> [elements]
        self.details_c1: dict[str, dict[str, list]] = {}
        # entries: entnum -> element
        self.entries: dict[str, etree._Element] = {}
        # texts: tag -> number(str) -> text
        self.texts: dict[str, dict[str, str]] = {}
        # options: tag -> element
        self.options: dict[str, etree._Element] = {}

        for section in self.root:
            tag = section.tag
            if tag == "others":
                for el in section:
                    if el.get("part") is not None:
                        continue  # score view only
                    self.others.setdefault(el.tag, {}).setdefault(el.get("cmper"), []).append(el)
            elif tag == "details":
                for el in section:
                    if el.get("part") is not None:
                        continue
                    entnum = el.get("entnum")
                    if entnum is not None:
                        self.details_ent.setdefault(el.tag, {}).setdefault(entnum, []).append(el)
                    else:
                        c1, c2 = el.get("cmper1"), el.get("cmper2")
                        if c2 is not None:
                            self.details_c12.setdefault(el.tag, {}).setdefault((c1, c2), []).append(el)
                        elif c1 is not None:
                            self.details_c1.setdefault(el.tag, {}).setdefault(c1, []).append(el)
            elif tag == "entries":
                for el in section:
                    self.entries[el.get("entnum")] = el
            elif tag == "texts":
                for el in section:
                    key = el.get("number") if el.get("number") is not None else el.get("type")
                    self.texts.setdefault(el.tag, {})[key] = el.text
            elif tag == "options":
                for el in section:
                    self.options[el.tag] = el

    # -- convenience accessors -------------------------------------------------

    def other(self, tag: str, cmper: str):
        """First <others> element of `tag` with given cmper, or None."""
        lst = self.others.get(tag, {}).get(str(cmper))
        return lst[0] if lst else None

    def other_list(self, tag: str, cmper: str) -> list:
        return self.others.get(tag, {}).get(str(cmper), [])

    def other_all(self, tag: str) -> list:
        out = []
        for lst in self.others.get(tag, {}).values():
            out.extend(lst)
        return out

    def text(self, tag: str, number: str) -> str | None:
        return self.texts.get(tag, {}).get(str(number))

    @staticmethod
    def get(el, child: str, default=None):
        """Text of a direct child element, or default."""
        if el is None:
            return default
        sub = el.find(child)
        return sub.text if sub is not None and sub.text is not None else default

    @staticmethod
    def has(el, child: str) -> bool:
        return el is not None and el.find(child) is not None
