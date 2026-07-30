import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Braces, Check, ChevronDown, CircleAlert, FileCode2, FlaskConical, RefreshCw, ShieldCheck, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { formatBytes } from '../lib/storage'
import type { CapabilitiesResult, CapabilityRevalidationPlan } from '../lib/types'
import { useFeedback } from './FeedbackProvider'

const shortHash = (value?: string | null) => value ? value.slice(0, 12) : '—'

export default function CapabilityDiagnostics({ data }: { data: CapabilitiesResult }) {
  const { t } = useTranslation()
  const { notify } = useFeedback()
  const client = useQueryClient()
  const [expanded, setExpanded] = useState('')
  const [plans, setPlans] = useState<Record<string, CapabilityRevalidationPlan>>({})
  const [jobId, setJobId] = useState('')
  const active = useQuery({ queryKey: ['capability-revalidation-active'], queryFn: api.activeCapabilityRevalidations, refetchInterval: 2500 })
  const job = useQuery({
    queryKey: ['job', jobId], queryFn: () => api.job(jobId), enabled: Boolean(jobId),
    refetchInterval: (query) => ['completed', 'failed', 'canceled'].includes(query.state.data?.status ?? '') ? false : 1000,
  })
  useEffect(() => {
    if (!jobId && active.data?.jobs[0]) setJobId(active.data.jobs[0].id)
  }, [active.data, jobId])
  useEffect(() => {
    if (!job.data || !['completed', 'failed', 'canceled'].includes(job.data.status)) return
    void client.invalidateQueries({ queryKey: ['capabilities'] })
    void client.invalidateQueries({ queryKey: ['capability-revalidation-active'] })
    void client.invalidateQueries({ queryKey: ['runs'] })
  }, [client, job.data])

  const refresh = useMutation({
    mutationFn: api.refreshCapabilities,
    onSuccess: (result) => {
      client.setQueryData(['capabilities'], result)
      setPlans({})
      notify(t('health.capabilityRefreshed'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const review = useMutation({
    mutationFn: api.capabilityRevalidationPlan,
    onSuccess: (plan) => setPlans((current) => ({ ...current, [plan.capability]: plan })),
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const start = useMutation({
    mutationFn: ({ name, token }: { name: string; token: string }) => api.startCapabilityRevalidation(name, token),
    onSuccess: (created) => {
      setJobId(created.id)
      client.setQueryData(['job', created.id], created)
      setPlans({})
      notify(t('health.revalidationQueued'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cancel = useMutation({
    mutationFn: () => api.cancelJob(jobId),
    onSuccess: (updated) => client.setQueryData(['job', jobId], updated),
  })
  const counts = useMemo(() => {
    const items = Object.values(data.capabilities)
    return { verified: items.filter((item) => item.runtime_ready).length, total: items.length }
  }, [data])

  return <section className="health-band capability-band capability-diagnostics" data-testid="capability-diagnostics">
    <header><Braces size={18} /><div><span>{t('health.capabilities')}</span><h2>{t('health.moduleContracts')}</h2></div><div className="capability-header-actions"><code>{counts.verified}/{counts.total}</code><button className="icon-button" title={t('health.refreshCapabilities')} aria-label={t('health.refreshCapabilities')} onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw size={15} className={refresh.isPending ? 'spin' : ''} /></button></div></header>
    {jobId && job.data && ['queued', 'running'].includes(job.data.status) ? <div className="capability-live-job" role="status"><span className="spinner" /><span><strong>{t('health.revalidationRunning')}</strong><small>{job.data.kind.toUpperCase()} · {job.data.stage}</small></span><code>{job.data.attempt_count}/{job.data.max_attempts}</code><button className="icon-button" onClick={() => cancel.mutate()} title={t('common.cancel')}><X size={14} /></button></div> : null}
    <div className="capability-register">{Object.entries(data.capabilities).map(([name, item]) => {
      const diagnostic = item.diagnostic
      const open = expanded === name
      const plan = plans[name]
      const state = diagnostic.state
      const activeFeatures = diagnostic.features.filter((feature) => feature.available)
      return <article key={name} className={`capability-module state-${state}`} data-capability={name} data-capability-state={state}>
        <button className="capability-summary" onClick={() => setExpanded(open ? '' : name)} aria-expanded={open}>
          <i className={item.runtime_ready ? 'pass' : state === 'source_changed' || state === 'dependency_blocked' ? 'warn' : 'fail'}>{item.runtime_ready ? <Check size={13} /> : <CircleAlert size={13} />}</i>
          <span><strong>{name}<b>Part {item.part}</b></strong><small title={t(`health.capabilityState.${state}`)}>{t(`health.capabilityState.${state}`)}</small></span>
          <code>{shortHash(diagnostic.source.current_sha256)}</code><ChevronDown size={15} />
        </button>
        {open ? <div className="capability-detail">
          <div className="capability-source-evidence"><FileCode2 size={15} /><dl><div><dt>{t('health.currentSource')}</dt><dd><code>{shortHash(diagnostic.source.current_sha256)}</code></dd></div><div><dt>{t('health.verifiedSource')}</dt><dd><code>{shortHash(diagnostic.source.expected_sha256)}</code></dd></div><div><dt>{t('health.lastEvidence')}</dt><dd>{diagnostic.latest_verified_evidence?.verified_at ? new Date(diagnostic.latest_verified_evidence.verified_at).toLocaleString() : '—'}</dd></div></dl><small title={diagnostic.source.path}>{diagnostic.source.path}</small></div>
          <div className="capability-check-grid">{Object.entries(item.contract?.checks ?? { callable: item.inspection.ok }).map(([check, passed]) => <div key={check}><i className={passed ? 'pass' : 'fail'}>{passed ? <Check size={11} /> : <X size={11} />}</i><span>{t(`health.capabilityCheck.${check}`, { defaultValue: check.replaceAll('_', ' ') })}</span></div>)}</div>
          <div className="capability-features"><span>{t('health.detectedFeatures')}</span><div>{activeFeatures.map((feature) => <span key={feature.key} title={feature.evidence}><ShieldCheck size={11} />{t(`health.capabilityFeature.${feature.key}`, { defaultValue: feature.key.replaceAll('_', ' ') })}</span>)}</div></div>
          {diagnostic.revalidation_supported && !item.runtime_ready ? <div className="revalidation-workspace">
            {!plan ? <button className="secondary-button" onClick={() => review.mutate(name)} disabled={review.isPending || Boolean(jobId && job.data && ['queued', 'running'].includes(job.data.status))}><FlaskConical size={15} />{t('health.reviewRevalidation')}</button> : <>
              <div className="revalidation-plan"><span><strong>{t('health.fullBaselineValidation')}</strong><small>{t('health.revalidationEvidence')}</small></span><dl><div><dt>{t('common.duration')}</dt><dd>~{Math.ceil((plan.duration_seconds ?? 0) / 60)} min</dd></div><div><dt>{t('health.diskReservation')}</dt><dd>{formatBytes(plan.disk_bytes ?? 0)}</dd></div><div><dt>{t('health.blockingChecks')}</dt><dd>{plan.blocking_checks?.length ? plan.blocking_checks.join(', ') : t('common.none')}</dd></div></dl></div>
              <div className="revalidation-actions"><button className="secondary-button" onClick={() => setPlans((current) => { const next = { ...current }; delete next[name]; return next })}>{t('common.cancel')}</button><button className="primary-button" disabled={!plan.eligible || !plan.plan_token || start.isPending} onClick={() => start.mutate({ name, token: plan.plan_token! })}><FlaskConical size={15} />{plan.eligible ? t('health.startRevalidation') : t('health.revalidationBlocked')}</button></div>
            </>}
          </div> : null}
        </div> : null}
      </article>
    })}</div>
  </section>
}
