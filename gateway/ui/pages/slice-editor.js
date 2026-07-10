/**
 * pages/slice-editor.js
 *
 * Página de creación de slices con canvas visual. Cubre:
 *   - Templates predefinidos (lineal, anillo, malla, árbol, bus) — R1B.
 *   - APLICACIÓN ADITIVA de templates: aplicar varios sobre el mismo canvas
 *     (p.ej. anillo + lineal), con auto-rename de nodos para evitar
 *     colisiones. El primer template REEMPLAZA (canvas vacío); los
 *     subsiguientes AGREGAN. Hay un botón explícito "Limpiar" para resetear.
 *   - Edición libre de nodos/enlaces (agregar, mover, conectar, borrar).
 *   - Edición de configuración por VM (vcpus/ram/disk/imagen/internet).
 *   - Selección de cluster (linux/openstack).
 *   - Para profesor: flujo curso → alumno DEPENDIENTE (primero eliges
 *     curso, después el selector de alumnos se filtra a los inscritos
 *     en ese curso).
 *   - Alumnos quedan bloqueados completamente (la ruta redirige).
 *
 * Nota sobre creación asíncrona (módulo de colas): SliceApi.createGraphSlice
 * devuelve inmediatamente (202) con {slice_name, job_id, status:"queued"},
 * SIN esperar a que el deploy físico termine. El detalle del slice
 * (slice-detail.js) es quien hace polling del estado y muestra el progreso
 * en vivo, así que aquí solo navegamos para allá apenas se acepta la request.
 */

import { SliceApi, AuthApi } from "../lib/api.js";
import { TopologyCanvas } from "../lib/topology-canvas.js";
import { h, openModal, showError, showToast } from "../lib/components.js";
import { getUser, getRole, canActOnBehalf, canWrite } from "../lib/auth.js";
import { navigate } from "../lib/router.js";

// ─── Catálogo de imágenes disponibles por cluster ─────────────────────
// Solo se muestran las imágenes verificadas que funcionan en cada cluster.
// Si se agrega una imagen nueva al lab, solo hay que agregarla aquí.
const IMAGE_CATALOG = {
  linux: [
    { value: "cirros-base.img", label: "Cirros 0.6.2 (ligero, 256MB)", min_ram: 256, min_disk: 4 },
    { value: "ubuntu-base.img", label: "Ubuntu 22.04 Server (completo)", min_ram: 1024, min_disk: 10 },
  ],
  openstack: [
    { value: "cirros", label: "Cirros 0.6.2 (ligero, 256MB)", min_ram: 256, min_disk: 4 },
  ],
};

// ─── Opciones discretas para sliders ──────────────────────────────────
// RAM: pasos comunes en MB; el slider mapea a estos valores.
const RAM_OPTIONS = [
  { mb: 128,  label: "128 MB" },
  { mb: 256,  label: "256 MB" },
  { mb: 512,  label: "512 MB" },
  { mb: 1024, label: "1 GB" },
  { mb: 2048, label: "2 GB" },
  { mb: 4096, label: "4 GB" },
  { mb: 8192, label: "8 GB" },
];

const VCPU_OPTIONS = [1, 2, 4, 8];

const DISK_OPTIONS = [
  { gb: 2,  label: "2 GB" },
  { gb: 3,  label: "3 GB" },
  { gb: 4,  label: "4 GB" },
  { gb: 5,  label: "5 GB" },
  { gb: 6,  label: "6 GB" },
  { gb: 8,  label: "8 GB" },
  { gb: 10, label: "10 GB" },
  { gb: 12, label: "12 GB" },
];

/**
 * Devuelve el catálogo de imágenes correspondiente al cluster actualmente
 * seleccionado. Si no se encuentra el cluster, devuelve un fallback con
 * la imagen Cirros para Linux.
 */
function imagesForCluster(cluster) {
  return IMAGE_CATALOG[cluster] || IMAGE_CATALOG.linux;
}

/**
 * Dado un valor en MB, encuentra el índice del slider de RAM más cercano.
 */
function ramOptionIndex(mb) {
  for (let i = 0; i < RAM_OPTIONS.length; i++) {
    if (RAM_OPTIONS[i].mb >= mb) return i;
  }
  return RAM_OPTIONS.length - 1;
}

/**
 * Dado un valor en GB, encuentra el índice del slider de disco más cercano.
 */
function diskOptionIndex(gb) {
  for (let i = 0; i < DISK_OPTIONS.length; i++) {
    if (DISK_OPTIONS[i].gb >= gb) return i;
  }
  return DISK_OPTIONS.length - 1;
}

/**
 * Dado un valor de vCPUs, encuentra el índice del slider más cercano.
 */
function vcpuOptionIndex(v) {
  for (let i = 0; i < VCPU_OPTIONS.length; i++) {
    if (VCPU_OPTIONS[i] >= v) return i;
  }
  return VCPU_OPTIONS.length - 1;
}

const TEMPLATES = [
  { key: "linear", label: "Lineal" },
  { key: "ring", label: "Anillo" },
  { key: "mesh", label: "Malla" },
  { key: "tree", label: "Árbol" },
  { key: "bus", label: "Bus" },
];

export async function renderSliceEditor(container, params = {}) {
  // Roles sin permiso de escritura (coach, alumno) no pueden estar aquí.
  // canWrite() ya devuelve false para ambos en la nueva auth.js.
  if (!canWrite()) {
    navigate("/slices");
    return;
  }
  const user = getUser();
  const role = getRole();
  const editingDraftName = params?.name || null;
  let initialDraft = null;

  if (editingDraftName) {
    try {
      const slices = await SliceApi.listGraphSlices();
      initialDraft = slices.find((s) => s.slice_name === editingDraftName) || null;
    } catch (err) {
      showError(err);
      navigate("/slices");
      return;
    }
    if (!initialDraft || initialDraft.state !== "draft") {
      showToast("Solo se pueden editar slices guardados como borrador", "error");
      navigate("/slices");
      return;
    }
  }

  container.innerHTML = "";
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, initialDraft ? `Editar borrador: ${initialDraft.slice_name}` : "Nuevo slice"),
        h(
          "div",
          { class: "page-subtitle" },
          initialDraft
            ? "Edita la topología guardada. Los cambios no crean VMs hasta que pulses Desplegar."
            : "Diseña la topología y configura cada VM. Puedes guardarla como borrador o desplegarla."
        )
      )
    )
  );

  const editorRoot = h("div", { class: "topo-editor" });
  container.append(editorRoot);

  // ─── Canvas ──────────────────────────────────────────────────────────
  const canvasWrap = h("div", { class: "topo-canvas-wrap" });
  const importInput = h("input", {
    type: "file",
    accept: ".json,application/json",
    style: "display:none",
    onChange: async (event) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (file) await importTopologyFile(file);
    },
  });

  const toolbar = h(
    "div",
    { class: "topo-toolbar" },
    h("span", { class: "text-dim", style: "font-size:0.78rem;margin-right:6px" }, "Plantilla:"),
    ...TEMPLATES.map((t) =>
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm",
          onClick: () => promptTemplateCount(t),
        },
        t.label
      )
    ),
    h("span", { style: "flex:1" }),
    h(
      "button",
      { class: "btn btn-ghost btn-sm", onClick: () => addNodeAtRandom() },
      "+ Nodo"
    ),
    h(
      "button",
      { class: "btn btn-ghost btn-sm", onClick: () => canvas.clear() },
      "Limpiar"
    ),
    h(
      "button",
      { class: "btn btn-ghost btn-sm", onClick: () => importInput.click() },
      "Importar JSON"
    )
  );
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "topo-svg");
  const svgScroll = h("div", { class: "topo-svg-scroll" });
  svgScroll.append(svg);
  const helpBar = h(
    "div",
    { class: "topo-help" },
    "Aplicar una plantilla con el canvas vacío lo inicializa. Aplicarla con nodos ya existentes AGREGA el subgrafo (para combinar topologías). Click en un nodo y luego en otro para conectar; doble click para editar; click en un enlace para borrarlo."
  );
  canvasWrap.append(toolbar, importInput, svgScroll, helpBar);
  editorRoot.append(canvasWrap);

  const canvas = new TopologyCanvas(svg, {
    onChange: () => refreshNodeList(),
    onNodeEdit: (node) => openNodeEditModal(node),
  });

  async function importTopologyFile(file) {
    try {
      const parsed = JSON.parse(await file.text());
      const topology = parsed?.topology || parsed;
      if (!topology || !Array.isArray(topology.nodes) || !Array.isArray(topology.links)) {
        throw new Error("El JSON no contiene una topología válida con nodes y links");
      }
      const baseName = String(topology.slice_name || file.name.replace(/\.json$/i, "") || "topologia");
      const suggested = `${baseName}-import`;
      const newName = window.prompt("Nombre para el nuevo borrador importado:", suggested)?.trim();
      if (!newName) return;
      await SliceApi.importGraphSlice(topology, newName);
      showToast(`Topología importada como borrador "${newName}"`, "success");
      navigate(`/slices/${encodeURIComponent(newName)}/edit`);
    } catch (err) {
      showError(err);
    }
  }

  function promptTemplateCount(template) {
    openModal({
      title: `Plantilla: ${template.label}`,
      renderContent: (body, close) => {
        body.append(
          h(
            "div",
            { class: "field" },
            h("label", {}, "Número de VMs"),
            h("input", { type: "number", id: "tpl-count", value: "4", min: "2", max: "12" })
          ),
          h(
            "div",
            { class: "text-faint", style: "font-size:0.75rem;margin-top:-6px;margin-bottom:8px" },
            canvas.nodes.length === 0
              ? "El canvas está vacío: esta plantilla lo inicializará."
              : `El canvas tiene ${canvas.nodes.length} nodos: la nueva plantilla se AGREGARÁ debajo, con nombres auto-renombrados para no colisionar.`
          ),
          h(
            "div",
            { class: "modal-actions" },
            h("button", { class: "btn btn-ghost", onClick: () => close(null) }, "Cancelar"),
            h(
              "button",
              {
                class: "btn btn-primary",
                onClick: () => {
                  const count = parseInt(document.getElementById("tpl-count").value, 10) || 4;
                  close(count);
                },
              },
              "Generar"
            )
          )
        );
      },
    }).then((count) => {
      if (!count) return;
      // Aditivo: si el canvas está vacío hace lo mismo que loadTemplate;
      // si tiene contenido lo agrega como subgrafo separado.
      canvas.appendTemplate(template.key, count);
    });
  }

  function addNodeAtRandom() {
    const x = 100 + Math.random() * 600;
    const y = 80 + Math.random() * 320;
    canvas.addNode(x, y);
  }

  // ─── Sidebar ─────────────────────────────────────────────────────────
  const sidebar = h("div", { class: "topo-sidebar" });
  editorRoot.append(sidebar);

  const propsCard = h("div", { class: "card" });
  sidebar.append(propsCard);

  const nodeListCard = h(
    "div",
    { class: "card" },
    h("h3", {}, "Nodos"),
    h("div", { id: "node-list" })
  );
  sidebar.append(nodeListCard);

  function refreshNodeList() {
    const listEl = nodeListCard.querySelector("#node-list");
    listEl.innerHTML = "";
    if (canvas.nodes.length === 0) {
      listEl.append(h("p", { class: "text-faint", style: "font-size:0.78rem" }, "Sin nodos aún."));
      return;
    }
    for (const node of canvas.nodes) {
      listEl.append(
        h(
          "div",
          { class: "node-list-item" },
          h("span", { class: "mono" }, node.name),
          h(
            "div",
            { class: "flex gap-sm" },
            h(
              "button",
              { class: "btn btn-ghost btn-sm btn-icon", onClick: () => openNodeEditModal(node) },
              "✎"
            ),
            h(
              "button",
              { class: "btn btn-ghost btn-sm btn-icon", onClick: () => canvas.removeNode(node.name) },
              "×"
            )
          )
        )
      );
    }
  }

  async function openNodeEditModal(node) {
    // El cluster seleccionado afecta qué imágenes están disponibles
    const clusterEl = document.getElementById("cluster-select");
    const currentCluster = clusterEl ? clusterEl.value : "linux";
    const availableImages = imagesForCluster(currentCluster);

    // Si la imagen actual del nodo no está en el catálogo del cluster,
    // forzar la primera del catálogo (p.ej. al cambiar a OpenStack y la
    // VM tenía cirros-base.img que solo existe en Linux).
    let initialImage = node.image_name;
    if (!availableImages.some((img) => img.value === initialImage)) {
      initialImage = availableImages[0].value;
    }

    await openModal({
      title: `Configurar ${node.name}`,
      renderContent: (body, close) => {
        // ─── Estado local del modal ──────────────────────────────
        // Se mantiene en variables locales para que los sliders y el
        // selector de imagen interactúen entre sí (validación de RAM/disco
        // mínimos según la imagen elegida).
        let selectedImage = initialImage;
        let selectedRamIdx = ramOptionIndex(node.ram_mb);
        let selectedDiskIdx = diskOptionIndex(node.disk_gb);
        let selectedVcpuIdx = vcpuOptionIndex(node.vcpus);

        // ─── Helpers para refrescar los labels de los sliders ────
        function updateRamLabel() {
          document.getElementById("node-ram-label").textContent =
            RAM_OPTIONS[selectedRamIdx].label;
        }
        function updateDiskLabel() {
          document.getElementById("node-disk-label").textContent =
            DISK_OPTIONS[selectedDiskIdx].label;
        }
        function updateVcpuLabel() {
          document.getElementById("node-vcpu-label").textContent =
            VCPU_OPTIONS[selectedVcpuIdx] + " vCPU" +
            (VCPU_OPTIONS[selectedVcpuIdx] > 1 ? "s" : "");
        }

        // ─── Helper para mostrar warning de RAM/disco insuficiente
        function refreshImageWarning() {
          const img = availableImages.find((i) => i.value === selectedImage);
          if (!img) return;
          const ramMb = RAM_OPTIONS[selectedRamIdx].mb;
          const diskGb = DISK_OPTIONS[selectedDiskIdx].gb;
          const warnEl = document.getElementById("node-image-warn");
          const warnings = [];
          if (ramMb < img.min_ram) {
            warnings.push(`RAM mínima recomendada: ${img.min_ram} MB`);
          }
          if (diskGb < img.min_disk) {
            warnings.push(`Disco mínimo recomendado: ${img.min_disk} GB`);
          }
          if (warnings.length > 0) {
            warnEl.textContent = "⚠ " + warnings.join(" · ");
            warnEl.style.display = "block";
          } else {
            warnEl.style.display = "none";
          }
        }

        // ─── Construir el formulario ─────────────────────────────
        body.append(
          // Nombre
          h(
            "div",
            { class: "field" },
            h("label", {}, "Nombre"),
            h("input", { type: "text", id: "node-name", value: node.name })
          ),

          // Imagen — dropdown filtrado por cluster
          h(
            "div",
            { class: "field" },
            h("label", { for: "node-image" }, `Imagen (${currentCluster})`),
            h(
              "select",
              {
                id: "node-image",
                onChange: (e) => {
                  selectedImage = e.target.value;
                  refreshImageWarning();
                },
              },
              ...availableImages.map((img) =>
                h(
                  "option",
                  {
                    value: img.value,
                    selected: img.value === initialImage ? "selected" : null,
                  },
                  img.label
                )
              )
            ),
            h(
              "div",
              {
                id: "node-image-warn",
                style:
                  "display:none;color:#f59e0b;font-size:0.72rem;margin-top:4px",
              }
            )
          ),

          // vCPUs — slider
          h(
            "div",
            { class: "field" },
            h(
              "label",
              { style: "display:flex;justify-content:space-between;align-items:center" },
              h("span", {}, "vCPUs"),
              h(
                "span",
                {
                  id: "node-vcpu-label",
                  class: "mono",
                  style: "color:var(--accent);font-size:0.85rem",
                },
                VCPU_OPTIONS[selectedVcpuIdx] + " vCPU" + (VCPU_OPTIONS[selectedVcpuIdx] > 1 ? "s" : "")
              )
            ),
            h("input", {
              type: "range",
              id: "node-vcpus",
              min: "0",
              max: String(VCPU_OPTIONS.length - 1),
              step: "1",
              value: String(selectedVcpuIdx),
              style: "width:100%;accent-color:var(--accent)",
              onInput: (e) => {
                selectedVcpuIdx = parseInt(e.target.value, 10);
                updateVcpuLabel();
              },
            }),
            h(
              "div",
              {
                style:
                  "display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-faint);margin-top:2px",
              },
              ...VCPU_OPTIONS.map((v) => h("span", {}, String(v)))
            )
          ),

          // RAM — slider
          h(
            "div",
            { class: "field" },
            h(
              "label",
              { style: "display:flex;justify-content:space-between;align-items:center" },
              h("span", {}, "RAM"),
              h(
                "span",
                {
                  id: "node-ram-label",
                  class: "mono",
                  style: "color:var(--accent);font-size:0.85rem",
                },
                RAM_OPTIONS[selectedRamIdx].label
              )
            ),
            h("input", {
              type: "range",
              id: "node-ram",
              min: "0",
              max: String(RAM_OPTIONS.length - 1),
              step: "1",
              value: String(selectedRamIdx),
              style: "width:100%;accent-color:var(--accent)",
              onInput: (e) => {
                selectedRamIdx = parseInt(e.target.value, 10);
                updateRamLabel();
                refreshImageWarning();
              },
            }),
            h(
              "div",
              {
                style:
                  "display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-faint);margin-top:2px",
              },
              ...RAM_OPTIONS.map((o) => h("span", {}, o.label.replace(" ", "")))
            )
          ),

          // Disco — slider
          h(
            "div",
            { class: "field" },
            h(
              "label",
              { style: "display:flex;justify-content:space-between;align-items:center" },
              h("span", {}, "Disco"),
              h(
                "span",
                {
                  id: "node-disk-label",
                  class: "mono",
                  style: "color:var(--accent);font-size:0.85rem",
                },
                DISK_OPTIONS[selectedDiskIdx].label
              )
            ),
            h("input", {
              type: "range",
              id: "node-disk",
              min: "0",
              max: String(DISK_OPTIONS.length - 1),
              step: "1",
              value: String(selectedDiskIdx),
              style: "width:100%;accent-color:var(--accent)",
              onInput: (e) => {
                selectedDiskIdx = parseInt(e.target.value, 10);
                updateDiskLabel();
                refreshImageWarning();
              },
            }),
            h(
              "div",
              {
                style:
                  "display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-faint);margin-top:2px",
              },
              ...DISK_OPTIONS.map((o) => h("span", {}, o.label.replace(" ", "")))
            )
          ),

          // Internet
          h(
            "div",
            { class: "checkbox-field field" },
            h("input", {
              type: "checkbox",
              id: "node-internet",
              checked: node.internet || null,
            }),
            h(
              "label",
              { for: "node-internet", style: "margin:0" },
              "Salida/acceso a Internet"
            )
          ),

          // Botones
          h(
            "div",
            { class: "modal-actions" },
            h(
              "button",
              { class: "btn btn-ghost", onClick: () => close(null) },
              "Cancelar"
            ),
            h(
              "button",
              {
                class: "btn btn-primary",
                onClick: () => {
                  const newName = document.getElementById("node-name").value.trim();
                  if (newName && newName !== node.name) {
                    const ok = canvas.renameNode(node.name, newName);
                    if (!ok) {
                      showToast("Ya existe un nodo con ese nombre", "error");
                      return;
                    }
                  }
                  canvas.updateNode(newName || node.name, {
                    vcpus: VCPU_OPTIONS[selectedVcpuIdx],
                    ram_mb: RAM_OPTIONS[selectedRamIdx].mb,
                    disk_gb: DISK_OPTIONS[selectedDiskIdx].gb,
                    image_name: selectedImage,
                    internet: document.getElementById("node-internet").checked,
                  });
                  close(true);
                },
              },
              "Guardar"
            )
          )
        );

        // Evaluar warning inicial (por si la imagen viene con RAM/disco bajos)
        setTimeout(refreshImageWarning, 0);
      },
    });
  }

  if (initialDraft) {
    canvas.loadFromGraph(initialDraft.nodes || [], initialDraft.links || []);
  } else {
    // Arrancamos con una plantilla lineal de 3 nodos para no dejar el canvas vacío.
    canvas.loadTemplate("linear", 3);
  }

  // ─── Panel de propiedades del slice ─────────────────────────────────
  await renderPropsForm(propsCard, canvas, user, role, initialDraft);
}

async function renderPropsForm(propsCard, canvas, user, role, initialDraft = null) {
  propsCard.innerHTML = "";
  propsCard.append(h("h3", {}, "Configuración del slice"));

  const form = h("form", {});

  const nameField = fieldInput(
    "slice-name",
    "Nombre del slice",
    "text",
    initialDraft?.slice_name || `slice-${Date.now()}`
  );
  if (initialDraft) {
    nameField.input.readOnly = true;
    nameField.input.title = "Para crear otro nombre usa Clonar";
  }
  const clusterField = fieldSelect("cluster-select", "Cluster", [
    { value: "linux", label: "Linux (KVM)" },
    { value: "openstack", label: "OpenStack" },
  ]);

  form.append(nameField.wrapper, clusterField.wrapper);
  if (initialDraft?.cluster) clusterField.input.value = initialDraft.cluster;

  // Cuando cambia el cluster, normalizar imágenes incompatibles de nodos
  // existentes. P.ej. al pasar de Linux a OpenStack, "cirros-base.img" se
  // remplaza por "cirros" (la única disponible en OpenStack actualmente).
  clusterField.input.addEventListener("change", () => {
    const newCluster = clusterField.input.value;
    const valid = imagesForCluster(newCluster).map((i) => i.value);
    const fallback = imagesForCluster(newCluster)[0].value;
    let changed = 0;
    for (const node of canvas.nodes) {
      if (!valid.includes(node.image_name)) {
        canvas.updateNode(node.name, { image_name: fallback });
        changed++;
      }
    }
    if (changed > 0) {
      showToast(
        `Se ajustaron ${changed} imagen(es) al catálogo de ${newCluster}`,
        "info"
      );
    }
  });

  // ─── On-behalf-of: solo admin/profesor ──────────────────────────────
  // - admin: selector con todos los alumnos (desde /students-listable)
  //   + selector opcional de curso.
  // - profesor: PRIMERO selector de curso, DESPUÉS selector de alumno
  //   filtrado a inscritos del curso elegido.
  let ownerSelect = null;
  let courseSelect = null;

  if (canActOnBehalf()) {
    const onBehalfBlock = h(
      "div",
      { class: "card", style: "background:var(--bg);margin-top:10px;margin-bottom:14px;padding:12px" },
      h(
        "div",
        { style: "font-size:0.76rem;color:var(--text-dim);margin-bottom:8px" },
        role === "profesor"
          ? "Asignar slice a un alumno (selecciona primero el curso)"
          : "Crear a nombre de otro usuario (opcional)"
      )
    );

    // Cursos visibles para el usuario (admin: todos; profesor: los suyos)
    let courses = [];
    try {
      courses = await AuthApi.listCourses();
    } catch {
      courses = [];
    }

    if (role === "admin") {
      // Admin ve TODOS los alumnos; el curso es opcional.
      let alumnos = [];
      try {
        alumnos = await AuthApi.listStudents();
      } catch {
        alumnos = [];
      }
      const ownerOpts = [
        { value: "", label: "— Yo mismo —" },
        ...alumnos.map((a) => ({ value: a.username, label: a.username })),
      ];
      const sel = fieldSelect("owner-select", "Alumno destinatario", ownerOpts);
      ownerSelect = sel.input;
      onBehalfBlock.append(sel.wrapper);

      if (courses.length > 0) {
        const courseOpts = [
          { value: "", label: "— Sin curso —" },
          ...courses.map((c) => ({ value: String(c.id), label: `${c.codigo} — ${c.nombre}` })),
        ];
        const courseSel = fieldSelect("course-select", "Curso (opcional)", courseOpts);
        courseSelect = courseSel.input;
        onBehalfBlock.append(courseSel.wrapper);
      }
    } else if (role === "profesor") {
      // Profesor: curso primero, alumno filtrado por curso.
      if (courses.length === 0) {
        onBehalfBlock.append(
          h(
            "p",
            { class: "text-faint", style: "font-size:0.78rem" },
            "No dictas ningún curso aún. Solo podrás crear slices para ti mismo."
          )
        );
      } else {
        const courseOpts = [
          { value: "", label: "— Para mí mismo —" },
          ...courses.map((c) => ({ value: String(c.id), label: `${c.codigo} — ${c.nombre}` })),
        ];
        const courseSel = fieldSelect("course-select", "Curso", courseOpts);
        courseSelect = courseSel.input;
        onBehalfBlock.append(courseSel.wrapper);

        // Selector de alumno (vacío hasta que se elija curso)
        const ownerSel = fieldSelect("owner-select", "Alumno destinatario", [
          { value: "", label: "— Para mí mismo —" },
        ]);
        ownerSelect = ownerSel.input;
        ownerSelect.disabled = true;
        onBehalfBlock.append(ownerSel.wrapper);

        // Al cambiar curso, repuebla el selector de alumno con los alumnos
        // inscritos en ese curso (vienen ya en course.alumnos).
        courseSelect.addEventListener("change", () => {
          const cid = courseSelect.value;
          ownerSelect.innerHTML = "";
          if (!cid) {
            ownerSelect.disabled = true;
            ownerSelect.append(new Option("— Para mí mismo —", ""));
            return;
          }
          const course = courses.find((c) => String(c.id) === cid);
          const alumnos = course?.alumnos || [];
          ownerSelect.disabled = false;
          ownerSelect.append(new Option("— Para mí mismo —", ""));
          for (const a of alumnos) {
            ownerSelect.append(new Option(a, a));
          }
          if (alumnos.length === 0) {
            ownerSelect.append(new Option("(curso sin alumnos inscritos)", ""));
          }
        });
      }
    }

    form.append(onBehalfBlock);
  }

  const saveBtn = h(
    "button",
    { type: "button", class: "btn btn-ghost w-full" },
    initialDraft ? "Guardar cambios" : "Guardar borrador"
  );
  const submitBtn = h(
    "button",
    { type: "submit", class: "btn btn-primary w-full" },
    initialDraft ? "Guardar y desplegar" : "Crear y desplegar"
  );
  form.append(
    h(
      "div",
      { class: "flex gap-sm mt-md", style: "flex-direction:column" },
      saveBtn,
      submitBtn
    )
  );

  saveBtn.addEventListener("click", async () => {
    await handleSaveDraft({
      canvas,
      nameInput: nameField.input,
      clusterInput: clusterField.input,
      ownerSelect,
      courseSelect,
      saveBtn,
      editingDraftName: initialDraft?.slice_name || null,
    });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await handleSubmit({
      canvas,
      nameInput: nameField.input,
      clusterInput: clusterField.input,
      ownerSelect,
      courseSelect,
      submitBtn,
      editingDraftName: initialDraft?.slice_name || null,
    });
  });

  propsCard.append(form);
}

function fieldInput(id, label, type, defaultValue) {
  const input = h("input", { type, id, value: defaultValue });
  const wrapper = h("div", { class: "field" }, h("label", { for: id }, label), input);
  return { wrapper, input };
}

function fieldSelect(id, label, options) {
  const input = h(
    "select",
    { id },
    ...options.map((o) => h("option", { value: o.value }, o.label))
  );
  const wrapper = h("div", { class: "field" }, h("label", { for: id }, label), input);
  return { wrapper, input };
}

function buildPayload({ canvas, nameInput, clusterInput, ownerSelect, courseSelect }) {
  const sliceName = nameInput.value.trim();
  const cluster = clusterInput.value;

  if (!sliceName) {
    showToast("El nombre del slice es requerido", "error");
    return null;
  }
  if (canvas.nodes.length < 2) {
    showToast("La topología debe tener al menos 2 nodos", "error");
    return null;
  }
  if (canvas.links.length < 1) {
    showToast("La topología debe tener al menos 1 enlace", "error");
    return null;
  }

  const nodes = canvas.toPayloadNodes();
  const nodeNames = new Set(nodes.map((n) => n.name));
  const rawLinks = canvas.toPayloadLinks();
  const links = rawLinks.filter(
    (link) => nodeNames.has(link.from) && nodeNames.has(link.to)
  );
  if (links.length < rawLinks.length) {
    showToast(
      `Se ignoraron ${rawLinks.length - links.length} enlace(s) huérfanos.`,
      "info"
    );
  }

  const hasInternet = nodes.some((node) => node.internet);
  const payload = {
    slice_name: sliceName,
    cluster,
    nodes,
    links,
    network_backend: "vlan",
    internet_mode: hasInternet ? "headnode_nat" : "none",
  };

  if (ownerSelect?.value) payload.owner_username = ownerSelect.value;
  if (courseSelect?.value) payload.curso_id = parseInt(courseSelect.value, 10);
  return payload;
}

async function handleSaveDraft({
  canvas,
  nameInput,
  clusterInput,
  ownerSelect,
  courseSelect,
  saveBtn,
  editingDraftName,
}) {
  const payload = buildPayload({
    canvas,
    nameInput,
    clusterInput,
    ownerSelect,
    courseSelect,
  });
  if (!payload) return;

  saveBtn.disabled = true;
  saveBtn.textContent = "Guardando…";
  try {
    if (editingDraftName) {
      await SliceApi.updateDraft(editingDraftName, payload);
    } else {
      await SliceApi.createDraft(payload);
    }
    showToast(`Borrador "${payload.slice_name}" guardado`, "success");
    navigate(`/slices/${encodeURIComponent(payload.slice_name)}`);
  } catch (err) {
    showError(err);
    saveBtn.disabled = false;
    saveBtn.textContent = editingDraftName ? "Guardar cambios" : "Guardar borrador";
  }
}

async function handleSubmit({
  canvas,
  nameInput,
  clusterInput,
  ownerSelect,
  courseSelect,
  submitBtn,
  editingDraftName,
}) {
  const payload = buildPayload({
    canvas,
    nameInput,
    clusterInput,
    ownerSelect,
    courseSelect,
  });
  if (!payload) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "Encolando…";
  try {
    if (editingDraftName) {
      await SliceApi.updateDraft(editingDraftName, payload);
      await SliceApi.deployDraft(editingDraftName);
    } else {
      await SliceApi.createGraphSlice(payload);
    }
    showToast(`Slice "${payload.slice_name}" encolado, desplegando…`, "info");
    navigate(`/slices/${encodeURIComponent(payload.slice_name)}`);
  } catch (err) {
    showError(err);
    submitBtn.disabled = false;
    submitBtn.textContent = editingDraftName
      ? "Guardar y desplegar"
      : "Crear y desplegar";
  }
}
