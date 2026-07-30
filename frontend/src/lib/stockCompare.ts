import type { StockCompareRow } from './types'

export function maximumAbsoluteDelta(rows: StockCompareRow[], metric: string): number {
  const values = rows
    .map((row) => row.metrics[metric]?.delta)
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    .map(Math.abs)
  return Math.max(0.1, ...values)
}

export function stockDeltaExpression(rows: StockCompareRow[], metric: string, property: string): unknown[] {
  const match: unknown[] = ['match', ['to-string', ['get', property]]]
  rows.forEach((row) => match.push(row.key, row.metrics[metric]?.delta ?? 0))
  match.push(0)
  const limit = maximumAbsoluteDelta(rows, metric)
  return [
    'interpolate', ['linear'], match,
    -limit, '#27658f',
    0, '#eceee7',
    limit, '#b8423a',
  ]
}

export function availableRowMetrics(rows: StockCompareRow[]): string[] {
  return Array.from(new Set(rows.flatMap((row) => Object.keys(row.metrics))))
}
