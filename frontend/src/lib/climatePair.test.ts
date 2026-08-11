import { describe, expect, it } from 'vitest'
import { climatePairChanged, climatePairPatch, climatePairReady } from './climatePair'

const draft = (over: Partial<Parameters<typeof climatePairPatch>[0]> = {}) => ({
  weather: 'bergamo-epw', ddy: 'bergamo-ddy', ground: '18', mains: '', ...over,
})

describe('climate pair activation', () => {
  it('always carries both sides, so a half-changed pair is never sent', () => {
    const result = climatePairPatch(draft())
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.patch.weather_dataset_id).toBe('bergamo-epw')
    expect(result.patch.ddy_dataset_id).toBe('bergamo-ddy')
  })

  it('refuses to send until both sides are chosen', () => {
    expect(climatePairReady({ weather: 'bergamo-epw', ddy: '' })).toBe(false)
    const result = climatePairPatch(draft({ ddy: '' }))
    expect(result.ok).toBe(false)
  })

  it('sends the declared ground temperature with the pair', () => {
    const result = climatePairPatch(draft({ ground: '18.5' }))
    expect(result.ok && result.patch.ground_temperature_c).toBe(18.5)
  })

  it('passes a blank declaration through as null rather than inventing one', () => {
    // Only the verified reference pair may leave these blank; the server is
    // what decides that, and it can only decide if the blank arrives as blank.
    const result = climatePairPatch(draft({ ground: '', mains: '' }))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.patch.ground_temperature_c).toBeNull()
    expect(result.patch.water_mains_temperature_c).toBeNull()
  })

  it('rejects a temperature that is not a number', () => {
    const result = climatePairPatch(draft({ ground: 'warm' }))
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.reason).toMatch(/°C/)
  })

  it('reports a pending change against whatever is active', () => {
    const active = { weather: 'valencia-iwec', ddy: 'valencia-iwec-ddy' }
    expect(climatePairChanged({ weather: 'valencia-iwec', ddy: 'valencia-iwec-ddy' }, active)).toBe(false)
    expect(climatePairChanged({ weather: 'bergamo-epw', ddy: 'valencia-iwec-ddy' }, active)).toBe(true)
  })

  it('treats an unset active pair as empty rather than crashing', () => {
    expect(climatePairChanged({ weather: 'bergamo-epw', ddy: 'bergamo-ddy' },
      { weather: null, ddy: null })).toBe(true)
  })
})
