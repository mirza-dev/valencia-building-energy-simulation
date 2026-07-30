import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Boxes, Check, CircleAlert, ExternalLink, FileText, Filter, Play, RotateCcw } from 'lucide-react'
import { Link } from 'react-router-dom'
import PageHeader from './PageHeader'
import { useFeedback } from './FeedbackProvider'
import { api } from '../lib/api'
import { formatDuration } from '../lib/locale'
import type { BuildConfig } from '../lib/types'

export default function BatchPage() {
  const { t } = useTranslation()
  const { notify } = useFeedback()
  const client = useQueryClient()
  const configQuery = useQuery({ queryKey: ['config'], queryFn: api.config })
  const batches = useQuery({ queryKey: ['batches'], queryFn: api.batches, refetchInterval: 1500 })
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs })
  const [config, setConfig] = useState<BuildConfig | null>(null)
  const [name, setName] = useState(() => t('batch.defaultName'))
  const [refs, setRefs] = useState('4252702YJ2745A')
  const [preflight, setPreflight] = useState<Awaited<ReturnType<typeof api.batchPreflight>> | null>(null)
  const [assignments, setAssignments] = useState<Record<string, string>>({})
  const [geometryApprovals, setGeometryApprovals] = useState<Record<string, Array<Record<string, unknown>>>>({})
  const [selectedBatch, setSelectedBatch] = useState('')
  const [statusFilter, setStatusFilter] = useState('ALL')
  const batchDetail = useQuery({ queryKey: ['batch', selectedBatch], queryFn: () => api.batch(selectedBatch), enabled: Boolean(selectedBatch), refetchInterval: 1200 })
  useEffect(() => { if (configQuery.data && !config) setConfig(configQuery.data.default) }, [configQuery.data, config])
  useEffect(() => { if (!selectedBatch && batches.data?.[0]) setSelectedBatch(String(batches.data[0].id)) }, [batches.data, selectedBatch])
  const parsedRefs = refs.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean)
  const inspect = useMutation({
    mutationFn: () => api.batchPreflight(parsedRefs),
    onSuccess: (result) => {
      setPreflight(result)
      setAssignments(Object.fromEntries(result.items.filter((item) => item.suggested_profile).map((item) => [item.refparcela, item.suggested_profile!])))
      setGeometryApprovals(Object.fromEntries(result.items.map((item) => [item.refparcela, item.geometry_actions])))
      notify(t('batch.ready'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const create = useMutation({
    mutationFn: () => api.createBatch(name, parsedRefs, config!, assignments, geometryApprovals),
    onSuccess: (result) => {
      setSelectedBatch(result.batch_id); setPreflight(null)
      void client.invalidateQueries({ queryKey: ['batches'] })
      notify(t('batch.created'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const retry = useMutation({
    mutationFn: (jobId: string) => api.retryJob(jobId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['batch', selectedBatch] }); void client.invalidateQueries({ queryKey: ['batches'] })
      notify(t('batch.retried'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const jobs = (batchDetail.data?.jobs ?? []) as Array<Record<string, unknown>>
  const filteredJobs = useMemo(() => jobs.filter((job) => statusFilter === 'ALL' || String(job.status).toUpperCase() === statusFilter), [jobs, statusFilter])
  const pendingGeometry = Object.values(geometryApprovals).some((actions) => actions.some((action) => Boolean(action.required) && !action.approved))

  return <div className="page">
    <PageHeader eyebrow={t('batch.eyebrow')} title={t('batch.title')} subtitle={t('batch.subtitle')}
      actions={preflight ? <button className="primary-button" onClick={() => create.mutate()} disabled={!config || create.isPending || !preflight.ready || pendingGeometry} title={pendingGeometry ? t('builder.geometryApproval') : undefined}><Play size={16} />{t('batch.confirm')}</button>
        : <button className="primary-button" onClick={() => inspect.mutate()} disabled={!config || inspect.isPending || !parsedRefs.length}><Play size={16} />{t('batch.review')}</button>} />
    <div className="batch-layout">
      <section className="batch-compose">
        <div className="step-title"><Boxes size={17} /><div><span>{t('batch.new')}</span><h2>{t('batch.independent')}</h2></div></div>
        <label className="stacked-field"><span>{t('batch.name')}</span><input value={name} onChange={(event) => { setName(event.target.value); setPreflight(null) }} /></label>
        <label className="stacked-field"><span>{t('batch.refs')}</span><textarea rows={12} value={refs} onChange={(event) => { setRefs(event.target.value); setPreflight(null) }} /></label>
        <p className="source-note">{t('batch.note')}</p>
        {preflight ? <div className="batch-preflight"><div className="table-head"><span>{t('batch.profileConfirmation')}</span><code>{t('batch.buildings', { count: preflight.items.length })}</code></div>
          {preflight.items.map((item) => <div key={item.refparcela} className={item.valid ? '' : 'invalid'}><span><strong>{item.refparcela}</strong><small>{item.cluster ?? item.error ?? t('builder.noCluster')}</small></span>
            <select value={assignments[item.refparcela] ?? ''} disabled={!item.valid} onChange={(event) => setAssignments((current) => ({ ...current, [item.refparcela]: event.target.value }))} aria-label={`${item.refparcela} ${t('batch.profile')}`}>
              {configQuery.data?.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.label}</option>)}</select>
            {item.geometry_actions.filter((action) => Boolean(action.required)).map((action, actionIndex) => <label className="batch-geometry-approval" key={`${String(action.action)}-${actionIndex}`}>
              <input type="checkbox" checked={Boolean(geometryApprovals[item.refparcela]?.[actionIndex]?.approved)} onChange={(event) => setGeometryApprovals((current) => ({ ...current, [item.refparcela]: (current[item.refparcela] ?? []).map((candidate, index) => index === actionIndex ? { ...candidate, approved: event.target.checked } : candidate) }))} />
              {t('batch.approve', { action: String(action.action).replaceAll('_', ' '), delta: String(action.area_delta_pct ?? '—') })}</label>)}</div>)}</div> : null}
        {(create.error ?? inspect.error) instanceof Error ? <div className="error-message" role="alert">{(create.error ?? inspect.error as Error).message}</div> : null}
      </section>
      <section className="batch-history">
        <div className="table-head"><span>{t('batch.ledger')}</span><code>{t('batch.total', { count: batches.data?.length ?? 0 })}</code></div>
        <div className="batch-list">{batches.data?.map((batch) => {
          const total = Number(batch.total); const completed = Number(batch.completed); const failed = Number(batch.failed)
          const progress = total ? ((completed + failed) / total) * 100 : 0
          const status = String(batch.status).toLowerCase()
          return <article className={`batch-card ${selectedBatch === String(batch.id) ? 'active' : ''}`} key={String(batch.id)}><button className="batch-card-select" onClick={() => setSelectedBatch(String(batch.id))} aria-pressed={selectedBatch === String(batch.id)}><header><span><strong>{String(batch.name)}</strong><code>{String(batch.id).slice(0, 10)}</code></span><em>{t(`simulation.status.${status}`, { defaultValue: String(batch.status) })}</em></header></button><i className="batch-progress" role="progressbar" aria-label={t('batch.progress', { name: String(batch.name) })} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)}><span style={{ width: `${progress}%` }} /></i><footer><span><Check size={13} />{t('batch.complete', { count: completed })}</span><span><CircleAlert size={13} />{t('batch.failed', { count: failed })}</span><strong>{t('batch.total', { count: total })}</strong></footer></article>
        })}{batches.isLoading ? <div className="skeleton-stack"><i /><i /></div> : !batches.data?.length ? <div className="empty-state batch-empty"><Boxes size={24} /><strong>{t('batch.emptyTitle')}</strong><p>{t('batch.emptyText')}</p></div> : null}</div>
        {batchDetail.data ? <div className="batch-job-ledger"><div className="job-ledger-toolbar"><div className="table-head"><span>{t('batch.jobs')}</span><code>{String(batchDetail.data.id).slice(0, 10)}</code></div><label><Filter size={14} /><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="ALL">{t('runs.allStates')}</option><option value="COMPLETED">{t('simulation.status.completed')}</option><option value="FAILED">{t('simulation.status.failed')}</option><option value="RUNNING">{t('simulation.status.running')}</option><option value="QUEUED">{t('simulation.status.queued')}</option></select></label></div>
          <div className="ledger-scroll"><table><thead><tr><th>{t('batch.building')}</th><th>{t('batch.profile')}</th><th>{t('batch.stage')}</th><th>{t('common.duration')}</th><th>{t('batch.attempt')}</th><th>{t('common.qa')}</th><th>{t('batch.error')}</th><th>{t('common.action')}</th></tr></thead><tbody>
            {filteredJobs.map((job) => {
              const payload = job.payload as { config?: BuildConfig }
              const linkedRun = runs.data?.find((candidate) => candidate.id === job.run_id)
              return <tr key={String(job.id)}><td><code>{String(job.refparcela)}</code></td><td>{payload.config?.provenance.scenario_name ?? '—'}</td><td><span className={`job-status ${String(job.status)}`}>{t(`simulation.status.${String(job.status).toLowerCase()}`, { defaultValue: String(job.status) })}</span><small>{String(job.stage)}</small></td><td>{formatDuration(job.created_at, job.updated_at)}</td><td>{String(job.attempt_count)}/{String(job.max_attempts)}</td><td>{linkedRun ? (linkedRun.qa.all_pass ? 'PASS' : 'FAIL') : '—'}</td><td className="job-error" title={String(job.error ?? '')}>{job.error ? String(job.error) : '—'}</td><td><div className="job-actions">{job.run_id ? <Link className="icon-button" title={t('batch.openRun')} aria-label={t('batch.openRun')} to={`/runs?run=${String(job.run_id)}`}><ExternalLink size={14} /></Link> : null}<a className="icon-button" href={api.jobLogUrl(String(job.id))} target="_blank" rel="noreferrer" title={t('batch.openLog')} aria-label={t('batch.openLog')}><FileText size={14} /></a>{job.status === 'failed' ? <button className="icon-button" title={t('batch.retryFailed')} aria-label={t('batch.retryFailed')} onClick={() => retry.mutate(String(job.id))}><RotateCcw size={14} /></button> : null}</div></td></tr>
            })}</tbody></table></div>
        </div> : null}
        {(batches.error ?? batchDetail.error) instanceof Error ? <div className="error-message" role="alert">{(batches.error ?? batchDetail.error as Error).message}</div> : null}
      </section>
    </div>
  </div>
}
