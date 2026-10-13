(() => {
  const boot = window.ADMIN_BOOT || { canUsers: false, canRoles: false };
  const usersPane = document.getElementById("usersPane");
  const rolesPane = document.getElementById("rolesPane");
  const alertBox = document.getElementById("adminAlert");
  let pack = null;
  let csrf = "";
  let tab = boot.canUsers ? "users" : "roles";
  let selectedUserId = null;
  let creatingUser = false;
  let selectedRoleId = null;
  let creatingRole = false;
  let userFilter = { q: "", roleId: "", status: "all" };
  let treeQuery = "";
  let previewTimer = 0;

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showAlert(message, ok) {
    if (!message) {
      alertBox.hidden = true;
      alertBox.textContent = "";
      return;
    }
    alertBox.hidden = false;
    alertBox.className = "admin-alert" + (ok ? " ok" : "");
    alertBox.textContent = message;
  }

  async function api(path, options) {
    const opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (csrf && opts.method && opts.method !== "GET") opts.headers["X-CSRF-Token"] = csrf;
    const res = await fetch(path, opts);
    const body = await res.json().catch(() => ({ ok: false, error: "Request failed" }));
    if (!res.ok || body.ok === false) throw new Error(body.error || "Request failed");
    return body;
  }

  function roleById(id) {
    return (pack.roles || []).find((role) => role.roleId === id) || null;
  }

  function roleHas(role, key) {
    if (!role) return false;
    if (role.unrestricted) return true;
    return (role.permissions || []).includes(key);
  }

  function draftFromUser(user) {
    return {
      userId: user ? user.userId : null,
      username: user ? user.username : "",
      displayName: user ? user.displayName : "",
      details: user ? user.details : "",
      roleId: user ? user.roleId : ((pack.roles.find((r) => r.roleId === "role_operator") || pack.roles[0] || {}).roleId || ""),
      isActive: user ? !!user.isActive : true,
      password: "",
      grants: user ? (user.grants || []).map((g) => ({ scopeType: g.scopeType, scopeId: g.scopeId })) : [],
      denies: user ? (user.denies || []).slice() : [],
    };
  }

  function readUserDraft(base) {
    const root = usersPane.querySelector("[data-draft]");
    if (!root) return base;
    const grants = [];
    root.querySelectorAll("[data-grant]:checked").forEach((el) => {
      grants.push({ scopeType: el.dataset.scope, scopeId: el.dataset.grant });
    });
    const denies = [];
    root.querySelectorAll("[data-deny]").forEach((el) => {
      if (el.dataset.on === "1") denies.push(el.dataset.deny);
    });
    return {
      userId: base.userId,
      username: root.querySelector("[name=username]").value.trim(),
      displayName: root.querySelector("[name=displayName]").value.trim(),
      details: root.querySelector("[name=details]").value.trim(),
      roleId: root.querySelector("[name=roleId]").value,
      isActive: root.querySelector("[name=isActive]").checked,
      password: root.querySelector("[name=password]").value,
      grants,
      denies,
    };
  }

  function visibleIndex() {
    const locations = new Set();
    const nvrs = new Set();
    const cameras = new Set();
    (pack.tree || []).forEach((loc) => {
      locations.add(loc.id);
      (loc.nvrs || []).forEach((nvr) => {
        nvrs.add(nvr.id);
        (nvr.cameras || []).forEach((cam) => cameras.add(cam.deviceId));
      });
    });
    return { locations, nvrs, cameras };
  }

  function hiddenAssignmentCount(user) {
    if (!user) return 0;
    const idx = visibleIndex();
    const grants = (user.grants || []).filter((g) => {
      if (g.scopeType === "LOCATION") return !idx.locations.has(g.scopeId);
      if (g.scopeType === "NVR") return !idx.nvrs.has(g.scopeId);
      return !idx.cameras.has(g.scopeId);
    }).length;
    const denies = (user.denies || []).filter((id) => !idx.cameras.has(id)).length;
    return grants + denies;
  }

  function filteredUsers() {
    const q = userFilter.q.trim().toLowerCase();
    return (pack.users || []).filter((user) => {
      if (userFilter.roleId && user.roleId !== userFilter.roleId) return false;
      if (userFilter.status === "active" && !user.isActive) return false;
      if (userFilter.status === "inactive" && user.isActive) return false;
      if (!q) return true;
      const role = roleById(user.roleId);
      const blob = [user.username, user.displayName, user.details, role && role.name].join(" ").toLowerCase();
      return blob.includes(q);
    });
  }

  function cameraRows(loc) {
    const rows = [];
    (loc.nvrs || []).forEach((nvr) => {
      (nvr.cameras || []).forEach((cam) => rows.push({ cam, nvrId: nvr.id, locId: loc.id }));
    });
    return rows;
  }

  function renderUsers() {
    const users = filteredUsers();
    const selected = creatingUser ? null : (pack.users || []).find((user) => user.userId === selectedUserId) || null;
    const draft = draftFromUser(creatingUser ? null : selected);
    if (!creatingUser && selected) selectedUserId = selected.userId;
    const roleOptions = (pack.roles || []).map((role) => {
      const disabled = role.unrestricted && !(pack.actor && pack.actor.unrestricted) ? " disabled" : "";
      const sel = role.roleId === draft.roleId ? " selected" : "";
      return `<option value="${esc(role.roleId)}"${sel}${disabled}>${esc(role.name)}</option>`;
    }).join("");
    const list = users.map((user) => {
      const role = roleById(user.roleId);
      const active = !creatingUser && user.userId === selectedUserId ? " active" : "";
      return `<button type="button" class="user-row${active}" data-user="${esc(user.userId)}">
        <strong>${esc(user.displayName || user.username)}
          <i class="badge ${user.isActive ? "on" : "off"}">${user.isActive ? "Active" : "Inactive"}</i>
        </strong>
        <span>${esc(user.username)} · ${esc(role ? role.name : "No role")}</span>
      </button>`;
    }).join("") || `<div class="empty">No users match this filter.</div>`;

    usersPane.innerHTML = `<div class="layout">
      <div class="panel">
        <div class="toolbar">
          <input id="userSearch" placeholder="Search name or username" value="${esc(userFilter.q)}" />
          <select id="roleFilter">
            <option value="">All roles</option>
            ${(pack.roles || []).map((role) => `<option value="${esc(role.roleId)}"${role.roleId === userFilter.roleId ? " selected" : ""}>${esc(role.name)}</option>`).join("")}
          </select>
          <select id="statusFilter">
            <option value="all"${userFilter.status === "all" ? " selected" : ""}>All statuses</option>
            <option value="active"${userFilter.status === "active" ? " selected" : ""}>Active</option>
            <option value="inactive"${userFilter.status === "inactive" ? " selected" : ""}>Inactive</option>
          </select>
        </div>
        <button type="button" class="btn" id="newUser">New user</button>
        <div style="margin-top:10px">${list}</div>
      </div>
      <form class="panel" data-draft>
        <div class="editor-head">
          <h2>${creatingUser ? "Create user" : "Edit user"}</h2>
          <button type="submit" class="btn">${creatingUser ? "Create user" : "Save user"}</button>
        </div>
        <p class="chain">User <b data-chain-user>${esc(draft.displayName || draft.username || "New user")}</b>
          → Role <b data-chain-role>${esc((roleById(draft.roleId) || {}).name || "—")}</b>
          → Locations → NVRs → Cameras → Permissions</p>
        ${selected && hiddenAssignmentCount(selected) ? `<p class="note">Some assignments are outside your own camera scope. They stay in place when you save.</p>` : ""}
        <div class="grid-2">
          <div class="field"><label>Username</label><input name="username" required value="${esc(draft.username)}" autocomplete="off" /></div>
          <div class="field"><label>Name</label><input name="displayName" value="${esc(draft.displayName)}" /></div>
        </div>
        <div class="field"><label>Details</label><textarea name="details" rows="2">${esc(draft.details)}</textarea></div>
        <div class="grid-2">
          <div class="field"><label>Password</label><input name="password" type="password" autocomplete="new-password" placeholder="${creatingUser ? "At least 8 characters" : "Leave blank to keep the current password"}" /></div>
          <div class="field"><label>Role</label><select name="roleId">${roleOptions}</select></div>
        </div>
        <label class="checkline"><input type="checkbox" name="isActive"${draft.isActive ? " checked" : ""} /> Active account</label>
        <div class="summary" id="accessSummary">
          <div><strong>—</strong><span>Cameras</span></div>
          <div><strong>—</strong><span>Locations</span></div>
          <div><strong>—</strong><span>NVRs</span></div>
        </div>
        <div class="section-label">Permission preview</div>
        <div class="perm-chips" id="permPreview"></div>
        <div class="section-label">Camera access</div>
        <div class="tree-tools"><input id="treeSearch" placeholder="Filter locations, NVRs, cameras" value="${esc(treeQuery)}" /></div>
        <div class="tree" id="accessTree"></div>
      </form>
    </div>`;

    usersPane.querySelector("#userSearch").addEventListener("input", (ev) => {
      userFilter.q = ev.target.value;
      const q = userFilter.q.trim().toLowerCase();
      usersPane.querySelectorAll("[data-user]").forEach((btn) => {
        const user = (pack.users || []).find((item) => item.userId === btn.dataset.user);
        const role = user && roleById(user.roleId);
        const blob = user ? [user.username, user.displayName, user.details, role && role.name].join(" ").toLowerCase() : "";
        btn.hidden = q ? !blob.includes(q) : false;
      });
    });
    usersPane.querySelector("#roleFilter").addEventListener("change", (ev) => {
      userFilter.roleId = ev.target.value;
      renderUsers();
    });
    usersPane.querySelector("#statusFilter").addEventListener("change", (ev) => {
      userFilter.status = ev.target.value;
      renderUsers();
    });
    usersPane.querySelector("#newUser").addEventListener("click", () => {
      creatingUser = true;
      selectedUserId = null;
      treeQuery = "";
      renderUsers();
    });
    usersPane.querySelectorAll("[data-user]").forEach((btn) => {
      btn.addEventListener("click", () => {
        creatingUser = false;
        selectedUserId = btn.dataset.user;
        treeQuery = "";
        renderUsers();
      });
    });
    const form = usersPane.querySelector("[data-draft]");
    form.addEventListener("input", schedulePreview);
    form.addEventListener("change", (ev) => {
      if (ev.target && ev.target.name === "roleId") {
        const current = readUserDraft({ userId: creatingUser ? null : selectedUserId });
        paintTree(current);
      }
      schedulePreview();
    });
    const treeSearch = usersPane.querySelector("#treeSearch");
    if (treeSearch) {
      treeSearch.addEventListener("input", (ev) => {
        treeQuery = ev.target.value;
        const current = readUserDraft({ userId: creatingUser ? null : selectedUserId });
        paintTree(current);
        const again = document.getElementById("treeSearch");
        if (again) {
          again.focus();
          const end = again.value.length;
          again.setSelectionRange(end, end);
        }
      });
    }
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = readUserDraft(draft);
      try {
        if (creatingUser) await api("/api/admin/users", { method: "POST", body: JSON.stringify(body) });
        else await api("/api/admin/users/" + encodeURIComponent(body.userId), { method: "PUT", body: JSON.stringify(body) });
        showAlert(creatingUser ? "User created." : "User saved.", true);
        creatingUser = false;
        await reload(body.username);
      } catch (err) {
        showAlert(err.message);
      }
    });
    paintTree(draft);
    schedulePreview();
  }

  function renderUsersKeeping(current) {
    const focus = document.activeElement && document.activeElement.id;
    renderUsers();
    const form = usersPane.querySelector("[data-draft]");
    if (!form || !current) return;
    ["username", "displayName", "details", "password", "roleId"].forEach((name) => {
      const el = form.querySelector(`[name=${name}]`);
      if (el && current[name] != null) el.value = current[name];
    });
    const active = form.querySelector("[name=isActive]");
    if (active) active.checked = !!current.isActive;
    if (focus) {
      const next = document.getElementById(focus);
      if (next) next.focus();
    }
  }

  function paintTree(draft) {
    const tree = document.getElementById("accessTree");
    const form = usersPane.querySelector("[data-draft]");
    if (!tree || !form) return;
    const role = roleById(form.querySelector("[name=roleId]").value);
    if (role && role.unrestricted) {
      tree.innerHTML = `<div class="empty">Super Admin has unrestricted access to every location, NVR, and camera. Assignments are not required.</div>`;
      return;
    }
    const q = treeQuery.trim().toLowerCase();
    const grantSet = new Set((draft.grants || []).map((g) => g.scopeType + ":" + g.scopeId));
    const checkedNow = new Set();
    form.querySelectorAll("[data-grant]:checked").forEach((el) => checkedNow.add(el.dataset.scope + ":" + el.dataset.grant));
    const useChecks = form.querySelector("[data-grant]") ? checkedNow : grantSet;
    const denySet = new Set(draft.denies || []);
    form.querySelectorAll("[data-deny]").forEach((el) => {
      if (el.dataset.on === "1") denySet.add(el.dataset.deny);
      else denySet.delete(el.dataset.deny);
    });
    const locOn = (id) => useChecks.has("LOCATION:" + id);
    const nvrOn = (id) => useChecks.has("NVR:" + id);
    const canLoc = roleHas(role, "locations.access");
    const canNvr = roleHas(role, "nvr.access");

    const html = (pack.tree || []).map((loc) => {
      const nvrs = (loc.nvrs || []).map((nvr) => {
        const cams = (nvr.cameras || []).filter((cam) => {
          if (!q) return true;
          return [loc.id, nvr.label, cam.name, cam.ip].join(" ").toLowerCase().includes(q);
        });
        if (q && !cams.length && !nvr.label.toLowerCase().includes(q)) return "";
        const camHtml = (q ? cams : nvr.cameras).map((cam) => {
          const viaLoc = canLoc && locOn(loc.id);
          const viaNvr = canNvr && nvrOn(nvr.id);
          const explicit = useChecks.has("CAMERA:" + cam.deviceId);
          const denied = denySet.has(cam.deviceId);
          const on = !denied && (explicit || viaLoc || viaNvr);
          const tag = !on ? "" : explicit ? "direct" : viaLoc ? "via location" : viaNvr ? "via NVR" : "";
          return `<div class="cam">
            <label><input type="checkbox" data-camera="${esc(cam.deviceId)}" data-loc="${esc(loc.id)}" data-nvr="${esc(nvr.id)}" ${on ? "checked" : ""} />
              <span>${esc(cam.name)} <span class="meta">${esc(cam.ip)}</span> ${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</span>
            </label>
            <input type="hidden" data-deny="${esc(cam.deviceId)}" data-on="${denied ? "1" : "0"}" />
          </div>`;
        }).join("");
        return `<div class="nvr"><div class="node"><label>
          <input type="checkbox" data-grant="${esc(nvr.id)}" data-scope="NVR" ${nvrOn(nvr.id) ? "checked" : ""} ${canNvr ? "" : "disabled"} />
          <span>${esc(nvr.label)} ${canNvr ? "" : `<span class="meta">role has no NVR access</span>`}</span>
        </label></div>${camHtml}</div>`;
      }).join("");
      if (!nvrs) return "";
      return `<div class="loc"><div class="node"><label>
        <input type="checkbox" data-grant="${esc(loc.id)}" data-scope="LOCATION" ${locOn(loc.id) ? "checked" : ""} ${canLoc ? "" : "disabled"} />
        <span>${esc(loc.id)} ${canLoc ? "" : `<span class="meta">role has no location access</span>`}</span>
      </label></div>${nvrs}</div>`;
    }).join("");
    tree.innerHTML = html || `<div class="empty">No cameras match.</div>`;
    tree.querySelectorAll("[data-camera]").forEach((box) => {
      box.addEventListener("change", () => {
        const id = box.dataset.camera;
        tree.querySelectorAll(`[data-deny="${CSS.escape(id)}"]`).forEach((hidden) => {
          hidden.dataset.on = box.checked ? "0" : "1";
        });
        const direct = tree.querySelector(`[data-grant="${CSS.escape(id)}"][data-scope="CAMERA"]`);
        if (box.checked) {
          const covered = (canLoc && locOn(box.dataset.loc)) || (canNvr && nvrOn(box.dataset.nvr));
          if (!covered && !direct) {
            const marker = document.createElement("input");
            marker.type = "checkbox";
            marker.dataset.grant = id;
            marker.dataset.scope = "CAMERA";
            marker.checked = true;
            marker.hidden = true;
            tree.appendChild(marker);
          }
        } else if (direct) {
          direct.checked = false;
        }
        schedulePreview();
        const current = readUserDraft(draftFromUser(null));
        current.userId = (usersPane.querySelector("[data-draft]") && selectedUserId && !creatingUser) ? selectedUserId : null;
        paintTree(current);
      });
    });
    tree.querySelectorAll("[data-scope='LOCATION'], [data-scope='NVR']").forEach((box) => {
      box.addEventListener("change", () => {
        if (box.checked) {
          const ids = box.dataset.scope === "LOCATION"
            ? cameraRows((pack.tree || []).find((loc) => loc.id === box.dataset.grant) || { nvrs: [] }).map((row) => row.cam.deviceId)
            : ((pack.tree || []).flatMap((loc) => loc.nvrs || []).find((nvr) => nvr.id === box.dataset.grant) || { cameras: [] }).cameras.map((cam) => cam.deviceId);
          ids.forEach((id) => {
            tree.querySelectorAll(`[data-deny="${CSS.escape(id)}"]`).forEach((hidden) => {
              hidden.dataset.on = "0";
            });
          });
        }
        const current = readUserDraft(draftFromUser(null));
        current.userId = !creatingUser ? selectedUserId : null;
        paintTree(current);
        schedulePreview();
      });
    });
  }

  function schedulePreview() {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(sendPreview, 200);
  }

  async function sendPreview() {
    const form = usersPane.querySelector("[data-draft]");
    if (!form || tab !== "users") return;
    const body = readUserDraft({ userId: creatingUser ? null : selectedUserId });
    const chainUser = form.querySelector("[data-chain-user]");
    const chainRole = form.querySelector("[data-chain-role]");
    if (chainUser) chainUser.textContent = body.displayName || body.username || "New user";
    if (chainRole) chainRole.textContent = (roleById(body.roleId) || {}).name || "—";
    try {
      const res = await api("/api/admin/preview", { method: "POST", body: JSON.stringify(body) });
      const summary = res.summary || {};
      const boxes = form.querySelectorAll("#accessSummary strong");
      if (boxes.length === 3) {
        boxes[0].textContent = summary.unrestricted ? "All" : String(summary.cameraCount ?? 0);
        boxes[1].textContent = summary.unrestricted ? "All" : String(summary.locationCount ?? 0);
        boxes[2].textContent = summary.unrestricted ? "All" : String(summary.nvrCount ?? 0);
      }
      const chips = form.querySelector("#permPreview");
      if (chips) {
        const perms = summary.permissions || [];
        chips.innerHTML = perms.map((item) => `<i>${esc(item.label)}</i>`).join("") || `<i>No module permissions</i>`;
      }
    } catch (err) {
      const chips = form.querySelector("#permPreview");
      if (chips) chips.innerHTML = `<i>${esc(err.message)}</i>`;
    }
  }

  function renderRoles() {
    const selected = creatingRole ? null : (pack.roles || []).find((role) => role.roleId === selectedRoleId) || (pack.roles || [])[0] || null;
    if (selected && !creatingRole) selectedRoleId = selected.roleId;
    const groups = {};
    (pack.permissionCatalog || []).forEach((item) => {
      (groups[item.group] = groups[item.group] || []).push(item);
    });
    const selectedPerms = new Set(selected && !creatingRole ? selected.permissions || [] : []);
    const locked = selected && selected.unrestricted && !creatingRole;
    const groupHtml = Object.keys(groups).map((name) => {
      const items = groups[name].map((item) => {
        const checked = locked || selectedPerms.has(item.key) ? " checked" : "";
        const disabled = locked ? " disabled" : "";
        return `<label class="perm-item"><input type="checkbox" name="perm" value="${esc(item.key)}"${checked}${disabled} /> ${esc(item.label)}</label>`;
      }).join("");
      return `<div class="perm-group"><h3>${esc(name)}</h3>${items}</div>`;
    }).join("");
    const list = (pack.roles || []).map((role) => {
      const active = !creatingRole && selected && role.roleId === selected.roleId ? " active" : "";
      return `<button type="button" class="role-row${active}" data-role="${esc(role.roleId)}">
        <strong>${esc(role.name)}${role.unrestricted ? `<i class="badge on">Unrestricted</i>` : role.isSystem ? `<i class="badge off">System</i>` : ""}</strong>
        <span>${esc(role.description || "Custom role")}</span>
      </button>`;
    }).join("");
    rolesPane.innerHTML = `<div class="layout">
      <div class="panel">
        <button type="button" class="btn" id="newRole">New role</button>
        <div style="margin-top:10px">${list}</div>
      </div>
      <form class="panel" id="roleForm">
        <div class="editor-head">
          <h2>${creatingRole ? "Create role" : "Edit role"}</h2>
          <div style="display:flex;gap:8px">
            ${!creatingRole && selected && !selected.isSystem ? `<button type="button" class="btn danger" id="deleteRole">Delete</button>` : ""}
            <button type="submit" class="btn">${creatingRole ? "Create role" : "Save role"}</button>
          </div>
        </div>
        ${locked ? `<p class="note">Super Admin always has every permission and unrestricted camera access. You can update the description only.</p>` : ""}
        <div class="field"><label>Name</label><input name="name" required value="${esc(creatingRole ? "" : (selected && selected.name) || "")}" ${locked ? "disabled" : ""} /></div>
        <div class="field"><label>Description</label><textarea name="description" rows="2">${esc(creatingRole ? "" : (selected && selected.description) || "")}</textarea></div>
        <div class="section-label">Permissions</div>
        ${groupHtml}
      </form>
    </div>`;
    rolesPane.querySelector("#newRole").addEventListener("click", () => {
      creatingRole = true;
      selectedRoleId = null;
      renderRoles();
    });
    rolesPane.querySelectorAll("[data-role]").forEach((btn) => {
      btn.addEventListener("click", () => {
        creatingRole = false;
        selectedRoleId = btn.dataset.role;
        renderRoles();
      });
    });
    const del = rolesPane.querySelector("#deleteRole");
    if (del && selected) {
      del.addEventListener("click", async () => {
        if (!window.confirm("Delete this role?")) return;
        try {
          await api("/api/admin/roles/" + encodeURIComponent(selected.roleId), { method: "DELETE" });
          showAlert("Role deleted.", true);
          creatingRole = false;
          selectedRoleId = null;
          await reload();
        } catch (err) {
          showAlert(err.message);
        }
      });
    }
    rolesPane.querySelector("#roleForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = ev.target;
      const permissions = [...form.querySelectorAll("input[name=perm]:checked")].map((el) => el.value);
      const body = {
        name: form.querySelector("[name=name]").value.trim(),
        description: form.querySelector("[name=description]").value.trim(),
        permissions: locked && selected ? selected.permissions : permissions,
      };
      try {
        if (creatingRole) await api("/api/admin/roles", { method: "POST", body: JSON.stringify(body) });
        else await api("/api/admin/roles/" + encodeURIComponent(selected.roleId), { method: "PUT", body: JSON.stringify(body) });
        showAlert(creatingRole ? "Role created." : "Role saved.", true);
        creatingRole = false;
        await reload();
      } catch (err) {
        showAlert(err.message);
      }
    });
  }

  function showTab(next) {
    tab = next;
    document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === next));
    usersPane.hidden = next !== "users";
    rolesPane.hidden = next !== "roles";
    if (next === "users") renderUsers();
    else renderRoles();
  }

  async function reload(preferUsername) {
    pack = await api("/api/admin/bootstrap");
    csrf = pack.csrf || csrf;
    if (preferUsername) {
      const match = (pack.users || []).find((user) => user.username.toLowerCase() === String(preferUsername).toLowerCase());
      if (match) selectedUserId = match.userId;
    }
    if (tab === "users" && boot.canUsers) renderUsers();
    else if (boot.canRoles) renderRoles();
  }

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  });

  reload().catch((err) => showAlert(err.message));
})();
