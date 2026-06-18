Attribute VB_Name = "DataEaseCsvToDbm"
Option Explicit

' Offline VBA converter matching generate_dataease_dbm.py in this project.
' Usage from Excel:
'   GenerateDataEaseFromCsv "C:\path\KUNDER.csv", "KUNDER", "C:\path\output"

Private Const FORMAT_VERSION As Integer = 6

Private Const FT_TEXT As Byte = &H1
Private Const FT_INTEGER As Byte = &H2
Private Const FT_NUMSTRING As Byte = &H3
Private Const FT_DATE_EXT As Byte = &H4
Private Const FT_DATE_STD As Byte = &H5
Private Const FT_FLOAT As Byte = &H6
Private Const FT_CURRENCY As Byte = &H7
Private Const FT_YESNO As Byte = &H8

Private Const FF_NONE As Byte = &H0

#If VBA7 Then
    Private Declare PtrSafe Sub CopyMemoryToBytes Lib "kernel32" Alias "RtlMoveMemory" (ByRef Destination As Any, ByRef Source As Any, ByVal Length As LongPtr)
#Else
    Private Declare Sub CopyMemoryToBytes Lib "kernel32" Alias "RtlMoveMemory" (ByRef Destination As Any, ByRef Source As Any, ByVal Length As Long)
#End If

Private Type FieldDef
    Name As String
    TypeCode As Byte
    Length As Integer
    Decimals As Byte
    Flags As Byte
End Type

Public Sub GenerateDataEaseFromCsv(ByVal csvPath As String, ByVal tableName As String, ByVal outputDir As String)
    Dim headers As Variant
    Dim rows As Collection
    Dim fields() As FieldDef
    Dim safeTable As String
    Dim dbmPath As String
    Dim tdfPath As String

    If Len(Dir(csvPath)) = 0 Then Err.Raise vbObjectError + 100, , "CSV-filen finnes ikke: " & csvPath
    If Len(outputDir) = 0 Then outputDir = ThisWorkbook.Path
    EnsureFolder outputDir

    Set rows = New Collection
    headers = ReadCsvFile(csvPath, rows)
    fields = InferFields(headers, rows)

    If Len(Trim$(tableName)) = 0 Then
        safeTable = NormalizeName(FileBaseName(csvPath), "TABLE", New Collection)
    Else
        safeTable = NormalizeName(tableName, "TABLE", New Collection)
    End If

    dbmPath = AddSlash(outputDir) & safeTable & ".DBM"
    tdfPath = AddSlash(outputDir) & safeTable & ".TDF"

    WriteDbm dbmPath, safeTable, fields, rows
    WriteTextFile tdfPath, BuildTdf(safeTable, fields, rows.Count)

    MsgBox "Konvertering fullfort:" & vbCrLf & dbmPath & vbCrLf & tdfPath, vbInformation
End Sub

Public Sub GenerateDataEaseFromSelectedCsv()
    Dim csvPath As Variant
    Dim tableName As String
    Dim outputDir As String

    csvPath = Application.GetOpenFilename("CSV-filer (*.csv),*.csv", , "Velg CSV-fil")
    If VarType(csvPath) = vbBoolean Then Exit Sub

    tableName = InputBox("Tabellnavn i DataEase/DBM:", "DataEase tabell", UCase$(FileBaseName(CStr(csvPath))))
    If Len(Trim$(tableName)) = 0 Then Exit Sub

    outputDir = InputBox("Output-mappe:", "Output", ThisWorkbook.Path)
    If Len(Trim$(outputDir)) = 0 Then Exit Sub

    GenerateDataEaseFromCsv CStr(csvPath), tableName, outputDir
End Sub

Private Function ReadCsvFile(ByVal csvPath As String, ByRef rows As Collection) As Variant
    Dim text As String
    Dim delimiter As String
    Dim lines As Variant
    Dim i As Long
    Dim headers As Variant
    Dim values As Variant

    text = ReadTextFile(csvPath, "utf-8")
    If Left$(text, 1) = ChrW$(&HFEFF) Then text = Mid$(text, 2)
    text = Replace(text, vbCrLf, vbLf)
    text = Replace(text, vbCr, vbLf)
    If Right$(text, 1) = vbLf Then text = Left$(text, Len(text) - 1)

    delimiter = GuessDelimiter(text)
    lines = Split(text, vbLf)
    If UBound(lines) < 0 Or Len(Trim$(CStr(lines(0)))) = 0 Then Err.Raise vbObjectError + 101, , "CSV-filen maa ha header-rad."

    headers = ParseCsvLine(CStr(lines(0)), delimiter)
    For i = LBound(lines) + 1 To UBound(lines)
        If Len(CStr(lines(i))) > 0 Then
            values = ParseCsvLine(CStr(lines(i)), delimiter)
            rows.Add PadRow(values, UBound(headers) - LBound(headers) + 1)
        End If
    Next i

    ReadCsvFile = headers
End Function

Private Function ParseCsvLine(ByVal line As String, ByVal delimiter As String) As Variant
    Dim result As Collection
    Dim current As String
    Dim i As Long
    Dim ch As String
    Dim inQuotes As Boolean
    Dim arr() As String

    Set result = New Collection
    For i = 1 To Len(line)
        ch = Mid$(line, i, 1)
        If ch = """" Then
            If inQuotes And i < Len(line) And Mid$(line, i + 1, 1) = """" Then
                current = current & """"
                i = i + 1
            Else
                inQuotes = Not inQuotes
            End If
        ElseIf ch = delimiter And Not inQuotes Then
            result.Add current
            current = vbNullString
        Else
            current = current & ch
        End If
    Next i
    result.Add current

    ReDim arr(0 To result.Count - 1)
    For i = 1 To result.Count
        arr(i - 1) = CStr(result(i))
    Next i
    ParseCsvLine = arr
End Function

Private Function PadRow(ByVal values As Variant, ByVal count As Long) As Variant
    Dim arr() As String
    Dim i As Long

    ReDim arr(0 To count - 1)
    For i = 0 To count - 1
        If i <= UBound(values) Then arr(i) = CStr(values(i)) Else arr(i) = vbNullString
    Next i
    PadRow = arr
End Function

Private Function InferFields(ByVal headers As Variant, ByVal rows As Collection) As FieldDef()
    Dim fields() As FieldDef
    Dim used As Collection
    Dim i As Long
    Dim values As Collection
    Dim row As Variant

    Set used = New Collection
    ReDim fields(LBound(headers) To UBound(headers))

    For i = LBound(headers) To UBound(headers)
        Set values = New Collection
        For Each row In rows
            values.Add CStr(row(i))
        Next row
        fields(i) = InferFieldDef(NormalizeName(CStr(headers(i)), "FIELD_" & CStr(i + 1), used), values)
    Next i

    InferFields = fields
End Function

Private Function InferFieldDef(ByVal fieldName As String, ByVal values As Collection) As FieldDef
    Dim f As FieldDef
    Dim present As Collection
    Dim v As Variant
    Dim s As String
    Dim maxLen As Long
    Dim maxDec As Long

    Set present = New Collection
    For Each v In values
        s = Trim$(CStr(v))
        If Len(s) > 0 Then present.Add s
    Next v

    f.Name = fieldName
    f.Flags = FF_NONE

    If present.Count = 0 Then
        f.TypeCode = FT_TEXT
        f.Length = 1
    ElseIf AllYesNo(present) Then
        f.TypeCode = FT_YESNO
        f.Length = 1
    ElseIf AllInteger(present) Then
        f.TypeCode = FT_INTEGER
        f.Length = 4
    ElseIf AllFloatOrInteger(present, maxDec) Then
        f.TypeCode = FT_FLOAT
        f.Length = 8
        If maxDec > 9 Then maxDec = 9
        f.Decimals = CByte(maxDec)
    ElseIf AllDateExt(present) Then
        f.TypeCode = FT_DATE_EXT
        f.Length = 10
    ElseIf AllDateStd(present) Then
        f.TypeCode = FT_DATE_STD
        f.Length = 8
    ElseIf AllNumString(present) Then
        f.TypeCode = FT_NUMSTRING
        maxLen = MaxTextLen(present)
        If maxLen > 255 Then maxLen = 255
        f.Length = CInt(maxLen)
    Else
        f.TypeCode = FT_TEXT
        maxLen = MaxTextLen(present)
        If maxLen > 255 Then maxLen = 255
        f.Length = CInt(maxLen)
    End If

    InferFieldDef = f
End Function

Private Sub WriteDbm(ByVal path As String, ByVal tableName As String, ByRef fields() As FieldDef, ByVal rows As Collection)
    Dim fileNo As Integer
    Dim nFields As Long
    Dim recordSize As Long
    Dim headerSize As Long
    Dim i As Long
    Dim row As Variant
    Dim buffer() As Byte

    nFields = UBound(fields) - LBound(fields) + 1
    recordSize = 1
    For i = LBound(fields) To UBound(fields)
        recordSize = recordSize + fields(i).Length
    Next i
    headerSize = 128 + nFields * 64 + 2

    fileNo = FreeFile
    Open path For Binary Access Write As #fileNo
    buffer = BuildHeader(tableName, nFields, rows.Count, headerSize, recordSize)
    PutBytes fileNo, buffer
    buffer = BuildFieldDescriptors(fields)
    PutBytes fileNo, buffer
    For Each row In rows
        PutByte fileNo, &H20
        For i = LBound(fields) To UBound(fields)
            buffer = EncodeField(fields(i), CStr(row(i)))
            PutBytes fileNo, buffer
        Next i
    Next row
    PutByte fileNo, &H1A
    Close #fileNo
End Sub

Private Function BuildHeader(ByVal tableName As String, ByVal nFields As Long, ByVal nRecords As Long, ByVal headerSize As Long, ByVal recordSize As Long) As Byte()
    Dim b(0 To 127) As Byte
    PutAscii b, 0, "DEFW", 4
    PutUInt16 b, 4, FORMAT_VERSION
    PutUInt16 b, 6, nFields
    PutUInt32 b, 8, nRecords
    PutUInt16 b, 12, headerSize
    PutUInt16 b, 14, recordSize
    PutAscii b, 16, tableName, 20
    BuildHeader = b
End Function

Private Function BuildFieldDescriptors(ByRef fields() As FieldDef) As Byte()
    Dim b() As Byte
    Dim offset As Long
    Dim i As Long

    ReDim b(0 To ((UBound(fields) - LBound(fields) + 1) * 64 + 2) - 1)
    For i = LBound(fields) To UBound(fields)
        PutAscii b, offset, fields(i).Name, 20
        b(offset + 20) = fields(i).TypeCode
        b(offset + 21) = fields(i).Flags
        PutUInt16 b, offset + 22, fields(i).Length
        b(offset + 24) = fields(i).Decimals
        offset = offset + 64
    Next i
    b(offset) = &HD
    b(offset + 1) = &HA
    BuildFieldDescriptors = b
End Function

Private Function EncodeField(ByRef f As FieldDef, ByVal value As String) As Byte()
    Dim yesNo(0 To 0) As Byte
    Dim emptyBytes() As Byte

    Select Case f.TypeCode
        Case FT_TEXT, FT_NUMSTRING
            EncodeField = FixedTextBytes(value, f.Length)
        Case FT_INTEGER
            EncodeField = Int32Bytes(CLng(Val(value)))
        Case FT_DATE_EXT
            EncodeField = FixedTextBytes(value, 10)
        Case FT_DATE_STD
            EncodeField = FixedTextBytes(value, 8)
        Case FT_FLOAT
            EncodeField = DoubleBytes(CDbl(NormalizeNumber(value)))
        Case FT_CURRENCY
            EncodeField = Int64Bytes(CDec(NormalizeNumber(value)) * 100)
        Case FT_YESNO
            yesNo(0) = IIf(IsYesValue(value), &H1, &H0)
            EncodeField = yesNo
        Case Else
            ReDim emptyBytes(0 To f.Length - 1)
            EncodeField = emptyBytes
    End Select
End Function

Private Function FixedTextBytes(ByVal value As String, ByVal length As Long) As Byte()
    Dim b() As Byte
    Dim ansi() As Byte
    Dim i As Long

    ReDim b(0 To length - 1)
    If Len(value) > 0 Then
        ansi = StrConv(value, vbFromUnicode)
        For i = 0 To length - 1
            If i <= UBound(ansi) Then b(i) = ansi(i)
        Next i
    End If
    FixedTextBytes = b
End Function

Private Function Int32Bytes(ByVal value As Long) As Byte()
    Dim b(0 To 3) As Byte
    Dim u As Double
    If value < 0 Then u = 4294967296# + value Else u = value
    b(0) = ByteAt(u, 1#)
    b(1) = ByteAt(u, 256#)
    b(2) = ByteAt(u, 65536#)
    b(3) = ByteAt(u, 16777216#)
    Int32Bytes = b
End Function

Private Function Int64Bytes(ByVal value As Variant) As Byte()
    Dim b(0 To 7) As Byte
    Dim cur As Currency
    Dim i As Long
    cur = CCur(value)
    For i = 0 To 7
        b(i) = ByteAt(CDbl(cur), 256# ^ i)
    Next i
    Int64Bytes = b
End Function

Private Function ByteAt(ByVal value As Double, ByVal place As Double) As Byte
    ByteAt = CByte(Fix(value / place) - Fix(value / (place * 256#)) * 256#)
End Function

Private Function DoubleBytes(ByVal value As Double) As Byte()
    Dim b(0 To 7) As Byte
    Dim tmp As Double
    Dim i As Long
    tmp = value
    CopyMemoryToBytes b(0), tmp, 8
    DoubleBytes = b
End Function

Private Sub PutUInt16(ByRef b() As Byte, ByVal offset As Long, ByVal value As Long)
    b(offset) = value And &HFF
    b(offset + 1) = (value \ 256) And &HFF
End Sub

Private Sub PutUInt32(ByRef b() As Byte, ByVal offset As Long, ByVal value As Long)
    b(offset) = value And &HFF
    b(offset + 1) = (value \ 256) And &HFF
    b(offset + 2) = (value \ 65536) And &HFF
    b(offset + 3) = (value \ 16777216) And &HFF
End Sub

Private Sub PutAscii(ByRef b() As Byte, ByVal offset As Long, ByVal value As String, ByVal length As Long)
    Dim bytes() As Byte
    Dim i As Long
    If Len(value) > 0 Then
        bytes = StrConv(value, vbFromUnicode)
        For i = 0 To length - 1
            If i <= UBound(bytes) Then b(offset + i) = bytes(i)
        Next i
    End If
End Sub

Private Sub PutBytes(ByVal fileNo As Integer, ByRef b() As Byte)
    Put #fileNo, , b
End Sub

Private Sub PutByte(ByVal fileNo As Integer, ByVal value As Byte)
    Put #fileNo, , value
End Sub

Private Function BuildTdf(ByVal tableName As String, ByRef fields() As FieldDef, ByVal nRecords As Long) As String
    Dim s As String
    Dim i As Long
    Dim recordSize As Long

    recordSize = 1
    For i = LBound(fields) To UBound(fields)
        recordSize = recordSize + fields(i).Length
    Next i

    s = "TABLE: " & tableName & vbCrLf
    s = s & "VERSION: " & FORMAT_VERSION & vbCrLf
    s = s & "FIELDS: " & (UBound(fields) - LBound(fields) + 1) & vbCrLf & vbCrLf
    s = s & "#    NAME                 TYPE           LEN  DEC FLAGS" & vbCrLf
    s = s & String$(60, "-") & vbCrLf
    For i = LBound(fields) To UBound(fields)
        s = s & Format$(i + 1, "0") & "    " & PadRight(fields(i).Name, 20) & " " & _
            PadRight(TypeNameFor(fields(i).TypeCode), 14) & " " & _
            PadLeft(CStr(fields(i).Length), 5) & " " & PadLeft(CStr(fields(i).Decimals), 4) & "  -" & vbCrLf
    Next i
    s = s & vbCrLf & "RECORD_SIZE:  " & recordSize & " bytes" & vbCrLf
    s = s & "HEADER_SIZE:  " & (128 + (UBound(fields) - LBound(fields) + 1) * 64 + 2) & " bytes" & vbCrLf
    s = s & "TOTAL_RECORDS: " & nRecords & vbCrLf
    BuildTdf = s
End Function

Private Function GuessDelimiter(ByVal text As String) As String
    Dim firstLine As String
    firstLine = Split(text, vbLf)(0)
    If CountChar(firstLine, ";") >= CountChar(firstLine, ",") And CountChar(firstLine, ";") >= CountChar(firstLine, vbTab) Then
        GuessDelimiter = ";"
    ElseIf CountChar(firstLine, vbTab) > CountChar(firstLine, ",") Then
        GuessDelimiter = vbTab
    Else
        GuessDelimiter = ","
    End If
End Function

Private Function NormalizeName(ByVal value As String, ByVal fallback As String, ByVal used As Collection) As String
    Dim i As Long
    Dim ch As String
    Dim name As String
    Dim candidate As String
    Dim suffix As Long

    value = UCase$(Trim$(value))
    For i = 1 To Len(value)
        ch = Mid$(value, i, 1)
        If ch Like "[A-Z0-9_]" Then name = name & ch Else name = name & "_"
    Next i
    Do While InStr(name, "__") > 0
        name = Replace(name, "__", "_")
    Loop
    Do While Left$(name, 1) = "_"
        name = Mid$(name, 2)
    Loop
    Do While Right$(name, 1) = "_"
        name = Left$(name, Len(name) - 1)
    Loop
    If Len(name) = 0 Then name = fallback
    name = Left$(name, 20)
    candidate = name

    suffix = 2
    Do While CollectionContains(used, candidate)
        candidate = Left$(name, 20 - Len("_" & suffix)) & "_" & suffix
        suffix = suffix + 1
    Loop
    used.Add candidate, candidate
    NormalizeName = candidate
End Function

Private Function AllYesNo(ByVal values As Collection) As Boolean
    Dim v As Variant
    For Each v In values
        If Not IsYesNoValue(CStr(v)) Then Exit Function
    Next v
    AllYesNo = True
End Function

Private Function AllInteger(ByVal values As Collection) As Boolean
    Dim v As Variant
    For Each v In values
        If Not CStr(v) Like "[+-]#*" And Not CStr(v) Like "#*" Then Exit Function
        If InStr(CStr(v), ".") > 0 Or InStr(CStr(v), ",") > 0 Then Exit Function
        If Not IsNumeric(CStr(v)) Then Exit Function
    Next v
    AllInteger = True
End Function

Private Function AllFloatOrInteger(ByVal values As Collection, ByRef maxDec As Long) As Boolean
    Dim v As Variant
    Dim s As String
    Dim p As Long
    maxDec = 0
    For Each v In values
        s = NormalizeNumber(CStr(v))
        If Not IsNumeric(s) Then Exit Function
        s = Replace(s, Application.International(xlDecimalSeparator), ".")
        p = InStr(s, ".")
        If p > 0 Then
            If Len(s) - p > maxDec Then maxDec = Len(s) - p
        End If
    Next v
    AllFloatOrInteger = maxDec > 0
End Function

Private Function NormalizeNumber(ByVal value As String) As String
    Dim decimalSeparator As String
    decimalSeparator = Application.International(xlDecimalSeparator)
    If decimalSeparator = "," Then
        NormalizeNumber = Replace(Trim$(value), ".", ",")
    Else
        NormalizeNumber = Replace(Trim$(value), ",", ".")
    End If
End Function

Private Function AllDateExt(ByVal values As Collection) As Boolean
    Dim v As Variant
    For Each v In values
        If Not CStr(v) Like "#/#/####" And Not CStr(v) Like "##/##/####" And Not CStr(v) Like "#/##/####" And Not CStr(v) Like "##/#/####" Then Exit Function
    Next v
    AllDateExt = True
End Function

Private Function AllDateStd(ByVal values As Collection) As Boolean
    Dim v As Variant
    For Each v In values
        If Not CStr(v) Like "#/#/##" And Not CStr(v) Like "##/##/##" And Not CStr(v) Like "#/##/##" And Not CStr(v) Like "##/#/##" Then Exit Function
    Next v
    AllDateStd = True
End Function

Private Function AllNumString(ByVal values As Collection) As Boolean
    Dim v As Variant
    Dim s As String
    Dim i As Long
    Dim ch As String
    Dim hasDigit As Boolean
    For Each v In values
        s = CStr(v)
        For i = 1 To Len(s)
            ch = Mid$(s, i, 1)
            If ch Like "#" Then hasDigit = True
            If InStr("+()- 0123456789", ch) = 0 Then Exit Function
        Next i
    Next v
    AllNumString = hasDigit
End Function

Private Function MaxTextLen(ByVal values As Collection) As Long
    Dim v As Variant
    Dim n As Long
    For Each v In values
        n = LenB(StrConv(CStr(v), vbFromUnicode))
        If n > MaxTextLen Then MaxTextLen = n
    Next v
    If MaxTextLen < 1 Then MaxTextLen = 1
End Function

Private Function IsYesNoValue(ByVal value As String) As Boolean
    Select Case LCase$(Trim$(value))
        Case "yes", "no", "true", "false", "ja", "nei", "1", "0"
            IsYesNoValue = True
    End Select
End Function

Private Function IsYesValue(ByVal value As String) As Boolean
    Select Case LCase$(Trim$(value))
        Case "yes", "true", "ja", "1"
            IsYesValue = True
    End Select
End Function

Private Function TypeNameFor(ByVal typeCode As Byte) As String
    Select Case typeCode
        Case FT_TEXT: TypeNameFor = "Text"
        Case FT_INTEGER: TypeNameFor = "Integer"
        Case FT_NUMSTRING: TypeNameFor = "NumString"
        Case FT_DATE_EXT: TypeNameFor = "Date(Ext)"
        Case FT_DATE_STD: TypeNameFor = "Date(Std)"
        Case FT_FLOAT: TypeNameFor = "Float"
        Case FT_CURRENCY: TypeNameFor = "Currency"
        Case FT_YESNO: TypeNameFor = "Yes/No"
        Case Else: TypeNameFor = "?"
    End Select
End Function

Private Function ReadTextFile(ByVal path As String, ByVal charsetName As String) As String
    On Error GoTo PlainFallback
    Dim stream As Object
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 2
    stream.Charset = charsetName
    stream.Open
    stream.LoadFromFile path
    ReadTextFile = stream.ReadText
    stream.Close
    Exit Function

PlainFallback:
    Dim fileNo As Integer
    On Error GoTo 0
    fileNo = FreeFile
    Open path For Input As #fileNo
    ReadTextFile = Input$(LOF(fileNo), #fileNo)
    Close #fileNo
End Function

Private Sub WriteTextFile(ByVal path As String, ByVal text As String)
    Dim fileNo As Integer
    fileNo = FreeFile
    Open path For Output As #fileNo
    Print #fileNo, text
    Close #fileNo
End Sub

Private Sub EnsureFolder(ByVal path As String)
    If Len(Dir(path, vbDirectory)) = 0 Then MkDir path
End Sub

Private Function AddSlash(ByVal path As String) As String
    If Right$(path, 1) = "\" Or Right$(path, 1) = "/" Then AddSlash = path Else AddSlash = path & "\"
End Function

Private Function FileBaseName(ByVal path As String) As String
    Dim name As String
    name = Mid$(path, InStrRev(path, "\") + 1)
    If InStrRev(name, ".") > 0 Then name = Left$(name, InStrRev(name, ".") - 1)
    FileBaseName = name
End Function

Private Function CountChar(ByVal text As String, ByVal ch As String) As Long
    CountChar = Len(text) - Len(Replace(text, ch, vbNullString))
End Function

Private Function CollectionContains(ByVal c As Collection, ByVal key As String) As Boolean
    On Error GoTo Missing
    Dim tmp As Variant
    tmp = c.Item(key)
    CollectionContains = True
    Exit Function
Missing:
    CollectionContains = False
End Function

Private Function PadRight(ByVal value As String, ByVal width As Long) As String
    PadRight = Left$(value & Space$(width), width)
End Function

Private Function PadLeft(ByVal value As String, ByVal width As Long) As String
    PadLeft = Right$(Space$(width) & value, width)
End Function
