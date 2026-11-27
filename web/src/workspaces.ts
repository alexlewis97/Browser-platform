// Workspaces: named sets of workflows that scope chat, skills, MCP and the run-site catalogue.
import { request } from "./api";
import { he } from "./copy/he";
import { escapeHtml } from "./validation";

export type Workspace = {
  id: string;
  slug: string;
  name: string;
  description?: string;
  instructions?: string;
  workflow_ids: string[];
  workflow_count?: number;
};

const CHOICE_KEY = "bp-workspace";

/** Remembered workspace slug ("" = all workflows). */
export function currentWorkspace(): string {
  try {
    return localStorage.getItem(CHOICE_KEY) || "";
  } catch {
    return "";
  }
}

export function setCurrentWorkspace(slug: string): void {
  try {
    if (slug) localStorage.setItem(CHOICE_KEY, slug);
    else localStorage.removeItem(CHOICE_KEY);
  } catch {
    /* private mode */
  }
}

export const workspacesApi = {
  list: () => request<{ workspaces: Workspace[] }>("/api/workspaces"),
  create: (body: Partial<Workspace>) => request<Workspace>("/api/workspaces", { method: "POST", body: JSON.stringify(body) }),
  update: (key: string, body: Partial<Workspace>) =>
    request<Workspace>(`/api/workspaces/${encodeURIComponent(key)}`, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (key: string) => request<{ ok: boolean }>(`/api/workspaces/${encodeURIComponent(key)}`, { method: "DELETE" }),
};

let cache: Workspace[] | null = null;

export async function loadWorkspaces(force = false): Promise<Workspace[]> {
  if (cache && !force) return cache;
  try {
    cache = (await workspacesApi.list()).workspaces || [];
  } catch {
    cache = cache || [];
  }
  // Forget a remembered workspace that no longer exists.
  const chosen = currentWorkspace();
  if (chosen && !cache.some((ws) => ws.slug === chosen)) setCurrentWorkspace("");
  return cache;
}

export function cachedWorkspaces(): Workspace[] {
  return cache || [];
}

/** <select> of workspaces; value is the slug, "" for all. */
export function workspaceSelect(id: string, selected: string, opts: { allLabel?: string; name?: string } = {}): string {
  const options = cachedWorkspaces()
    .map((ws) => `<option value="${escapeHtml(ws.slug)}" ${ws.slug === selected ? "selected" : ""}>${escapeHtml(ws.name)} (${ws.workflow_ids.length})</option>`)
    .join("");
  return `<select id="${id}" ${opts.name ? `name="${opts.name}"` : ""}><option value="">${escapeHtml(opts.allLabel || he.workspaces.all)}</option>${options}</select>`;
}

type WorkflowLite = { id: string; name: string; description?: string };

export function renderWorkspacesBody(workspaces: Workspace[], workflows: WorkflowLite[], editing: string): string {
  const cards = workspaces
    .map((ws) => {
      const open = ws.slug === editing;
      const members = new Set(ws.workflow_ids);
      const names = workflows.filter((w) => members.has(w.id)).map((w) => escapeHtml(w.name));
      const body = open
        ? `<form class="ws-edit" data-ws="${escapeHtml(ws.id)}">
            <label class="field"><span>${he.workspaces.name}</span><input name="name" required value="${escapeHtml(ws.name)}" /></label>
            <label class="field"><span>${he.workspaces.description}</span><input name="description" value="${escapeHtml(ws.description || "")}" /></label>
            <label class="field"><span>${he.workspaces.instructions}</span><textarea name="instructions" placeholder="${he.workspaces.instructionsPh}">${escapeHtml(ws.instructions || "")}</textarea></label>
            <fieldset class="ws-members"><legend>${he.workspaces.members}</legend>
              ${workflows
                .map(
                  (w) => `<label class="check"><input type="checkbox" name="wf" value="${escapeHtml(w.id)}" ${members.has(w.id) ? "checked" : ""} /><span>${escapeHtml(w.name)}</span></label>`,
                )
                .join("") || `<p class="muted">${he.workspaces.noWorkflows}</p>`}
            </fieldset>
            <div class="btn-row">
              <button class="btn primary" type="submit">${he.workspaces.save}</button>
              <a class="btn" href="#/workspaces">${he.workspaces.close}</a>
              <button class="btn danger" type="button" data-ws-delete="${escapeHtml(ws.id)}" data-ws-name="${escapeHtml(ws.name)}">${he.workspaces.delete}</button>
            </div>
          </form>`
        : `<p class="muted">${escapeHtml(ws.description || "")}</p>
           <p style="margin:6px 0">${names.length ? names.join(" · ") : `<span class="muted">${he.workspaces.empty}</span>`}</p>
           <div class="btn-row">
             <a class="btn" href="#/workspaces/${escapeHtml(ws.slug)}">${he.workspaces.edit}</a>
             <code class="muted" dir="ltr">/mcp/${escapeHtml(ws.slug)}</code>
           </div>`;
      return `<div class="card"><h2 style="margin:0 0 6px;font-size:1.1rem">${escapeHtml(ws.name)} <span class="muted">(${ws.workflow_ids.length})</span></h2>${body}</div>`;
    })
    .join("");
  return `
    <p class="muted">${he.workspaces.explain}</p>
    ${cards || `<div class="card"><p class="muted">${he.workspaces.none}</p></div>`}
    <form id="ws-create" class="card">
      <h2 style="margin:0 0 8px;font-size:1.05rem">${he.workspaces.create}</h2>
      <label class="field"><span>${he.workspaces.name}</span><input name="name" required placeholder="${he.workspaces.namePh}" /></label>
      <label class="field"><span>${he.workspaces.description}</span><input name="description" placeholder="${he.workspaces.descriptionPh}" /></label>
      <button class="btn primary" type="submit">${he.workspaces.create}</button>
    </form>`;
}

/** Wire the workspaces page. ``rerender`` re-renders the route after a change. */
export function bindWorkspacesPage(rerender: () => void): void {
  const create = document.getElementById("ws-create") as HTMLFormElement | null;
  create?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(create);
    try {
      const ws = await workspacesApi.create({ name: String(data.get("name") || ""), description: String(data.get("description") || "") });
      await loadWorkspaces(true);
      location.hash = `#/workspaces/${ws.slug}`;
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  });
  document.querySelectorAll<HTMLFormElement>("form.ws-edit").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      try {
        await workspacesApi.update(form.dataset.ws!, {
          name: String(data.get("name") || ""),
          description: String(data.get("description") || ""),
          instructions: String(data.get("instructions") || ""),
          workflow_ids: data.getAll("wf").map(String),
        });
        const list = await loadWorkspaces(true);
        const slug = list.find((ws) => ws.id === form.dataset.ws)?.slug;
        if (slug && location.hash !== `#/workspaces/${slug}`) location.hash = `#/workspaces/${slug}`;
        else rerender();
      } catch (err) {
        alert(err instanceof Error ? err.message : String(err));
      }
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-ws-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm(`${he.workspaces.confirmDelete} "${button.dataset.wsName}"?`)) return;
      try {
        await workspacesApi.remove(button.dataset.wsDelete!);
        await loadWorkspaces(true);
        location.hash = "#/workspaces";
      } catch (err) {
        alert(err instanceof Error ? err.message : String(err));
      }
    });
  });
}
