// Add to List modal: opens on .add-to-list-btn click, disables submit until
// at least one select has a non-empty value, then POSTs one request per
// selection to /items/{id}/list and reloads.

(function () {
  const dialog = document.getElementById("add-to-list-modal");
  if (!dialog) return;

  const librarySelect = document.getElementById("add-to-list-library");
  const projectSelect = document.getElementById("add-to-list-project");
  const submitBtn = document.getElementById("add-to-list-submit");
  const cancelBtn = document.getElementById("add-to-list-cancel");
  const form = document.getElementById("add-to-list-form");

  function refreshSubmitState() {
    const hasSelection = !!librarySelect.value || !!projectSelect.value;
    submitBtn.disabled = !hasSelection;
  }

  librarySelect.addEventListener("change", refreshSubmitState);
  projectSelect.addEventListener("change", refreshSubmitState);

  cancelBtn.addEventListener("click", () => dialog.close());

  document.addEventListener("click", (event) => {
    const btn = event.target.closest(".add-to-list-btn");
    if (!btn) return;
    event.preventDefault();

    dialog.dataset.itemId = btn.dataset.itemId || "";
    const currentLib = btn.dataset.currentLibraryId || "";
    librarySelect.value = currentLib;
    projectSelect.value = "";
    refreshSubmitState();
    dialog.showModal();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const itemId = dialog.dataset.itemId;
    if (!itemId) return;

    const selections = [librarySelect.value, projectSelect.value].filter(Boolean);
    if (selections.length === 0) return;

    submitBtn.disabled = true;
    try {
      for (const listId of selections) {
        const body = new URLSearchParams({ list_id: listId });
        const res = await fetch(`/items/${itemId}/list`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        if (!res.ok && res.status !== 303) {
          throw new Error(`Request failed: ${res.status}`);
        }
      }
      window.location.reload();
    } catch (err) {
      alert("Failed to add to list: " + err.message);
      refreshSubmitState();
    }
  });
})();
