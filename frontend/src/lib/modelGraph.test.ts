import { describe, expect, it } from 'vitest'
import { compactValue, formatModelDisplayText, humanizeModelKey, matchesGraphSearch } from './modelGraph'

describe('model graph inspector helpers', () => {
  const construction = {
    id: '{construction}', name: 'IVE brick wall', type: 'Construction',
    surface_usage: { surface_count: 12, area_m2: 450.25 },
  }

  it('searches nested graph evidence case-insensitively', () => {
    expect(matchesGraphSearch(construction, 'brick')).toBe(true)
    expect(matchesGraphSearch(construction, '450.25')).toBe(true)
    expect(matchesGraphSearch(construction, 'roof')).toBe(false)
  })

  it('formats scalar and record evidence compactly', () => {
    expect(compactValue(1.409000)).toBe('1.409')
    expect(compactValue(true)).toBe('YES')
    expect(compactValue({ month: 1, day: 1 })).toBe('month: 1 · day: 1')
  })

  it('corrects legacy floor labels only in the display layer', () => {
    const source = 'Story 2 - 2th floor'
    expect(formatModelDisplayText(source, 'tr')).toBe('Kat 2 · 2. kat')
    expect(formatModelDisplayText(source, 'en')).toBe('Story 2 · 2nd floor')
    expect(formatModelDisplayText('[{"name":"Space 3 - 3th floor"}]', 'en')).toBe('[{"name":"Space 3 · 3rd floor"}]')
    expect(formatModelDisplayText('Zone 0 - Ground (commercial or buffer zone)', 'tr')).toBe('Zon 0 · Zemin (ticari veya tampon bölge)')
    expect(source).toBe('Story 2 - 2th floor')
  })

  it('humanizes snake and camel case evidence keys', () => {
    expect(humanizeModelKey('boundary_condition')).toBe('Boundary condition')
    expect(humanizeModelKey('thermalConductivity')).toBe('Thermal Conductivity')
  })
})
