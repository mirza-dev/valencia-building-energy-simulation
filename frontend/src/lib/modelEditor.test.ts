import { describe, expect, it } from 'vitest'
import { automaticSpaceTypeDraft, formatSchedulePoints, inferCuratedHvacSystem, parseSchedulePoints, parseStoredModelEditSession, parseTypedPatch } from './modelEditor'
import type { ModelGraph, ModelSpaceType } from './types'

describe('model editor client contracts', () => {
  it('recovers only scoped session credentials', () => {
    const sessionId = 'a'.repeat(32)
    expect(parseStoredModelEditSession(JSON.stringify({ sessionId, token: 't'.repeat(32) }))).toEqual({ sessionId, token: 't'.repeat(32) })
    expect(parseStoredModelEditSession('{broken')).toBeNull()
    expect(parseStoredModelEditSession(JSON.stringify({ sessionId: '../bad', token: 't'.repeat(32) }))).toBeNull()
  })

  it('validates typed patches and complete schedule days', () => {
    expect(parseTypedPatch('{"op":"project_parameter.update","payload":{"key":"wall_u","value":1.5}}').op).toBe('project_parameter.update')
    expect(() => parseTypedPatch('{"payload":{}}')).toThrow(/op field/)
    const points = parseSchedulePoints('8:18\n18:21\n24:18')
    expect(formatSchedulePoints(points)).toBe('8:18\n18:21\n24:18')
    expect(() => parseSchedulePoints('12:20')).toThrow(/hour 24/)
  })

  it('starts expert forms from auto-derived space-type and HVAC values', () => {
    const spaceType = {
      id: 'space-type', name: 'Vivienda CTE', type: 'OS:SpaceType',
      load_summary: { peoplePerFloorArea: 0.04, lightingPowerPerFloorArea: 3.5, electricEquipmentPowerPerFloorArea: 4.1, gasEquipmentPowerPerFloorArea: 0 },
      infiltration: [{ id: 'infiltration', name: 'Infitracion Aire constante 0,2ACH Viv CTE', type: 'OS:SpaceInfiltrationDesignFlowRate', air_changes_per_hour: 0.2, design_flow_rate_m3_s: null, flow_per_floor_area_m3_s_m2: null, flow_per_exterior_area_m3_s_m2: null, flow_per_exterior_wall_area_m3_s_m2: null }],
      outdoor_air: { id: 'dsoa', name: 'Vivienda CTE DSOA', type: 'OS:DesignSpecificationOutdoorAir', flow_per_person_m3_s: 0.0063, flow_per_floor_area_m3_s_m2: 0, flow_rate_m3_s: 0, air_changes_per_hour: 0 },
    } as ModelSpaceType
    expect(automaticSpaceTypeDraft(spaceType)).toEqual({
      loads: { people_per_floor_area: '0.04', lighting_power_per_floor_area: '3.5', electric_equipment_power_per_floor_area: '4.1', gas_equipment_power_per_floor_area: '0' },
      infiltration: { id: 'infiltration', method: 'air_changes_per_hour', value: '0.2' },
      outdoorAir: { method: 'flow_per_person_m3_s', value: '0.0063' },
    })
    expect(inferCuratedHvacSystem({ hvac: { zone_equipment: [{ id: 'ideal', name: 'Ideal Loads', type: 'OS:ZoneHVAC:IdealLoadsAirSystem' }], air_loops: [], plant_loops: [] } } as Pick<ModelGraph, 'hvac'>)).toBe('ideal_loads')
  })
})
