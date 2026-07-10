"""Percussion note type table and drum map extraction.

The percussion note type table is derived from Finale's PercNoteTypes.txt
as published in the musxdom project (MIT License, Copyright (c) 2025
Robert Patterson, https://github.com/rpatters1/musxdom).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources


def _load_table() -> dict[int, dict]:
    with resources.files("musx2mscz.data").joinpath("perc_note_types.json").open() as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


PERC_NOTE_TYPES = _load_table()


def note_type_name(perc_note_type_id: int) -> str:
    """Human-readable name for a percussion note type id (with order suffix)."""
    base_id = perc_note_type_id & 0xFFF
    order_id = perc_note_type_id >> 12
    entry = PERC_NOTE_TYPES.get(base_id)
    if entry is None:
        return f"Percussion {perc_note_type_id}"
    name = entry["rawName"].replace("%g", "")
    if "%d" in name:
        name = name.replace("%d", f" ({order_id + 1})" if order_id else "")
    return name.strip()


def general_midi_note(perc_note_type_id: int) -> int | None:
    entry = PERC_NOTE_TYPES.get(perc_note_type_id & 0xFFF)
    if entry is None or entry["gm"] < 0:
        return None
    return entry["gm"]


# Map Finale percussion notehead glyph codepoints to MusicXML notehead types.
# Codepoints are from Maestro-compatible percussion fonts (Maestro, Kousaku
# Percussion, Finale Percussion share the base layout for common heads).
_NOTEHEAD_BY_CHAR = {
    207: "normal",  # closed (filled) notehead
    250: "normal",  # half notehead
    119: "normal",  # whole notehead
    87: "normal",   # double whole
    120: "x",       # small x
    88: "x",        # large X
    192: "circle-x",
    171: "x",
    226: "diamond",
    225: "diamond",
    224: "diamond",
    79: "circle-x",
    111: "circle-x",
    243: "triangle",  # triangle up
    45: "slash",
    47: "slash",
}


def notehead_type(codepoint: int | None) -> str:
    if codepoint is None:
        return "normal"
    return _NOTEHEAD_BY_CHAR.get(codepoint, "normal")


@dataclass
class DrumMapNote:
    perc_note_type: int      # full id incl. order bits
    staff_position: int      # harmLev: 0 = middle-C position of treble clef
    closed_notehead: int | None
    half_notehead: int | None
    whole_notehead: int | None

    @property
    def name(self) -> str:
        return note_type_name(self.perc_note_type)

    @property
    def gm_note(self) -> int | None:
        return general_midi_note(self.perc_note_type)

    @property
    def notehead(self) -> str:
        return notehead_type(self.closed_notehead)

    def display_step_octave(self) -> tuple[str, int]:
        """MusicXML display-step/display-octave for this staff position.

        harmLev 0 corresponds to the middle-C position of a treble staff, so we
        express positions as if pitched on a treble clef.
        """
        steps = ("C", "D", "E", "F", "G", "A", "B")
        pos = self.staff_position
        return steps[pos % 7], 4 + pos // 7


def load_drum_maps(doc) -> dict[str, dict[int, DrumMapNote]]:
    """All percussion maps in the document: map_id -> {percNoteType -> DrumMapNote}."""
    maps: dict[str, dict[int, DrumMapNote]] = {}
    for cmper, elems in doc.others.get("percussionNoteInfo", {}).items():
        m: dict[int, DrumMapNote] = {}
        for el in elems:
            pnt = int(doc.get(el, "percNoteType", "0"))
            note = DrumMapNote(
                perc_note_type=pnt,
                staff_position=int(doc.get(el, "harmLev", "0")),
                closed_notehead=_int_or_none(doc.get(el, "closedNotehead")),
                half_notehead=_int_or_none(doc.get(el, "halfNotehead")),
                whole_notehead=_int_or_none(doc.get(el, "wholeNotehead")),
            )
            m[pnt] = note
        maps[cmper] = m
    return maps


def _int_or_none(v):
    return int(v) if v is not None else None


def staff_drum_map_id(doc, staff_cmper: str) -> str | None:
    """Percussion map id for a staff (via others/drumStaff whichDrumLib)."""
    ds = doc.other("drumStaff", staff_cmper)
    if ds is None:
        return None
    return doc.get(ds, "whichDrumLib")
