import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'


export default function globalSetup() {
  const root = process.env.WORKBENCH_E2E_ROOT
  if (!root) throw new Error('WORKBENCH_E2E_ROOT is missing')
  mkdirSync(root, { recursive: true })
  const project = fileURLToPath(new URL('../..', import.meta.url))
  const python = fileURLToPath(new URL('../../.venv/bin/python', import.meta.url))
  const canary = fileURLToPath(new URL('../../scripts/workbench_state_canary.py', import.meta.url))
  execFileSync(python, [canary, 'snapshot', '--project', project, '--output', `${root}/live-state-before.json`], {
    cwd: project,
    stdio: 'inherit',
  })
}
