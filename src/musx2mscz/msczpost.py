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
    from lxml import etree
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


def postprocess(mscz_path: Path, musx: MusxFile, doc: EnigmaDoc, bind_sounds: bool = True) -> None:
    strip_empty_frames(mscz_path)
    if add_system_locks(mscz_path, doc):
        print("  locked Finale system layout")
    if bind_sounds and uses_aria(musx):
        if bind_aria_sounds(mscz_path):
            print("  bound Garritan ARIA Player to instrument tracks")
        else:
            print("  note: ARIA Player VST not registered in MuseScore; skipped sound binding")
