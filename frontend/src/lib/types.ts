export type SourceType = 'human_judgement' | 'dataset' | 'publication' | 'supervisor' | 'other'

export interface OverrideRecord {
  field: string
  reason: string
  source_type: SourceType
  source_ref: string | null
}

export interface BuildConfig {
  data: {
    building_path: string
    neighbor_path: string
    template_path: string
    epw_path: string
    output_root: string
  }
  geometry: {
    floor_height_m: number
    ground_unconditioned: boolean
    neighbor_assume_ground: boolean
    simplify_tolerance_m: number
    max_area_delta_fraction: number
    footprint_min_m2: number
    footprint_max_m2: number
    party_wall_tolerance_m: number
    min_shared_edge_m: number
    party_overlap_ratio: number
    context_radius_m: number
  }
  envelope: {
    wall_u: number | null
    roof_u: number | null
    window_u: number
    window_g: number
    thermal_bridge_du: number
    massless: boolean
  }
  openings: {
    wwr_north: number
    wwr_east: number
    wwr_south: number
    wwr_west: number
    window_width_m: number
    window_height_m: number
    window_sill_m: number
    door_width_m: number
    door_height_m: number
    door_sill_m: number
    balcony_doors_per_facade_floor: number
    balcony_depth_m: number
  }
  shading: {
    context_enabled: boolean
    blind_name: string
    setpoint_w_m2: number
    summer_start_month: number
    summer_end_month: number
    max_shadow_figures: number
  }
  operation: {
    infiltration_ach: number | null
    output_variables: Record<string, string>
  }
  qa: {
    facade_wwr_warning_pct: number
  }
  provenance: {
    baseline_profile: string
    scenario_name: string
    locale: 'tr' | 'en'
    overrides: OverrideRecord[]
  }
}

export interface Profile {
  id: string
  label: string
  source: string
  config: BuildConfig
}

export interface ConfigResponse {
  schema: Record<string, unknown>
  default: BuildConfig
  profiles: Profile[]
  metadata_labels: Record<string, Record<'tr' | 'en', string>>
  read_only_system_fields: string[]
}

export interface GeometryResult {
  valid: boolean
  ready: boolean
  requires_approval: boolean
  issues: string[]
  geometry_type: string
  original_area_m2: number
  simplified_area_m2: number
  area_delta_pct: number
  original_vertices: number
  simplified_vertices: number
  original: Geometry
  simplified: Geometry
  neighbors: FeatureCollection
  party_geometry: Geometry | null
  party_length_m: number
  actions: Array<Record<string, unknown>>
}

export interface DatasetRecord {
  id: string
  kind: 'gis' | 'template' | 'weather' | 'companion' | 'tipo15' | 'ddy'
    | 'stock' | 'eu_database' | 'microclimate'
  name: string
  path: string
  sha256: string
  snapshot_hash?: string | null
  verification_status?: string
  read_only: number
  created_at?: string
  metadata: {
    managed?: boolean
    normalized?: boolean
    rows?: number
    crs?: string
    columns?: string[]
    geometry_types?: string[]
    field_mapping?: Record<string, string | null>
    inspection_error?: string
    contract?: string
    buildings?: number
    envelope_source?: 'pinned' | 'tabula_es'
    has_district_column?: boolean
    slice_name?: string
    coverage_note?: string
    weather_site?: string
    annual_rows?: number
    winter_design_days?: number
    summer_design_days?: number
    chosen_heating_design_day?: string
    chosen_cooling_design_day?: string
    required_roles?: string[]
    available_roles?: string[]
  }
}

export type DictionaryEffect = 'physical_model' | 'stock_method' | 'scaling' | 'validation_only' | 'reporting_only' | 'not_used'

export interface LocalizedCopy {
  en: string
  tr: string
}

export interface DictionaryColumn {
  field: string
  meaning: LocalizedCopy
  scope: LocalizedCopy | null
  source: string
  definition_status: string
  effect: DictionaryEffect
  workflows: Array<'builder' | 'simulation' | 'neighborhood' | 'city' | 'lhs'>
  inferred_type: 'numeric' | 'text' | 'geometry' | 'boolean' | 'date'
  row_count: number
  present_count: number
  present_pct: number
  usable_count: number
  usable_pct: number
  missing_count: number
  invalid_count: number
  zero_count: number
  coverage_basis: string
  validity_rule: LocalizedCopy
  evidence: { dataset_median?: number; author_reference?: number; unit?: string }
  user_note: string
  user_semantic_label: string
  note_updated_at: string | null
}

export interface DatasetDictionary {
  schema_version: number
  registry_version: string
  dataset: {
    id: string
    name: string
    kind: 'gis' | 'companion'
    snapshot_hash: string
    rows: number
  }
  columns: DictionaryColumn[]
  companion_links: Array<{
    dataset_id: string
    name: string
    source: string
    join: {
      available: boolean
      reason?: string
      coverage_basis?: string
      building_rows?: number
      residential_area?: { joined_count: number; joined_pct: number }
      ground_rule?: { joined_count: number; joined_pct: number }
    }
  }>
}

export interface WorkflowInputItem {
  key: string
  label: string
  value: string
  effect: 'physical_model' | 'stock_method' | 'scaling'
  detail: string
  source: string
}

export interface WorkflowInputContract {
  schema_version: number
  phase: number
  workflow: 'builder' | 'simulation' | 'neighborhood' | 'city' | 'lhs'
  summary: string
  status: string
  locked: boolean
  inputs: WorkflowInputItem[]
  passive_fields: Array<{
    field: string
    effect: DictionaryEffect
    meaning: LocalizedCopy
    definition_status: string
  }>
  project_dataset_ids: Record<string, string>
  editor_link: string
  registry_version?: string
  source?: 'automatic_default' | 'project_default' | 'run_override'
  project_revision?: number | null
  requested_policy?: StockInputPolicy
  resolved_policy?: StockInputPolicy | null
  policy_fingerprint?: string | null
  dataset?: { id: string; name: string; path: string; snapshot_hash?: string | null } | null
  tipo15_dataset?: { id: string; name: string; path: string; snapshot_hash?: string | null } | null
  field_options?: StockPolicyFieldOptions
  cluster_targets?: string[]
  coverage?: Record<string, unknown>
  warnings?: StockPolicyIssue[]
  blockers?: StockPolicyIssue[]
  ready?: boolean
  override_diff?: Record<string, { project_default: unknown; requested: unknown }>
}

export interface StockInputPolicy {
  schema_version: number
  gis_dataset_id: string
  reference_field: string
  district_field: string
  footprint_area_mode: 'field' | 'geometry_epsg25830'
  footprint_area_field: string | null
  floors_field: string
  cluster_field: string
  cluster_mapping: Record<string, string>
  floor_invalid_policy: 'cluster_family_one' | 'exclude_invalid' | 'block_run' | 'fixed_fallback'
  floor_fixed_fallback: number | null
  ground_floor_mode: 'tipo15_family_fallback' | 'family_default' | 'force_unconditioned' | 'force_conditioned'
  residential_area_mode: 'tipo15_proxy' | 'field_proxy' | 'proxy_only'
  residential_area_field: string | null
}

export interface StockPolicyFieldOptions {
  all_fields: string[]
  numeric_fields: string[]
  text_fields: string[]
  geometry_area_available: boolean
  cluster_values: string[]
}

export interface StockPolicyIssue { code: string; message: string; count?: number }

export interface StockPolicyPreflight {
  schema_version: number
  registry_version: string
  workflow: 'neighborhood' | 'city'
  source: 'automatic_default' | 'project_default' | 'run_override'
  project_revision: number | null
  requested_policy: StockInputPolicy
  resolved_policy: StockInputPolicy | null
  policy_fingerprint: string | null
  dataset: { id: string; name: string; path: string; snapshot_hash?: string | null } | null
  tipo15_dataset: { id: string; name: string; path: string; snapshot_hash?: string | null } | null
  field_options: StockPolicyFieldOptions
  cluster_targets: string[]
  coverage: Record<string, unknown>
  warnings: StockPolicyIssue[]
  blockers: StockPolicyIssue[]
  ready: boolean
  override_diff: Record<string, { project_default: unknown; requested: unknown }>
}

export interface ProjectSettings {
  project_id: string
  building_dataset_id: string | null
  neighbor_dataset_id: string | null
  template_dataset_id: string | null
  weather_dataset_id: string | null
  tipo15_dataset_id: string | null
  ddy_dataset_id: string | null
  stock_dataset_id: string | null
  microclimate_dataset_id: string | null
  city_name: string | null
  // The two site values no weather file can supply.  Held per project so an
  // activated climate records what was declared for this city rather than
  // inheriting the values verified for Valencia.
  ground_temperature_c: number | null
  water_mains_temperature_c: number | null
  updated_at: string
  datasets: Record<string, DatasetRecord | null>
}

export interface ProductProfile {
  profile: { fingerprint: string; source_hashes: Record<string, string> }
  inputs: {
    gis: string | null; tipo15: string | null; stock: string | null
    microclimate: string | null; climate: string | null; template: string | null
  }
  prepared_stock?: boolean
  missing_inputs: string[]
  entrypoints: Record<string, boolean>
}

export interface ProductPreflight {
  ok: boolean
  missing_inputs?: string[]
  scope?: string
  district?: string | null
  buildings_in_scope?: number
  runnable?: number
  excluded?: number
  exclusion_reasons?: Record<string, number>
  estimated_minutes?: number
  estimated_seconds_per_building?: number
  estimated_rate_basis?: string
  estimated_bytes?: number
  policy_fingerprint?: string | null
  stock_source_fingerprint?: string | null
  profile?: { fingerprint: string; source_hashes: Record<string, string> }
}

export interface ProductRunListItem {
  run: string
  modified: number
  has_summary: boolean
  buildings: number
  running: boolean
}

export interface ProductProgress {
  run: string
  started: boolean
  counts?: Record<string, number>
  completed?: number
  cpu_seconds?: number
}

export interface ProductClusterSummary {
  cluster: string
  buildings: number
  residential_area_m2: number
  total_site_gwh: number
  area_weighted_kwh_m2: number
  rai_consume_kwh_m2?: number
  cadastral_area_m2?: number
  cadastral_kwh_m2?: number
  vs_rai_pct?: number
}

export interface ProductStockSummary {
  buildings_ok: number
  buildings_failed: number
  buildings_failed_qa: number
  buildings_excluded: number
  coverage: {
    buildings_in_scope: number
    buildings_with_result: number
    buildings_without_result: number
    building_coverage_pct: number
    footprint_coverage_pct: number
    note?: string
  }
  qa_failed: number
  unexplained_severes: number
  implausible_occupancy: number
  // What period the energy figures cover.  An annual run and a microclimate
  // event run fill the same fields, so the totals below cannot be labelled
  // `/ year` unconditionally.  Absent on runs finished before it was recorded,
  // and those were annual.
  energy_period?: {
    period: 'annual' | 'microclimate_event' | 'mixed' | 'no_rows'
    unit: string | null
    event_days?: number | null
    event_window?: string | null
    note?: string
  } | null
  totals: {
    heating_gwh: number
    cooling_gwh: number
    dhw_gwh: number
    total_site_gwh: number
    residential_area_m2: number
    area_weighted_total_site_kwh_m2: number
    cadastral_total_site_kwh_m2: number
    carbon_total_site_t_yr: number
    tipo15_residential_area_m2: number
    area_basis?: string
    area_basis_note?: string
  }
  by_cluster: ProductClusterSummary[]
  // Absent from every run finished before the layer existed, which is why the
  // download is offered on `written` rather than on the run being complete.
  results_layer?: {
    written: boolean
    features?: number
    crs?: string | null
    reason?: string
    context_fields?: string[]
    derived_fields?: string[]
    zone_layers?: Record<string, number>
    style?: { written: boolean; field?: string; breaks?: number[]; reason?: string }
    heatmap?: {
      written: boolean
      panels?: string[]
      buildings_without_result?: number
      reason?: string
    }
  }
  provenance?: Record<string, unknown>
}

export interface ProductScope {
  runnable: number
  excluded: number
  total: number
}

export interface ProductRunDetail {
  progress: ProductProgress
  running: boolean
  summary: ProductStockSummary | null
  // A running tally carries the same field names as a final one, so `summary`
  // alone cannot say which it is.  Optional: a response cached before these
  // fields existed must not break the page.
  summary_is_partial?: boolean
  scope?: ProductScope | null
}

export interface ProductLedgerRow extends Record<string, unknown> {
  refparcela: string
  status: string
  cluster?: string
  total_site_kwh_m2?: number
  total_site_kwh?: number
  total_site_co2_t_yr?: number
  occupancy_plausibility?: string
  qa_all_passed?: boolean
  error?: string
  reason?: string
  message?: string
}

export interface ProductLedgerPage {
  run: string
  total: number
  offset: number
  limit: number
  items: ProductLedgerRow[]
}

export type BuildingFeature = Feature<Geometry, Record<string, unknown>> & { id: string }

export interface BuildingCollection extends FeatureCollection<Geometry, Record<string, unknown>> {
  total: number
  truncated: boolean
}

export interface BuildingSearchResult {
  total: number
  items: Array<{ refparcela: string; cluster: string | null; floors: number | null; center: [number, number] }>
}

export interface SceneItem {
  id: string
  parent_id?: string
  name: string
  category: 'surface' | 'window' | 'door' | 'context' | 'overhang'
  vertices: number[][]
  surface_type?: string
  subsurface_type?: string
  boundary_condition?: string
  construction?: string | null
  construction_id?: string | null
  area_m2: number
  azimuth_deg?: number
  space?: string | null
  space_id?: string | null
  space_type?: string | null
  space_type_id?: string | null
  zone?: string | null
  zone_id?: string | null
  story?: string | null
  story_id?: string | null
  group_type?: string | null
  context_role?: 'wall' | 'roof'
}

export interface SceneModel {
  schema_version: number
  refparcela: string
  origin_epsg25830: [number, number]
  north_axis_deg: number
  surfaces: SceneItem[]
  subsurfaces: SceneItem[]
  shading: SceneItem[]
  facade_qa: FacadeQA[]
}

export interface ModelGraphReference {
  id: string
  name: string
}

export interface ModelGraphObject extends ModelGraphReference {
  type: string
  [key: string]: unknown
}

export interface ModelConstruction extends ModelGraphObject {
  layers: ModelGraphReference[]
  u_factor_w_m2k: number | null
  surface_usage: { surface_count: number; area_m2: number; surface_types: Record<string, number> }
}

export interface ModelMaterial extends ModelGraphObject {
  properties: Record<string, string | number | boolean | null>
}

export interface ModelSchedule extends ModelGraphObject {
  type_limits: (ModelGraphObject & { lower: number | null; upper: number | null; numeric_type: string | null; unit_type: string | null }) | null
  profiles: Array<ModelGraphObject & { role: string; points: Array<{ hour: number; value: number }> }>
  rules: Array<ModelGraphObject & { start: { month: number; day: number } | null; end: { month: number; day: number } | null; days: string[] }>
  value?: number | null
}

export interface ModelProjectParameter extends ModelGraphObject {
  key: string
  current_value: string | number | boolean | null
  unit: string | null
  value_kind: 'number' | 'boolean'
  warn_bounds: { minimum: number; maximum: number } | null
  binding: {
    status: 'bound' | 'run_setting'
    object_id: string | null
    object_name: string | null
    object_type: string | null
    property_path: string
  }
  evidence: Record<string, unknown>
  read_only: true
}

export interface ModelSpaceType extends ModelGraphObject {
  load_summary: Record<string, number | null>
  infiltration: Array<ModelGraphObject & {
    design_flow_rate_m3_s: number | null
    flow_per_floor_area_m3_s_m2: number | null
    flow_per_exterior_area_m3_s_m2: number | null
    flow_per_exterior_wall_area_m3_s_m2: number | null
    air_changes_per_hour: number | null
  }>
  outdoor_air: (ModelGraphObject & {
    flow_per_person_m3_s: number | null
    flow_per_floor_area_m3_s_m2: number | null
    flow_rate_m3_s: number | null
    air_changes_per_hour: number | null
  }) | null
}

export interface ModelHvacComponent extends ModelGraphObject {
  index: number
  side: 'supply' | 'demand'
  node: boolean
  editable: boolean
  properties: Record<string, { value: number | null; autosized: boolean; unit: string }>
}

export interface ModelHvacLoop extends ModelGraphObject {
  kind: 'air' | 'plant'
  supply_components: ModelHvacComponent[]
  demand_components: ModelHvacComponent[]
  zones: ModelGraphReference[]
  sizing: Record<string, string | number | boolean | null>
}

export interface ModelMeasureArgument {
  name: string
  display_name: string
  description: string
  type: string
  units: string | null
  required: boolean
  model_dependent: boolean
  default_value: string | null
  choices: Array<{ value: string; display_name: string }>
}

export interface ModelMeasure {
  id: string
  name: string
  display_name: string
  description: string
  modeler_description: string
  measure_type: 'ModelMeasure'
  language: 'Ruby' | 'Python'
  source: 'builtin' | 'uploaded'
  sha256: string
  arguments: ModelMeasureArgument[]
}

export interface ModelGraph {
  schema_version: number
  openstudio_version: string
  osm_sha256: string
  graph_sha256: string
  counts: Record<string, number>
  constructions: ModelConstruction[]
  materials: ModelMaterial[]
  schedules: ModelSchedule[]
  space_types: ModelSpaceType[]
  spaces: ModelGraphObject[]
  zones: ModelGraphObject[]
  hvac: {
    zone_equipment: ModelGraphObject[]
    air_loops: ModelHvacLoop[]
    plant_loops: ModelHvacLoop[]
  }
  project_parameters: ModelProjectParameter[]
  project_parameter_context: {
    base_template: string
    reference_baseline_kwh_m2: { heating: number; cooling: number }
    cadastre_reference_kwh_m2: { demanda_ca: number; demanda__1: number }
    cadastre_definition_status: string
  }
  simulation: {
    run_period: ModelGraphObject & { begin: { month: number; day: number }; end: { month: number; day: number } }
    timesteps_per_hour: number
    simulation_control: Record<string, unknown>
    sizing: Record<string, unknown>
    design_days: ModelGraphObject[]
    output_variables: Array<ModelGraphObject & { variable: string; key: string; frequency: string }>
  }
}

export interface ModelArtifactMetadata {
  requested_id: string
  model_id: string
  source_kind: 'committed_run' | 'authored_run' | 'preview'
  refparcela: string | null
  scenario_name: string | null
  verification_status: string
  immutable: boolean
  osm_sha256: string
}

export interface ModelGraphResponse {
  model: ModelArtifactMetadata
  graph: ModelGraph
}

export interface ModelEditPatch {
  op: 'project_parameter.update' | 'construction.set_layers' | 'construction.reorder'
    | 'material.update' | 'material.create' | 'material.delete'
    | 'schedule.update_day' | 'schedule.add_rule' | 'schedule.delete_rule'
    | 'space_type.set_loads' | 'space_type.set_infiltration' | 'space_type.set_dsoa'
    | 'thermostat.set_setpoints' | 'hvac.set_system'
    | 'hvac.air_loop.create' | 'hvac.plant_loop.create' | 'hvac.loop.delete'
    | 'hvac.zone.connect' | 'hvac.zone.disconnect'
    | 'hvac.component.add' | 'hvac.component.update' | 'hvac.component.remove'
    | 'measure.apply'
    | 'sim.set_output_variables' | 'sim.set_run_period'
    | 'surface.create' | 'surface.move' | 'surface.delete' | 'surface.set_wwr'
    | 'subsurface.create' | 'subsurface.delete' | 'subsurface.add_overhang'
    | 'story.add' | 'space.duplicate_story'
  target_id?: string
  payload: Record<string, unknown>
}

export interface ModelEditReport {
  index: number
  op: ModelEditPatch['op']
  status: 'applied' | 'rejected'
  severity: 'success' | 'warning' | 'error'
  code: string
  message: string
  warnings: Array<{ severity: 'warning'; code: string; message: string }>
  result?: Record<string, unknown>
}

export interface ModelPreflight {
  schema_version: number
  ready: boolean
  errors: Array<{ code: string; message: string }>
  warnings: Array<{ code: string; message: string }>
  checks: Record<string, boolean>
  graph_sha256?: string
  patch_count?: number
  run_settings?: Record<string, number | null>
}

export interface ModelEditSessionResponse {
  token?: string
  session: {
    id: string
    status: 'open'
    source_model_id: string
    resolved_model_id: string
    authored_from: string
    created_at: string
    updated_at: string
    expires_at: string
    patch_count: number
  }
  model: ModelArtifactMetadata
  graph: ModelGraph
  scene: SceneModel
  journal: Array<Record<string, unknown>>
  run_settings: Record<string, number | null>
  preflight: ModelPreflight
  reports: ModelEditReport[]
  measures: ModelMeasure[]
  uploaded_measure?: ModelMeasure
}

export interface ModelEditorOptions {
  schema_version: number
  mode_default: 'advanced'
  guardrail: 'warn_not_block'
  base_template: string
  supported_patches: ModelEditPatch['op'][]
  project_parameter_bounds: Record<string, { minimum: number; maximum: number }>
  hvac_systems: Array<{ id: string; label: string; system_type: number | null }>
  hvac_air_loop_templates: Array<{ id: string; label: string; system_type: number }>
  hvac_equipment_library: Array<{ id: string; label: string; class_name: string; loop_kinds: Array<'air' | 'plant'>; requires_plant?: 'heating' | 'cooling' }>
  measures: ModelMeasure[]
  guided_presets: Array<{ id: string; name: string; patches: ModelEditPatch[] }>
}

export interface FacadeQA {
  azimut: number
  wwr_target: number
  wall_m2: number
  target_m2: number
  glass_m2: number
  window: number
  door: number
  wwr_real: number
  lapse_pct: number
}

export interface QACheck {
  id: string
  status: 'pass' | 'warn' | 'fail'
  message: string
}

export interface QAResult {
  all_pass: boolean
  warning_count?: number
  checks: QACheck[]
  warnings?: string[]
  scientific_status?: ScientificStatus
  unmet_hours?: number
  error_summary?: string | null
  warning_summary?: WarningSummary
}

export interface PreviewDetail {
  job: JobRecord
  scene: SceneModel | null
  stats: Record<string, unknown> | null
  qa: QAResult | null
  renderer: RendererSummary | null
  config: BuildConfig
  geometry_actions: Array<Record<string, unknown>>
  request_fingerprint: string
  artifact_state: PreviewArtifactState
  queue_context: PreviewQueueContext
}

export interface PreviewArtifactState {
  status: 'PENDING' | 'AVAILABLE' | 'COMMITTED' | 'MISSING' | 'TAMPERED'
  recoverable: boolean
  issues: string[]
}

export interface PreviewQueueContext {
  position: number | null
  queued_total: number
  worker_busy: boolean
  active_job: null | {
    id: string
    kind: string
    refparcela: string
    stage: string
    attempt_count: number
    heartbeat_at?: string | null
  }
}

export interface PreviewRecoveryItem {
  id: string
  status: JobRecord['status']
  stage: string
  refparcela: string
  scenario_name: string
  baseline_profile?: string | null
  created_at: string
  updated_at: string
  queue_position?: number | null
  artifact_state: PreviewArtifactState
}

export interface PreviewRecoveryResult {
  active: PreviewRecoveryItem | null
  items: PreviewRecoveryItem[]
  ready_count: number
}

export interface RendererSummary {
  status: 'VERSIONED' | 'UNVERSIONED'
  renderer_version?: string | null
  source_fingerprint?: string | null
  visual_geometry_fingerprint?: string | null
  context_roof_count?: number
  current_match: boolean
}

export interface RunRecord {
  id: string
  job_id: string
  refparcela: string
  scenario_name: string
  config: BuildConfig | SimulationSettings | NeighborhoodSettings | CitySettings | LhsSettings
  stats: Record<string, unknown>
  qa: QAResult
  artifact_dir: string
  verification_status: 'VERIFIED' | 'LEGACY' | 'TAMPERED' | 'COMMITTING'
  raw_model_sha256?: string | null
  canonical_fingerprint?: string | null
  run_type: 'model' | 'simulation' | 'neighborhood' | 'city' | 'lhs'
  parent_run_id?: string | null
  scenario_id?: string | null
  provenance?: 'pipeline' | 'authored'
  authored_from?: string | null
  patch_journal?: Array<Record<string, unknown>>
  renderer?: RendererSummary | null
  created_at: string
  artifacts?: Array<{ name: string; sha256: string; size_bytes: number }>
}

export type ScientificStatus = 'VALIDATED' | 'UNVERIFIED' | 'INVALID'

export interface SimulationCheck {
  check: string
  model: number | string | null
  eplus: number | string | null
  tolerance: number | string
  passed: boolean
}

export interface SimulationQA extends Omit<QAResult, 'checks'> {
  checks: SimulationCheck[]
  scientific_status: ScientificStatus
  warning_summary: WarningSummary
}

export interface WarningSummary {
  warnings: number
  severes: number
  fatals: number
  categories: Array<{ category: string; count: number }>
  messages: string[]
}

export interface SimulationSettings {
  run_period: 'annual'
  timestep_per_hour: number
  output_variables: Array<{ name: string; key: string; frequency: string }>
  energy_output_variables: Record<'heating' | 'cooling', string>
  energy_basis: 'ideal_loads_demand' | 'detailed_hvac_consumption'
  area_basis: 'conditioned_residential_area'
  conditioned_residential_area_m2: number
  parent_run_id: string
  parent_model_sha256: string
  parent_canonical_fingerprint: string
  weather_snapshot_hash: string
  cadastre_heating: CadastreHeating
  provenance: Record<string, string>
  scenario?: PartGScenario | null
}

export interface PartGScenario {
  scenario_id?: string | null
  name: string
  parent_run_id?: string
  heat_delta_c: number
  cool_delta_c: number
  weather_dataset_id?: string | null
  weather_dataset_name?: string | null
  weather_snapshot_hash: string
  weather_source_name: string
  weather_changed: boolean
  reason?: string | null
  source_type?: SourceType | null
  source_ref?: string | null
}

export interface ScenarioParent extends EligibleModel {
  weather_source_name: string
}

export interface ScenarioWeatherDataset {
  id: string
  name: string
  snapshot_hash: string
  source_name: string
  managed: boolean
}

export interface ScenarioOptions {
  ready: boolean
  delta_bounds_c: { min: number; max: number; step: number }
  parents: ScenarioParent[]
  weather_datasets: ScenarioWeatherDataset[]
  source_types: SourceType[]
}

export interface ScenarioRequest {
  parent_run_id: string
  name: string
  heat_delta_c: number
  cool_delta_c: number
  weather_dataset_id: string | null
  reason: string
  source_type: SourceType
  source_ref: string | null
}

export interface CadastreHeating {
  baseline_heating_kwh_m2: number | null
  post_intervention_heating_kwh_m2: number | null
  cooling_reference: null
  source_snapshot_hash: string
}

export interface RawEnergyMetric {
  variable: string
  joule: number
  kwh: number
  kwh_m2: number
}

export interface SimulationResult {
  schema_version: number
  settings: SimulationSettings
  raw_energy: {
    area_basis: string
    area_m2: number
    heating: RawEnergyMetric
    cooling: RawEnergyMetric
  } | null
  normalized_energy: {
    heating_kwh: number
    cooling_kwh: number
    heating_kwh_m2: number
    cooling_kwh_m2: number
    hvac_consumption_kwh_m2?: number
    total_site_kwh_m2?: number
    energy_basis?: 'detailed_hvac_consumption'
  } | null
  consumption?: Record<string, number> | null
  qa: SimulationQA
  warnings: WarningSummary
  carbon: Record<string, string | number> | null
  cadastre_heating: CadastreHeating
  provenance: Record<string, string>
}

export interface SimulationRun extends RunRecord {
  run_type: 'simulation'
  result: SimulationResult | null
  verification: { ok: boolean; status: string; issues: string[] }
  parent: {
    id: string
    refparcela: string
    scenario_name: string
    verification_status: string
    provenance: 'pipeline' | 'authored'
    authored_from: string | null
  } | null
  automatic_baseline: {
    model_id: string
    simulation_run_id: string | null
    job_id: string | null
    status: 'completed' | 'queued' | 'running' | 'missing'
  } | null
}

export interface EligibleModel {
  id: string
  refparcela: string
  scenario_name: string
  created_at: string
  verification_status: 'VERIFIED'
  raw_model_sha256: string
  canonical_fingerprint: string
  weather_snapshot_hash: string
  settings: Omit<SimulationSettings, 'parent_run_id' | 'parent_model_sha256' | 'parent_canonical_fingerprint' | 'weather_snapshot_hash' | 'cadastre_heating' | 'provenance'>
  provenance: 'pipeline' | 'authored'
  authored_from: string | null
  patch_count: number
}

export interface SimulationPairLeg {
  model_id: string
  simulation_run_id: string | null
  job: JobRecord | null
  status: 'completed' | 'active' | 'queued'
}

export interface SimulationPairResponse {
  schema_version: number
  refparcela: string
  automatic_baseline_model_id: string
  authored_model_id: string
  baseline: SimulationPairLeg
  authored: SimulationPairLeg
}

export interface NeighborhoodSettings {
  scope: string
  scope_mode?: 'boundary' | 'district' | 'building'
  district?: string | null
  building_ref?: string | null
  run_mode: 'full_baseline' | 'full_scenario'
  method: 'representative_typology_period'
  capability_version: string
  scenario?: StockScenario | null
  input_snapshot_hashes?: Record<string, string>
}

export interface StockScenario {
  name: string
  heat_delta_c: number
  cool_delta_c: number
  weather_dataset_id: string | null
  weather_dataset_name: string | null
  weather_snapshot_hash: string | null
  reason: string
  source_type: string
  source_ref: string | null
  fingerprint: string
}

export interface StockRunRequest {
  mode: 'baseline' | 'scenario'
  name?: string | null
  heat_delta_c: number
  cool_delta_c: number
  weather_dataset_id?: string | null
  reason?: string | null
  source_type?: 'human_judgement' | 'dataset' | 'publication' | 'supervisor' | 'other' | null
  source_ref?: string | null
  input_policy_override?: Partial<StockInputPolicy> | null
}

export interface NeighborhoodRunRequest extends StockRunRequest {
  scope_mode: 'boundary' | 'district' | 'building'
  district?: string | null
  building_ref?: string | null
}

export interface StockRunOptions {
  features: Record<string, boolean>
  districts?: string[]
  weather_datasets: Array<{ id: string; name: string; snapshot_hash: string; source_name: string }>
  delta_bounds_c: { min: number; max: number; step: number }
  source_types: Array<'human_judgement' | 'dataset' | 'publication' | 'supervisor' | 'other'>
}

export interface NeighborhoodRepresentative {
  cluster: string
  family: string
  period: string
  refparcela: string
  n_buildings: number
  rep_area_m2: number
  cluster_med_area_m2: number
  rep_floors: number
  cluster_med_floors: number
  rep_vertices: number
}

export interface NeighborhoodCluster extends NeighborhoodRepresentative {
  qa_all_pass: boolean
  heating_kwh_m2: number
  cooling_kwh_m2: number
  s1_co2_kg_m2: number
  s2_co2_kg_m2: number
  cons_hc_kwh_m2?: number
  total_site_kwh_m2?: number
  s1_consumption_kwh_m2?: number
  s2_consumption_kwh_m2?: number
  hvac_co2_kg_m2?: number
  total_site_co2_kg_m2?: number
  qa_all_pass_hvac?: boolean
  eplus_warnings: number
  param_wall_u: number
  param_roof_u: number
  param_window_u: number
  param_window_g: number
  param_ground_unconditioned: boolean
}

export interface NeighborhoodPreflight {
  schema_version: number
  scope: string
  method: string
  locked: boolean
  summary: {
    buildings: number
    clusters: number
    representatives: number
    residential_area_m2: number
    imputed_floor_buildings: number
    proxy_area_buildings: number
  }
  representatives: NeighborhoodRepresentative[]
  map: FeatureCollection | null
  map_descriptor: NeighborhoodMapDescriptor
  capability: { version: string; runner_sha256: string; adapter_sha256: string }
}

export interface NeighborhoodMapDescriptor {
  fingerprint: string
  sha256: string
  feature_count: number
  unique_refparcela_count?: number
  duplicate_refparcela_count?: number
  bounds: [number, number, number, number]
  crs: 'EPSG:4326'
  size_bytes: number
  url: string
}

export interface NeighborhoodResult {
  schema_version: number
  settings: NeighborhoodSettings
  summary: {
    scope: string
    buildings: number
    clusters_expected: number
    clusters_completed: number
    clusters_failed: number
    qa_passed_clusters: number
    totals: null | {
      heating_gwh_yr: number
      cooling_gwh_yr: number
      s1_co2_t_yr: number
      s2_co2_t_yr: number
      hvac_consumption_gwh_yr?: number
      total_site_gwh_yr?: number
      hvac_co2_t_yr?: number
      total_site_co2_t_yr?: number
      residential_area_m2: number
    }
  }
  qa: QAResult & {
    scientific_status: ScientificStatus
    failures: Array<{ cluster: string; refparcela: string; stage: string; error: string }>
  }
  representatives: NeighborhoodRepresentative[]
  clusters: NeighborhoodCluster[]
  validation: string
  map_available: boolean
  map_descriptor?: NeighborhoodMapDescriptor
  single_building?: {
    refparcela: string
    exit_code: 0 | 1 | 2
    report_artifact: string | null
    report_text: string
  }
}

export interface NeighborhoodRun extends RunRecord {
  run_type: 'neighborhood'
  result: NeighborhoodResult | null
  verification: { ok: boolean; status: string; issues: string[] }
}

export interface StockRunSummary {
  id: string
  scenario_name: string
  verification_status: RunRecord['verification_status']
  scientific_status: ScientificStatus | null
  created_at: string
}

export interface CitySettings {
  scope: 'Valencia'
  run_mode: 'full_baseline' | 'full_scenario'
  method: 'representative_typology_period'
  capability_version: string
  scenario?: StockScenario | null
  input_snapshot_hashes?: Record<string, string>
}

export interface CityDistrict {
  nombre: string
  coddistrit: number | string | null
  n_buildings: number
  res_area_m2: number
  longitude?: number | null
  latitude?: number | null
  heating_gwh?: number
  cooling_gwh?: number
  s1_co2_t?: number
  s2_co2_t?: number
  cons_hc_gwh?: number
  total_site_gwh?: number
  hvac_co2_t?: number
  total_site_co2_t?: number
}

export interface CityPreflight {
  schema_version: number
  scope: string
  method: string
  locked: boolean
  summary: {
    buildings: number
    clusters: number
    representatives: number
    districts: number
    residential_area_m2: number
    imputed_floor_buildings: number
    proxy_area_buildings: number
    duplicate_parcel_rows: number
  }
  representatives: NeighborhoodRepresentative[]
  districts: CityDistrict[]
  bounds: [number, number, number, number]
  capability: { version: string; runner_sha256: string; adapter_sha256: string }
}

export interface CityMapMetrics {
  schema_version: number
  bounds: [number, number, number, number]
  focus_bounds?: [number, number, number, number] | null
  focus_buildings?: number
  focus_coverage?: number
  clusters: NeighborhoodCluster[]
  districts: CityDistrict[]
  energy_available: boolean
}

export interface CityResult {
  schema_version: number
  settings: CitySettings
  summary: {
    scope: string
    buildings: number
    districts: number
    clusters_expected: number
    clusters_completed: number
    clusters_failed: number
    qa_passed_clusters: number
    totals: null | {
      heating_gwh_yr: number
      cooling_gwh_yr: number
      s1_co2_t_yr: number
      s2_co2_t_yr: number
      hvac_consumption_gwh_yr: number
      total_site_gwh_yr: number
      s1_consumption_gwh_yr: number
      s2_consumption_gwh_yr: number
      hvac_co2_t_yr: number
      total_site_co2_t_yr: number
      residential_area_m2: number
    }
    certificate_heating: null | {
      buildings: number
      model_kwh_m2: number | null
      certificate_kwh_m2: number | null
      ratio: number | null
      definition_status?: string
    }
  }
  qa: QAResult & {
    scientific_status: ScientificStatus
    failures: Array<{ cluster: string; refparcela: string; stage: string; error: string }>
    visualization_warning?: string | null
  }
  representatives: NeighborhoodRepresentative[]
  clusters: NeighborhoodCluster[]
  districts: CityDistrict[]
  validation: string
  map_available: boolean
  energy_map_available: boolean
}

export interface CityRun extends RunRecord {
  run_type: 'city'
  result: CityResult | null
  verification: { ok: boolean; status: string; issues: string[] }
}

export type StockCompareKind = 'neighborhood' | 'city'

export interface StockCompareMetric {
  left: number | null
  right: number | null
  delta: number | null
  percent: number | null
}

export interface StockCompareSummaryMetric extends StockCompareMetric {
  key: string
  category: 'demand' | 'consumption' | 'carbon'
  unit: string
}

export interface StockCompareRow {
  key: string
  label: string
  left_present: boolean
  right_present: boolean
  left_buildings: number | null
  right_buildings: number | null
  metrics: Record<string, StockCompareMetric>
}

export interface StockCompareEvidence {
  id: string
  run_type: StockCompareKind
  scenario_name: string
  created_at: string
  verification_status: string
  verification_ok: boolean
  verification_issues: string[]
  scientific_status: ScientificStatus
  scope: string
  scope_mode: string
  run_mode: string
  scenario: StockScenario | null
  weather_snapshot_hash: string | null
  input_snapshot_hashes: Record<string, string>
  summary: NeighborhoodResult['summary'] | CityResult['summary']
  qa_checks: Array<{ id: string; status: string; message: string }>
  map_descriptor?: NeighborhoodMapDescriptor | null
}

export interface StockComparison {
  kind: StockCompareKind
  left: StockCompareEvidence
  right: StockCompareEvidence
  compatibility: {
    interpretation_enabled: boolean
    percent_enabled: boolean
    reasons: string[]
    checks: Array<{ id: string; passed: boolean; left: unknown; right: unknown }>
  }
  metrics: StockCompareSummaryMetric[]
  clusters: StockCompareRow[]
  districts: StockCompareRow[]
}

export interface LhsSettings {
  scope: '4252702YJ2745A'
  run_mode: 'frozen_baseline'
  method: 'latin_hypercube_uniform_spearman'
  n: number
  seed: number
  capability_version: string
  input_snapshot_hashes?: Record<string, string>
}

export interface LhsVariable {
  name: string
  minimum: number
  maximum: number
  distribution: 'uniform'
  group: 'simulation' | 'post'
  unit: string
  domain: string
}

export interface LhsBaselines {
  massless: Record<string, number>
  layered: Record<string, number>
  cadastre: Record<string, number>
}

export interface LhsPreflight {
  schema_version: number
  scope: string
  method: string
  locked: boolean
  settings: {
    n: number
    seed: number
    simulation_variables: number
    post_variables: number
    model_path: string
    context_shading: boolean
    estimated_minutes: number
  }
  variables: LhsVariable[]
  variable_fingerprint: string
  baselines: LhsBaselines
  outputs: string[]
  capability: { version: string; runner_sha256: string; adapter_sha256: string }
  accepted_reference?: {
    n?: number
    seed?: number
    statistics: Record<string, Partial<LhsStatistic>>
  }
}

export interface LhsStatistic {
  mean: number
  median: number
  p5: number
  p95: number
  minimum: number
  maximum: number
  stddev: number
}

export interface LhsDriver {
  variable: string
  rho: number
  rank: number
}

export interface LhsCheck {
  name: string
  passed: boolean
  expected: number | boolean | string
  actual: number | boolean | string
}

export interface LhsSample {
  wall_u: number
  roof_u: number
  window_u: number
  window_g: number
  infiltration_ach: number
  shade_setpoint: number
  thermal_bridge_du: number
  cop: number
  seer: number
  emission_factor: number
  heating_kwh_m2: number
  cooling_kwh_m2: number
  consumption_kwh_m2: number
  co2_kg_m2: number
  co2_t_building: number
}

export interface LhsResult {
  schema_version: number
  settings: LhsSettings
  summary: {
    scope: string
    samples_expected: number
    samples_completed: number
    simulation_variables: number
    post_variables: number
    statistics: Record<string, LhsStatistic>
  }
  qa: { all_pass: boolean; scientific_status: ScientificStatus; checks: LhsCheck[] }
  variables: LhsVariable[]
  variable_fingerprint: string
  baselines: LhsBaselines
  sensitivity: Record<string, LhsDriver[]>
  samples: LhsSample[]
  figures: { distributions: string; sensitivity: string }
  summary_text: string
}

export interface LhsRun extends RunRecord {
  run_type: 'lhs'
  result: LhsResult | null
  verification: { ok: boolean; status: string; issues: string[] }
  current_compatibility?: { current: boolean; changed_roles: string[] }
}

export interface LhsComparison {
  left_run_id: string
  right_run_id: string
  comparable: boolean
  reason: string | null
  rows: Array<{
    output: string
    statistic: string
    left: number
    right: number
    delta: number
    percent: number | null
  }>
}

export interface JobRecord {
  id: string
  kind: string
  status: 'queued' | 'running' | 'ready' | 'completed' | 'failed' | 'canceled'
  stage: string
  refparcela: string
  error?: string | null
  run_id?: string | null
  attempt_count: number
  max_attempts: number
  cancel_requested: number
  timeout_seconds: number
  heartbeat_at?: string | null
  attempt_started_at?: string | null
  terminal_at?: string | null
  queue_position?: number | null
  payload?: { parent_run_id?: string; [key: string]: unknown }
  created_at: string
  updated_at: string
}

export interface CapabilitiesResult {
  schema_version: number
  capabilities: Record<string, {
    part: string
    runtime_ready: boolean
    declared_ready: boolean
    inspection: { ok: boolean; sha256?: string; signature_parameters?: string[]; reason?: string; detail?: string }
    contract?: {
      ok: boolean
      checks: Record<string, boolean>
      reason?: string
      expected?: Record<string, unknown>
      runner_sha256?: string
      expected_runner_sha256?: string
    } | null
    diagnostic: {
      state: 'verified' | 'source_changed' | 'contract_failed' | 'dependency_blocked' | 'disabled' | 'blocked'
      reason: string | null
      source: {
        path: string
        current_sha256: string | null
        expected_sha256: string | null
        modified_at: string | null
      }
      latest_verified_evidence: null | {
        run_id: string
        verified_at: string
        source_sha256: string
        input_snapshot_hash: string
      }
      failed_checks: string[]
      features: Array<{ key: string; available: boolean; evidence: string }>
      revalidation_supported: boolean
    }
  }>
}

export interface CapabilityRevalidationPlan {
  capability: string
  supported: boolean
  eligible: boolean
  reason: string | null
  blocking_checks?: string[]
  current_sha256?: string | null
  expected_sha256?: string | null
  latest_verified_evidence?: CapabilitiesResult['capabilities'][string]['diagnostic']['latest_verified_evidence']
  expected?: Record<string, unknown>
  duration_seconds?: number
  disk_bytes?: number
  steps?: string[]
  plan_token?: string
}

export interface HealthResult {
  ok: boolean
  readiness: 'READY' | 'WARNING' | 'BLOCKED'
  openstudio_version: string
  python_builder: string
  database: string
  files: Record<string, { ok: boolean; path: string; snapshot_hash?: string; components?: number; error?: string }>
  checks: Record<string, { ok: boolean; [key: string]: unknown }>
  worker: { running: boolean; active_job_id?: string | null; active_pid?: number | null }
  storage?: StorageCapacity
  template: {
    ok: boolean
    error: string | null
    required: Record<string, { ok: boolean; missing: string[] }>
  }
}

export interface StorageCapacity {
  status: 'READY' | 'WARNING' | 'BLOCKED'
  total_bytes: number
  used_bytes: number
  free_bytes: number
  free_ratio: number
  used_ratio: number
  warning_free_bytes: number
  blocked_free_bytes: number
  warning_free_ratio: number
  blocked_free_ratio: number
  reserve_floor_bytes: number
}

export interface StorageOverview {
  schema_version: number
  policy_version: string
  status: 'READY' | 'WARNING' | 'BLOCKED'
  capacity: StorageCapacity
  reservations: {
    total_bytes: number
    items: Array<{ job_id: string; kind: string; status: string; bytes: number }>
  }
  job_admissions: Record<string, {
    allowed: boolean
    reason: string | null
    estimated_bytes: number
    available_after_bytes: number
    reserve_floor_bytes: number
  }>
  categories: Record<string, { id: string; size_bytes: number; file_count: number }>
  tracked_bytes: number
  cleanup: { candidate_count: number; reclaimable_bytes: number; pending_object_snapshots: number }
  archive: { configured: boolean; path: string | null; writable: boolean; same_device: boolean | null; free_bytes: number | null }
}

export type StorageCleanupCategory =
  | 'abandoned_scratch'
  | 'expired_previews'
  | 'export_cache'
  | 'map_cache'
  | 'object_store_gc'

export interface StorageCleanupPlan {
  schema_version: number
  plan_token: string
  generated_at: string
  candidate_count: number
  reclaimable_bytes: number
  pending_object_snapshots: number
  categories: Partial<Record<StorageCleanupCategory, { count: number; size_bytes: number }>>
  items: Array<{
    id: string
    category: StorageCleanupCategory
    path: string | null
    snapshot_hash?: string
    size_bytes: number
    file_count: number
    modified_at: string
    reason: string
  }>
  protected: { immutable_runs: number; active_or_ready_jobs: number; referenced_snapshots: number }
}

export interface StorageCleanupResult {
  deleted_count: number
  estimated_freed_bytes: number
  categories: StorageCleanupCategory[]
  storage: StorageOverview
}

export interface JobEvent {
  id: number
  level: 'info' | 'error' | 'warning'
  message: string
  progress: number
  created_at: string
}
import type { Feature, FeatureCollection, Geometry } from 'geojson'
