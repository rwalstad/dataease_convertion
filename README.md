# dataease_convertion
read howitwork.md

[DataEaseCsvToDbm.bas](/home/waldo/dataease_convertion/DataEaseCsvToDbm.bas)
Excelmakro: leser en CSV-fil, finner feltnavn og felttyper, og skriver en DataEase-lignende .DBM + en .TDF beskrivelsesfil. Den kan kjøres helt offline på en PC med Excel.
Bruk i Excel:
Åpne Excel-filen din.
Trykk Alt + F11.
Velg File > Import File....
Importer DataEaseCsvToDbm.bas.
Kjør makroen:

Viktig presisering: 
Excel-appen  lager en ny .DBM-fil fra CSV. Den åpner ikke en eksisterende DataEase-tabell og patcher bare enkelte rader. Så praktisk bruk blir normalt:
Excel-tabell -> CSV -> ny KUNDER.DBM -> kopieres inn der DataEase forventer filen

Ta backup av eksisterende .DBM først, og sørg for at DataEase er lukket før filen byttes.