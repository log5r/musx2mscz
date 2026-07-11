"""Post-processing of the generated .mscz.

Binds the sound library used by the Finale document (e.g. Garritan ARIA
Player) to the MuseScore project's audio settings, when the corresponding
VST is registered with the local MuseScore installation.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from lxml import etree

from . import style
from .enigma import EnigmaDoc
from .musxfile import MusxFile

KNOWN_PLUGINS_PATH = Path.home() / "Library/Application Support/MuseScore/MuseScore4/known_audio_plugins.json"

# known_audio_plugins.json uses camel type names; audiosettings.json uses snake
_RESOURCE_TYPE = {
    "VstPlugin": "vst_plugin",
    "FluidSoundfont": "fluid_soundfont",
    "MuseSamplerSoundPack": "muse_sampler_sound_pack",
    "MusePlugin": "muse_plugin",
}


def uses_aria(musx: MusxFile) -> bool:
    """Finale attaches ARIA (Garritan) states as presets/*.preset with AriaData."""
    return any(b"AriaData" in blob for blob in musx.presets.values())


def find_known_plugin(plugin_id: str) -> dict | None:
    if not KNOWN_PLUGINS_PATH.exists():
        return None
    try:
        plugins = json.loads(KNOWN_PLUGINS_PATH.read_text())
    except Exception:
        return None
    for p in plugins:
        meta = p.get("meta", {})
        if meta.get("id") == plugin_id and p.get("enabled", False):
            return meta
    return None


def _read_mscz(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _write_mscz(path: Path, contents: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in contents.items():
            zf.writestr(name, blob)


def patch_embedded_style(mscz_path: Path, values: dict[str, str]) -> bool:
    contents = _read_mscz(mscz_path)
    mss_name = next((n for n in contents if n.endswith(".mss")), None)
    if mss_name is None:
        return False
    root = etree.fromstring(contents[mss_name])
    style_el = root.find("Style")
    changed = False
    if style_el is None:
        style_el = etree.SubElement(root, "Style")
        changed = True
    for key, value in values.items():
        el = style_el.find(key)
        if el is None:
            el = etree.SubElement(style_el, key)
            changed = True
        if el.text != value:
            el.text = value
            changed = True
    if changed:
        contents[mss_name] = etree.tostring(
            root, encoding="UTF-8", xml_declaration=True)
        _write_mscz(mscz_path, contents)
    return changed


def elevate_system_texts(mscz_path: Path, hints) -> bool:
    contents = _read_mscz(mscz_path)
    mscx_name = next((n for n in contents if n.endswith(".mscx")), None)
    if mscx_name is None:
        return False
    positions = {}
    for text, y_sp in hints:
        positions.setdefault(text, y_sp)
    root = etree.fromstring(contents[mscx_name])
    changed = False
    for tag in ("StaffText", "SystemText"):
        for el in root.iter(tag):
            text_el = el.find("text")
            if text_el is None or el.find("offset") is not None:
                continue
            plain = "".join(text_el.itertext()).strip()
            if plain not in positions:
                continue
            autoplace = etree.Element("autoplace")
            autoplace.text = "0"
            offset = etree.Element("offset", x="0", y=f"{positions[plain]:.2f}")
            el.insert(0, offset)
            el.insert(0, autoplace)
            changed = True
    if changed:
        contents[mscx_name] = etree.tostring(
            root, encoding="UTF-8", xml_declaration=True)
        _write_mscz(mscz_path, contents)
    return changed


def _parts_from_mscx(mscx: str) -> list[tuple[str, str]]:
    """(partId, instrumentId) pairs in score order."""
    out = []
    for m in re.finditer(r'<Part id="(\d+)">(.*?)</Part>', mscx, re.S):
        part_id = m.group(1)
        im = re.search(r'<Instrument id="([^"]*)"', m.group(2))
        instrument_id = im.group(1) if im else ""
        out.append((part_id, instrument_id))
    return out


def bind_aria_sounds(mscz_path: Path) -> bool:
    """Assign the ARIA Player VST instrument to every track in audiosettings.json.

    Returns True if the file was modified.
    """
    meta = find_known_plugin("ARIA Player")
    if meta is None:
        return False
    contents = _read_mscz(mscz_path)
    audio_name = next((n for n in contents if n.endswith("audiosettings.json")), None)
    mscx_name = next((n for n in contents if n.endswith(".mscx")), None)
    if audio_name is None or mscx_name is None:
        return False
    settings = json.loads(contents[audio_name])
    parts = _parts_from_mscx(contents[mscx_name].decode("utf-8", errors="replace"))
    if not parts:
        return False

    resource_meta = {
        "id": meta.get("id"),
        "type": _RESOURCE_TYPE.get(meta.get("type", "VstPlugin"), "vst_plugin"),
        "vendor": meta.get("vendor", ""),
        "attributes": meta.get("attributes", {}),
        "hasNativeEditorSupport": meta.get("hasNativeEditorSupport", True),
    }
    existing = {(t.get("partId"), t.get("instrumentId")) for t in settings.get("tracks", [])}
    tracks = settings.setdefault("tracks", [])
    changed = False
    for part_id, instrument_id in parts:
        if (part_id, instrument_id) in existing:
            continue
        tracks.append({
            "partId": part_id,
            "instrumentId": instrument_id,
            "in": {
                "resourceMeta": resource_meta,
                "unitConfiguration": {},
            },
            "out": {
                "fxChain": {},
                "balance": 0,
                "volumeDb": 0,
            },
            "soloMuteState": {"mute": False, "solo": False},
        })
        changed = True
    if changed:
        contents[audio_name] = json.dumps(settings, indent=4).encode()
        _write_mscz(mscz_path, contents)
    return changed


def add_system_locks(mscz_path: Path, doc: EnigmaDoc) -> bool:
    """Lock Finale's system contents into MuseScore systems.

    MuseScore 4.6 system locks force a measure range into a single system,
    squeezing content as needed - reproducing Finale's horizontal layout.
    """
    meas_cmpers = sorted(
        int(m.get("cmper")) for m in doc.other_all("measSpec") if m.find("shared") is None)
    if not meas_cmpers:
        return False
    index_of = {c: i for i, c in enumerate(meas_cmpers)}
    systems: list[tuple[int, int]] = []
    for el in sorted(doc.other_all("staffSystemSpec"), key=lambda e: int(e.get("cmper"))):
        s, e = doc.get(el, "startMeas"), doc.get(el, "endMeas")
        if s is None:
            continue
        si = index_of.get(int(s))
        if si is None:
            continue
        ei_excl = index_of.get(int(e)) if e is not None else None
        ei = ei_excl - 1 if ei_excl is not None else len(meas_cmpers) - 1
        if ei < si:
            ei = si
        systems.append((si, ei))
    if not systems:
        return False

    contents = _read_mscz(mscz_path)
    mscx_name = next((n for n in contents if n.endswith(".mscx")), None)
    if mscx_name is None:
        return False
    root = etree.fromstring(contents[mscx_name])
    score = root.find("Score")
    if score is None:
        return False
    staff = score.find("Staff")
    if staff is None:
        return False
    eids = []
    for meas in staff.iterchildren("Measure"):
        eid = meas.findtext("eid")
        eids.append(eid)
    if not eids or any(e is None for e in eids):
        return False

    old = score.find("SystemLocks")
    if old is not None:
        score.remove(old)
    locks = etree.SubElement(score, "SystemLocks")
    n = len(eids)
    for si, ei in systems:
        if si >= n:
            continue
        ei = min(ei, n - 1)
        lock = etree.SubElement(locks, "systemLock")
        etree.SubElement(lock, "startMeasure").text = eids[si]
        etree.SubElement(lock, "endMeasure").text = eids[ei]
    contents[mscx_name] = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
    _write_mscz(mscz_path, contents)
    return True


def fix_title_frame_alignment(mscz_path: Path) -> bool:
    """Anchor composer/poet texts in the title frame to the top.

    The MusicXML importer gives them bottom-anchored styles, which inflates
    the auto-sized frame to a large fraction of the page. Finale's page
    texts near the top of page 1 should stay in the header area.
    """
    contents = _read_mscz(mscz_path)
    mscx_name = next((n for n in contents if n.endswith(".mscx")), None)
    if mscx_name is None:
        return False
    mscx = contents[mscx_name].decode("utf-8", errors="replace")
    i = mscx.find("<VBox>")
    j = mscx.find("</VBox>")
    if i < 0 or j < 0:
        return False
    vbox = mscx[i:j]
    new_vbox = vbox
    for style, align in (("composer", "right,top"), ("poet", "left,top"),
                         ("lyricist", "left,top")):
        pattern = re.compile(
            rf"(<Text>(?:(?!</Text>)(?!<align>).)*?<style>{style}</style>)((?:(?!</Text>)(?!<align>).)*?</Text>)",
            re.S)
        new_vbox = pattern.sub(rf"\1\n          <align>{align}</align>\2", new_vbox)
    if new_vbox == vbox:
        return False
    contents[mscx_name] = (mscx[:i] + new_vbox + mscx[j:]).encode()
    _write_mscz(mscz_path, contents)
    return True


_EMPTY_VBOX_RE = re.compile(
    r"<VBox>\s*<height>[^<]*</height>\s*(?:<eid>[^<]*</eid>\s*)?</VBox>\s*")


def strip_empty_frames(mscz_path: Path) -> bool:
    """Remove empty vertical frames the MusicXML importer adds at the top."""
    contents = _read_mscz(mscz_path)
    mscx_name = next((n for n in contents if n.endswith(".mscx")), None)
    if mscx_name is None:
        return False
    mscx = contents[mscx_name].decode("utf-8", errors="replace")
    new = _EMPTY_VBOX_RE.sub("", mscx)
    if new == mscx:
        return False
    contents[mscx_name] = new.encode()
    _write_mscz(mscz_path, contents)
    return True


def postprocess(mscz_path: Path, musx: MusxFile, doc: EnigmaDoc,
                bind_sounds: bool = True, layout_hints=None) -> None:
    patch_embedded_style(mscz_path, style.StyleBuilder(doc).build())
    if layout_hints:
        elevate_system_texts(mscz_path, layout_hints)
    strip_empty_frames(mscz_path)
    fix_title_frame_alignment(mscz_path)
    if add_system_locks(mscz_path, doc):
        print("  locked Finale system layout")
    if bind_sounds and uses_aria(musx):
        if bind_aria_sounds(mscz_path):
            print("  bound Garritan ARIA Player to instrument tracks")
        else:
            print("  note: ARIA Player VST not registered in MuseScore; skipped sound binding")
