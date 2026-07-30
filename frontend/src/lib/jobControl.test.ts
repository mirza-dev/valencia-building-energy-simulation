import { describe, expect, it } from 'vitest'
import {
  appendLiveLog,
  formatDuration,
  jobTiming,
  readJobStreamCheckpoint,
  writeJobStreamCheckpoint,
} from './jobControl'
import type { JobRecord } from './types'

const job: JobRecord = {
  id: 'job', kind: 'simulation', status: 'running', stage: 'EnergyPlus', refparcela: 'A',
  attempt_count: 1, max_attempts: 2, cancel_requested: 0, timeout_seconds: 600,
  heartbeat_at: '2026-07-14T00:01:38.000Z', attempt_started_at: '2026-07-14T00:00:00.000Z',
  terminal_at: null, queue_position: null, created_at: '2026-07-13T23:59:58.000Z',
  updated_at: '2026-07-14T00:01:38.000Z',
}

describe('simulation job control', () => {
  it('derives deterministic elapsed, timeout, and worker heartbeat clocks', () => {
    const timing = jobTiming(job, Date.parse('2026-07-14T00:01:40.000Z'))
    expect(timing).toEqual({ elapsedSeconds: 100, remainingSeconds: 500, heartbeatAgeSeconds: 2 })
    expect(formatDuration(timing.elapsedSeconds)).toBe('01:40')
  })

  it('freezes elapsed time at the terminal timestamp', () => {
    const timing = jobTiming({ ...job, status: 'failed', terminal_at: '2026-07-14T00:02:00.000Z' }, Date.parse('2026-07-14T01:00:00.000Z'))
    expect(timing.elapsedSeconds).toBe(120)
    expect(timing.remainingSeconds).toBe(480)
  })

  it('freezes migrated terminal jobs at updated_at when terminal_at is absent', () => {
    const timing = jobTiming({ ...job, status: 'completed', terminal_at: null, updated_at: '2026-07-14T00:02:10.000Z' }, Date.parse('2026-07-14T01:00:00.000Z'))
    expect(timing.elapsedSeconds).toBe(130)
  })

  it('caps live logs without reordering their newest evidence', () => {
    expect(appendLiveLog('12345', '67890', 7)).toBe('4567890')
  })

  it('round-trips reconnect cursors, events, and log evidence in session storage', () => {
    sessionStorage.clear()
    const checkpoint = {
      version: 1 as const,
      cursor: { afterId: 7, stdoutOffset: 120, stderrOffset: 40 },
      events: [{ id: 7, level: 'info' as const, message: 'EnergyPlus', progress: 0.26, created_at: job.updated_at }],
      logs: { stdout: 'annual simulation', stderr: 'translator evidence' },
    }
    expect(writeJobStreamCheckpoint(sessionStorage, job.id, checkpoint)).toBe(true)
    expect(readJobStreamCheckpoint(sessionStorage, job.id)).toEqual(checkpoint)
  })

  it('ignores malformed reconnect checkpoints instead of corrupting the live stream', () => {
    sessionStorage.setItem(`workbench:simulation-job-stream:v1:${job.id}`, '{broken')
    expect(readJobStreamCheckpoint(sessionStorage, job.id)).toBeNull()
  })
})
