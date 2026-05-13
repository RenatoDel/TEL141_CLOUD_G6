import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table, TableBody, TableCell,
  TableHead, TableHeader, TableRow
} from '@/components/ui/table'
import { PlusCircle, Network, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import { toast } from 'sonner'
import TopologyViewer from '@/components/TopologyViewer'
import { getSlices, deleteSlice } from '@/api/slices'

const STATUS_COLORS = {
  running:  'default',
  creating: 'secondary',
  error:    'destructive',
  deleting: 'secondary',
  deleted:  'outline',
}

export default function Slices() {
  const [expanded, setExpanded] = useState(null)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data: slices = [], isLoading, error } = useQuery({
    queryKey: ['slices'],
    queryFn:  getSlices,
    refetchInterval: 5000,
  })

  const deleteMutation = useMutation({
    mutationFn: (slice_uid) => deleteSlice(slice_uid),
    onSuccess: (data) => {
      toast.success('Slice encolado para borrado')
      queryClient.invalidateQueries(['slices'])
      navigate(`/jobs/${data.job_uid}`)
    },
    onError: () => toast.error('Error al borrar el slice'),
  })

  function toggleExpand(id) {
    setExpanded(prev => prev === id ? null : id)
  }

  if (isLoading) return <div className="text-muted-foreground">Cargando slices...</div>
  if (error)     return <div className="text-destructive">Error conectando al servidor</div>

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
                    key={slice.slice_uid}
                    className="cursor-pointer hover:bg-muted/50"
                    onClick={() => toggleExpand(slice.slice_uid)}
                  >
                    <TableCell>
                      {expanded === slice.slice_uid
                        ? <ChevronUp size={14} />
                        : <ChevronDown size={14} />
                      }
                    </TableCell>
                    <TableCell className="font-medium">{slice.nombre}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{slice.topologia}</Badge>
                    </TableCell>
                    <TableCell>{slice.vlan_id}</TableCell>
                    <TableCell className="font-mono text-sm">{slice.cidr}</TableCell>
                    <TableCell>{slice.vms?.length ?? 0}</TableCell>
                    <TableCell>
                      <Badge variant={STATUS_COLORS[slice.estado] ?? 'outline'}>
                        {slice.estado}
                      </Badge>
                    </TableCell>
                    <TableCell onClick={e => e.stopPropagation()}>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => deleteMutation.mutate(slice.slice_uid)}
                      >
                        <Trash2 size={16} className="text-destructive" />
                      </Button>
                    </TableCell>
                  </TableRow>

                  {expanded === slice.slice_uid && (
                    <TableRow key={`${slice.slice_uid}-detail`}>
                      <TableCell colSpan={8} className="bg-muted/20 p-4">
                        <CardHeader className="p-0 pb-3">
                          <CardTitle className="text-sm">
                            Topología — {slice.topologia} · VLAN {slice.vlan_id} · {slice.cidr}
                          </CardTitle>
                        </CardHeader>
                        {slice.vms?.length > 0 ? (
                          <TopologyViewer slice={{
                            topology: slice.topologia,
                            vms: slice.vms.map(vm => ({
                              name:     vm.nombre,
                              server:   vm.servidor,
                              vnc_port: vm.vnc_port,
                              status:   vm.estado,
                            }))
                          }} />
                        ) : (
                          <p className="text-sm text-muted-foreground">
                            Sin VMs desplegadas aún
                          </p>
                        )}
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