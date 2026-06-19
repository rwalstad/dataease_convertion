from __future__ import annotations

import cgi
import base64
import html as html_lib
import json
import math
import os
import re
import struct
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from generate_dataease_dbm import generate as generate_dbm
from generate_dataease_dbm import parse_csv


BASE_DIR = Path(__file__).resolve().parent
HOWITWORK_FILE = BASE_DIR / "howitwork.md"
KUNDOAAB_DBM_FILE = BASE_DIR / "KUNDOAAB.DBM"
KUNDOAAB_DBA_FILE = BASE_DIR / "KUNDOAAB.DBA"
IS_VERCEL = os.environ.get("VERCEL") == "1" or BASE_DIR == Path("/var/task")
DEFAULT_OUTPUT_DIR = Path("/tmp/dataease_convertion") if IS_VERCEL else BASE_DIR
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
MAX_DISPLAY_FIELDS = 10


@dataclass(frozen=True)
class Field:
    name: str
    type_code: int
    length: int
    decimals: int
    flags: int


@dataclass(frozen=True)
class ReversedLayout:
    record_size: int
    fields: list["ReversedField"]


@dataclass(frozen=True)
class ReversedField:
    name: str
    type_name: str
    storage_length: int
    decimals: int
    offset: int


def _decode_fixed_text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(ascii_text.casefold().split())


def _decode_field(field: Field, raw: bytes):
    if field.type_code == 0x01:
        return _decode_fixed_text(raw)
    if field.type_code == 0x02:
        return struct.unpack("<i", raw[:4])[0]
    if field.type_code == 0x03:
        return _decode_fixed_text(raw)
    if field.type_code in {0x04, 0x05}:
        return _decode_fixed_text(raw)
    if field.type_code == 0x06:
        return struct.unpack("<d", raw[:8])[0]
    if field.type_code == 0x07:
        return struct.unpack("<q", raw[:8])[0] / 100
    if field.type_code == 0x08:
        return bool(raw and raw[0])
    return raw.hex()


def _decode_dos_text(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0].rstrip(b"\x00 ")
    return " ".join(raw.decode("cp865", errors="replace").split())


def _raw_dos_text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("cp865", errors="replace").strip()


def _format_decimal(value: float) -> str:
    if not math.isfinite(value):
        return ""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _decode_reversed_number(field: ReversedField, raw: bytes):
    text = _raw_dos_text(raw)
    compact = text.replace(" ", "")
    if compact and all(ch.isalnum() or ch in {".", ",", "-", "+"} for ch in compact):
        return text

    if field.storage_length == 4:
        if field.decimals:
            value = struct.unpack("<f", raw[:4])[0]
            return "" if not math.isfinite(value) or abs(value) > 1e20 else _format_decimal(value)
        return str(struct.unpack("<i", raw[:4])[0])

    if field.storage_length == 2:
        return str(struct.unpack("<h", raw[:2])[0])

    if field.storage_length == 8:
        value = struct.unpack("<q", raw[:8])[0]
        if field.decimals:
            value = value / (10 ** field.decimals)
        return _format_decimal(float(value))

    return _decode_dos_text(raw)


def _decode_reversed_field(field: ReversedField, raw: bytes):
    if field.type_name == "Choice/Lookup":
        return raw[0] if raw else ""

    if field.type_name in {"Number", "Number/Decimal"}:
        return _decode_reversed_number(field, raw)

    value = _decode_dos_text(raw)
    if value:
        return value

    if any(raw):
        return raw.hex()
    return ""


TYPE_NAMES = {
    0x01: "Text",
    0x02: "Number",
    0x03: "Number/Decimal",
    0x04: "Date",
    0x08: "Choice/Lookup",
}
DBA_DESCRIPTOR_START = 0x28
DBA_DESCRIPTOR_SIZE = 16
DOS_FIXED_FIELD_COUNT = 253
DOS_FIXED_NAME_LIST_START = 4861
DOS_FIXED_RECORD_SIZE = 1000


def _parse_reversed_tdf(path: Path) -> ReversedLayout:
    record_size = 0
    fields: list[ReversedField] = []
    field_pattern = re.compile(
        r"^\s*\d+\s+(?P<name>.{25})\s+"
        r"(?P<type>.{16})\s+"
        r"\d+\s+(?P<storage>\d+)\s+(?P<decimals>\d+)\s+"
        r"(?P<offset>\d+)\s+(?P<stored>yes|no)\b"
    )

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("RECORD_SIZE:"):
            match = re.search(r"(\d+)", line)
            if match:
                record_size = int(match.group(1))
            continue

        match = field_pattern.match(line)
        if not match or match.group("stored") != "yes":
            continue

        fields.append(
            ReversedField(
                name=match.group("name").strip(),
                type_name=match.group("type").strip(),
                storage_length=int(match.group("storage")),
                decimals=int(match.group("decimals")),
                offset=int(match.group("offset")),
            )
        )

    if not record_size:
        raise ValueError("TDF-filen mangler RECORD_SIZE.")
    if not fields:
        raise ValueError("Fant ingen lagrede felt i TDF-filen.")

    return ReversedLayout(record_size=record_size, fields=fields)


def _is_plausible_dba_name(value: str) -> bool:
    if not value or len(value) > 40:
        return False
    return all((ord(ch) >= 32 and ch != "\x7f") for ch in value)


def _read_dba_names(data: bytes, field_count: int, search_start: int) -> list[str]:
    best: list[str] = []
    for start in range(search_start, min(len(data), search_start + 4096)):
        names: list[str] = []
        position = start
        while position < len(data) and len(names) < field_count:
            end = data.find(b"\x00", position)
            if end < 0:
                break

            name = data[position:end].decode("cp865", errors="replace")
            if not _is_plausible_dba_name(name):
                break

            names.append(name)
            position = end + 1

        if len(names) > len(best):
            best = names
        if len(names) == field_count:
            return names

    if not best:
        raise ValueError("Fant ikke feltnavn i DBA-filen.")
    return best


def _read_dba_names_at(data: bytes, field_count: int, start: int) -> list[str]:
    names: list[str] = []
    position = start
    while position < len(data) and len(names) < field_count:
        end = data.find(b"\x00", position)
        if end < 0:
            break
        name = data[position:end].decode("cp865", errors="replace")
        if not _is_plausible_dba_name(name):
            break
        names.append(name)
        position = end + 1
    return names


def _parse_fixed_dos_dba_layout(data: bytes, dbm_size: int) -> ReversedLayout | None:
    if dbm_size % DOS_FIXED_RECORD_SIZE != 0:
        return None

    names = _read_dba_names_at(data, DOS_FIXED_FIELD_COUNT, DOS_FIXED_NAME_LIST_START)
    if len(names) != DOS_FIXED_FIELD_COUNT:
        return None

    fields: list[ReversedField] = []
    for index, name in enumerate(names):
        position = DBA_DESCRIPTOR_START + index * DBA_DESCRIPTOR_SIZE
        descriptor = data[position : position + DBA_DESCRIPTOR_SIZE]
        if len(descriptor) != DBA_DESCRIPTOR_SIZE:
            return None

        storage_length = descriptor[7]
        offset = descriptor[9] + (descriptor[10] << 8)
        if offset < DOS_FIXED_RECORD_SIZE and offset + storage_length <= DOS_FIXED_RECORD_SIZE:
            fields.append(
                ReversedField(
                    name=name,
                    type_name=TYPE_NAMES.get(descriptor[2], f"Unknown(0x{descriptor[2]:02X})"),
                    storage_length=storage_length,
                    decimals=descriptor[4],
                    offset=offset,
                )
            )

    if not fields:
        return None
    return ReversedLayout(record_size=DOS_FIXED_RECORD_SIZE, fields=fields)


def _parse_dba_layout(path: Path, dbm_size: int) -> ReversedLayout:
    data = path.read_bytes()
    fixed_layout = _parse_fixed_dos_dba_layout(data, dbm_size)
    if fixed_layout is not None:
        return fixed_layout

    descriptors: list[bytes] = []

    for index in range(1024):
        position = DBA_DESCRIPTOR_START + index * DBA_DESCRIPTOR_SIZE
        descriptor = data[position : position + DBA_DESCRIPTOR_SIZE]
        if len(descriptor) != DBA_DESCRIPTOR_SIZE:
            break

        type_code = descriptor[2]
        display_length = descriptor[3]
        storage_length = descriptor[7]
        offset = descriptor[9] + (descriptor[10] << 8)
        if (
            type_code not in TYPE_NAMES
            or display_length == 0
            or display_length > 100
            or storage_length > 100
            or offset > 10000
        ):
            break

        descriptors.append(descriptor)

    if not descriptors:
        raise ValueError("Fant ingen feltbeskrivelser i DBA-filen.")

    names_start = DBA_DESCRIPTOR_START + len(descriptors) * DBA_DESCRIPTOR_SIZE
    names = _read_dba_names(data, len(descriptors), names_start)
    if len(names) < len(descriptors):
        descriptors = descriptors[: len(names)]

    raw_fields: list[ReversedField] = []
    max_stored_end = 0
    for name, descriptor in zip(names, descriptors):
        storage_length = descriptor[7]
        offset = descriptor[9] + (descriptor[10] << 8)
        raw_fields.append(
            ReversedField(
                name=name,
                type_name=TYPE_NAMES.get(descriptor[2], f"Unknown(0x{descriptor[2]:02X})"),
                storage_length=storage_length,
                decimals=descriptor[4],
                offset=offset,
            )
        )
        if storage_length:
            max_stored_end = max(max_stored_end, offset + storage_length)

    if not max_stored_end:
        raise ValueError("DBA-filen beskriver ingen lagrede DBM-felt.")

    record_size = max_stored_end
    for candidate in range(max_stored_end, max_stored_end + 4096):
        if dbm_size % candidate == 0:
            record_size = candidate
            break

    fields = [
        field
        for field in raw_fields
        if field.storage_length and field.offset + field.storage_length <= record_size
    ]
    if not fields:
        raise ValueError("Fant ingen felt i DBA-filen som peker inn i DBM-recorden.")

    return ReversedLayout(record_size=record_size, fields=fields)


def _read_layout_records(dbm_path: Path, schema_path: Path) -> list[dict]:
    if schema_path.suffix.casefold() == ".dba":
        layout = _parse_dba_layout(schema_path, dbm_path.stat().st_size)
    else:
        layout = _parse_reversed_tdf(schema_path)
    data = dbm_path.read_bytes()
    records: list[dict] = []

    for position in range(0, len(data), layout.record_size):
        record = data[position : position + layout.record_size]
        if len(record) != layout.record_size:
            continue

        row = {}
        for field in layout.fields:
            raw = record[field.offset : field.offset + field.storage_length]
            row[field.name] = _decode_reversed_field(field, raw)
        records.append(row)

    return records


def _read_fixed_dos_records(dbm_path: Path, dba_path: Path) -> list[dict]:
    if not dbm_path.exists():
        raise ValueError(f"KUNDOAAB.DBM finnes ikke i app-mappen: {dbm_path}")
    if not dba_path.exists():
        raise ValueError(f"KUNDOAAB.DBA finnes ikke i app-mappen: {dba_path}")

    layout = _parse_fixed_dos_dba_layout(dba_path.read_bytes(), dbm_path.stat().st_size)
    if layout is None:
        raise ValueError("KUNDOAAB.DBA/DBM matcher ikke den validerte statiske DOS-layouten.")

    data = dbm_path.read_bytes()
    records: list[dict] = []
    for position in range(0, len(data), layout.record_size):
        record = data[position : position + layout.record_size]
        if len(record) != layout.record_size:
            continue

        row = {}
        for field in layout.fields:
            raw = record[field.offset : field.offset + field.storage_length]
            row[field.name] = _decode_reversed_field(field, raw)
        records.append(row)
    return records


def read_dataease_records(path: Path, tdf_path: Path | None = None) -> list[dict]:
    data = path.read_bytes()
    if len(data) < 128:
        if tdf_path:
            return _read_layout_records(path, tdf_path)
        raise ValueError("DBM-filen er for kort til å inneholde en gyldig header.")
    if data[:4] != b"DEFW":
        if tdf_path:
            return _read_layout_records(path, tdf_path)
        raise ValueError(
            "DBM-filen har ikke DataEase-signatur DEFW. Last opp tilhørende DBA, "
            "eller velg en mappe som inneholder DBA med samme filnavn."
        )

    field_count = struct.unpack_from("<H", data, 6)[0]
    record_count = struct.unpack_from("<I", data, 8)[0]
    header_size = struct.unpack_from("<H", data, 12)[0]
    record_size = struct.unpack_from("<H", data, 14)[0]

    fields: list[Field] = []
    offset = 128
    for _ in range(field_count):
        descriptor = data[offset : offset + 64]
        fields.append(
            Field(
                name=_decode_fixed_text(descriptor[0:20]),
                type_code=descriptor[20],
                flags=descriptor[21],
                length=struct.unpack_from("<H", descriptor, 22)[0],
                decimals=descriptor[24],
            )
        )
        offset += 64

    records: list[dict] = []
    for index in range(record_count):
        record_offset = header_size + index * record_size
        record = data[record_offset : record_offset + record_size]
        if len(record) < record_size or record[0] == 0x2A:
            continue

        row = {}
        cursor = 1
        for field in fields:
            raw = record[cursor : cursor + field.length]
            row[field.name] = _decode_field(field, raw)
            cursor += field.length
        records.append(row)

    return records


def _save_uploaded_file(file_item, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_path = Path(temp_file.name)
        file_item.file.seek(0)
        shutil.copyfileobj(file_item.file, temp_file)
    return temp_path


def read_uploaded_database(file_item, schema_file_item=None) -> tuple[str, list[dict]]:
    filename = Path(file_item.filename or "").name
    if not filename:
        raise ValueError("Velg en databasefil først.")

    suffix = Path(filename).suffix or ".dbm"
    temp_path = _save_uploaded_file(file_item, suffix)
    temp_schema_path = None
    if schema_file_item is not None and getattr(schema_file_item, "filename", ""):
        schema_suffix = Path(schema_file_item.filename).suffix or ".dba"
        temp_schema_path = _save_uploaded_file(schema_file_item, schema_suffix)

    try:
        records = read_dataease_records(temp_path, temp_schema_path)
    finally:
        temp_path.unlink(missing_ok=True)
        if temp_schema_path is not None:
            temp_schema_path.unlink(missing_ok=True)

    return filename, records


def _search_records_payload(source: str, records: list[dict], query: str) -> dict:
    needle = _normalize(query)
    if not needle:
        raise ValueError("Skriv inn et søkeord først.")

    field_names = list(records[0].keys())[:MAX_DISPLAY_FIELDS] if records else []
    matches = []
    total_matches = 0

    for row in records:
        searchable = _normalize(" ".join(str(value) for value in row.values()))
        if needle in searchable or all(part in searchable for part in needle.split()):
            total_matches += 1
            if len(matches) < 100:
                matches.append(
                    {
                        "title": _generic_record_title(row, total_matches),
                        "fields": [
                            {"label": name, "value": value}
                            for name, value in list(row.items())[:MAX_DISPLAY_FIELDS]
                        ],
                    }
                )

    return {
        "source": source,
        "fields": field_names,
        "records": len(records),
        "matches": matches,
        "totalMatches": total_matches,
        "limited": total_matches > len(matches),
    }


def _list_records_payload(source: str, records: list[dict]) -> dict:
    field_names = list(records[0].keys())[:MAX_DISPLAY_FIELDS] if records else []
    display_records = [
        {field: row.get(field, "") for field in field_names}
        for row in records
    ]

    return {
        "source": source,
        "fields": field_names,
        "records": display_records,
        "recordCount": len(records),
        "totalFields": len(records[0]) if records else 0,
        "limitedFields": bool(records and len(records[0]) > MAX_DISPLAY_FIELDS),
    }


def search_uploaded_database(file_item, query: str, schema_file_item=None) -> dict:
    filename, records = read_uploaded_database(file_item, schema_file_item)
    return _search_records_payload(filename, records, query)


def list_uploaded_database(file_item, schema_file_item=None) -> dict:
    filename, records = read_uploaded_database(file_item, schema_file_item)
    return _list_records_payload(filename, records)


def read_static_kundoaab_records() -> list[dict]:
    return _read_fixed_dos_records(KUNDOAAB_DBM_FILE, KUNDOAAB_DBA_FILE)


def search_static_kundoaab(query: str) -> dict:
    return _search_records_payload("KUNDOAAB statisk", read_static_kundoaab_records(), query)


def list_static_kundoaab() -> dict:
    return _list_records_payload("KUNDOAAB statisk", read_static_kundoaab_records())


def _generic_record_title(row: dict, index: int) -> str:
    for preferred in ["Kundenr", "CUSTOMER_ID", "PRODUKT_ID", "ID", "Navn", "NAVN", "NAME"]:
        value = str(row.get(preferred, "")).strip()
        if value:
            return value

    for value in row.values():
        text = str(value).strip()
        if text:
            return text[:80]

    return f"Record {index}"


def resolve_output_dir(output_dir: str) -> Path:
    requested = (output_dir or "").strip()
    if IS_VERCEL:
        if not requested or requested in {".", "./"}:
            return DEFAULT_OUTPUT_DIR

        candidate = Path(requested).expanduser()
        if candidate.is_absolute():
            tmp_dir = Path("/tmp")
            if candidate == tmp_dir or tmp_dir in candidate.parents:
                return candidate
            return DEFAULT_OUTPUT_DIR / candidate.name

        return DEFAULT_OUTPUT_DIR / candidate

    target_dir = Path(requested).expanduser() if requested else DEFAULT_OUTPUT_DIR
    if not target_dir.is_absolute():
        target_dir = BASE_DIR / target_dir
    return target_dir


def convert_csv(csv_path: str, table_name: str, output_dir: str) -> dict:
    source = Path(csv_path).expanduser()
    target_dir = resolve_output_dir(output_dir)

    if not source.is_absolute():
        source = BASE_DIR / source

    if not source.exists():
        raise ValueError(f"CSV-filen finnes ikke: {source}")
    if not source.is_file():
        raise ValueError(f"CSV-stien peker ikke på en fil: {source}")

    table, field_defs, records = parse_csv(source, table_name.strip() or None)
    dbm_path, tdf_path = generate_dbm(table, field_defs, records, target_dir)

    return {
        "table": table,
        "fields": len(field_defs),
        "records": len(records),
        "dbm": str(dbm_path),
        "tdf": str(tdf_path),
        "outputDir": str(target_dir),
        "dbmFilename": dbm_path.name,
        "tdfFilename": tdf_path.name,
        "dbmBase64": base64.b64encode(dbm_path.read_bytes()).decode("ascii"),
        "tdfBase64": base64.b64encode(tdf_path.read_bytes()).decode("ascii"),
        "temporaryOutput": IS_VERCEL,
    }


def convert_uploaded_csv(file_item, table_name: str, output_dir: str) -> dict:
    filename = Path(file_item.filename or "").name
    if not filename:
        raise ValueError("Velg en CSV-fil først.")

    target_dir = resolve_output_dir(output_dir)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
        temp_path = Path(temp_file.name)
        file_item.file.seek(0)
        shutil.copyfileobj(file_item.file, temp_file)

    try:
        table, field_defs, records = parse_csv(temp_path, table_name.strip() or None)
        dbm_path, tdf_path = generate_dbm(table, field_defs, records, target_dir)
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "table": table,
        "fields": len(field_defs),
        "records": len(records),
        "dbm": str(dbm_path),
        "tdf": str(tdf_path),
        "outputDir": str(target_dir),
        "dbmFilename": dbm_path.name,
        "tdfFilename": tdf_path.name,
        "dbmBase64": base64.b64encode(dbm_path.read_bytes()).decode("ascii"),
        "tdfBase64": base64.b64encode(tdf_path.read_bytes()).decode("ascii"),
        "temporaryOutput": IS_VERCEL,
        "source": filename,
    }


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict | list) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, status: int, markup: str) -> None:
    body = markup.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def howitwork_response(handler: BaseHTTPRequestHandler) -> None:
    try:
        markdown = HOWITWORK_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        html_response(handler, 404, "<!doctype html><title>Ikke funnet</title><p>howitwork.md finnes ikke.</p>")
        return

    escaped = html_lib.escape(markdown)
    html_response(
        handler,
        200,
        f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>How It Works</title>
  <style>
    :root {{
      --bg: #f6f7f3;
      --ink: #1f2623;
      --line: #d6ddd5;
      --accent: #116a5b;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(980px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }}
    a {{
      color: var(--accent);
      font-weight: 800;
      text-decoration: none;
    }}
    pre {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 20px;
      white-space: pre-wrap;
      line-height: 1.55;
      font: 0.95rem ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    }}
  </style>
</head>
<body>
  <main>
    <p><a href="/">Tilbake til appen</a></p>
    <pre>{escaped}</pre>
  </main>
</body>
</html>""",
    )


HTML = """<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DataEase filtolker</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f3;
      --ink: #1f2623;
      --muted: #69736d;
      --line: #d6ddd5;
      --panel: #ffffff;
      --accent: #116a5b;
      --accent-strong: #0b4f44;
      --soft: #e9f2ef;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
    }
    .app {
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #fff;
      padding: 28px 18px;
    }
    .brand-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 0 0 24px;
    }
    .brand {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: 0;
    }
    .help-badge {
      display: inline-grid;
      place-items: center;
      width: 30px;
      height: 30px;
      border: 1px solid var(--accent);
      border-radius: 999px;
      color: var(--accent);
      background: var(--soft);
      font-weight: 900;
      text-decoration: none;
    }
    .help-badge:hover {
      background: var(--accent);
      color: #fff;
    }
    .menu {
      display: grid;
      gap: 8px;
    }
    .menu-button {
      width: 100%;
      min-height: 44px;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 0 12px;
      background: transparent;
      color: var(--ink);
      font: inherit;
      font-weight: 700;
      text-align: left;
      cursor: pointer;
    }
    .menu-button:hover {
      background: var(--soft);
    }
    .menu-button.active {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    main {
      position: relative;
      width: min(920px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 48px 0;
    }
    .data-source {
      position: absolute;
      top: 18px;
      right: 0;
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 700;
      text-align: right;
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(2rem, 5vw, 4rem);
      line-height: 1;
      letter-spacing: 0;
    }
    .lede {
      margin: 0 0 28px;
      max-width: 680px;
      color: var(--muted);
      font-size: 1.05rem;
      line-height: 1.55;
    }
    .search {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      margin-bottom: 22px;
    }
    input,
    select {
      width: 100%;
      min-height: 52px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 16px;
      color: var(--ink);
      background: #fff;
      font: inherit;
      outline: none;
    }
    input:focus,
    select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(17, 106, 91, 0.14);
    }
    .hidden-file-input {
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }
    .search button,
    .convert-form button {
      min-height: 52px;
      border: 0;
      border-radius: 8px;
      padding: 0 20px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .search button:hover,
    .convert-form button:hover { background: var(--accent-strong); }
    .convert-form {
      display: grid;
      gap: 14px;
      max-width: 680px;
      margin-bottom: 22px;
    }
    #generic-file-search-form {
      max-width: 920px;
    }
    .field {
      display: grid;
      gap: 7px;
    }
    .field label {
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 800;
    }
    .database-picker {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .database-option {
      display: grid;
      gap: 10px;
      align-content: start;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
    }
    .database-option h2 {
      margin: 0;
      color: var(--text);
      font-size: 1rem;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .field-note {
      margin: 0;
      color: var(--muted);
      font-size: 0.86rem;
      line-height: 1.35;
    }
    .file-tabs {
      display: inline-grid;
      grid-template-columns: repeat(2, minmax(110px, 1fr));
      gap: 6px;
      max-width: 320px;
      margin: 4px 0 10px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .file-tab {
      min-height: 40px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-weight: 800;
      cursor: pointer;
    }
    .file-tab.active {
      background: var(--accent);
      color: #fff;
    }
    .file-tab-panel {
      display: none;
    }
    .file-tab-panel.active {
      display: block;
    }
    .table-actions {
      display: flex;
      justify-content: flex-end;
      margin: 0 0 12px;
    }
    .download-button {
      min-height: 42px;
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 0 14px;
      background: var(--soft);
      color: var(--accent);
      font: inherit;
      font-weight: 800;
      cursor: pointer;
    }
    .download-button:hover {
      background: var(--accent);
      color: #fff;
    }
    .view {
      display: none;
    }
    .view.active {
      display: block;
    }
    .status {
      min-height: 28px;
      color: var(--muted);
      font-size: 0.95rem;
    }
    .results {
      display: grid;
      gap: 12px;
      margin-top: 10px;
    }
    .person {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 18px;
    }
    .person h2 {
      margin: 0 0 14px;
      font-size: 1.2rem;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .customer-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }
    .customer-heading h2 {
      margin-bottom: 0;
    }
    .detail-toggle {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    .detail-toggle summary {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      border: 1px solid var(--accent);
      border-radius: 999px;
      padding: 0 12px;
      color: var(--accent);
      background: var(--soft);
      font-size: 0.88rem;
      font-weight: 800;
      cursor: pointer;
      list-style: none;
    }
    .detail-toggle summary::-webkit-details-marker {
      display: none;
    }
    .detail-toggle[open] summary {
      background: var(--accent);
      color: #fff;
    }
    .detail-sections {
      display: grid;
      gap: 18px;
      margin-top: 16px;
    }
    .detail-section {
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    .detail-section h3 {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 0.86rem;
      font-weight: 900;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px 18px;
    }
    .detail-item {
      min-width: 0;
    }
    .detail-label {
      display: block;
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 800;
    }
    .detail-value {
      display: block;
      margin-top: 2px;
      overflow-wrap: anywhere;
    }
    dl {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 10px 14px;
      margin: 0;
    }
    dt {
      color: var(--muted);
      font-weight: 700;
    }
    dd {
      margin: 0;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 22px;
      color: var(--muted);
      background: var(--soft);
    }
    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    table {
      width: 100%;
      min-width: 780px;
      border-collapse: collapse;
    }
    th,
    td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 800;
      text-transform: uppercase;
    }
    tr:last-child td {
      border-bottom: 0;
    }
    @media (max-width: 620px) {
      .app { grid-template-columns: 1fr; }
      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 16px;
      }
      .brand-row { margin-bottom: 12px; }
      .menu { grid-template-columns: 1fr 1fr; }
      .menu-button { text-align: center; }
      main { padding: 28px 0; }
      .data-source {
        position: static;
        margin-bottom: 18px;
        text-align: left;
      }
      .search { grid-template-columns: 1fr; }
      .search button { width: 100%; }
      .database-picker { grid-template-columns: 1fr; }
      dl { grid-template-columns: 1fr; gap: 3px 0; }
      .customer-heading { display: block; }
      .detail-grid { grid-template-columns: 1fr; }
      dt { margin-top: 8px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand-row">
        <p class="brand">DataEase</p>
        <a class="help-badge" href="/howitwork" title="Vis howitwork.md" aria-label="Vis howitwork.md">?</a>
      </div>
      <nav class="menu" aria-label="Hovedmeny">
        <button class="menu-button active" type="button" data-view="generic-file-search-view">Generisk fil tolker</button>
        <button class="menu-button" type="button" data-view="csv-convert-view">Konvertering av CSV</button>
      </nav>
    </aside>
    <main>
      <section class="view active" id="generic-file-search-view">
        <h1>Generisk fil tolker</h1>
        <p class="lede">Velg én DataEase DBM-fil direkte, eller velg en mappe og plukk en DBM-fil fra listen.</p>
        <form class="convert-form" id="generic-file-search-form">
          <div class="database-picker">
            <section class="database-option" aria-labelledby="single-db-title">
              <h2 id="single-db-title">Valg 1: enkeltfil</h2>
              <div class="field">
                <label for="generic-db-file">Velg Databasefil - flervalg for hente både dbm og dba filen</label>
                <input id="generic-db-file" name="generic-db-file" type="file" accept=".dbm,.DBM,.dba,.DBA,.tdf,.TDF,application/octet-stream,text/plain" multiple>
              </div>
              <div class="field">
                <label for="generic-tdf-file" id="generic-tdf-label">Definisjonsfil fallback</label>
                <input id="generic-tdf-file" name="generic-tdf-file" type="file" accept=".dba,.DBA,.tdf,.TDF,application/octet-stream,text/plain">
                <span class="field-note" id="generic-tdf-status"></span>
              </div>
              <p class="field-note">Velg DBM og DBA samtidig i samme filvalg, saa matches DBA automatisk. Fallback-feltet brukes bare hvis definisjonsfilen velges separat.</p>
            </section>
            <section class="database-option" aria-labelledby="folder-db-title">
              <h2 id="folder-db-title">Valg 2: mappe</h2>
              <div class="field">
                <label for="generic-db-folder-button">Mappe med DBM-filer</label>
                <button id="generic-db-folder-button" type="button" title="Velg mappe. Bare DBM-filer vises i listen.">Velg mappe</button>
                <input id="generic-db-folder" class="hidden-file-input" name="generic-db-folder" type="file" webkitdirectory directory multiple tabindex="-1" aria-hidden="true" title="Velg mappe. Bare DBM-filer vises i listen.">
              </div>
              <div class="field">
                <label for="generic-db-folder-list">Database fra mappe</label>
                <select id="generic-db-folder-list" name="generic-db-folder-list" disabled>
                  <option value="">Velg en mappe først</option>
                </select>
              </div>
              <p class="field-note">Bare .DBM-filer fra valgt mappe vises alfabetisk. Matchende .DBA/.TDF sendes med automatisk.</p>
            </section>
          </div>
          <div class="file-tabs" role="tablist" aria-label="Generisk fil tolker">
            <button class="file-tab active" type="button" data-generic-tab="generic-search-panel">Søk</button>
            <button class="file-tab" type="button" data-generic-tab="generic-list-panel">Liste</button>
          </div>
          <div class="file-tab-panel active" id="generic-search-panel">
            <div class="field">
              <label for="generic-db-query">Søkeord</label>
              <input id="generic-db-query" name="generic-db-query" placeholder="F.eks. navn, nummer, adresse eller e-post">
            </div>
            <button type="submit">Søk i fil</button>
          </div>
          <div class="file-tab-panel" id="generic-list-panel">
            <button id="generic-load-list" type="button">Last liste</button>
          </div>
        </form>
        <div class="status" id="generic-file-status"></div>
        <section class="results" id="generic-file-results" aria-live="polite"></section>
        <section id="generic-list-results" aria-live="polite"></section>
      </section>
      <section class="view" id="kundoaab-static-view">
        <h1>KUNDOAAB statisk</h1>
        <p class="lede">Leser KUNDOAAB.DBM og KUNDOAAB.DBA direkte fra app-mappen med den validerte statiske DOS-layouten.</p>
        <form class="convert-form" id="kundoaab-static-form">
          <div class="file-tabs" role="tablist" aria-label="KUNDOAAB statisk">
            <button class="file-tab active" type="button" data-kundoaab-tab="kundoaab-search-panel">Søk</button>
            <button class="file-tab" type="button" data-kundoaab-tab="kundoaab-list-panel">Liste</button>
          </div>
          <div class="file-tab-panel active" id="kundoaab-search-panel">
            <div class="field">
              <label for="kundoaab-query">Søkeord</label>
              <input id="kundoaab-query" name="kundoaab-query" placeholder="F.eks. navn, kundenummer, telefon eller adresse">
            </div>
            <button type="submit">Søk statisk</button>
          </div>
          <div class="file-tab-panel" id="kundoaab-list-panel">
            <button id="kundoaab-load-list" type="button">Last statisk liste</button>
          </div>
        </form>
        <div class="status" id="kundoaab-status"></div>
        <section class="results" id="kundoaab-results" aria-live="polite"></section>
        <section id="kundoaab-list-results" aria-live="polite"></section>
      </section>
      <section class="view" id="csv-convert-view">
        <h1>Konvertering av CSV</h1>
        <p class="lede">Lag en DataEase DBM-fil fra en CSV-fil og velg tabellnavnet som skal lagres i filen.</p>
        <form class="convert-form" id="csv-convert-form">
          <div class="field">
            <label for="csv-file">CSV-fil</label>
            <input id="csv-file" name="csv-file" type="file" accept=".csv,text/csv">
          </div>
          <div class="field">
            <label for="csv-table">Tabellnavn</label>
            <input id="csv-table" name="csv-table" placeholder="F.eks. KUNDER">
          </div>
          <div class="field">
            <label for="csv-output">Output-mappe</label>
            <input id="csv-output" name="csv-output" placeholder="Lokalt: f.eks. testkonvertering. På nett brukes midlertidig /tmp.">
          </div>
          <button type="submit">Konverter</button>
        </form>
        <div class="status" id="csv-convert-status"></div>
        <section class="results" id="csv-convert-results" aria-live="polite"></section>
      </section>
    </main>
  </div>
  <script>
    const menuButtons = document.querySelectorAll(".menu-button");
    const views = document.querySelectorAll(".view");
    const genericFileForm = document.querySelector("#generic-file-search-form");
    const genericDbFileInput = document.querySelector("#generic-db-file");
    const genericTdfLabel = document.querySelector("#generic-tdf-label");
    const genericTdfFileInput = document.querySelector("#generic-tdf-file");
    const genericTdfStatus = document.querySelector("#generic-tdf-status");
    const genericDbFolderButton = document.querySelector("#generic-db-folder-button");
    const genericDbFolderInput = document.querySelector("#generic-db-folder");
    const genericDbFolderList = document.querySelector("#generic-db-folder-list");
    const genericDbQueryInput = document.querySelector("#generic-db-query");
    const genericFileStatus = document.querySelector("#generic-file-status");
    const genericFileResults = document.querySelector("#generic-file-results");
    const genericFileTabs = document.querySelectorAll("[data-generic-tab]");
    const genericFilePanels = document.querySelectorAll(".file-tab-panel");
    const genericLoadListButton = document.querySelector("#generic-load-list");
    const genericListResults = document.querySelector("#generic-list-results");
    const kundoaabForm = document.querySelector("#kundoaab-static-form");
    const kundoaabQueryInput = document.querySelector("#kundoaab-query");
    const kundoaabStatus = document.querySelector("#kundoaab-status");
    const kundoaabResults = document.querySelector("#kundoaab-results");
    const kundoaabTabs = document.querySelectorAll("[data-kundoaab-tab]");
    const kundoaabPanels = document.querySelectorAll("#kundoaab-search-panel, #kundoaab-list-panel");
    const kundoaabLoadListButton = document.querySelector("#kundoaab-load-list");
    const kundoaabListResults = document.querySelector("#kundoaab-list-results");
    const csvConvertForm = document.querySelector("#csv-convert-form");
    const csvFileInput = document.querySelector("#csv-file");
    const csvTableInput = document.querySelector("#csv-table");
    const csvOutputInput = document.querySelector("#csv-output");
    const csvConvertStatus = document.querySelector("#csv-convert-status");
    const csvConvertResults = document.querySelector("#csv-convert-results");
    let genericListPayload = null;
    let genericFolderDbmFiles = [];
    let genericFolderFiles = [];
    let genericDatabaseMode = "single";

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[char]));
    }

    function renderConversion(payload) {
      const downloadLinks = payload.dbmBase64 && payload.tdfBase64 ? `
        <dt>Nedlasting</dt>
        <dd>
          <a download="${escapeHtml(payload.dbmFilename)}" href="data:application/octet-stream;base64,${escapeHtml(payload.dbmBase64)}">Last ned DBM</a>
          &nbsp;
          <a download="${escapeHtml(payload.tdfFilename)}" href="data:text/plain;base64,${escapeHtml(payload.tdfBase64)}">Last ned TDF</a>
        </dd>
      ` : "";
      const temporaryNote = payload.temporaryOutput
        ? '<dt>Lagring</dt><dd>Midlertidig servermappe. Last ned filene for å beholde dem.</dd>'
        : "";

      csvConvertResults.innerHTML = `
        <article class="person">
          <h2>${escapeHtml(payload.table)}</h2>
          <dl>
            <dt>DBM-fil</dt><dd>${escapeHtml(payload.dbm)}</dd>
            <dt>TDF-fil</dt><dd>${escapeHtml(payload.tdf)}</dd>
            <dt>Output-mappe</dt><dd>${escapeHtml(payload.outputDir || "-")}</dd>
            <dt>CSV-fil</dt><dd>${escapeHtml(payload.source || "-")}</dd>
            <dt>Felter</dt><dd>${escapeHtml(payload.fields)}</dd>
            <dt>Records</dt><dd>${escapeHtml(payload.records)}</dd>
            ${temporaryNote}
            ${downloadLinks}
          </dl>
        </article>
      `;
    }

    function renderRecordResults(payload, target) {
      if (!payload.matches.length) {
        target.innerHTML = '<div class="empty">Ingen treff funnet.</div>';
        return;
      }

      target.innerHTML = payload.matches.map((record) => `
        <article class="person">
          <h2>${escapeHtml(record.title)}</h2>
          <div class="detail-grid">
            ${record.fields.map((field) => `
              <div class="detail-item">
                <span class="detail-label">${escapeHtml(field.label)}</span>
                <span class="detail-value">${escapeHtml(field.value ?? "-")}</span>
              </div>
            `).join("")}
          </div>
        </article>
      `).join("");
    }

    function renderGenericResults(payload) {
      renderRecordResults(payload, genericFileResults);
    }

    function renderRecordList(payload, target, clearTarget) {
      genericListPayload = payload;
      clearTarget.innerHTML = "";

      if (!payload.records.length) {
        target.innerHTML = '<div class="empty">Ingen records funnet.</div>';
        return;
      }

      target.innerHTML = `
        <div class="table-actions">
          <button class="download-button download-csv" type="button">Lagre som CSV</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                ${payload.fields.map((field) => `<th>${escapeHtml(field)}</th>`).join("")}
              </tr>
            </thead>
            <tbody>
              ${payload.records.map((record) => `
                <tr>
                  ${payload.fields.map((field) => `<td>${escapeHtml(record[field] ?? "")}</td>`).join("")}
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;

      target.querySelector(".download-csv").addEventListener("click", downloadGenericCsv);
    }

    function renderGenericList(payload) {
      renderRecordList(payload, genericListResults, genericFileResults);
    }

    function renderKundoaabList(payload) {
      renderRecordList(payload, kundoaabListResults, kundoaabResults);
    }

    function csvCell(value) {
      return `"${String(value ?? "").replaceAll('"', '""')}"`;
    }

    function downloadGenericCsv() {
      if (!genericListPayload) return;

      const rows = [
        genericListPayload.fields.map(csvCell).join(","),
        ...genericListPayload.records.map((record) =>
          genericListPayload.fields.map((field) => csvCell(record[field])).join(",")
        )
      ];
      const csv = `\uFEFF${rows.join("\\r\\n")}`;
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const source = genericListPayload.source.replace(/\\.[^.]+$/, "") || "database";
      const filename = `${source}-liste.csv`;
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      URL.revokeObjectURL(link.href);
      link.remove();
    }

    function clearGenericOutput() {
      genericListPayload = null;
      genericFileResults.innerHTML = "";
      genericListResults.innerHTML = "";
    }

    function selectedFolderDbmFile() {
      if (genericDbFolderList.value === "") return null;
      const selectedIndex = Number(genericDbFolderList.value);
      if (!Number.isInteger(selectedIndex)) return null;
      return genericFolderDbmFiles[selectedIndex] || null;
    }

    function selectedGenericDbFile() {
      if (genericDatabaseMode === "folder") return selectedFolderDbmFile();
      return Array.from(genericDbFileInput.files || [])
        .find((file) => file.name.toLowerCase().endsWith(".dbm"))
        || genericDbFileInput.files[0]
        || null;
    }

    function fileStem(file) {
      return (file?.name || "").replace(/\\.[^.]+$/, "").toLowerCase();
    }

    function folderPathDir(file) {
      const path = file?.webkitRelativePath || "";
      const slash = path.lastIndexOf("/");
      return slash >= 0 ? path.slice(0, slash).toLowerCase() : "";
    }

    function selectedFolderName() {
      const firstPath = genericFolderFiles[0]?.webkitRelativePath || "";
      return firstPath.split("/")[0] || "";
    }

    function selectedFolderSchemaFile() {
      const dbFile = selectedFolderDbmFile();
      if (!dbFile) return null;
      const stem = fileStem(dbFile);
      const dir = folderPathDir(dbFile);
      const sameTable = (file) => fileStem(file) === stem && folderPathDir(file) === dir;
      return genericFolderFiles.find((file) => file.name.toLowerCase().endsWith(".dba") && sameTable(file))
        || genericFolderFiles.find((file) => file.name.toLowerCase().endsWith(".tdf") && sameTable(file))
        || null;
    }

    function selectedGenericSchemaFile() {
      if (genericDatabaseMode === "folder") return selectedFolderSchemaFile();
      const dbFile = selectedGenericDbFile();
      const sameTable = (file) => fileStem(file) === fileStem(dbFile);
      const selectedFiles = Array.from(genericDbFileInput.files || []);
      const pairedSchema = selectedFiles.find((file) => file.name.toLowerCase().endsWith(".dba") && sameTable(file))
        || selectedFiles.find((file) => file.name.toLowerCase().endsWith(".tdf") && sameTable(file));
      if (pairedSchema) return pairedSchema;
      return genericTdfFileInput.files[0] || null;
    }

    function selectedSchemaFromDbPicker() {
      if (genericDatabaseMode === "folder") return null;
      const dbFile = selectedGenericDbFile();
      if (!dbFile || !dbFile.name.toLowerCase().endsWith(".dbm")) return null;
      const selectedFiles = Array.from(genericDbFileInput.files || []);
      const sameTable = (file) => fileStem(file) === fileStem(dbFile);
      return selectedFiles.find((file) => file.name.toLowerCase().endsWith(".dba") && sameTable(file))
        || selectedFiles.find((file) => file.name.toLowerCase().endsWith(".tdf") && sameTable(file))
        || null;
    }

    function refreshGenericTdfFallbackState() {
      const bundledSchema = selectedSchemaFromDbPicker();
      const fallbackSchema = genericTdfFileInput.files[0] || null;
      const selectedDbPickerFiles = Array.from(genericDbFileInput.files || []);
      if (bundledSchema) {
        genericTdfLabel.textContent = "Definisjonsfil fallback - fil valgt med dbm";
        genericTdfStatus.textContent = bundledSchema.name;
        return;
      }
      if (genericDatabaseMode !== "folder" && selectedDbPickerFiles.length) {
        genericTdfLabel.textContent = "Definisjonsfil fallback - mangler DBM";
        genericTdfStatus.textContent = "";
        return;
      }
      genericTdfLabel.textContent = "Definisjonsfil fallback";
      genericTdfStatus.textContent = fallbackSchema ? `Separat valgt: ${fallbackSchema.name}` : "";
    }

    function selectedGenericDbSourceLabel() {
      return genericDatabaseMode === "folder" ? "fra mappelisten" : "som enkeltfil";
    }

    function selectedGenericSchemaLabel() {
      const schemaFile = selectedGenericSchemaFile();
      if (!schemaFile) return "";
      return ` med definisjonsfil ${schemaFile.webkitRelativePath || schemaFile.name}`;
    }

    function folderDbmSummary() {
      if (!genericFolderDbmFiles.length) return "Ingen .DBM-filer funnet";
      const names = genericFolderDbmFiles.map((file) => file.webkitRelativePath || file.name);
      const visibleNames = names.slice(0, 8).join(", ");
      const hiddenCount = names.length - 8;
      return hiddenCount > 0
        ? `${genericFolderDbmFiles.length} DBM-filer: ${visibleNames} og ${hiddenCount} til`
        : `${genericFolderDbmFiles.length} DBM-filer: ${visibleNames}`;
    }

    function refreshFolderDbmList() {
      genericFolderFiles = Array.from(genericDbFolderInput.files || []);
      genericFolderDbmFiles = genericFolderFiles
        .filter((file) => file.name.toLowerCase().endsWith(".dbm"))
        .sort((left, right) => {
          const byName = left.name.localeCompare(right.name, "nb", { sensitivity: "base" });
          if (byName !== 0) return byName;
          return (left.webkitRelativePath || left.name).localeCompare(
            right.webkitRelativePath || right.name,
            "nb",
            { sensitivity: "base" }
          );
        });

      genericDbFolderList.innerHTML = "";

      if (!genericFolderDbmFiles.length) {
        genericDbFolderList.disabled = true;
        genericDbFolderList.innerHTML = '<option value="">Ingen .DBM-filer funnet</option>';
        genericDbFolderButton.textContent = selectedFolderName() || "Velg mappe";
        genericDbFolderInput.title = "Ingen .DBM-filer funnet i valgt mappe.";
        genericDbFolderList.title = "Ingen .DBM-filer funnet i valgt mappe.";
        genericFileStatus.textContent = "Ingen .DBM-filer ble funnet i valgt mappe.";
        return;
      }

      genericDbFolderList.disabled = false;
      genericFolderDbmFiles.forEach((file, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        const schemaFile = genericFolderFiles.find((candidate) =>
          candidate.name.toLowerCase().endsWith(".dba") &&
          fileStem(candidate) === fileStem(file) &&
          folderPathDir(candidate) === folderPathDir(file)
        ) || genericFolderFiles.find((candidate) =>
          candidate.name.toLowerCase().endsWith(".tdf") &&
          fileStem(candidate) === fileStem(file) &&
          folderPathDir(candidate) === folderPathDir(file)
        );
        const schemaMarker = schemaFile ? ` + ${schemaFile.name.split(".").pop().toUpperCase()}` : "";
        option.textContent = `${file.webkitRelativePath || file.name}${schemaMarker}`;
        option.title = option.textContent;
        genericDbFolderList.appendChild(option);
      });
      genericDatabaseMode = "folder";
      const summary = folderDbmSummary();
      genericDbFolderButton.textContent = selectedFolderName() || "Valgt mappe";
      genericDbFolderInput.title = summary;
      genericDbFolderList.title = summary;
      genericFileStatus.textContent = `${genericFolderDbmFiles.length} DBM-filer funnet i valgt mappe. Velg database i listen.`;
    }

    async function loadGenericList() {
      const dbFile = selectedGenericDbFile();

      if (!dbFile) {
        genericFileStatus.textContent = "Velg en databasefil først, enten som enkeltfil eller fra mappelisten.";
        genericListResults.innerHTML = "";
        return;
      }

      genericFileStatus.textContent = `Tolker ${dbFile.name} ${selectedGenericDbSourceLabel()}${selectedGenericSchemaLabel()} og laster liste...`;
      genericFileResults.innerHTML = "";
      genericListResults.innerHTML = "";

      try {
        const formData = new FormData();
        formData.append("databaseFile", dbFile);
        const schemaFile = selectedGenericSchemaFile();
        if (schemaFile) formData.append("schemaFile", schemaFile);

        const response = await fetch("/api/generic-file/list", {
          method: "POST",
          body: formData
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Kunne ikke laste listen.");
        const fieldLimitText = payload.limitedFields ? ` Viser de første ${payload.fields.length} av ${payload.totalFields} felter.` : "";
        genericFileStatus.textContent = `${payload.recordCount} records i ${payload.source}. ${payload.fields.length} felter.${fieldLimitText}`;
        renderGenericList(payload);
      } catch (error) {
        genericFileStatus.textContent = error.message;
        genericListResults.innerHTML = "";
      }
    }

    async function loadKundoaabList() {
      kundoaabStatus.textContent = "Laster statisk KUNDOAAB-liste...";
      kundoaabResults.innerHTML = "";
      kundoaabListResults.innerHTML = "";

      try {
        const response = await fetch("/api/kundoaab/list");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Kunne ikke laste statisk KUNDOAAB-liste.");
        const fieldLimitText = payload.limitedFields ? ` Viser de første ${payload.fields.length} av ${payload.totalFields} felter.` : "";
        kundoaabStatus.textContent = `${payload.recordCount} records i ${payload.source}. ${payload.fields.length} felter.${fieldLimitText}`;
        renderKundoaabList(payload);
      } catch (error) {
        kundoaabStatus.textContent = error.message;
        kundoaabListResults.innerHTML = "";
      }
    }

    menuButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const viewId = button.dataset.view;
        menuButtons.forEach((item) => item.classList.toggle("active", item === button));
        views.forEach((view) => view.classList.toggle("active", view.id === viewId));
        if (viewId === "generic-file-search-view") genericDbFileInput.focus();
        if (viewId === "kundoaab-static-view") kundoaabQueryInput.focus();
        if (viewId === "csv-convert-view") csvFileInput.focus();
      });
    });

    genericFileTabs.forEach((button) => {
      button.addEventListener("click", () => {
        const panelId = button.dataset.genericTab;
        genericFileTabs.forEach((item) => item.classList.toggle("active", item === button));
        genericFilePanels.forEach((panel) => panel.classList.toggle("active", panel.id === panelId));
        genericFileResults.innerHTML = "";
        genericListResults.innerHTML = "";
        if (panelId === "generic-search-panel") genericDbQueryInput.focus();
        if (panelId === "generic-list-panel") loadGenericList();
      });
    });

    kundoaabTabs.forEach((button) => {
      button.addEventListener("click", () => {
        const panelId = button.dataset.kundoaabTab;
        kundoaabTabs.forEach((item) => item.classList.toggle("active", item === button));
        kundoaabPanels.forEach((panel) => panel.classList.toggle("active", panel.id === panelId));
        kundoaabResults.innerHTML = "";
        kundoaabListResults.innerHTML = "";
        if (panelId === "kundoaab-search-panel") kundoaabQueryInput.focus();
        if (panelId === "kundoaab-list-panel") loadKundoaabList();
      });
    });

    genericDbFileInput.addEventListener("change", () => {
      genericDatabaseMode = "single";
      refreshGenericTdfFallbackState();
      const dbFile = selectedGenericDbFile();
      genericFileStatus.textContent = dbFile
        ? `Valgt enkeltfil: ${dbFile.name}${selectedGenericSchemaLabel()}`
        : "";
      clearGenericOutput();
    });

    genericTdfFileInput.addEventListener("change", () => {
      genericDatabaseMode = "single";
      refreshGenericTdfFallbackState();
      const dbFile = genericDbFileInput.files[0];
      const schemaFile = genericTdfFileInput.files[0];
      genericFileStatus.textContent = schemaFile
        ? `Valgt definisjonsfil: ${schemaFile.name}${dbFile ? ` for ${dbFile.name}` : ""}`
        : "";
      clearGenericOutput();
    });

    genericDbFolderButton.addEventListener("click", () => genericDbFolderInput.click());

    genericDbFolderInput.addEventListener("change", () => {
      clearGenericOutput();
      refreshFolderDbmList();
      refreshGenericTdfFallbackState();
    });

    genericDbFolderList.addEventListener("change", () => {
      genericDatabaseMode = "folder";
      refreshGenericTdfFallbackState();
      const dbFile = selectedFolderDbmFile();
      genericFileStatus.textContent = dbFile
        ? `Valgt fra mappe: ${dbFile.webkitRelativePath || dbFile.name}${selectedGenericSchemaLabel()}`
        : "";
      clearGenericOutput();
    });

    genericLoadListButton.addEventListener("click", loadGenericList);
    kundoaabLoadListButton.addEventListener("click", loadKundoaabList);

    genericFileForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const dbFile = selectedGenericDbFile();
      const query = genericDbQueryInput.value.trim();

      if (!dbFile) {
        genericFileStatus.textContent = "Velg en databasefil først, enten som enkeltfil eller fra mappelisten.";
        genericFileResults.innerHTML = "";
        return;
      }

      if (!query) {
        genericFileStatus.textContent = "Skriv inn et søkeord først.";
        genericFileResults.innerHTML = "";
        return;
      }

      genericFileStatus.textContent = `Tolker ${dbFile.name} ${selectedGenericDbSourceLabel()}${selectedGenericSchemaLabel()} og søker...`;
      genericFileResults.innerHTML = "";
      genericListResults.innerHTML = "";

      try {
        const formData = new FormData();
        formData.append("databaseFile", dbFile);
        const schemaFile = selectedGenericSchemaFile();
        if (schemaFile) formData.append("schemaFile", schemaFile);
        formData.append("query", query);

        const response = await fetch("/api/generic-file/search", {
          method: "POST",
          body: formData
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Søket i filen feilet.");
        const limitText = payload.limited ? " Viser de første 100." : "";
        genericFileStatus.textContent = `${payload.totalMatches} treff for "${query}" i ${payload.source}. ${payload.records} records, ${payload.fields.length} felter.${limitText}`;
        renderGenericResults(payload);
      } catch (error) {
        genericFileStatus.textContent = error.message;
        genericFileResults.innerHTML = "";
      }
    });

    kundoaabForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = kundoaabQueryInput.value.trim();

      if (!query) {
        kundoaabStatus.textContent = "Skriv inn et søkeord først.";
        kundoaabResults.innerHTML = "";
        return;
      }

      kundoaabStatus.textContent = "Søker statisk KUNDOAAB...";
      kundoaabResults.innerHTML = "";
      kundoaabListResults.innerHTML = "";

      try {
        const response = await fetch(`/api/kundoaab/search?query=${encodeURIComponent(query)}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Statisk KUNDOAAB-søk feilet.");
        const limitText = payload.limited ? " Viser de første 100." : "";
        kundoaabStatus.textContent = `${payload.totalMatches} treff for "${query}" i ${payload.source}. ${payload.records} records, ${payload.fields.length} felter.${limitText}`;
        renderRecordResults(payload, kundoaabResults);
      } catch (error) {
        kundoaabStatus.textContent = error.message;
        kundoaabResults.innerHTML = "";
      }
    });

    csvConvertForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const csvFile = csvFileInput.files[0];
      const tableName = csvTableInput.value.trim();
      const outputDir = csvOutputInput.value.trim();

      if (!csvFile) {
        csvConvertStatus.textContent = "Velg en CSV-fil først.";
        csvConvertResults.innerHTML = "";
        return;
      }

      csvConvertStatus.textContent = "Konverterer...";
      csvConvertResults.innerHTML = "";

      try {
        const formData = new FormData();
        formData.append("csvFile", csvFile);
        formData.append("tableName", tableName);
        formData.append("outputDir", outputDir);

        const response = await fetch("/api/convert-csv", {
          method: "POST",
          body: formData
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Konverteringen feilet.");
        csvConvertStatus.textContent = "Konvertering fullført.";
        renderConversion(payload);
      } catch (error) {
        csvConvertStatus.textContent = error.message;
        csvConvertResults.innerHTML = "";
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html_response(self, 200, HTML)
            return

        if parsed.path == "/howitwork":
            howitwork_response(self)
            return

        if parsed.path == "/api/kundoaab/search":
            query = parse_qs(parsed.query).get("query", [""])[0]
            try:
                json_response(self, 200, search_static_kundoaab(query))
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return

        if parsed.path == "/api/kundoaab/list":
            try:
                json_response(self, 200, list_static_kundoaab())
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return

        json_response(self, 404, {"error": "Ikke funnet"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/convert-csv":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("multipart/form-data"):
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={
                            "REQUEST_METHOD": "POST",
                            "CONTENT_TYPE": content_type,
                            "CONTENT_LENGTH": str(content_length),
                        },
                    )
                    file_item = form["csvFile"] if "csvFile" in form else None
                    if file_item is None or not getattr(file_item, "file", None):
                        raise ValueError("Velg en CSV-fil først.")
                    result = convert_uploaded_csv(
                        file_item,
                        form.getfirst("tableName", ""),
                        form.getfirst("outputDir", ""),
                    )
                else:
                    raw_body = self.rfile.read(content_length)
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    result = convert_csv(
                        str(payload.get("csvPath", "")),
                        str(payload.get("tableName", "")),
                        str(payload.get("outputDir", "")),
                    )
                json_response(self, 200, result)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return

        if parsed.path == "/api/generic-file/search":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise ValueError("Send databasefilen som multipart/form-data.")

                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                file_item = form["databaseFile"] if "databaseFile" in form else None
                if file_item is None or not getattr(file_item, "file", None):
                    raise ValueError("Velg en databasefil først.")
                schema_item = form["schemaFile"] if "schemaFile" in form else None

                result = search_uploaded_database(file_item, form.getfirst("query", ""), schema_item)
                json_response(self, 200, result)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return

        if parsed.path == "/api/generic-file/list":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data"):
                    raise ValueError("Send databasefilen som multipart/form-data.")

                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": str(content_length),
                    },
                )
                file_item = form["databaseFile"] if "databaseFile" in form else None
                if file_item is None or not getattr(file_item, "file", None):
                    raise ValueError("Velg en databasefil først.")
                schema_item = form["schemaFile"] if "schemaFile" in form else None

                result = list_uploaded_database(file_item, schema_item)
                json_response(self, 200, result)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return

        json_response(self, 404, {"error": "Ikke funnet"})

    def log_message(self, format: str, *args) -> None:
        return


handler = Handler


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Åpne http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
