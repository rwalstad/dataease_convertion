# How It Works: lesing av DataEase-tabeller

Denne filen beskriver hvordan `app.py` tolker DataEase-lignende `.DBM`-filer for aa hente ut data fra tabellene som brukes i appen.

## Hovedfiler

- `CUSTOMERS.DBM` leses som person-/kundetabell.
- `PRODUKTER.DBM` leses som produkttabell.
- `CUSTOMERS.TDF` og `PRODUKTER.TDF` er menneskelesbare beskrivelser av tabellstrukturene.
- `app.py` inneholder selve leseren i funksjonen `read_dataease_records()`.

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

Hvis filen er kortere enn 128 bytes, eller ikke starter med signaturen `DEFW`, stopper parseren med en feilmelding.

## Feltbeskrivelser

Etter de forste 128 bytes ligger feltbeskrivelsene. Hvert felt har en descriptor paa 64 bytes.

For hvert felt leser `app.py`:

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

| Typekode | Tolkning i `app.py` |
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

## Persondata

Persondata bruker standardfilen:

```python
DATA_FILE = BASE_DIR / "CUSTOMERS.DBM"
```

`list_people()` leser alle records fra `CUSTOMERS.DBM` og mapper DataEase-feltene til JSON-felt for UI-et:

| DataEase-felt | JSON/UI-felt |
| --- | --- |
| `CUSTOMER_ID` | `id` |
| `FIRST_NAME` + `LAST_NAME` | `name` |
| `ADDRESS` | `address` |
| `CITY` | `city` |
| `PHONE` | `phone` |
| `EMAIL` | `email` |
| `CREATED_DATE` | `createdDate` |

`search_people(query)` lager et søkbart fullt navn av `FIRST_NAME` og `LAST_NAME`.

Søk normaliseres med `_normalize()`:

- Unicode normaliseres.
- Aksenter/diakritiske tegn fjernes.
- Tekst casefoldes.
- Ekstra mellomrom fjernes.

Derfor kan søk fungere mer tolerant paa navn.

## Produktdata

Produktdata bruker:

```python
PRODUCT_FILE = BASE_DIR / "PRODUKTER.DBM"
```

`list_products()` leser alle records fra `PRODUKTER.DBM`.

Hver rad sendes gjennom `_product_payload()`, som mapper feltene slik:

| DataEase-felt | JSON/UI-felt |
| --- | --- |
| `PRODUKT_ID` | `id` |
| `NAVN` | `name` |
| `VARENUMMER` | `itemNumber` |
| `ENHETSPRIS` | `unitPrice` og `unitPriceDisplay` |
| `KOSTPRIS` | `costPrice` og `costPriceDisplay` |
| `MVA_TYPE` | `vatType` |
| `INNTEKTSKONTO` | `incomeAccount` |

Priser formatteres i `_format_price()` til to desimaler for visning.

`search_products(query)` søker i:

- `NAVN`
- `VARENUMMER`
- `MVA_TYPE`
- `INNTEKTSKONTO`

Søket bruker samme `_normalize()`-logikk som personsøket.

## API-endepunkter

`Handler.do_GET()` eksponerer dataene slik:

| URL | Funksjon |
| --- | --- |
| `/api/records` | Returnerer alle personer fra `CUSTOMERS.DBM`. |
| `/api/search?name=...` | Søker i personnavn. |
| `/api/products` | Returnerer alle produkter fra `PRODUKTER.DBM`. |
| `/api/products/search?query=...` | Søker i produktfeltene. |

Alle API-svar returneres som JSON via `json_response()`.

## Viktige begrensninger

- Parseren forutsetter signaturen `DEFW`.
- Feltbeskrivelser maa ligge rett etter 128-byte headeren.
- Hver field descriptor maa vaere 64 bytes.
- Record-data maa ligge fra `header_size`.
- Feltdata maa ligge i samme rekkefolge som descriptorene.
- Slettede records med statusbyte `0x2A` hoppes over.
- Ukjente typekoder returneres som hex-streng.

## Kort flyt

1. Les hele `.DBM`-filen som bytes.
2. Valider `DEFW`-signatur.
3. Les `field_count`, `record_count`, `header_size` og `record_size`.
4. Les `field_count` feltbeskrivelser, 64 bytes per felt.
5. Gaa gjennom hver record fra `header_size`.
6. Hopp over slettede records.
7. Del recorden opp etter feltlengdene.
8. Decode hvert felt basert paa typekode.
9. Returner en liste med dictionaries.
10. Mapper radene til person- eller produktformat for UI/API.
