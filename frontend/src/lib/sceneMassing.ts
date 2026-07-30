import type { SceneItem } from './types'

type Point3 = [number, number, number]
type TopEdge = { a: Point3; b: Point3; height: number }

const pointKey = ([x, y]: Point3) => `${x.toFixed(3)}:${y.toFixed(3)}`

function topEdge(item: SceneItem): TopEdge | null {
  if (item.category !== 'context' || item.vertices.length < 4) return null
  const height = Math.max(...item.vertices.map((vertex) => vertex[2]))
  const top = item.vertices.filter((vertex) => Math.abs(vertex[2] - height) < 1e-3) as Point3[]
  if (top.length !== 2 || pointKey(top[0]) === pointKey(top[1])) return null
  return { a: top[0], b: top[1], height }
}

function polygonArea(vertices: Point3[]) {
  return Math.abs(vertices.reduce((sum, vertex, index) => {
    const next = vertices[(index + 1) % vertices.length]
    return sum + vertex[0] * next[1] - next[0] * vertex[1]
  }, 0)) / 2
}

/** Rebuild visual roof caps from the closed top edges of OpenStudio context walls. */
export function deriveContextRoofs(items: SceneItem[]): SceneItem[] {
  const groups = new Map<string, TopEdge[]>()
  for (const item of items) {
    const edge = topEdge(item)
    if (!edge) continue
    const key = edge.height.toFixed(3)
    groups.set(key, [...(groups.get(key) ?? []), edge])
  }

  const roofs: SceneItem[] = []
  for (const [heightKey, edges] of groups) {
    const unused = new Set(edges.map((_, index) => index))
    while (unused.size) {
      const firstIndex = unused.values().next().value as number
      unused.delete(firstIndex)
      const first = edges[firstIndex]
      const startKey = pointKey(first.a)
      let currentKey = pointKey(first.b)
      const ring: Point3[] = [first.a, first.b]

      while (currentKey !== startKey && unused.size) {
        const nextIndex = Array.from(unused).find((index) => {
          const edge = edges[index]
          return pointKey(edge.a) === currentKey || pointKey(edge.b) === currentKey
        })
        if (nextIndex == null) break
        unused.delete(nextIndex)
        const edge = edges[nextIndex]
        const next = pointKey(edge.a) === currentKey ? edge.b : edge.a
        ring.push(next)
        currentKey = pointKey(next)
      }

      if (currentKey !== startKey || ring.length < 4) continue
      ring.pop()
      const area = polygonArea(ring)
      if (area < 0.1) continue
      roofs.push({
        id: `context-roof-${heightKey}-${roofs.length + 1}`,
        name: `Context roof ${roofs.length + 1}`,
        category: 'context',
        context_role: 'roof',
        vertices: ring,
        area_m2: Number(area.toFixed(3)),
        group_type: 'Site',
      })
    }
  }
  return roofs
}
