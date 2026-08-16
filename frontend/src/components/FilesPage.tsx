import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Box, CheckCircle2, CloudSun, Database, FileSpreadsheet, Fingerprint, Thermometer, Upload } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ComponentType, ReactNode, SVGProps } from 'react'
import { api } from '../lib/api'
import type { DatasetRecord, ProjectSettings } from '../lib/types'
import { climatePairChanged, climatePairPatch, climatePairReady } from '../lib/climatePair'
import { inputReadiness, shortHash } from '../lib/productStock'
import { useFeedback } from './FeedbackProvider'

type SettingKey = keyof Pick<ProjectSettings,
  'building_dataset_id' | 'neighbor_dataset_id' | 'tipo15_dataset_id' |
  'template_dataset_id' | 'weather_dataset_id' | 'ddy_dataset_id' |
  'stock_dataset_id' | 'microclimate_dataset_id'>

type IconType = ComponentType<SVGProps<SVGSVGElement> & { size?: number; strokeWidth?: number }>

/**
 * Run the activation half of a two-write flow and keep its failure its own.
 *
 * Registering a file and activating it are two server writes inside one
 * mutation, so a single `onError` message described whichever one the caller
 * named - always the first.  A rejected activation would then be reported as a
 * failed upload or a failed extraction, sending the user to redo work that had
 * already succeeded.  The file is on disk either way; what failed is stated.
 */
async function activationStep(name: string, write: () => Promise<unknown>): Promise<void> {
  try {
    await write()
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'the server rejected the change'
    throw new Error(`${name} is registered but could not be activated: ${reason}`, { cause: error })
  }
}

function EvidenceRows({ dataset }: { dataset: DatasetRecord | null | undefined }) {
  if (!dataset) return <p className="product-empty-note">No active file selected.</p>
  const meta = dataset.metadata ?? {}
  return <dl className="file-evidence">
    <div><dt>Snapshot</dt><dd><code title={dataset.sha256}>{shortHash(dataset.sha256, 14)}</code></dd></div>
    <div><dt>Contract</dt><dd>{meta.contract ?? dataset.verification_status ?? 'validated on import'}</dd></div>
    {meta.rows != null && <div><dt>Rows</dt><dd>{meta.rows.toLocaleString('en-GB')}</dd></div>}
    {meta.annual_rows != null && <div><dt>Annual rows</dt><dd>{meta.annual_rows.toLocaleString('en-GB')}</dd></div>}
    {meta.crs && <div><dt>CRS</dt><dd>{meta.crs}</dd></div>}
    {meta.buildings != null && <div><dt>Buildings</dt><dd>{meta.buildings.toLocaleString('en-GB')}</dd></div>}
    {meta.envelope_source && <div><dt>Envelope from</dt><dd>{meta.envelope_source === 'pinned'
      ? 'this file’s own U-values' : 'cluster → Spanish TABULA table'}</dd></div>}
    {meta.slice_name && <div><dt>Slice</dt><dd>{meta.slice_name}</dd></div>}
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

function StagedDatasetControl({ label, kind, accept, value, datasets, onSelect, onUpload, busy }: {
  label: string
  kind: DatasetRecord['kind']
  accept: string
  value: string
  datasets: DatasetRecord[]
  onSelect: (id: string) => void
  onUpload: (file: File) => void
  busy: boolean
}) {
  const options = datasets.filter((dataset) => dataset.kind === kind)
  return <div className="dataset-control">
    <label>
      <span>{label}</span>
      <select value={value} onChange={(event) => onSelect(event.target.value)} disabled={busy}>
        <option value="" disabled>Select a validated file</option>
        {options.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
      </select>
    </label>
    <label className="compact-upload">
      <Upload size={14} /> Upload
      <input type="file" accept={accept} disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0]
        if (file) onUpload(file)
        event.currentTarget.value = ''
      }} />
    </label>
  </div>
}

function DatabaseIngest({ datasets, busy, onIngest }: {
  datasets: DatasetRecord[]
  busy: boolean
  onIngest: (datasetId: string, population: number, crs: string) => void
}) {
  const options = datasets.filter((dataset) => dataset.kind === 'eu_database')
  const [selected, setSelected] = useState('')
  const [population, setPopulation] = useState('')
  const [crs, setCrs] = useState('')
  const chosen = selected || options[options.length - 1]?.id || ''
  const count = Number(population)
  const runnable = Boolean(chosen) && Number.isFinite(count) && count > 0 && crs.trim() !== ''
  return <div className="database-ingest">
    <label>
      <span>Uploaded database</span>
      <select value={chosen} onChange={(event) => setSelected(event.target.value)} disabled={busy}>
        <option value="" disabled>Upload a building database first</option>
        {options.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
      </select>
    </label>
    <div className="ingest-fields">
      <label>
        <span>Resident population</span>
        <input type="number" min="1" step="1" value={population} placeholder="e.g. 47500" disabled={busy}
          onChange={(event) => setPopulation(event.target.value)} />
      </label>
      <label>
        <span>Metric CRS</span>
        <input type="text" value={crs} placeholder="e.g. EPSG:32632" disabled={busy}
          onChange={(event) => setCrs(event.target.value)} />
      </label>
    </div>
    <button type="button" className="ghost" disabled={busy || !runnable}
      onClick={() => onIngest(chosen, count, crs.trim())}>
      {busy ? 'Working…' : 'Build stock from database'}
    </button>
    <p className="product-empty-note">The database carries footprints, heights and construction
      classes but no residents, so the population is allocated across the housing it describes.
      The CRS is the metric projection the city is measured in — areas and distances are computed
      in it. What this writes is an ordinary stock file that goes through the same contract as an
      uploaded one, and it is activated as the stock in use.</p>
  </div>
}

function FileCard({ icon: Icon, eyebrow, title, description, status, optional, children }: {
  icon: IconType; eyebrow: string; title: string; description: string; status: boolean
  optional?: boolean; children: ReactNode
}) {
  const state = status ? 'ACTIVE' : optional ? 'NOT SET' : 'REQUIRED'
  return <article className={`file-card ${status ? 'ready' : optional ? 'optional' : 'missing'}`}>
    <header>
      <div className="file-card-icon"><Icon size={20} strokeWidth={1.6} /></div>
      <div><span>{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>
      <div className="file-state">{status ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}{state}</div>
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
      // Two writes, and only the second can fail after the file is already on
      // disk.  `set_project_settings` re-validates the climate pair on every
      // write, so a half-configured climate can reject an unrelated activation
      // - and reporting that as "Upload failed" would send the user to re-upload
      // a file that is registered and listed.
      await activationStep(imported.name, () => api.updateProjectSettings(patch))
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

  const usingPrepared = Boolean(active.stock_dataset_id)
  const clearPrepared = () => activateMutation.mutate({ stock_dataset_id: null })
  // Three ways to arrive at a stock, and the third is not an activation but a
  // translation, so it cannot be derived from the active settings the way the
  // other two can.  It is a view the user opens, hence local state.
  const [source, setSource] = useState<'cadastre' | 'stock' | 'database'>(
    usingPrepared ? 'stock' : 'cadastre')
  useEffect(() => { setSource((current) => current === 'database' ? current : usingPrepared ? 'stock' : 'cadastre') },
    [usingPrepared])
  const databaseUpload = useMutation({
    mutationFn: (file: File) => api.uploadDataset('eu_database', file.name, file),
    onSuccess: async (imported) => { await refresh(); notify(`${imported.name} registered.`, 'success') },
    onError: (error) => notify(error instanceof Error ? error.message : 'Upload failed.', 'error'),
  })
  const ingestMutation = useMutation({
    mutationFn: async ({ id, population, crs }: { id: string; population: number; crs: string }) => {
      // The costlier half of the same shape: the extraction is minutes of work
      // on a large database, so labelling a failed activation "Extraction
      // failed" invites the user to run all of it again for nothing.
      const stock = await api.ingestDataset(id, { population, crs })
      await activationStep(stock.name, () => api.updateProjectSettings({ stock_dataset_id: stock.id }))
      return stock
    },
    onSuccess: async (stock) => {
      await refresh()
      setSource('stock')
      notify(`${stock.metadata?.buildings?.toLocaleString('en-GB') ?? 'The'} buildings extracted and activated.`, 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : 'Extraction failed.', 'error'),
  })
  // The EPW and the .ddy are activated together, never one at a time.  The
  // engine refuses a pair from two different stations - the gate that catches
  // a building simulated in one city with equipment sized for another - so
  // switching a field on its own always leaves a mismatched pair and is
  // rejected, which made moving off the verified climate impossible from here.
  const [weatherDraft, setWeatherDraft] = useState('')
  const [ddyDraft, setDdyDraft] = useState('')
  useEffect(() => { setWeatherDraft(settings.data?.weather_dataset_id ?? '') }, [settings.data?.weather_dataset_id])
  useEffect(() => { setDdyDraft(settings.data?.ddy_dataset_id ?? '') }, [settings.data?.ddy_dataset_id])
  const climateUpload = useMutation({
    mutationFn: ({ kind, file }: { kind: 'weather' | 'ddy'; file: File }) =>
      api.uploadDataset(kind, file.name, file),
    onSuccess: async (imported) => {
      await refresh()
      if (imported.kind === 'weather') setWeatherDraft(imported.id)
      else setDdyDraft(imported.id)
      notify(`${imported.name} validated. Activate the pair once both sides are chosen.`, 'success')
    },
    onError: (error) => notify(error instanceof Error ? error.message : 'Upload failed.', 'error'),
  })
  const cityName = settings.data?.city_name ?? ''
  const [cityDraft, setCityDraft] = useState(cityName)
  const [groundDraft, setGroundDraft] = useState('')
  const [mainsDraft, setMainsDraft] = useState('')
  // Server state is the source of truth; the drafts exist so typing does not
  // fire a write per keystroke.  Re-sync whenever the server value changes.
  useEffect(() => { setCityDraft(settings.data?.city_name ?? '') }, [settings.data?.city_name])
  useEffect(() => {
    setGroundDraft(settings.data?.ground_temperature_c?.toString() ?? '')
    setMainsDraft(settings.data?.water_mains_temperature_c?.toString() ?? '')
  }, [settings.data?.ground_temperature_c, settings.data?.water_mains_temperature_c])
  // The pair this project was verified on keeps its measured site temperatures;
  // stating them again would only risk changing the identity every published
  // Valencia run resumes and compares against.
  const usingReferenceClimate = active.weather_dataset_id?.metadata?.weather_site?.includes('VALENCIA')
    ?? false
  const commitTemperature = (field: 'ground_temperature_c' | 'water_mains_temperature_c', raw: string) => {
    const current = settings.data?.[field] ?? null
    const next = raw.trim() === '' ? null : Number(raw)
    if (next !== null && Number.isNaN(next)) { notify('Enter a temperature in °C.', 'error'); return }
    if (next !== current) activateMutation.mutate({ [field]: next } as Partial<ProjectSettings>)
  }
  const climateDraft = { weather: weatherDraft, ddy: ddyDraft, ground: groundDraft, mains: mainsDraft }
  const climatePending = climatePairChanged(climateDraft, {
    weather: settings.data?.weather_dataset_id ?? null,
    ddy: settings.data?.ddy_dataset_id ?? null,
  })
  const activateClimatePair = () => {
    const result = climatePairPatch(climateDraft)
    if (!result.ok) { notify(result.reason, 'error'); return }
    activateMutation.mutate(result.patch)
  }
  const readiness = inputReadiness(profile.data, profile.isError)
  return <div className="product-page files-page">
    <header className="product-page-header">
      <div><span>01 / INPUT CONTROL</span><h1>Files</h1><p>Validate, register and activate the inputs every stock run reads. The active set decides which city is being modelled.</p></div>
      <div className={`product-readiness ${readiness.state}`}>
        <span className="status-dot" />
        <div>
          <strong>{readiness.state === 'ready' ? 'RUN READY' : readiness.state === 'checking' ? 'CHECKING INPUTS' : 'INPUTS REQUIRED'}</strong>
          <small title={readiness.reason || undefined}>{readiness.state === 'ready'
            ? 'All engine entry points and inputs resolved'
            : readiness.state === 'checking' ? 'Reading the active input set…' : readiness.reason}</small>
        </div>
      </div>
    </header>

    <div className="product-scroll">
      <section className="product-intro-band">
        <Fingerprint size={18} /><div><strong>Verified profile</strong><code>{profile.data?.profile.fingerprint ?? 'loading…'}</code></div>
        <p>Files are copied into managed, content-addressed storage. A run resolves the active set once and records its fingerprints.</p>
      </section>

      <section className="city-band">
        <div className="city-identity">
          <label>
            <span>Active city</span>
            <input type="text" value={cityDraft} placeholder="Name this city" disabled={busy}
              onChange={(event) => setCityDraft(event.target.value)}
              onBlur={() => { if (cityDraft !== cityName) activateMutation.mutate({ city_name: cityDraft || null }) }} />
          </label>
          <p>Runs are labelled with this name. Every run also records the stock and climate
            fingerprints it read, so results from two cities can never be mistaken for each other.</p>
        </div>
        <div className="site-temperatures">
          <span className="eyebrow">Site conditions this weather file cannot supply</span>
          <div className="site-temperature-fields">
            <label>
              <span>Ground in contact with the slab (°C)</span>
              <input type="number" step="0.1" value={groundDraft} disabled={busy}
                placeholder={usingReferenceClimate ? '18.0 (verified)' : 'declare'}
                onChange={(event) => setGroundDraft(event.target.value)}
                onBlur={() => commitTemperature('ground_temperature_c', groundDraft)} />
            </label>
            <label>
              <span>Water mains (°C)</span>
              <input type="number" step="0.1" value={mainsDraft} disabled={busy}
                placeholder={usingReferenceClimate ? '10.0 (verified)' : 'derived from the EPW'}
                onChange={(event) => setMainsDraft(event.target.value)}
                onBlur={() => commitTemperature('water_mains_temperature_c', mainsDraft)} />
            </label>
          </div>
          <p>Neither can be read from an EPW. The ground figure follows the indoor regime, not the
            weather; the mains figure is derived from this file's own ground temperature at 2 m
            unless you state otherwise. Leaving them blank is only valid for the climate this
            project was verified on — any other climate must declare them rather than inherit
            values measured for Valencia.</p>
        </div>
      </section>
      <div className="file-card-grid">
        <FileCard icon={Database} eyebrow="GEOMETRY + IDENTITY" title="Building stock"
          description={usingPrepared
            ? 'A prepared file that already carries what the engine reads: identity, storeys, occupancy, dwellings and construction.'
            : 'A raw cadastre. It cannot say how much of a building is housing, so it needs the dwelling ledger below.'}
          status={Boolean(active.stock_dataset_id ?? active.building_dataset_id)}>
          <div className="stock-source-choice" role="group" aria-label="Building stock source">
            <button type="button" className={source === 'cadastre' ? 'active' : ''} disabled={busy}
              onClick={() => { setSource('cadastre'); if (usingPrepared) clearPrepared() }}>Cadastre + ledger</button>
            <button type="button" className={source === 'stock' ? 'active' : ''} disabled={busy}
              onClick={() => { setSource('stock'); if (!usingPrepared) notify('Upload or select a prepared stock file to switch.', 'info') }}>Prepared stock</button>
            <button type="button" className={source === 'database' ? 'active' : ''} disabled={busy}
              onClick={() => setSource('database')}>Building database</button>
          </div>
          {source === 'database'
            ? <>
                <label className="compact-upload">
                  <Upload size={14} /> Upload database
                  <input type="file" accept=".db,.sqlite,.sqlite3,.gpkg" disabled={busy || databaseUpload.isPending}
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) databaseUpload.mutate(file)
                      event.currentTarget.value = ''
                    }} />
                </label>
                <DatabaseIngest datasets={all} busy={busy || databaseUpload.isPending || ingestMutation.isPending}
                  onIngest={(id, population, crs) => ingestMutation.mutate({ id, population, crs })} />
                {active.stock_dataset_id && <EvidenceRows dataset={active.stock_dataset_id} />}
              </>
            : source === 'stock'
            ? <>
                <DatasetControl label="Active stock" kind="stock" accept=".gpkg,.geojson,.json" active={active.stock_dataset_id} datasets={all} setting="stock_dataset_id" activate={activate} upload={upload} busy={busy} />
                <EvidenceRows dataset={active.stock_dataset_id} />
              </>
            : <>
                <DatasetControl label="Active cadastre" kind="gis" accept=".gpkg,.shp,.geojson,.zip" active={active.building_dataset_id} datasets={all} setting="building_dataset_id" activate={activate} upload={upload} busy={busy} />
                <DatasetControl label="Or a prepared stock" kind="stock" accept=".gpkg,.geojson,.json" active={active.stock_dataset_id} datasets={all} setting="stock_dataset_id" activate={activate} upload={upload} busy={busy} />
                <EvidenceRows dataset={active.building_dataset_id} />
              </>}
        </FileCard>
        {!usingPrepared && <FileCard icon={FileSpreadsheet} eyebrow="RESIDENTIAL AREA + USE" title="Dwelling ledger" description="Per-dwelling floor, use and residential area, joined to the cadastre by parcel reference. Required only for a raw cadastre." status={Boolean(active.tipo15_dataset_id)}>
          <DatasetControl label="Active ledger" kind="tipo15" accept=".csv" active={active.tipo15_dataset_id} datasets={all} setting="tipo15_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.tipo15_dataset_id} />
        </FileCard>}
        <FileCard icon={CloudSun} eyebrow="ANNUAL + SIZING WEATHER" title="Climate pair" description="A full annual EPW plus winter and summer DDY design days; activated and validated as one pair." status={Boolean(active.weather_dataset_id && active.ddy_dataset_id)}>
          <StagedDatasetControl label="Annual EPW" kind="weather" accept=".epw" value={weatherDraft} datasets={all}
            onSelect={setWeatherDraft} onUpload={(file) => climateUpload.mutate({ kind: 'weather', file })} busy={busy || climateUpload.isPending} />
          <StagedDatasetControl label="Design days (DDY)" kind="ddy" accept=".ddy" value={ddyDraft} datasets={all}
            onSelect={setDdyDraft} onUpload={(file) => climateUpload.mutate({ kind: 'ddy', file })} busy={busy || climateUpload.isPending} />
          <button type="button" className="ghost"
            disabled={busy || climateUpload.isPending || !climatePending || !climatePairReady(climateDraft)}
            onClick={activateClimatePair}>
            {climatePending ? 'Activate climate pair' : 'Pair active'}
          </button>
          <p className="product-empty-note">Both sides are sent in one activation. An EPW and a .ddy
            from different stations are refused — that is the gate against a building simulated in
            one city with equipment sized for another — so a half-changed pair could never be
            saved. A climate this project was not verified on must also declare the ground
            temperature above.</p>
          <div className="climate-evidence-grid"><EvidenceRows dataset={active.weather_dataset_id} /><EvidenceRows dataset={active.ddy_dataset_id} /></div>
        </FileCard>
        <FileCard icon={Box} eyebrow="OPENSTUDIO SOURCE" title="PlantillaOS template" description="The library model holding every required construction, schedule and space type. Its schedules and thermostats are the operating regime the results describe." status={Boolean(active.template_dataset_id)}>
          <DatasetControl label="Active template" kind="template" accept=".osm" active={active.template_dataset_id} datasets={all} setting="template_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.template_dataset_id} />
        </FileCard>
        <FileCard icon={Thermometer} eyebrow="OPTIONAL · EVENT RUNS" title="Microclimate slice" description="A PALM temperature field. With one active, a run can be an event over the weather file's hottest week, offset per building." status={Boolean(active.microclimate_dataset_id)} optional>
          <DatasetControl label="Active slice" kind="microclimate" accept=".zip" active={active.microclimate_dataset_id} datasets={all} setting="microclimate_dataset_id" activate={activate} upload={upload} busy={busy} />
          <EvidenceRows dataset={active.microclimate_dataset_id} />
        </FileCard>
      </div>
    </div>
  </div>
}
