import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const playwrightCli = require.resolve('@playwright/test/cli')
const forwardedArgs = process.argv.slice(2)
const runnerOwnedArg = forwardedArgs.find(
  (arg) =>
    arg === '--pass-with-no-tests' ||
    arg === '--project' ||
    arg.startsWith('--project='),
)

if (runnerOwnedArg !== undefined) {
  console.error(`E2E runner argument is not allowed: ${runnerOwnedArg}`)
  process.exit(2)
}

for (const project of ['local', 'hosted']) {
  const result = spawnSync(
    process.execPath,
    [
      playwrightCli,
      'test',
      'e2e',
      ...forwardedArgs,
      `--project=${project}`,
    ],
    {
      env: process.env,
      stdio: 'inherit',
    },
  )

  if (result.error) {
    console.error(result.error)
    process.exit(1)
  }

  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}
