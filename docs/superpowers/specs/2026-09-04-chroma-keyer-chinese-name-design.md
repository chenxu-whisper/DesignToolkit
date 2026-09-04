# Chroma Keyer Chinese Name Design

## Goal

Rename the user-facing `Studio Chroma Keyer` module to `色键抠图`.

## Scope

- Change the sidebar module label to `色键抠图`.
- Change the module page heading to `色键抠图`.
- Update current README and PRD references that describe the visible module name.
- Update frontend unit and Playwright assertions that locate the module by its visible name.

## Non-Goals

- Do not rename the internal `chroma_keyer` module ID, source directories, API routes, task kinds, or Python package.
- Do not rewrite historical design and implementation plan documents.
- Do not change layout, behavior, or styling.

## Verification

- App shell tests verify the Chinese navigation label and heading.
- Existing Chroma Keyer frontend tests, Playwright tests, lint, and production build pass.
