---
name: frontend-design
description: Build web interfaces that look finished - layout, typography, colour, states, responsive behaviour and accessibility. Use when creating or changing pages, components or styles.
---

# Frontend design

- Start from the content and the main action of the screen; one clear primary action per view.
- Layout: a spacing scale (4/8/12/16/24/32), aligned edges, max text width ~70 characters. Works at 360 px wide with 16 px side margins and no horizontal scroll.
- Typography: at most two families; a size scale (12/14/16/20/24/32); line height 1.4-1.6 for text.
- Colour: define colours as CSS variables; text contrast at least 4.5:1; support dark mode with `prefers-color-scheme` when the project has it.
- Every interactive element has hover, focus-visible, active and disabled states; every list has empty, loading and error states.
- Accessibility: real `<button>`/`<a>`, labels for inputs, alt text, keyboard navigation, `aria-live` for async updates.
- Check the result with the Screenshot tool (desktop and phone): look at the pictures, fix what looks off, take them again.
