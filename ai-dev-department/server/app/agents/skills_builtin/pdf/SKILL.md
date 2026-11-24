---
name: pdf
description: Create PDF documents (reports, invoices, certificates) or read, merge, split and extract text from existing PDFs. Use whenever the result is a PDF or a PDF is attached.
---

# PDF

Installed: **reportlab** (create), **pypdf** (read, merge, split, rotate).

## Create
```python
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Cyrillic needs a TTF font: find one first, e.g. with `fc-list | grep -i dejavu`
pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
styles = getSampleStyleSheet()
for name in ("Normal", "Title", "Heading2"):
    styles[name].fontName = "DejaVu"
doc = SimpleDocTemplate("out/report.pdf", pagesize=A4)
table = Table([["Позиция", "Сумма"], ["Дизайн", "50 000 ₽"]])
table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "DejaVu"), ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#305496")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]))
doc.build([Paragraph("Отчёт за ноябрь", styles["Title"]), Spacer(1, 12), table])
```
Another way: write HTML and convert with `soffice --headless --convert-to pdf` when LibreOffice is installed, or build a .docx (docx skill) and convert it.

## Read and change
```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("in.pdf"); text = "\n".join(page.extract_text() or "" for page in reader.pages)
writer = PdfWriter()
for page in reader.pages[:3]:
    writer.add_page(page)
writer.write("out/first-3-pages.pdf")
```
Scanned PDFs have no text layer: say so instead of guessing the content.

## Deliver
Check the page count with pypdf, then **SendFile** the PDF - it is shown right in the chat.
