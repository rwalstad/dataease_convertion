#!/usr/bin/env python3
"""Reverse a practical field layout from DataEase DOS 4.x DBA/DBM files.

This is intentionally conservative: it extracts the field descriptor table that
matches KUNDOAAB.DBA and validates offsets against the fixed-width DBM records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


BASE = Path(__file__).resolve().parent
DBA = BASE / "KUNDOAAB.DBA"
DBM = BASE / "KUNDOAAB.DBM"
OUT = BASE / "KUNDOAAB_REVERSED.TDF"

DESCRIPTOR_START = 0x28
DESCRIPTOR_SIZE = 16
FIELD_COUNT = 253
NAME_LIST_START = 4861
RECORD_SIZE = 1000
DOS_ENCODING = "cp865"


TYPE_NAMES = {
    0x01: "Text",
    0x02: "Number",
    0x03: "Number/Decimal",
    0x04: "Date",
    0x08: "Choice/Lookup",
}


@dataclass(frozen=True)
class Field:
    number: int
    name: str
    type_code: int
    type_name: str
    display_length: int
    storage_length: int
    decimals: int
    offset: int
    stored: bool
    raw_descriptor: bytes


def clean_bytes(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0].rstrip(b"\x00 ")
    if not raw:
        return ""
    text = raw.decode(DOS_ENCODING, errors="replace")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def read_names(data: bytes) -> list[str]:
    names: list[str] = []
    pos = NAME_LIST_START
    while pos < len(data) and len(names) < FIELD_COUNT:
        end = data.find(b"\x00", pos)
        if end < 0:
            break
        raw = data[pos:end]
        names.append(raw.decode(DOS_ENCODING, errors="replace"))
        pos = end + 1
    return names


def read_fields(data: bytes) -> list[Field]:
    names = read_names(data)
    if len(names) != FIELD_COUNT:
        raise ValueError(f"Expected {FIELD_COUNT} names, got {len(names)}")

    fields: list[Field] = []
    for index, name in enumerate(names):
        pos = DESCRIPTOR_START + index * DESCRIPTOR_SIZE
        desc = data[pos : pos + DESCRIPTOR_SIZE]
        type_code = desc[2]
        display_length = desc[3]
        storage_length = desc[7]
        decimals = desc[4]
        offset = desc[9] + (desc[10] << 8)
        stored = offset < RECORD_SIZE and offset + storage_length <= RECORD_SIZE
        fields.append(
            Field(
                number=index + 1,
                name=name,
                type_code=type_code,
                type_name=TYPE_NAMES.get(type_code, f"Unknown(0x{type_code:02X})"),
                display_length=display_length,
                storage_length=storage_length,
                decimals=decimals,
                offset=offset,
                stored=stored,
                raw_descriptor=desc,
            )
        )
    return fields


def sample_values(field: Field, records: list[bytes], limit: int = 5) -> list[str]:
    if not field.stored:
        return []
    seen: list[str] = []
    for record in records:
        raw = record[field.offset : field.offset + field.storage_length]
        value = clean_bytes(raw)
        if field.type_code == 0x08 and raw:
            value = f"code:{raw[0]}"
        elif value and any(ord(ch) < 32 for ch in value):
            value = "hex:" + raw.hex()
        elif not value and any(raw):
            value = "hex:" + raw.hex()
        if not value:
            continue
        if value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def render_tdf(fields: list[Field], dbm: bytes) -> str:
    records = [
        dbm[pos : pos + RECORD_SIZE]
        for pos in range(0, len(dbm), RECORD_SIZE)
        if len(dbm[pos : pos + RECORD_SIZE]) == RECORD_SIZE
    ]
    stored = [field for field in fields if field.stored]

    lines = [
        "TABLE: KUNDOAAB",
        "SOURCE: Reversed from KUNDOAAB.DBA + KUNDOAAB.DBM",
        "DATAEASE_VERSION: DOS 4.2.3 (inferred from source system)",
        f"FIELDS_IN_DBA: {len(fields)}",
        f"STORED_FIELDS_IN_DBM: {len(stored)}",
        f"RECORD_SIZE: {RECORD_SIZE} bytes",
        f"TOTAL_RECORDS: {len(records)}",
        "",
        "NOTES:",
        "  - This file is reconstructed, not an original DataEase .TDF.",
        "  - Offset/length/type come from 16-byte field descriptors in KUNDOAAB.DBA.",
        "  - STORED=yes means the field points inside the 1000-byte DBM record.",
        "  - STORED=no usually means calculated/lookup/screen-only metadata.",
        "  - Text is decoded as DOS code page 865, matching Norwegian æ/ø/å bytes.",
        "  - Type names are inferred from observed DataEase DOS descriptor codes.",
        "",
        "#    NAME                      TYPE             DISP STORE DEC OFFSET STORED SAMPLES",
        "-" * 120,
    ]

    for field in fields:
        samples = "; ".join(sample_values(field, records))
        stored_flag = "yes" if field.stored else "no"
        lines.append(
            f"{field.number:<4} {field.name[:25]:<25} "
            f"{field.type_name:<16} {field.display_length:>4} {field.storage_length:>5} "
            f"{field.decimals:>3} "
            f"{field.offset:>6} {stored_flag:<6} {samples}"
        )

    lines.extend(
        [
            "",
            "RAW_DESCRIPTOR_FORMAT_OBSERVED:",
            "  byte 2     = type code",
            "  byte 3     = display length",
            "  byte 4     = decimals",
            "  byte 7     = storage length in DBM record",
            "  bytes 9-10 = little-endian offset into the 1000-byte DBM record",
            "  16 bytes per descriptor, starting at DBA offset 0x28",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    dba = DBA.read_bytes()
    dbm = DBM.read_bytes()
    fields = read_fields(dba)
    OUT.write_text(render_tdf(fields, dbm), encoding="utf-8")
    print(f"Wrote {OUT.name}")


if __name__ == "__main__":
    main()
