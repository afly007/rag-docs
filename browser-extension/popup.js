document.addEventListener("DOMContentLoaded", async () => {
  const urlDisplay = document.getElementById("url-display");
  const vendorInput = document.getElementById("vendor");
  const productInput = document.getElementById("product");
  const saveBtn = document.getElementById("save-btn");
  const statusEl = document.getElementById("status");
  const settingsLink = document.getElementById("settings-link");

  // Get current tab URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || "";
  urlDisplay.textContent = url;

  // Load saved settings
  const { serverUrl, apiKey, lastVendor, lastProduct } =
    await chrome.storage.sync.get(["serverUrl", "apiKey", "lastVendor", "lastProduct"]);

  // Pre-fill last-used tags
  if (lastVendor) vendorInput.value = lastVendor;
  if (lastProduct) productInput.value = lastProduct;

  if (!serverUrl || !apiKey) {
    showStatus("error", "Not configured — click ⚙ Settings to add your server URL and API key.");
    saveBtn.disabled = true;
  }

  saveBtn.addEventListener("click", async () => {
    const vendor = vendorInput.value.trim().toLowerCase();
    const product = productInput.value.trim().toLowerCase();

    saveBtn.disabled = true;
    showStatus("loading", "Fetching and indexing…");

    const body = { url };
    if (vendor) body.vendor = vendor;
    if (product) body.product = product;

    try {
      const resp = await fetch(`${serverUrl}/clip`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(body),
      });

      const data = await resp.json();

      if (data.skipped) {
        showStatus("skipped", "Already in your docs — nothing to do.");
      } else if (!resp.ok) {
        showStatus("error", data.error || `Server error ${resp.status}`);
      } else {
        showStatus("success", `Saved! ${data.chunks} chunk${data.chunks !== 1 ? "s" : ""} indexed.`);
        // Remember tags for next time
        await chrome.storage.sync.set({ lastVendor: vendor, lastProduct: product });
      }
    } catch (err) {
      showStatus("error", `Could not reach server: ${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  });

  settingsLink.addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });
});

function showStatus(type, message) {
  const el = document.getElementById("status");
  el.className = `status ${type}`;
  el.textContent = message;
}
