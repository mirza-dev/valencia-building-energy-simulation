import { describe, expect, it } from 'vitest'
import { scenarioValid, validateScenario } from './scenarioValidation'
import type { ScenarioRequest, ScenarioWeatherDataset } from './types'

const base: ScenarioRequest = {
  parent_run_id: 'parent-verified',
  name: 'Comfort +1 K',
  heat_delta_c: 1,
  cool_delta_c: 0,
  weather_dataset_id: null,
  reason: 'Scientific sensitivity',
  source_type: 'human_judgement',
  source_ref: null,
}
const weather: ScenarioWeatherDataset[] = [{
  id: 'future', name: 'Future EPW', snapshot_hash: 'future-hash', source_name: 'future.epw', managed: true,
}]

describe('Part G scenario validation', () => {
  it('accepts a bounded thermostat variant', () => {
    expect(scenarioValid(validateScenario(base, 'parent-hash', weather))).toBe(true)
  })

  it('rejects no-op, nonfinite, and out-of-band requests', () => {
    const noop = { ...base, heat_delta_c: 0 }
    expect(validateScenario(noop, 'parent-hash', weather).weather).toBe('noop')
    expect(validateScenario({ ...base, heat_delta_c: Number.NaN }, 'parent-hash', weather).heat).toBe('bounds')
    expect(validateScenario({ ...base, cool_delta_c: -3.1 }, 'parent-hash', weather).cool).toBe('bounds')
  })

  it('accepts a weather-only variant when the snapshot differs', () => {
    const request = { ...base, heat_delta_c: 0, weather_dataset_id: 'future' }
    expect(scenarioValid(validateScenario(request, 'parent-hash', weather))).toBe(true)
  })
})
