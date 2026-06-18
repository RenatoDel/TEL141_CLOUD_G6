/**
 * pages/slice-editor.js
 *
 * Página de creación de slices con canvas visual. Cubre:
 *   - Templates predefinidos (lineal, anillo, malla, árbol, bus) — R1B.
 *   - Edición libre de nodos/enlaces (agregar, mover, conectar, borrar).
 *   - Edición de configuración por VM (vcpus/ram/disk/imagen/internet).
 *   - Selección de cluster (linux/openstack) y zona de disponibilidad.
 *   - Para admin/profesor: asignar el slice a otro usuario ("on behalf of")
 *     y, si aplica, a uno de sus cursos.
 */

import { SliceApi, AuthApi } from "../lib/api.js";
import { TopologyCanvas } from "../lib/topology-canvas.js";
import { h, openModal, showError, showToast } from "../lib/components.js";
import { getUser, getRole, canActOnBehalf, canWrite } from "../lib/auth.js";
import { navigate } from "../lib/router.js";

const TEMPLATES = [
  { key: "linear", label: "Lineal" },
  { key: "ring", label: "Anillo" },
  { key: "mesh", label: "Malla" },
  { key: "tree", label: "Árbol" },
  { key: "bus", label: "Bus" },
];

export async function renderSliceEditor(container) {
  // Protección de ruta: roles read-only (coach) redirigen a /slices.
  if (!canWrite()) {
    navigate("/slices");
    return;
  }
  const user = getUser();
  const role = getRole();

  container.innerHTML = "";
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, "Nuevo slice"),
        h("div", { class: "page-subtitle" }, "Diseña la topología y configura cada VM")
      )
    )
  );

  const editorRoot = h("div", { class: "topo-editor" });
  container.append(editorRoot);

  // ─── Canvas ──────────────────────────────────────────────────────────
  const canvasWrap = h("div", { class: "topo-canvas-wrap" });
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
    )
  );
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "topo-svg");
  const helpBar = h(
    "div",
    { class: "topo-help" },
    "Click en un nodo y luego en otro para conectar. Doble click en un nodo para editar su configuración. Click en un enlace para borrarlo."
  );
  canvasWrap.append(toolbar, svg, helpBar);
  editorRoot.append(canvasWrap);

  const canvas = new TopologyCanvas(svg, {
    onChange: () => refreshNodeList(),
    onNodeEdit: (node) => openNodeEditModal(node),
  });

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
      if (count) canvas.loadTemplate(template.key, count);
    });
  }

  function addNodeAtRandom() {
    const x = 100 + Math.random() * 600;
    const y = 80 + Math.random() * 320;
    canvas.addNode(x, y);
  }

  // ─── Sidebar: propiedades del slice + lista de nodos ────────────────
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
    await openModal({
      title: `Configurar ${node.name}`,
      renderContent: (body, close) => {
        body.append(
          h(
            "div",
            { class: "field" },
            h("label", {}, "Nombre"),
            h("input", { type: "text", id: "node-name", value: node.name })
          ),
          h(
            "div",
            { class: "field-row" },
            h(
              "div",
              { class: "field" },
              h("label", {}, "vCPUs"),
              h("input", { type: "number", id: "node-vcpus", value: node.vcpus, min: "1", max: "8" })
            ),
            h(
              "div",
              { class: "field" },
              h("label", {}, "RAM (MB)"),
              h("input", { type: "number", id: "node-ram", value: node.ram_mb, min: "128", step: "128" })
            )
          ),
          h(
            "div",
            { class: "field" },
            h("label", {}, "Disco (GB)"),
            h("input", { type: "number", id: "node-disk", value: node.disk_gb, min: "2", max: "200" })
          ),
          h(
            "div",
            { class: "field" },
            h("label", {}, "Imagen"),
            h("input", { type: "text", id: "node-image", value: node.image_name })
          ),
          h(
            "div",
            { class: "checkbox-field field" },
            h("input", { type: "checkbox", id: "node-internet", checked: node.internet || null }),
            h("label", { for: "node-internet", style: "margin:0" }, "Salida/acceso a Internet")
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
                  const newName = document.getElementById("node-name").value.trim();
                  if (newName && newName !== node.name) {
                    const ok = canvas.renameNode(node.name, newName);
                    if (!ok) {
                      showToast("Ya existe un nodo con ese nombre", "error");
                      return;
                    }
                  }
                  canvas.updateNode(newName || node.name, {
                    vcpus: parseInt(document.getElementById("node-vcpus").value, 10) || 1,
                    ram_mb: parseInt(document.getElementById("node-ram").value, 10) || 256,
                    disk_gb: parseInt(document.getElementById("node-disk").value, 10) || 10,
                    image_name: document.getElementById("node-image").value.trim() || "cirros-base.img",
                    internet: document.getElementById("node-internet").checked,
                  });
                  close(true);
                },
              },
              "Guardar"
            )
          )
        );
      },
    });
  }

  // Arrancamos con una plantilla lineal de 3 nodos para no dejar el canvas vacío.
  canvas.loadTemplate("linear", 3);

  // ─── Panel de propiedades del slice ─────────────────────────────────
  await renderPropsForm(propsCard, canvas, user, role);
}

async function renderPropsForm(propsCard, canvas, user, role) {
  propsCard.innerHTML = "";
  propsCard.append(h("h3", {}, "Configuración del slice"));

  const form = h("form", {});

  const nameField = fieldInput("slice-name", "Nombre del slice", "text", `slice-${Date.now()}`);
  const clusterField = fieldSelect("cluster-select", "Cluster", [
    { value: "linux", label: "Linux (KVM)" },
    { value: "openstack", label: "OpenStack" },
  ]);

  form.append(nameField.wrapper, clusterField.wrapper);

  // ─── On-behalf-of: solo admin/profesor ──────────────────────────────
  let ownerSelect = null;
  let courseSelect = null;
  if (canActOnBehalf()) {
    const onBehalfBlock = h(
      "div",
      { class: "card", style: "background:var(--bg);margin-top:10px;margin-bottom:14px;padding:12px" },
      h("div", { style: "font-size:0.76rem;color:var(--text-dim);margin-bottom:8px" }, "Crear a nombre de otro usuario (opcional)")
    );

    if (role === "admin") {
      let users = [];
      try {
        users = await AuthApi.listUsers();
      } catch {
        users = [];
      }
      const alumnos = users.filter((u) => u.rol === "alumno");
      const opts = [{ value: "", label: "— Yo mismo —" }, ...alumnos.map((a) => ({ value: a.username, label: a.username }))];
      const sel = fieldSelect("owner-select", "Alumno destinatario", opts);
      ownerSelect = sel.input;
      onBehalfBlock.append(sel.wrapper);
    } else if (role === "profesor") {
      let courses = [];
      try {
        courses = await AuthApi.listCourses();
      } catch {
        courses = [];
      }
      const myCourse = courses[0]; // simplificación: primer curso que dicta
      const opts = [{ value: "", label: "— Yo mismo —" }, ...(myCourse?.alumnos || []).map((a) => ({ value: a, label: a }))];
      const sel = fieldSelect("owner-select", "Alumno destinatario (de tus cursos)", opts);
      ownerSelect = sel.input;
      onBehalfBlock.append(sel.wrapper);

      if (courses.length > 0) {
        const courseOpts = courses.map((c) => ({ value: String(c.id), label: `${c.codigo} — ${c.nombre}` }));
        const courseSel = fieldSelect("course-select", "Curso", courseOpts);
        courseSelect = courseSel.input;
        onBehalfBlock.append(courseSel.wrapper);
      }
    }

    form.append(onBehalfBlock);
  }

  const submitBtn = h("button", { type: "submit", class: "btn btn-primary w-full mt-md" }, "Crear slice");
  form.append(submitBtn);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await handleSubmit({
      canvas,
      nameInput: nameField.input,
      clusterInput: clusterField.input,
      ownerSelect,
      courseSelect,
      submitBtn,
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

async function handleSubmit({ canvas, nameInput, clusterInput, ownerSelect, courseSelect, submitBtn }) {
  const sliceName = nameInput.value.trim();
  const cluster = clusterInput.value;

  if (!sliceName) {
    showToast("El nombre del slice es requerido", "error");
    return;
  }
  if (canvas.nodes.length < 2) {
    showToast("La topología debe tener al menos 2 nodos", "error");
    return;
  }
  if (canvas.links.length < 1) {
    showToast("La topología debe tener al menos 1 enlace", "error");
    return;
  }

  const payload = {
    slice_name: sliceName,
    // vlan_base omitido → el backend asigna automáticamente la siguiente libre
    cluster,
    nodes: canvas.toPayloadNodes(),
    links: canvas.toPayloadLinks(),
  };

  if (ownerSelect && ownerSelect.value) {
    payload.owner_username = ownerSelect.value;
  }
  if (courseSelect && courseSelect.value) {
    payload.curso_id = parseInt(courseSelect.value, 10);
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "Creando…";

  try {
    await SliceApi.createGraphSlice(payload);
    showToast(`Slice "${sliceName}" creado correctamente`, "success");
    navigate(`/slices/${encodeURIComponent(sliceName)}`);
  } catch (err) {
    showError(err);
    submitBtn.disabled = false;
    submitBtn.textContent = "Crear slice";
  }
}
