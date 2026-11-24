---
name: html-prototype
description: Build an interactive prototype or a small web page as ONE self-contained HTML file and show it live in the chat - landing pages, app screens, dashboards, mock-ups to look at and click. Use when the user wants to see a design or a prototype.
---

# HTML prototypes

The chat shows an HTML file live (in a sandbox) when you send it with **SendFile**. Make it worth looking at.

## Rules of the format
- ONE file: CSS in `<style>`, JS in `<script>`. Other files next to it are not loaded in the chat preview.
- External code only from a CDN with an exact version (e.g. `https://cdn.jsdelivr.net/npm/chart.js@4.4.1`), fonts from Google Fonts. No build step.
- Mock data inside the file; no calls to real APIs.
- Works at 360 px wide (16 px side margins, no sideways scroll) and on a desktop.

## Design
- Start from the content and the main action of the screen. A clear hierarchy: one big title, readable body text (16 px, line height 1.5), generous spacing on a 4/8 px grid.
- Colours as CSS variables; contrast 4.5:1 for text; a dark theme when the user's product is dark.
- Real states: hover, focus, empty, loading - and make the main buttons do something (switch tabs, open a dialog, add an item).

## Steps
1. Write `prototype/index.html`.
2. Check it if a browser is available (`python3 -c "import playwright"`): open it, look for console errors, take a screenshot at 390 and 1440 px.
3. **SendFile** `prototype/index.html` with a one-line caption of what to look at.
