/**
 * lib/topology-canvas.js
 *
 * Motor de canvas SVG para editar topologías de red visualmente:
 *   - Arrastrar nodos para posicionarlos.
 *   - Click en un nodo, luego click en otro para crear un enlace.
 *   - Doble click en un nodo para editar su configuración (vcpus/ram/disk/etc).
 *   - Click derecho (o botón "x") para borrar nodo/enlace.
 *
 * El estado del grafo vive en `this.nodes` / `this.links` con la forma
 * exacta que espera el backend (GraphNodeSpec / GraphLinkSpec), más
 * un campo `x`/`y` por nodo SOLO para el layout visual (no se envía al API).
 *
 * Uso:
 *   const canvas = new TopologyCanvas(svgEl, {
 *     onNodeEdit: (node) => {...},   // abrir modal de edición
 *     onChange: () => {...},         // cada vez que cambia el grafo
 *   });
 *   canvas.loadTemplate("ring", 5);
 *   canvas.toPayloadNodes() / canvas.toPayloadLinks()
 */

const NODE_RADIUS = 28;

let nodeIdCounter = 0;
let linkIdCounter = 0;

function nextNodeName() {
  nodeIdCounter += 1;
  return `vm${nodeIdCounter}`;
}

function nextLinkId() {
  linkIdCounter += 1;
  return `link${linkIdCounter}`;
}

export class TopologyCanvas {
  constructor(svgEl, { onChange, onNodeEdit } = {}) {
    this.svg = svgEl;
    this.onChange = onChange || (() => {});
    this.onNodeEdit = onNodeEdit || (() => {});

    this.nodes = []; // {name, x, y, vcpus, ram_mb, disk_gb, image_name, internet, preferred_worker}
    this.links = []; // {id, from_node, to_node}

    this.selectedNodeForLink = null;
    this.dragState = null;

    this._setupSvg();
  }

  _setupSvg() {
    this.svg.setAttribute("viewBox", "0 0 800 480");
    this.svg.style.touchAction = "none";

    this.linksLayer = this._svgEl("g", { class: "links-layer" });
    this.nodesLayer = this._svgEl("g", { class: "nodes-layer" });
    this.svg.append(this.linksLayer, this.nodesLayer);

    this.svg.addEventListener("pointermove", (e) => this._handlePointerMove(e));
    this.svg.addEventListener("pointerup", () => this._handlePointerUp());
    this.svg.addEventListener("pointerleave", () => this._handlePointerUp());
  }

  _svgEl(tag, attrs = {}) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  _svgPoint(evt) {
    const rect = this.svg.getBoundingClientRect();
    const viewBox = this.svg.viewBox.baseVal;
    const x = ((evt.clientX - rect.left) / rect.width) * viewBox.width;
    const y = ((evt.clientY - rect.top) / rect.height) * viewBox.height;
    return { x, y };
  }

  // ════════════════════════════════════════════════════════════════════
  // API pública de manipulación del grafo
  // ════════════════════════════════════════════════════════════════════
  clear() {
    this.nodes = [];
    this.links = [];
    nodeIdCounter = 0;
    linkIdCounter = 0;
    this._render();
  }

  addNode(x, y, overrides = {}) {
    const node = {
      name: nextNodeName(),
      x,
      y,
      vcpus: 1,
      ram_mb: 256,
      disk_gb: 10,
      image_name: "cirros-base.img",
      internet: false,
      preferred_worker: null,
      ...overrides,
    };
    this.nodes.push(node);
    this._render();
    this.onChange();
    return node;
  }

  removeNode(name) {
    this.nodes = this.nodes.filter((n) => n.name !== name);
    this.links = this.links.filter((l) => l.from_node !== name && l.to_node !== name);
    if (this.selectedNodeForLink === name) this.selectedNodeForLink = null;
    this._render();
    this.onChange();
  }

  addLink(fromName, toName) {
    if (fromName === toName) return null;
    const exists = this.links.some(
      (l) =>
        (l.from_node === fromName && l.to_node === toName) ||
        (l.from_node === toName && l.to_node === fromName)
    );
    if (exists) return null;
    const link = { id: nextLinkId(), from_node: fromName, to_node: toName };
    this.links.push(link);
    this._render();
    this.onChange();
    return link;
  }

  removeLink(id) {
    this.links = this.links.filter((l) => l.id !== id);
    this._render();
    this.onChange();
  }

  updateNode(name, patch) {
    const node = this.nodes.find((n) => n.name === name);
    if (!node) return;
    Object.assign(node, patch);
    this._render();
    this.onChange();
  }

  renameNode(oldName, newName) {
    if (oldName === newName) return true;
    if (this.nodes.some((n) => n.name === newName)) return false;
    const node = this.nodes.find((n) => n.name === oldName);
    if (!node) return false;
    node.name = newName;
    this.links.forEach((l) => {
      if (l.from_node === oldName) l.from_node = newName;
      if (l.to_node === oldName) l.to_node = newName;
    });
    this._render();
    this.onChange();
    return true;
  }

  toPayloadNodes() {
    return this.nodes.map(({ x, y, ...rest }) => rest);
  }

  toPayloadLinks() {
    return this.links.map(({ id, from_node, to_node }) => ({
      id,
      from: from_node,
      to: to_node,
    }));
  }

  loadFromGraph(nodes, links) {
    // nodes: [{name,...}] sin x/y → generamos layout circular automático
    this.clear();
    const n = nodes.length;
    const cx = 400, cy = 230, r = Math.min(180, 60 + n * 14);
    nodes.forEach((node, idx) => {
      const angle = (2 * Math.PI * idx) / Math.max(n, 1) - Math.PI / 2;
      this.nodes.push({
        ...node,
        x: cx + r * Math.cos(angle),
        y: cy + r * Math.sin(angle),
      });
      const num = parseInt((node.name.match(/\d+/) || [0])[0], 10);
      if (num >= nodeIdCounter) nodeIdCounter = num;
    });
    links.forEach((link) => {
      this.links.push({
        id: link.id,
        from_node: link.from || link.from_node,
        to_node: link.to || link.to_node,
      });
    });
    this._render();
    this.onChange();
  }

  // ════════════════════════════════════════════════════════════════════
  // Templates predefinidos (cumplen rúbrica R1B: lineal, malla, árbol,
  // anillo, bus)
  // ════════════════════════════════════════════════════════════════════
  loadTemplate(kind, count = 4) {
    this.clear();
    const cx = 400, cy = 230;

    if (kind === "linear") {
      const spacing = 600 / Math.max(count - 1, 1);
      for (let i = 0; i < count; i++) {
        this.addNode(100 + i * spacing, cy);
      }
      for (let i = 0; i < count - 1; i++) {
        this.addLink(this.nodes[i].name, this.nodes[i + 1].name);
      }
    } else if (kind === "ring") {
      const r = Math.min(180, 60 + count * 12);
      for (let i = 0; i < count; i++) {
        const angle = (2 * Math.PI * i) / count - Math.PI / 2;
        this.addNode(cx + r * Math.cos(angle), cy + r * Math.sin(angle));
      }
      for (let i = 0; i < count; i++) {
        this.addLink(this.nodes[i].name, this.nodes[(i + 1) % count].name);
      }
    } else if (kind === "mesh") {
      const r = Math.min(170, 60 + count * 12);
      for (let i = 0; i < count; i++) {
        const angle = (2 * Math.PI * i) / count - Math.PI / 2;
        this.addNode(cx + r * Math.cos(angle), cy + r * Math.sin(angle));
      }
      for (let i = 0; i < count; i++) {
        for (let j = i + 1; j < count; j++) {
          this.addLink(this.nodes[i].name, this.nodes[j].name);
        }
      }
    } else if (kind === "tree") {
      // Árbol binario simple por niveles
      const levels = Math.ceil(Math.log2(count + 1));
      const positions = [];
      let placed = 0;
      for (let level = 0; level < levels && placed < count; level++) {
        const nodesInLevel = Math.min(2 ** level, count - placed);
        const y = 70 + level * (340 / Math.max(levels - 1, 1));
        const spacing = 700 / (nodesInLevel + 1);
        for (let i = 0; i < nodesInLevel; i++) {
          positions.push({ x: 50 + spacing * (i + 1), y });
          placed++;
        }
      }
      positions.forEach((p) => this.addNode(p.x, p.y));
      for (let i = 1; i < this.nodes.length; i++) {
        const parentIdx = Math.floor((i - 1) / 2);
        this.addLink(this.nodes[parentIdx].name, this.nodes[i].name);
      }
    } else if (kind === "bus") {
      // Un nodo "bus" central (primer nodo) conectado a todos los demás,
      // representando el segmento compartido.
      this.addNode(cx, 90, { name: nextNodeName() });
      const busName = this.nodes[0].name;
      const spacing = 600 / Math.max(count - 1, 1);
      for (let i = 0; i < count - 1; i++) {
        this.addNode(100 + i * spacing, 340);
      }
      for (let i = 1; i < this.nodes.length; i++) {
        this.addLink(busName, this.nodes[i].name);
      }
    }
  }

  // ════════════════════════════════════════════════════════════════════
  // Render + interacción
  // ════════════════════════════════════════════════════════════════════
  _render() {
    this.linksLayer.innerHTML = "";
    this.nodesLayer.innerHTML = "";

    for (const link of this.links) {
      const from = this.nodes.find((n) => n.name === link.from_node);
      const to = this.nodes.find((n) => n.name === link.to_node);
      if (!from || !to) continue;

      const line = this._svgEl("line", {
        x1: from.x, y1: from.y, x2: to.x, y2: to.y,
        class: "topo-link",
      });
      line.addEventListener("click", (e) => {
        e.stopPropagation();
        this.removeLink(link.id);
      });
      this.linksLayer.append(line);

      const midX = (from.x + to.x) / 2;
      const midY = (from.y + to.y) / 2;
      const delBtn = this._svgEl("circle", {
        cx: midX, cy: midY, r: 7, class: "topo-link-delete",
      });
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.removeLink(link.id);
      });
      this.linksLayer.append(delBtn);
    }

    for (const node of this.nodes) {
      const isSelected = this.selectedNodeForLink === node.name;
      const group = this._svgEl("g", {
        class: `topo-node ${isSelected ? "topo-node--selected" : ""}`,
        transform: `translate(${node.x}, ${node.y})`,
      });

      const circle = this._svgEl("circle", { r: NODE_RADIUS, class: "topo-node-circle" });
      const label = this._svgEl("text", {
        class: "topo-node-label", "text-anchor": "middle", dy: "4",
      });
      label.textContent = node.name;

      if (node.internet) {
        const dot = this._svgEl("circle", {
          cx: NODE_RADIUS - 6, cy: -NODE_RADIUS + 6, r: 5, class: "topo-node-internet",
        });
        group.append(dot);
      }

      group.append(circle, label);

      group.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        this.dragState = { name: node.name, moved: false };
      });
      group.addEventListener("click", (e) => {
        e.stopPropagation();
        if (this.dragState && this.dragState.moved) return; // fue drag, no click
        this._handleNodeClick(node.name);
      });
      group.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        this.onNodeEdit(node);
      });

      this.nodesLayer.append(group);
    }
  }

  _handleNodeClick(name) {
    if (this.selectedNodeForLink === null) {
      this.selectedNodeForLink = name;
      this._render();
      return;
    }
    if (this.selectedNodeForLink === name) {
      this.selectedNodeForLink = null;
      this._render();
      return;
    }
    this.addLink(this.selectedNodeForLink, name);
    this.selectedNodeForLink = null;
    this._render();
  }

  _handlePointerMove(evt) {
    if (!this.dragState) return;
    const node = this.nodes.find((n) => n.name === this.dragState.name);
    if (!node) return;
    const { x, y } = this._svgPoint(evt);
    node.x = Math.max(NODE_RADIUS, Math.min(800 - NODE_RADIUS, x));
    node.y = Math.max(NODE_RADIUS, Math.min(480 - NODE_RADIUS, y));
    this.dragState.moved = true;
    this._render();
  }

  _handlePointerUp() {
    if (this.dragState && this.dragState.moved) {
      this.onChange();
    }
    this.dragState = null;
  }
}
