import type { ReactNode } from 'react'
import { Archive, BookOpen, Building2, ExternalLink, Files, Play } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { inputReadiness, shortHash } from '../lib/productStock'
import { readinessClass } from '../lib/storage'

export default function AppShell({ children }: { children: ReactNode }) {
  const health = useQuery({ queryKey: ['health-mini'], queryFn: api.health, refetchInterval: 30_000 })
  const profile = useQuery({ queryKey: ['stock-profile'], queryFn: api.stockProfile, staleTime: 30_000 })
  // The published documents, opened in a new tab so the operator never loses
  // the run they are watching to read about it.
  const helpItems = [
    { href: '/help/user-guide.html', label: 'User Guide', note: 'How to run it' },
    { href: '/help/installation-guide.html', label: 'Installation', note: 'Set-up' },
    { href: '/help/valencia-simulation-report.html', label: 'Valencia Report', note: 'Method and results' },
  ]
  const navItems = [
    { to: '/files', label: 'Files', note: 'Inputs', icon: Files },
    { to: '/run', label: 'Run', note: 'Execution', icon: Play },
    { to: '/outputs', label: 'Outputs', note: 'Evidence', icon: Archive },
  ]
  const readiness = inputReadiness(profile.data, profile.isError)

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true"><Building2 size={18} /></div>
          <div><strong>BSEW</strong><span>Building Stock Energy Workbench</span></div>
        </div>
        <div className="topbar-center">
          <span>VERIFIED MODEL PROFILE</span><span className="topbar-separator" />
          <code title={profile.data?.profile.fingerprint}>{shortHash(profile.data?.profile.fingerprint, 16)}</code>
          <span className={`profile-ready-dot ${readiness.state}`} title={readiness.reason || undefined}>
            {readiness.state === 'ready' ? 'READY' : readiness.state === 'checking' ? 'CHECKING…' : 'CHECK INPUTS'}
          </span>
        </div>
        <div className="topbar-actions">
          <div className={`health-chip ${health.isError ? 'is-bad' : readinessClass(health.data?.readiness)}`}>
            <span className="status-dot" />
            {health.data?.ok ? `OpenStudio ${health.data.openstudio_version} · ${health.data.readiness}` : 'Environment'}
          </div>
        </div>
      </header>

      <aside className="sidebar">
        <div className="product-sidebar-intro"><span>PIPELINE</span><strong>Per-building stock simulation</strong><small>One verified model, three operational surfaces.</small></div>
        <nav aria-label="Primary navigation">
          <section className="nav-group">
            <span className="nav-group-label">WORKFLOW</span>
            {navItems.map(({ to, label, note, icon: Icon }, index) => (
              <NavLink key={to} to={to} className={({ isActive }) => `nav-link product-nav-link ${isActive ? 'active' : ''}`}>
                <i>{String(index + 1).padStart(2, '0')}</i><Icon size={17} strokeWidth={1.8} />
                <span><strong>{label}</strong><small>{note}</small></span>
              </NavLink>
            ))}
          </section>
          <section className="nav-group">
            <span className="nav-group-label">HELP</span>
            {helpItems.map(({ href, label, note }) => (
              <a key={href} className="nav-link product-nav-link help-nav-link" href={href}
                 target="_blank" rel="noreferrer">
                <i aria-hidden="true"><BookOpen size={13} strokeWidth={1.8} /></i>
                <ExternalLink size={17} strokeWidth={1.8} />
                <span><strong>{label}</strong><small>{note}</small></span>
              </a>
            ))}
          </section>
        </nav>
        <div className="sidebar-foot"><span>LOCAL ONLY</span><code>127.0.0.1</code></div>
      </aside>

      <main className="app-main" id="main-content" tabIndex={-1}>{children}</main>
    </div>
  )
}
