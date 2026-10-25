# Drop Harbor visual system

The web console borrows the compact workspace/navigation hierarchy of
[Linear](https://linear.app) and the whitespace, restrained typography and
neutral surfaces of [Apple](https://www.apple.com). It does not reuse either
brand's logo, product artwork or page templates.

- White canvas, soft gray sidebar, thin neutral separators.
- Compact application header; generous spacing in the overview, not a marketing page.
- Graphite primary actions; muted violet for selection/progress and green only for
  actual connected/running states. Errors retain a distinct red treatment.
- Original inline SVG icons, system fonts and no external asset/font dependencies.
- 224px desktop navigation, reduced-width tablet navigation, four-item mobile nav.
- At 700px the workspace switches to a single column; controls remain full-width
  or grouped for touch. At 900px overview panels stack. Preserve visible focus
  states, reduced-motion support, labels and aria-current on navigation.
- All model bindings and API behavior remain unchanged. Preview data is explicitly
  offline fixture data; do not present it as evidence of live Twitch progress.

References fetched with the local `smart-search fetch URL --format content`
workflow. Visual QA covers desktop (1440px), narrow/mobile (390px) and the existing
authenticated preview; no NAS state is changed by styling work.
