/**
 * pages/images.js
 *
 * Gestión de imágenes del cluster — listar, importar por URL, subir archivo
 * local y borrar. Solo admin (las imágenes son un recurso compartido del lab).
 *
 * Backend: /api/images (GET, DELETE), /api/images/import-url,
 *          /api/images/upload  →  image_service vía gateway.
 */

import { ImageApi } from "../lib/api.js";
import { h, openModal, showError, showToast, confirmDialog } from "../lib/components.js";
import { isAdmin } from "../lib/auth.js";

function fmtSize(bytes) {
  if (!bytes) return "—";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(0)} MB`;
}

export async function renderImages(container) {
  if (!isAdmin()) {
    container.innerHTML = "";
    container.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Acceso restringido"),
        h("p", {}, "La gestión de imágenes es solo para administradores.")
      )
    );
    return;
  }

  container.innerHTML = "";
  const header = h(
    "div",
    { class: "page-header" },
    h("h1", {}, "Imágenes"),
    h(
      "div",
      { class: "page-actions" },
      h(
        "button",
        { class: "btn btn-secondary", onclick: () => openUploadModal(container) },
        "Subir archivo"
      ),
      h(
        "button",
        { class: "btn btn-primary", onclick: () => openImportUrlModal(container) },
        "+ Importar por URL"
      )
    )
  );
  container.append(header);

  const listWrap = h("div", { class: "card" }, h("p", {}, "Cargando imágenes…"));
  container.append(listWrap);

  try {
    const images = await ImageApi.list();
    renderTable(listWrap, images, container);
  } catch (err) {
    listWrap.innerHTML = "";
    listWrap.append(h("p", { class: "text-error" }, "No se pudieron cargar las imágenes."));
    showError(err);
  }
}

function renderTable(wrap, images, container) {
  wrap.innerHTML = "";
  if (!images || images.length === 0) {
    wrap.append(
      h(
        "div",
        { class: "empty-state" },
        h("p", {}, "No hay imágenes registradas. Importa una por URL para empezar.")
      )
    );
    return;
  }

  const rows = images.map((img) =>
    h(
      "tr",
      {},
      h("td", {}, img.name),
      h("td", {}, h("span", { class: "badge" }, img.os_type || "—")),
      h("td", {}, img.format || "—"),
      h("td", {}, fmtSize(img.size_bytes)),
      h("td", {}, img.stored_filename || img.filename || "—"),
      h(
        "td",
        {},
        h(
          "button",
          {
            class: "btn btn-danger btn-sm",
            onclick: async () => {
              const ok = await confirmDialog({
                title: "Borrar imagen",
                message: `¿Borrar la imagen "${img.name}"? Los slices que la usen ya desplegados no se ven afectados, pero no podrás crear nuevos con ella.`,
                confirmLabel: "Borrar",
              });
              if (!ok) return;
              try {
                await ImageApi.remove(img.name);
                showToast(`Imagen ${img.name} borrada`, "success");
                renderImages(container);
              } catch (err) {
                showError(err);
              }
            },
          },
          "Borrar"
        )
      )
    )
  );

  const table = h(
    "table",
    { class: "data-table" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "Nombre"),
        h("th", {}, "Tipo"),
        h("th", {}, "Formato"),
        h("th", {}, "Tamaño"),
        h("th", {}, "Archivo"),
        h("th", {}, "")
      )
    ),
    h("tbody", {}, ...rows)
  );
  wrap.append(table);
}

function openImportUrlModal(container) {
  openModal({
    title: "Importar imagen por URL",
    renderContent: (body, close) => {
      const nameInput = h("input", { class: "input", placeholder: "ubuntu-22.04" });
      const urlInput = h("input", {
        class: "input",
        placeholder: "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
      });
      const osSelect = h(
        "select",
        { class: "input" },
        h("option", { value: "ubuntu" }, "ubuntu"),
        h("option", { value: "linux" }, "linux"),
        h("option", { value: "cirros" }, "cirros")
      );
      const fmtSelect = h(
        "select",
        { class: "input" },
        h("option", { value: "qcow2" }, "qcow2"),
        h("option", { value: "raw" }, "raw"),
        h("option", { value: "img" }, "img")
      );
      const status = h("p", { class: "text-muted" }, "");
      const submitBtn = h("button", { class: "btn btn-primary" }, "Importar");

      submitBtn.onclick = async () => {
        const name = nameInput.value.trim();
        const url = urlInput.value.trim();
        if (!name || !url) {
          showToast("Completa nombre y URL", "error");
          return;
        }
        submitBtn.disabled = true;
        status.textContent = "Descargando en el servidor… puede tardar 1–2 min para imágenes grandes.";
        try {
          await ImageApi.importUrl({
            name,
            url,
            os_type: osSelect.value,
            format: fmtSelect.value,
          });
          showToast(`Imagen ${name} importada`, "success");
          close(true);
          renderImages(container);
        } catch (err) {
          submitBtn.disabled = false;
          status.textContent = "";
          showError(err);
        }
      };

      body.append(
        h("label", { class: "form-label" }, "Nombre (así se elige al crear el slice)"),
        nameInput,
        h("label", { class: "form-label" }, "URL (http/https)"),
        urlInput,
        h("label", { class: "form-label" }, "Tipo de SO"),
        osSelect,
        h("label", { class: "form-label" }, "Formato"),
        fmtSelect,
        status,
        h("div", { class: "modal-footer" }, submitBtn)
      );
    },
  });
}

function openUploadModal(container) {
  openModal({
    title: "Subir imagen (archivo local)",
    renderContent: (body, close) => {
      const nameInput = h("input", { class: "input", placeholder: "ubuntu-22.04" });
      const fileInput = h("input", { class: "input", type: "file", accept: ".img,.qcow2,.raw" });
      const osSelect = h(
        "select",
        { class: "input" },
        h("option", { value: "ubuntu" }, "ubuntu"),
        h("option", { value: "linux" }, "linux"),
        h("option", { value: "cirros" }, "cirros")
      );
      const fmtSelect = h(
        "select",
        { class: "input" },
        h("option", { value: "qcow2" }, "qcow2"),
        h("option", { value: "raw" }, "raw"),
        h("option", { value: "img" }, "img")
      );
      const status = h("p", { class: "text-muted" }, "");
      const submitBtn = h("button", { class: "btn btn-primary" }, "Subir");

      submitBtn.onclick = async () => {
        const name = nameInput.value.trim();
        const file = fileInput.files && fileInput.files[0];
        if (!name || !file) {
          showToast("Completa nombre y elige un archivo", "error");
          return;
        }
        submitBtn.disabled = true;
        status.textContent = `Subiendo ${fmtSize(file.size)}… no cierres esta ventana.`;
        try {
          await ImageApi.upload({
            name,
            file,
            os_type: osSelect.value,
            format: fmtSelect.value,
          });
          showToast(`Imagen ${name} subida`, "success");
          close(true);
          renderImages(container);
        } catch (err) {
          submitBtn.disabled = false;
          status.textContent = "";
          showError(err);
        }
      };

      body.append(
        h("label", { class: "form-label" }, "Nombre (así se elige al crear el slice)"),
        nameInput,
        h("label", { class: "form-label" }, "Archivo (.img / .qcow2 / .raw)"),
        fileInput,
        h("label", { class: "form-label" }, "Tipo de SO"),
        osSelect,
        h("label", { class: "form-label" }, "Formato"),
        fmtSelect,
        status,
        h(
          "p",
          { class: "text-muted" },
          "Para imágenes grandes, 'Importar por URL' es más rápido (el servidor descarga directo)."
        ),
        h("div", { class: "modal-footer" }, submitBtn)
      );
    },
  });
}
