import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CheckCircle, XCircle, Loader, Clock, Server } from 'lucide-react'

// mock — después viene del API real
function fetchJob(jobId) {
  const steps = [
    { label: 'Validando request',       status: 'done'    },
    { label: 'VM Placement',            status: 'done'    },
    { label: 'Configurando red VLAN',   status: 'running' },
    { label: 'Creando VMs en cluster',  status: 'pending' },
    { label: 'Verificando estado VMs',  status: 'pending' },
  ]
  return Promise.resolve({
    job_id:   jobId,
    status:   'running',
    slice_id: 'slice-lab4-001',
    topology: 'linear',
    steps,
    vms: [],
    error: null,
  })
}

const STEP_ICON = {
  done:    <CheckCircle size={16} className="text-green-500" />,
  running: <Loader size={16} className="text-blue-500 animate-spin" />,
  pending: <Clock size={16} className="text-muted-foreground" />,
  error:   <XCircle size={16} className="text-destructive" />,
}

const JOB_BADGE = {
  running:   'secondary',
  completed: 'default',
  failed:    'destructive',
}

export default function JobStatus() {
  const { id }     = useParams()
  const navigate   = useNavigate()

  const { data: job, isLoading } = useQuery({
    queryKey: ['job', id],
    queryFn:  () => fetchJob(id),
    refetchInterval: (query) =>
      query.state.data?.status === 'completed' ||
      query.state.data?.status === 'failed'
        ? false : 2000,
  })

  if (isLoading) return (
    <div className="flex items-center gap-2 text-muted-foreground">
      <Loader size={16} className="animate-spin" /> Cargando...
    </div>
  )

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Estado del Deployment</h2>
          <p className="text-muted-foreground font-mono text-sm">{id}</p>
        </div>
        <Badge variant={JOB_BADGE[job.status] ?? 'outline'}>
          {job.status}
        </Badge>
      </div>

      {/* Pasos */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Progreso</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {job.steps.map((step, i) => (
            <div key={i} className="flex items-center gap-3">
              {STEP_ICON[step.status]}
              <span className={`text-sm ${
                step.status === 'pending' ? 'text-muted-foreground' : ''
              }`}>
                {step.label}
              </span>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* VMs resultado */}
      {job.vms.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">VMs desplegadas</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {job.vms.map((vm, i) => (
              <div key={i} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <Server size={14} />
                  <span className="font-mono">{vm.name}</span>
                </div>
                <div className="flex items-center gap-4 text-muted-foreground">
                  <span>{vm.server}</span>
                  <span>VNC :{vm.vnc_port}</span>
                  <Badge variant="outline">{vm.status}</Badge>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* Error */}
      {job.error && (
        <Card className="border-destructive">
          <CardContent className="pt-4">
            <p className="text-destructive text-sm font-mono">{job.error}</p>
          </CardContent>
        </Card>
      )}

      <div className="flex gap-3">
        <Button variant="outline" onClick={() => navigate('/slices')}>
          Ver todos los slices
        </Button>
      </div>
    </div>
  )
}