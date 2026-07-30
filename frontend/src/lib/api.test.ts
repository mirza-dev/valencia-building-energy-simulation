import { describe, expect, it } from 'vitest'
import { absoluteApiUrl } from './api'

describe('absolute API resource URLs', () => {
  it('uses the browser origin when no API base is configured and preserves tile placeholders', () => {
    expect(absoluteApiUrl('/api/map/tiles/{z}/{x}/{y}.mvt', '', 'http://127.0.0.1:8765')).toBe(
      'http://127.0.0.1:8765/api/map/tiles/{z}/{x}/{y}.mvt',
    )
  })

  it('resolves a relative API base to an absolute browser URL', () => {
    expect(absoluteApiUrl('figures/tornado.png', '/workbench-api/', 'https://example.test')).toBe(
      'https://example.test/workbench-api/figures/tornado.png',
    )
  })

  it('keeps an explicitly configured remote API base', () => {
    expect(absoluteApiUrl('/api/city/map/tiles/{z}/{x}/{y}.mvt', 'https://api.example.test/root', 'https://ui.example.test')).toBe(
      'https://api.example.test/root/api/city/map/tiles/{z}/{x}/{y}.mvt',
    )
  })
})
