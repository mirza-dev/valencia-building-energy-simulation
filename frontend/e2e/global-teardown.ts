import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'


export default function globalTeardown() {
  const root = process.env.WORKBENCH_E2E_ROOT
  if (!root) throw new Error('WORKBENCH_E2E_ROOT is missing')
  const project = fileURLToPath(new URL('../..', import.meta.url))
  const python = fileURLToPath(new URL('../../.venv/bin/python', import.meta.url))
  const canary = fileURLToPath(new URL('../../scripts/workbench_state_canary.py', import.meta.url))
  execFileSync(python, [canary, 'verify', '--project', project, '--output', `${root}/live-state-before.json`], {
    cwd: project,
    stdio: 'inherit',
  })
}
