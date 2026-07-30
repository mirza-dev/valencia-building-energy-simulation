import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, CircleStop, FileText, RefreshCw, TerminalSquare } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { JobRecord } from '../lib/types'
import { api } from '../lib/api'
import { formatDuration, jobTiming, TERMINAL_JOB_STATUSES } from '../lib/jobControl'
import { useJobStream } from '../lib/useJobStream'

export default function NeighborhoodJobControl({ jobId, job, onCancel, onRetry, onDismiss, translationRoot = 'neighborhood' }: {
  jobId: string
  job?: JobRecord
  onCancel: () => void
  onRetry: () => void
  onDismiss: () => void
  translationRoot?: 'neighborhood' | 'city' | 'lhs' | 'scenario'
}) {
  const { t } = useTranslation()
  const terminal = TERMINAL_JOB_STATUSES.has(job?.status ?? '')
  const { events, logs, connection } = useJobStream(jobId, terminal)
  const [collapsed, setCollapsed] = useState(false)
  const [cancelArmed, setCancelArmed] = useState(false)
  const [stream, setStream] = useState<'stdout' | 'stderr'>('stdout')
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (terminal) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [terminal])
  const timing = jobTiming(job, now)
  const progress = events.length ? Math.max(...events.map((event) => event.progress)) : 0
  const eventLabel = (message: string) => translationRoot === 'scenario'
    ? t(`scenario.events.${message}`, { defaultValue: message })
    : message

  return <section className={`neighborhood-job-control ${collapsed ? 'collapsed' : ''} ${terminal ? 'terminal' : ''}`}>
    <header>
      <span><i className={`connection-pip ${connection}`} />{t(`${translationRoot}.job.title`)}</span>
      <code>{job?.stage ?? 'queued'} · {Math.round(progress * 100)}%</code>
      <button className="icon-button" onClick={() => setCollapsed((value) => !value)} aria-label={t(collapsed ? 'simulation.job.expand' : 'simulation.job.collapse')}>{collapsed ? <ChevronDown size={15} /> : <ChevronUp size={15} />}</button>
    </header>
    <div className="neighborhood-job-body">
      <div className="neighborhood-job-metrics">
        <span>{t('simulation.job.elapsed')}<strong>{formatDuration(timing.elapsedSeconds)}</strong></span>
        <span>{t('simulation.job.timeout')}<strong>{formatDuration(timing.remainingSeconds)}</strong></span>
        <span>{t('simulation.job.attempt')}<strong>{job?.attempt_count ?? 0} / {job?.max_attempts ?? 2}</strong></span>
      </div>
      <div className="neighborhood-job-evidence">
        <div className="neighborhood-event-list"><div className="table-head"><span>{t(`${translationRoot}.job.events`)}</span><code>{events.length}</code></div>{events.slice(-8).map((event) => <div key={event.id} data-event-id={event.id} className={event.level}><span>{eventLabel(event.message)}</span><code>{Math.round(event.progress * 100)}%</code></div>)}</div>
        <div className="neighborhood-log"><header><div className="segmented-control"><button className={stream === 'stdout' ? 'active' : ''} onClick={() => setStream('stdout')}>stdout</button><button className={stream === 'stderr' ? 'active' : ''} onClick={() => setStream('stderr')}>stderr</button></div><a href={api.jobLogUrl(jobId, stream)} target="_blank" rel="noreferrer" aria-label={t('simulation.job.fullLog')}><FileText size={14} /></a></header><pre>{logs[stream] || t('simulation.job.noLog')}</pre></div>
      </div>
      {job?.error ? <div className="job-error-evidence"><TerminalSquare size={15} /><code>{job.error}</code></div> : null}
      <footer>
        {!terminal ? <button className={cancelArmed ? 'commit-button' : 'secondary-button'} onClick={() => cancelArmed ? onCancel() : setCancelArmed(true)}><CircleStop size={15} />{t(cancelArmed ? 'simulation.job.confirmCancel' : 'simulation.job.requestCancel')}</button> : null}
        {job?.status === 'failed' ? <button className="primary-button" onClick={onRetry}><RefreshCw size={15} />{t('simulation.job.retry')}</button> : null}
        {terminal ? <button className="secondary-button" onClick={onDismiss}>{t('simulation.job.dismiss')}</button> : null}
      </footer>
    </div>
  </section>
}
