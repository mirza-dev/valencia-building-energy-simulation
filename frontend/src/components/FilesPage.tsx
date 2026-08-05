import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Box, CheckCircle2, CloudSun, Database, FileSpreadsheet, Fingerprint, Upload } from 'lucide-react'
import type { ComponentType, ReactNode, SVGProps } from 'react'
import { api } from '../lib/api'
import type { DatasetRecord, ProjectSettings } from '../lib/types'
import { shortHash } from '../lib/productStock'
import { useFeedback } from './FeedbackProvider'

type SettingKey = keyof Pick<ProjectSettings,
  'building_dataset_id' | 'neighbor_dataset_id' | 'tipo15_dataset_id' |
  'template_dataset_id' | 'weather_dataset_id' | 'ddy_dataset_id'>

type IconType = ComponentType<SVGProps<SVGSVGElement> & { size?: number; strokeWidth?: number }>

function EvidenceRows({ dataset }: { dataset: DatasetRecord | null | undefined }) {
  if (!dataset) return <p className="product-empty-note">No active file selected.</p>
  const meta = dataset.metadata ?? {}
  return <dl className="file-evidence">
    <div><dt>Snapshot</dt><dd><code title={dataset.sha256}>{shortHash(dataset.sha256, 14)}</code></dd></div>
    <div><dt>Contract</dt><dd>{meta.contract ?? dataset.verification_status ?? 'validated on import'}</dd></div>
    {meta.rows != null && <div><dt>Rows</dt><dd>{meta.rows.toLocaleString('en-GB')}</dd></div>}
    {meta.annual_rows != null && <div><dt>Annual rows</dt><dd>{meta.annual_rows.toLocaleString('en-GB')}</dd></div>}
    {meta.crs && <div><dt>CRS</dt><dd>{meta.crs}</dd></div>}
    <div className="file-path-row"><dt>Managed path</dt><dd title={dataset.path}>{dataset.path}</dd></div>
  </dl>
}

function DatasetControl({
  label, kind, accept, active, datasets, setting, activate, upload, busy,
}: {
  label: string
  kind: DatasetRecord['kind']
  accept: string
  active: DatasetRecord | null | undefined
  datasets: DatasetRecord[]
  setting: SettingKey
  activate: (setting: SettingKey, id: string) => void
  upload: (kind: DatasetRecord['kind'], setting: SettingKey, file: File) => void
  busy: boolean
}) {
  const options = datasets.filter((dataset) => dataset.kind === kind || (kind === 'tipo15' && dataset.kind === 'companion'))
  return <div className="dataset-control">
    <label>
      <span>{label}</span>
      <select value={active?.id ?? ''} onChange={(event) => activate(setting, event.target.value)} disabled={busy}>
        <option value="" disabled>Select a validated file</option>
        {options.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
      </select>
    </label>
    <label className="compact-upload">
      <Upload size={14} /> Upload
      <input type="file" accept={accept} disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0]
        if (file) upload(kind, setting, file)
        event.currentTarget.value = ''
      }} />
    </label>
  </div>
}

function FileCard({ icon: Icon, eyebrow, title, description, status, children }: {
  icon: IconType; eyebrow: string; title: string; description: string; status: boolean; children: ReactNode
}) {
  return <article className={`file-card ${status ? 'ready' : 'missing'}`}>
    <header>
      <div className="file-card-icon"><Icon size={20} strokeWidth={1.6} /></div>
      <div><span>{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>
      <div className="file-state">{status ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}{status ? 'ACTIVE' : 'REQUIRED'}</div>
    </header>
    {children}
  </article>
}

export default function FilesPage() {
  const queryClient = useQueryClient()
  const { notify } = useFeedback()
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: api.datasets })
  const settings = useQuery({ queryKey: ['project-settings'], queryFn: api.projectSettings })
  const profile = useQuery({ queryKey: ['stock-profile'], queryFn: api.stockProfile })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['datasets'] }),
      queryClient.invalidateQueries({ queryKey: ['project-settings'] }),
      queryClient.invalidateQueries({ queryKey: ['stock-profile'] }),
    ])
  }
  const activateMutation = useMutation({
    mutationFn: (value: Partial<ProjectSettings>) => api.updateProjectSettings(value),
    onSuccess: async () => { await refresh(); notify('Active input updated.', 'success') },
    onError: (error) => notify(error instanceof Error ? error.message : 'Activation failed.', 'error'),
  })
  const uploadMutation = useMutation({
    mutationFn: async ({ kind, setting, file }: { kind: DatasetRecord['kind']; setting: SettingKey; file: File }) => {
      const imported = await api.uploadDataset(kind, file.name, file)
      const patch: Partial<ProjectSettings> = { [setting]: imported.id }
      if (setting === 'building_dataset_id') patch.neighbor_dataset_id = imported.id
      await api.updateProjectSettings(patch)
      return imported
    },
    onSuccess: async (imported) => { await refresh(); notify(`${imported.name} validated and activated.`, 'success') },
    onError: (error) => notify(error instanceof Error ? error.message : 'Upload failed.', 'error'),
  })

  const active = settings.data?.datasets ?? {}
  const all = datasets.data ?? []
  const busy = activateMutation.isPending || uploadMutation.isPending
  const activate = (setting: SettingKey, id: string) => {
    const patch: Partial<ProjectSettings> = { [setting]: id }
    if (setting === 'building_dataset_id') patch.neighbor_dataset_id = id
    activateMutation.mutate(patch)
  }
  const upload = (kind: DatasetRecord['kind'], setting: SettingKey, file: File) =>
    uploadMutation.mutate({ kind, setting, file })

  const ready = profile.data?.missing_inputs.length === 0 && Object.values(profile.data?.entrypoints ?? {}).every(Boolean)
  return <div className="product-page files-page">
    <header className="product-page-header">
      <div><span>01 / INPUT CONTROL</span><h1>Files</h1><p>Validate, register and activate the four authoritative inputs used by every stock run.</p></div>
      <div className={`product-readiness ${ready ? 'ready' : 'blocked'}`}>
        <span className="status-dot" />
        <div><strong>{ready ? 'RUN READY' : 'INPUTS REQUIRED'}</strong><small>{profile.data?.missing_inputs.join(', ') || 'All engine entry points and inputs resolved'}</small></div>
      </div>
    </header>

    <div className="product-scroll">
      <section className="product-intro-band">
        <Fingerprint size={18} /><div><strong>Verified profile</strong><code>{profile.data?.profile.fingerprint ?? 'loading…'}</code></div>
        <p>Files are copied into managed, content-addressed storage. A run resolves the active set once and records its fingerprints.</p>
      </section>
      <div className="file-card-grid">
        <FileCard icon={Database} eyebrow="GEOMETRY + IDENTITY" title="Building GIS" description="Valencia cadastral footprints, reference IDs, storeys, cluster and district." status={Boolean(active.building_dataset_id)}>
          <DatasetControl label="Active GIS" kind="gis" accept=".gpkg,.shp,.geojson,.zip" active={active.building_dataset_id} datasets={all} setting="building_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.building_dataset_id} />
        </FileCard>
        <FileCard icon={FileSpreadsheet} eyebrow="RESIDENTIAL AREA + USE" title="Tipo15 companion" description="Cadastral 31_pc, floor, use and residential-area records joined by parcel reference." status={Boolean(active.tipo15_dataset_id)}>
          <DatasetControl label="Active Tipo15" kind="tipo15" accept=".csv" active={active.tipo15_dataset_id} datasets={all} setting="tipo15_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.tipo15_dataset_id} />
        </FileCard>
        <FileCard icon={CloudSun} eyebrow="ANNUAL + SIZING WEATHER" title="Climate pair" description="A full annual EPW plus winter and summer DDY design days; activated and validated as one pair." status={Boolean(active.weather_dataset_id && active.ddy_dataset_id)}>
          <DatasetControl label="Annual EPW" kind="weather" accept=".epw" active={active.weather_dataset_id} datasets={all} setting="weather_dataset_id" activate={activate} upload={upload} busy={busy} />
          <DatasetControl label="Design days (DDY)" kind="ddy" accept=".ddy" active={active.ddy_dataset_id} datasets={all} setting="ddy_dataset_id" activate={activate} upload={upload} busy={busy} />
          <div className="climate-evidence-grid"><EvidenceRows dataset={active.weather_dataset_id} /><EvidenceRows dataset={active.ddy_dataset_id} /></div>
        </FileCard>
        <FileCard icon={Box} eyebrow="OPENSTUDIO SOURCE" title="PlantillaOS template" description="The authoritative library model with all required constructions, schedules and space types." status={Boolean(active.template_dataset_id)}>
          <DatasetControl label="Active template" kind="template" accept=".osm" active={active.template_dataset_id} datasets={all} setting="template_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.template_dataset_id} />
        </FileCard>
      </div>
    </div>
  </div>
}
