import type { ModelEditPatch, ModelGraph, ModelSpaceType } from './types'

export interface StoredModelEditSession { sessionId: string; token: string }

export interface AutomaticSpaceTypeDraft {
  loads: Record<'people_per_floor_area' | 'lighting_power_per_floor_area' | 'electric_equipment_power_per_floor_area' | 'gas_equipment_power_per_floor_area', string>
  infiltration: { id: string | null; method: string; value: string }
  outdoorAir: { method: string; value: string }
}

const LOAD_FIELDS = {
  people_per_floor_area: 'peoplePerFloorArea',
  lighting_power_per_floor_area: 'lightingPowerPerFloorArea',
  electric_equipment_power_per_floor_area: 'electricEquipmentPowerPerFloorArea',
  gas_equipment_power_per_floor_area: 'gasEquipmentPowerPerFloorArea',
} as const

const INFILTRATION_FIELDS = [
  'air_changes_per_hour', 'design_flow_rate_m3_s', 'flow_per_floor_area_m3_s_m2',
  'flow_per_exterior_area_m3_s_m2', 'flow_per_exterior_wall_area_m3_s_m2',
] as const

const OUTDOOR_AIR_FIELDS = [
  'flow_per_person_m3_s', 'flow_per_floor_area_m3_s_m2', 'flow_rate_m3_s', 'air_changes_per_hour',
] as const

function numericDraft(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : ''
}

function firstFilled<T extends string>(source: Record<string, unknown> | null | undefined, fields: readonly T[]): { method: T; value: string } {
  for (const method of fields) {
    const value = numericDraft(source?.[method])
    if (value !== '') return { method, value }
  }
  return { method: fields[0], value: '' }
}

export function automaticSpaceTypeDraft(spaceType: ModelSpaceType | undefined): AutomaticSpaceTypeDraft {
  const infiltration = spaceType?.infiltration[0]
  const infiltrationValue = firstFilled(infiltration, INFILTRATION_FIELDS)
  const outdoorAir = firstFilled(spaceType?.outdoor_air, OUTDOOR_AIR_FIELDS)
  return {
    loads: Object.fromEntries(Object.entries(LOAD_FIELDS).map(([patchKey, graphKey]) => [patchKey, numericDraft(spaceType?.load_summary[graphKey])])) as AutomaticSpaceTypeDraft['loads'],
    infiltration: { id: infiltration?.id ?? null, ...infiltrationValue },
    outdoorAir,
  }
}

export function inferCuratedHvacSystem(graph: Pick<ModelGraph, 'hvac'>): string {
  const types = graph.hvac.zone_equipment.map((item) => item.type.toLowerCase())
  if (types.some((type) => type.includes('ideal') && type.includes('load'))) return 'ideal_loads'
  if (types.some((type) => type.includes('packagedterminalheatpump'))) return 'pthp'
  if (types.some((type) => type.includes('packagedterminalairconditioner'))) return 'ptac_gas_dx'
  if (types.some((type) => type.includes('gas') && type.includes('furnace'))) return 'gas_furnace'
  if (types.some((type) => type.includes('electric') && type.includes('furnace'))) return 'electric_furnace'
  return ''
}

export function modelEditStorageKey(modelId: string): string {
  return `workbench:model-edit:${modelId}`
}

export function parseStoredModelEditSession(raw: string | null): StoredModelEditSession | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<StoredModelEditSession>
    return typeof value.sessionId === 'string' && /^[a-f0-9]{32}$/.test(value.sessionId)
      && typeof value.token === 'string' && value.token.length >= 32
      ? { sessionId: value.sessionId, token: value.token }
      : null
  } catch {
    return null
  }
}

export function parseTypedPatch(raw: string): ModelEditPatch {
  const value = JSON.parse(raw) as Partial<ModelEditPatch>
  if (!value || typeof value !== 'object' || typeof value.op !== 'string') {
    throw new Error('Patch must be an object with an op field.')
  }
  if (!value.payload || typeof value.payload !== 'object' || Array.isArray(value.payload)) {
    throw new Error('Patch payload must be an object.')
  }
  return value as ModelEditPatch
}

export function parseSchedulePoints(raw: string): Array<{ hour: number; value: number }> {
  const points = raw.split(/\n|,/).map((item) => item.trim()).filter(Boolean).map((item) => {
    const [hourRaw, valueRaw, ...rest] = item.split(':')
    const hour = Number(hourRaw)
    const value = Number(valueRaw)
    if (rest.length || !Number.isFinite(hour) || !Number.isFinite(value)) throw new Error(`Invalid point: ${item}`)
    return { hour, value }
  })
  if (!points.length || points.at(-1)?.hour !== 24) throw new Error('The final point must end at hour 24.')
  if (points.some((point, index) => point.hour <= 0 || point.hour > 24 || (index > 0 && point.hour <= points[index - 1].hour))) {
    throw new Error('Hours must be strictly increasing in the range (0, 24].')
  }
  return points
}

export function formatSchedulePoints(points: Array<{ hour: number; value: number }>): string {
  return points.map((point) => `${point.hour}:${point.value}`).join('\n')
}
