"""EnigmaXML -> MusicXML conversion.

Structure and note/measure logic derived from musx2mxl (MIT License,
Copyright (c) Joris Van Eyghen, https://github.com/joris-vaneyghen/musx2mxl),
rewritten around indexed lookups (see enigma.EnigmaDoc) for performance and
extended with percussion maps, font/style fidelity and more direction types.
"""

from __future__ import annotations

import math
import re
from datetime import date
from io import BytesIO

from lxml.etree import Element, SubElement, ElementTree, parse

from . import __version__
from .enigma import EnigmaDoc
from .fonts import FontInfo, font_info_from_efx, parse_enigma_text, plain_text
from .percussion import DrumMapNote, load_drum_maps, staff_drum_map_id
from . import helpers as H

DIVISIONS = 16  # divisions per quarter note

PIANO_BRACE = "3"

DYNAMIC_NAMES = {
    "p", "pp", "ppp", "pppp", "ppppp", "f", "ff", "fff", "ffff", "fffff",
    "mp", "mf", "sf", "sfp", "sfpp", "fp", "rf", "rfz", "sfz", "sffz", "fz", "n", "pf",
}

# Characters in Finale music fonts that build up dynamics strings.
_DYN_CHAR_LETTER = {
    112: "p", 102: "f", 109: "m", 115: "s", 122: "z", 110: "n", 114: "r",
    80: "mp", 70: "mf", 83: "sf", 90: "fz",
    185: "pp", 184: "ppp", 175: "pppp",
    196: "ff", 236: "fff", 235: "ffff",
    130: "sfp", 182: "sfpp", 234: "fp", 167: "sfz", 141: "sffz",
}


def _dynamics_from_text(text: str, music: bool) -> str | None:
    """Return a MusicXML dynamics element name if `text` denotes a dynamic."""
    s = text.strip()
    if not s:
        return None
    if music:
        parts = []
        for ch in s:
            letter = _DYN_CHAR_LETTER.get(ord(ch))
            if letter is None:
                return None
            parts.append(letter)
        s = "".join(parts)
    return s if s in DYNAMIC_NAMES else None


# Articulation specs by Finale charMain codepoint (Maestro-compatible layout).
# artic: children of <articulations>; tremolo: slashes; others: notation flags.
ARTIC_SPECS: dict[int, dict] = {
    46: {"artic": [("staccato", None)]},
    62: {"artic": [("accent", None)]},
    94: {"artic": [("strong-accent", "up")]},
    118: {"artic": [("strong-accent", "down")]},
    45: {"artic": [("tenuto", None)]},
    95: {"artic": [("tenuto", None)]},
    248: {"artic": [("detached-legato", None)]},
    224: {"artic": [("staccatissimo", None)]},
    100: {"artic": [("staccatissimo", None)]},
    171: {"artic": [("staccatissimo", None)]},
    249: {"artic": [("accent", None), ("staccato", None)]},   # accent+staccato
    223: {"artic": [("accent", None), ("staccato", None)]},
    172: {"artic": [("strong-accent", "up"), ("staccato", None)]},
    44: {"artic": [("breath-mark", None)]},
    34: {"artic": [("caesura", None)]},
    85: {"fermata": "upright"},
    117: {"fermata": "inverted"},
    33: {"tremolo": 1},
    64: {"tremolo": 2},
    190: {"tremolo": 3},
    109: {"mordent": True},
    103: {"arpeggiate": True},
    161: {"pedal": "start"},
    42: {"pedal": "stop"},
}


class StaffCtx:
    """Per-staff conversion context."""

    def __init__(self, cmper: str, part_id: str, is_percussion: bool,
                 drum_map: dict[int, DrumMapNote] | None):
        self.cmper = cmper
        self.part_id = part_id
        self.is_percussion = is_percussion
        self.drum_map = drum_map or {}
        self.used_drum_notes: dict[int, DrumMapNote] = {}  # percNoteType -> map note


class Converter:
    def __init__(self, enigmaxml: bytes, metadata: bytes | None):
        self.doc = EnigmaDoc(enigmaxml)
        self.meta_root = None
        if metadata:
            try:
                self.meta_root = parse(BytesIO(metadata)).getroot()
            except Exception:
                try:
                    self.meta_root = parse(
                        BytesIO(metadata.decode("latin1").encode("utf-8"))).getroot()
                except Exception:
                    self.meta_root = None
        self.drum_maps = load_drum_maps(self.doc)
        self.staff_ctx: dict[str, StaffCtx] = {}
        self.rehearsal_counter = 0
        self.warnings: list[str] = []
        self._seen_system_exprs: set = set()

    # ------------------------------------------------------------------ utils

    def warn(self, msg: str):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def block_text(self, block_id: str | None) -> str | None:
        if block_id is None:
            return None
        tb = self.doc.other("textBlock", block_id)
        if tb is None:
            return None
        text_id = self.doc.get(tb, "textID")
        raw = self.doc.text("blockText", text_id)
        return plain_text(raw) if raw else None

    def block_text_font(self, block_id: str | None) -> FontInfo:
        if block_id is None:
            return FontInfo()
        tb = self.doc.other("textBlock", block_id)
        if tb is None:
            return FontInfo()
        raw = self.doc.text("blockText", self.doc.get(tb, "textID"))
        if not raw:
            return FontInfo()
        runs = parse_enigma_text(raw)
        return runs[0].font if runs else FontInfo()

    # ------------------------------------------------------------- staff prep

    def _staff_groups(self):
        groups = []
        elems = []
        for d in (self.doc.details_c1, self.doc.details_c12, self.doc.details_ent):
            for key, m in d.get("staffGroup", {}).items() if isinstance(d.get("staffGroup", {}), dict) else []:
                elems.extend(m)
        # staffGroup may also appear in others in some versions
        elems.extend(self.doc.other_all("staffGroup"))
        for g in elems:
            get = self.doc.get
            groups.append({
                "startInst": get(g, "startInst"),
                "endInst": get(g, "endInst"),
                "fullName": self.block_text(get(g, "fullID")),
                "abbrvName": self.block_text(get(g, "abbrvID")),
                "bracket_id": get(g, "bracket/id") if g.find("bracket/id") is not None else None,
            })
        return groups

    def _group_name(self, param, staff_cmper, groups):
        names = [g[param] for g in groups
                 if g["startInst"] and g["endInst"] and g[param]
                 and int(g["startInst"]) <= int(staff_cmper) <= int(g["endInst"])]
        return " ".join(names) if names else None

    def _piano_group(self, staff_cmper, groups):
        for g in groups:
            if (g["bracket_id"] == PIANO_BRACE and g["startInst"] != g["endInst"]
                    and g["startInst"] and g["endInst"]
                    and int(g["startInst"]) <= int(staff_cmper) <= int(g["endInst"])):
                return g
        return None

    # ------------------------------------------------------------ entry chain

    def frame_entries(self, frame_cmper: str):
        """Yield entry elements of a frame in order."""
        for frameSpec in self.doc.other_list("frameSpec", frame_cmper):
            start = self.doc.get(frameSpec, "startEntry")
            end = self.doc.get(frameSpec, "endEntry")
            if start is None or end is None:
                continue
            entnum = start
            while entnum is not None:
                entry = self.doc.entries.get(entnum)
                if entry is None:
                    break
                yield entry
                if entnum == end:
                    break
                entnum = entry.get("next")

    # ---------------------------------------------------------------- convert

    def convert(self) -> ElementTree:
        doc = self.doc
        score = Element("score-partwise", version="4.0")
        self._handle_metadata(score)
        part_list = SubElement(score, "part-list")

        groups = self._staff_groups()
        staff_specs = [el for el in doc.other_all("staffSpec") if el.get("cmper") != "32767"]
        staff_specs.sort(key=lambda el: int(el.get("cmper")))
        # order staves by instUsed order if available (score layout order)
        order = self._staff_order()
        if order:
            pos = {c: i for i, c in enumerate(order)}
            staff_specs.sort(key=lambda el: pos.get(el.get("cmper"), 10_000 + int(el.get("cmper"))))

        part_ids: dict[str, str] = {}
        i = 1
        for spec in staff_specs:
            cmper = spec.get("cmper")
            piano = self._piano_group(cmper, groups)
            if piano is not None and piano["startInst"] != cmper:
                continue
            part_id = f"P{i}"
            i += 1
            part_ids[cmper] = part_id

            full = self.block_text(self.doc.get(spec, "fullName")) or self._group_name("fullName", cmper, groups)
            abbrv = self.block_text(self.doc.get(spec, "abbrvName")) or self._group_name("abbrvName", cmper, groups)

            sp = SubElement(part_list, "score-part", id=part_id)
            SubElement(sp, "part-name").text = full or ""
            if abbrv:
                SubElement(sp, "part-abbreviation").text = abbrv

            inst_uuid = self.doc.get(spec, "instUuid")
            is_perc = self.doc.get(spec, "notationStyle") == "percussion"
            drum_map = None
            if is_perc:
                map_id = staff_drum_map_id(doc, cmper)
                drum_map = self.drum_maps.get(map_id or "", {})
            ctx = StaffCtx(cmper, part_id, is_perc, drum_map)
            self.staff_ctx[cmper] = ctx

            if is_perc:
                # score-instruments are appended after conversion (we then know
                # which drum notes are actually used); remember the element.
                ctx.score_part_el = sp
            else:
                name, sound = H.translate_instrument(inst_uuid)
                if name:
                    si = SubElement(sp, "score-instrument", id=f"{part_id}-I1")
                    SubElement(si, "instrument-name").text = name
                    if sound:
                        SubElement(si, "instrument-sound").text = sound

        meas_specs = doc.other_all("measSpec")
        meas_specs = [m for m in meas_specs if m.find("shared") is None]
        meas_specs.sort(key=lambda el: int(el.get("cmper")))

        self._compute_breaks()

        first_staff = True
        for spec in staff_specs:
            cmper = spec.get("cmper")
            if cmper not in part_ids:
                continue
            part = SubElement(score, "part", id=part_ids[cmper])
            self._convert_part(part, spec, cmper, meas_specs, groups, staff_specs,
                               system_items=first_staff)
            first_staff = False

        # append percussion score-instruments now that usage is known
        for ctx in self.staff_ctx.values():
            if ctx.is_percussion and getattr(ctx, "score_part_el", None) is not None:
                self._emit_drum_instruments(ctx)

        return ElementTree(score)

    def _compute_breaks(self):
        """Measure numbers where Finale starts a new system / page."""
        doc = self.doc
        systems = {}
        for el in doc.other_all("staffSystemSpec"):
            start = doc.get(el, "startMeas")
            if start is not None:
                systems[el.get("cmper")] = start
        self.system_starts = {m for c, m in systems.items() if c != "1" and m != "1"}
        self.page_starts = set()
        for el in doc.other_all("pageSpec"):
            if el.get("cmper") == "1":
                continue
            first_sys = doc.get(el, "firstSystem")
            if first_sys and first_sys in systems and int(first_sys) > 0:
                self.page_starts.add(systems[first_sys])
        self.system_starts -= self.page_starts

    def _staff_order(self):
        """Staff cmpers in score order from instUsed lists (cmper 0 = score view)."""
        used = self.doc.other_list("instUsed", "0")
        order = []
        for el in used:
            inst = self.doc.get(el, "inst")
            if inst is not None:
                order.append(inst)
        return order

    def _emit_drum_instruments(self, ctx: StaffCtx):
        sp = ctx.score_part_el
        notes = sorted(ctx.used_drum_notes.values(), key=lambda n: -n.staff_position)
        midi_els = []
        for n in notes:
            iid = f"{ctx.part_id}-I{n.perc_note_type}"
            si = SubElement(sp, "score-instrument", id=iid)
            SubElement(si, "instrument-name").text = n.name
            gm = n.gm_note
            mi = Element("midi-instrument", id=iid)
            SubElement(mi, "midi-channel").text = "10"
            if gm is not None:
                SubElement(mi, "midi-unpitched").text = str(gm + 1)
            midi_els.append(mi)
        for mi in midi_els:
            sp.append(mi)

    # ------------------------------------------------------------------ parts

    def _convert_part(self, part, spec, staff_cmper, meas_specs, groups, staff_specs,
                      system_items: bool):
        doc = self.doc
        get = doc.get
        piano = self._piano_group(staff_cmper, groups)
        piano_staves = None
        if piano:
            piano_staves = [s.get("cmper") for s in staff_specs
                            if int(piano["startInst"]) <= int(s.get("cmper")) <= int(piano["endInst"])]

        transp_key_adjust = int(get(spec, "transposition/keysig/adjust", "0") or 0) \
            if spec.find("transposition/keysig/adjust") is not None else 0
        transp_interval = int(get(spec, "transposition/keysig/interval", "0") or 0) \
            if spec.find("transposition/keysig/interval") is not None else 0

        timeSigDoAbrvCommon = doc.has(self.doc.options.get("timeSignatureOptions"), "timeSigDoAbrvCommon")
        timeSigDoAbrvCut = doc.has(self.doc.options.get("timeSignatureOptions"), "timeSigDoAbrvCut")

        current_key = -1
        current_beats = None
        current_divbeat = None
        current_clef = None
        ending_cnt = 0
        nb = len(meas_specs)

        for idx, meas in enumerate(meas_specs):
            meas_cmper = meas.get("cmper")
            measure = SubElement(part, "measure", number=meas_cmper)
            if system_items:
                if meas_cmper in self.page_starts:
                    SubElement(measure, "print", {"new-page": "yes"})
                elif meas_cmper in self.system_starts:
                    SubElement(measure, "print", {"new-system": "yes"})
            beats = get(meas, "beats")
            divbeat = get(meas, "divbeat")
            key_el = meas.find("keySig/key")
            key = int(key_el.text) if key_el is not None else None
            barline = get(meas, "barline", "normal")
            if idx == nb - 1:
                barline = "final"
            forRepBar = doc.has(meas, "forRepBar")
            bacRepBar = doc.has(meas, "bacRepBar")
            barEnding = doc.has(meas, "barEnding")

            if doc.has(meas, "txtRepeats"):
                self._emit_text_repeats(measure, meas_cmper, staff_cmper, system_items)

            self._emit_measure_smart_shapes(measure, meas_cmper, staff_cmper)

            attributes = None
            if idx == 0:
                attributes = SubElement(measure, "attributes")
                SubElement(attributes, "divisions").text = str(DIVISIONS)
                if piano_staves:
                    SubElement(attributes, "staves").text = str(len(piano_staves))
                if self.staff_ctx[staff_cmper].is_percussion:
                    lines = get(spec, "staffLines", "5")
                    if lines != "5":
                        sd = SubElement(attributes, "staff-details")
                        SubElement(sd, "staff-lines").text = lines

            if key != current_key:
                attributes = self._attr(measure, attributes)
                self._emit_key(attributes, key, transp_key_adjust, transp_interval,
                               self.staff_ctx[staff_cmper].is_percussion)
                current_key = key

            if beats != current_beats or divbeat != current_divbeat:
                attributes = self._attr(measure, attributes)
                self._emit_time(attributes, beats, divbeat, timeSigDoAbrvCommon, timeSigDoAbrvCut)
                current_beats, current_divbeat = beats, divbeat

            if forRepBar or barEnding:
                lb = SubElement(measure, "barline", location="left")
                if barEnding:
                    ending_cnt += 1
                    SubElement(lb, "ending", number=str(ending_cnt), type="start").text = f"{ending_cnt}."
                if forRepBar:
                    SubElement(lb, "bar-style").text = "heavy-light"
                    SubElement(lb, "repeat", direction="forward")

            key_for_pitch = key
            if piano_staves:
                clefs = {}
                prev = False
                for staff_id, pcmper in enumerate(piano_staves, start=1):
                    if prev:
                        backup = SubElement(measure, "backup")
                        SubElement(backup, "duration").text = str(
                            (int(current_beats) * int(current_divbeat) * DIVISIONS) // 1024)
                    if doc.has(meas, "hasChord"):
                        self._emit_chords(measure, pcmper, meas_cmper, key_for_pitch, transp_key_adjust, staff_id)
                    clef = self._process_gfhold(
                        measure, pcmper, meas_cmper, staff_id, key_for_pitch,
                        transp_key_adjust, transp_interval, current_beats, current_divbeat,
                        staff_cmper, system_items)
                    clefs[staff_id] = clef
                    prev = True
                if clefs != current_clef:
                    attributes = self._attr(measure, attributes)
                    for sid, clef in clefs.items():
                        self._emit_clef(attributes, clef, sid)
                    current_clef = clefs
            else:
                if doc.has(meas, "hasChord"):
                    self._emit_chords(measure, staff_cmper, meas_cmper, key_for_pitch, transp_key_adjust, 1)
                clef = self._process_gfhold(
                    measure, staff_cmper, meas_cmper, None, key_for_pitch,
                    transp_key_adjust, transp_interval, current_beats, current_divbeat,
                    staff_cmper, system_items)
                if clef != current_clef:
                    attributes = self._attr(measure, attributes)
                    self._emit_clef(attributes, clef, None)
                    current_clef = clef

            if attributes is not None:
                H.reorder_children(attributes, [
                    "footnote", "level", "divisions", "key", "time", "staves", "part-symbol",
                    "instruments", "clef", "staff-details", "transpose", "for-part",
                    "directive", "measure-style"])

            rb = SubElement(measure, "barline", location="right")
            SubElement(rb, "bar-style").text = H.translate_bar_style(barline, bacRepBar, barEnding)
            if barEnding:
                SubElement(rb, "ending", number=str(ending_cnt), type="stop").text = f"{ending_cnt}."
            else:
                ending_cnt = 0
            if bacRepBar:
                SubElement(rb, "repeat", direction="backward", winged="none")

    @staticmethod
    def _attr(measure, attributes):
        if attributes is None:
            attributes = SubElement(measure, "attributes")
        return attributes

    # ------------------------------------------------------------- attributes

    def _emit_key(self, attributes, key, transp_key_adjust, transp_interval, is_perc):
        if is_perc:
            key_ = SubElement(attributes, "key")
            SubElement(key_, "fifths").text = "0"
            return
        mode, fifths = H.calculate_mode_and_key_fifths(key, transp_key_adjust)
        key_ = SubElement(attributes, "key")
        SubElement(key_, "fifths").text = str(fifths)
        SubElement(key_, "mode").text = mode
        if transp_interval:
            diatonic, chromatic, octave_change = H.calculate_transpose(transp_interval)
            transpose = SubElement(attributes, "transpose")
            SubElement(transpose, "diatonic").text = str(diatonic)
            SubElement(transpose, "chromatic").text = str(chromatic)
            if octave_change:
                SubElement(transpose, "octave-change").text = str(octave_change)

    def _emit_time(self, attributes, beats, divbeat, abrv_common, abrv_cut):
        time_ = SubElement(attributes, "time")
        b = SubElement(time_, "beats")
        bt = SubElement(time_, "beat-type")
        if int(divbeat) % 1536 == 0:
            bt.text = "8"
            b.text = str(int(beats) * 3 * int(divbeat) // 1536)
        elif 4096 % int(divbeat) == 0:
            b.text = beats
            bt.text = str(4096 // int(divbeat))
            if beats == "4" and divbeat == "1024" and abrv_common:
                time_.set("symbol", "common")
            if beats == "2" and divbeat == "2048" and abrv_cut:
                time_.set("symbol", "cut")
        else:
            self.warn(f"Unknown divbeat {divbeat}")
            b.text = beats or "4"
            bt.text = "4"

    def _clef_info(self, clefID):
        if clefID:
            clef_def = None
            opts = self.doc.options.get("clefOptions")
            if opts is not None:
                for cd in opts.findall("clefDef"):
                    if cd.get("index") == str(clefID):
                        clef_def = cd
                        break
            clef_char = self.doc.get(clef_def, "clefChar")
            sign, octave_change = H.translate_clef_sign(clef_char)
            y = int(self.doc.get(clef_def, "clefYDisp", "0") or 0)
            if sign == "percussion":
                return {"sign": "percussion", "line": None, "oct": "0"}
            return {"sign": sign, "line": str(5 + y // 2), "oct": str(octave_change)}
        return {"sign": "G", "line": "2", "oct": "0"}

    def _emit_clef(self, attributes, clefID, staff_id):
        info = self._clef_info(clefID)
        clef = SubElement(attributes, "clef")
        if staff_id is not None:
            clef.set("number", str(staff_id))
        SubElement(clef, "sign").text = info["sign"]
        if info["line"]:
            SubElement(clef, "line").text = info["line"]
        if info["oct"] != "0":
            SubElement(clef, "clef-octave-change").text = info["oct"]

    # ------------------------------------------------------------- directions

    def _emit_text_repeats(self, measure, meas_cmper, staff_cmper, system_items):
        doc = self.doc
        for tra in doc.other_list("textRepeatAssign", meas_cmper):
            top_only = doc.has(tra, "topStaffOnly")
            staff_list = doc.get(tra, "staffList")
            if top_only and not system_items:
                continue
            if not top_only and staff_list is not None and staff_list != staff_cmper:
                continue
            repnum = doc.get(tra, "repnum")
            if repnum is None:
                continue
            trt = doc.other("textRepeatText", repnum)
            text = doc.get(trt, "rptText") if trt is not None else None
            if text is None:
                continue
            if text == "%":
                d = SubElement(measure, "direction", placement="above")
                SubElement(SubElement(d, "direction-type"), "segno")
            elif text == "Þ":
                d = SubElement(measure, "direction", placement="above")
                SubElement(SubElement(d, "direction-type"), "coda")
            else:
                d = SubElement(measure, "direction", placement="below")
                SubElement(SubElement(d, "direction-type"), "words").text = plain_text(text)

    def _emit_measure_smart_shapes(self, measure, meas_cmper, staff_cmper):
        doc = self.doc
        for mark in doc.other_list("smartShapeMeasMark", meas_cmper):
            shape_num = doc.get(mark, "shapeNum")
            shape = doc.other("smartShape", shape_num) if shape_num else None
            if shape is None:
                continue
            stype = doc.get(shape, "shapeType")
            s_meas = doc.get(shape, "startTermSeg/endPt/meas")
            s_inst = doc.get(shape, "startTermSeg/endPt/inst")
            s_edu = doc.get(shape, "startTermSeg/endPt/edu")
            e_meas = doc.get(shape, "endTermSeg/endPt/meas")
            e_inst = doc.get(shape, "endTermSeg/endPt/inst")
            e_edu = doc.get(shape, "endTermSeg/endPt/edu")

            def _dir(placement="below"):
                return SubElement(measure, "direction", placement=placement)

            def _offset(d, edu):
                if edu:
                    SubElement(d, "offset").text = str(math.ceil((int(edu) * DIVISIONS) / 1024))

            if stype in ("cresc", "decresc"):
                anchor = s_inst if stype == "cresc" else e_inst
                wedge_type = "crescendo" if stype == "cresc" else "diminuendo"
                if s_meas == meas_cmper and anchor == staff_cmper:
                    d = _dir()
                    _offset(d, s_edu)
                    SubElement(SubElement(d, "direction-type"), "wedge", type=wedge_type)
                if e_meas == meas_cmper and anchor == staff_cmper:
                    d = _dir()
                    _offset(d, e_edu)
                    SubElement(SubElement(d, "direction-type"), "wedge", type="stop")
            elif stype in ("octaveUp", "octaveDown", "twoOctaveUp", "twoOctaveDown"):
                if s_inst != staff_cmper:
                    continue
                size = "15" if "two" in stype else "8"
                if s_meas == meas_cmper:
                    d = _dir("above" if "Up" in stype else "below")
                    _offset(d, s_edu)
                    SubElement(SubElement(d, "direction-type"), "octave-shift",
                               type="down" if "Up" in stype else "up", size=size)
                if e_meas == meas_cmper:
                    d = _dir("above" if "Up" in stype else "below")
                    _offset(d, e_edu)
                    SubElement(SubElement(d, "direction-type"), "octave-shift",
                               type="stop", size=size)

    def _category(self, category_id: str):
        return self.doc.other("markingsCategory", category_id)

    def _expression_font(self, expr_def, category) -> FontInfo:
        """Effective base font of a text expression."""
        if self.doc.has(expr_def, "useCategoryFonts") and category is not None:
            return font_info_from_efx(self.doc, category.find("textFont"))
        return FontInfo()

    def _emit_expressions(self, measure, meas_cmper, staff_cmper, staff_id, system_items):
        doc = self.doc
        for assign in doc.other_list("measExprAssign", meas_cmper):
            expr_id = doc.get(assign, "textExprID")
            if expr_id is None:
                continue
            expr_def = doc.other("textExprDef", expr_id)
            if expr_def is None:
                continue
            staff_assign = doc.get(assign, "staffAssign")
            horz_edu = doc.get(assign, "horzEduOff")
            category_id = doc.get(expr_def, "categoryID")
            category = self._category(category_id) if category_id else None
            cat_type = doc.get(category, "categoryType") if category is not None else "misc"

            text_key = doc.get(expr_def, "textIDKey")
            tb = doc.other("textBlock", text_key) if text_key else None
            raw = doc.text("expression", doc.get(tb, "textID")) if tb is not None else None
            if raw is None:
                continue

            base_font = self._expression_font(expr_def, category)
            runs = parse_enigma_text(raw, base_font)
            text = "".join(r.text for r in runs).strip()

            vert = doc.get(expr_def, "vertMeasExprAlign")
            placement = "below" if vert == "belowStaffOrEntry" else "above"

            is_system = cat_type in ("tempoMarks", "tempoAlts", "rehearsalMarks")
            if is_system:
                if not system_items:
                    continue
                seen_key = (meas_cmper, expr_id)
                if seen_key in self._seen_system_exprs:
                    continue
                self._seen_system_exprs.add(seen_key)
            elif staff_assign != staff_cmper:
                continue

            def _dir():
                d = SubElement(measure, "direction", placement=placement)
                if horz_edu:
                    SubElement(d, "offset").text = str(math.ceil((int(horz_edu) * DIVISIONS) / 1024))
                if staff_id:
                    SubElement(d, "staff").text = str(staff_id)
                return d

            # dynamics?
            music_run = any(r.font.music for r in runs)
            dyn = _dynamics_from_text(text, music_run)
            if dyn is None and cat_type == "dynamics":
                dyn = _dynamics_from_text(text, True) or _dynamics_from_text(text, False)
            if dyn is not None:
                d = _dir()
                dt = SubElement(d, "direction-type")
                SubElement(SubElement(dt, "dynamics"), dyn)
                continue

            if cat_type == "tempoMarks":
                self._emit_tempo(measure, _dir, raw, runs)
            elif cat_type == "rehearsalMarks":
                d = _dir()
                dt = SubElement(d, "direction-type")
                self.rehearsal_counter += 1
                label = text or self._rehearsal_label(self.rehearsal_counter)
                SubElement(dt, "rehearsal").text = label
            else:
                if not text:
                    continue
                d = _dir()
                dt = SubElement(d, "direction-type")
                words = SubElement(dt, "words")
                words.text = self._runs_display_text(runs)
                self._apply_font(words, runs, category, cat_type)

    @staticmethod
    def _rehearsal_label(n: int) -> str:
        label = ""
        n -= 1
        while True:
            label = chr(ord("A") + n % 26) + label
            n = n // 26 - 1
            if n < 0:
                break
        return label

    def _apply_font(self, words, runs, category, cat_type):
        """Set explicit font attributes when they deviate from sane defaults."""
        font = None
        for r in runs:
            if not r.font.music and r.text.strip():
                font = r.font
                break
        if font is None:
            return
        if font.italic:
            words.set("font-style", "italic")
        if font.bold:
            words.set("font-weight", "bold")

    # metronome note characters by music font family (default covers Engraver)
    _METRO_CHARS_DEFAULT = {"x": ("16th", False), "e": ("eighth", False),
                            "q": ("quarter", False), "h": ("half", False),
                            "w": ("whole", False)}
    # Rentaro/Kousaku (Finale Japan fonts): '!'=64th ... '%'=quarter '&'=half "'"=whole
    _METRO_CHARS_JP = {"!": ("64th", False), '"': ("32nd", False), "#": ("16th", False),
                       "$": ("eighth", False), "%": ("quarter", False),
                       "&": ("half", False), "'": ("whole", False)}
    _METRO_CHARS_BY_FONT = {
        "rentaro": _METRO_CHARS_JP,
        "kousaku": _METRO_CHARS_JP,
    }
    _UNICODE_NOTE = {"whole": "\U0001D15D", "half": "\U0001D15E", "quarter": "♩",
                     "eighth": "♪", "16th": "\U0001D161", "32nd": "\U0001D162",
                     "64th": "\U0001D163"}

    def _table_for(self, font):
        return self._METRO_CHARS_BY_FONT.get(
            (font.family or "").strip().lower(), self._METRO_CHARS_DEFAULT)

    def _music_text_display(self, text: str, table) -> str:
        out = []
        for ch in text:
            if ch in table:
                out.append(self._UNICODE_NOTE[table[ch][0]])
            elif ch.isascii():
                out.append(ch)
            # non-ascii glyphs from music fonts are dropped
        return "".join(out)

    def _runs_display_text(self, runs) -> str:
        parts = []
        for r in runs:
            if r.font.music:
                parts.append(self._music_text_display(r.text, self._table_for(r.font)))
            else:
                parts.append(r.text)
        return "".join(parts).strip()

    def _parse_tempo_runs(self, runs):
        """Font-aware parse of a tempo mark into (words, beat_unit, dot, per_minute, paren)."""
        # locate a music-font run containing the beat-unit glyph
        beat_unit = None
        has_dot = False
        unit_idx = None
        ch_pos = None
        for i, r in enumerate(runs):
            if not r.font.music:
                continue
            table = self._METRO_CHARS_BY_FONT.get(
                (r.font.family or "").strip().lower(), self._METRO_CHARS_DEFAULT)
            for j, ch in enumerate(r.text):
                if ch in table and "=" in r.text[j + 1:] + "".join(
                        rr.text for rr in runs[i + 1:]):
                    beat_unit, _ = table[ch]
                    unit_idx, ch_pos = i, j
                    break
            if beat_unit is not None:
                break
        if beat_unit is None:
            return None
        unit_run = runs[unit_idx].text
        rest = unit_run[ch_pos + 1:]
        if rest.lstrip()[:1] in (".", "d") and rest.lstrip()[:1]:
            has_dot = True
            rest = rest.lstrip()[1:]
        before = "".join(r.text for r in runs[:unit_idx]) + unit_run[:ch_pos]
        after = rest + "".join(r.text for r in runs[unit_idx + 1:])
        m = re.match(r"\s*=\s*(c[a.]{0,2}\s*)?(\d+)\s*(\))?", after)
        if not m:
            return None
        per_minute = ("c. " if m.group(1) else "") + m.group(2)
        parentheses = "no"
        words = before.strip()
        if words.endswith("("):
            words = words[:-1].rstrip()
            parentheses = "yes"
        tail = after[m.end():].strip()
        if parentheses == "yes" and tail.endswith(")"):
            tail = tail[:-1].rstrip()
        if tail and tail != ")":
            table = self._table_for(runs[unit_idx].font)
            tail = self._music_text_display(tail, table)
            words = (words + " " + tail).strip() if words else tail
        return words, beat_unit, has_dot, per_minute, parentheses

    def _emit_tempo(self, measure, _dir, raw, runs):
        parsed = self._parse_tempo_runs(runs)
        if parsed is not None:
            words, beat_unit, has_dot, per_minute, parentheses = parsed
        else:
            # no parsable "note = number": render everything as tempo words,
            # converting music-font note glyphs to unicode equivalents
            words = self._runs_display_text(runs)
            beat_unit = has_dot = per_minute = parentheses = None
        d = _dir()
        if words:
            dt = SubElement(d, "direction-type")
            SubElement(dt, "words").text = words
        if beat_unit and per_minute:
            dt = SubElement(d, "direction-type")
            metronome = SubElement(dt, "metronome", parentheses=parentheses)
            SubElement(metronome, "beat-unit").text = beat_unit
            if has_dot:
                SubElement(metronome, "beat-unit-dot")
            SubElement(metronome, "per-minute").text = per_minute
            quarters = {"whole": 4, "half": 2, "quarter": 1, "eighth": 0.5, "16th": 0.25}.get(beat_unit, 1)
            if has_dot:
                quarters *= 1.5
            try:
                bpm = float(re.sub(r"[^\d.]", "", per_minute))
                sound = SubElement(d, "sound")
                sound.set("tempo", f"{bpm * quarters:g}")
            except ValueError:
                pass

    # ----------------------------------------------------------------- chords

    def _emit_chords(self, measure, staff_cmper, meas_cmper, key, transp_key_adjust, staff_id):
        doc = self.doc
        for ca in doc.details_c12.get("chordAssign", {}).get((staff_cmper, meas_cmper), []):
            get = doc.get
            suffix_text = ""
            suffix_cmper = get(ca, "suffix")
            if suffix_cmper:
                for cs in doc.other_list("chordSuffix", suffix_cmper):
                    s = get(cs, "suffix")
                    if s is None:
                        continue
                    if s == "209":
                        suffix_text += "b"
                    elif int(s) >= 20:
                        suffix_text += chr(int(s))
                    else:
                        suffix_text += s
            root_num, root_alter = get(ca, "rootScaleNum"), get(ca, "rootAlter")
            if suffix_text == "es":
                suffix_text, root_alter = "", "-1"
            if suffix_text == "is":
                suffix_text, root_alter = "", "1"
            suffix = H.translate_chord_suffix(suffix_text)
            harmony = SubElement(measure, "harmony")
            chord_root = SubElement(harmony, "root")
            step, alter = H.translate_chord_step(key, transp_key_adjust, root_num, root_alter)
            SubElement(chord_root, "root-step").text = step
            if alter != 0:
                SubElement(chord_root, "root-alter").text = str(alter)
            kind = SubElement(harmony, "kind")
            kind.text = suffix["kind"]
            kind.set("use-symbols", suffix["use-symbols"])
            kind.set("parentheses-degrees", suffix["parentheses-degrees"])
            if suffix["text"]:
                kind.set("text", suffix["text"])
            if doc.has(ca, "showAltBass"):
                bass = SubElement(harmony, "bass")
                if get(ca, "bassPosition") == "underRoot":
                    bass.set("arrangement", "vertical")
                bstep, balter = H.translate_chord_step(
                    key, transp_key_adjust, get(ca, "bassScaleNum"), get(ca, "bassAlter"))
                SubElement(bass, "bass-step").text = str(bstep)
                if balter != 0:
                    SubElement(bass, "bass-alter").text = str(balter)
            for degree in suffix["degrees"]:
                deg = SubElement(harmony, "degree")
                SubElement(deg, "degree-value").text = str(degree["degree-value"])
                SubElement(deg, "degree-alter").text = str(degree["degree-alter"])
                SubElement(deg, "degree-type").text = str(degree["degree-type"])
            horz = get(ca, "horzEdu")
            if horz:
                SubElement(harmony, "offset").text = str(math.ceil((int(horz) * DIVISIONS) / 1024))
            if staff_id > 1:
                SubElement(harmony, "staff").text = str(staff_id)

    # ---------------------------------------------------------------- gfholds

    def _process_gfhold(self, measure, staff_cmper, meas_cmper, staff_id, key,
                        transp_key_adjust, transp_interval, current_beats, current_divbeat,
                        expr_staff_cmper, system_items):
        doc = self.doc
        gfholds = doc.details_c12.get("gfhold", {}).get((staff_cmper, meas_cmper), [])
        clefID = None

        meas = doc.other("measSpec", meas_cmper)
        if doc.has(meas, "hasExpr") and (staff_id is None or staff_id == 1):
            self._emit_expressions(measure, meas_cmper, expr_staff_cmper, staff_id, system_items)

        if not gfholds:
            # keep the staff's prevailing clef; fill with a measure rest
            first = None
            gf_map = doc.details_c12.get("gfhold", {})
            for (c1, _c2), els in gf_map.items():
                if c1 == staff_cmper:
                    for el in els:
                        if doc.get(el, "clefID") is not None:
                            first = doc.get(el, "clefID")
                            break
                if first:
                    break
            clefID = first
            self._emit_measure_rest(measure, staff_id, current_beats, current_divbeat)
            return clefID

        for gfhold in gfholds:
            if doc.get(gfhold, "clefID") is not None:
                clefID = doc.get(gfhold, "clefID")
            has_prev = False
            for frame_num in range(1, 5):
                frame = doc.get(gfhold, f"frame{frame_num}")
                if frame is None:
                    continue
                if has_prev:
                    backup = SubElement(measure, "backup")
                    SubElement(backup, "duration").text = str(
                        (int(current_beats) * int(current_divbeat) * DIVISIONS) // 1024)
                voice = frame_num if staff_id is None else (staff_id - 1) * 4 + frame_num
                self._process_frame(measure, frame, staff_id, voice, key,
                                    transp_key_adjust, transp_interval, staff_cmper)
                has_prev = True
        return clefID

    def _emit_measure_rest(self, measure, staff_id, beats, divbeat):
        if beats is None or divbeat is None:
            return
        dura_edu = int(beats) * int(divbeat)
        note = SubElement(measure, "note")
        SubElement(note, "rest", measure="yes")
        SubElement(note, "duration").text = str((dura_edu * DIVISIONS) // 1024)
        voice = (staff_id - 1) * 4 + 1 if staff_id else 1
        SubElement(note, "voice").text = str(voice)
        if staff_id:
            SubElement(note, "staff").text = str(staff_id)

    # ---------------------------------------------------------------- entries

    def _process_frame(self, measure, frame_cmper, staff_id, voice, key,
                       transp_key_adjust, transp_interval, staff_cmper):
        tuplet_attrs: list[dict] = []
        for entry in self.frame_entries(frame_cmper):
            tuplet_attrs = self._process_entry(
                measure, entry, staff_id, voice, key, transp_key_adjust,
                transp_interval, tuplet_attrs, staff_cmper)

    def _lookup_perc_codes(self, entnum: str) -> dict[str, int]:
        out = {}
        for el in self.doc.details_ent.get("percussionNoteCode", {}).get(entnum, []):
            note_id = self.doc.get(el, "noteID")
            code = self.doc.get(el, "noteCode")
            if note_id is not None and code is not None:
                out[note_id] = int(code)
        return out

    def _entry_smart_shapes(self, entnum: str, notations, note_el):
        doc = self.doc
        for mark in doc.details_ent.get("smartShapeEntryMark", {}).get(entnum, []):
            shape_num = doc.get(mark, "shapeNum")
            shape = doc.other("smartShape", shape_num) if shape_num else None
            if shape is None:
                continue
            stype = doc.get(shape, "shapeType")
            start_entry = doc.get(shape, "startTermSeg/endPt/entryNum")
            end_entry = doc.get(shape, "endTermSeg/endPt/entryNum")
            if stype in ("slurAuto", "slurUp", "slurDown", "dashSlurAuto", "dashSlurUp", "dashSlurDown"):
                line_type = "dashed" if stype.startswith("dash") else None
                if start_entry == entnum:
                    el = SubElement(notations, "slur", number="1", type="start")
                    if line_type:
                        el.set("line-type", line_type)
                elif end_entry == entnum:
                    SubElement(notations, "slur", number="1", type="stop")
            elif stype in ("trillExt", "trill"):
                orn = notations.find("ornaments")
                if orn is None:
                    orn = SubElement(notations, "ornaments")
                if stype == "trill" and start_entry == entnum:
                    SubElement(orn, "trill-mark")
                if start_entry == entnum:
                    SubElement(orn, "wavy-line", type="start")
                elif end_entry == entnum:
                    SubElement(orn, "wavy-line", type="stop")

    def _handle_tuplet_start(self, entry, notations, tuplet_attrs):
        entnum = entry.get("entnum")
        for td in self.doc.details_ent.get("tupletDef", {}).get(entnum, []):
            if not self.doc.has(td, "symbolicNum"):
                continue
            idx = max([int(a["number"]) for a in tuplet_attrs], default=0) + 1
            attrs = {
                "symbolicNum": self.doc.get(td, "symbolicNum"),
                "symbolicDur": self.doc.get(td, "symbolicDur"),
                "refNum": self.doc.get(td, "refNum"),
                "refDur": self.doc.get(td, "refDur"),
                "count": 0,
                "number": str(idx),
            }
            tuplet = SubElement(notations, "tuplet", number=str(idx), type="start")
            if idx > 1:
                actual_type, _ = H.calculate_type_and_dots(int(attrs["symbolicDur"]))
                normal_type, _ = H.calculate_type_and_dots(int(attrs["refDur"]))
                ta = SubElement(tuplet, "tuplet-actual")
                SubElement(ta, "tuplet-number").text = attrs["symbolicNum"]
                SubElement(ta, "tuplet-type").text = actual_type
                tn = SubElement(tuplet, "tuplet-normal")
                SubElement(tn, "tuplet-number").text = attrs["refNum"]
                SubElement(tn, "tuplet-type").text = normal_type
            tuplet_attrs.append(attrs)

    def _close_tuplets(self, note, notations, dura, tuplet_attrs):
        if not tuplet_attrs:
            return
        is_nested = len(tuplet_attrs) > 1
        H.count_tuplet(tuplet_attrs, dura)
        actual, normal = 1, 1
        for attrs in list(tuplet_attrs):
            actual *= int(attrs["symbolicNum"])
            normal *= int(attrs["refNum"])
            if attrs["count"] >= int(attrs["symbolicNum"]) - 1e-6:
                SubElement(notations, "tuplet", number=attrs["number"], type="stop")
                tuplet_attrs.remove(attrs)
        tm = SubElement(note, "time-modification")
        SubElement(tm, "actual-notes").text = str(actual)
        SubElement(tm, "normal-notes").text = str(normal)
        if is_nested and tuplet_attrs:
            normal_type, _ = H.calculate_type_and_dots(int(tuplet_attrs[0]["symbolicDur"]))
            SubElement(tm, "normal-type").text = normal_type

    @staticmethod
    def _emit_articulations(note, notations, artic_specs):
        art_el = None
        orn_el = None
        for spec in artic_specs:
            for tag, typ in spec.get("artic", []):
                if art_el is None:
                    art_el = SubElement(notations, "articulations")
                a = SubElement(art_el, tag)
                if typ:
                    a.set("type", typ)
            if spec.get("tremolo"):
                if orn_el is None:
                    orn_el = SubElement(notations, "ornaments")
                SubElement(orn_el, "tremolo", type="single").text = str(spec["tremolo"])
            if spec.get("mordent"):
                if orn_el is None:
                    orn_el = SubElement(notations, "ornaments")
                SubElement(orn_el, "inverted-mordent")
            if spec.get("fermata"):
                SubElement(notations, "fermata", type=spec["fermata"])
            if spec.get("arpeggiate"):
                SubElement(notations, "arpeggiate")

    NOTE_ORDER = ["grace", "chord", "pitch", "unpitched", "rest", "cue", "duration", "tie",
                  "instrument", "footnote", "level", "voice", "type", "dot", "accidental",
                  "time-modification", "stem", "notehead", "notehead-text", "staff", "beam",
                  "notations", "lyric", "play", "listen"]

    def _process_entry(self, measure, entry, staff_id, voice, key, transp_key_adjust,
                       transp_interval, tuplet_attrs, staff_cmper):
        doc = self.doc
        entnum = entry.get("entnum")
        dura = int(doc.get(entry, "dura", "0"))
        is_note = doc.has(entry, "isNote")
        grace = doc.has(entry, "graceNote")
        ctx = self.staff_ctx.get(staff_cmper)
        perc_codes = self._lookup_perc_codes(entnum) if (ctx and ctx.is_percussion) else {}

        note_alter_map = {}
        if doc.has(entry, "noteDetail"):
            for na in doc.details_ent.get("noteAlter", {}).get(entnum, []):
                nid = doc.get(na, "noteID")
                if nid is not None:
                    note_alter_map[nid] = {"enharmonic": doc.has(na, "enharmonic")}

        artic_specs = []
        if doc.has(entry, "articDetail"):
            for aa in doc.details_ent.get("articAssign", {}).get(entnum, []):
                ad_cmper = doc.get(aa, "articDef")
                ad = doc.other("articDef", ad_cmper) if ad_cmper else None
                if ad is None:
                    continue
                ch = doc.get(ad, "charMain")
                spec = ARTIC_SPECS.get(int(ch)) if ch else None
                if spec is None and ch is not None:
                    tag, typ = H.translate_articualtion(ch)
                    spec = {"artic": [(tag, typ)]}
                if spec:
                    artic_specs.append(spec)

        # piano pedal markings are attached as articulations in Finale but are
        # directions in MusicXML; emit them at the note's position.
        for spec in artic_specs:
            pedal = spec.get("pedal")
            if pedal:
                d = SubElement(measure, "direction", placement="below")
                dt = SubElement(d, "direction-type")
                SubElement(dt, "pedal", type=pedal, line="no", sign="yes")
                if staff_id:
                    SubElement(d, "staff").text = str(staff_id)

        if not is_note:
            note = SubElement(measure, "note")
            SubElement(note, "rest")
            SubElement(note, "duration").text = str((dura * DIVISIONS) // 1024)
            SubElement(note, "voice").text = str(voice)
            type_name, nb_dots = H.calculate_type_and_dots(dura)
            if type_name:
                SubElement(note, "type").text = type_name
                for _ in range(nb_dots):
                    SubElement(note, "dot")
                if staff_id:
                    SubElement(note, "staff").text = str(staff_id)
            notations = SubElement(note, "notations")
            self._entry_smart_shapes(entnum, notations, note)
            if doc.has(entry, "tupletStart"):
                self._handle_tuplet_start(entry, notations, tuplet_attrs)
            self._close_tuplets(note, notations, dura, tuplet_attrs)
            if len(notations) == 0:
                note.remove(notations)
            H.reorder_children(note, self.NOTE_ORDER)
            return tuplet_attrs

        notes = entry.findall("note")
        for idx, note_ in enumerate(notes):
            note = SubElement(measure, "note")
            if idx == 0 and doc.has(entry, "lyricDetail"):
                for lv in doc.details_ent.get("lyrDataVerse", {}).get(entnum, []):
                    number = doc.get(lv, "lyricNumber")
                    syll = doc.get(lv, "syll")
                    verse = doc.text("verse", number) if number else None
                    if verse and syll:
                        text, syllabic, extend = H.find_nth_syllabic(verse, int(syll))
                        lyric = SubElement(note, "lyric", name="verse", number=number)
                        SubElement(lyric, "syllabic").text = syllabic
                        SubElement(lyric, "text").text = text
                        if extend:
                            SubElement(lyric, "extend")
            if idx > 0:
                SubElement(note, "chord")
            if grace:
                SubElement(note, "grace", slash="no")

            note_id = note_.get("id")
            drum_note = None
            if ctx and ctx.is_percussion:
                code = perc_codes.get(note_id)
                if code is not None and code in ctx.drum_map:
                    drum_note = ctx.drum_map[code]
                elif code is not None:
                    # unmapped code: synthesize from table defaults
                    from .percussion import PERC_NOTE_TYPES
                    base = PERC_NOTE_TYPES.get(code & 0xFFF)
                    drum_note = DrumMapNote(
                        perc_note_type=code,
                        staff_position=base["staffPos"] if base else 6,
                        closed_notehead=None, half_notehead=None, whole_notehead=None)
                if drum_note is not None:
                    ctx.used_drum_notes.setdefault(drum_note.perc_note_type, drum_note)

            if drum_note is not None:
                unp = SubElement(note, "unpitched")
                step, octave = drum_note.display_step_octave()
                SubElement(unp, "display-step").text = step
                SubElement(unp, "display-octave").text = str(octave)
            elif ctx and ctx.is_percussion:
                # percussion staff but unmapped note: place by harmLev directly
                unp = SubElement(note, "unpitched")
                lev = int(doc.get(note_, "harmLev", "6"))
                steps = ("C", "D", "E", "F", "G", "A", "B")
                SubElement(unp, "display-step").text = steps[lev % 7]
                SubElement(unp, "display-octave").text = str(4 + lev // 7)
            else:
                pitch = SubElement(note, "pitch")
                harm_lev = int(doc.get(note_, "harmLev", "0"))
                harm_alt = int(doc.get(note_, "harmAlt", "0"))
                enharmonic = note_alter_map.get(note_id, {}).get("enharmonic", False)
                step_v, alter_v, octave_v = H.calculate_step_alter_and_octave(
                    harm_lev, harm_alt, key, transp_key_adjust, transp_interval, enharmonic)
                SubElement(pitch, "step").text = step_v
                if alter_v != 0:
                    SubElement(pitch, "alter").text = str(alter_v)
                SubElement(pitch, "octave").text = str(octave_v)

            if not grace:
                SubElement(note, "duration").text = str((dura * DIVISIONS) // 1024)
            if drum_note is not None:
                SubElement(note, "instrument", id=f"{ctx.part_id}-I{drum_note.perc_note_type}")

            if doc.has(note_, "tieStart"):
                SubElement(note, "tie", type="start")
            if doc.has(note_, "tieEnd"):
                SubElement(note, "tie", type="stop")

            SubElement(note, "voice").text = str(voice)
            type_name, nb_dots = H.calculate_type_and_dots(dura)
            if type_name:
                SubElement(note, "type").text = type_name
                for _ in range(nb_dots):
                    SubElement(note, "dot")

            if drum_note is not None and drum_note.notehead != "normal":
                SubElement(note, "notehead").text = drum_note.notehead

            if staff_id:
                SubElement(note, "staff").text = str(staff_id)

            if idx == 0:
                notations = SubElement(note, "notations")
                self._entry_smart_shapes(entnum, notations, note)
                if doc.has(entry, "tupletStart"):
                    self._handle_tuplet_start(entry, notations, tuplet_attrs)
                self._close_tuplets(note, notations, dura, tuplet_attrs)
                self._emit_articulations(note, notations, artic_specs)
                if len(notations) == 0:
                    note.remove(notations)

            H.reorder_children(note, self.NOTE_ORDER)
        return tuplet_attrs

    # --------------------------------------------------------------- metadata

    _INSERT_TYPES = {
        "title": "title", "subtitle": "subtitle", "composer": "composer",
        "lyricist": "lyricist", "arranger": "arranger", "copyright": "copyright",
    }
    _CREDIT_TYPE = {
        "title": "title", "subtitle": "subtitle", "composer": "composer",
        "lyricist": "lyricist", "copyright": "rights",
    }

    def file_info(self, kind: str) -> str | None:
        v = self.doc.text("fileInfo", kind)
        if v is None and self.meta_root is not None:
            ns = {"m": "http://www.makemusic.com/2012/NotationMetadata"}
            el = self.meta_root.find(f"m:fileInfo/m:{kind}", ns)
            v = el.text if el is not None else None
        return v

    def _substitute_inserts(self, raw: str) -> tuple[str, str | None]:
        """Replace ^title() style inserts; returns (text, credit_kind)."""
        kind = None

        def repl(m):
            nonlocal kind
            name = m.group(1)
            if name in self._INSERT_TYPES:
                if kind is None:
                    kind = name
                value = self.file_info(self._INSERT_TYPES[name])
                return (value or "").replace("^", "^^")
            if name == "partname":
                kind = kind or "partname"
                return "Score"
            return ""

        out = re.sub(r"\^(title|subtitle|composer|lyricist|arranger|copyright|partname|page|filename|date|time|perftime)\((.*?)\)",
                     repl, raw)
        return out, kind

    def _emit_defaults(self, score):
        from .style import page_metrics, MM_PER_INCH
        pm = page_metrics(self.doc)
        self._page_metrics = pm
        sp = pm["spatium_mm"]
        self._tenths_per_mm = 10.0 / sp
        d = SubElement(score, "defaults")
        scaling = SubElement(d, "scaling")
        SubElement(scaling, "millimeters").text = f"{sp * 4:.4f}"
        SubElement(scaling, "tenths").text = "40"
        pl = SubElement(d, "page-layout")
        SubElement(pl, "page-height").text = f"{pm['h_in'] * MM_PER_INCH * self._tenths_per_mm:.1f}"
        SubElement(pl, "page-width").text = f"{pm['w_in'] * MM_PER_INCH * self._tenths_per_mm:.1f}"
        margins = SubElement(pl, "page-margins", type="both")
        SubElement(margins, "left-margin").text = f"{pm['m_left'] * MM_PER_INCH * self._tenths_per_mm:.1f}"
        SubElement(margins, "right-margin").text = f"{pm['m_right'] * MM_PER_INCH * self._tenths_per_mm:.1f}"
        SubElement(margins, "top-margin").text = f"{pm['m_top'] * MM_PER_INCH * self._tenths_per_mm:.1f}"
        SubElement(margins, "bottom-margin").text = f"{pm['m_bottom'] * MM_PER_INCH * self._tenths_per_mm:.1f}"

    def _emit_page_texts(self, score) -> bool:
        """Convert Finale page-attached text blocks on page 1 into credits."""
        from .style import EVPU_TO_MM, MM_PER_INCH
        doc = self.doc
        pm = self._page_metrics
        t = self._tenths_per_mm
        page_w_mm = pm["w_in"] * MM_PER_INCH
        page_h_mm = pm["h_in"] * MM_PER_INCH
        pct = pm["page_percent"] / 100.0
        emitted = False
        for pta in doc.other_all("pageTextAssign"):
            start = int(doc.get(pta, "startPage", "0") or 0)
            if start != 1:
                continue
            block_id = doc.get(pta, "block")
            tb = doc.other("textBlock", block_id) if block_id else None
            raw = doc.text("blockText", doc.get(tb, "textID")) if tb is not None else None
            if not raw:
                continue
            if "^page(" in raw:
                continue  # page numbers handled via footer style
            text_raw, kind = self._substitute_inserts(raw)
            runs = parse_enigma_text(text_raw, resolver=self._font_by_id)
            text = "".join(r.text for r in runs).strip()
            if not text:
                continue
            hpos = doc.get(pta, "hposLp", "left")
            vpos = doc.get(pta, "vpos", "top")
            ydisp_mm = int(doc.get(pta, "ydisp", "0") or 0) * EVPU_TO_MM * pct
            xdisp_mm = int(doc.get(pta, "xdisp", "0") or 0) * EVPU_TO_MM * pct
            m_top_mm = pm["m_top"] * MM_PER_INCH
            m_bottom_mm = pm["m_bottom"] * MM_PER_INCH
            m_left_mm = pm["m_left"] * MM_PER_INCH
            m_right_mm = pm["m_right"] * MM_PER_INCH
            if vpos == "bottom":
                y_mm = m_bottom_mm + ydisp_mm
            else:
                y_mm = page_h_mm - m_top_mm + ydisp_mm
            if hpos == "center":
                x_mm = page_w_mm / 2 + xdisp_mm
            elif hpos == "right":
                x_mm = page_w_mm - m_right_mm + xdisp_mm
            else:
                x_mm = m_left_mm + xdisp_mm

            c = SubElement(score, "credit", page="1")
            ctype = self._CREDIT_TYPE.get(kind or "")
            if ctype:
                SubElement(c, "credit-type").text = ctype
            cw = SubElement(c, "credit-words")
            cw.set("default-x", f"{x_mm * t:.1f}")
            cw.set("default-y", f"{y_mm * t:.1f}")
            cw.set("justify", {"center": "center", "right": "right"}.get(hpos, "left"))
            cw.set("valign", "bottom" if vpos == "bottom" else "top")
            font = runs[0].font if runs else None
            if font and font.size:
                cw.set("font-size", f"{font.size * pct:g}")
            if font and font.family:
                cw.set("font-family", font.family)
            if font and font.bold:
                cw.set("font-weight", "bold")
            if font and font.italic:
                cw.set("font-style", "italic")
            cw.text = text
            emitted = True
        return emitted

    def _font_by_id(self, font_id: str):
        from .fonts import resolve_font_name
        return resolve_font_name(self.doc, font_id)

    def _handle_metadata(self, score):
        identification = SubElement(score, "identification")
        title = self.file_info("title")
        composer = self.file_info("composer")
        rights = self.file_info("copyright")
        if composer:
            creator = SubElement(identification, "creator", type="composer")
            creator.text = composer
            identification.insert(0, creator)
        if rights:
            SubElement(identification, "rights").text = rights
        encoding = SubElement(identification, "encoding")
        SubElement(encoding, "software").text = "musx2mscz " + __version__
        SubElement(encoding, "encoding-date").text = date.today().strftime("%Y-%m-%d")
        if title:
            work = Element("work")
            SubElement(work, "work-title").text = title
            score.insert(0, work)

        self._emit_defaults(score)
        if not self._emit_page_texts(score):
            # fall back to plain credits from file info
            def credit(ctype, text, justify, valign, size):
                c = SubElement(score, "credit", page="1")
                SubElement(c, "credit-type").text = ctype
                cw = SubElement(c, "credit-words")
                cw.set("justify", justify)
                cw.set("valign", valign)
                cw.set("font-size", str(size))
                cw.text = text

            if title:
                credit("title", title, "center", "top", 22)
            subtitle = self.file_info("subtitle")
            if subtitle:
                credit("subtitle", subtitle, "center", "top", 14)
            if composer:
                credit("composer", composer, "right", "bottom", 10)


def convert_enigma_to_musicxml(enigmaxml: bytes, metadata: bytes | None) -> tuple[bytes, list[str]]:
    conv = Converter(enigmaxml, metadata)
    tree = conv.convert()
    out = BytesIO()
    doctype = ('<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
               '"http://www.musicxml.org/dtds/partwise.dtd">')
    tree.write(out, pretty_print=True, encoding="UTF-8", xml_declaration=True, doctype=doctype)
    return out.getvalue(), conv.warnings
