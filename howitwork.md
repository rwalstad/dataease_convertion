# How It Works: lesing av DataEase-tabeller

Denne filen beskriver hvordan `app.py` tolker DataEase-lignende `.DBM`-filer for aa hente ut data fra tabellene som brukes i appen.

## Hovedfiler

- `app.py` inneholder selve leseren i funksjonen `read_dataease_records()`.
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
