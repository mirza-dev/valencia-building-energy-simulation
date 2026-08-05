import type {
  BuildConfig,
  BuildingCollection,
  BuildingFeature,
  ConfigResponse,
  DatasetRecord,
  DatasetDictionary,
  FacadeQA,
  GeometryResult,
  HealthResult,
  CapabilitiesResult,
  CapabilityRevalidationPlan,
  EligibleModel,
  JobRecord,
  BuildingSearchResult,
  PreviewDetail,
  PreviewRecoveryResult,
  ProjectSettings,
  RunRecord,
  SimulationPairResponse,
  SimulationRun,
  ScenarioOptions,
  ScenarioRequest,
  NeighborhoodPreflight,
  NeighborhoodRun,
  NeighborhoodRunRequest,
  CityPreflight,
  CityMapMetrics,
  CityRun,
  StockRunOptions,
  StockRunRequest,
  StockRunSummary,
  StockComparison,
  LhsPreflight,
  LhsRun,
  LhsComparison,
  StorageCleanupCategory,
  StorageCleanupPlan,
  StorageCleanupResult,
  StorageOverview,
  SceneModel,
  ModelGraphResponse,
  ModelEditPatch,
  ModelEditSessionResponse,
  ModelEditorOptions,
  ModelPreflight,
  StockInputPolicy,
  StockPolicyPreflight,
  WorkflowInputContract,
  ProductProfile,
  ProductPreflight,
  ProductRunListItem,
  ProductRunDetail,
  ProductLedgerPage,
} from './types'
import type { FeatureCollection } from 'geojson'

export const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function absoluteApiUrl(path: string, apiBase = API_BASE, browserOrigin?: string): string {
  const origin = (browserOrigin ?? window.location.origin).replace(/\/+$/, '')
  const base = apiBase
    ? new URL(apiBase.endsWith('/') ? apiBase : `${apiBase}/`, `${origin}/`).toString().replace(/\/+$/, '')
    : origin
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${base}${normalizedPath}`
}

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    const payload = detail.detail
    if (payload && typeof payload === 'object') {
      const structured = payload as { message?: string; errors?: Array<{ row?: unknown; field?: string; code?: string; message?: string }> }
      const evidence = structured.errors?.slice(0, 8).map((item) =>
        `[${item.code ?? 'ERROR'}] row ${String(item.row ?? 'dataset')} · ${item.field ?? 'unknown'}: ${item.message ?? ''}`,
      ).join('\n')
      throw new ApiError(
        [structured.message ?? response.statusText, evidence].filter(Boolean).join('\n'),
        response.status,
      )
    }
    throw new ApiError(String(payload ?? response.statusText), response.status)
  }
  return response.json() as Promise<T>
}

async function requestText(path: string): Promise<string> {
  const response = await fetch(`${API_BASE}${path}`, { cache: 'no-store' })
  if (!response.ok) throw new ApiError(response.statusText, response.status)
  return response.text()
}

export const api = {
  health: () => request<HealthResult>('/api/health'),
  storage: () => request<StorageOverview>('/api/storage'),
  storageCleanupPlan: () => request<StorageCleanupPlan>('/api/storage/cleanup/plan', { method: 'POST' }),
  storageCleanup: (plan_token: string, categories: StorageCleanupCategory[]) =>
    request<StorageCleanupResult>('/api/storage/cleanup', {
      method: 'POST', body: JSON.stringify({ plan_token, categories }),
    }),
  archiveRun: (runId: string) => request<{
    run_id: string; archive_path: string; sha256: string; size_bytes: number; archived_at: string
  }>(`/api/storage/archive/${encodeURIComponent(runId)}`, { method: 'POST' }),
  datasets: () => request<DatasetRecord[]>('/api/datasets'),
  datasetDictionary: (datasetId: string) => request<DatasetDictionary>(
    `/api/datasets/${encodeURIComponent(datasetId)}/dictionary`,
  ),
  updateDatasetFieldNote: (datasetId: string, field: string, note: string, semantic_label: string) =>
    request<{ dataset_id: string; field_name: string; note: string; semantic_label: string; updated_at: string }>(
      `/api/datasets/${encodeURIComponent(datasetId)}/dictionary/${encodeURIComponent(field)}/note`,
      { method: 'PUT', body: JSON.stringify({ note, semantic_label }) },
    ),
  workflowInputPolicy: (workflow: 'builder' | 'simulation' | 'neighborhood' | 'city' | 'lhs') =>
    request<WorkflowInputContract>(`/api/workflows/${workflow}/input-policy`),
  preflightStockInputPolicy: (
    workflow: 'neighborhood' | 'city', input_policy_override: Partial<StockInputPolicy> | null,
    district?: string | null, buildingRef?: string | null,
  ) => request<StockPolicyPreflight>(`/api/workflows/${workflow}/input-policy/preflight`, {
    method: 'POST', body: JSON.stringify({
      input_policy_override, district: district || null, building_ref: buildingRef || null,
    }),
  }),
  updateStockInputPolicy: (workflow: 'neighborhood' | 'city', policy: StockInputPolicy) =>
    request<WorkflowInputContract>(`/api/workflows/${workflow}/input-policy`, {
      method: 'PUT', body: JSON.stringify(policy),
    }),
  uploadDataset: async (kind: string, name: string, file: File) => {
    const body = new FormData()
    body.set('kind', kind)
    body.set('name', name)
    body.set('file', file)
    const response = await fetch(`${API_BASE}/api/datasets`, { method: 'POST', body })
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail ?? response.statusText)
    return response.json() as Promise<DatasetRecord>
  },
  fieldMapDataset: (datasetId: string, mapping: { reference_field: string; floors_field: string; cluster_field: string | null }) =>
    request<DatasetRecord>(`/api/datasets/${encodeURIComponent(datasetId)}/field-map`, {
      method: 'POST', body: JSON.stringify(mapping),
    }),
  projectSettings: () => request<ProjectSettings>('/api/project/settings'),
  updateProjectSettings: (settings: Partial<Pick<ProjectSettings,
    'building_dataset_id' | 'neighbor_dataset_id' | 'tipo15_dataset_id' | 'template_dataset_id' | 'weather_dataset_id' | 'ddy_dataset_id'>>) =>
    request<ProjectSettings>('/api/project/settings', { method: 'PATCH', body: JSON.stringify(settings) }),
  stockProfile: () => request<ProductProfile>('/api/stock/profile'),
  stockDistricts: () => request<{ districts: string[] }>('/api/stock/districts'),
  stockPreflight: (payload: {
    scope: 'all' | 'district' | 'references'; district?: string; references?: string[];
    keep?: 'full' | 'summary'; workers?: number
  }) => request<ProductPreflight>('/api/stock/preflight', {
    method: 'POST', body: JSON.stringify(payload),
  }),
  stockRuns: () => request<{ runs: ProductRunListItem[] }>('/api/stock/runs'),
  stockRun: (name: string) => request<ProductRunDetail>(`/api/stock/runs/${encodeURIComponent(name)}`),
  startStockRun: (payload: {
    name: string; scope: 'all' | 'district' | 'references'; district?: string;
    references?: string[]; keep?: 'full' | 'summary'; workers?: number; resume?: boolean
  }) => request<{ started: { run: string; pid: number; started_at: number }; estimate: ProductPreflight }>('/api/stock/runs', {
    method: 'POST', body: JSON.stringify(payload),
  }),
  stopStockRun: (name: string) => request<{ stopped: boolean; resumable: boolean; run: string }>(
    `/api/stock/runs/${encodeURIComponent(name)}/stop`, { method: 'POST' },
  ),
  stockLedger: (name: string, query = '', status = '', offset = 0, limit = 100) => {
    const params = new URLSearchParams({ q: query, status, offset: String(offset), limit: String(limit) })
    return request<ProductLedgerPage>(`/api/stock/runs/${encodeURIComponent(name)}/ledger?${params}`)
  },
  stockLog: (name: string) => requestText(`/api/stock/runs/${encodeURIComponent(name)}/log`),
  stockLedgerCsvUrl: (name: string) => absoluteApiUrl(`/api/stock/runs/${encodeURIComponent(name)}/ledger.csv`),
  stockArtifactUrl: (name: string, reference: string, filename: string) => absoluteApiUrl(
    `/api/stock/runs/${encodeURIComponent(name)}/buildings/${encodeURIComponent(reference)}/${encodeURIComponent(filename)}`,
  ),
  stockExportPlan: (name: string, references?: string[]) => {
    const params = new URLSearchParams()
    references?.forEach((reference) => params.append('references', reference))
    const query = params.size ? `?${params}` : ''
    return request<{
      run: string; scope: 'full' | 'selection'; references: number | null;
      files: number; uncompressed_bytes: number; signed: boolean
    }>(`/api/stock/runs/${encodeURIComponent(name)}/export-plan${query}`)
  },
  stockExportUrl: (name: string, references?: string[]) => {
    const params = new URLSearchParams()
    references?.forEach((reference) => params.append('references', reference))
    const query = params.size ? `?${params}` : ''
    return absoluteApiUrl(`/api/stock/runs/${encodeURIComponent(name)}/export.zip${query}`)
  },
  config: () => request<ConfigResponse>('/api/config/schema'),
  buildings: (bbox: string) =>
    request<BuildingCollection>(`/api/buildings?bbox=${encodeURIComponent(bbox)}&limit=2500`),
  searchBuildings: (query: string) => request<BuildingSearchResult>(
    `/api/buildings/search?query=${encodeURIComponent(query)}&limit=20`,
  ),
  building: (ref: string) => request<BuildingFeature>(`/api/buildings/${encodeURIComponent(ref)}`),
  validateGeometry: (building_ref: string, config: BuildConfig) =>
    request<GeometryResult>('/api/geometry/validate', {
      method: 'POST',
      body: JSON.stringify({ building_ref, config }),
    }),
  createPreview: (building_ref: string, config: BuildConfig, geometry_actions: Record<string, unknown>[] = []) =>
    request<{ job_id: string; status: string }>('/api/previews', {
      method: 'POST',
      body: JSON.stringify({ building_ref, config, geometry_actions }),
    }),
  preview: (jobId: string) => request<PreviewDetail>(`/api/previews/${jobId}`),
  recoverablePreviews: () => request<PreviewRecoveryResult>('/api/previews/recoverable'),
  activePreviewJob: () => request<{ job: JobRecord | null }>('/api/previews/jobs/active'),
  commit: (jobId: string, view_state?: Record<string, unknown>, screenshot_data_url?: string) =>
    request<RunRecord>(`/api/previews/${jobId}/commit`, {
      method: 'POST', body: JSON.stringify({ view_state, screenshot_data_url }),
    }),
  cancelJob: (jobId: string) => request<JobRecord>(`/api/jobs/${jobId}/cancel`, { method: 'POST' }),
  retryJob: (jobId: string) => request<JobRecord>(`/api/jobs/${jobId}/retry`, { method: 'POST' }),
  verifyRun: (id: string) => request<{ ok: boolean; status: string; issues: string[] }>(`/api/runs/${id}/verify`, { method: 'POST' }),
  capabilities: () => request<CapabilitiesResult>('/api/capabilities'),
  refreshCapabilities: () => request<CapabilitiesResult>('/api/capabilities/refresh', { method: 'POST' }),
  capabilityRevalidationPlan: (name: string) => request<CapabilityRevalidationPlan>(
    `/api/capabilities/${encodeURIComponent(name)}/revalidation/plan`, { method: 'POST' },
  ),
  startCapabilityRevalidation: (name: string, plan_token: string) => request<JobRecord>(
    `/api/capabilities/${encodeURIComponent(name)}/revalidation`, {
      method: 'POST', body: JSON.stringify({ plan_token }),
    },
  ),
  activeCapabilityRevalidations: () => request<{ jobs: JobRecord[] }>('/api/capabilities/revalidation/active'),
  neighborhoodOptions: () => request<StockRunOptions>('/api/neighborhood/options'),
  neighborhoodPreflight: (district?: string | null) => request<NeighborhoodPreflight>(
    `/api/neighborhood/preflight?include_map=false${district ? `&district=${encodeURIComponent(district)}` : ''}`,
  ),
  neighborhoodRuns: () => request<NeighborhoodRun[]>('/api/neighborhood/runs'),
  neighborhoodRunSummaries: () => request<StockRunSummary[]>('/api/neighborhood/run-summaries'),
  compareNeighborhoods: (left: string, right: string) => request<StockComparison>(
    `/api/neighborhood/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
  ),
  neighborhoodRun: (id: string) => request<NeighborhoodRun>(`/api/neighborhood/runs/${encodeURIComponent(id)}`),
  createNeighborhoodRun: (payload?: NeighborhoodRunRequest) => request<JobRecord>('/api/neighborhood/runs', {
    method: 'POST', body: payload ? JSON.stringify(payload) : undefined,
  }),
  activeNeighborhoodJob: () => request<{ job: JobRecord | null }>('/api/neighborhood/jobs/active'),
  neighborhoodJob: (id: string) => request<JobRecord>(`/api/neighborhood/jobs/${encodeURIComponent(id)}`),
  neighborhoodMapUrl: (id: string) => `${API_BASE}/api/neighborhood/runs/${encodeURIComponent(id)}/map`,
  neighborhoodMap: (id: string) => request<FeatureCollection>(`/api/neighborhood/runs/${encodeURIComponent(id)}/map`),
  neighborhoodMapResource: (url: string) => request<FeatureCollection>(url),
  cityPreflight: () => request<CityPreflight>('/api/city/preflight'),
  cityOptions: () => request<StockRunOptions>('/api/city/options'),
  cityRuns: () => request<CityRun[]>('/api/city/runs'),
  cityRunSummaries: () => request<StockRunSummary[]>('/api/city/run-summaries'),
  compareCities: (left: string, right: string) => request<StockComparison>(
    `/api/city/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
  ),
  cityRun: (id: string) => request<CityRun>(`/api/city/runs/${encodeURIComponent(id)}`),
  createCityRun: (payload?: StockRunRequest) => request<JobRecord>('/api/city/runs', {
    method: 'POST', body: payload ? JSON.stringify(payload) : undefined,
  }),
  activeCityJob: () => request<{ job: JobRecord | null }>('/api/city/jobs/active'),
  cityJob: (id: string) => request<JobRecord>(`/api/city/jobs/${encodeURIComponent(id)}`),
  cityMapMetrics: (id: string) => request<CityMapMetrics>(
    `/api/city/runs/${encodeURIComponent(id)}/map-metrics`,
    { cache: 'no-store' },
  ),
  cityTileUrl: (id?: string) => {
    return id
      ? absoluteApiUrl(`/api/city/runs/${encodeURIComponent(id)}/tiles/{z}/{x}/{y}.mvt?schema=8`)
      : absoluteApiUrl('/api/city/map/tiles/{z}/{x}/{y}.mvt?schema=8')
  },
  lhsPreflight: () => request<LhsPreflight>('/api/lhs/preflight'),
  lhsRuns: () => request<LhsRun[]>('/api/lhs/runs'),
  lhsRun: (id: string) => request<LhsRun>(`/api/lhs/runs/${encodeURIComponent(id)}`),
  createLhsRun: () => request<JobRecord>('/api/lhs/runs', { method: 'POST' }),
  activeLhsJob: () => request<{ job: JobRecord | null }>('/api/lhs/jobs/active'),
  lhsJob: (id: string) => request<JobRecord>(`/api/lhs/jobs/${encodeURIComponent(id)}`),
  lhsCompare: (left: string, right: string) => request<LhsComparison>(
    `/api/lhs/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
  ),
  lhsFigureUrl: (id: string, name: 'histograms.png' | 'tornado.png') =>
    absoluteApiUrl(`/api/lhs/runs/${encodeURIComponent(id)}/figures/${name}`),
  job: (id: string) => request<JobRecord>(`/api/jobs/${id}`),
  runs: () => request<RunRecord[]>('/api/runs'),
  run: (id: string) => request<RunRecord>(`/api/runs/${id}`),
  scene: (id: string) => request<SceneModel>(`/api/runs/${id}/scene`),
  modelGraph: (id: string) => request<ModelGraphResponse>(`/api/models/${encodeURIComponent(id)}/graph`),
  modelScene: (id: string) => request<SceneModel>(`/api/models/${encodeURIComponent(id)}/scene`),
  modelEditorOptions: () => request<ModelEditorOptions>('/api/models/editor/options'),
  createModelEditSession: (id: string) => request<ModelEditSessionResponse>(
    `/api/models/${encodeURIComponent(id)}/session`, { method: 'POST' },
  ),
  recoverModelEditSession: (sessionId: string, token: string) => request<ModelEditSessionResponse>(
    `/api/models/session/${encodeURIComponent(sessionId)}`, {
      method: 'POST', body: JSON.stringify({ token }),
    },
  ),
  applyModelEdits: (sessionId: string, token: string, patches: ModelEditPatch[]) => request<ModelEditSessionResponse>(
    `/api/models/session/${encodeURIComponent(sessionId)}/edits`, {
      method: 'POST', body: JSON.stringify({ token, patches }),
    },
  ),
  uploadModelMeasure: async (sessionId: string, token: string, file: File, trusted: boolean) => {
    const body = new FormData()
    body.set('token', token)
    body.set('trusted', String(trusted))
    body.set('file', file)
    const response = await fetch(`${API_BASE}/api/models/session/${encodeURIComponent(sessionId)}/measures`, { method: 'POST', body })
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: unknown }
      throw new ApiError(typeof detail.detail === 'string' ? detail.detail : JSON.stringify(detail.detail), response.status)
    }
    return response.json() as Promise<ModelEditSessionResponse>
  },
  preflightModelEdit: (sessionId: string, token: string) => request<ModelPreflight>(
    `/api/models/session/${encodeURIComponent(sessionId)}/preflight`, {
      method: 'POST', body: JSON.stringify({ token }),
    },
  ),
  commitModelEdit: (sessionId: string, token: string, scenario_name?: string) => request<RunRecord>(
    `/api/models/session/${encodeURIComponent(sessionId)}/commit`, {
      method: 'POST', body: JSON.stringify({ token, scenario_name }),
    },
  ),
  discardModelEdit: (sessionId: string, token: string) => request<{ session_id: string; discarded: boolean; patch_count: number }>(
    `/api/models/session/${encodeURIComponent(sessionId)}/discard`, {
      method: 'POST', body: JSON.stringify({ token }),
    },
  ),
  compare: (left: string, right: string) =>
    request<{ left: RunRecord; right: RunRecord; differences: Array<{ field: string; left: unknown; right: unknown }>;
      evidence: Record<'left' | 'right', { verification_status: string; input_snapshots: Record<string, { snapshot_hash: string }>;
        provenance: { overrides?: unknown[] }; facade_qa: FacadeQA[]; artifacts: Array<{ name: string }> }> }>(
      `/api/runs/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
    ),
  eligibleSimulationModels: () => request<EligibleModel[]>('/api/simulations/eligible-models'),
  simulations: () => request<SimulationRun[]>('/api/simulations'),
  simulation: (id: string) => request<SimulationRun>(`/api/simulations/${id}`),
  activeSimulationJob: (parentRunId?: string) => request<{ job: JobRecord | null }>(
    `/api/simulations/jobs/active${parentRunId ? `?parent_run_id=${encodeURIComponent(parentRunId)}` : ''}`,
  ),
  simulationJob: (id: string) => request<JobRecord>(`/api/simulations/jobs/${encodeURIComponent(id)}`),
  createSimulation: (parent_run_id: string) => request<JobRecord>('/api/simulations', {
    method: 'POST', body: JSON.stringify({ parent_run_id }),
  }),
  createSimulationPair: (parent_run_id: string) => request<SimulationPairResponse>('/api/simulations/paired', {
    method: 'POST', body: JSON.stringify({ parent_run_id }),
  }),
  scenarioOptions: () => request<ScenarioOptions>('/api/scenarios/options'),
  activeScenarioJob: () => request<{ job: JobRecord | null }>('/api/scenarios/jobs/active'),
  scenarioJob: (id: string) => request<JobRecord>(`/api/scenarios/jobs/${encodeURIComponent(id)}`),
  createScenario: (payload: ScenarioRequest) => request<JobRecord>('/api/scenarios', {
    method: 'POST', body: JSON.stringify(payload),
  }),
  compareSimulations: (left: string, right: string) => request<{
    left: SimulationRun
    right: SimulationRun
    same_basis: boolean
    metrics: Record<string, { left: number | null; right: number | null; delta: number | null; percent: number | null }>
  }>(`/api/simulations/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`),
  batchPreflight: (building_refs: string[]) => request<{ ready: boolean; items: Array<{
    refparcela: string; cluster: string | null; suggested_profile: string | null; valid: boolean;
    geometry_actions: Array<Record<string, unknown>>; error: string | null
  }> }>('/api/batches/preflight', { method: 'POST', body: JSON.stringify({ building_refs }) }),
  createBatch: (name: string, building_refs: string[], config: BuildConfig,
    profile_assignments: Record<string, string>, geometry_actions_by_ref: Record<string, Array<Record<string, unknown>>>) =>
    request<{ batch_id: string; job_ids: string[] }>('/api/batches', {
      method: 'POST',
      body: JSON.stringify({ name, building_refs, config, geometry_actions: [], profile_assignments, geometry_actions_by_ref }),
    }),
  batches: () => request<Array<Record<string, unknown>>>('/api/batches'),
  batch: (id: string) => request<Record<string, unknown>>(`/api/batches/${id}`),
  code: (name: string) => request<{ name: string; file: string; start_line: number; end_line: number; sha256: string; source: string }>(
    `/api/code/symbols/${encodeURIComponent(name)}`,
  ),
  exportUrl: (id: string) => `${API_BASE}/api/runs/${id}/export`,
  artifactViewUrl: (id: string, name: string) =>
    `${API_BASE}/api/runs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`,
  eventsUrl: (id: string, cursor?: { afterId: number; stdoutOffset: number; stderrOffset: number }) => {
    const query = cursor ? `?${new URLSearchParams({
      after_id: String(cursor.afterId),
      stdout_offset: String(cursor.stdoutOffset),
      stderr_offset: String(cursor.stderrOffset),
    })}` : ''
    return `${API_BASE}/api/jobs/${id}/events${query}`
  },
  jobLogUrl: (id: string, stream: 'stdout' | 'stderr' = 'stderr') => `${API_BASE}/api/jobs/${id}/log?stream=${stream}`,
}
