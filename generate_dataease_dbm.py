"""
DataEase for Windows (.DBM) Sample File Generator
===================================================
Generates a realistic .DBM binary file modelled on the DataEase for Windows
6.x/7.x table-data format, based on reverse-engineering findings and the
official DataEase field-type documentation.

TABLE: CUSTOMERS
Fields:
  1. CUSTOMER_ID   – Numeric/Integer       (4 bytes, little-endian int32)
  2. FIRST_NAME    – Text                  (30 bytes, null-padded)
  3. LAST_NAME     – Text                  (30 bytes, null-padded)
  4. ADDRESS       – Text                  (50 bytes, null-padded)
  5. CITY          – Text                  (30 bytes, null-padded)
  6. PHONE         – Numeric String        (15 bytes, null-padded)
  7. EMAIL         – Text                  (50 bytes, null-padded)
  8. CREATED_DATE  – Date (Extended)       (10 bytes, "MM/DD/YYYY\0" ASCII)

FILE LAYOUT
-----------
Offset 0x0000  FILE HEADER       (128 bytes)
Offset 0x0080  FIELD DESCRIPTORS (N_FIELDS × 64 bytes each)
Offset 0x0080 + N_FIELDS*64 + 2  RECORD DATA (1 byte flag + fixed-width fields)

HEADER FIELDS (all little-endian unless noted)
  +0x00  4 bytes  Magic signature  "DEFW" (0x44 0x45 0x46 0x57)
  +0x04  2 bytes  Format version   0x0006 (v6) or 0x0007 (v7)  ← change here
  +0x06  2 bytes  Number of fields
  +0x08  4 bytes  Number of records
  +0x0C  2 bytes  Header size in bytes (= 128 + N_FIELDS*64 + 2 terminator)
  +0x0E  2 bytes  Record size in bytes (flag byte + sum of field widths)
  +0x10  20 bytes Table name (null-padded ASCII)
  +0x24  4 bytes  Reserved / flags (0x00000000)
  +0x28  88 bytes Reserved padding (zeros)

FIELD DESCRIPTOR (64 bytes each)
  +0x00  20 bytes Field name (null-padded ASCII, upper-case)
  +0x14  1 byte   Field type code:
                    0x01 = Text
                    0x02 = Numeric/Integer
                    0x03 = Numeric String
                    0x04 = Date (Extended 10-char)
                    0x05 = Date (Standard 8-char)
                    0x06 = Number (Float)
                    0x07 = Currency
                    0x08 = Yes/No (Boolean)
  +0x15  1 byte   Field flags   (0x00 = plain, 0x01 = required, 0x02 = indexed)
  +0x16  2 bytes  Field length in bytes
  +0x18  1 byte   Decimal places (for numeric types)
  +0x19  43 bytes Reserved padding (zeros)

RECORD
  +0x00  1 byte   Record status:  0x20 = active, 0x2A = deleted
  +0x01  N bytes  Field data, concatenated in field-descriptor order

FIELD ENCODING
  Text / Numeric String : fixed-length, right-padded with 0x00
  Numeric/Integer       : 4-byte little-endian signed int32
  Number (Float)        : 8-byte little-endian IEEE 754 double
  Date (Extended)       : 10-byte ASCII "MM/DD/YYYY", null-padded to 10
  Yes/No                : 1 byte, 0x01=Yes, 0x00=No

HOW TO ADAPT THIS FILE
-----------------------
1. Change FORMAT_VERSION to 0x0007 for v7 files.
2. Add/remove entries in FIELD_DEFS to match your real table schema.
3. Add/remove rows in RECORDS to get the sample size you need.
4. If your converter uses a different magic signature, change MAGIC.
5. If your converter uses space-padded (0x20) fields instead of null-padded,
   change the `encode_text` helper.

Run:
    python generate_dataease_dbm.py
Outputs:
    CUSTOMERS.DBM  – the binary data file
    CUSTOMERS.TDF  – companion plain-text schema descriptor (for reference)
"""

import struct
import os

# ── Configuration ─────────────────────────────────────────────────────────────

OUTPUT_DBM  = "CUSTOMERS.DBM"
OUTPUT_TDF  = "CUSTOMERS.TDF"
TABLE_NAME  = "CUSTOMERS"
MAGIC       = b"DEFW"          # DataEase For Windows signature
FORMAT_VERSION = 0x0006        # 0x0006 = v6,  0x0007 = v7

# Field type codes
FT_TEXT        = 0x01
FT_INTEGER     = 0x02
FT_NUMSTRING   = 0x03
FT_DATE_EXT    = 0x04   # Extended date: MM/DD/YYYY (10 chars)
FT_DATE_STD    = 0x05   # Standard date: MM/DD/YY  (8 chars)
FT_FLOAT       = 0x06
FT_CURRENCY    = 0x07
FT_YESNO       = 0x08

# Field flag bits
FF_NONE     = 0x00
FF_REQUIRED = 0x01
FF_INDEXED  = 0x02

# (name, type_code, length_bytes, decimals, flags)
FIELD_DEFS = [
    ("CUSTOMER_ID",  FT_INTEGER,    4,  0, FF_INDEXED),
    ("FIRST_NAME",   FT_TEXT,      30,  0, FF_NONE),
    ("LAST_NAME",    FT_TEXT,      30,  0, FF_NONE),
    ("ADDRESS",      FT_TEXT,      50,  0, FF_NONE),
    ("CITY",         FT_TEXT,      30,  0, FF_NONE),
    ("PHONE",        FT_NUMSTRING, 15,  0, FF_NONE),
    ("EMAIL",        FT_TEXT,      50,  0, FF_NONE),
    ("CREATED_DATE", FT_DATE_EXT,  10,  0, FF_NONE),
]

# Sample customer records  (id, first, last, address, city, phone, email, date)
RECORDS = [
    (1,  "Alice",   "Hansen",   "Strandgaten 12",     "Stavanger", "51234567",  "alice.hansen@example.no",   "03/14/2019"),
    (2,  "Bjorn",   "Olsen",    "Kirkegata 5B",       "Bergen",    "55987654",  "bjorn.olsen@example.no",    "07/22/2020"),
    (3,  "Camilla", "Berg",     "Osloveien 88",       "Oslo",      "22334455",  "camilla.berg@example.no",   "11/01/2021"),
    (4,  "Dag",     "Andersen", "Nedre Vollgate 3",   "Trondheim", "73456789",  "dag.andersen@example.no",   "02/09/2022"),
    (5,  "Eva",     "Nilsen",   "Havnegata 1",        "Kristiansand","38112233","eva.nilsen@example.no",     "05/30/2022"),
    (6,  "Fredrik", "Larsen",   "Kongens gate 7",     "Tromso",    "77654321",  "fredrik.larsen@example.no", "08/15/2023"),
    (7,  "Guro",    "Dahl",     "Parkveien 22",       "Drammen",   "32001122",  "guro.dahl@example.no",      "12/03/2023"),
    (8,  "Henrik",  "Eriksen",  "Torget 4",           "Fredrikstad","69112244", "henrik.eriksen@example.no", "01/17/2024"),
    (9,  "Ingrid",  "Christensen","Sognsveien 15",    "Lillehammer","61234321", "ingrid.c@example.no",       "03/28/2024"),
    (10, "Jonas",   "Pettersen","Bredgata 9",         "Bodo",      "75221133",  "jonas.p@example.no",        "06/06/2024"),
]

# ── Encoding helpers ───────────────────────────────────────────────────────────

def encode_text(value: str, length: int) -> bytes:
    """Fixed-width text field, null-padded (change to .ljust for space-padded)."""
    encoded = value.encode("latin-1", errors="replace")
    return encoded[:length].ljust(length, b"\x00")

def encode_integer(value: int) -> bytes:
    return struct.pack("<i", value)

def encode_numstring(value: str, length: int) -> bytes:
    return encode_text(value, length)

def encode_date_ext(value: str) -> bytes:
    """Encode MM/DD/YYYY as 10-byte ASCII, null-padded."""
    return encode_text(value, 10)

def encode_field(ftype: int, flen: int, value) -> bytes:
    if ftype == FT_TEXT:
        return encode_text(str(value), flen)
    elif ftype == FT_INTEGER:
        return encode_integer(int(value))
    elif ftype == FT_NUMSTRING:
        return encode_numstring(str(value), flen)
    elif ftype == FT_DATE_EXT:
        return encode_date_ext(str(value))
    elif ftype == FT_DATE_STD:
        return encode_text(str(value), 8)
    elif ftype == FT_FLOAT:
        return struct.pack("<d", float(value))
    elif ftype == FT_CURRENCY:
        return struct.pack("<q", int(float(value) * 100))  # cents as int64
    elif ftype == FT_YESNO:
        return b"\x01" if value else b"\x00"
    else:
        return b"\x00" * flen

# ── Build sections ─────────────────────────────────────────────────────────────

N_FIELDS      = len(FIELD_DEFS)
N_RECORDS     = len(RECORDS)
RECORD_DATA_SIZE = sum(f[2] for f in FIELD_DEFS)   # sum of field widths
RECORD_SIZE   = 1 + RECORD_DATA_SIZE                # 1 flag byte + fields
FIELD_DESC_BLOCK = N_FIELDS * 64
HEADER_SIZE   = 128 + FIELD_DESC_BLOCK + 2          # +2 for terminator word


def build_header() -> bytes:
    buf = bytearray(128)
    buf[0:4]   = MAGIC
    struct.pack_into("<H", buf, 4,  FORMAT_VERSION)
    struct.pack_into("<H", buf, 6,  N_FIELDS)
    struct.pack_into("<I", buf, 8,  N_RECORDS)
    struct.pack_into("<H", buf, 12, HEADER_SIZE)
    struct.pack_into("<H", buf, 14, RECORD_SIZE)
    tname = TABLE_NAME.encode("latin-1")[:20].ljust(20, b"\x00")
    buf[16:36] = tname
    # bytes 36-127 remain zero (reserved)
    return bytes(buf)


def build_field_descriptors() -> bytes:
    buf = bytearray()
    for (name, ftype, flen, decimals, flags) in FIELD_DEFS:
        fd = bytearray(64)
        fname = name.encode("latin-1")[:20].ljust(20, b"\x00")
        fd[0:20]  = fname
        fd[20]    = ftype
        fd[21]    = flags
        struct.pack_into("<H", fd, 22, flen)
        fd[24]    = decimals
        # fd[25:64] = zero padding
        buf.extend(fd)
    # Two-byte terminator after field descriptors
    buf.extend(b"\x0D\x0A")
    return bytes(buf)


def build_records() -> bytes:
    buf = bytearray()
    for row in RECORDS:
        rec = bytearray()
        rec.append(0x20)   # 0x20 = active record
        for i, (name, ftype, flen, decimals, flags) in enumerate(FIELD_DEFS):
            rec.extend(encode_field(ftype, flen, row[i]))
        buf.extend(rec)
    # EOF marker
    buf.append(0x1A)
    return bytes(buf)


def build_tdf() -> str:
    """Human-readable companion schema file (plain text, not binary)."""
    lines = [
        f"TABLE: {TABLE_NAME}",
        f"VERSION: {FORMAT_VERSION}",
        f"FIELDS: {N_FIELDS}",
        "",
        f"{'#':<4} {'NAME':<20} {'TYPE':<14} {'LEN':>5} {'DEC':>4} {'FLAGS'}",
        "-" * 60,
    ]
    type_names = {
        FT_TEXT: "Text", FT_INTEGER: "Integer", FT_NUMSTRING: "NumString",
        FT_DATE_EXT: "Date(Ext)", FT_DATE_STD: "Date(Std)",
        FT_FLOAT: "Float", FT_CURRENCY: "Currency", FT_YESNO: "Yes/No",
    }
    flag_names = {FF_NONE: "-", FF_REQUIRED: "REQUIRED", FF_INDEXED: "INDEXED"}
    for i, (name, ftype, flen, decimals, flags) in enumerate(FIELD_DEFS, 1):
        lines.append(
            f"{i:<4} {name:<20} {type_names.get(ftype,'?'):<14} "
            f"{flen:>5} {decimals:>4}  {flag_names.get(flags, str(flags))}"
        )
    lines += [
        "",
        f"RECORD_SIZE:  {RECORD_SIZE} bytes  (1 flag + {RECORD_DATA_SIZE} data)",
        f"HEADER_SIZE:  {HEADER_SIZE} bytes",
        f"TOTAL_RECORDS: {N_RECORDS}",
        "",
        "NOTES:",
        "  - Text fields: fixed-width, null-padded (0x00)",
        "  - Integer: 4-byte little-endian signed int32",
        "  - Date(Ext): 10-byte ASCII MM/DD/YYYY",
        "  - Record flag: 0x20=active, 0x2A=deleted",
        "  - File ends with EOF marker 0x1A",
        "  - Memo fields stored separately (not in .DBM)",
    ]
    return "\n".join(lines)


# ── Write files ────────────────────────────────────────────────────────────────

def generate():
    dbm_path = os.path.join("/mnt/user-data/outputs", OUTPUT_DBM)
    tdf_path = os.path.join("/mnt/user-data/outputs", OUTPUT_TDF)

    header      = build_header()
    field_descs = build_field_descriptors()
    records     = build_records()

    with open(dbm_path, "wb") as f:
        f.write(header)
        f.write(field_descs)
        f.write(records)

    with open(tdf_path, "w", encoding="utf-8") as f:
        f.write(build_tdf())

    total = len(header) + len(field_descs) + len(records)
    print(f"Written: {dbm_path}  ({total} bytes)")
    print(f"Written: {tdf_path}")
    print()
    print(f"  Header block   : {len(header)} bytes  (offset 0x0000)")
    print(f"  Field descs    : {len(field_descs)} bytes  (offset 0x{len(header):04X})")
    print(f"  Record data    : {len(records)} bytes  (offset 0x{len(header)+len(field_descs):04X})")
    print(f"  Fields         : {N_FIELDS}")
    print(f"  Records        : {N_RECORDS}  ({RECORD_SIZE} bytes each)")


if __name__ == "__main__":
    generate()
