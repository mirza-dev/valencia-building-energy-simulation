import type { StorageCleanupCategory, StorageCleanupPlan } from './types'

export function formatBytes(value: number, digits = 1): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  const amount = value / 1024 ** index
  return `${amount.toFixed(index === 0 ? 0 : digits)} ${units[index]}`
}

export function readinessClass(status?: string): 'is-ok' | 'is-warn' | 'is-bad' | '' {
  if (status === 'READY') return 'is-ok'
  if (status === 'WARNING') return 'is-warn'
  if (status === 'BLOCKED') return 'is-bad'
  return ''
}

export function cleanupCategoryKeys(plan: StorageCleanupPlan): StorageCleanupCategory[] {
  return Object.entries(plan.categories)
    .filter(([, value]) => Boolean(value?.count))
    .map(([key]) => key as StorageCleanupCategory)
}
