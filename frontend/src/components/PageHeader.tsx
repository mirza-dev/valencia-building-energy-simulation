import type { ReactNode } from 'react'

export default function PageHeader({ eyebrow, title, subtitle, actions }: {
  eyebrow: string
  title: string
  subtitle: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{subtitle}</p>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  )
}
