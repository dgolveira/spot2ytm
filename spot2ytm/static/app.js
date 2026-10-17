// Small enhancements on top of the server-rendered pages.

document.addEventListener("click", (event) => {
  const copyBtn = event.target.closest("[data-copy]");
  if (copyBtn) {
    const source = document.querySelector(copyBtn.dataset.copy);
    navigator.clipboard.writeText(source.textContent.trim()).then(() => {
      const label = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      setTimeout(() => (copyBtn.textContent = label), 1500);
    });
  }

  const confirmBtn = event.target.closest("[data-confirm]");
  if (confirmBtn && !window.confirm(confirmBtn.dataset.confirm)) {
    event.preventDefault();
  }
});

// Playlist picker: select all, filter, live count.
(function playlistPicker() {
  const form = document.getElementById("playlist-form");
  if (!form) return;
  const boxes = () => [...form.querySelectorAll('input[name="playlist_id"]:not(:disabled)')];
  const visibleBoxes = () => boxes().filter((b) => !b.closest("tr").classList.contains("hidden"));
  const selectAll = document.getElementById("select-all");
  const count = document.getElementById("selected-count");
  const button = document.getElementById("transfer-btn");

  function refresh() {
    const n = boxes().filter((b) => b.checked).length;
    count.textContent = n;
    button.disabled = n === 0;
    const noun = document.getElementById("selected-noun");
    if (noun) noun.textContent = n === 1 ? "playlist" : "playlists";
    const visible = visibleBoxes();
    selectAll.checked = visible.length > 0 && visible.every((b) => b.checked);
  }

  selectAll.addEventListener("change", () => {
    visibleBoxes().forEach((b) => (b.checked = selectAll.checked));
    refresh();
  });
  form.addEventListener("change", (e) => {
    if (e.target.name === "playlist_id") refresh();
  });
  document.getElementById("playlist-filter")?.addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    form.querySelectorAll("tbody tr").forEach((tr) => {
      tr.classList.toggle("hidden", q !== "" && !tr.dataset.name.includes(q));
    });
    refresh();
  });
  form.addEventListener("submit", () => {
    button.disabled = true;
    button.textContent = "Working…";
  });
  refresh();
})();

// Close other open pickers when one opens.
document.addEventListener("toggle", (event) => {
  if (event.target.matches?.("details.picker") && event.target.open) {
    document.querySelectorAll("details.picker[open]").forEach((d) => d !== event.target && (d.open = false));
  }
}, true);

// Tabs on the import page.
document.querySelectorAll("[data-tabs]").forEach((tabs) => {
  tabs.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]");
    if (!tab) return;
    tabs.querySelectorAll("[data-tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== tab.dataset.tab));
    history.replaceState(null, "", "?tab=" + tab.dataset.tab);
  });
});
