"""Reading of .musx containers: EnigmaXML extraction and decryption.

The decryption algorithm is ported from musx2mxl (MIT License,
Copyright (c) Joris Van Eyghen. https://github.com/joris-vaneyghen/musx2mxl).
"""

from __future__ import annotations

import gzip
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Constants for the MUSX PRNG-based stream cipher
_CIPHER_INITIAL_STATE = 0x28006D45
_CIPHER_MULTIPLIER = 0x41C64E6D
_CIPHER_INCREMENT = 0x3039
_CIPHER_RESET_INTERVAL = 0x20000


def _keystream(length: int) -> bytes:
    out = bytearray(length)
    state = _CIPHER_INITIAL_STATE
    for i in range(length):
        if i % _CIPHER_RESET_INTERVAL == 0:
            state = _CIPHER_INITIAL_STATE
        state = (state * _CIPHER_MULTIPLIER + _CIPHER_INCREMENT) & 0xFFFFFFFF
        upper = state >> 16
        out[i] = (upper + upper // 255) & 0xFF
    return bytes(out)


def decrypt(data: bytes) -> bytes:
    """Decrypt (or encrypt) a score.dat buffer with Finale's stream cipher."""
    ks = _keystream(len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


@dataclass
class MusxFile:
    """Contents of a .musx container relevant for conversion."""

    path: Path
    enigmaxml: bytes
    metadata: bytes | None = None
    presets: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "MusxFile":
        path = Path(path)
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            if "score.dat" not in names:
                raise ValueError(f"{path}: not a Finale .musx file (missing score.dat)")
            raw = zf.read("score.dat")
            enigmaxml = gzip.decompress(decrypt(raw))
            metadata = zf.read("NotationMetadata.xml") if "NotationMetadata.xml" in names else None
            presets = {
                n.split("/", 1)[1]: zf.read(n)
                for n in names
                if n.startswith("presets/") and n.endswith(".preset")
            }
        return cls(path=path, enigmaxml=enigmaxml, metadata=metadata, presets=presets)
