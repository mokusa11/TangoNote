"use strict";

const state = {
  groups: [],
  notes: [],
  selectedId: null,
  pendingDeleteId: null,
  pendingActionNoteId: null,
  longPressTimer: null,
  longPressTriggeredId: null,
  longPressClickResetTimer: null,
  longPressStart: null,
  backupImporting: false,
  syncBusy: false,
  syncConfigured: false,
  editingGroupId: null,
  pendingGroupName: null,
  draftGroupPaths: null,
  notesDirectory: "",
  pendingGroupDeleteId: null,
  activeGroupId: null,
  groupPaths: Object.create(null),
  pathEditingGroupName: null,
  query: "",
  searchTimer: null,
};

const byId = (id) => document.getElementById(id);
const editorDialog = byId("editor-dialog");
const editorFrame = byId("editor-frame");
const deleteDialog = byId("delete-dialog");
const groupDialog = byId("group-dialog");
const groupDeleteDialog = byId("group-delete-dialog");
const noteActionsDialog = byId("note-actions-dialog");
const groupPathDialog = byId("group-path-dialog");
const importDialog = byId("import-dialog");
const settingsDialog = byId("settings-dialog");
const syncReportDialog = byId("sync-report-dialog");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function showToast(message) {
  byId("toast").textContent = message;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

function renderGroups() {
  const groupList = byId("group-list");
  const selected = state.activeGroupId;
  const total = state.groups.reduce((sum, group) => sum + group.note_count, 0);
  const options = [{ id: null, name: "全部笔记", note_count: total }, ...state.groups];
  groupList.innerHTML = options.map((group) => `
    <div class="group-row">
      <button class="group-item ${selected === group.id ? "active" : ""}" type="button" data-group-id="${escapeHtml(group.id ?? "")}">
        <span>${escapeHtml(group.name)}</span><span class="group-count">${group.note_count}</span>
      </button>
      ${group.id && !group.is_default ? `<button class="group-manage-button" type="button" data-manage-group-id="${escapeHtml(group.id)}" aria-label="管理分组：${escapeHtml(group.name)}" title="管理分组">⋯</button>` : ""}
    </div>`).join("");
  groupList.querySelectorAll("[data-group-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const value = button.dataset.groupId;
      state.activeGroupId = value || null;
      state.selectedId = null;
      refreshNotes();
    });
  });
  groupList.querySelectorAll("[data-manage-group-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const group = state.groups.find((item) => item.id === button.dataset.manageGroupId);
      if (group) openGroupDialog(group);
    });
  });
}

function renderCurrentGroupInfo() {
  const group = state.groups.find((item) => item.id === state.activeGroupId);
  const total = state.groups.reduce((sum, item) => sum + item.note_count, 0);
  byId("current-group-name").textContent = group?.name || "全部笔记";
  byId("current-group-count").textContent = `${group?.note_count ?? total} 条笔记`;

  const paths = group && state.groupPaths[group.name];
  const pathList = byId("current-group-paths");
  const hint = byId("current-group-info-hint");
  pathList.hidden = !paths;
  hint.hidden = Boolean(paths);
  if (!paths) {
    hint.textContent = group
      ? "分组路径信息暂不可用。"
      : "选择单个分组可查看其笔记文件和资源目录。";
    return;
  }
  byId("current-group-note-path").textContent = paths.note_path;
  byId("current-group-resource-path").textContent =
    paths.resource_directory || paths.default_resource_directory;
}

function renderNotes() {
  clearTimeout(state.longPressTimer);
  state.longPressStart = null;
  const list = byId("results-list");
  const notes = state.notes;
  byId("note-count").textContent = state.groups.reduce((sum, group) => sum + group.note_count, 0);
  byId("result-count").textContent = `${notes.length} 条`;
  if (!notes.length) {
    list.innerHTML = `<div class="no-results">${state.query ? "没有匹配的笔记" : "这个分组还没有笔记"}</div>`;
  } else {
    list.innerHTML = notes.map((note) => `
      <button class="result-item ${note.id === state.selectedId ? "active" : ""}" type="button" data-note-id="${note.id}">
        <span class="result-term">${escapeHtml(note.term)}</span>
        <span class="result-meta">${escapeHtml(note.group)}${note.body ? ` · ${escapeHtml(plainText(note.body_html || note.body).replace(/\s+/g, " ").slice(0, 54))}` : ""}</span>
      </button>`).join("");
    list.querySelectorAll("[data-note-id]").forEach((button) => {
      button.addEventListener("click", () => selectNote(button.dataset.noteId));
    });
  }
  renderGroups();
  renderCurrentGroupInfo();
  renderDetail();
}

function plainText(value) {
  const documentValue = new DOMParser().parseFromString(value, "text/html");
  return documentValue.body.textContent || "";
}

function renderDetail() {
  const note = state.notes.find((item) => item.id === state.selectedId);
  byId("detail-empty").hidden = Boolean(note);
  byId("note-detail").hidden = !note;
  if (!note) return;
  byId("detail-group").textContent = note.group;
  byId("detail-term").textContent = note.term;
  byId("detail-meta").textContent = `更新于 ${formatDate(note.updated_at)}`;
  byId("detail-body").innerHTML = note.body_html;
}

async function selectNote(id) {
  state.selectedId = id;
  renderNotes();
  try {
    const note = await api(`/api/notes/${encodeURIComponent(id)}`);
    const index = state.notes.findIndex((item) => item.id === id);
    if (index >= 0) state.notes[index] = note;
    renderDetail();
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshNotes() {
  try {
    const [groups, notes, settings, highlightSettings, groupPaths] = await Promise.all([
      api("/api/groups"),
      api(`/api/notes?${new URLSearchParams({
        q: state.query,
        ...(state.activeGroupId ? { group_id: state.activeGroupId } : {}),
      })}`),
      api("/api/settings/notes-directory"),
      api("/api/settings/highlight-rules"),
      api("/api/settings/group-paths"),
    ]);
    state.groups = groups;
    state.notes = notes;
    state.notesDirectory = settings.notes_directory;
    state.groupPaths = Object.assign(
      Object.create(null),
      groupPaths.group_paths,
    );
    const notesDirectoryInput = byId("notes-directory");
    if (document.activeElement !== notesDirectoryInput) {
      notesDirectoryInput.value = settings.notes_directory;
    }
    const highlightRulesPathInput = byId("highlight-rules-path");
    if (document.activeElement !== highlightRulesPathInput) {
      highlightRulesPathInput.value = highlightSettings.highlight_rules_path;
    }
    if (!notes.some((note) => note.id === state.selectedId)) {
      state.selectedId = notes[0]?.id ?? null;
    }
    renderNotes();
    byId("connection-status").textContent = "已连接";
    return true;
  } catch (error) {
    byId("connection-status").textContent = "连接失败";
    showToast(error.message);
    return false;
  }
}

async function importBackup(file) {
  if (!file || state.backupImporting) return;
  state.backupImporting = true;
  byId("import-file").disabled = true;
  byId("import-button").disabled = true;
  byId("cancel-import-button").disabled = true;
  byId("import-status").textContent = "正在校验并导入备份…";
  try {
    const backup = JSON.parse(await file.text());
    const result = await api("/api/backup/import", { method: "POST", body: JSON.stringify(backup) });
    const refreshed = await refreshNotes();
    byId("import-status").textContent = refreshed
      ? `导入完成：新增 ${result.created} 条，更新 ${result.updated} 条。`
      : `备份已导入：新增 ${result.created} 条，更新 ${result.updated} 条；但笔记列表刷新失败，请重新加载页面。`;
  } catch (error) {
    byId("import-status").textContent =
      error instanceof SyntaxError ? "备份文件不是有效的 JSON。" : error.message;
  } finally {
    byId("import-file").value = "";
    byId("import-button").disabled = true;
    byId("import-file").disabled = false;
    byId("cancel-import-button").disabled = false;
    state.backupImporting = false;
  }
}

function wireEvents() {
  const resultsList = byId("results-list");
  resultsList.addEventListener("pointerdown", (event) => {
    const button = event.target.closest("[data-note-id]");
    if (!button || event.button !== 0 || noteActionsDialog.open) return;
    clearTimeout(state.longPressTimer);
    state.longPressStart = {
      id: button.dataset.noteId,
      x: event.clientX,
      y: event.clientY,
    };
    state.longPressTimer = setTimeout(() => {
      const note = state.notes.find((item) => item.id === state.longPressStart?.id);
      if (!note) return;
      state.longPressTriggeredId = note.id;
      clearTimeout(state.longPressClickResetTimer);
      state.longPressClickResetTimer = setTimeout(() => {
        state.longPressTriggeredId = null;
      }, 1200);
      openNoteActions(note);
    }, 550);
  });
  document.addEventListener("pointermove", (event) => {
    if (!state.longPressStart) return;
    if (
      Math.abs(event.clientX - state.longPressStart.x) > 10
      || Math.abs(event.clientY - state.longPressStart.y) > 10
    ) {
      clearTimeout(state.longPressTimer);
      state.longPressStart = null;
    }
  });
  document.addEventListener("pointerup", () => {
    clearTimeout(state.longPressTimer);
    state.longPressStart = null;
  });
  document.addEventListener("pointercancel", () => {
    clearTimeout(state.longPressTimer);
    state.longPressStart = null;
  });
  resultsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-note-id]");
    if (button && button.dataset.noteId === state.longPressTriggeredId) {
      state.longPressTriggeredId = null;
      clearTimeout(state.longPressClickResetTimer);
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  resultsList.addEventListener("contextmenu", (event) => {
    const button = event.target.closest("[data-note-id]");
    const note = button && state.notes.find((item) => item.id === button.dataset.noteId);
    if (!note) return;
    event.preventDefault();
    openNoteActions(note);
  });
  resultsList.addEventListener("keydown", (event) => {
    if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
    const button = event.target.closest("[data-note-id]");
    const note = button && state.notes.find((item) => item.id === button.dataset.noteId);
    if (!note) return;
    event.preventDefault();
    openNoteActions(note);
  });
  byId("search-input").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    byId("clear-search").hidden = !state.query;
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(refreshNotes, 160);
  });
  byId("clear-search").addEventListener("click", () => {
    byId("search-input").value = "";
    state.query = "";
    byId("clear-search").hidden = true;
    refreshNotes();
  });
  const openNewNoteEditor = () => {
    const group = state.groups.find((item) => item.id === state.activeGroupId);
    openEditor({
      term: state.query,
      group: group?.name || "[Default]",
    });
  };
  byId("new-note-button").addEventListener("click", openNewNoteEditor);
  byId("empty-new-button").addEventListener("click", openNewNoteEditor);
  byId("add-group-button").addEventListener("click", () => openGroupDialog());
  byId("group-form").addEventListener("submit", saveGroup);
  byId("group-cancel-button").addEventListener("click", () => {
    state.pendingGroupName = null;
    groupDialog.close();
  });
  byId("group-delete-button").addEventListener("click", openGroupDeleteConfirmation);
  byId("edit-group-path-button").addEventListener("click", () => openGroupPathDialog());
  byId("cancel-group-path-button").addEventListener("click", () => groupPathDialog.close());
  byId("save-group-paths-button").addEventListener("click", saveGroupPathSettings);
  groupPathDialog.addEventListener("close", () => {
    state.pathEditingGroupName = null;
    byId("group-path-status").textContent = "";
    if (state.pendingGroupName) {
      state.draftGroupPaths = null;
      openGroupDialog(null, state.pendingGroupName);
    }
  });
  document.querySelectorAll(
    'input[name="note-path-mode"], input[name="resource-path-mode"]',
  ).forEach((input) => input.addEventListener("change", updateGroupPathFields));
  byId("group-note-path").addEventListener("input", updateGroupPathFields);
  byId("cancel-group-delete-button").addEventListener("click", () => groupDeleteDialog.close());
  byId("confirm-group-delete-button").addEventListener("click", deleteSelectedGroup);
  groupDeleteDialog.addEventListener("close", () => {
    state.pendingGroupDeleteId = null;
    byId("group-delete-status").textContent = "";
    byId("confirm-group-delete-button").disabled = false;
  });
  byId("notes-directory-form").addEventListener("submit", saveNotesDirectory);
  byId("highlight-rules-form").addEventListener("submit", saveHighlightRulesPath);
  byId("sync-directory-form").addEventListener("submit", saveSyncDirectory);
  byId("disable-sync-button").addEventListener("click", disableSync);
  byId("run-sync-button").addEventListener("click", runSync);
  byId("quick-sync-button").addEventListener("click", quickSync);
  byId("close-sync-report-button").addEventListener("click", () => syncReportDialog.close());
  byId("sync-report-view-conflicts").addEventListener("click", () => {
    syncReportDialog.close();
    if (!settingsDialog.open) settingsDialog.showModal();
    refreshSyncStatus().then(() => {
      byId("sync-conflicts").scrollIntoView({ block: "nearest" });
    });
  });
  byId("sync-conflicts").addEventListener("click", (event) => {
    const button = event.target.closest("[data-sync-choice]");
    if (!button) return;
    resolveSyncConflict(button.dataset.syncPath, button.dataset.syncChoice);
  });
  byId("open-settings-dialog").addEventListener("click", () => {
    if (!settingsDialog.open) settingsDialog.showModal();
    refreshSyncStatus();
  });
  byId("close-settings-dialog").addEventListener("click", () => settingsDialog.close());
  byId("open-import-dialog").addEventListener("click", () => {
    byId("import-file").value = "";
    byId("import-file").disabled = false;
    byId("import-button").disabled = true;
    byId("import-status").textContent = "";
    if (!importDialog.open) importDialog.showModal();
  });
  byId("cancel-import-button").addEventListener("click", () => importDialog.close());
  importDialog.addEventListener("cancel", (event) => {
    if (state.backupImporting) event.preventDefault();
  });
  importDialog.addEventListener("close", () => {
    byId("import-file").value = "";
    byId("import-button").disabled = true;
    byId("import-status").textContent = "";
  });
  byId("edit-button").addEventListener("click", () => {
    const note = state.notes.find((item) => item.id === state.selectedId);
    if (note) openEditor(note);
  });
  byId("delete-button").addEventListener("click", () => {
    const note = state.notes.find((item) => item.id === state.selectedId);
    if (note) openDeleteConfirmation(note);
  });
  byId("cancel-delete-button").addEventListener("click", () => deleteDialog.close());
  byId("confirm-delete-button").addEventListener("click", deleteSelectedNote);
  byId("note-actions-edit").addEventListener("click", () => {
    const note = state.notes.find((item) => item.id === state.pendingActionNoteId);
    noteActionsDialog.close();
    if (note) openEditor(note);
  });
  byId("note-actions-delete").addEventListener("click", () => {
    const note = state.notes.find((item) => item.id === state.pendingActionNoteId);
    noteActionsDialog.close();
    if (note) openDeleteConfirmation(note);
  });
  byId("note-actions-cancel").addEventListener("click", () => noteActionsDialog.close());
  noteActionsDialog.addEventListener("close", () => {
    state.pendingActionNoteId = null;
    state.longPressTriggeredId = null;
    clearTimeout(state.longPressClickResetTimer);
  });
  byId("close-editor-button").addEventListener("click", () => editorDialog.close());
  editorDialog.addEventListener("click", (event) => {
    if (event.target === editorDialog) editorDialog.close();
  });
  editorDialog.addEventListener("close", () => {
    editorFrame.src = "about:blank";
  });
  deleteDialog.addEventListener("close", () => {
    state.pendingDeleteId = null;
    byId("delete-status").textContent = "";
    byId("confirm-delete-button").disabled = false;
  });
  editorFrame.addEventListener("load", () => {
    try {
      editorFrame.contentDocument.documentElement.style.width = `${editorFrame.clientWidth}px`;
      const frameUrl = new URL(editorFrame.contentWindow.location.href);
      if (frameUrl.searchParams.get("saved") === "1") {
        state.selectedId = frameUrl.searchParams.get("note_id");
        editorDialog.close();
        refreshNotes();
        return;
      }
      editorFrame.contentDocument.querySelector(".cancel")?.addEventListener("click", (event) => {
        event.preventDefault();
        editorDialog.close();
      }, { once: true });
    } catch (error) {
      if (editorFrame.src !== "about:blank") showToast(`无法读取编辑器状态：${error.message}`);
    }
  });
  byId("import-file").addEventListener("change", (event) => {
    byId("import-button").disabled = !event.target.files?.[0];
  });
  byId("import-button").addEventListener("click", () => importBackup(byId("import-file").files?.[0]));
}

function openNoteActions(note) {
  state.pendingActionNoteId = note.id;
  byId("note-actions-message").textContent = `词条：${note.term}`;
  if (!noteActionsDialog.open) noteActionsDialog.showModal();
}

function openEditor(note) {
  const params = new URLSearchParams({ modal: "1" });
  if (note.id) params.set("note_id", note.id);
  else {
    params.set("term", note.term || "");
    params.set("group", note.group || "[Default]");
  }
  editorFrame.src = `/editor?${params}`;
  if (!editorDialog.open) editorDialog.showModal();
}

function openDeleteConfirmation(note) {
  state.pendingDeleteId = note.id;
  byId("delete-confirmation-message").textContent =
    `确定删除“${note.term}”这条笔记吗？此操作无法撤销。`;
  if (!deleteDialog.open) deleteDialog.showModal();
}

function updateGroupPathFields() {
  const noteIsCustom = document.querySelector(
    'input[name="note-path-mode"]:checked',
  )?.value === "custom";
  const resourcesAreCustom = document.querySelector(
    'input[name="resource-path-mode"]:checked',
  )?.value === "custom";
  byId("group-note-path-field").hidden = !noteIsCustom;
  byId("group-note-path").disabled = !noteIsCustom;
  byId("group-resource-path-field").hidden = !resourcesAreCustom;
  byId("group-resource-path").disabled = !resourcesAreCustom;
  if (state.pendingGroupName && state.draftGroupPaths) {
    const notePath = noteIsCustom
      ? byId("group-note-path").value.trim()
      : state.draftGroupPaths.default_note_path;
    const filename = `MyNote-${state.pendingGroupName}.txt`;
    const directory = pathParent(notePath || state.draftGroupPaths.default_note_path);
    byId("group-resource-default-path").textContent =
      `默认资源位置：${joinPath(directory, filename.slice(0, -4))}`;
  }
}

function joinPath(directory, name) {
  const separator = directory.includes("\\") || /^[A-Za-z]:$/.test(directory) ? "\\" : "/";
  const normalizedDirectory = directory.replace(/[\\/]+$/, "");
  return `${normalizedDirectory}${separator}${name}`;
}

function pathParent(path) {
  const separatorIndex = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
  return separatorIndex < 0 ? "" : path.slice(0, separatorIndex);
}

function draftGroupPaths(name) {
  const filename = `MyNote-${name}.txt`;
  const defaultNotePath = joinPath(state.notesDirectory, filename);
  return {
    note_path: defaultNotePath,
    default_note_path: defaultNotePath,
    resource_directory: "",
    default_resource_directory: joinPath(
      state.notesDirectory,
      filename.slice(0, -4),
    ),
  };
}

function showGroupPathDialog(groupName, paths, creating = false) {
  state.pathEditingGroupName = creating ? null : groupName;
  state.draftGroupPaths = creating ? paths : null;
  byId("group-path-dialog-title").textContent =
    creating ? "设置新分组路径" : "编辑分组路径";
  byId("group-path-dialog-name").textContent = `分组：${groupName}`;
  byId("group-note-default-path").textContent =
    `默认 TXT 路径：${paths.default_note_path}`;
  byId("group-resource-default-path").textContent =
    `默认资源位置：${paths.default_resource_directory}`;
  const noteIsCustom = paths.note_path !== paths.default_note_path;
  document.querySelector(
    `input[name="note-path-mode"][value="${noteIsCustom ? "custom" : "default"}"]`,
  ).checked = true;
  document.querySelector(
    `input[name="resource-path-mode"][value="${paths.resource_directory ? "custom" : "default"}"]`,
  ).checked = true;
  byId("group-note-path").value = noteIsCustom ? paths.note_path : "";
  byId("group-resource-path").value = paths.resource_directory;
  byId("group-path-status").textContent = "";
  byId("save-group-paths-button").textContent = creating ? "创建分组" : "保存路径";
  updateGroupPathFields();
  if (!groupPathDialog.open) groupPathDialog.showModal();
}

function openGroupPathDialog(groupId = state.editingGroupId) {
  const group = state.groups.find((item) => item.id === groupId);
  const paths = group && state.groupPaths[group.name];
  if (!group || !paths) {
    byId("group-dialog-status").textContent = "无法读取该分组的路径设置。";
    return;
  }
  state.pendingGroupName = null;
  showGroupPathDialog(group.name, paths);
}

function openNewGroupPathDialog(name) {
  state.pendingGroupName = name;
  showGroupPathDialog(name, draftGroupPaths(name), true);
}

function openGroupDialog(group = null, initialName = "") {
  state.editingGroupId = group?.id ?? null;
  state.pendingGroupName = initialName || null;
  byId("group-dialog-title").textContent = group ? "管理分组" : "新建分组";
  byId("group-name").value = group?.name ?? initialName;
  byId("group-save-button").textContent = group ? "保存名称" : "下一步：设置路径";
  byId("group-dialog-status").textContent = group?.note_count
    ? `该分组包含 ${group.note_count} 条笔记；删除前须先移出或删除笔记。`
    : "";
  const deleteButton = byId("group-delete-button");
  deleteButton.hidden = !group;
  deleteButton.disabled = Boolean(group?.note_count);
  byId("edit-group-path-button").hidden = !group;
  if (!groupDialog.open) groupDialog.showModal();
  byId("group-name").focus();
}

async function saveGroup(event) {
  event.preventDefault();
  const name = byId("group-name").value.trim();
  const saveButton = byId("group-save-button");
  if (!name) {
    byId("group-dialog-status").textContent = "分组名称不能为空。";
    return;
  }
  saveButton.disabled = true;
  byId("group-dialog-status").textContent = "正在保存…";
  try {
    if (!state.editingGroupId) {
      state.pendingGroupName = name;
      groupDialog.close();
      openNewGroupPathDialog(name);
      return;
    }
    const group = await api(`/api/groups/${encodeURIComponent(state.editingGroupId)}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    });
    state.activeGroupId = group.id;
    state.selectedId = null;
    groupDialog.close();
    const refreshed = await refreshNotes();
    if (refreshed) showToast("分组名称已更新");
  } catch (error) {
    byId("group-dialog-status").textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

function openGroupDeleteConfirmation() {
  const group = state.groups.find((item) => item.id === state.editingGroupId);
  if (!group || group.is_default || group.note_count) return;
  state.pendingGroupDeleteId = group.id;
  byId("group-delete-message").textContent =
    `确定删除空分组“${group.name}”吗？此操作无法撤销。`;
  groupDialog.close();
  if (!groupDeleteDialog.open) groupDeleteDialog.showModal();
}

async function deleteSelectedGroup() {
  if (!state.pendingGroupDeleteId) return;
  const confirmButton = byId("confirm-group-delete-button");
  confirmButton.disabled = true;
  byId("group-delete-status").textContent = "正在删除…";
  try {
    await api(`/api/groups/${encodeURIComponent(state.pendingGroupDeleteId)}`, {
      method: "DELETE",
    });
    if (state.activeGroupId === state.pendingGroupDeleteId) state.activeGroupId = null;
    state.selectedId = null;
    groupDeleteDialog.close();
    await refreshNotes();
    showToast("分组已删除");
  } catch (error) {
    byId("group-delete-status").textContent = error.message;
  } finally {
    confirmButton.disabled = false;
  }
}

async function saveNotesDirectory(event) {
  event.preventDefault();
  const input = byId("notes-directory");
  const saveButton = byId("save-notes-directory-button");
  saveButton.disabled = true;
  byId("notes-directory-status").textContent = "正在切换笔记文件夹…";
  try {
    const settings = await api("/api/settings/notes-directory", {
      method: "PUT",
      body: JSON.stringify({ notes_directory: input.value }),
    });
    input.value = settings.notes_directory;
    state.activeGroupId = null;
    state.selectedId = null;
    await refreshNotes();
    byId("notes-directory-status").textContent = "笔记根文件夹已切换；原目录内容未移动或删除。";
  } catch (error) {
    byId("notes-directory-status").textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

async function saveHighlightRulesPath(event) {
  event.preventDefault();
  const input = byId("highlight-rules-path");
  const saveButton = byId("save-highlight-rules-button");
  saveButton.disabled = true;
  byId("highlight-rules-status").textContent = "正在保存高光数据路径…";
  try {
    const settings = await api("/api/settings/highlight-rules", {
      method: "PUT",
      body: JSON.stringify({ highlight_rules_path: input.value }),
    });
    input.value = settings.highlight_rules_path;
    byId("highlight-rules-status").textContent =
      "高光数据路径已切换；旧文件保留。";
  } catch (error) {
    byId("highlight-rules-status").textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

function renderSyncConflicts(conflicts) {
  const container = byId("sync-conflicts");
  if (!conflicts.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = conflicts.map((conflict) => `
    <article class="sync-conflict">
      <p>存在冲突：<code>${escapeHtml(conflict.path)}</code></p>
      <div class="sync-conflict-actions">
        <button class="button button-quiet" type="button" data-sync-path="${escapeHtml(conflict.path)}" data-sync-choice="local">
          ${conflict.local_hash ? "保留本机版本" : "保留本机删除"}
        </button>
        ${conflict.remote_versions.map((version) => `
          <button class="button button-secondary" type="button" data-sync-path="${escapeHtml(conflict.path)}" data-sync-choice="${escapeHtml(version.id)}">
            ${version.content_hash ? "使用" : "恢复"}设备 ${escapeHtml(version.device_id.slice(0, 8))} 的版本
          </button>`).join("")}
      </div>
    </article>`).join("");
}

async function refreshSyncStatus(message = "") {
  try {
    const status = await api("/api/sync/status");
    state.syncConfigured = status.configured;
    const input = byId("sync-directory");
    if (document.activeElement !== input) {
      input.value = status.sync_directory;
    }
    byId("disable-sync-button").disabled = !status.configured || state.syncBusy;
    byId("run-sync-button").disabled = !status.configured || state.syncBusy;
    updateQuickSyncButton();
    byId("sync-directory-status").textContent = status.configured
      ? `同步已启用：${status.sync_directory}`
      : "尚未配置同步文件夹。";
    byId("sync-last-run").textContent = status.last_sync_at
      ? `上次同步：${formatDate(status.last_sync_at)}`
      : "";
    renderSyncConflicts(status.conflicts);
    if (message) byId("sync-directory-status").textContent = message;
    return status;
  } catch (error) {
    state.syncConfigured = false;
    updateQuickSyncButton();
    byId("sync-directory-status").textContent = error.message;
    byId("run-sync-button").disabled = true;
    byId("disable-sync-button").disabled = true;
    return null;
  }
}

function updateQuickSyncButton() {
  const button = byId("quick-sync-button");
  button.disabled = state.syncBusy;
  button.classList.toggle("is-syncing", state.syncBusy);
  button.setAttribute("aria-busy", String(state.syncBusy));
  const label = state.syncBusy
    ? "正在同步"
    : state.syncConfigured
      ? "立即同步"
      : "配置同步位置";
  button.title = label;
  button.setAttribute("aria-label", label);
}

async function quickSync() {
  if (state.syncBusy) return;
  const status = await refreshSyncStatus();
  if (!status) {
    if (!settingsDialog.open) settingsDialog.showModal();
    return;
  }
  if (!status.configured) {
    if (!settingsDialog.open) settingsDialog.showModal();
    byId("sync-directory-status").textContent = "请先配置同步文件夹，再使用快捷同步。";
    byId("sync-directory").focus();
    return;
  }
  await runSync({ showReport: true });
}

function showSyncReport(result, errorMessage = "") {
  const stats = byId("sync-report-stats");
  const conflictSection = byId("sync-report-conflicts");
  const conflictList = byId("sync-report-conflict-list");
  const conflictButton = byId("sync-report-view-conflicts");
  const error = byId("sync-report-error");
  const icon = byId("sync-report-icon");
  const conflicts = result?.conflicts || [];

  if (errorMessage) {
    byId("sync-report-title").textContent = "同步未完成";
    byId("sync-report-message").textContent = "请检查错误信息，修正后再重试。";
    icon.textContent = "!";
    icon.classList.add("is-error");
    stats.hidden = true;
    conflictSection.hidden = true;
    conflictButton.hidden = true;
    error.textContent = errorMessage;
    error.hidden = false;
  } else {
    byId("sync-report-title").textContent = "同步完成";
    byId("sync-report-message").textContent =
      result.uploaded === 0 && result.downloaded === 0 && conflicts.length === 0
        ? "本机与 OneDrive 内容已一致，无需传输。"
        : "请确认 OneDrive 已完成上传，再在另一台设备操作。";
    icon.textContent = "✓";
    icon.classList.remove("is-error");
    byId("sync-report-uploaded").textContent = String(result.uploaded);
    byId("sync-report-downloaded").textContent = String(result.downloaded);
    byId("sync-report-conflict-count").textContent = String(conflicts.length);
    stats.hidden = false;
    error.hidden = true;
    conflictSection.hidden = conflicts.length === 0;
    conflictButton.hidden = conflicts.length === 0;
    conflictList.replaceChildren(...conflicts.map((conflict) => {
      const item = document.createElement("li");
      item.textContent = conflict.path;
      return item;
    }));
  }

  if (!syncReportDialog.open) syncReportDialog.showModal();
}

async function saveSyncDirectory(event) {
  event.preventDefault();
  const input = byId("sync-directory");
  const saveButton = byId("save-sync-directory-button");
  saveButton.disabled = true;
  byId("sync-directory-status").textContent = "正在保存同步文件夹…";
  try {
    const result = await api("/api/settings/sync-directory", {
      method: "PUT",
      body: JSON.stringify({ sync_directory: input.value }),
    });
    input.value = result.sync_directory;
    await refreshSyncStatus("同步位置已保存。可在 OneDrive 完成同步后点击“立即同步”。");
  } catch (error) {
    byId("sync-directory-status").textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

async function disableSync() {
  const button = byId("disable-sync-button");
  button.disabled = true;
  try {
    await api("/api/settings/sync-directory", {
      method: "PUT",
      body: JSON.stringify({ sync_directory: "" }),
    });
    await refreshSyncStatus("同步已关闭；云端和本机文件均未删除。");
  } catch (error) {
    byId("sync-directory-status").textContent = error.message;
    button.disabled = false;
  }
}

async function runSync({ showReport = false } = {}) {
  if (state.syncBusy) return;
  state.syncBusy = true;
  updateQuickSyncButton();
  const button = byId("run-sync-button");
  button.disabled = true;
  byId("sync-directory-status").textContent = "正在比对并同步笔记、附件和高光规则…";
  let outcomeMessage = "";
  let syncResult = null;
  let syncError = "";
  try {
    syncResult = await api("/api/sync/run", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refreshNotes();
    outcomeMessage = `同步完成：上传 ${syncResult.uploaded} 项，下载 ${syncResult.downloaded} 项，待处理冲突 ${syncResult.conflicts.length} 项。`;
  } catch (error) {
    outcomeMessage = error.message;
    syncError = error.message;
  } finally {
    state.syncBusy = false;
    button.disabled = false;
    await refreshSyncStatus(outcomeMessage);
    if (showReport) {
      showSyncReport(syncResult, syncError);
    } else {
      showToast(outcomeMessage);
    }
  }
}

async function resolveSyncConflict(path, choice) {
  if (!path || !choice) return;
  state.syncBusy = true;
  updateQuickSyncButton();
  byId("sync-directory-status").textContent = "正在保存冲突处理结果…";
  let outcomeMessage = "";
  try {
    await api("/api/sync/resolve", {
      method: "POST",
      body: JSON.stringify({ path, choice }),
    });
    await refreshNotes();
    outcomeMessage = `已处理冲突：${path}`;
  } catch (error) {
    outcomeMessage = error.message;
  } finally {
    state.syncBusy = false;
    await refreshSyncStatus(outcomeMessage);
  }
}

async function saveGroupPathSettings() {
  const isCreatingGroup = state.pendingGroupName !== null;
  const group = isCreatingGroup ? state.pendingGroupName : state.pathEditingGroupName;
  const paths = isCreatingGroup ? state.draftGroupPaths : state.groupPaths[group];
  if (!group || !paths) {
    byId("group-path-status").textContent = "无法读取该分组的路径设置。";
    return;
  }
  const noteMode = document.querySelector('input[name="note-path-mode"]:checked')?.value;
  const resourceMode = document.querySelector('input[name="resource-path-mode"]:checked')?.value;
  const notePath = noteMode === "custom"
    ? byId("group-note-path").value.trim()
    : isCreatingGroup ? "" : paths.default_note_path;
  const resourceDirectory = resourceMode === "custom"
    ? byId("group-resource-path").value.trim()
    : "";
  if (noteMode === "custom" && !notePath) {
    byId("group-path-status").textContent = "请输入自定义 TXT 文件路径。";
    return;
  }
  if (resourceMode === "custom" && !resourceDirectory) {
    byId("group-path-status").textContent = "请输入自定义资源文件夹路径。";
    return;
  }
  const saveButton = byId("save-group-paths-button");
  const cancelButton = byId("cancel-group-path-button");
  saveButton.disabled = true;
  cancelButton.disabled = true;
  byId("group-path-status").textContent = isCreatingGroup
    ? "正在创建分组并保存路径…"
    : "正在保存分组文件路径…";
  try {
    if (isCreatingGroup) {
      const created = await api("/api/groups", {
        method: "POST",
        body: JSON.stringify({
          name: group,
          note_path: notePath,
          resource_directory: resourceDirectory,
        }),
      });
      state.pendingGroupName = null;
      state.draftGroupPaths = null;
      state.activeGroupId = created.id;
      state.selectedId = null;
    } else {
      const settings = await api("/api/settings/group-paths", {
        method: "PUT",
        body: JSON.stringify({
          group,
          note_path: notePath,
          resource_directory: resourceDirectory,
        }),
      });
      state.groupPaths = Object.assign(Object.create(null), settings.group_paths);
    }
    const refreshed = await refreshNotes();
    groupPathDialog.close();
    if (refreshed) showToast(isCreatingGroup ? "分组已创建" : "分组路径已更新");
  } catch (error) {
    byId("group-path-status").textContent = error.message;
  } finally {
    saveButton.disabled = false;
    cancelButton.disabled = false;
  }
}

async function deleteSelectedNote() {
  if (!state.pendingDeleteId) return;
  const confirmButton = byId("confirm-delete-button");
  confirmButton.disabled = true;
  byId("delete-status").textContent = "正在删除…";
  try {
    await api(`/api/notes/${encodeURIComponent(state.pendingDeleteId)}`, { method: "DELETE" });
    deleteDialog.close();
    state.selectedId = null;
    await refreshNotes();
    showToast("笔记已删除");
  } catch (error) {
    byId("delete-status").textContent = error.message;
  } finally {
    confirmButton.disabled = false;
  }
}

async function start() {
  wireEvents();
  const params = new URLSearchParams(window.location.search);
  const term = params.get("word") || params.get("q") || params.get("keyword") || "";
  const noteIdValue = params.get("note");
  const noteId = noteIdValue || null;
  if (term) {
    state.query = term;
    byId("search-input").value = term;
    byId("clear-search").hidden = false;
  }
  await refreshNotes();
  await refreshSyncStatus();
  if (noteId && state.notes.some((note) => note.id === noteId)) {
    await selectNote(noteId);
  }
}

start();
