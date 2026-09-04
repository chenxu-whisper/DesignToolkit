# Chroma Keyer Chinese Name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the visible Chroma Keyer module to `色键抠图` without changing internal identifiers or behavior.

**Architecture:** Keep `chroma_keyer`, API routes, and source paths unchanged. Replace only current user-facing labels and the tests and documentation that assert those labels.

**Tech Stack:** React 19, TypeScript, Vitest, Playwright, Markdown

---

### Task 1: Rename The Visible Module

**Files:**
- Modify: `web/frontend/src/app/AppShell.test.tsx`
- Modify: `web/frontend/e2e/app.spec.ts`
- Modify: `web/frontend/src/app/modules.ts`
- Modify: `web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx`
- Modify: `README.md`

- [ ] **Step 1: Update the focused unit assertion first**

Change the AppShell navigation test to require the new visible name:

```typescript
it('opens 色键抠图 from the navigation', async () => {
  const user = userEvent.setup()

  render(<AppShell />)

  await user.click(screen.getByRole('button', { name: '色键抠图' }))

  expect(screen.getByRole('heading', { name: '色键抠图' })).toBeInTheDocument()
})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
npm --prefix web/frontend test -- src/app/AppShell.test.tsx
```

Expected: the renamed navigation test fails because the current UI still renders `Studio Chroma Keyer`.

- [ ] **Step 3: Apply the visible name**

Set the `chroma_keyer` label in `web/frontend/src/app/modules.ts` to `色键抠图`, and change the `h1` text in `ChromaKeyerPage.tsx` to the same value.

- [ ] **Step 4: Synchronize current browser assertions and README**

Replace current Playwright navigation and heading locators with `色键抠图`. Rename the current README section and its sidebar instruction to the same visible name. Do not alter historical plan/spec documents or internal identifiers.

- [ ] **Step 5: Run GREEN verification**

Run:

```bash
npm --prefix web/frontend test -- src/app/AppShell.test.tsx
npm --prefix web/frontend test
npm --prefix web/frontend run lint
npm --prefix web/frontend run build
npm --prefix web/frontend run test:e2e
git diff --check
```

Expected: all commands exit zero and no current UI assertion still depends on `Studio Chroma Keyer`.

- [ ] **Step 6: Commit only scoped files**

```bash
git add README.md web/frontend/src/app/AppShell.test.tsx web/frontend/src/app/modules.ts web/frontend/src/modules/chroma-keyer/ChromaKeyerPage.tsx web/frontend/e2e/app.spec.ts
git commit -m "refactor: rename chroma keyer module"
```
