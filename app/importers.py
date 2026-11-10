"""Read spreadsheet-ish files into a plain 2D list of cells for the UI's column mapper."""
import csv
import io
import os

MAX_ROWS = 20000


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float)):
        return v
    return str(v).strip()


def read_table(path, display_name=None):
    """Return dict(sheets=[{name, rows}]) for xlsx/xlsm/csv/tsv/txt."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)  # data_only -> formula results
        sheets = []
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                row = [_clean(c) for c in r]
                while row and row[-1] == "":
                    row.pop()
                if any(c != "" for c in row):
                    rows.append(row)
                if len(rows) >= MAX_ROWS:
                    break
            sheets.append(dict(name=ws.title, rows=rows))
        wb.close()
        return dict(sheets=sheets)
    if ext == ".xls":
        try:
            import pandas as pd
            book = pd.read_excel(path, sheet_name=None, header=None)
        except Exception as e:
            raise ValueError("Old .xls files need 'xlrd' installed. Re-save as .xlsx or CSV. " + str(e))
        return dict(sheets=[dict(name=n, rows=[[_clean(c) for c in r] for r in df.fillna("").values.tolist()][:MAX_ROWS])
                            for n, df in book.items()])
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    return dict(sheets=[dict(name=display_name or os.path.basename(path), rows=parse_text(text))])


def parse_text(text):
    sample = text[:4000]
    delim = "\t" if "\t" in sample else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;| ").delimiter
        except csv.Error:
            delim = ","
    rows = []
    for r in csv.reader(io.StringIO(text), delimiter=delim):
        r = [c.strip() for c in r if c.strip() != ""] if delim == " " else [c.strip() for c in r]
        if any(r):
            rows.append(r)
        if len(rows) >= MAX_ROWS:
            break
    return rows
