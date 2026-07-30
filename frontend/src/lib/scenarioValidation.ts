import type { ScenarioRequest, ScenarioWeatherDataset } from './types'

export type ScenarioField = 'parent' | 'name' | 'heat' | 'cool' | 'weather' | 'reason' | 'source'

export function validateScenario(
  request: ScenarioRequest,
  parentWeatherHash: string | undefined,
  weatherDatasets: ScenarioWeatherDataset[],
): Partial<Record<ScenarioField, string>> {
  const errors: Partial<Record<ScenarioField, string>> = {}
  if (!request.parent_run_id) errors.parent = 'required'
  const name = request.name.trim()
  if (name.length < 2 || name.length > 80) errors.name = 'length'
  for (const [field, value] of [['heat', request.heat_delta_c], ['cool', request.cool_delta_c]] as const) {
    if (!Number.isFinite(value) || value < -3 || value > 3) errors[field] = 'bounds'
  }
  if (request.reason.trim().length < 3 || request.reason.trim().length > 500) errors.reason = 'length'
  if (!request.source_type) errors.source = 'required'
  const weather = request.weather_dataset_id
    ? weatherDatasets.find((item) => item.id === request.weather_dataset_id)
    : undefined
  if (request.weather_dataset_id && !weather) errors.weather = 'missing'
  const weatherChanged = Boolean(weather && parentWeatherHash && weather.snapshot_hash !== parentWeatherHash)
  if (!errors.heat && !errors.cool && request.heat_delta_c === 0 && request.cool_delta_c === 0 && !weatherChanged) {
    errors.weather = 'noop'
  }
  return errors
}

export const scenarioValid = (errors: Partial<Record<ScenarioField, string>>) =>
  Object.keys(errors).length === 0
