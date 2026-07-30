import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, ChevronDown, Database, OctagonX, Save, ShieldCheck } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import type { StockInputPolicy, StockPolicyPreflight, WorkflowInputContract } from '../lib/types'

interface Props {
  workflow: 'neighborhood' | 'city'
  mode: 'project' | 'run'
  district?: string | null
  buildingRef?: string | null
  onOverrideChange?: (policy: Partial<StockInputPolicy> | null, ready: boolean) => void
}

const readable = (value: unknown) => typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—')

export default function StockInputPolicyPanel({ workflow, mode, district, buildingRef, onOverrideChange }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [enabled, setEnabled] = useState(mode === 'project')
  const [open, setOpen] = useState(mode === 'project')
  const shouldLoadContract = mode === 'project' || enabled
  const contract = useQuery({
    queryKey: ['workflow-input-policy', workflow],
    queryFn: () => api.workflowInputPolicy(workflow),
    staleTime: 60_000,
    enabled: shouldLoadContract,
  })
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: api.datasets, staleTime: 60_000, enabled: shouldLoadContract })
  const [draft, setDraft] = useState<StockInputPolicy | null>(null)
  const [review, setReview] = useState<StockPolicyPreflight | null>(null)
  const [confirmed, setConfirmed] = useState(false)

  useEffect(() => {
    if (!draft && contract.data?.requested_policy) {
      setDraft(contract.data.resolved_policy ?? contract.data.requested_policy)
    }
  }, [contract.data, draft])

  const resetReview = (next: StockInputPolicy) => {
    setDraft(next)
    setReview(null)
    setConfirmed(false)
    if (mode === 'run') onOverrideChange?.(null, false)
  }
  const set = <K extends keyof StockInputPolicy>(key: K, value: StockInputPolicy[K]) => {
    if (draft) resetReview({ ...draft, [key]: value })
  }
  const setClusterSource = (key: 'gis_dataset_id' | 'cluster_field', value: string) => {
    if (draft) resetReview({ ...draft, [key]: value, cluster_mapping: {} })
  }
  const preflight = useMutation({
    mutationFn: () => api.preflightStockInputPolicy(workflow, draft, district, buildingRef),
    onSuccess: (result) => {
      setReview(result)
      if (result.resolved_policy) setDraft(result.resolved_policy)
      setConfirmed(false)
      if (mode === 'run') onOverrideChange?.(null, false)
    },
  })
  const save = useMutation({
    mutationFn: () => api.updateStockInputPolicy(workflow, draft as StockInputPolicy),
    onSuccess: async (result) => {
      queryClient.setQueryData<WorkflowInputContract>(['workflow-input-policy', workflow], result)
      await queryClient.invalidateQueries({ queryKey: ['workflow-input-policy'] })
      setReview(null)
      setConfirmed(false)
    },
  })
  useEffect(() => {
    if (mode !== 'run') return
    if (!enabled) onOverrideChange?.(null, true)
    else if (confirmed && review?.ready && draft) {
      const override = Object.fromEntries(
        Object.keys(review.override_diff).map((key) => [key, draft[key as keyof StockInputPolicy]]),
      ) as Partial<StockInputPolicy>
      onOverrideChange?.(Object.keys(override).length ? override : null, true)
    }
    else onOverrideChange?.(null, false)
  }, [enabled, confirmed, review, draft, mode, onOverrideChange])
  useEffect(() => {
    if (mode !== 'run' || !enabled) return
    setReview(null)
    setConfirmed(false)
    onOverrideChange?.(null, false)
  }, [district, buildingRef, enabled, mode, onOverrideChange])

  const fields = review?.field_options ?? contract.data?.field_options
  const clusterTargets = review?.cluster_targets ?? contract.data?.cluster_targets ?? []
  const gisDatasets = useMemo(() => (datasets.data ?? []).filter((item) => item.kind === 'gis'), [datasets.data])
  const mapping = review?.resolved_policy?.cluster_mapping ?? draft?.cluster_mapping ?? {}
  const clusterSources = Array.from(new Set([...(fields?.cluster_values ?? []), ...Object.keys(mapping)])).sort()
  const visibleMapping = Object.fromEntries(clusterSources.map((source) => [
    source,
    mapping[source] ?? (clusterTargets.includes(source) ? source : ''),
  ]))
  const coverage = review?.coverage ?? contract.data?.coverage ?? {}
  if (mode === 'run' && !enabled) return <section className="policy-card policy-card-run" data-testid={`${workflow}-${mode}-policy`}>
    <header className="policy-card-header">
      <div><Database size={17} /><span><strong>{t('stockPolicy.title')}</strong><small>{t('stockPolicy.runSubtitle')}</small></span></div>
      <label className="policy-toggle"><input type="checkbox" checked={false} onChange={() => { setEnabled(true); setOpen(true) }} /><span>{t('stockPolicy.overrideRun')}</span></label>
    </header>
    <div className="policy-summary-line"><span><b>{t('stockPolicy.projectDefault')}</b></span><span>{t('stockPolicy.noRunOverride')}</span></div>
  </section>
  if (contract.isLoading || !draft) return <div className="policy-card policy-loading"><span className="spinner" />{t('stockPolicy.loading')}</div>

  return <section className={`policy-card ${mode === 'run' ? 'policy-card-run' : ''}`} data-testid={`${workflow}-${mode}-policy`}>
    <header className="policy-card-header">
      <div><Database size={17} /><span><strong>{t('stockPolicy.title')}</strong><small>{t(`stockPolicy.${mode}Subtitle`)}</small></span></div>
      {mode === 'run' ? <label className="policy-toggle"><input type="checkbox" checked={enabled} onChange={(event) => { setEnabled(event.target.checked); setOpen(event.target.checked); setReview(null); setConfirmed(false) }} /><span>{t('stockPolicy.overrideRun')}</span></label> : null}
      <button className="icon-button" aria-label={open ? t('stockPolicy.collapse') : t('stockPolicy.expand')} aria-expanded={open} onClick={() => setOpen((value) => !value)}><ChevronDown size={17} /></button>
    </header>
    <div className="policy-summary-line">
      <span><b>{contract.data?.source === 'project_default' ? t('stockPolicy.projectDefault') : t('stockPolicy.automaticDefault')}</b> · {contract.data?.policy_fingerprint?.slice(0, 12) ?? t('common.none')}</span>
      <span>{String(coverage.retained_buildings ?? '—')} {t('stockPolicy.buildings')} · {String(coverage.representatives ?? '—')} {t('stockPolicy.representatives')}</span>
    </div>
    {open && (enabled || mode === 'project') ? <div className="policy-editor">
      <div className="policy-grid">
        <label><span>{t('stockPolicy.gisDataset')}</span><select value={draft.gis_dataset_id} onChange={(event) => setClusterSource('gis_dataset_id', event.target.value)}>{gisDatasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}</select></label>
        <label><span>{t('stockPolicy.referenceField')}</span><select value={draft.reference_field} onChange={(event) => set('reference_field', event.target.value)}>{fields?.all_fields.map((field) => <option key={field}>{field}</option>)}</select></label>
        <label><span>{t('stockPolicy.districtField')}</span><select value={draft.district_field} onChange={(event) => set('district_field', event.target.value)}>{fields?.all_fields.map((field) => <option key={field}>{field}</option>)}</select></label>
        <label><span>{t('stockPolicy.floorsField')}</span><select value={draft.floors_field} onChange={(event) => set('floors_field', event.target.value)}>{fields?.all_fields.map((field) => <option key={field}>{field}</option>)}</select></label>
        <label><span>{t('stockPolicy.clusterField')}</span><select value={draft.cluster_field} onChange={(event) => setClusterSource('cluster_field', event.target.value)}>{fields?.all_fields.map((field) => <option key={field}>{field}</option>)}</select></label>
        <label><span>{t('stockPolicy.floorPolicy')}</span><select value={draft.floor_invalid_policy} onChange={(event) => set('floor_invalid_policy', event.target.value as StockInputPolicy['floor_invalid_policy'])}><option value="cluster_family_one">{t('stockPolicy.floor.cluster_family_one')}</option><option value="exclude_invalid">{t('stockPolicy.floor.exclude_invalid')}</option><option value="block_run">{t('stockPolicy.floor.block_run')}</option><option value="fixed_fallback">{t('stockPolicy.floor.fixed_fallback')}</option></select></label>
        {draft.floor_invalid_policy === 'fixed_fallback' ? <label><span>{t('stockPolicy.fixedFallback')}</span><input type="number" min={1} max={100} step={1} value={draft.floor_fixed_fallback ?? 1} onChange={(event) => set('floor_fixed_fallback', Number(event.target.value))} /></label> : null}
        <label><span>{t('stockPolicy.footprintMode')}</span><select value={draft.footprint_area_mode} onChange={(event) => set('footprint_area_mode', event.target.value as StockInputPolicy['footprint_area_mode'])}><option value="field">{t('stockPolicy.footprint.field')}</option><option value="geometry_epsg25830">{t('stockPolicy.footprint.geometry')}</option></select></label>
        {draft.footprint_area_mode === 'field' ? <label><span>{t('stockPolicy.footprintField')}</span><select value={draft.footprint_area_field ?? ''} onChange={(event) => set('footprint_area_field', event.target.value)}>{fields?.numeric_fields.map((field) => <option key={field}>{field}</option>)}</select></label> : null}
        <label><span>{t('stockPolicy.groundMode')}</span><select value={draft.ground_floor_mode} onChange={(event) => set('ground_floor_mode', event.target.value as StockInputPolicy['ground_floor_mode'])}><option value="tipo15_family_fallback">{t('stockPolicy.ground.tipo15_family_fallback')}</option><option value="family_default">{t('stockPolicy.ground.family_default')}</option><option value="force_unconditioned">{t('stockPolicy.ground.force_unconditioned')}</option><option value="force_conditioned">{t('stockPolicy.ground.force_conditioned')}</option></select></label>
        <label><span>{t('stockPolicy.residentialMode')}</span><select value={draft.residential_area_mode} onChange={(event) => set('residential_area_mode', event.target.value as StockInputPolicy['residential_area_mode'])}><option value="tipo15_proxy">{t('stockPolicy.residential.tipo15_proxy')}</option><option value="field_proxy">{t('stockPolicy.residential.field_proxy')}</option><option value="proxy_only">{t('stockPolicy.residential.proxy_only')}</option></select></label>
        {draft.residential_area_mode === 'field_proxy' ? <label><span>{t('stockPolicy.residentialField')}</span><select value={draft.residential_area_field ?? ''} onChange={(event) => set('residential_area_field', event.target.value)}>{fields?.numeric_fields.map((field) => <option key={field}>{field}</option>)}</select></label> : null}
      </div>
      {clusterSources.length ? <div className="cluster-mapping"><h4>{t('stockPolicy.clusterMapping')}</h4><p>{t('stockPolicy.clusterMappingNote')}</p><div>{clusterSources.map((source) => <label key={source}><code>{source}</code><select value={visibleMapping[source]} onChange={(event) => set('cluster_mapping', { ...visibleMapping, [source]: event.target.value })}><option value="" disabled>—</option><option value="__exclude__">{t('stockPolicy.exclude')}</option>{clusterTargets.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>)}</div></div> : null}
      <div className="policy-actions"><button className="secondary-button" onClick={() => preflight.mutate()} disabled={preflight.isPending}>{preflight.isPending ? t('stockPolicy.reviewing') : t('stockPolicy.review')}</button></div>
      {preflight.error instanceof Error ? <div className="page-error" role="alert"><OctagonX size={15} />{preflight.error.message}</div> : null}
      {save.error instanceof Error ? <div className="page-error" role="alert"><OctagonX size={15} />{save.error.message}</div> : null}
      {review ? <div className="policy-review" aria-live="polite">
        <div className={`policy-ready ${review.ready ? 'ready' : 'blocked'}`}>{review.ready ? <CheckCircle2 size={17} /> : <OctagonX size={17} />}<strong>{review.ready ? t('stockPolicy.ready') : t('stockPolicy.blocked')}</strong><code>{review.policy_fingerprint?.slice(0, 16) ?? '—'}</code></div>
        {review.warnings.length ? <section className="policy-issues warnings"><h4><AlertTriangle size={15} />{t('stockPolicy.warnings')}</h4><ul>{review.warnings.map((issue) => <li key={issue.code}><strong>{issue.code}</strong><span>{issue.message}</span>{issue.count != null ? <b>{issue.count}</b> : null}</li>)}</ul></section> : null}
        {review.blockers.length ? <section className="policy-issues blockers"><h4><OctagonX size={15} />{t('stockPolicy.blockers')}</h4><ul>{review.blockers.map((issue) => <li key={issue.code}><strong>{issue.code}</strong><span>{issue.message}</span></li>)}</ul></section> : null}
        {Object.keys(review.override_diff).length ? <section className="policy-diff"><h4>{t('stockPolicy.diff')}</h4><dl>{Object.entries(review.override_diff).map(([key, value]) => <div key={key}><dt>{key}</dt><dd><span>{readable(value.project_default)}</span><b>→</b><span>{readable(value.requested)}</span></dd></div>)}</dl></section> : <p className="policy-no-diff"><ShieldCheck size={15} />{t('stockPolicy.noDiff')}</p>}
        {review.ready ? <label className="policy-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>{mode === 'project' ? t('stockPolicy.confirmProject') : t('stockPolicy.confirmRun')}</span></label> : null}
        {mode === 'project' && review.ready ? <button className="primary-button" disabled={!confirmed || save.isPending} onClick={() => save.mutate()}><Save size={15} />{save.isPending ? t('stockPolicy.applying') : t('stockPolicy.applyProject')}</button> : null}
      </div> : null}
    </div> : null}
  </section>
}
