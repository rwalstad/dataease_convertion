# How It Works: lesing av DataEase-tabeller

Denne filen beskriver hvordan `dataeaseconvert.py` tolker DataEase-lignende `.DBM`-filer for aa hente ut data fra tabellene som brukes i appen.

## Hovedfiler

- `dataeaseconvert.py` inneholder selve leseren i funksjonen `read_dataease_records()`.
- Den generiske filtolkeren bruker opplastede `.DBM`-filer i stedet for faste person- og produkttabeller i menyen.
- Eldre `.DBM`-filer uten `DEFW`-header tolkes ved hjelp av en `.DBA`-definisjonsfil med samme filnavn.
- `CUSTOMERS.TDF` og `PRODUKTER.TDF` er eksempler paa menneskelesbare beskrivelser av tabellstrukturer.

## Filformatet som forventes

Parseren forventer at `.DBM`-filen starter med en fast header paa minst 128 bytes.

De viktigste header-feltene er:

| Offset | Lengde | Betydning |
| --- | ---: | --- |
| `0x00` | 4 bytes | Signatur. Maa vaere `DEFW`. |
| `0x06` | 2 bytes | Antall felt i tabellen. |
| `0x08` | 4 bytes | Antall records/rader. |
| `0x0C` | 2 bytes | Header-storrelse, altsaa hvor record-data starter. |
| `0x0E` | 2 bytes | Storrelse paa hver record/rad. |

Tallene leses som little-endian med Python-modulen `struct`.

Hvis filen starter med `DEFW`, brukes denne headeren direkte. Hvis signaturen mangler, forventer parseren en sidefil med tabell-layout. For gamle DataEase-tabeller er dette normalt `.DBA`, ikke `.TDF`.

## Eldre DBM uten DEFW

For DOS-/eldre DataEase-filer ligger feltnavn og record-layout ikke i `.DBM`-filen. Da bruker appen en definisjonsfil med samme basenavn:

```text
KUNDER.DBM
KUNDER.DBA
```

Ved mappevalg finner UI-et automatisk `.DBA` i samme mappe og sender den med til API-et. Ved enkeltfilvalg kan DBM og DBA velges samtidig i samme fil-dialog; da matcher UI-et `.DBA` med samme basenavn automatisk. Nettlesere tillater ikke at appen leser en søskenfil fra disken etter at bare én enkelt DBM-fil er valgt, så enten mappevalg eller samtidig valg av DBM + DBA maa brukes.

DBA-parseren gjør dette:

1. Leser 16-byte feltbeskrivelser fra offset `0x28`.
2. Leser typekode, visningslengde, lagringslengde, desimaler og offset i DBM-recorden.
3. Finner null-separerte feltnavn senere i DBA-filen.
4. Beregner record-storrelse fra felt-offsetene og DBM-filstorrelsen.
5. Leser hver record etter offset/lengde fra DBA-layouten.

For DataEase DOS-layouten som ble validert mot KUNDOAAB, prøver parseren først den samme konservative layouten som den tidligere statiske løsningen brukte: 253 feltbeskrivelser fra offset `0x28`, navneliste fra offset `4861`, og 1000 bytes per record. Dette brukes bare når DBM-storrelsen passer med 1000-byte records og DBA-filen har forventet navneliste. Hvis dette ikke passer, faller parseren tilbake til generisk DBA-heuristikk.

Tekst i gamle DOS-/DataEase-filer dekodes med `cp865`. Det er en gammel DOS-tegnkode som ble brukt på norske og danske systemer. Dette gjør at byteverdier fra de gamle filene blir vist som riktige norske tegn, for eksempel `æ`, `ø`, `å`, `Æ`, `Ø` og `Å`. Hvis samme bytes tolkes som UTF-8 eller latin-1, kan norske tegn bli feil eller vises som rare symboler.

Appen kan også lese en tidligere reversert `.TDF` med `RECORD_SIZE` og feltlinjer i formatet som `reverse_kundoaab_layout.py` laget. Dette er kun en kompatibilitetsvei; de originale 2003-tabellene forventes aa bruke `.DBA`.

## Feltbeskrivelser

Etter de forste 128 bytes ligger feltbeskrivelsene. Hvert felt har en descriptor paa 64 bytes.

For hvert felt leser `dataeaseconvert.py`:

| Byte i descriptor | Lengde | Betydning |
| --- | ---: | --- |
| `0..19` | 20 bytes | Feltnavn, null-padded latin-1 tekst. |
| `20` | 1 byte | Typekode. |
| `21` | 1 byte | Feltflagg, for eksempel indexed/required. |
| `22..23` | 2 bytes | Feltlengde i bytes. |
| `24` | 1 byte | Antall desimaler. |

Dette lagres i en `Field` dataclass med:

```python
Field(name, type_code, length, decimals, flags)
```

## Record-lesing

Naar alle feltbeskrivelsene er lest, gaar parseren gjennom radene.

Startposisjonen for rad nummer `index` beregnes slik:

```python
record_offset = header_size + index * record_size
```

Hver record starter med 1 statusbyte:

| Verdi | Betydning |
| --- | --- |
| `0x20` | Aktiv record. |
| `0x2A` | Slettet record. Hoppes over. |

Etter statusbyten kommer feltdataene i samme rekkefolge som feltbeskrivelsene.

Parseren starter derfor med:

```python
cursor = 1
```

For hvert felt henter den ut nøyaktig `field.length` bytes:

```python
raw = record[cursor : cursor + field.length]
row[field.name] = _decode_field(field, raw)
cursor += field.length
```

Resultatet er en Python-dict per rad, der nøkler er feltnavnene fra DataEase-tabellen.

Eksempel:

```python
{
    "PRODUKT_ID": 4,
    "NAVN": "Arbeidstid",
    "VARENUMMER": "Arb1",
    "ENHETSPRIS": 1153.0,
    "KOSTPRIS": 0.0,
    "MVA_TYPE": "Hoy",
    "INNTEKTSKONTO": "3020",
}
```

## Datatyper

Dekodingen skjer i `_decode_field()`.

| Typekode | Tolkning i `dataeaseconvert.py` |
| ---: | --- |
| `0x01` | Tekst. Null-padded latin-1, trimmes. |
| `0x02` | Integer. 4-byte little-endian signed int. |
| `0x03` | Numeric string. Leses som tekst. |
| `0x04` | Date extended. Leses som tekst, typisk `MM/DD/YYYY`. |
| `0x05` | Date standard. Leses som tekst. |
| `0x06` | Float. 8-byte little-endian IEEE 754 double. |
| `0x07` | Currency. 8-byte little-endian integer delt paa 100. |
| `0x08` | Yes/No. Tolkes som boolsk verdi. |

Tekst dekodes slik:

```python
raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
```

Det betyr:

- Les bare frem til forste nullbyte.
- Bruk latin-1, siden norske tegn kan ligge direkte i byteverdiene.
- Fjern whitespace rundt teksten.

## Generisk filsok

`read_uploaded_database(file_item, schema_file_item)` lagrer den opplastede DBM-filen og eventuell DBA/TDF-fil midlertidig, sender filstiene til `read_dataease_records()`, og sletter tempfilene etter tolking.

`search_uploaded_database(file_item, query)` normaliserer søkeordet med `_normalize()` og søker i alle verdier i hver rad.

Resultatet inneholder:

- filnavn
- feltnavn
- antall records
- antall treff
- inntil 100 viste treff

`list_uploaded_database(file_item)` returnerer alle records og feltnavn slik at UI-et kan vise tabellen og lage CSV-nedlasting.

## API-endepunkter

`Handler` eksponerer dataene slik:

| URL | Funksjon |
| --- | --- |
| `/api/generic-file/search` | Søker i en opplastet `.DBM`-fil. |
| `/api/generic-file/list` | Returnerer alle records fra en opplastet `.DBM`-fil. |

| `/api/convert-csv` | Konverterer CSV til DataEase DBM/TDF. |

Alle API-svar returneres som JSON via `json_response()`.

## Viktige begrensninger

- DBM uten `DEFW` maa ha en tilhørende `.DBA` eller reversert `.TDF`.
- Feltbeskrivelser maa ligge rett etter 128-byte headeren.
- Hver field descriptor maa vaere 64 bytes.
- Record-data maa ligge fra `header_size`.
- Feltdata maa ligge i samme rekkefolge som descriptorene.
- Slettede records med statusbyte `0x2A` hoppes over.
- Ukjente typekoder returneres som hex-streng.

## Kort flyt

1. Les hele `.DBM`-filen som bytes.
2. Hvis `DEFW` finnes: les `field_count`, `record_count`, `header_size` og `record_size`.
3. Hvis `DEFW` mangler: les layout fra tilhørende `.DBA` eller reversert `.TDF`.
4. Gaa gjennom hver record.
5. Del recorden opp etter feltlengde og offset.
6. Decode hvert felt basert paa typekode.
7. Returner en liste med dictionaries.
8. Returner radene som generiske dictionaries for UI/API.

## Filtolkerens oppbygning og virkemåte - kode-ekstrakt

Koden under er selve filtolkeren trukket ut fra appen. Den inneholder ikke web-UI,
opplasting, API-endepunkter eller CSV-konvertering. Kunden kan bruke denne delen som
en ren decoder i egen dataflyt:

```python
from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path


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


def _decode_fixed_text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


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


def read_dataease_records(path: Path, schema_path: Path | None = None) -> list[dict]:
    """Read a DataEase DBM file and return one dictionary per active record.

    `schema_path` is required for old DOS DBM files without the DEFW signature.
    The schema file is normally a matching .DBA file. A reversed .TDF file is
    also supported for compatibility with the reverse-engineering helper.
    """
    data = path.read_bytes()
    if len(data) < 128:
        if schema_path:
            return _read_layout_records(path, schema_path)
        raise ValueError("DBM-filen er for kort til å inneholde en gyldig header.")

    if data[:4] != b"DEFW":
        if schema_path:
            return _read_layout_records(path, schema_path)
        raise ValueError("DBM-filen mangler DEFW-signatur. Send med tilhørende DBA/TDF.")

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
```

Bruk i kundens dataflyt kan holdes så enkelt:

```python
from pathlib import Path

records = read_dataease_records(
    Path("KUNDER.DBM"),
    Path("KUNDER.DBA"),  # brukes for gamle DOS-filer uten DEFW-header
)

for row in records:
    print(row)
```

Returverdien er en liste med dictionaries. Hver dictionary representerer én rad i
DBM-filen, og nøklene er feltnavnene som er lest fra `DEFW`-headeren eller fra
tilhørende `.DBA`/`.TDF`.
