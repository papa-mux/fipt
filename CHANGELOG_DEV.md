# FIPT — development changes

## UI defaults

- New installations use the readable text icon set instead of Braille art.
- The built-in log panel is disabled by default. It remains available through Playback settings (`show_log`) for troubleshooting.
- Existing user preferences are preserved: an already saved `icons: braille` or `show_log: true` is not overwritten.

## Compatibility notes

- No new mandatory Python dependencies were added.
- mpv remains the playback backend and the existing mobile/classic layouts remain intact.
- The SVG files in `assets/icons/` remain source assets for the icon-generation workflow. Native inline SVG rendering is not enabled in this build: Textual terminals do not provide a portable SVG surface by themselves. A terminal-image backend (and capability detection/fallback) is needed before exposing an `svg` setting honestly.
- MPRIS, embedded cover-art display and editable key bindings are not implemented in this incremental build. They need separate, tested integrations rather than placeholder settings.
