import { describe, expect, it } from 'vitest'
import { clusterColorExpression, districtColorExpression, energyColorExpression, metricMatchExpression } from './cityMap'
import type { CityDistrict, NeighborhoodCluster, NeighborhoodRepresentative } from './types'

const representative: NeighborhoodRepresentative = {
  cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', refparcela: 'A', n_buildings: 8097,
  rep_area_m2: 331.7, cluster_med_area_m2: 333.3, rep_floors: 5, cluster_med_floors: 5, rep_vertices: 6,
}
const cluster: NeighborhoodCluster = {
  ...representative, qa_all_pass: true, heating_kwh_m2: 14.26, cooling_kwh_m2: 18.88,
  cons_hc_kwh_m2: 16.44, total_site_kwh_m2: 44.12,
  s1_co2_kg_m2: 7.84, s2_co2_kg_m2: 4.39, eplus_warnings: 11,
  param_wall_u: 1.33, param_roof_u: 1.92, param_window_u: 5.7, param_window_g: 0.82,
  param_ground_unconditioned: true,
}

describe('city vector-tile expressions', () => {
  it('maps immutable cluster metrics without changing technical keys', () => {
    expect(metricMatchExpression([cluster as unknown as Record<string, unknown>], 'cluster', 'heating_kwh_m2')).toEqual(
      ['match', ['to-string', ['get', 'cluster']], 'BlocPluriP04', 14.26, 0],
    )
    expect(energyColorExpression([cluster], 'cooling_kwh_m2')).toContain(18.88)
    expect(energyColorExpression([cluster], 'cons_hc_kwh_m2')).toContain(16.44)
    expect(clusterColorExpression([representative])).toContain('#3f756d')
  })

  it('prioritizes district HVAC consumption after a Part F run and stock area during preflight', () => {
    const base: CityDistrict = { nombre: "L'EIXAMPLE", coddistrit: 2, n_buildings: 1969, res_area_m2: 3172335 }
    expect(JSON.stringify(districtColorExpression([base]))).toContain('3172335')
    expect(JSON.stringify(districtColorExpression([{ ...base, heating_gwh: 54.824 }]))).toContain('54.824')
    expect(JSON.stringify(districtColorExpression([{ ...base, heating_gwh: 54.824, cons_hc_gwh: 64.2 }]))).toContain('64.2')
  })
})
