from __future__ import annotations

import json
import struct
import unicodedata
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "CUSTOMERS.DBM"
HOST = "127.0.0.1"
PORT = 8000


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


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict | list) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


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
    .brand {
      margin: 0 0 24px;
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: 0;
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
    .search button {
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
    .search button:hover { background: var(--accent-strong); }
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
      .brand { margin-bottom: 12px; }
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
      <p class="brand">DataEase</p>
      <nav class="menu" aria-label="Hovedmeny">
        <button class="menu-button active" type="button" data-view="search-view">Søk</button>
        <button class="menu-button" type="button" data-view="list-view">Liste</button>
      </nav>
    </aside>
    <main>
      <div class="data-source">Current data is from file: CUSTOMERS.DBM</div>
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
    let listLoaded = false;

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

    menuButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const viewId = button.dataset.view;
        menuButtons.forEach((item) => item.classList.toggle("active", item === button));
        views.forEach((view) => view.classList.toggle("active", view.id === viewId));
        if (viewId === "list-view") loadList();
        if (viewId === "search-view") input.focus();
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
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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

        json_response(self, 404, {"error": "Ikke funnet"})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Åpne http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
