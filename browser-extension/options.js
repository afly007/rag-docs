document.addEventListener("DOMContentLoaded", async () => {
  const serverUrlInput = document.getElementById("server-url");
  const apiKeyInput = document.getElementById("api-key");
  const saveBtn = document.getElementById("save-btn");
  const testBtn = document.getElementById("test-btn");
  const savedMsg = document.getElementById("saved-msg");
  const testResult = document.getElementById("test-result");

  // Load saved values
  const { serverUrl, apiKey } = await chrome.storage.sync.get(["serverUrl", "apiKey"]);
  if (serverUrl) serverUrlInput.value = serverUrl;
  if (apiKey) apiKeyInput.value = apiKey;

  saveBtn.addEventListener("click", async () => {
    const url = serverUrlInput.value.trim().replace(/\/$/, "");
    const key = apiKeyInput.value.trim();

    await chrome.storage.sync.set({ serverUrl: url, apiKey: key });

    savedMsg.classList.add("visible");
    setTimeout(() => savedMsg.classList.remove("visible"), 2000);
  });

  testBtn.addEventListener("click", async () => {
    const url = serverUrlInput.value.trim().replace(/\/$/, "");
    const key = apiKeyInput.value.trim();

    testResult.className = "test-result";
    testResult.textContent = "Testing…";
    testResult.style.display = "block";

    try {
      // POST a dummy URL — server will reject it with 400 (missing text) or 422,
      // but a 401 means wrong key and a network error means wrong URL.
      const resp = await fetch(`${url}/clip`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${key}`,
        },
        body: JSON.stringify({ url: "https://example.com/__clipper_test__" }),
      });

      if (resp.status === 401) {
        showTestResult("error", "Wrong API key — server returned 401 Unauthorized.");
      } else if (resp.status === 503) {
        showTestResult("error", "Server reached but CLIP_API_KEY is not set on the server.");
      } else {
        // Any other response (200, 422, etc.) means we connected and authenticated OK
        showTestResult("success", `Connected! Server responded with HTTP ${resp.status}.`);
      }
    } catch (err) {
      showTestResult("error", `Could not reach server: ${err.message}`);
    }
  });
});

function showTestResult(type, message) {
  const el = document.getElementById("test-result");
  el.className = `test-result ${type}`;
  el.textContent = message;
}
