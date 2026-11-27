function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

type ConfirmDetails = {
  ok: boolean;
  error?: string;
  name?: string;
  description?: string;
  values?: Record<string, string>;
};

const params = new URLSearchParams(location.search);
const requestId = params.get("id") || "";

const nameEl = document.getElementById("name") as HTMLElement;
const descriptionEl = document.getElementById("description") as HTMLElement;
const valuesEl = document.getElementById("values") as HTMLElement;
const errorEl = document.getElementById("error") as HTMLElement;
const confirmBtn = document.getElementById("confirm") as HTMLButtonElement;
const cancelBtn = document.getElementById("cancel") as HTMLButtonElement;

function showError(message: string): void {
  errorEl.hidden = false;
  errorEl.textContent = message;
}

async function decide(accepted: boolean): Promise<void> {
  confirmBtn.disabled = true;
  cancelBtn.disabled = true;
  const result = (await chrome.runtime.sendMessage({
    kind: "bp-confirm-decision",
    id: requestId,
    accepted,
  })) as { ok?: boolean; error?: string };
  if (result && result.ok === false && result.error) {
    showError(result.error);
    confirmBtn.disabled = false;
    cancelBtn.disabled = false;
    return;
  }
  window.close();
}

confirmBtn.addEventListener("click", () => void decide(true));
cancelBtn.addEventListener("click", () => void decide(false));

void (async () => {
  if (!requestId) {
    showError("בקשת האישור חסרה.");
    confirmBtn.disabled = true;
    return;
  }
  const details = (await chrome.runtime.sendMessage({
    kind: "bp-confirm-details",
    id: requestId,
  })) as ConfirmDetails;
  if (!details?.ok) {
    showError(details?.error || "לא נמצאה בקשת הרצה.");
    confirmBtn.disabled = true;
    return;
  }
  nameEl.textContent = details.name || "זרימה";
  descriptionEl.textContent = details.description || "אין תיאור";
  const values = details.values || {};
  const entries = Object.entries(values);
  valuesEl.innerHTML = entries.length
    ? entries
        .map(
          ([key, value]) =>
            `<div class="value"><span>${escapeHtml(key)}</span><b>${escapeHtml(value)}</b></div>`,
        )
        .join("")
    : `<p class="muted" style="margin:0">אין קלטים להרצה הזו.</p>`;
})();
