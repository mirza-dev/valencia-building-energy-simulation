import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import {
  appendLiveLog,
  readJobStreamCheckpoint,
  writeJobStreamCheckpoint,
  type JobStreamCheckpoint,
} from './jobControl'
import type { JobEvent } from './types'

type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'closed'
type LogStream = 'stdout' | 'stderr'

export function useJobStream(jobId: string, terminal: boolean) {
  const [events, setEvents] = useState<JobEvent[]>([])
  const [logs, setLogs] = useState<Record<LogStream, string>>({ stdout: '', stderr: '' })
  const [connection, setConnection] = useState<ConnectionState>('closed')
  const cursor = useRef({ afterId: 0, stdoutOffset: 0, stderrOffset: 0 })
  const checkpoint = useRef<JobStreamCheckpoint>({
    version: 1,
    cursor: { afterId: 0, stdoutOffset: 0, stderrOffset: 0 },
    events: [],
    logs: { stdout: '', stderr: '' },
  })
  const terminalRef = useRef(terminal)

  useEffect(() => { terminalRef.current = terminal }, [terminal])

  useEffect(() => {
    let storage: Storage | null = null
    try { storage = window.sessionStorage } catch { storage = null }
    const restored = jobId && storage ? readJobStreamCheckpoint(storage, jobId) : null
    checkpoint.current = restored ?? {
      version: 1,
      cursor: { afterId: 0, stdoutOffset: 0, stderrOffset: 0 },
      events: [],
      logs: { stdout: '', stderr: '' },
    }
    cursor.current = { ...checkpoint.current.cursor }
    setEvents(checkpoint.current.events)
    setLogs(checkpoint.current.logs)
    if (!jobId) { setConnection('closed'); return }

    let disposed = false
    let source: EventSource | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectAttempt = 0
    const persist = () => {
      checkpoint.current.cursor = { ...cursor.current }
      if (storage) writeJobStreamCheckpoint(storage, jobId, checkpoint.current)
    }

    const connect = () => {
      if (disposed) return
      setConnection(reconnectAttempt ? 'reconnecting' : 'connecting')
      source = new EventSource(api.eventsUrl(jobId, cursor.current))
      source.onopen = () => { reconnectAttempt = 0; setConnection('live') }
      source.onmessage = (message) => {
        const event = JSON.parse(message.data) as JobEvent
        cursor.current.afterId = Math.max(cursor.current.afterId, event.id)
        if (!checkpoint.current.events.some((item) => item.id === event.id)) {
          checkpoint.current.events = [...checkpoint.current.events, event].slice(-100)
          setEvents(checkpoint.current.events)
        }
        persist()
      }
      source.addEventListener('log', (message) => {
        const payload = JSON.parse((message as MessageEvent).data) as {
          stream: LogStream; offset: number; reset?: boolean; text: string
        }
        cursor.current[payload.stream === 'stdout' ? 'stdoutOffset' : 'stderrOffset'] = payload.offset
        checkpoint.current.logs = {
          ...checkpoint.current.logs,
          [payload.stream]: appendLiveLog(
            payload.reset ? '' : checkpoint.current.logs[payload.stream], payload.text,
          ),
        }
        setLogs(checkpoint.current.logs)
        persist()
      })
      source.onerror = () => {
        source?.close()
        if (disposed || terminalRef.current) { setConnection('closed'); return }
        reconnectAttempt += 1
        setConnection('reconnecting')
        reconnectTimer = setTimeout(connect, Math.min(3000, 350 * 2 ** Math.min(reconnectAttempt, 3)))
      }
    }

    connect()
    return () => {
      disposed = true
      source?.close()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [jobId])

  return { events, logs, connection }
}
