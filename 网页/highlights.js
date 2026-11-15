"use strict";

const state = {
  items: [],
  categories: [],
  page: 1,
  pageSize: 50,
  total: 0,
  pages: 1,
  includedCount: 0,
  excludedCount: 0,
  selectedTerms: new Set(),
  allMatchingSelected: false,
  anchorTerm: null,
  pendingShiftTerm: null,
  dragSelection: null,
  suppressClickAfterDrag: false,
  searchTimer: null,
  busy: false,
  requestId: 0,
};

const byId = (id) => document.getElementById(id);

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
  if (!response.ok) throw new Error(payload?.error || `请求失败 (${response.status})`);
  return payload;
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

function filters() {
  return {
    q: byId("highlight-search").value.trim(),
    status: byId("highlight-status-filter").value,
    category: byId("highlight-category-filter").value,
  };
}

function resetSelection() {
  state.selectedTerms.clear();
  state.allMatchingSelected = false;
  state.anchorTerm = null;
  renderSelection();
}

function renderCategories() {
  const filter = byId("highlight-category-filter");
  const selected = filter.value;
  filter.innerHTML = '<option value="">全部分类</option>' + state.categories
    .map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`)
    .join("");
  filter.value = state.categories.includes(selected) ? selected : "";
  byId("highlight-category-options").innerHTML = state.categories
    .map((category) => `<option value="${escapeHtml(category)}"></option>`)
    .join("");
  byId("category-count").textContent = state.categories.length;
}

function renderSelection() {
  const count = state.allMatchingSelected ? state.total : state.selectedTerms.size;
  byId("selection-count").textContent = state.allMatchingSelected
    ? `已选择全部 ${count} 条筛选结果`
    : count ? `已选择 ${count} 个词头` : "尚未选择词头";
  const disabled = count === 0 || state.busy;
  byId("include-selected").disabled = disabled;
  byId("exclude-selected").disabled = disabled;
  byId("add-category").disabled = disabled;
  byId("remove-category").disabled = disabled;
  byId("select-page").disabled = !state.items.length || state.busy;
  byId("select-all-matching").disabled = !state.total || state.busy;
  byId("clear-selection").disabled = !count || state.busy;
  for (const row of byId("highlight-list").querySelectorAll(".highlight-item")) {
    const checkbox = row.querySelector("[data-select-term]");
    const selected = state.allMatchingSelected
      || state.selectedTerms.has(checkbox.dataset.selectTerm);
    checkbox.checked = selected;
    checkbox.disabled = state.allMatchingSelected;
    row.classList.toggle("is-selected", selected);
  }
}

function renderItems() {
  const list = byId("highlight-list");
  if (!state.items.length) {
    list.innerHTML = '<p class="highlight-empty">没有符合条件的词头。</p>';
  } else {
    list.innerHTML = state.items.map((item) => {
      const selected = state.allMatchingSelected || state.selectedTerms.has(item.term);
      const categories = item.categories.length
        ? item.categories.map((category) =>
          `<span class="highlight-category-chip">${escapeHtml(category)}</span>`).join("")
        : '<span class="highlight-no-category">未分类</span>';
      const statusText = item.included ? "会发送" : "不发送";
      return `
        <article class="highlight-item">
          <label class="highlight-select">
            <input type="checkbox" data-select-term="${escapeHtml(item.term)}"
              aria-label="选择词头：${escapeHtml(item.term)}"
              ${selected ? "checked" : ""} ${state.allMatchingSelected ? "disabled" : ""}>
          </label>
          <div class="highlight-item-term">${escapeHtml(item.term)}</div>
          <div class="highlight-item-categories">${categories}</div>
          <button class="highlight-status ${item.included ? "is-included" : "is-excluded"}"
            type="button" data-toggle-term="${escapeHtml(item.term)}"
            aria-label="${item.included ? "设为不发送" : "设为发送"}：${escapeHtml(item.term)}">
            ${statusText}
          </button>
        </article>`;
    }).join("");
  }
  byId("highlight-result-count").textContent = `共 ${state.total} 个词头`;
  byId("page-label").textContent = `第 ${state.page} / ${state.pages} 页`;
  byId("previous-page").disabled = state.page <= 1 || state.busy;
  byId("next-page").disabled = state.page >= state.pages || state.busy;
  byId("included-count").textContent = state.includedCount;
  byId("excluded-count").textContent = state.excludedCount;
  renderSelection();
}

async function refreshHighlights() {
  const requestId = ++state.requestId;
  const params = new URLSearchParams({
    ...filters(),
    page: String(state.page),
    page_size: String(state.pageSize),
  });
  byId("highlight-status-message").textContent = "正在读取高光词头…";
  try {
    const data = await api(`/api/highlights?${params}`);
    if (requestId !== state.requestId) return null;
    if (state.page > data.pages) {
      state.page = data.pages;
      return refreshHighlights();
    }
    state.items = data.items;
    state.categories = data.categories;
    state.total = data.total;
    state.pages = data.pages;
    state.includedCount = data.included_count;
    state.excludedCount = data.excluded_count;
    renderCategories();
    if (
      byId("highlight-category-filter").value === ""
      && params.get("category")
      && !state.categories.includes(params.get("category"))
    ) {
      return refreshHighlights();
    }
    renderItems();
    byId("highlight-status-message").textContent = "";
    return true;
  } catch (error) {
    if (requestId !== state.requestId) return null;
    state.items = [];
    state.total = 0;
    state.pages = 1;
    state.includedCount = 0;
    state.excludedCount = 0;
    state.categories = [];
    renderCategories();
    renderItems();
    byId("highlight-list").innerHTML =
      `<p class="highlight-empty highlight-error">${escapeHtml(error.message)}</p>`;
    byId("highlight-status-message").textContent = error.message;
    return false;
  }
}

function selectionPayload() {
  if (state.allMatchingSelected) {
    return { all_matching: true, ...filters() };
  }
  return { terms: [...state.selectedTerms] };
}

function selectRange(anchorTerm, targetTerm) {
  const checkboxes = [...byId("highlight-list").querySelectorAll("[data-select-term]")];
  const anchorIndex = checkboxes.findIndex(
    (checkbox) => checkbox.dataset.selectTerm === anchorTerm,
  );
  const targetIndex = checkboxes.findIndex(
    (checkbox) => checkbox.dataset.selectTerm === targetTerm,
  );
  if (anchorIndex < 0 || targetIndex < 0) return false;
  const start = Math.min(anchorIndex, targetIndex);
  const end = Math.max(anchorIndex, targetIndex);
  state.selectedTerms.clear();
  state.allMatchingSelected = false;
  checkboxes.forEach((checkbox, index) => {
    checkbox.checked = index >= start && index <= end;
    if (checkbox.checked) state.selectedTerms.add(checkbox.dataset.selectTerm);
  });
  renderSelection();
  return true;
}

function updateDragSelection(event) {
  const drag = state.dragSelection;
  if (!drag || event.pointerId !== drag.pointerId) return;
  if (
    !drag.moved
    && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4
  ) return;
  drag.moved = true;

  const left = Math.min(drag.startX, event.clientX);
  const top = Math.min(drag.startY, event.clientY);
  const right = Math.max(drag.startX, event.clientX);
  const bottom = Math.max(drag.startY, event.clientY);
  const rectangle = byId("selection-rectangle");
  rectangle.hidden = false;
  Object.assign(rectangle.style, {
    left: `${left}px`,
    top: `${top}px`,
    width: `${right - left}px`,
    height: `${bottom - top}px`,
  });

  state.selectedTerms.clear();
  state.allMatchingSelected = false;
  const rows = [...byId("highlight-list").querySelectorAll(".highlight-item")];
  for (const row of rows) {
    const bounds = row.getBoundingClientRect();
    const overlaps = bounds.left < right
      && bounds.right > left
      && bounds.top < bottom
      && bounds.bottom > top;
    const checkbox = row.querySelector("[data-select-term]");
    checkbox.checked = overlaps;
    row.classList.toggle("is-selected", overlaps);
    if (overlaps) state.selectedTerms.add(checkbox.dataset.selectTerm);
  }
  state.anchorTerm = drag.anchorTerm
    || rows.find((row) => row.classList.contains("is-selected"))
      ?.querySelector("[data-select-term]")?.dataset.selectTerm
    || null;
  renderSelection();
}

function finishDragSelection(event) {
  if (!state.dragSelection || event.pointerId !== state.dragSelection.pointerId) return;
  const didMove = state.dragSelection.moved;
  state.dragSelection = null;
  byId("selection-rectangle").hidden = true;
  document.body.classList.remove("is-range-selecting");
  renderSelection();
  if (didMove) {
    state.suppressClickAfterDrag = true;
    setTimeout(() => {
      state.suppressClickAfterDrag = false;
    }, 0);
  }
}

async function applyBatch(operation) {
  const hasExplicitTerms = Array.isArray(operation.terms) && operation.terms.length > 0;
  if (
    state.busy
    || (!state.allMatchingSelected && !state.selectedTerms.size && !hasExplicitTerms)
  ) return;
  const selection = Array.isArray(operation.terms)
    ? { terms: operation.terms }
    : selectionPayload();
  const payload = { ...selection, ...operation };
  state.busy = true;
  renderSelection();
  byId("highlight-status-message").textContent = "正在批量更新…";
  try {
    const result = await api("/api/highlights", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    resetSelection();
    const refreshed = await refreshHighlights();
    if (refreshed !== null) {
      byId("highlight-status-message").textContent = refreshed
        ? `已处理 ${result.selected} 个词头，其中 ${result.changed} 个设置发生变化。`
        : `已保存 ${result.changed} 项规则，但列表刷新失败，请点击“刷新”重试。`;
    }
  } catch (error) {
    byId("highlight-status-message").textContent = error.message;
  } finally {
    state.busy = false;
    renderItems();
  }
}

function setFiltersChanged() {
  state.page = 1;
  resetSelection();
  refreshHighlights();
}

function wireEvents() {
  byId("highlight-search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(setFiltersChanged, 180);
  });
  byId("highlight-status-filter").addEventListener("change", setFiltersChanged);
  byId("highlight-category-filter").addEventListener("change", setFiltersChanged);
  byId("refresh-highlights").addEventListener("click", refreshHighlights);
  byId("previous-page").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      refreshHighlights();
    }
  });
  byId("next-page").addEventListener("click", () => {
    if (state.page < state.pages) {
      state.page += 1;
      refreshHighlights();
    }
  });
  byId("select-page").addEventListener("click", () => {
    const newTerms = state.items.filter(
      (item) => !state.selectedTerms.has(item.term),
    );
    if (state.selectedTerms.size + newTerms.length > 5000) {
      byId("highlight-status-message").textContent =
        "手动选择一次最多 5000 个词头；大批量操作请使用“选择全部筛选结果”。";
      return;
    }
    for (const item of state.items) state.selectedTerms.add(item.term);
    state.anchorTerm = state.items[0]?.term ?? null;
    renderSelection();
    renderItems();
  });
  byId("select-all-matching").addEventListener("click", () => {
    state.selectedTerms.clear();
    state.allMatchingSelected = true;
    renderSelection();
    renderItems();
  });
  byId("clear-selection").addEventListener("click", resetSelection);
  byId("include-selected").addEventListener("click", () =>
    applyBatch({ included: true }));
  byId("exclude-selected").addEventListener("click", () =>
    applyBatch({ included: false }));
  byId("add-category").addEventListener("click", () => {
    const categoryName = byId("highlight-category-name").value.trim();
    if (!categoryName) {
      byId("highlight-status-message").textContent = "请先输入分类名称。";
      return;
    }
    applyBatch({ category_name: categoryName, category_action: "add" });
  });
  byId("remove-category").addEventListener("click", () => {
    const categoryName = byId("highlight-category-name").value.trim();
    if (!categoryName) {
      byId("highlight-status-message").textContent = "请先输入要移除的分类名称。";
      return;
    }
    applyBatch({ category_name: categoryName, category_action: "remove" });
  });
  byId("highlight-list").addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-select-term]");
    if (!checkbox) return;
    const term = checkbox.dataset.selectTerm;
    const shiftAnchor = state.pendingShiftTerm === term ? state.anchorTerm : null;
    state.pendingShiftTerm = null;
    if (shiftAnchor && selectRange(shiftAnchor, term)) return;
    if (
      checkbox.checked
      && !state.selectedTerms.has(term)
      && state.selectedTerms.size >= 5000
    ) {
      checkbox.checked = false;
      byId("highlight-status-message").textContent =
        "手动选择一次最多 5000 个词头；大批量操作请使用“选择全部筛选结果”。";
      return;
    }
    if (checkbox.checked) state.selectedTerms.add(term);
    else state.selectedTerms.delete(term);
    state.anchorTerm = term;
    renderSelection();
  });
  byId("highlight-list").addEventListener("click", (event) => {
    const checkbox = event.target.closest("[data-select-term]");
    if (checkbox) {
      state.pendingShiftTerm = event.shiftKey
        ? checkbox.dataset.selectTerm
        : null;
      return;
    }
    state.pendingShiftTerm = null;
    const button = event.target.closest("[data-toggle-term]");
    if (!button) return;
    const item = state.items.find((candidate) => candidate.term === button.dataset.toggleTerm);
    if (item) applyBatch({ terms: [item.term], included: !item.included });
  });
  byId("highlight-list").addEventListener("click", (event) => {
    if (!state.suppressClickAfterDrag) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  byId("highlight-list").addEventListener("pointerdown", (event) => {
    if (state.busy || event.button !== 0 || event.pointerType !== "mouse") return;
    if (event.target.closest("input, button, a, select, textarea")) return;
    const row = event.target.closest(".highlight-item");
    const rowCheckbox = row?.querySelector("[data-select-term]");
    state.dragSelection = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      anchorTerm: rowCheckbox?.dataset.selectTerm ?? null,
      moved: false,
    };
    document.body.classList.add("is-range-selecting");
    event.preventDefault();
  });
  document.addEventListener("pointermove", updateDragSelection);
  document.addEventListener("pointerup", finishDragSelection);
  document.addEventListener("pointercancel", finishDragSelection);
}

wireEvents();
refreshHighlights();