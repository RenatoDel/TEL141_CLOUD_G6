import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Server, Cpu, Network, Activity } from 'lucide-react'

const stats = [
  { title: 'Slices Activos',   value: '0', icon: Network,  desc: 'desplegados en el cluster' },
  { title: 'VMs Corriendo',    value: '0', icon: Cpu,      desc: 'en server1 y server2'      },
  { title: 'Servidores',       value: '3', icon: Server,   desc: 'server1, server2, server3' },
  { title: 'Jobs en Cola',     value: '0', icon: Activity, desc: 'pendientes de ejecutar'    },
]

const recentSlices = []

export default function Dashboard() {
  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-2xl font-bold">Dashboard</h2>
        <p className="text-muted-foreground">Vista general del cluster Linux</p>
      </div>

      {/* Métricas */}
      <div className="grid grid-cols-4 gap-4">
        {stats.map(({ title, value, icon: Icon, desc }) => (
          <Card key={title}>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardTitle className="text-sm font-medium">{title}</CardTitle>
              <Icon size={16} className="text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{value}</div>
              <p className="text-xs text-muted-foreground mt-1">{desc}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Slices recientes */}
      <div>
        <h3 className="text-lg font-semibold mb-4">Slices Recientes</h3>
        {recentSlices.length === 0 ? (
          <Card>
            <CardContent className="flex flex-col items-center justify-center py-12">
              <Network size={40} className="text-muted-foreground mb-3" />
              <p className="text-muted-foreground">No hay slices desplegados aún</p>
              <a href="/slices/new"
                className="mt-4 text-sm text-primary underline underline-offset-4">
                Crear primer slice →
              </a>
            </CardContent>
          </Card>
        ) : null}
      </div>
    </div>
  )
}