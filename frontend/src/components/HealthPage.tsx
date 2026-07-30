import { useEffect, useMemo, useState, type KeyboardEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Archive, Check, CircleAlert, Database, FileArchive, FolderUp, GitMerge, HardDrive, RefreshCw, Settings2, ShieldCheck, ThermometerSun, Trash2, X } from 'lucide-react'
import PageHeader from './PageHeader'
import CapabilityDiagnostics from './CapabilityDiagnostics'
import DataDictionaryPanel from './DataDictionaryPanel'
import InputContractsPanel from './InputContractsPanel'
import { useFeedback } from './FeedbackProvider'
import { api } from '../lib/api'
import { cleanupCategoryKeys, formatBytes, readinessClass } from '../lib/storage'
import type { ProjectSettings, StorageCleanupCategory, StorageCleanupPlan } from '../lib/types'

type ActiveField = 'building_dataset_id' | 'neighbor_dataset_id' | 'template_dataset_id' | 'weather_dataset_id'
const activeFields: Array<[ActiveField, string, 'gis' | 'template' | 'weather']> = [
  ['building_dataset_id', 'health.buildingGis', 'gis'],
  ['neighbor_dataset_id', 'health.contextGis', 'gis'],
  ['template_dataset_id', 'health.template', 'template'],
  ['weather_dataset_id', 'health.weather', 'weather'],
]
type HealthTab = 'overview' | 'datasets' | 'dictionary' | 'inputs'
const healthTabs: HealthTab[] = ['overview', 'datasets', 'dictionary', 'inputs']

export default function HealthPage() {
  const { t, i18n } = useTranslation()
  const { notify } = useFeedback()
  const queryClient = useQueryClient()
  const health = useQuery({ queryKey: ['health'], queryFn: api.health })
  const storage = useQuery({ queryKey: ['storage'], queryFn: api.storage })
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities, refetchInterval: 5000 })
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: api.datasets })
  const settings = useQuery({ queryKey: ['project-settings'], queryFn: api.projectSettings })
  const runs = useQuery({ queryKey: ['runs'], queryFn: api.runs })
  const [draftSettings, setDraftSettings] = useState<Partial<ProjectSettings>>({})
  const [kind, setKind] = useState('gis')
  const [name, setName] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [mappingDatasetId, setMappingDatasetId] = useState('')
  const [referenceField, setReferenceField] = useState('')
  const [floorsField, setFloorsField] = useState('')
  const [clusterField, setClusterField] = useState('')
  const [cleanupPlan, setCleanupPlan] = useState<StorageCleanupPlan | null>(null)
  const [cleanupCategories, setCleanupCategories] = useState<StorageCleanupCategory[]>([])
  const [archiveRunId, setArchiveRunId] = useState('')
  const [healthTab, setHealthTab] = useState<HealthTab>('overview')

  useEffect(() => {
    if (!settings.data) return
    setDraftSettings(Object.fromEntries(activeFields.map(([field]) => [field, settings.data[field]])))
  }, [settings.data?.updated_at])
  useEffect(() => {
    const firstVerified = runs.data?.find((run) => run.verification_status === 'VERIFIED')
    if (!archiveRunId && firstVerified) setArchiveRunId(firstVerified.id)
  }, [archiveRunId, runs.data])

  const upload = useMutation({
    mutationFn: () => api.uploadDataset(kind, name || file?.name || 'Managed dataset', file!),
    onSuccess: () => {
      setFile(null); setName('')
      void queryClient.invalidateQueries({ queryKey: ['datasets'] })
      notify(t('health.datasetRegistered'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const gisDatasets = useMemo(() => datasets.data?.filter((dataset) => dataset.kind === 'gis') ?? [], [datasets.data])
  const mappingDataset = gisDatasets.find((dataset) => dataset.id === mappingDatasetId)
  const columns = mappingDataset?.metadata.columns ?? []
  useEffect(() => { if (!mappingDatasetId && gisDatasets[0]) setMappingDatasetId(gisDatasets[0].id) }, [gisDatasets, mappingDatasetId])
  useEffect(() => {
    if (!columns.length) return
    setReferenceField(columns.includes('refparcela') ? 'refparcela' : columns[0])
    setFloorsField(columns.includes('altura_max') ? 'altura_max' : columns.find((field) => /floor|altura|plant/i.test(field)) ?? '')
    setClusterField(columns.includes('cluster') ? 'cluster' : '')
  }, [mappingDatasetId, columns.join('|')])

  const pendingChanges = useMemo(() => activeFields.flatMap(([field, label]) => {
    const current = settings.data?.[field] ?? null
    const next = (draftSettings[field] as string | null | undefined) ?? null
    return current === next ? [] : [{ field, label, current, next }]
  }), [draftSettings, settings.data])
  const datasetName = (id: string | null) => datasets.data?.find((dataset) => dataset.id === id)?.name ?? t('common.notSelected')
  const activate = useMutation({
    mutationFn: () => api.updateProjectSettings(Object.fromEntries(pendingChanges.map((item) => [item.field, item.next]))),
    onSuccess: (result) => {
      setDraftSettings(Object.fromEntries(activeFields.map(([field]) => [field, result[field]])))
      void queryClient.invalidateQueries({ queryKey: ['project-settings'] })
      void queryClient.invalidateQueries({ queryKey: ['health'] })
      void queryClient.invalidateQueries({ queryKey: ['config'] })
      void queryClient.invalidateQueries({ queryKey: ['buildings'] })
      notify(t('health.inputsApplied'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const discardDraft = () => setDraftSettings(Object.fromEntries(activeFields.map(([field]) => [field, settings.data?.[field] ?? null])))
  const fieldMap = useMutation({
    mutationFn: () => api.fieldMapDataset(mappingDatasetId, { reference_field: referenceField, floors_field: floorsField, cluster_field: clusterField || null }),
    onSuccess: (normalized) => {
      void queryClient.invalidateQueries({ queryKey: ['datasets'] })
      setMappingDatasetId(normalized.id)
      notify(t('health.mappingCreated'), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })

  const planCleanup = useMutation({
    mutationFn: api.storageCleanupPlan,
    onSuccess: (plan) => {
      setCleanupPlan(plan)
      setCleanupCategories(cleanupCategoryKeys(plan))
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const cleanStorage = useMutation({
    mutationFn: () => api.storageCleanup(cleanupPlan!.plan_token, cleanupCategories),
    onSuccess: (result) => {
      setCleanupPlan(null)
      setCleanupCategories([])
      void queryClient.invalidateQueries({ queryKey: ['storage'] })
      void queryClient.invalidateQueries({ queryKey: ['health'] })
      notify(t('health.cleanupCompleted', { value: formatBytes(result.estimated_freed_bytes) }), 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })
  const archiveRun = useMutation({
    mutationFn: () => api.archiveRun(archiveRunId),
    onSuccess: (result) => notify(t('health.archiveCompleted', { value: formatBytes(result.size_bytes) }), 'success'),
    onError: (error) => notify(error instanceof Error ? error.message : t('common.fail'), 'error'),
  })

  if (health.isLoading || storage.isLoading || capabilities.isLoading || datasets.isLoading || settings.isLoading || runs.isLoading) return <div className="page-loading"><span className="spinner" />{t('common.loading')}</div>
  const pageError = health.error ?? storage.error ?? capabilities.error ?? datasets.error ?? settings.error ?? runs.error
  const verifiedRuns = runs.data?.filter((run) => run.verification_status === 'VERIFIED') ?? []
  const storageData = storage.data
  const selectHealthTab = (tab: HealthTab) => setHealthTab(tab)
  const handleHealthTabKey = (event: KeyboardEvent<HTMLButtonElement>, tab: HealthTab) => {
    const index = healthTabs.indexOf(tab)
    let next: number
    if (event.key === 'ArrowRight') next = (index + 1) % healthTabs.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + healthTabs.length) % healthTabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = healthTabs.length - 1
    else return
    event.preventDefault()
    selectHealthTab(healthTabs[next])
    document.getElementById(`health-tab-${healthTabs[next]}`)?.focus()
  }

  return (
    <div className="page">
      <PageHeader eyebrow={t('health.eyebrow')} title={t('health.title')} subtitle={t('health.subtitle')}
        actions={<div className={`health-chip large ${readinessClass(health.data?.readiness)}`}><span className="status-dot" />{health.data?.readiness ?? t('common.checking').toLocaleUpperCase(i18n.language)}</div>} />
      {pageError instanceof Error ? <div className="page-error" role="alert"><CircleAlert size={16} />{pageError.message}</div> : null}
      <div className="health-local-nav" role="tablist" aria-label={t('health.sectionsLabel')}>{healthTabs.map((tab) => <button key={tab} id={`health-tab-${tab}`} role="tab" aria-selected={healthTab === tab} aria-controls={`health-panel-${tab}`} tabIndex={healthTab === tab ? 0 : -1} className={healthTab === tab ? 'active' : ''} onClick={() => selectHealthTab(tab)} onKeyDown={(event) => handleHealthTabKey(event, tab)}>{t(`health.tabs.${tab}`)}</button>)}</div>
      <div className="health-layout" id={`health-panel-${healthTab}`} role="tabpanel" aria-labelledby={`health-tab-${healthTab}`}>
        {healthTab === 'overview' ? <>
        <section className={`health-band storage-band storage-${storageData?.status.toLowerCase()}`} data-testid="storage-workspace">
          <header><HardDrive size={18} /><div><span>{t('health.storage')}</span><h2>{t('health.storageTitle')}</h2></div><strong className={`storage-status ${readinessClass(storageData?.status)}`}>{storageData?.status}</strong></header>
          <div className="storage-capacity-row">
            <div className="capacity-figure"><strong>{Math.round((storageData?.capacity.used_ratio ?? 0) * 100)}%</strong><span>{t('health.used')}</span></div>
            <div className="capacity-track-wrap">
              <div className="capacity-track" role="progressbar" aria-label={t('health.diskUsage')} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((storageData?.capacity.used_ratio ?? 0) * 100)}><span style={{ width: `${Math.min(100, (storageData?.capacity.used_ratio ?? 0) * 100)}%` }} /></div>
              <div className="capacity-copy"><strong>{t('health.capacityFree', { free: formatBytes(storageData?.capacity.free_bytes ?? 0), total: formatBytes(storageData?.capacity.total_bytes ?? 0) })}</strong><small>{t('health.reservePolicy', { reserve: formatBytes(storageData?.capacity.reserve_floor_bytes ?? 0), warning: formatBytes(storageData?.capacity.warning_free_bytes ?? 0) })}</small></div>
            </div>
            <dl className="storage-policy"><div><dt>{t('health.activeReservations')}</dt><dd>{formatBytes(storageData?.reservations.total_bytes ?? 0)}</dd></div><div><dt>{t('health.tracked')}</dt><dd>{formatBytes(storageData?.tracked_bytes ?? 0)}</dd></div><div><dt>{t('health.policy')}</dt><dd>{storageData?.policy_version}</dd></div></dl>
          </div>

          <div className="storage-ledgers">
            <div className="storage-ledger">
              <div className="storage-subhead"><span>{t('health.storageInventory')}</span><code>{Object.keys(storageData?.categories ?? {}).length}</code></div>
              <div className="storage-table">{Object.entries(storageData?.categories ?? {}).map(([key, item]) => <div key={key}><span><strong>{t(`health.storageCategory.${key}`, { defaultValue: key.replaceAll('_', ' ') })}</strong><small>{item.file_count} {t('health.files')}</small></span><code>{formatBytes(item.size_bytes)}</code></div>)}</div>
            </div>
            <div className="storage-ledger">
              <div className="storage-subhead"><span>{t('health.jobAdmission')}</span><code>{t('health.protectedFloor', { value: formatBytes(storageData?.capacity.reserve_floor_bytes ?? 0) })}</code></div>
              <div className="storage-table admission-table">{Object.entries(storageData?.job_admissions ?? {}).map(([key, item]) => <div key={key}><i className={item.allowed ? 'pass' : 'fail'}>{item.allowed ? <Check size={12} /> : <CircleAlert size={12} />}</i><span><strong>{key}</strong><small>{item.allowed ? t('health.admissionReady') : t('health.admissionBlocked')} · {formatBytes(item.estimated_bytes)}</small></span><code>{formatBytes(Math.max(0, item.available_after_bytes))}</code></div>)}</div>
            </div>
          </div>

          <div className="storage-actions-row">
            <div className="cleanup-workspace">
              <div className="storage-action-copy"><Trash2 size={16} /><span><strong>{t('health.safeCleanup')}</strong><small>{t('health.safeCleanupNote')}</small></span></div>
              {!cleanupPlan ? <button className="secondary-button" onClick={() => planCleanup.mutate()} disabled={planCleanup.isPending}><RefreshCw size={15} />{t('health.reviewCleanup')}</button> : <div className="cleanup-review" role="region" aria-label={t('health.cleanupReview')}>
                <div className="cleanup-evidence"><ShieldCheck size={15} /><span>{t('health.cleanupProtected', cleanupPlan.protected)}</span><strong>{formatBytes(cleanupPlan.reclaimable_bytes)}</strong></div>
                <div className="cleanup-options">{cleanupCategoryKeys(cleanupPlan).map((category) => {
                  const summary = cleanupPlan.categories[category]
                  return <label key={category}><input type="checkbox" checked={cleanupCategories.includes(category)} onChange={() => setCleanupCategories((current) => current.includes(category) ? current.filter((item) => item !== category) : [...current, category])} /><span><strong>{t(`health.cleanupCategory.${category}`)}</strong><small>{summary?.count ?? 0} · {formatBytes(summary?.size_bytes ?? 0)}</small></span></label>
                })}</div>
                <div className="cleanup-confirm"><span>{cleanupPlan.pending_object_snapshots ? t('health.gcPending', { count: cleanupPlan.pending_object_snapshots }) : t('health.noGcPending')}</span><button className="secondary-button" onClick={() => { setCleanupPlan(null); setCleanupCategories([]) }}><X size={14} />{t('common.cancel')}</button><button className="danger-button" onClick={() => cleanStorage.mutate()} disabled={!cleanupCategories.length || cleanStorage.isPending}><Trash2 size={14} />{t('health.confirmCleanup')}</button></div>
              </div>}
            </div>
            <div className="archive-workspace">
              <div className="storage-action-copy"><Archive size={16} /><span><strong>{t('health.externalArchive')}</strong><small>{storageData?.archive.configured ? storageData.archive.path : t('health.archiveNotConfigured')}</small></span></div>
              <select value={archiveRunId} onChange={(event) => setArchiveRunId(event.target.value)} disabled={!storageData?.archive.configured} aria-label={t('health.archiveRun')}><option value="">{t('common.notSelected')}</option>{verifiedRuns.map((run) => <option key={run.id} value={run.id}>{run.run_type} · {run.refparcela} · {run.id.slice(0, 8)}</option>)}</select>
              <button className="secondary-button" disabled={!storageData?.archive.configured || !archiveRunId || archiveRun.isPending} onClick={() => archiveRun.mutate()}><Archive size={15} />{t('health.createArchive')}</button>
            </div>
          </div>
        </section>

        <section className="health-band environment-band">
          <header><HardDrive size={18} /><div><span>{t('health.runtime')}</span><h2>{t('health.local')}</h2></div></header>
          <div className="runtime-figure"><strong>{health.data?.openstudio_version ?? '—'}</strong><span>OpenStudio SDK</span></div>
          <dl className="health-paths"><div><dt>{t('health.builder')}</dt><dd>{health.data?.python_builder}</dd></div><div><dt>{t('health.database')}</dt><dd>{health.data?.database}</dd></div></dl>
          <div className="contract-list health-system-checks">
            {health.data && Object.entries(health.data.checks).map(([key, item]) => <div key={key}><i className={key === 'disk' && item.status === 'WARNING' ? 'warn' : item.ok ? 'pass' : 'fail'}>{item.ok && item.status !== 'WARNING' ? <Check size={13} /> : <CircleAlert size={13} />}</i>
              <span><strong>{key.replaceAll('_', ' ')}</strong><small>{key === 'disk' ? t('health.free', { value: (Number(item.free_bytes ?? 0) / 1024 ** 3).toFixed(1) }) : item.ok ? t('health.contractPassed') : String(item.error ?? item.message ?? t('health.checkFailed'))}</small></span></div>)}
          </div>
        </section>

        <section className="health-band template-band">
          <header><ThermometerSun size={18} /><div><span>{t('health.openstudio')}</span><h2>{t('health.bindings')}</h2></div></header>
          <div className="contract-list">{health.data && Object.entries(health.data.template.required).map(([key, item]) => <div key={key}><i className={item.ok ? 'pass' : 'fail'}>{item.ok ? <Check size={13} /> : <CircleAlert size={13} />}</i><span><strong>{key.replace('_', ' ')}</strong><small>{item.ok ? t('health.objectsPresent') : item.missing.join(', ')}</small></span></div>)}</div>
        </section>

        {capabilities.data ? <CapabilityDiagnostics data={capabilities.data} /> : null}
        </> : null}

        {healthTab === 'datasets' ? <>
        <section className="health-band">
          <header><Database size={18} /><div><span>{t('health.inputs')}</span><h2>{t('health.registered')}</h2></div></header>
          <div className="contract-list">
            {health.data && Object.entries(health.data.files).map(([key, item]) => <div key={key}><i className={item.ok ? 'pass' : 'fail'}>{item.ok ? <Check size={13} /> : <CircleAlert size={13} />}</i><span><strong>{key}</strong><code title={item.path}>{item.path}</code><small>{item.snapshot_hash?.slice(0, 16) ?? t('common.noHash')} · {item.components ?? 0} {t('common.components')}{item.error ? ` · ${item.error}` : ''}</small></span></div>)}
          </div>
          <div className="active-dataset-grid">
            {activeFields.map(([field, label, datasetKind]) => <label key={field}><span>{t(label)}</span><select value={(draftSettings[field] as string | null | undefined) ?? ''}
              onChange={(event) => setDraftSettings((current) => ({ ...current, [field]: event.target.value || null }))}>
              <option value="">{t('common.notSelected')}</option>{datasets.data?.filter((dataset) => dataset.kind === datasetKind).map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
            </select></label>)}
          </div>
          {pendingChanges.length ? <div className="dataset-activation-review" role="region" aria-label={t('health.pendingTitle')}>
            <div className="activation-copy"><Settings2 size={16} /><span><strong>{t('health.pendingTitle')}</strong><small>{t('health.pendingText')}</small></span></div>
            <dl>{pendingChanges.map((item) => <div key={item.field}><dt>{t(item.label)}</dt><dd><span>{datasetName(item.current)}</span><b>→</b><strong>{datasetName(item.next)}</strong></dd></div>)}</dl>
            <div className="activation-actions"><button className="secondary-button" onClick={discardDraft}><X size={15} />{t('common.discard')}</button><button className="primary-button" onClick={() => activate.mutate()} disabled={activate.isPending}><Check size={15} />{t('health.applyInputs')}</button></div>
          </div> : null}
        </section>

        <section className="health-band managed-band">
          <header><FileArchive size={18} /><div><span>{t('health.contentAddressed')}</span><h2>{t('health.managed')}</h2></div></header>
          <div className="dataset-table">{datasets.data?.map((dataset) => <div key={dataset.id}><span><strong>{dataset.name}</strong><small>{dataset.kind}{dataset.metadata.normalized ? ` · ${t('health.normalized')}` : ''}</small></span><code>{dataset.sha256.slice(0, 12)}</code></div>)}</div>
          <form className="upload-row" onSubmit={(event) => { event.preventDefault(); if (file) upload.mutate() }}>
            <select value={kind} onChange={(event) => setKind(event.target.value)} aria-label={t('health.datasetType')}><option value="gis">GIS</option><option value="template">Template</option><option value="weather">Weather</option></select>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder={t('health.datasetName')} aria-label={t('health.datasetName')} />
            <label className="file-picker"><FolderUp size={15} /><span>{file?.name ?? t('health.chooseFile')}</span><input type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
            <button className="secondary-button" disabled={!file || upload.isPending}>{t('health.register')}</button>
          </form>
        </section>

        <section className="health-band mapping-band">
          <header><GitMerge size={18} /><div><span>{t('health.schema')}</span><h2>{t('health.mapping')}</h2></div></header>
          <p className="band-note">{t('health.mappingNote')}</p>
          <div className="mapping-form">
            <label><span>{t('health.sourceGis')}</span><select value={mappingDatasetId} onChange={(event) => setMappingDatasetId(event.target.value)}>{gisDatasets.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}</select></label>
            <label><span>{t('health.buildingId')}</span><select value={referenceField} onChange={(event) => setReferenceField(event.target.value)}><option value="">{t('health.selectField')}</option>{columns.map((field) => <option key={field}>{field}</option>)}</select></label>
            <label><span>{t('health.floors')}</span><select value={floorsField} onChange={(event) => setFloorsField(event.target.value)}><option value="">{t('health.selectField')}</option>{columns.map((field) => <option key={field}>{field}</option>)}</select></label>
            <label><span>{t('health.cluster')}</span><select value={clusterField} onChange={(event) => setClusterField(event.target.value)}><option value="">{t('health.notMapped')}</option>{columns.map((field) => <option key={field}>{field}</option>)}</select></label>
          </div>
          <div className="mapping-footer"><span>{mappingDataset ? `${mappingDataset.metadata.rows ?? '—'} ${t('common.rows')} · ${mappingDataset.metadata.crs ?? t('common.unknownCrs')}` : t('health.selectDataset')}</span>
            <button className="secondary-button" disabled={!mappingDatasetId || !referenceField || !floorsField || fieldMap.isPending} onClick={() => fieldMap.mutate()}><Settings2 size={15} />{t('health.normalize')}</button></div>
          {(fieldMap.error ?? activate.error ?? upload.error) instanceof Error ? <div className="error-message" role="alert">{(fieldMap.error ?? activate.error ?? upload.error as Error).message}</div> : null}
        </section>
        </> : null}

        {healthTab === 'dictionary' ? <DataDictionaryPanel datasets={datasets.data ?? []} activeDatasetId={settings.data?.building_dataset_id ?? null} /> : null}
        {healthTab === 'inputs' ? <InputContractsPanel /> : null}
      </div>
    </div>
  )
}
