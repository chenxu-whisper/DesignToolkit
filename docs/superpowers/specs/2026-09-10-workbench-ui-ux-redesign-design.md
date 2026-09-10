# DesignToolkit Workbench UI/UX Redesign

Date: 2026-09-10

## Objective

Redesign DesignToolkit as a desktop-first professional media workbench without
changing its processing APIs, task semantics, or core workflows. The interface
must improve visual hierarchy, repeated-operation efficiency, responsive
behavior, accessibility, and status feedback across video frame extraction,
media resizing, and chroma keying.

## Design Direction

The approved direction combines:

- an inspector-first workspace;
- a graphite-gray and mint-green visual system;
- light, dark, and system-following themes;
- a compact desktop layout with responsive fallbacks;
- a collapsible, resizable bottom log panel.

The interface must feel like a focused media tool rather than a marketing site.
It should use restrained surfaces, compact controls, stable dimensions, and
clear operational states. It must not add decorative gradients, floating page
sections, oversized headings, or nested cards.

## Audience And Usage

The primary user performs repeated media-processing operations on desktop.
Tablet and phone layouts must remain complete and usable, but desktop
throughput and media-preview space take priority.

## Information Architecture

### Global shell

The application shell contains four persistent regions:

1. A 52px command bar.
2. A 72px desktop tool rail.
3. A module-specific workspace.
4. A bottom log panel.

The document body does not scroll. Each region owns its required scrolling so
the command bar, tool rail, inspector actions, and log controls remain stable.

### Command bar

The command bar contains:

- the DesignToolkit identity;
- the active module name;
- the current task state;
- a theme menu;
- a log-panel toggle.

The task state uses icon, text, and color together. Theme controls use a
three-state menu: system, light, and dark.

### Tool rail

The desktop tool rail uses Lucide icons and short labels for:

- video frame extraction;
- media resizing;
- chroma keying.

The active module uses the mint accent and a visible active indicator. All
items expose tooltips and preserve accessible names.

### Module workspaces

Video frame extraction and media resizing use a centered, compact form
workspace. They do not render an empty inspector column.

Chroma keying uses an inspector-first grid:

- the center area owns media loading, preview, sampling, and timeline controls;
- a 340px right inspector owns media configuration, keying parameters, preview
  mode, output configuration, validation, and export;
- the inspector scrolls independently;
- the primary export action remains visible at the inspector bottom.

## Component Design

### Theme system

Theme tokens are defined with CSS custom properties. Semantic tokens cover:

- application and elevated surfaces;
- subtle and strong borders;
- primary and secondary text;
- muted text;
- mint accent and accent contrast;
- information, success, warning, and danger states;
- focus rings and overlays.

The light theme uses cool graphite neutrals with restrained white surfaces. The
dark theme uses near-black graphite surfaces rather than blue-dominant slate.
Mint is reserved for active controls, sampling, progress, and success-adjacent
interaction emphasis.

Theme selection is stored in local storage. System mode responds to
`prefers-color-scheme` changes while the application is open. The HTML root
reflects the resolved theme so native controls and CSS tokens remain aligned.

### Form controls

Inputs, selects, text areas, radio controls, sliders, and buttons share stable
heights, border treatment, focus treatment, disabled states, and error states.
Labels remain visible and compact. Required status is available to assistive
technology and is not conveyed only by color.

Slider values remain synchronized with their numeric fields. Focus, hover,
invalid, disabled, and pending states must not resize surrounding layout.

### Chroma preview and inspector

The preview area is the dominant visual region. An unloaded state presents the
existing file selection or upload workflow as the primary action. Loaded media
uses a neutral viewer background that works in both themes.

The inspector groups controls into:

- media;
- keying parameters;
- preview;
- output.

Groups are unframed sections separated by spacing and subtle dividers. The
export action sits in a sticky inspector footer. Existing validation and task
submission behavior remain unchanged.

### Log panel

The log panel supports three interaction states:

- collapsed: 44px high and shows the latest event summary;
- expanded: restores the last user-selected height;
- resized: drag handle adjusts height from 120px to 45vh.

The drag handle supports pointer input and keyboard adjustment. The panel
exposes its expanded state and current size through appropriate ARIA
attributes. Copy and clear commands use icons with tooltips. Log content keeps
its monospaced presentation and independently scrolls.

The selected height and collapsed state are local UI preferences. Resizing must
not write to task or log business stores.

### Motion

Motion is limited to:

- panel expansion and collapse;
- menu and drawer transitions;
- state-change feedback;
- button and focus feedback.

Animations use opacity and transform, complete within 180ms, and do not animate
layout continuously during drag resizing. Under `prefers-reduced-motion:
reduce`, transitions and smooth scrolling are removed.

## Responsive Behavior

### Wide desktop: 1180px and above

- Full command bar and 72px tool rail.
- Chroma preview and 340px inspector remain visible together.
- Log panel is collapsed, expanded, or resized at the bottom.

### Compact desktop and tablet: 760px to 1179px

- Tool rail remains available with compact labels.
- The chroma inspector becomes a right-side drawer.
- The workspace receives the full available width.
- Drawer focus is trapped while open and returns to its trigger when closed.

### Phone: below 760px

- Module navigation becomes a stable horizontal top bar.
- The inspector opens as a full-screen overlay.
- The log panel is either a collapsed summary or a full-screen log view.
- Forms become single-column.
- Interactive controls provide at least a 44px touch target.

No viewport may introduce incoherent overlap or horizontal page scrolling.
Long filenames, errors, task identifiers, and log messages must wrap or
truncate within defined containers.

## State And Data Flow

Existing API calls, payloads, task state, log state, and module form state stay
unchanged.

New local UI state is isolated from business state:

- theme preference and resolved theme;
- log collapsed state and height;
- responsive inspector open state;
- transient menu and tooltip state.

Theme preference and resolution belong to a dedicated `ThemeProvider`. Log
panel state belongs to the application shell. Inspector visibility belongs to
the chroma workspace. No new server endpoint or backend dependency is required.

## Error And Status Handling

Existing field-level validation remains adjacent to the affected control.
Task-level failures remain visible in the active workspace and are announced
through an accessible live region.

Pending operations:

- disable conflicting actions;
- preserve layout dimensions;
- expose text and semantic status;
- avoid indefinite decorative loading states.

Empty, loading, success, warning, and failure states use semantic tokens and
icons. Color is never the only indicator.

## Accessibility

The redesign must preserve or improve:

- semantic landmarks and heading order;
- keyboard access to every command;
- visible `:focus-visible` treatment;
- accessible names for icon buttons;
- tooltip availability for unfamiliar icons;
- sufficient text and control contrast in both themes;
- reduced-motion behavior;
- logical focus order;
- focus management for menus and responsive drawers;
- screen-reader announcements for task-state changes.

## Implementation Boundaries

The work may refactor the application shell, shared controls, chroma workspace
layout, CSS organization, and frontend tests. It must not change media
algorithms, task payloads, API routes, backend behavior, or processing limits.

Existing Lucide React icons must be reused. The redesign adds no new frontend
dependency.

## Verification

Automated verification includes:

- Vitest coverage for theme selection and persistence;
- Vitest coverage for log collapse, expansion, keyboard resizing, and bounds;
- existing module, form, API-client, and task-state tests;
- TypeScript lint;
- production build.

Browser verification includes:

- light and dark themes;
- desktop, compact desktop/tablet, and phone viewports;
- all three modules;
- unloaded and loaded chroma layouts where test media is available;
- expanded, collapsed, minimum-height, and maximum-height log states;
- keyboard navigation and visible focus;
- reduced-motion behavior;
- horizontal overflow and text-overlap checks;
- browser console checks.

## Acceptance Criteria

1. Desktop chroma keying presents a dominant preview and a 340px inspector.
2. Simpler modules use the workspace efficiently without an empty inspector.
3. Theme selection supports system, light, and dark modes and persists.
4. The log panel collapses, expands, and resizes with pointer and keyboard.
5. Layout remains usable without overlap at the defined responsive ranges.
6. Existing task submission, validation, upload, preview, and result behavior
   remains intact.
7. Icon-only commands have tooltips and accessible names.
8. Both themes maintain readable contrast and visible focus.
9. Reduced-motion users do not receive nonessential motion.
10. Frontend tests, TypeScript lint, and production build pass.
