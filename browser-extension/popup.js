document.addEventListener("DOMContentLoaded", async () => {
  const urlDisplay = document.getElementById("url-display");
  const vendorInput = document.getElementById("vendor");
  const productInput = document.getElementById("product");
  const saveBtn = document.getElementById("save-btn");
  const statusEl = document.getElementById("status");
  const settingsLink = document.getElementById("settings-link");

  // Get current tab URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let url = tab?.url || "";

  // Reddit serves JS-rendered HTML — rewrite to old.reddit.com for plain HTML
  const redditRewrite = url.match(/^(https?:\/\/)(www\.|new\.|sh\.)?reddit\.com(\/.*)?$/);
  if (redditRewrite) {
    url = `https://old.reddit.com${redditRewrite[3] || "/"}`;
    showStatus("info", "Redirected to old.reddit.com for better text extraction.");
  }

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
  } else {
    // Populate vendor/product dropdowns from server
    try {
      const metaResp = await fetch(`${serverUrl}/clip/meta`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      if (metaResp.ok) {
        const { vendors = [], products = [] } = await metaResp.json();
        const vendorList = document.getElementById("vendor-list");
        const productList = document.getElementById("product-list");
        vendors.forEach((v) => { const o = document.createElement("option"); o.value = v; vendorList.appendChild(o); });
        products.forEach((p) => { const o = document.createElement("option"); o.value = p; productList.appendChild(o); });
      }
    } catch (_) {
      // Non-fatal — dropdowns just won't have suggestions
    }
  }

  saveBtn.addEventListener("click", async () => {
    const vendor = vendorInput.value.trim().toLowerCase();
    const product = productInput.value.trim().toLowerCase();

    saveBtn.disabled = true;
    showStatus("loading", "Fetching and indexing…");

    const body = { url };
    if (vendor) body.vendor = vendor;
    if (product) body.product = product;

    // Capture the already-rendered DOM so JS-gated pages (SPAs, login walls, etc.)
    // are indexed correctly. Skip for Reddit — those are rewritten to old.reddit.com
    // which the server fetches as plain HTML.
    if (!redditRewrite) {
      try {
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => document.documentElement.outerHTML,
        });
        if (result) body.html_content = result;
      } catch (_) {
        // Tab doesn't allow script injection (e.g. browser built-in pages) — fall back to server fetch
      }
    }

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
    chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
  });
});

function showStatus(type, message) {
  const el = document.getElementById("status");
  el.className = `status ${type}`;
  el.textContent = message;
}
