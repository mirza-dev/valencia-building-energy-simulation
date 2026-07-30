import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, ChevronDown, ChevronUp, CircleStop, Clock3, FileText,
  Radio, RefreshCw, TerminalSquare, X,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { formatDuration, jobTiming, TERMINAL_JOB_STATUSES } from '../lib/jobControl'
import { useJobStream } from '../lib/useJobStream'
import type { JobEvent, JobRecord } from '../lib/types'

const stages = ['Parent Verification', 'EnergyPlus', 'SQL Parse', 'QA', 'Finalize']

function eventLabel(event: JobEvent, translate: (key: string) => string) {
  if (event.message === 'Queued') return translate('simulation.events.queued')
  if (event.message === 'Cancel requested') return translate('simulation.events.cancelRequested')
  if (event.message.startsWith('Worker retry queued')) return translate('simulation.events.retryQueued')
  const stage = stages.find((item) => event.message.includes(item))
  return stage ? translate(`simulation.stages.${stage}`) : event.message
}

interface Props {
  jobId: string
  job?: JobRecord
  cancelPending: boolean
  retryPending: boolean
  onCancel: () => void
  onRetry: () => void
  onDismiss: () => void
}

export default function SimulationJobControl({
  jobId, job, cancelPending, retryPending, onCancel, onRetry, onDismiss,
}: Props) {
  const { t, i18n } = useTranslation()
  const terminal = TERMINAL_JOB_STATUSES.has(job?.status ?? '')
  const { events, logs, connection } = useJobStream(jobId, terminal)
  const [now, setNow] = useState(Date.now())
  const [cancelArmed, setCancelArmed] = useState(false)
  const [logStream, setLogStream] = useState<'stdout' | 'stderr'>('stdout')
  const [expanded, setExpanded] = useState(true)
  const [followLog, setFollowLog] = useState(true)
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    if (followLog && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [followLog, logStream, logs])
  useEffect(() => setCancelArmed(false), [jobId, job?.status])

  const timing = jobTiming(job, now)
  const latestEvent = events.at(-1)
  const stageSource = latestEvent?.message ?? job?.stage ?? ''
  const foundStage = stages.findIndex((stage) => stageSource.includes(stage))
  const stageIndex = Math.max(0, foundStage)
  const progress = job?.status === 'completed'
    ? 100
    : Math.max(0, Math.min(100, Math.round((latestEvent?.progress ?? 0) * 100)))
  const active = job?.status === 'queued' || job?.status === 'running'
  const currentLog = logs[logStream]
  const workerSignal = terminal
    ? t('simulation.job.terminal')
    : timing.heartbeatAgeSeconds === null
    ? '--'
    : timing.heartbeatAgeSeconds <= 3 ? t('simulation.job.live') : `${timing.heartbeatAgeSeconds}s`
  const statusLabel = t(`simulation.status.${job?.status ?? 'queued'}`)
  const connectionLabel = t(`simulation.connection.${connection}`)
  const visibleEvents = useMemo(() => events.slice(-8), [events])

  return <section className={`simulation-job-control ${terminal ? 'terminal' : ''} ${expanded ? '' : 'collapsed'}`} aria-live="polite">
    <header className="job-control-header">
      <div><Activity size={15} /><span><strong>{t('simulation.job.title')}</strong><small>{statusLabel} · {jobId.slice(0, 10)}</small></span></div>
      <div className="job-control-header-actions">
        <span className={`stream-state ${connection}`}><Radio size={12} />{connectionLabel}</span>
        <button className="icon-button" onClick={() => setExpanded((value) => !value)} title={t(expanded ? 'simulation.job.collapse' : 'simulation.job.expand')} aria-label={t(expanded ? 'simulation.job.collapse' : 'simulation.job.expand')}>{expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</button>
        {terminal ? <button className="icon-button" onClick={onDismiss} title={t('simulation.job.dismiss')} aria-label={t('simulation.job.dismiss')}><X size={16} /></button> : null}
      </div>
    </header>
    <div className="job-control-body">
      <div className="job-metrics">
        <div><span>{t('simulation.job.stage')}</span><strong>{foundStage >= 0 ? t(`simulation.stages.${stages[stageIndex]}`) : statusLabel}</strong></div>
        <div><span>{t('simulation.job.elapsed')}</span><strong><Clock3 size={12} />{formatDuration(timing.elapsedSeconds)}</strong></div>
        <div><span>{t('simulation.job.timeout')}</span><strong>{formatDuration(timing.remainingSeconds)}</strong></div>
        <div><span>{t('simulation.job.attempt')}</span><strong>{job?.attempt_count ?? 0} / {job?.max_attempts ?? 2}</strong></div>
        <div><span>{t('simulation.job.workerSignal')}</span><strong>{workerSignal}</strong></div>
        {job?.status === 'queued' ? <div><span>{t('simulation.job.queue')}</span><strong>#{job.queue_position ?? '--'}</strong></div> : null}
      </div>
      <div className="job-stage-strip">
        <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
        <ol>{stages.map((stage, index) => <li key={stage} className={index < stageIndex || job?.status === 'completed' ? 'done' : index === stageIndex && active ? 'active' : ''}><i>{index + 1}</i><span>{t(`simulation.stages.${stage}`)}</span></li>)}</ol>
      </div>
      <div className="job-diagnostics-grid">
        <section className="job-event-ledger">
          <header><span>{t('simulation.job.events')}</span><code>{events.length}</code></header>
          <div>{visibleEvents.map((event) => <div key={event.id} data-event-id={event.id} className={event.level}><time>{new Date(event.created_at).toLocaleTimeString(i18n.language, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span>{eventLabel(event, t)}</span><code>{Math.round(event.progress * 100)}%</code></div>)}{!events.length ? <p>{t('simulation.job.awaitingEvents')}</p> : null}</div>
        </section>
        <section className="job-log-panel">
          <header>
            <div className="segmented-control" aria-label={t('simulation.job.logs')}><button className={logStream === 'stdout' ? 'active' : ''} onClick={() => setLogStream('stdout')}>stdout</button><button className={logStream === 'stderr' ? 'active' : ''} onClick={() => setLogStream('stderr')}>stderr</button></div>
            <label><input type="checkbox" checked={followLog} onChange={(event) => setFollowLog(event.target.checked)} />{t('simulation.job.follow')}</label>
            <a href={api.jobLogUrl(jobId, logStream)} target="_blank" rel="noreferrer" title={t('simulation.job.fullLog')} aria-label={t('simulation.job.fullLog')}><FileText size={14} /></a>
          </header>
          <pre ref={logRef}>{currentLog || t('simulation.job.noLog')}</pre>
        </section>
      </div>
      {job?.error && (job.status === 'failed' || job.status === 'canceled') ? <div className="job-error-evidence"><TerminalSquare size={15} /><span><strong>{t('simulation.job.failureEvidence')}</strong><code>{job.error}</code></span></div> : null}
      <footer className="job-control-actions">
        {active && !job?.cancel_requested ? <>
          {cancelArmed ? <button className="secondary-button" onClick={() => setCancelArmed(false)} disabled={cancelPending}>{t('common.discard')}</button> : null}
          <button className={cancelArmed ? 'commit-button' : 'secondary-button'} onClick={() => cancelArmed ? onCancel() : setCancelArmed(true)} disabled={cancelPending}><CircleStop size={15} />{t(cancelArmed ? 'simulation.job.confirmCancel' : 'simulation.job.requestCancel')}</button>
        </> : null}
        {active && job?.cancel_requested ? <button className="secondary-button" disabled><span className="spinner small" />{t('simulation.job.canceling')}</button> : null}
        {job?.status === 'failed' ? <button className="primary-button" onClick={onRetry} disabled={retryPending}><RefreshCw size={15} />{t('simulation.job.retry')}</button> : null}
      </footer>
    </div>
  </section>
}
