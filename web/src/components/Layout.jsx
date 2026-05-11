import { Link, useLocation } from 'react-router-dom'
import { LayoutDashboard, Server, PlusCircle } from 'lucide-react'

const navItems = [
  { path: '/',          label: 'Dashboard',    icon: LayoutDashboard },
  { path: '/slices',    label: 'Slices',       icon: Server },
  { path: '/slices/new',label: 'Crear Slice',  icon: PlusCircle },
]

export default function Layout({ children }) {
  const location = useLocation()

  return (
    <div className="flex h-screen bg-background">
      {/* Sidebar */}
      <aside className="w-64 border-r flex flex-col">
        <div className="p-6 border-b">
          <h1 className="text-lg font-bold">PUCP Cloud</h1>
          <p className="text-xs text-muted-foreground">TEL141 — Grupo 6</p>
        </div>
        <nav className="flex-1 p-4 space-y-1">
          {navItems.map(({ path, label, icon: Icon }) => {
            const active = location.pathname === path
            return (
              <Link
                key={path}
                to={path}
                className={`flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors
                  ${active
                    ? 'bg-primary text-primary-foreground'
                    : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                  }`}
              >
                <Icon size={16} />
                {label}
              </Link>
            )
          })}
        </nav>
        <div className="p-4 border-t">
          <p className="text-xs text-muted-foreground">Linux Cluster — Fase 1</p>
        </div>
      </aside>

      {/* Contenido */}
      <main className="flex-1 overflow-auto p-8">
        {children}
      </main>
    </div>
  )
}