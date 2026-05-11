import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table, TableBody, TableCell,
  TableHead, TableHeader, TableRow
} from '@/components/ui/table'
import { PlusCircle, Network, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import TopologyViewer from '@/components/TopologyViewer'

const STATUS_COLORS = {
  running:  'default',
  creating: 'secondary',
  error:    'destructive',
  deleting: 'secondary',
}

const mockSlices = [
  {
    id:       'slice-linear-001',
    name:     'slice-linear-001',
    topology: 'linear',
    vlan_id:  101,
    cidr:     '192.168.101.0/24',
    vm_count: 3,
    status:   'running',
    servers:  'server1, server2',
    vms: [
      { name: 'slice-linear-001-vm1', server: 'server1', vnc_port: 5901, status: 'running' },
      { name: 'slice-linear-001-vm2', server: 'server1', vnc_port: 5902, status: 'running' },
      { name: 'slice-linear-001-vm3', server: 'server2', vnc_port: 5903, status: 'running' },
    ],
  },
  {
    id:       'slice-ring-001',
    name:     'slice-ring-001',
    topology: 'ring',
    vlan_id:  201,
    cidr:     '192.168.201.0/24',
    vm_count: 3,
    status:   'running',
    servers:  'server1, server2',
    vms: [
      { name: 'slice-ring-001-vm1', server: 'server1', vnc_port: 5911, status: 'running' },
      { name: 'slice-ring-001-vm2', server: 'server2', vnc_port: 5912, status: 'running' },
      { name: 'slice-ring-001-vm3', server: 'server1', vnc_port: 5913, status: 'running' },
    ],
  },
]

export default function Slices() {
  const [expanded, setExpanded] = useState(null)
  const slices = mockSlices

  function toggleExpand(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Slices</h2>
          <p className="text-muted-foreground">Topologías desplegadas en el cluster</p>
        </div>
        <Link to="/slices/new">
          <Button>
            <PlusCircle size={16} className="mr-2" />
            Nuevo Slice
          </Button>
        </Link>
      </div>

      {slices.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Network size={40} className="text-muted-foreground mb-3" />
            <p className="text-muted-foreground">No hay slices desplegados</p>
            <Link to="/slices/new">
              <Button variant="outline" className="mt-4">Crear primer slice</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead></TableHead>
                <TableHead>Nombre</TableHead>
                <TableHead>Topología</TableHead>
                <TableHead>VLAN</TableHead>
                <TableHead>CIDR</TableHead>
                <TableHead>VMs</TableHead>
                <TableHead>Estado</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {slices.map((slice) => (
                <>
                  <TableRow
                    key={slice.id}
                    className="cursor-pointer hover:bg-muted/50"
                    onClick={() => toggleExpand(slice.id)}
                  >
                    <TableCell>
                      {expanded === slice.id
                        ? <ChevronUp size={14} />
                        : <ChevronDown size={14} />
                      }
                    </TableCell>
                    <TableCell className="font-medium">{slice.name}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{slice.topology}</Badge>
                    </TableCell>
                    <TableCell>{slice.vlan_id}</TableCell>
                    <TableCell className="font-mono text-sm">{slice.cidr}</TableCell>
                    <TableCell>{slice.vm_count}</TableCell>
                    <TableCell>
                      <Badge variant={STATUS_COLORS[slice.status] ?? 'outline'}>
                        {slice.status}
                      </Badge>
                    </TableCell>
                    <TableCell onClick={e => e.stopPropagation()}>
                      <Button variant="ghost" size="icon">
                        <Trash2 size={16} className="text-destructive" />
                      </Button>
                    </TableCell>
                  </TableRow>

                  {expanded === slice.id && (
                    <TableRow key={`${slice.id}-detail`}>
                      <TableCell colSpan={8} className="bg-muted/20 p-4">
                        <CardHeader className="p-0 pb-3">
                          <CardTitle className="text-sm">
                            Topología — {slice.topology} · VLAN {slice.vlan_id} · {slice.cidr}
                          </CardTitle>
                        </CardHeader>
                        <TopologyViewer slice={slice} />
                      </TableCell>
                    </TableRow>
                  )}
                </>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}