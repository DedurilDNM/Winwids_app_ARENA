---
name: docx
description: Create or edit Word documents (.docx) - contracts, reports, letters, specifications - with headings, tables, lists, images, headers/footers. Use whenever the result should be a Word file or a .docx is attached.
---

# Word documents (.docx)

Work in Python with **python-docx** (installed). Write a script file (e.g. `make_doc.py`) and run it with Bash, so it can be fixed and re-run.

## New document
```python
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
style = doc.styles["Normal"]; style.font.name = "Calibri"; style.font.size = Pt(11)
for s in doc.sections:
    s.left_margin = s.right_margin = Cm(2.5); s.top_margin = s.bottom_margin = Cm(2)

doc.add_heading("Договор подряда № 12", level=1)
p = doc.add_paragraph("г. Москва"); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
doc.add_paragraph("Первый пункт", style="List Number")
table = doc.add_table(rows=1, cols=3); table.style = "Light Grid Accent 1"
for cell, text in zip(table.rows[0].cells, ["Работа", "Срок", "Цена"]):
    cell.text = text
row = table.add_row().cells; row[0].text = "Дизайн"; row[1].text = "5 дней"; row[2].text = "50 000 ₽"
doc.sections[0].footer.paragraphs[0].text = "Страница из договора"
doc.add_picture("chart.png", width=Cm(15))  # only if the image exists
doc.save("out/dogovor.docx")
```

## Editing an existing file
- `Document("in.docx")`; walk `doc.paragraphs` and `doc.tables`. Replace text run by run (`for r in p.runs`) to keep formatting; a placeholder split over several runs must be joined first.
- Keep the original: save to a new name unless asked to overwrite.

## Quality
- Real structure: headings (Heading 1-3), not bold paragraphs; lists as list styles; tables for tabular data.
- Russian text: keep «ёлочки», non-breaking spaces before units (`50 000 ₽`).
- Check: re-open the file with python-docx and print the headings / paragraph count. If `soffice` exists, `soffice --headless --convert-to pdf out/x.docx --outdir out` shows that it opens.
- Finish with **SendFile** on the .docx (and the PDF if you made one).
