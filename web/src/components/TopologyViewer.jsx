import {
  ReactFlow, Background, Controls,
  useNodesState, useEdgesState,
  Handle, Position,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Badge } from '@/components/ui/badge'

function VMNode({ data }) {
  return (
    <div className={`
      px-4 py-3 rounded-lg border-2 bg-card shadow-md min-w-36 text-center
      ${data.status === 'running' ? 'border-green-500' : ''}
      ${data.status === 'error'   ? 'border-destructive' : ''}
      ${data.status === 'stopped' ? 'border-muted' : ''}
    `}>
      <Handle type="source" position={Position.Right} id="s" style={{ background: '#6366f1' }} />
      <Handle type="target" position={Position.Left}  id="t" style={{ background: '#6366f1' }} />
      <div className="text-xs font-bold mb-1">{data.label}</div>
      <div className="text-xs text-muted-foreground">{data.server}</div>
      <div className="text-xs text-muted-foreground">VNC :{data.vnc_port}</div>
      <Badge
        variant={data.status === 'running' ? 'default' : 'secondary'}
        className="mt-2 text-xs"
      >
        {data.status}
      </Badge>
    </div>
  )
}

const nodeTypes = { vm: VMNode }

function buildNodes(vms, topology) {
  const count = vms.length
  return vms.map((vm, i) => {
    let x, y
    if (topology === 'ring') {
      const angle = (2 * Math.PI * i) / count - Math.PI / 2
      x = 300 + 200 * Math.cos(angle)
      y = 180 + 150 * Math.sin(angle)
    } else {
      x = 60 + i * 280
      y = 150
    }
    return {
      id:       vm.name,
      type:     'vm',
      position: { x, y },
      data: {
        label:    vm.name,
        server:   vm.server,
        vnc_port: vm.vnc_port,
        status:   vm.status,
      },
    }
  })
}

function buildEdges(vms, topology) {
  const edges = []
  for (let i = 0; i < vms.length - 1; i++) {
    edges.push({
      id:           `e${i}`,
      source:       vms[i].name,
      target:       vms[i + 1].name,
      sourceHandle: 's',
      targetHandle: 't',
      type:         'straight',
      style:        { stroke: '#6366f1', strokeWidth: 2 },
    })
  }
  if (topology === 'ring' && vms.length >= 3) {
    edges.push({
      id:           'e-close',
      source:       vms[vms.length - 1].name,
      target:       vms[0].name,
      sourceHandle: 's',
      targetHandle: 't',
      type:         'straight',
      style:        { stroke: '#6366f1', strokeWidth: 2 },
    })
  }
  return edges
}

export default function TopologyViewer({ slice }) {
  const [nodes, , onNodesChange] = useNodesState(buildNodes(slice.vms, slice.topology))
  const [edges, , onEdgesChange] = useEdgesState(buildEdges(slice.vms, slice.topology))

  return (
    <div style={{ height: 340 }} className="rounded-lg border bg-muted/30">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}