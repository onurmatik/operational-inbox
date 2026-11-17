/* Session-only transfer controls. All remote upload URLs come from our own CSRF-protected view. */
document.addEventListener("DOMContentLoaded", function initMailTransfers() {
  const root = document.querySelector("[data-mail-transfers]");
  if (!root) return;
  const form = root.querySelector("[data-import-form]");
  const results = root.querySelector("[data-transfer-results]");
  const pollStatus = root.querySelector("[data-poll-status]");
  let uploading = false;
  let active = root.dataset.busy === "true";
  let timer;

  function controls(busy) {
    root.querySelectorAll("[data-transfer-control]").forEach((node) => { node.disabled = busy; });
  }
  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", body,
      headers: { "X-CSRFToken": form.elements.csrfmiddlewaretoken.value, "Accept": "application/json" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The request could not finish.");
    return data;
  }
  async function poll() {
    if (uploading || document.hidden) { timer = setTimeout(poll, 5000); return; }
    try {
      const response = await fetch(root.dataset.statusUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!response.ok) throw new Error();
      const data = await response.json();
      const open = [...results.querySelectorAll("details[open]")].map((node) => node.id);
      if (!data.active || !results.contains(document.activeElement)) {
        results.innerHTML = data.html;
        open.forEach((id) => { const detail = document.getElementById(id); if (detail) detail.open = true; });
      }
      active = data.active;
      controls(active);
      pollStatus.textContent = "";
    } catch (_) {
      pollStatus.textContent = "Status unavailable. Retrying…";
    }
    if (active) timer = setTimeout(poll, 5000);
  }
  if (active) timer = setTimeout(poll, 3000);
  if (!form) return;
  const feedback = form.querySelector("[data-upload-status]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (uploading || active) return;
    const file = form.elements.file.files[0];
    if (!file || !file.size || file.size > Number(root.dataset.maxBytes)) {
      feedback.textContent = "Choose an MBOX file of up to 250 MB.";
      return;
    }
    uploading = true;
    controls(true);
    clearTimeout(timer);
    let transfer;
    let uploaded = false;
    try {
      const body = new FormData();
      body.append("filename", file.name);
      body.append("size", String(file.size));
      feedback.textContent = "Preparing upload…";
      transfer = await post(form.action, body);
      const upload = new FormData();
      Object.entries(transfer.upload.fields).forEach(([key, value]) => upload.append(key, value));
      upload.append("file", file);
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", transfer.upload.url);
        xhr.timeout = 30 * 60 * 1000;
        xhr.upload.onprogress = (progress) => {
          feedback.textContent = progress.lengthComputable ? `Uploading · ${Math.round(progress.loaded / progress.total * 100)}%` : "Uploading…";
        };
        xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error("Upload failed. Try again."));
        xhr.onerror = xhr.ontimeout = () => reject(new Error("Upload interrupted. Try again."));
        xhr.send(upload);
      });
      uploaded = true;
      feedback.textContent = "Starting import…";
      await post(transfer.complete_url, new FormData());
      window.location.assign(transfer.page_url);
    } catch (error) {
      if (transfer && !uploaded) {
        try { await post(transfer.cancel_url, new FormData()); } catch (_) { /* Poll resolves server state. */ }
      }
      feedback.textContent = uploaded ? "File uploaded. Use Finish import below to retry." : error.message;
      uploading = false;
      active = true;
      await poll();
    }
  });
});
