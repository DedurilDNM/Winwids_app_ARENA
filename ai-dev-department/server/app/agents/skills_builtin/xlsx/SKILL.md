---
name: xlsx
description: Create, fill or analyse Excel spreadsheets (.xlsx, .csv) - tables, formulas, totals, formatting, charts, several sheets. Use whenever the result is a spreadsheet or an Excel/CSV file is attached.
---

# Excel spreadsheets (.xlsx)

Use **openpyxl** (installed) from a Python script run with Bash. For big data analysis pandas may be missing: check with `python3 -c "import pandas"` and fall back to csv + openpyxl.

## Build a sheet
```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, Reference

wb = Workbook(); ws = wb.active; ws.title = "Продажи"
ws.append(["Месяц", "Выручка", "Расходы", "Прибыль"])
for row in data:                      # data: list of [month, revenue, costs]
    ws.append(row)
last = ws.max_row
for r in range(2, last + 1):
    ws[f"D{r}"] = f"=B{r}-C{r}"       # formulas, not computed numbers
ws[f"A{last + 1}"] = "Итого"
for col in "BCD":
    ws[f"{col}{last + 1}"] = f"=SUM({col}2:{col}{last})"
head = Font(bold=True, color="FFFFFF"); fill = PatternFill("solid", fgColor="305496")
for c in ws[1]:
    c.font, c.fill, c.alignment = head, fill, Alignment(horizontal="center")
for col in "BCD":
    for c in ws[col][1:]:
        c.number_format = '#,##0 "₽"'
ws.freeze_panes = "A2"
for i, width in enumerate([14, 14, 14, 14], 1):
    ws.column_dimensions[get_column_letter(i)].width = width
chart = BarChart(); chart.title = "Выручка по месяцам"
chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=last), titles_from_data=True)
chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last))
ws.add_chart(chart, "F2")
wb.save("out/report.xlsx")
```

## Reading an attached file
`load_workbook(path, data_only=True)` gives the last computed values (formulas as text with `data_only=False`). Print the sheet names, the header row and a few rows first, then work.

## Quality
- Keep formulas live (`=SUM(...)`), so the user can change numbers; never paste computed totals.
- One table per sheet, a header row, frozen header, number formats, sensible widths.
- Check: re-open with `load_workbook` and print the first rows; for formulas, also compute the expected totals in Python and compare.
- Finish with **SendFile** on the .xlsx - the chat shows it as a table.
