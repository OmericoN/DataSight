# DataSight UM Discovery — Design QA

## Evidence

- Source visual truth: [Runs screen](design-qa-source.png)
  - Existing DataSight Runs screen used as the product-design-system reference.
  - CSS viewport: 1280 × 720, light theme, DPR reported as 0.9.
  - Capture pixels: 1422 × 800.
- Rendered implementation: [Launch screen](design-qa-implementation.png)
  - `/launch`, UM Broad Discovery selected, live catalog profile loaded, OpenAlex ready.
  - CSS viewport: 1280 × 720, light theme, DPR reported as 0.9.
  - Capture pixels: 1403 × 800; the width difference is the implementation page's visible scrollbar, so comparison used the shared 1280 × 720 CSS viewport and content frame rather than raw pixel width.
- Responsive implementation: [Mobile Launch screen](design-qa-implementation-mobile.png)
  - `/launch`, compact preview sheet, persistent launch action, live profile ready.
  - CSS viewport reported as 433 × 938 after the browser's responsive override; capture pixels: 462 × 1041.

The source and implementation are different product screens, so the comparison is for design-language fidelity rather than pixel-identical content. The navigation shell, header, theme, typography, button treatment, card borders, spacing scale, icons, and status treatments are directly comparable.

## Full-view comparison evidence

- Fonts and typography: both screens use the existing Geist Mono/Lora DataSight typography, the same heading weights, compact metadata scale, and readable wrapping. The denser Launch copy maintains the source hierarchy without truncation.
- Spacing and layout rhythm: the Launch screen retains the 20 px desktop content inset, card border treatment, square-to-soft radius scale, and 12–20 px internal rhythm visible on Runs. The two-column builder/preview layout fits without horizontal overflow.
- Colors and tokens: background, muted text, orange primary action, selected-card tint, semantic status colors, borders, and disabled states all use the existing token system. No new unrelated palette or gradient was introduced.
- Image quality and assets: the existing Maastricht/DataSight raster mark is preserved at the same scale and crop. All functional icons use the existing Remix icon family; no placeholder, CSS-drawn, emoji, or custom SVG assets were introduced.
- Copy and content: preset names, evidence strengths, cost estimates, PDF readiness, candidate/process limits, and warnings are concise and operator-oriented. Controls remain understandable without relying only on color.
- Responsive behavior: the mobile view has no horizontal overflow, a 2 × 2 readiness grid, stacked preset cards, a compact expandable Strategy preview bottom sheet, and a persistent launch bar.

## Focused region comparison evidence

Focused comparison was performed on the shared navigation/header and primary card/action regions because typography, icon alignment, selected states, border density, and button treatment are the fidelity-critical surfaces. The new lane rows deliberately add denser evidence metadata, but preserve the source screen's border, muted-text, badge, and spacing conventions.

## Interaction and accessibility checks

- Preset cards expose pressed state and are keyboard buttons.
- Lane switches have explicit accessible names and non-color evidence-strength text.
- Every scope, publication-type, path, cost, and execution control has a label.
- Lane explanations expose an expandable keyboard-focusable summary and a bounded quota input.
- The mobile Strategy preview exposes `aria-expanded` and successfully reveals the Test strategy control.
- Invalid/missing OpenAlex state blocks preview/launch and provides a direct status message.
- Candidate review keeps non-PDF records visible while disabling their pipeline inclusion control.
- Browser console errors checked: none.
- Automated interaction coverage includes preset switching, manual launch, preview loading/results, lane quotas, reviewed launch, and non-PDF review state.

## Comparison history

1. Earlier P2: the desktop persistent launch action overlaid the Strategy preview's Test strategy button.
   - Fix: made the launch bar static at the desktop breakpoint and reserved sticky/fixed behavior for smaller screens.
   - Post-fix evidence: `design-qa-implementation.png` shows separate, unobstructed Test strategy and launch regions.
2. Earlier P2: the mobile Strategy preview occupied too much of the viewport and the launch action was not consistently persistent.
   - Fix: collapsed the mobile preview sheet by default, added an accessible Expand/Collapse action, made the launch bar fixed, and reduced readiness cards to a 2 × 2 grid.
   - Post-fix evidence: `design-qa-implementation-mobile.png` shows the builder above the compact sheet with the launch action anchored at the bottom and no horizontal overflow.
3. Earlier P2: the dev proxy used `localhost`, which resolved differently from the IPv4-only local API and presented a false provider-unavailable state.
   - Fix: pinned the local Vite proxy target to `127.0.0.1`.
   - Post-fix evidence: both implementation captures show 2,748 UM datasets and OpenAlex Ready with no alert.

## Findings

No actionable P0, P1, or P2 visual or interaction findings remain.

Residual P3: the Launch screen is intentionally denser than the Runs reference because seven evidence lanes and cost/readiness controls are required. Progressive disclosure and the sticky summary keep that density manageable.

## Implementation checklist

- [x] Match existing shell, typography, tokens, cards, and Remix icons.
- [x] Verify desktop layout and sticky preview behavior.
- [x] Verify compact responsive layout and persistent action.
- [x] Verify labels, semantic state, focusable disclosures, and non-color evidence indicators.
- [x] Verify live readiness state and absence of console errors.
- [x] Fix all P0/P1/P2 findings and recapture evidence.

final result: passed
