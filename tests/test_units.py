from pathlib import Path

import pytest

from musx2mscz.fonts import FontInfo, parse_enigma_text, plain_text, is_music_font
from musx2mscz.percussion import note_type_name, general_midi_note, notehead_type
from musx2mscz import helpers as H

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "musx"


def test_parse_enigma_text_runs():
    raw = ("^fontTxt(Times New Roman,4096)^size(16)^nfx(1)Meno mosso "
           "^fontNum(Rentaro,8194)^size(24)^nfx(0)(&=108)")
    runs = parse_enigma_text(raw)
    assert runs[0].text.startswith("Meno mosso")
    assert runs[0].font.bold and runs[0].font.family == "Times New Roman"
    assert runs[0].font.size == 16
    assert runs[1].font.family == "Rentaro" and runs[1].font.music
    assert runs[1].text == "(&=108)"


def test_plain_text_symbols():
    assert plain_text("^fontTxt(X,0)a ^flat()b") == "a ♭b"
    assert plain_text("caret ^^ escape") == "caret ^ escape"


def test_music_font_detection():
    assert is_music_font("Kousaku")
    assert is_music_font("rentaro")
    assert not is_music_font("Times New Roman")


def test_percussion_table():
    assert note_type_name(2) == "Kick Drum"
    assert general_midi_note(2) == 36
    assert note_type_name(13) == "Bass Drum"
    # order id in the top four bits produces a numbered variant
    assert "(" in note_type_name((1 << 12) | 77)
    assert notehead_type(120) == "x"
    assert notehead_type(207) == "normal"


def test_key_fifths():
    assert H.calculate_mode_and_key_fifths(None, 0) == ("major", 0)
    assert H.calculate_mode_and_key_fifths(256, 0) == ("minor", 0)
    assert H.calculate_mode_and_key_fifths(253, 0)[1] == -3  # C minor relative


def test_type_and_dots():
    assert H.calculate_type_and_dots(1024) == ("quarter", 0)
    assert H.calculate_type_and_dots(1024 + 512) == ("quarter", 1)


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples not present")
def test_musx_decrypt():
    from musx2mscz.musxfile import MusxFile
    files = sorted(SAMPLES.glob("*.musx"))
    assert files, "no sample musx files"
    musx = MusxFile.load(files[0])
    head = musx.enigmaxml[:200].decode("utf-8", errors="replace")
    assert "<finale" in head
    assert musx.presets  # ARIA presets present in these samples


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples not present")
def test_convert_to_musicxml():
    from musx2mscz.musxfile import MusxFile
    from musx2mscz.converter import convert_enigma_to_musicxml
    musx = MusxFile.load(sorted(SAMPLES.glob("*.musx"))[0])
    xml, warnings = convert_enigma_to_musicxml(musx.enigmaxml, musx.metadata)
    text = xml.decode("utf-8")
    assert "<score-partwise" in text
    assert "<unpitched>" in text  # percussion present in samples
    assert "metronome" in text
