---
name: html-prototype
description: Build an interactive prototype or a small web page in HTML, look at it with screenshots and show it live in the chat - landing pages, app screens, dashboards, mock-ups to look at and click. Use when the user wants to see a design or a prototype.
---

# HTML prototypes

The chat shows an HTML file live (in a sandbox) when you send it with **SendFile**. Make it worth looking at.

## Rules of the format
- A folder per prototype: `prototype/index.html`, with `style.css` and `app.js` next to it when the page is big (the chat preview loads files next to the page by relative paths). A small page can be one file.
- External code only from a CDN with an exact version (e.g. `https://cdn.jsdelivr.net/npm/chart.js@4.4.1`), fonts from Google Fonts. No build step.
- Mock data inside the file; no calls to real APIs.
- Works at 360 px wide (16 px side margins, no sideways scroll) and on a desktop.

## Design
- Start from the content and the main action of the screen. A clear hierarchy: one big title, readable body text (16 px, line height 1.5), generous spacing on a 4/8 px grid.
- Colours as CSS variables; contrast 4.5:1 for text; a dark theme when the user's product is dark.
- Real states: hover, focus, empty, loading - and make the main buttons do something (switch tabs, open a dialog, add an item).

## Steps
1. Write the page (`prototype/index.html`; several variants: `prototype/a/index.html`, `prototype/b/...`). A big file in parts: the first ~300 lines, then Write with `append: true`.
2. **Look at it**: `Screenshot` the page (desktop and phone). Study the pictures like a designer would: alignment, spacing, overflow, text that is cut or unreadable, contrast, what draws the eye first, the phone layout. Fix what is wrong and take the screenshots again - at least one round of fixes is normal.
3. **SendFile** the page (it opens live in the chat) with a one-line caption of what to look at, and SendFile the desktop screenshot (`screenshots/<name>-desktop.png`) so the user sees it at a glance.
