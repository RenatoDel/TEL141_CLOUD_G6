import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { createSlice } from '@/api/slices'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select, SelectContent, SelectItem,
  SelectTrigger, SelectValue
} from '@/components/ui/select'
import { toast } from 'sonner'

const TOPOLOGIES = ['linear', 'ring']
const SERVERS    = ['server1', 'server2']

export default function SliceNew() {
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name:      '',
    topology:  'linear',
    vlan_id:   '',
    cidr:      '',
    vm_count:  2,
    vnc_start: 5901,
    servers:   ['server1', 'server1'],
  })
  const mutation = useMutation({
    mutationFn: createSlice,
    onSuccess: (data) => {
      toast.success('Slice encolado correctamente')
      navigate(`/jobs/${data.job_uid}`)
    },
    onError: (err) => {
      toast.error(err?.response?.data?.detail || 'Error al crear el slice')
    },
  })

  function setField(key, value) {
    setForm(f => ({ ...f, [key]: value }))
  }

  function setServer(index, value) {
    const servers = [...form.servers]
    servers[index] = value
    setForm(f => ({ ...f, servers }))
  }

  function handleVmCount(value) {
    const n = parseInt(value)
    const servers = Array.from({ length: n }, (_, i) => form.servers[i] ?? 'server1')
    setForm(f => ({ ...f, vm_count: n, servers }))
  }

  async function handleSubmit() {
    if (!form.name || !form.vlan_id || !form.cidr) {
      toast.error('Completa todos los campos requeridos')
      return
    }
    if (form.topology === 'ring' && form.vm_count < 3) {
      toast.error('La topología anillo requiere mínimo 3 VMs')
      return
    }

    mutation.mutate({
      nombre:    form.name,
      topology:  form.topology,
      vlan_id:   parseInt(form.vlan_id),
      cidr:      form.cidr,
      vm_count:  form.vm_count,
      servers:   form.servers,
      vnc_start: form.vnc_start,
    })
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h2 className="text-2xl font-bold">Nuevo Slice</h2>
        <p className="text-muted-foreground">Despliega una topología en el cluster Linux</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Configuración general</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">

          <div className="space-y-1">
            <Label>Nombre del slice</Label>
            <Input
              placeholder="ej: slice-lab4-001"
              value={form.name}
              onChange={e => setField('name', e.target.value)}
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label>Topología</Label>
              <Select value={form.topology} onValueChange={v => setField('topology', v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TOPOLOGIES.map(t => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1">
              <Label>Número de VMs</Label>
              <Select
                value={String(form.vm_count)}
                onValueChange={handleVmCount}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {[2,3,4,5].map(n => (
                    <SelectItem key={n} value={String(n)}>{n} VMs</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label>VLAN ID</Label>
              <Input
                type="number"
                placeholder="ej: 100"
                value={form.vlan_id}
                onChange={e => setField('vlan_id', e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label>CIDR</Label>
              <Input
                placeholder="ej: 192.168.100.0/24"
                value={form.cidr}
                onChange={e => setField('cidr', e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-1">
            <Label>Puerto VNC inicial</Label>
            <Input
              type="number"
              value={form.vnc_start}
              onChange={e => setField('vnc_start', parseInt(e.target.value))}
            />
          </div>

        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Asignación de VMs a servidores</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {Array.from({ length: form.vm_count }, (_, i) => (
            <div key={i} className="flex items-center gap-4">
              <span className="text-sm w-16 text-muted-foreground">VM {i + 1}</span>
              <Select value={form.servers[i]} onValueChange={v => setServer(i, v)}>
                <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {SERVERS.map(s => (
                    <SelectItem key={s} value={s}>{s}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="flex gap-3">
        <Button onClick={handleSubmit} disabled={mutation.isPending}>
          {mutation.isPending ? 'Desplegando...' : 'Crear Slice'}
        </Button>
        <Button variant="outline" onClick={() => navigate('/slices')}>
          Cancelar
        </Button>
      </div>
    </div>
  )
}