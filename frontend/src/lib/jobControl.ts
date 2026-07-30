import type { JobEvent, JobRecord } from './types'

export const TERMINAL_JOB_STATUSES = new Set(['completed', 'failed', 'canceled', 'ready'])
export const MAX_LIVE_LOG_CHARS = 120_000
export const MAX_JOB_EVENTS = 100

export interface JobStreamCursor {
  afterId: number
  stdoutOffset: number
  stderrOffset: number
}

export interface JobStreamCheckpoint {
  version: 1
  cursor: JobStreamCursor
  events: JobEvent[]
  logs: { stdout: string; stderr: string }
}

const checkpointKey = (jobId: string) => `workbench:simulation-job-stream:v1:${jobId}`

export function readJobStreamCheckpoint(storage: Storage, jobId: string): JobStreamCheckpoint | null {
  try {
    const raw = storage.getItem(checkpointKey(jobId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<JobStreamCheckpoint>
    const cursor = parsed.cursor
    if (parsed.version !== 1 || !cursor
      || ![cursor.afterId, cursor.stdoutOffset, cursor.stderrOffset].every(Number.isFinite)) return null
    return {
      version: 1,
      cursor: {
        afterId: Math.max(0, cursor.afterId),
        stdoutOffset: Math.max(0, cursor.stdoutOffset),
        stderrOffset: Math.max(0, cursor.stderrOffset),
      },
      events: Array.isArray(parsed.events)
        ? parsed.events.filter((item): item is JobEvent => Number.isFinite(item?.id)).slice(-MAX_JOB_EVENTS)
        : [],
      logs: {
        stdout: appendLiveLog('', typeof parsed.logs?.stdout === 'string' ? parsed.logs.stdout : ''),
        stderr: appendLiveLog('', typeof parsed.logs?.stderr === 'string' ? parsed.logs.stderr : ''),
      },
    }
  } catch {
    return null
  }
}

export function writeJobStreamCheckpoint(
  storage: Storage, jobId: string, checkpoint: JobStreamCheckpoint,
) {
  try {
    storage.setItem(checkpointKey(jobId), JSON.stringify({
      ...checkpoint,
      events: checkpoint.events.slice(-MAX_JOB_EVENTS),
      logs: {
        stdout: appendLiveLog('', checkpoint.logs.stdout),
        stderr: appendLiveLog('', checkpoint.logs.stderr),
      },
    }))
    return true
  } catch {
    return false
  }
}

function secondsBetween(start: string | null | undefined, endMs: number) {
  if (!start) return null
  const startMs = Date.parse(start)
  if (!Number.isFinite(startMs)) return null
  return Math.max(0, Math.floor((endMs - startMs) / 1000))
}

export function jobTiming(job: JobRecord | undefined, nowMs = Date.now()) {
  if (!job) return { elapsedSeconds: null, remainingSeconds: null, heartbeatAgeSeconds: null }
  const terminalReference = TERMINAL_JOB_STATUSES.has(job.status)
    ? job.terminal_at ?? job.updated_at
    : null
  const terminalMs = terminalReference ? Date.parse(terminalReference) : Number.NaN
  const effectiveNow = Number.isFinite(terminalMs) ? terminalMs : nowMs
  const elapsedSeconds = secondsBetween(job.attempt_started_at ?? job.created_at, effectiveNow)
  const remainingSeconds = elapsedSeconds === null
    ? null
    : Math.max(0, Number(job.timeout_seconds ?? 0) - elapsedSeconds)
  return {
    elapsedSeconds,
    remainingSeconds,
    heartbeatAgeSeconds: secondsBetween(job.heartbeat_at, nowMs),
  }
}

export function formatDuration(totalSeconds: number | null) {
  if (totalSeconds === null || !Number.isFinite(totalSeconds)) return '--:--'
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  const clock = `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
  return hours ? `${String(hours).padStart(2, '0')}:${clock}` : clock
}

export function appendLiveLog(current: string, addition: string, limit = MAX_LIVE_LOG_CHARS) {
  const combined = current + addition
  return combined.length <= limit ? combined : combined.slice(combined.length - limit)
}
