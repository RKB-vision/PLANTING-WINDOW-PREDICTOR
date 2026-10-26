import Link from 'next/link'
import type { ReactNode } from 'react'

type NavKey = 'dashboard' | 'feedback'

export default function Layout({
  active,
  children,
}: {
  active: NavKey
  children: ReactNode
}) {
  return (
    <div className="app-shell">
      <nav className="nav">
        <div className="nav-inner">
          <Link href="/" className="brand">
            <span className="brand-mark" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M12 21c0-5.5 3.6-9.3 9-10.5C19.8 16.5 16.4 19.7 12 21z" />
                <path d="M12 21C12 15.5 8.4 11.7 3 10.5 4.2 16.5 7.6 19.7 12 21z" />
                <path d="M12 21v-5" />
              </svg>
            </span>
            Plant Readiness
          </Link>
          <div className="nav-links">
            <Link
              href="/"
              className={'nav-link' + (active === 'dashboard' ? ' is-active' : '')}
            >
              Dashboard
            </Link>
            <Link
              href="/feedback"
              className={'nav-link' + (active === 'feedback' ? ' is-active' : '')}
            >
              Feedback
            </Link>
          </div>
        </div>
      </nav>

      <main className="container">{children}</main>

      <footer className="footer">
        Plant Readiness · Species-level prediction · Next.js UI + Flask backend
      </footer>
    </div>
  )
}
