import type { ReactNode } from 'react'
import { Building2, GitCompareArrows, Languages, Layers3, ListChecks, Play } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { readinessClass } from '../lib/storage'
import { useActiveBuilding } from '../lib/activeBuilding'

export default function AppShell({ children }: { children: ReactNode }) {
  const { t, i18n } = useTranslation()
  const { activeBuilding } = useActiveBuilding()
  const health = useQuery({ queryKey: ['health-mini'], queryFn: api.health, refetchInterval: 30_000 })
  const capabilities = useQuery({ queryKey: ['capabilities'], queryFn: api.capabilities, staleTime: 5_000, refetchInterval: 10_000 })
  const navGroups = [
    {
      key: 'buildingWorkflow',
      items: [
        { to: '/builder', key: 'builder', icon: Layers3 },
        ...(capabilities.data?.capabilities.simulation?.runtime_ready ? [{ to: '/simulation', key: 'simulation', icon: Play }] : []),
        { to: '/compare', key: 'compare', icon: GitCompareArrows },
        { to: '/runs', key: 'runs', icon: ListChecks },
      ],
    },
  ]

  const switchLanguage = async () => {
    const next = i18n.language === 'tr' ? 'en' : 'tr'
    localStorage.setItem('workbench-language', next)
    await i18n.changeLanguage(next)
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">{t('shell.skip')}</a>
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true"><Building2 size={18} /></div>
          <div>
            <strong>VAL / ONE BUILDING</strong>
            <span>{t('shell.workbench')}</span>
          </div>
        </div>
        <div className="topbar-center">
          <Building2 size={14} />
          <span>{t('shell.activeBuilding')}</span>
          <span className="topbar-separator" />
          <code>{activeBuilding}</code>
        </div>
        <div className="topbar-actions">
          <div className={`health-chip ${health.isError ? 'is-bad' : readinessClass(health.data?.readiness)}`}>
            <span className="status-dot" />
            {health.data?.ok ? `OpenStudio ${health.data.openstudio_version} · ${health.data.readiness}` : t('common.environment')}
          </div>
          <button className="icon-text-button" onClick={switchLanguage} title="TR / EN">
            <Languages size={16} />
            {i18n.language.toUpperCase()}
          </button>
        </div>
      </header>

      <aside className="sidebar">
        <div className="active-building-card">
          <span>{t('shell.activeBuilding')}</span>
          <code>{activeBuilding}</code>
          <small>{t('shell.changeInBuilder')}</small>
        </div>
        <nav aria-label={t('nav.primary')}>
          {navGroups.filter((group) => group.items.length).map((group) => <section className="nav-group" key={group.key}>
            <span className="nav-group-label">{t(`nav.groups.${group.key}`)}</span>
            {group.items.map(({ to, key, icon: Icon }) => (
              <NavLink key={to} to={to} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
                <Icon size={17} strokeWidth={1.8} />
                <span>{t(`nav.${key}`)}</span>
              </NavLink>
            ))}
          </section>)}
        </nav>
        <div className="sidebar-foot">
          <span>{t('common.localOnly')}</span>
          <code>127.0.0.1</code>
        </div>
      </aside>

      <main className="app-main" id="main-content" tabIndex={-1}>{children}</main>
    </div>
  )
}
