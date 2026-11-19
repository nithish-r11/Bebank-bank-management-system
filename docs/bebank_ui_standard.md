# BEBANK UI Design Standard

## 1. Overview & Purpose
This document establishes the official visual design language and UI standards for all current and future screens of the **BEBANK Bank Management System**. 

The design standard is anchored in the approved BEBANK Login & Terms design, projecting trust, precision, security, and a professional institutional banking aesthetic.

---

## 2. Core Visual Foundations

### A. Color Palette
* **Deep Navy Dark**: `#071326` (Primary dark background, foundational contrast)
* **Deep Navy**: `#0a192f` (Navbar, headers, card borders, primary buttons)
* **Navy Mid / Surfaces**: `#112240` (Secondary dark panels, subtle depth)
* **Navy Border**: `#1e3a5f` or `rgba(255, 255, 255, 0.08)` (Subtle dividers and borders)
* **Clean White**: `#ffffff` (Primary content panels, cards, high-contrast text on dark backgrounds)
* **Light Slate Background**: `#f8fafc` / `#f1f5f9` (Clean, modern canvas for light dashboard views)
* **Subtle Gold Accents**:
  * Primary Gold: `#f59e0b` / `#d97706`
  * Ribbon Gradient: Linear gradient from `#fef08a` through `#eab308` to `#854d0e`
* **Text Colors**:
  * Primary Text (Light Surface): `#0f172a` (Slate-900)
  * Secondary / Muted Text: `#64748b` (Slate-500)
  * High-Contrast Text (Dark Surface): `#ffffff`
  * Muted Light Text: `#94a3b8` (Slate-400)

### B. Typography
* **Primary Sans-Serif Font**: **Inter** (`'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`)
  * Weights: `400` (Regular), `500` (Medium), `600` (Semi-Bold), `700` (Bold), `800` (Extra-Bold)
  * Prohibited: Decorative serif fonts (e.g., 'Times New Roman')
* **Monospace / Tabular Font**: `'Consolas', 'SF Mono', 'Courier New', monospace`
  * Usage: Account numbers, currency amounts, transaction IDs, timestamps (`font-variant-numeric: tabular-nums;`)

### C. Official Brand Logo & Symbols
* **Primary Monogram**: Transparent scalable **B-Monogram SVG** (`static/images/bebank_b_monogram.svg`)
  * Classical architectural temple columns embedded inside the capital letter 'B' with golden pediment and sweep.
* **Header Branding Group**:
  * Visual size: 46px × 46px logo, preserving aspect ratio (`object-fit: contain`).
  * 1.5px vertical divider (46px height, `rgba(255, 255, 255, 0.28)`).
  * Stacked brand title (`BEBANK`, 1.15–1.35rem, bold) and subtitle (`Bank Management System`, 0.66–0.76rem, slate-400).
  * Centered on the exact same horizontal center line via a single flex container (`display: flex; align-items: center; gap: 16px; height: 46px;`).
* **Browser Tab Favicons**:
  * SVG Favicon: `static/images/bebank_b_monogram.svg` (`type="image/svg+xml"`)
  * Multi-size PNG fallbacks: 16x16, 32x32, 48x48
  * Apple Touch Icon: 180x180 on `#0a192f` navy brand background
  * Standard: Use monogram only in browser tabs; never cram full wordmarks into tiny icon spaces.

### D. Iconography
* **Style**: Clean, modern outline icons (Lucide-style / 2px stroke inline SVGs).
* **Color**: Coordinated with surrounding text and navy/blue palette.
* **Prohibited**: Emojis, inconsistent colored bitmap icons, and unstyled Unicode symbols.

---

## 3. UI Component Specifications

### A. Rounded Panels & Cards
* **Border Radius**: 18px to 24px for major cards; 8px to 12px for internal cards/tables.
* **Background**: Clean white (`#ffffff`) on light or dark canvas.
* **Shadows**: Soft, layered elevation:
  `box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);`
* **Padding**: Consistent, breathable interior spacing (`32px` to `48px` for major desktop containers).

### B. Action Controls & Buttons
* **Primary Action Button**:
  * Background: Solid deep navy `#0a192f`
  * Hover: Navy Mid `#112240` with subtle upward transform (`translateY(-1px)`) and enhanced shadow.
  * Border Radius: Pill (`50px`) or modern rounded (`8px`).
  * Text: Crisp white (`#ffffff`), font-weight 600, letter-spacing 0.3px.
  * Inline Action Icon: White outline SVG with 8px gap.
* **Secondary / Danger Buttons**:
  * Secondary: Subtle slate outline or light navy wash.
  * Danger: Crimson `#dc2626` with clean outline icon.

### C. Form Controls
* **Inputs & Selects**:
  * Height: 46px to 50px for primary form controls.
  * Border: 1.5px solid `#e2e8f0` (focus: `#1d4ed8` or `#0a192f` with soft focus ring).
  * Border Radius: Modern rounded (`8px` to `12px`).
  * Floating or clear top label with accompanying outline SVG icon.

---

## 4. Application to Future Screens

This visual language must be applied across all future banking modules:
1. **Customer Dashboard**: Clean account overview cards, tabular transaction history, quick-action transfer tiles.
2. **Employee Dashboard**: Account approval lists, verification queues, customer search.
3. **Admin Dashboard**: System health indicators, user lock/unlock management, system audit telemetry.
4. **Fixed Deposit (FD) Screens**: Term calculation cards, interest rate schedules, maturity projections.
5. **Loan Management Screens**: Application status badges, EMI calculation tables, administrative approval workflows.

### Architectural Rules for Future Screens
* **Do NOT clone the Login form layout**: Dashboards and operational views must use clean multi-column, data-dense layouts appropriate for their functions, not login cards.
* **No Virtual Money Clutter**: Avoid cartoonish money illustrations, fake coin stacks, or simulated game-like balances.
* **Preserve Security Standards**: All interactive forms must include CSRF tokens, duplicate submission guards, and RBAC authorization decorators.
* **Responsive Layout**: All screens must adapt seamlessly across desktop (`>=1200px`), tablet (`768px - 1199px`), and mobile (`<768px`).

