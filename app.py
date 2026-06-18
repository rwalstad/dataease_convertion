from __future__ import annotations

import cgi
import base64
import html as html_lib
import json
import os
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
DATA_FILE = BASE_DIR / "CUSTOMERS.DBM"
PRODUCT_FILE = BASE_DIR / "PRODUKTER.DBM"
HOWITWORK_FILE = BASE_DIR / "howitwork.md"
IS_VERCEL = os.environ.get("VERCEL") == "1" or BASE_DIR == Path("/var/task")
DEFAULT_OUTPUT_DIR = Path("/tmp/dataease_convertion") if IS_VERCEL else BASE_DIR
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))


@dataclass(frozen=True)
class Field:
    name: str
    type_code: int
    length: int
    decimals: int
    flags: int


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


def read_dataease_records(path: Path = DATA_FILE) -> list[dict]:
    data = path.read_bytes()
    if len(data) < 128:
        raise ValueError("DBM-filen er for kort til å inneholde en gyldig header.")
    if data[:4] != b"DEFW":
        raise ValueError("DBM-filen har ikke forventet DataEase-signatur DEFW.")

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


def search_people(query: str) -> list[dict]:
    needle = _normalize(query)
    if not needle:
        return []

    matches = []
    for row in read_dataease_records():
        first_name = str(row.get("FIRST_NAME", ""))
        last_name = str(row.get("LAST_NAME", ""))
        full_name = f"{first_name} {last_name}"
        searchable = _normalize(full_name)

        if needle in searchable or all(part in searchable for part in needle.split()):
            matches.append(
                {
                    "name": full_name,
                    "address": ", ".join(
                        part
                        for part in [str(row.get("ADDRESS", "")), str(row.get("CITY", ""))]
                        if part
                    ),
                    "email": row.get("EMAIL", ""),
                    "phone": row.get("PHONE", ""),
                }
            )

    return matches


def list_people() -> list[dict]:
    people = []
    for row in read_dataease_records():
        first_name = str(row.get("FIRST_NAME", ""))
        last_name = str(row.get("LAST_NAME", ""))
        people.append(
            {
                "id": row.get("CUSTOMER_ID", ""),
                "name": f"{first_name} {last_name}".strip(),
                "address": row.get("ADDRESS", ""),
                "city": row.get("CITY", ""),
                "phone": row.get("PHONE", ""),
                "email": row.get("EMAIL", ""),
                "createdDate": row.get("CREATED_DATE", ""),
            }
        )
    return people


def _format_price(value) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value or "")


def _product_payload(row: dict) -> dict:
    return {
        "id": row.get("PRODUKT_ID", ""),
        "name": row.get("NAVN", ""),
        "itemNumber": row.get("VARENUMMER", ""),
        "unitPrice": row.get("ENHETSPRIS", ""),
        "unitPriceDisplay": _format_price(row.get("ENHETSPRIS", "")),
        "costPrice": row.get("KOSTPRIS", ""),
        "costPriceDisplay": _format_price(row.get("KOSTPRIS", "")),
        "vatType": row.get("MVA_TYPE", ""),
        "incomeAccount": row.get("INNTEKTSKONTO", ""),
    }


def search_products(query: str) -> list[dict]:
    needle = _normalize(query)
    if not needle:
        return []

    matches = []
    for row in read_dataease_records(PRODUCT_FILE):
        searchable = _normalize(
            " ".join(
                str(row.get(field, ""))
                for field in ["NAVN", "VARENUMMER", "MVA_TYPE", "INNTEKTSKONTO"]
            )
        )

        if needle in searchable or all(part in searchable for part in needle.split()):
            matches.append(_product_payload(row))

    return matches


def list_products() -> list[dict]:
    return [_product_payload(row) for row in read_dataease_records(PRODUCT_FILE)]


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
  <title>DataEase personsøk</title>
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
    input {
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
    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(17, 106, 91, 0.14);
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
    .field {
      display: grid;
      gap: 7px;
    }
    .field label {
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 800;
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
      dl { grid-template-columns: 1fr; gap: 3px 0; }
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
        <button class="menu-button active" type="button" data-view="search-view">Person-Søk</button>
        <button class="menu-button" type="button" data-view="list-view">Liste over personer</button>
        <button class="menu-button" type="button" data-view="product-search-view">Produkt-Søk</button>
        <button class="menu-button" type="button" data-view="product-list-view">Liste over produkter</button>
        <button class="menu-button" type="button" data-view="csv-convert-view">Konvertering av CSV</button>
      </nav>
    </aside>
    <main>
      <div class="data-source">Current data is from files: CUSTOMERS.DBM / PRODUKTER.DBM</div>
      <section class="view active" id="search-view">
        <h1>Personsøk</h1>
        <p class="lede">Skriv inn fornavn og etternavn for å hente kontaktinformasjon fra DataEase-filen.</p>
        <form class="search" id="search-form">
          <input id="name" name="name" autocomplete="name" placeholder="F.eks. Alice Hansen" autofocus>
          <button type="submit">Søk</button>
        </form>
        <div class="status" id="status"></div>
        <section class="results" id="results" aria-live="polite"></section>
      </section>
      <section class="view" id="list-view">
        <h1>Liste</h1>
        <p class="lede">Alle records i tabellen.</p>
        <div class="status" id="list-status"></div>
        <section id="list-results" aria-live="polite"></section>
      </section>
      <section class="view" id="product-search-view">
        <h1>Produktsøk</h1>
        <p class="lede">Søk etter produktnavn, varenummer, MVA-type eller inntektskonto.</p>
        <form class="search" id="product-search-form">
          <input id="product-query" name="product-query" placeholder="F.eks. Arbeidstid eller 3020">
          <button type="submit">Søk</button>
        </form>
        <div class="status" id="product-status"></div>
        <section class="results" id="product-results" aria-live="polite"></section>
      </section>
      <section class="view" id="product-list-view">
        <h1>Produktliste</h1>
        <p class="lede">Alle produkter i produkttabellen.</p>
        <div class="status" id="product-list-status"></div>
        <section id="product-list-results" aria-live="polite"></section>
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
    const form = document.querySelector("#search-form");
    const input = document.querySelector("#name");
    const status = document.querySelector("#status");
    const results = document.querySelector("#results");
    const listStatus = document.querySelector("#list-status");
    const listResults = document.querySelector("#list-results");
    const productForm = document.querySelector("#product-search-form");
    const productInput = document.querySelector("#product-query");
    const productStatus = document.querySelector("#product-status");
    const productResults = document.querySelector("#product-results");
    const productListStatus = document.querySelector("#product-list-status");
    const productListResults = document.querySelector("#product-list-results");
    const csvConvertForm = document.querySelector("#csv-convert-form");
    const csvFileInput = document.querySelector("#csv-file");
    const csvTableInput = document.querySelector("#csv-table");
    const csvOutputInput = document.querySelector("#csv-output");
    const csvConvertStatus = document.querySelector("#csv-convert-status");
    const csvConvertResults = document.querySelector("#csv-convert-results");
    let listLoaded = false;
    let productListLoaded = false;

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[char]));
    }

    function renderPeople(people) {
      if (!people.length) {
        results.innerHTML = '<div class="empty">Ingen treff.</div>';
        return;
      }

      results.innerHTML = people.map((person) => `
        <article class="person">
          <h2>${escapeHtml(person.name)}</h2>
          <dl>
            <dt>Adresse</dt><dd>${escapeHtml(person.address)}</dd>
            <dt>E-post</dt><dd><a href="mailto:${escapeHtml(person.email)}">${escapeHtml(person.email)}</a></dd>
            <dt>Telefon</dt><dd><a href="tel:${escapeHtml(person.phone)}">${escapeHtml(person.phone)}</a></dd>
          </dl>
        </article>
      `).join("");
    }

    function renderList(people) {
      if (!people.length) {
        listResults.innerHTML = '<div class="empty">Ingen records funnet.</div>';
        return;
      }

      listResults.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Navn</th>
                <th>Adresse</th>
                <th>By</th>
                <th>Telefon</th>
                <th>E-post</th>
                <th>Opprettet</th>
              </tr>
            </thead>
            <tbody>
              ${people.map((person) => `
                <tr>
                  <td>${escapeHtml(person.id)}</td>
                  <td>${escapeHtml(person.name)}</td>
                  <td>${escapeHtml(person.address)}</td>
                  <td>${escapeHtml(person.city)}</td>
                  <td><a href="tel:${escapeHtml(person.phone)}">${escapeHtml(person.phone)}</a></td>
                  <td><a href="mailto:${escapeHtml(person.email)}">${escapeHtml(person.email)}</a></td>
                  <td>${escapeHtml(person.createdDate)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
    }

    function renderProducts(products, target) {
      if (!products.length) {
        target.innerHTML = '<div class="empty">Ingen produkter funnet.</div>';
        return;
      }

      target.innerHTML = products.map((product) => `
        <article class="person">
          <h2>${escapeHtml(product.name)}</h2>
          <dl>
            <dt>Varenummer</dt><dd>${escapeHtml(product.itemNumber || "-")}</dd>
            <dt>Enhetspris</dt><dd>${escapeHtml(product.unitPriceDisplay)}</dd>
            <dt>Kostpris</dt><dd>${escapeHtml(product.costPriceDisplay)}</dd>
            <dt>MVA</dt><dd>${escapeHtml(product.vatType)}</dd>
            <dt>Konto</dt><dd>${escapeHtml(product.incomeAccount)}</dd>
          </dl>
        </article>
      `).join("");
    }

    function renderProductList(products) {
      if (!products.length) {
        productListResults.innerHTML = '<div class="empty">Ingen produkter funnet.</div>';
        return;
      }

      productListResults.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Produkt</th>
                <th>Varenummer</th>
                <th>Enhetspris</th>
                <th>Kostpris</th>
                <th>MVA</th>
                <th>Konto</th>
              </tr>
            </thead>
            <tbody>
              ${products.map((product) => `
                <tr>
                  <td>${escapeHtml(product.id)}</td>
                  <td>${escapeHtml(product.name)}</td>
                  <td>${escapeHtml(product.itemNumber)}</td>
                  <td>${escapeHtml(product.unitPriceDisplay)}</td>
                  <td>${escapeHtml(product.costPriceDisplay)}</td>
                  <td>${escapeHtml(product.vatType)}</td>
                  <td>${escapeHtml(product.incomeAccount)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
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

    async function loadList() {
      if (listLoaded) return;
      listStatus.textContent = "Laster records...";
      listResults.innerHTML = "";

      try {
        const response = await fetch("/api/records");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Kunne ikke laste records.");
        listStatus.textContent = `${payload.length} records`;
        renderList(payload);
        listLoaded = true;
      } catch (error) {
        listStatus.textContent = error.message;
        listResults.innerHTML = "";
      }
    }

    async function loadProductList() {
      if (productListLoaded) return;
      productListStatus.textContent = "Laster produkter...";
      productListResults.innerHTML = "";

      try {
        const response = await fetch("/api/products");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Kunne ikke laste produkter.");
        productListStatus.textContent = `${payload.length} produkter`;
        renderProductList(payload);
        productListLoaded = true;
      } catch (error) {
        productListStatus.textContent = error.message;
        productListResults.innerHTML = "";
      }
    }

    menuButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const viewId = button.dataset.view;
        menuButtons.forEach((item) => item.classList.toggle("active", item === button));
        views.forEach((view) => view.classList.toggle("active", view.id === viewId));
        if (viewId === "list-view") loadList();
        if (viewId === "search-view") input.focus();
        if (viewId === "product-list-view") loadProductList();
        if (viewId === "product-search-view") productInput.focus();
        if (viewId === "csv-convert-view") csvFileInput.focus();
      });
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name) {
        status.textContent = "Skriv inn et navn først.";
        results.innerHTML = "";
        return;
      }

      status.textContent = "Søker...";
      results.innerHTML = "";

      try {
        const response = await fetch(`/api/search?name=${encodeURIComponent(name)}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Søket feilet.");
        status.textContent = `${payload.length} treff for "${name}"`;
        renderPeople(payload);
      } catch (error) {
        status.textContent = error.message;
        results.innerHTML = "";
      }
    });

    productForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = productInput.value.trim();
      if (!query) {
        productStatus.textContent = "Skriv inn et produkt, varenummer eller konto først.";
        productResults.innerHTML = "";
        return;
      }

      productStatus.textContent = "Søker...";
      productResults.innerHTML = "";

      try {
        const response = await fetch(`/api/products/search?query=${encodeURIComponent(query)}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Produktsøket feilet.");
        productStatus.textContent = `${payload.length} treff for "${query}"`;
        renderProducts(payload, productResults);
      } catch (error) {
        productStatus.textContent = error.message;
        productResults.innerHTML = "";
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

        if parsed.path == "/api/search":
            query = parse_qs(parsed.query).get("name", [""])[0]
            try:
                json_response(self, 200, search_people(query))
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/records":
            try:
                json_response(self, 200, list_people())
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/products/search":
            query = parse_qs(parsed.query).get("query", [""])[0]
            try:
                json_response(self, 200, search_products(query))
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return

        if parsed.path == "/api/products":
            try:
                json_response(self, 200, list_products())
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
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
