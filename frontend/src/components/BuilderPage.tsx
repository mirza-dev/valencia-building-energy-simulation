import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import {
  ArrowRight, Braces, Building2, Check, CircleAlert, Database, Eye, FileCheck2,
  History, Map as MapIcon, Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Play, Save, Search, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import PageHeader from './PageHeader'
import ConfigPanel from './ConfigPanel'
import CodeTrace from './CodeTrace'
import { api, ApiError } from '../lib/api'
import { changedFields, deepClone, withOverrideRecords } from '../lib/config'
import { validateBuildConfig } from '../lib/buildConfigValidation'
import { compactRecoveryItems, isAttachedPreviewTerminal, restorePreviewDraft } from '../lib/previewRecovery'
import { useJobStream } from '../lib/useJobStream'
import { useActiveBuilding } from '../lib/activeBuilding'
import { useFeedback } from './FeedbackProvider'
import { formatDate } from '../lib/locale'
import type { BuildConfig, Profile, SourceType } from '../lib/types'

const MapPanel = lazy(() => import('./MapPanel'))
const ModelViewer = lazy(() => import('./ModelViewer'))

const stepSymbols = [
  ['validate_input_files', 'load_template'],
  ['load_buildings', 'validate_building_row'],
  ['clean_polygon', 'prepare_footprint', 'find_party_walls'],
  ['_build_layered_wall', '_build_layered_roof', '_add_facade_openings'],
  ['build_model_with_config', '_add_context_shading', 'save_model'],
]

function value(properties: Record<string, unknown> | undefined, key: string) {
  const item = properties?.[key]
  return item == null || item === '' ? '—' : String(item)
}

export default function BuilderPage() {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const { activeBuilding, setActiveBuilding } = useActiveBuilding()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [step, setStep] = useState(1)
  const [search, setSearch] = useState('')
  const [selectedRef, setSelectedRef] = useState(activeBuilding)
  const [view, setView] = useState<'map' | 'model'>('map')
  const [config, setConfig] = useState<BuildConfig | null>(null)
  const [baseline, setBaseline] = useState<BuildConfig | null>(null)
  const [rationale, setRationale] = useState('')
  const [sourceType, setSourceType] = useState<SourceType>('human_judgement')
  const [sourceRef, setSourceRef] = useState('')
  const [jobId, setJobId] = useState<string | null>(() => searchParams.get('preview'))
  const [codeSymbol, setCodeSymbol] = useState<string | null>(null)
  const [previewStale, setPreviewStale] = useState(false)
  const [geometryActions, setGeometryActions] = useState<Array<Record<string, unknown>>>([])
  const [capturedView, setCapturedView] = useState<{ state: Record<string, unknown>; png: string } | null>(null)
  const [pendingProfile, setPendingProfile] = useState<Profile | null>(null)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)
  const [canvasFullscreen, setCanvasFullscreen] = useState(false)
  const [restoredGeometry, setRestoredGeometry] = useState<Awaited<ReturnType<typeof api.validateGeometry>> | null>(null)
  const restoredJob = useRef<string | null>(null)
  const activeDiscoveryHandled = useRef(false)

  const attachPreview = useCallback((nextJobId: string | null, replace = false) => {
    setJobId(nextJobId)
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      if (nextJobId) next.set('preview', nextJobId)
      else next.delete('preview')
      return next
    }, { replace })
  }, [setSearchParams])

  useEffect(() => {
    const routeJobId = searchParams.get('preview')
    if (routeJobId !== jobId) setJobId(routeJobId)
  }, [jobId, searchParams])

  const configQuery = useQuery({ queryKey: ['config'], queryFn: api.config })
  const healthQuery = useQuery({ queryKey: ['health'], queryFn: api.health })
  const recoveryQuery = useQuery({
    queryKey: ['preview-recovery'],
    queryFn: api.recoverablePreviews,
    refetchInterval: (query) => query.state.data?.active ? 1_000 : 15_000,
  })
  const searchQuery = useQuery({
    queryKey: ['building-search', search],
    queryFn: () => api.searchBuildings(search),
    enabled: search.trim().length >= 2,
    placeholderData: (previous) => previous,
  })
  const buildingQuery = useQuery({
    queryKey: ['building', selectedRef],
    queryFn: () => api.building(selectedRef),
    enabled: Boolean(selectedRef),
  })

  useEffect(() => {
    if (!configQuery.data || config) return
    const next = deepClone(configQuery.data.default)
    next.provenance.locale = i18n.language === 'en' ? 'en' : 'tr'
    setConfig(next)
    setBaseline(deepClone(next))
  }, [configQuery.data, config, i18n.language])

  const geometryMutation = useMutation({
    mutationFn: () => api.validateGeometry(selectedRef, config!),
    onSuccess: (result) => {
      setRestoredGeometry(result)
      setGeometryActions(result.actions)
      setStep(2)
      setView('map')
    },
  })

  const previewMutation = useMutation({
    mutationFn: async () => {
      if (!config || !baseline) throw new Error('Config is not ready')
      const fields = changedFields(config, baseline)
      if (fields.length && rationale.trim().length < 3) throw new Error('Override rationale is required')
      const prepared = withOverrideRecords(config, baseline, rationale.trim(), sourceType, sourceRef.trim())
      return api.createPreview(selectedRef, prepared, geometryActions)
    },
    onSuccess: (data) => {
      attachPreview(data.job_id)
      setStep(4)
      setView('model')
      setPreviewStale(false)
      setCapturedView(null)
      void queryClient.invalidateQueries({ queryKey: ['preview-recovery'] })
      notify(t('builder.previewQueued'), 'success')
    },
  })

  const previewQuery = useQuery({
    queryKey: ['preview', jobId],
    queryFn: () => api.preview(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.job.status
      return status && ['ready', 'completed', 'failed', 'canceled'].includes(status) ? false : 800
    },
  })

  const preview = previewQuery.data
  const terminalPreview = isAttachedPreviewTerminal(preview?.job.status)
  const { events, connection } = useJobStream(jobId ?? '', terminalPreview)

  useEffect(() => {
    const active = recoveryQuery.data?.active
    if (jobId || !active || activeDiscoveryHandled.current) return
    activeDiscoveryHandled.current = true
    attachPreview(active.id, true)
    notify(t('builder.previewRestored'), 'info')
  }, [attachPreview, jobId, notify, recoveryQuery.data?.active, t])

  useEffect(() => {
    if (!preview || !configQuery.data || restoredJob.current === preview.job.id) return
    restoredJob.current = preview.job.id
    const restored = restorePreviewDraft(preview, configQuery.data.profiles)
    geometryMutation.reset()
    setSelectedRef(restored.selectedRef)
    setActiveBuilding(restored.selectedRef)
    setConfig(restored.config)
    setBaseline(restored.baseline)
    setGeometryActions(restored.geometryActions)
    setRationale(restored.rationale)
    setSourceType(restored.sourceType)
    setSourceRef(restored.sourceRef)
    setPreviewStale(false)
    setCapturedView(null)
    setStep(4)
    if (preview.scene) setView('model')
    void api.validateGeometry(restored.selectedRef, restored.config)
      .then(setRestoredGeometry)
      .catch(() => setRestoredGeometry(null))
  }, [configQuery.data, geometryMutation, preview, setActiveBuilding])

  useEffect(() => {
    const invalid = previewQuery.error instanceof ApiError
      && [404, 422].includes(previewQuery.error.status)
    if (!jobId || !invalid) return
    attachPreview(null, true)
    notify(t('builder.previewRecoveryInvalid'), 'info')
  }, [attachPreview, jobId, notify, previewQuery.error, t])

  const commitMutation = useMutation({
    mutationFn: () => api.commit(jobId!, capturedView?.state, capturedView?.png),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['runs'] })
      void queryClient.invalidateQueries({ queryKey: ['preview-recovery'] })
      void previewQuery.refetch()
      notify(t('builder.runCommitted'), 'success')
    },
  })
  const cancelMutation = useMutation({
    mutationFn: () => api.cancelJob(jobId!),
    onSuccess: () => {
      void previewQuery.refetch()
      void queryClient.invalidateQueries({ queryKey: ['preview-recovery'] })
    },
  })

  const handleConfig = useCallback((next: BuildConfig) => {
    if (config && JSON.stringify(config.geometry) !== JSON.stringify(next.geometry)) {
      geometryMutation.reset()
      setRestoredGeometry(null)
      setGeometryActions([])
      setStep(2)
      setView('map')
    }
    setConfig(next)
    if (jobId) setPreviewStale(true)
  }, [config, geometryMutation, jobId])

  const selectBuilding = (ref: string) => {
    if (ref === selectedRef) return
    setSelectedRef(ref)
    setActiveBuilding(ref)
    attachPreview(null)
    geometryMutation.reset()
    setRestoredGeometry(null)
    setGeometryActions([])
    setCapturedView(null)
    setPendingProfile(null)
    setStep(1)
    setView('map')
  }

  const applyProfile = (profile: Profile) => {
    const next = deepClone(profile.config)
    next.provenance.locale = i18n.language === 'en' ? 'en' : 'tr'
    next.provenance.scenario_name = profile.label
    setConfig(next)
    setBaseline(deepClone(next))
    setRationale('')
    setSourceRef('')
    setPreviewStale(Boolean(jobId))
    setPendingProfile(null)
    notify(t('builder.profileApplied', { profile: profile.label }), 'success')
  }

  const changedCount = useMemo(() => config && baseline ? changedFields(config, baseline).length : 0, [config, baseline])
  const validationErrors = useMemo(() => config ? validateBuildConfig(config) : {}, [config])
  const props = buildingQuery.data?.properties
  const cluster = value(props, 'cluster')
  const suggestedProfile = configQuery.data?.profiles.find((profile) => profile.id === `tabula_${cluster}`)
  const geometryResult = geometryMutation.data ?? restoredGeometry
  const geometryReady = Boolean(geometryResult?.valid && !geometryActions.some(
    (action) => Boolean(action.required) && !action.approved,
  ))
  const previewBlockers = useMemo(() => {
    const blockers: string[] = []
    if (healthQuery.data?.readiness === 'BLOCKED') blockers.push(t('builder.healthBlocked'))
    if (!geometryResult?.valid) blockers.push(t('builder.geometryMissing'))
    if (geometryResult?.valid && !geometryReady) blockers.push(t('builder.geometryApproval'))
    if (Object.keys(validationErrors).length) blockers.push(t('builder.configInvalid', { count: Object.keys(validationErrors).length }))
    if (changedCount > 0 && rationale.trim().length < 3) blockers.push(t('builder.rationaleMissing'))
    return blockers
  }, [changedCount, geometryReady, geometryResult?.valid, healthQuery.data?.readiness, rationale, t, validationErrors])
  const commitBlockers = useMemo(() => {
    if (!preview) return [t('builder.previewMissing')]
    if (preview.job.status === 'queued' || preview.job.status === 'running') return [t('builder.previewRunning')]
    if (previewStale) return [t('builder.previewChanged')]
    if (!preview.artifact_state.recoverable) return [t('builder.previewIntegrityFailed')]
    if (preview.job.status !== 'ready') return [t('builder.previewMissing')]
    return []
  }, [preview, previewStale, t])
  const canPreview = Boolean(config && previewBlockers.length === 0 && !previewMutation.isPending)
  const canCommit = preview?.job.status === 'ready' && preview.artifact_state.recoverable
    && !previewStale && !commitMutation.isPending
  const maxStep = preview ? 4 : geometryResult ? (geometryReady ? 3 : 2) : 1
  const profileChanges = pendingProfile && config ? changedFields(pendingProfile.config, config) : []
  const allRecoveryItems = recoveryQuery.data?.items ?? []
  const recoveryItems = useMemo(
    () => compactRecoveryItems(allRecoveryItems, jobId),
    [allRecoveryItems, jobId],
  )
  const supersededPreviewCount = allRecoveryItems.length - recoveryItems.length
  const detachPreview = () => {
    attachPreview(null)
    setStep(1)
    setView('map')
    setCapturedView(null)
  }
  const updateGeometryAction = (index: number, patch: Record<string, unknown>) => {
    setGeometryActions((current) => current.map((action, actionIndex) => (
      actionIndex === index ? { ...action, ...patch } : action
    )))
  }

  if (!config || !baseline || !configQuery.data) {
    return <div className="page-loading"><span className="spinner" /> {configQuery.error instanceof Error ? configQuery.error.message : t('common.loading')}</div>
  }

  return (
    <div className="page builder-page">
      <PageHeader eyebrow={t('builder.eyebrow')} title={t('builder.title')} subtitle={t('builder.subtitle')}
        actions={(
          <>
            <button className="secondary-button" onClick={() => geometryMutation.mutate()} disabled={geometryMutation.isPending}
              title={geometryMutation.isPending ? t('common.loading') : undefined}>
              <ShieldCheck size={16} /> {t('builder.validate')}
            </button>
            <button className="primary-button" onClick={() => previewMutation.mutate()} disabled={!canPreview}
              title={!canPreview ? previewBlockers[0] : undefined}>
              <Play size={16} /> {t('builder.preview')}
            </button>
            <button className="commit-button" onClick={() => commitMutation.mutate()} disabled={!canCommit}
              title={!canCommit ? commitBlockers[0] : undefined}>
              <Save size={16} /> {t('builder.commit')}
            </button>
          </>
        )} />

      <div className="scope-disclosure" data-testid="builder-stock-scope">
        <Building2 size={16} />
        <span><strong>{t('builder.scopeAny')}</strong><small>{t('builder.scopeEvidence')}</small></span>
        <code>{selectedRef}</code>
      </div>

      {previewBlockers.length ? <div className="action-guidance" role="status"><CircleAlert size={15} />
        <strong>{t('builder.blocker')}</strong><span>{previewBlockers[0]}</span>
        {previewBlockers.length > 1 ? <code>{t('builder.blockers', { count: previewBlockers.length })}</code> : null}
      </div> : null}

      {recoveryItems.length ? <section className="preview-recovery-strip" aria-label={t('builder.previewLedger')}>
        <header><History size={15} /><strong>{t('builder.previewLedger')}</strong><code>{t('builder.readyPreviews', { count: recoveryItems.filter((item) => item.status === 'ready').length })}{supersededPreviewCount ? ` · ${t('previewRecovery.superseded', { count: supersededPreviewCount })}` : ''}</code></header>
        <div className="preview-recovery-list">
          {recoveryItems.map((item) => <button key={item.id} className={jobId === item.id ? 'active' : ''}
            onClick={() => attachPreview(item.id)} disabled={!item.artifact_state.recoverable}
            data-preview-job={item.id} data-preview-status={item.status}
            aria-label={t('builder.openPreview', { name: item.scenario_name })}
            title={!item.artifact_state.recoverable ? item.artifact_state.issues.join('; ') : item.stage}>
            <i className={item.status} /><span><strong>{item.scenario_name}</strong><small>{item.refparcela} · {item.baseline_profile ?? 'custom'} · {formatDate(item.created_at, i18n.language)} · {item.id.slice(0, 8)}</small></span>
            <em>{t(`simulation.status.${item.status}`)}{item.queue_position ? ` #${item.queue_position}` : ''}</em>
          </button>)}
        </div>
        {jobId ? <button className="icon-button" onClick={detachPreview} title={t('builder.detachPreview')} aria-label={t('builder.detachPreview')}><X size={14} /></button> : null}
      </section> : null}

      <div className="workflow-strip">
        {(t('builder.steps', { returnObjects: true }) as string[]).map((label, index) => (
          <button key={label} className={`${step === index ? 'active' : ''} ${index < step ? 'done' : ''}`}
            onClick={() => setStep(index)} disabled={index > maxStep} aria-current={step === index ? 'step' : undefined}>
            <i>{index < step ? <Check size={13} /> : index + 1}</i><span>{label}</span>
          </button>
        ))}
        <div className="workflow-ref"><Building2 size={14} /><code>{selectedRef}</code></div>
      </div>

      {pendingProfile ? <div className="profile-confirmation" role="region" aria-live="polite" aria-labelledby="profile-review-title" aria-describedby="profile-review-description">
        <Sparkles size={17} /><div><strong id="profile-review-title">{t('builder.profileReview')} · {pendingProfile.label}</strong>
          <span id="profile-review-description">{t('builder.profileReviewText')} <b>{t('builder.profileChanges', { count: profileChanges.length })}</b></span>
          {profileChanges.length ? <code>{profileChanges.slice(0, 4).join(' · ')}{profileChanges.length > 4 ? ` · +${profileChanges.length - 4}` : ''}</code> : null}
        </div><button className="secondary-button" onClick={() => setPendingProfile(null)}><X size={15} />{t('common.cancel')}</button>
        <button className="primary-button" onClick={() => applyProfile(pendingProfile)}><Check size={15} />{t('builder.confirmProfile')}</button>
      </div> : null}

      <div className={`builder-workspace ${leftCollapsed ? 'left-collapsed' : ''} ${rightCollapsed ? 'right-collapsed' : ''} ${canvasFullscreen ? 'canvas-fullscreen' : ''}`}>
        <aside className="builder-left">
          {step === 0 ? (
            <div className="step-panel">
              <div className="step-title"><Database size={17} /><div><span>01</span><h2>{t('builder.dataContracts')}</h2></div></div>
              <div className={`contract-banner ${healthQuery.data?.ok ? 'pass' : 'fail'}`}>
                {healthQuery.data?.ok ? <FileCheck2 size={18} /> : <CircleAlert size={18} />}
                <div><strong>{healthQuery.data?.ok ? t('builder.environmentReady') : t('builder.healthRequired')}</strong><span>OpenStudio {healthQuery.data?.openstudio_version ?? '—'}</span></div>
              </div>
              {healthQuery.data && Object.entries(healthQuery.data.files).map(([key, item]) => (
                <div className="source-row" key={key}><span>{key}</span><code title={item.path}>{item.path.split('/').slice(-2).join('/')}</code><i className={item.ok ? 'pass' : 'fail'} /></div>
              ))}
              <button className="text-button" onClick={() => setStep(1)}>{t('builder.continueBuilding')} <ArrowRight size={15} /></button>
            </div>
          ) : null}

          {step === 1 ? (
            <div className="step-panel">
              <div className="step-title"><Building2 size={17} /><div><span>02</span><h2>{t('builder.buildingStock')}</h2></div></div>
              <div className="global-search">
                <label className="search-field"><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('builder.globalSearch')} /></label>
                {search.trim().length >= 2 ? <div className="search-results">
                  {searchQuery.isLoading ? <span>{t('builder.searching')}</span> : searchQuery.data?.items.map((item) => (
                    <button key={item.refparcela} onClick={() => { selectBuilding(item.refparcela); setSearch('') }}>
                      <strong>{item.refparcela}</strong><small>{item.cluster ?? t('builder.noCluster')} · {t('builder.floors', { count: item.floors ?? '—' })}</small>
                    </button>
                  ))}
                  {!searchQuery.isLoading && searchQuery.data?.items.length === 0 ? <span>{t('builder.noMatches')}</span> : null}
                </div> : null}
              </div>
              <div className="building-identity"><span>{t('common.selected').toLocaleUpperCase(i18n.language)}</span><strong>{selectedRef}</strong><em>{cluster}</em></div>
              <dl className="metadata-list">
                <div><dt>{t('builder.constructionYear')}</dt><dd>{value(props, 'ano_constr')}</dd></div>
                <div><dt>{t('builder.residentialFloors')}</dt><dd>{value(props, 'altura_max')}</dd></div>
                <div><dt>{t('builder.population')}</dt><dd>{value(props, 'pob_total')}</dd></div>
                <div><dt>{t('builder.baselineHeating')}</dt><dd>{value(props, 'demanda_ca')} kWh/m²</dd></div>
                <div><dt>{t('builder.interventionHeating')}</dt><dd>{value(props, 'demanda__1')} kWh/m²</dd></div>
              </dl>
              {suggestedProfile ? (
                <button className="suggestion-button" onClick={() => setPendingProfile(suggestedProfile)}>
                  <Sparkles size={15} /><span><strong>{t('builder.suggestProfile')}</strong><small>{suggestedProfile.label} · {t('builder.confirmationRequired')}</small></span>
                </button>
              ) : null}
              <button className="text-button" onClick={() => geometryMutation.mutate()}>{t('builder.validateNext')} <ArrowRight size={15} /></button>
            </div>
          ) : null}

          {step === 2 ? (
            <div className="step-panel">
              <div className="step-title"><MapIcon size={17} /><div><span>03</span><h2>{t('builder.geometryContext')}</h2></div></div>
              {geometryResult ? (
                <>
                  <div className="metric-grid">
                    <div><span>{t('builder.original')}</span><strong>{geometryResult.original_area_m2.toFixed(1)}</strong><em>m²</em></div>
                    <div><span>{t('builder.simplified')}</span><strong>{geometryResult.simplified_area_m2.toFixed(1)}</strong><em>m²</em></div>
                    <div><span>{t('builder.areaDelta')}</span><strong>{geometryResult.area_delta_pct.toFixed(3)}</strong><em>%</em></div>
                    <div><span>{t('builder.partyWall')}</span><strong>{geometryResult.party_length_m.toFixed(2)}</strong><em>m</em></div>
                  </div>
                  <div className="geometry-check">{geometryResult.valid ? <Check size={15} /> : <CircleAlert size={15} />}<span>{geometryResult.valid ? t('builder.buildable') : t('builder.unsupported')} {geometryResult.geometry_type}</span><code>{geometryResult.original_vertices} → {geometryResult.simplified_vertices} {t('builder.vertices')}</code></div>
                  <div className="geometry-check"><Check size={15} /><span>{t('builder.contextLoaded')}</span><code>{t('builder.neighbors', { count: geometryResult.neighbors.features.length })}</code></div>
                  {geometryResult.issues.map((issue) => <div className="geometry-issue" key={issue}><CircleAlert size={13} /><span>{issue}</span></div>)}
                  <div className="geometry-actions">
                    {geometryActions.map((action, index) => (
                      <div key={`${String(action.action)}-${index}`}>
                        <label><input type="checkbox" checked={Boolean(action.approved)}
                          onChange={(event) => updateGeometryAction(index, { approved: event.target.checked })} />
                          <span><strong>{String(action.action).replaceAll('_', ' ')}</strong><small>{action.area_delta_pct != null ? `${t('builder.areaDelta')} ${String(action.area_delta_pct)}%` : t('builder.recordedOperation')}</small></span>
                        </label>
                        {Array.isArray(action.parts) && action.parts.length > 1 ? (
                          <select value={Number(action.part_index)} onChange={(event) => updateGeometryAction(index, { part_index: Number(event.target.value) })}>
                            {(action.parts as Array<Record<string, unknown>>).map((part) => <option key={String(part.part_index)} value={Number(part.part_index)}>{t('builder.part', { index: Number(part.part_index) + 1 })} · {String(part.area_m2)} m²</option>)}
                          </select>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  <button className="text-button" disabled={!geometryReady} title={!geometryReady ? t('builder.geometryApproval') : undefined} onClick={() => setStep(3)}>{t('builder.reviewPhysics')} <ArrowRight size={15} /></button>
                </>
              ) : (
                <div className="empty-state"><MapIcon size={24} /><p>{t('builder.geometryEmpty')}</p></div>
              )}
              {geometryMutation.error instanceof Error ? <div className="error-message">{geometryMutation.error.message}</div> : null}
            </div>
          ) : null}

          {step === 3 ? (
            <ConfigPanel config={config} profiles={configQuery.data.profiles} changedCount={changedCount}
              validationErrors={validationErrors} onChange={handleConfig} onProfileRequest={setPendingProfile}
              rationale={rationale} onRationale={setRationale}
              sourceType={sourceType} onSourceType={setSourceType}
              sourceRef={sourceRef} onSourceRef={setSourceRef} />
          ) : null}

          {step === 4 ? (
            <div className="step-panel preview-step">
              <div className="step-title"><Eye size={17} /><div><span>05</span><h2>{t('builder.previewQa')}</h2></div></div>
              {previewStale ? <div className="stale-banner"><CircleAlert size={16} />{t('builder.stale')}</div> : null}
              {preview && !preview.artifact_state.recoverable ? <div className="preview-integrity-failure" role="alert"><CircleAlert size={16} /><div><strong>{t('builder.previewIntegrityFailed')}</strong>{preview.artifact_state.issues.map((issue) => <span key={issue}>{issue}</span>)}</div></div> : null}
              {preview?.job.status === 'queued' ? <div className="preview-queue-context">
                <span>{t('builder.queuePosition')}<strong>#{preview.queue_context.position ?? '—'} / {preview.queue_context.queued_total}</strong></span>
                {preview.queue_context.worker_busy && preview.queue_context.active_job ? <span>{t('builder.workerOccupied')}<strong>{preview.queue_context.active_job.kind} · {preview.queue_context.active_job.stage}</strong></span> : null}
              </div> : null}
              <div className="job-progress">
                <div><span><i className={`connection-pip ${connection}`} />{preview?.job.status ?? (previewMutation.isPending ? t('builder.queueing') : t('builder.notStarted'))}</span><strong>{Math.round((events.at(-1)?.progress ?? 0) * 100)}%</strong></div>
                <i><span style={{ width: `${(events.at(-1)?.progress ?? 0) * 100}%` }} /></i>
              </div>
              <div className="event-log" aria-live="polite">
                {events.map((event) => <div key={event.id} data-event-id={event.id} className={event.level}><time>{Math.round(event.progress * 100)}%</time><span>{event.message}</span></div>)}
              </div>
              {preview?.job.status === 'queued' || preview?.job.status === 'running' ? <button className="secondary-button" onClick={() => cancelMutation.mutate()} disabled={cancelMutation.isPending}>{t('builder.cancelJob')}</button> : null}
              {preview?.job.status === 'failed' ? <div className="error-message" role="alert"><strong>{t('builder.failedDuring', { stage: events.at(-1)?.message ?? t('builder.modelBuild') })}</strong><span>{preview.job.error}</span><a href={api.jobLogUrl(preview.job.id)} target="_blank" rel="noreferrer">{t('builder.openLog')}</a><button className="secondary-button" onClick={() => previewMutation.mutate()}>{t('builder.retryInputs')}</button></div> : null}
              {preview?.stats ? (
                <div className="metric-grid compact-metrics">
                  <div><span>{t('builder.surfaces')}</span><strong>{preview.scene?.surfaces.length}</strong></div>
                  <div><span>{t('builder.openings')}</span><strong>{preview.scene?.subsurfaces.length}</strong></div>
                  <div><span>{t('builder.shading')}</span><strong>{preview.scene?.shading.length}</strong></div>
                  <div><span>{t('builder.glass')}</span><strong>{String(preview.stats.window_area_m2)}</strong><em>m²</em></div>
                </div>
              ) : null}
              {(previewMutation.error ?? previewQuery.error) instanceof Error ? <div className="error-message">{(previewMutation.error ?? previewQuery.error as Error).message}</div> : null}
            </div>
          ) : null}
        </aside>

        <section className="builder-canvas">
          <div className="canvas-toolbar">
            <div className="canvas-toolbar-start"><button className="icon-button panel-toggle" onClick={() => setLeftCollapsed((current) => !current)}
              title={t('builder.toggleLeft')} aria-label={t('builder.toggleLeft')} aria-pressed={leftCollapsed}>
              {leftCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}</button><div className="segmented-control">
              <button className={view === 'map' ? 'active' : ''} onClick={() => setView('map')}><MapIcon size={15} /> {t('builder.map')}</button>
              <button className={view === 'model' ? 'active' : ''} onClick={() => setView('model')} disabled={!preview?.scene}><Eye size={15} /> {t('builder.model')}</button>
            </div></div>
            <div className="canvas-toolbar-end"><div className="canvas-status"><span className={preview?.qa?.all_pass ? 'pass' : 'neutral'} />{view === 'map' ? t('builder.vectorStock') : t('builder.exactSurfaces', { count: preview?.scene?.surfaces.length ?? 0 })}</div>
              <button className="icon-button panel-toggle" onClick={() => setRightCollapsed((current) => !current)} title={t('builder.toggleRight')} aria-label={t('builder.toggleRight')} aria-pressed={rightCollapsed}>{rightCollapsed ? <PanelRightOpen size={16} /> : <PanelRightClose size={16} />}</button>
              <button className="icon-button panel-toggle" onClick={() => setCanvasFullscreen((current) => !current)} title={canvasFullscreen ? t('builder.exitFullscreen') : t('builder.fullscreen')} aria-label={canvasFullscreen ? t('builder.exitFullscreen') : t('builder.fullscreen')} aria-pressed={canvasFullscreen}>{canvasFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>
            </div>
          </div>
          <div className="canvas-body">
            <Suspense fallback={<div className="model-empty"><span className="spinner" /><p>{t('builder.loadingWorkspace')}</p></div>}>
            {view === 'map' ? (
              <MapPanel selected={buildingQuery.data} geometry={geometryResult} onSelect={selectBuilding} />
            ) : preview?.scene ? <ModelViewer scene={preview.scene} onCapture={(state, png) => setCapturedView({ state, png })} /> : (
              <div className="model-empty"><Eye size={30} /><p>{t('builder.noModel')}</p></div>
            )}
            </Suspense>
          </div>
        </section>

        <aside className="builder-right">
          <div className="inspector-header"><span className="eyebrow">{t('builder.evidence')}</span><strong>{t('builder.qualityLedger')}</strong></div>
          <div className="qa-summary">
            <div className={`qa-orbit ${preview?.qa?.all_pass ? 'pass' : preview?.job.status === 'failed' ? 'fail' : ''}`}><span>{preview?.qa ? (preview.qa.all_pass ? 'PASS' : 'FAIL') : '—'}</span></div>
            <div><strong>{preview?.qa ? `${preview.qa.checks.filter((check) => check.status === 'pass').length}/${preview.qa.checks.length}` : t('builder.awaitingPreview')}</strong><span>{t('builder.warnings', { count: preview?.qa?.warning_count ?? 0 })}</span></div>
          </div>
          <div className="qa-list">
            {capturedView ? <div className="pass"><i><Check size={12} /></i><span>{t('builder.captureRecorded')}</span></div> : null}
            {preview?.qa?.checks.map((check) => (
              <div key={check.id} className={check.status}><i>{check.status === 'pass' ? <Check size={12} /> : <CircleAlert size={12} />}</i><span>{check.message}</span></div>
            )) ?? <p>{t('builder.qaEmpty')}</p>}
          </div>
          {preview?.scene ? (
            <div className="facade-table-wrap">
              <div className="inspector-subhead">{t('builder.facadeQa')}</div>
              <table className="facade-table"><thead><tr><th>Az.</th><th>{t('builder.target')}</th><th>{t('builder.actual')}</th><th>Δ</th></tr></thead><tbody>
                {preview.scene.facade_qa.map((facade) => <tr key={facade.azimut}><td>{facade.azimut}°</td><td>{facade.wwr_target}</td><td>{facade.wwr_real}</td><td className={Math.abs(facade.lapse_pct) > config.qa.facade_wwr_warning_pct ? 'warn' : ''}>{facade.lapse_pct}%</td></tr>)}
              </tbody></table>
            </div>
          ) : null}
          <div className="code-trace-list">
            <div className="inspector-subhead"><Braces size={13} /> {t('builder.codeTrace')}</div>
            {stepSymbols[step].map((symbol) => <button key={symbol} onClick={() => setCodeSymbol(symbol)}><code>{symbol}()</code><ArrowRight size={13} /></button>)}
          </div>
        </aside>
      </div>
      {codeSymbol ? <CodeTrace symbol={codeSymbol} onClose={() => setCodeSymbol(null)} /> : null}
    </div>
  )
}
