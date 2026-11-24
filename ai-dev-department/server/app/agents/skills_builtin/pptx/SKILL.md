---
name: pptx
description: Create PowerPoint presentations (.pptx) - pitch decks, reports, lesson slides - with titles, bullet points, images, tables and charts in a consistent design. Use whenever the result is a presentation.
---

# Presentations (.pptx)

Use **python-pptx** (installed) from a Python script run with Bash.

## Plan first
Write the outline before code: one message per slide, 3-5 short points, 8-15 slides for a deck. A title slide, an agenda, content, a summary / call to action.

## Build
```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

prs = Presentation(); prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)  # 16:9
title = prs.slides.add_slide(prs.slide_layouts[0])
title.shapes.title.text = "Студия 3D: план на 2027"
title.placeholders[1].text = "Инвесторам · ноябрь 2026"

s = prs.slides.add_slide(prs.slide_layouts[1])
s.shapes.title.text = "Почему сейчас"
body = s.placeholders[1].text_frame
body.text = "Рынок растёт на 30% в год"
for line in ["Спрос со стороны игр и рекламы", "Свой конвейер: 3x быстрее"]:
    p = body.add_paragraph(); p.text = line; p.level = 0

c = prs.slides.add_slide(prs.slide_layouts[5]); c.shapes.title.text = "Выручка"
data = CategoryChartData(); data.categories = ["2024", "2025", "2026"]; data.add_series("млн ₽", (12, 19, 31))
c.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.6), Inches(11), Inches(5.2), data)
prs.save("out/deck.pptx")
```

## Design
- One idea per slide; at most ~6 lines of text; font sizes 28+ for titles, 18+ for text.
- One accent colour and one font across the deck; images with `add_picture` sized to the slide.
- Speaker notes for the details: `slide.notes_slide.notes_text_frame.text = "..."`.

## Check and deliver
Re-open with `Presentation(path)` and print every slide's title. If `soffice` exists, convert to PDF (`soffice --headless --convert-to pdf`) - the user can look at it in the chat. Finish with **SendFile** on the .pptx (and the PDF).
