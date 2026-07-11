"""musx2mscz command line interface."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import __version__
from .converter import convert_enigma_to_musicxml
from .enigma import EnigmaDoc
from .musxfile import MusxFile
from .style import build_mss
from . import mscore
from . import msczpost


def convert_one(input_path: Path, output_path: Path, keep: bool = False,
                verbose: bool = False, no_sound_binding: bool = False) -> None:
    print(f"Converting {input_path.name} ...")
    musx = MusxFile.load(input_path)

    musicxml, warnings, layout_hints = convert_enigma_to_musicxml(
        musx.enigmaxml, musx.metadata)
    if verbose:
        for w in warnings:
            print(f"  [warn] {w}")

    doc = EnigmaDoc(musx.enigmaxml)
    mss = build_mss(doc)

    with tempfile.TemporaryDirectory(prefix="musx2mscz-") as td:
        tdir = Path(td)
        xml_path = tdir / (input_path.stem + ".musicxml")
        mss_path = tdir / (input_path.stem + ".mss")
        xml_path.write_bytes(musicxml)
        mss_path.write_bytes(mss)
        if keep:
            keep_dir = output_path.parent
            (keep_dir / xml_path.name).write_bytes(musicxml)
            (keep_dir / mss_path.name).write_bytes(mss)
            (keep_dir / (input_path.stem + ".enigmaxml")).write_bytes(musx.enigmaxml)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        mscore.convert(xml_path, output_path, style_path=mss_path)

    msczpost.postprocess(output_path, musx, doc, bind_sounds=not no_sound_binding,
                         layout_hints=layout_hints)
    print(f"  -> {output_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="musx2mscz",
        description="Convert Finale Notation Files (.musx) to MuseScore files (.mscz).")
    parser.add_argument("input", help="a .musx file or a directory containing .musx files")
    parser.add_argument("-o", "--output",
                        help="output .mscz path (or directory when input is a directory)")
    parser.add_argument("--keep", action="store_true",
                        help="keep intermediate .enigmaxml/.musicxml/.mss files next to the output")
    parser.add_argument("--no-sound-binding", action="store_true",
                        help="do not bind detected VST sound libraries (e.g. Garritan ARIA) in the output")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    failures = 0
    if input_path.is_dir():
        out_dir = Path(args.output) if args.output else input_path
        files = sorted(input_path.glob("*.musx"))
        if not files:
            print(f"No .musx files found in {input_path}", file=sys.stderr)
            return 1
        for f in files:
            try:
                convert_one(f, out_dir / (f.stem + ".mscz"), keep=args.keep,
                            verbose=args.verbose, no_sound_binding=args.no_sound_binding)
            except Exception as e:
                failures += 1
                print(f"  ERROR converting {f.name}: {e}", file=sys.stderr)
    elif input_path.is_file():
        out = Path(args.output) if args.output else input_path.with_suffix(".mscz")
        try:
            convert_one(input_path, out, keep=args.keep, verbose=args.verbose,
                        no_sound_binding=args.no_sound_binding)
        except Exception as e:
            failures += 1
            print(f"  ERROR converting {input_path.name}: {e}", file=sys.stderr)
    else:
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
