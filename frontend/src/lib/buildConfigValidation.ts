import { z } from 'zod'
import type { BuildConfig } from './types'

const positive = z.number().finite('validation.positive').positive('validation.positive')
const nonNegative = z.number().finite('validation.nonNegative').nonnegative('validation.nonNegative')
const fraction = z.number().finite('validation.fraction').min(0, 'validation.fraction').max(1, 'validation.fraction')
const wwr = z.number().finite('validation.wwr').min(0, 'validation.wwr').max(0.95, 'validation.wwr')

export const buildConfigSchema = z.object({
  data: z.object({
    building_path: z.string(),
    neighbor_path: z.string(),
    template_path: z.string(),
    epw_path: z.string(),
    output_root: z.string(),
  }),
  geometry: z.object({
    floor_height_m: positive,
    ground_unconditioned: z.boolean(),
    neighbor_assume_ground: z.boolean(),
    simplify_tolerance_m: nonNegative,
    max_area_delta_fraction: fraction,
    footprint_min_m2: positive,
    footprint_max_m2: positive,
    party_wall_tolerance_m: nonNegative,
    min_shared_edge_m: positive,
    party_overlap_ratio: fraction,
    context_radius_m: positive,
  }).refine((value) => value.footprint_min_m2 < value.footprint_max_m2, {
    path: ['footprint_max_m2'],
    message: 'validation.footprintRange',
  }),
  envelope: z.object({
    wall_u: positive.nullable(),
    roof_u: positive.nullable(),
    window_u: positive,
    window_g: fraction,
    thermal_bridge_du: nonNegative,
    massless: z.boolean(),
  }),
  openings: z.object({
    wwr_north: wwr,
    wwr_east: wwr,
    wwr_south: wwr,
    wwr_west: wwr,
    window_width_m: positive,
    window_height_m: positive,
    window_sill_m: nonNegative,
    door_width_m: positive,
    door_height_m: positive,
    door_sill_m: nonNegative,
    balcony_doors_per_facade_floor: z.number().finite('validation.integer').int('validation.integer').min(0, 'validation.integer').max(20, 'validation.integer'),
    balcony_depth_m: nonNegative,
  }),
  shading: z.object({
    context_enabled: z.boolean(),
    blind_name: z.string().min(1),
    setpoint_w_m2: positive,
    summer_start_month: z.number().finite('validation.integer').int('validation.integer').min(1, 'validation.integer').max(12, 'validation.integer'),
    summer_end_month: z.number().finite('validation.integer').int('validation.integer').min(1, 'validation.integer').max(12, 'validation.integer'),
    max_shadow_figures: z.number().finite('validation.integer').int('validation.integer').positive('validation.integer'),
  }).refine((value) => value.summer_start_month <= value.summer_end_month, {
    path: ['summer_end_month'],
    message: 'validation.monthRange',
  }),
  operation: z.object({
    infiltration_ach: nonNegative.nullable(),
    output_variables: z.record(z.string(), z.string()),
  }),
  qa: z.object({ facade_wwr_warning_pct: nonNegative }),
  provenance: z.object({
    baseline_profile: z.string(),
    scenario_name: z.string().min(1, 'validation.scenarioRequired'),
    locale: z.enum(['tr', 'en']),
    overrides: z.array(z.object({
      field: z.string(),
      reason: z.string(),
      source_type: z.enum(['human_judgement', 'dataset', 'publication', 'supervisor', 'other']),
      source_ref: z.string().nullable(),
    })),
  }),
}).strict()

export type BuildConfigErrors = Record<string, string>

export function validateBuildConfig(config: BuildConfig): BuildConfigErrors {
  const result = buildConfigSchema.safeParse(config)
  if (result.success) return {}
  return Object.fromEntries(result.error.issues.map((issue) => [issue.path.join('.'), issue.message]))
}
