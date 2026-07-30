import type { SceneItem } from './types'

export interface NormalizedOpeningRect {
  u_min: number
  u_max: number
  v_min: number
  v_max: number
}

type Point3 = [number, number, number]

function localBounds(wall: SceneItem, opening: SceneItem): NormalizedOpeningRect | null {
  if (wall.vertices.length < 3 || opening.vertices.length < 3) return null
  const wallPoints = wall.vertices as Point3[]
  const openingPoints = opening.vertices as Point3[]
  let horizontal: Point3 | null = null
  let longest = 0
  for (let index = 0; index < wallPoints.length; index += 1) {
    const left = wallPoints[index]
    const right = wallPoints[(index + 1) % wallPoints.length]
    const dx = right[0] - left[0]
    const dy = right[1] - left[1]
    const horizontalLength = Math.hypot(dx, dy)
    if (horizontalLength > longest && Math.abs(right[2] - left[2]) < 0.01) {
      longest = horizontalLength
      horizontal = [dx / horizontalLength, dy / horizontalLength, 0]
    }
  }
  if (!horizontal || longest < 0.01) return null

  const projectU = (point: Point3) => point[0] * horizontal![0] + point[1] * horizontal![1]
  const wallU = wallPoints.map(projectU)
  const wallV = wallPoints.map((point) => point[2])
  const minU = Math.min(...wallU)
  const maxU = Math.max(...wallU)
  const minV = Math.min(...wallV)
  const maxV = Math.max(...wallV)
  const width = maxU - minU
  const height = maxV - minV
  if (width < 0.01 || height < 0.01) return null
  const openingU = openingPoints.map(projectU)
  const openingV = openingPoints.map((point) => point[2])
  return {
    u_min: (Math.min(...openingU) - minU) / width,
    u_max: (Math.max(...openingU) - minU) / width,
    v_min: (Math.min(...openingV) - minV) / height,
    v_max: (Math.max(...openingV) - minV) / height,
  }
}

function overlaps(left: NormalizedOpeningRect, right: NormalizedOpeningRect, clearance = 0.008): boolean {
  return !(
    left.u_max + clearance <= right.u_min
    || left.u_min >= right.u_max + clearance
    || left.v_max + clearance <= right.v_min
    || left.v_min >= right.v_max + clearance
  )
}

/** Suggest a small legal drawing slot without replacing pipeline-authored openings. */
export function suggestOpeningRect(wall: SceneItem, openings: SceneItem[]): NormalizedOpeningRect {
  const occupied = openings
    .filter((item) => item.parent_id === wall.id)
    .map((item) => localBounds(wall, item))
    .filter((item): item is NormalizedOpeningRect => item !== null)
  if (!occupied.length) return { u_min: 0.3, u_max: 0.7, v_min: 0.25, v_max: 0.75 }

  const width = 0.04
  const height = 0.18
  for (const vMin of [0.78, 0.05, 0.58, 0.38]) {
    for (let uMin = 0.02; uMin + width <= 0.98; uMin += 0.01) {
      const candidate = { u_min: uMin, u_max: uMin + width, v_min: vMin, v_max: vMin + height }
      if (!occupied.some((item) => overlaps(candidate, item))) return candidate
    }
  }
  return { u_min: 0.46, u_max: 0.54, v_min: 0.78, v_max: 0.96 }
}
