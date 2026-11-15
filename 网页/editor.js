"use strict";

const form = document.querySelector(".editor");
const editor = document.getElementById("body-editor");
const sourceEditor = document.getElementById("body-source");
const bodyField = document.getElementById("body");
const groupField = document.getElementById("group");
const groupOptions = document.getElementById("group-options");
const noteIdField = form.querySelector('input[name="note_id"]');
const status = document.getElementById("editor-status");
const linkField = document.getElementById("link-url");
const sourceToggle = document.getElementById("toggle-html-source");
const sourceModeTools = document.querySelectorAll(".source-tools");
const toolbar = document.querySelector(".toolbar");
const toolGroups = [...toolbar.querySelectorAll(".tool-group")];
const pasteModeField = document.getElementById("paste-mode");
const localizeImagesField = document.getElementById("localize-pasted-images");
const deleteSourceButton = document.getElementById("delete-current-source");
const cleanSourceButton = document.getElementById("clean-current-source");
const actionButtons = document.querySelector(".actions");
const deletedImageAssetsField = document.getElementById("deleted-image-assets");
const pendingImageAssetDeletions = new Map();
const pasteModes = new Set(["nh3", "klembord", "sanitizer"]);
const pasteModeStorageKey = "dictTangoPasteMode";
const localizeImagesStorageKey = "dictTangoLocalizePastedImages";
const groupStorageKey = "dictTangoLastNoteGroup";
let pastePreferenceWarning = "";
let groupPreferenceWarning = "";
let visualDirty = false;
let editorMode = "visual";
const visualLineBreak = "<br>\n";

try {
  const savedPasteMode = localStorage.getItem(pasteModeStorageKey);
  if (pasteModes.has(savedPasteMode)) pasteModeField.value = savedPasteMode;
  const savedLocalization = localStorage.getItem(localizeImagesStorageKey);
  if (savedLocalization === "false" && localizeImagesField) {
    localizeImagesField.checked = false;
  }
} catch (error) {
  pastePreferenceWarning = `无法读取粘贴设置：${error.message}`;
}

function findGroupOption(value) {
  const normalized = value.trim().toLowerCase();
  return [...groupOptions.options].find(
    (option) => option.value.trim().toLowerCase() === normalized,
  )?.value || "";
}

function rememberGroupSelection() {
  const selectedGroup = groupField.value.trim();
  if (!selectedGroup) return;
  try {
    localStorage.setItem(groupStorageKey, selectedGroup);
    groupPreferenceWarning = "";
  } catch (error) {
    groupPreferenceWarning = `无法记忆上次分组：${error.message}`;
  }
}

try {
  const rememberedGroup = localStorage.getItem(groupStorageKey);
  if (!noteIdField.value && groupField.value === "[Default]" && rememberedGroup) {
    groupField.value = findGroupOption(rememberedGroup) || groupField.value;
  }
  rememberGroupSelection();
} catch (error) {
  groupPreferenceWarning = `无法读取上次分组：${error.message}`;
}

groupField.addEventListener("change", () => {
  rememberGroupSelection();
  updateStatus();
});

pasteModeField.addEventListener("change", () => {
  try {
    localStorage.setItem(pasteModeStorageKey, pasteModeField.value);
    pastePreferenceWarning = "";
  } catch (error) {
    pastePreferenceWarning = `无法保存粘贴模式设置：${error.message}`;
  }
  updateStatus();
});

localizeImagesField?.addEventListener("change", () => {
  try {
    localStorage.setItem(localizeImagesStorageKey, String(localizeImagesField.checked));
    pastePreferenceWarning = "";
  } catch (error) {
    pastePreferenceWarning = `无法保存图片本地化设置：${error.message}`;
  }
  updateStatus();
});

function positionToolPanel(group) {
  const panel = group.querySelector(".tool-panel");
  group.removeAttribute("data-panel-placement");
  panel.style.left = "";
  panel.style.top = "";
  panel.style.bottom = "";
  panel.style.maxHeight = "";

  const toolbarRect = toolbar.getBoundingClientRect();
  const groupRect = group.getBoundingClientRect();
  const panelWidth = panel.getBoundingClientRect().width;
  const maxLeft = Math.max(0, toolbar.clientWidth - panelWidth);
  const groupLeft = groupRect.left - toolbarRect.left;
  panel.style.left = `${Math.max(0, Math.min(groupLeft, maxLeft))}px`;

  const panelHeight = panel.getBoundingClientRect().height;
  const belowSpace = window.innerHeight - groupRect.bottom - 14;
  const aboveSpace = groupRect.top - 14;
  const openAbove = panelHeight > belowSpace && aboveSpace > belowSpace;
  group.dataset.panelPlacement = openAbove ? "above" : "below";
  if (openAbove) {
    panel.style.bottom = `${toolbarRect.bottom - groupRect.top + 6}px`;
  } else {
    panel.style.top = `${groupRect.bottom - toolbarRect.top + 6}px`;
  }
  panel.style.maxHeight = `${Math.max(
    0,
    Math.min(window.innerHeight - 16, openAbove ? aboveSpace : belowSpace),
  )}px`;
}

toolGroups.forEach((group) => {
  group.addEventListener("toggle", () => {
    if (!group.open) {
      group.removeAttribute("data-panel-placement");
      const panel = group.querySelector(".tool-panel");
      panel.style.left = "";
      panel.style.top = "";
      panel.style.bottom = "";
      panel.style.maxHeight = "";
      return;
    }
    toolGroups.forEach((other) => {
      if (other !== group) other.open = false;
    });
    positionToolPanel(group);
  });
});

window.addEventListener("resize", () => {
  toolGroups.filter((group) => group.open).forEach(positionToolPanel);
});

function constrainToFrameWidth() {
  const frameWidth = window.frameElement?.getBoundingClientRect().width;
  if (frameWidth) document.documentElement.style.maxWidth = `${frameWidth}px`;
}

constrainToFrameWidth();
window.addEventListener("resize", constrainToFrameWidth);

function safeUrlValue(value, resource = false) {
  const href = value.trim();
  if (/^https?:\/\/\S+$/i.test(href) || href.startsWith("/lookup?") || href.startsWith("/assets/") || href.startsWith("#")) {
    return href;
  }
  if (resource && (/^data:image\/(?:png|gif|jpe?g|webp|bmp);base64,/i.test(href) || (!/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith("//") && !href.split("/").includes("..")))) {
    return href;
  }
  return "";
}

function escapeVisualText(value) {
  return value
    .replace(/&/g, "&#38;")
    .replace(/</g, "&#60;")
    .replace(/>/g, "&#62;");
}

function escapeVisualAttribute(value) {
  return escapeVisualText(value).replace(/"/g, "&quot;");
}

function serializePlainText(value) {
  return value
    .split(/\r\n|\r|\n/)
    .map(escapeVisualText)
    .join(visualLineBreak);
}

function htmlSourceToPlainText(source) {
  const parsed = new DOMParser().parseFromString(source, "text/html");
  parsed.querySelectorAll("script,style,iframe,object,svg,math,template").forEach((node) => {
    node.remove();
  });
  const blockTags = new Set([
    "blockquote", "dd", "div", "dl", "dt", "figcaption", "h1", "h2",
    "h3", "h4", "h5", "h6", "li", "ol", "p", "pre", "tr", "ul",
  ]);
  const parts = [];
  function appendNewline(force = false) {
    const last = parts[parts.length - 1];
    if (last) parts[parts.length - 1] = last.replace(/[ \t]+$/, "");
    if (force || !parts.length || !parts[parts.length - 1].endsWith("\n")) {
      parts.push("\n");
    }
  }
  function appendText(value, preserveWhitespace) {
    let text = value.replace(/\r\n?/g, "\n");
    if (!preserveWhitespace) text = text.replace(/[ \t\n\f]+/g, " ");
    if (!preserveWhitespace && parts[parts.length - 1]?.endsWith("\n")) {
      text = text.replace(/^[ \t]+/, "");
    }
    if (text) parts.push(text);
  }
  function visit(node, preserveWhitespace = false) {
    if (node.nodeType === Node.TEXT_NODE) {
      appendText(node.nodeValue || "", preserveWhitespace);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    if (tag === "br") {
      appendNewline(true);
      return;
    }
    const isBlock = blockTags.has(tag);
    if (isBlock) appendNewline();
    if (tag === "img") {
      const alt = node.getAttribute("alt");
      if (alt) visit(document.createTextNode(alt), preserveWhitespace);
    }
    const children = [...node.childNodes];
    for (let index = 0; index < children.length; index += 1) {
      const child = children[index];
      const next = children[index + 1];
      if (
        child.nodeType === Node.ELEMENT_NODE
        && child.tagName.toLowerCase() === "br"
        && next?.nodeType === Node.TEXT_NODE
      ) {
        visit(child, preserveWhitespace || tag === "pre");
        appendText((next.nodeValue || "").replace(/^\r?\n/, ""), preserveWhitespace || tag === "pre");
        index += 1;
      } else {
        visit(child, preserveWhitespace || tag === "pre");
      }
    }
    if (isBlock) appendNewline();
  }
  for (const child of parsed.body.childNodes) visit(child);
  return parts.join("").replace(/^\n+|\n+$/g, "");
}

function updateStatus(bodyLength = editorMode === "source"
  ? sourceEditor.value.length
  : visualDirty
    ? editor.value.length
    : bodyField.value.length) {
  status.textContent = bodyLength > Number(bodyField.dataset.maxLength)
    ? "内容过长，请缩短后再保存。"
    : pastePreferenceWarning
      || groupPreferenceWarning
      || "每个内容换行只保存为一个 <br>；源码中的换行仅供阅读，不会额外显示。格式工具在源码模式使用。";
}

function syncBody() {
  if (editorMode === "source") bodyField.value = sourceEditor.value;
  else if (visualDirty) bodyField.value = serializePlainText(editor.value);
  updateStatus(bodyField.value.length);
}

sourceToggle.addEventListener("click", () => {
  if (editorMode === "visual") {
    if (visualDirty) {
      syncBody();
      sourceEditor.value = bodyField.value;
    }
    editor.hidden = true;
    sourceEditor.hidden = false;
    editorMode = "source";
    actionButtons.classList.add("source-mode");
    sourceModeTools.forEach((control) => { control.hidden = false; });
    deleteSourceButton.hidden = false;
    cleanSourceButton.hidden = false;
    sourceToggle.textContent = "返回可视化编辑";
    sourceToggle.setAttribute("aria-pressed", "true");
    sourceEditor.focus();
  } else {
    editor.value = htmlSourceToPlainText(sourceEditor.value);
    sourceEditor.hidden = true;
    editor.hidden = false;
    editorMode = "visual";
    actionButtons.classList.remove("source-mode");
    sourceModeTools.forEach((control) => { control.hidden = true; });
    deleteSourceButton.hidden = true;
    cleanSourceButton.hidden = true;
    visualDirty = true;
    sourceToggle.textContent = "编辑 HTML 源码";
    sourceToggle.setAttribute("aria-pressed", "false");
    editor.focus();
  }
  syncBody();
});

sourceEditor.addEventListener("input", syncBody);
editor.addEventListener("input", () => {
  visualDirty = true;
  updateStatus();
});

function localImageFilenames(source, group) {
  const parsed = new DOMParser().parseFromString(source, "text/html");
  const folder = group === "[Default]" ? "Default" : group;
  const assetPrefix = `/assets/${encodeURIComponent(folder)}/`;
  const filenames = new Set();
  for (const image of parsed.querySelectorAll("img[src]")) {
    const imageSource = image.getAttribute("src")?.trim() || "";
    const path = imageSource.split(/[?#]/, 1)[0];
    let filename = "";
    if (path.startsWith(assetPrefix)) {
      filename = path.slice(assetPrefix.length);
    } else if (
      path
      && !path.startsWith("/")
      && !path.startsWith("//")
      && !/^[a-z][a-z0-9+.-]*:/i.test(path)
    ) {
      filename = path;
    }
    if (!filename || filename.includes("/") || filename.includes("\\")) continue;
    try {
      filename = decodeURIComponent(filename);
    } catch (error) {
      if (error instanceof URIError) continue;
      throw error;
    }
    if (filename && filename !== "." && filename !== "..") filenames.add(filename);
  }
  return [...filenames];
}

function queueImageAssetCleanup(source, group) {
  const filenames = localImageFilenames(source, group);
  if (!filenames.length || !deletedImageAssetsField) return 0;
  const groupFilenames = pendingImageAssetDeletions.get(group) || new Set();
  filenames.forEach((filename) => groupFilenames.add(filename));
  pendingImageAssetDeletions.set(group, groupFilenames);
  deletedImageAssetsField.value = JSON.stringify(
    [...pendingImageAssetDeletions].flatMap(([assetGroup, assets]) =>
      [...assets].map((filename) => ({ group: assetGroup, filename })),
    ),
  );
  return filenames.length;
}

deleteSourceButton.addEventListener("click", () => {
  const group = document.getElementById("group").value.trim() || "[Default]";
  const imageCount = queueImageAssetCleanup(sourceEditor.value, group);
  sourceEditor.value = "";
  sourceEditor.dispatchEvent(new Event("input", { bubbles: true }));
  sourceEditor.focus();
  sourceEditor.setSelectionRange(0, 0);
  status.textContent = imageCount
    ? `源码输入框已清空；保存时会检查并清理不再被笔记引用的本地图片（识别到 ${imageCount} 张）。`
    : "源码输入框已清空。";
});

function insertAtSelection(target, text, cursorOffset) {
  const start = target.selectionStart;
  const end = target.selectionEnd;
  target.setRangeText(text, start, end, "end");
  if (cursorOffset !== undefined) {
    const cursor = start + cursorOffset;
    target.setSelectionRange(cursor, cursor);
  }
  target.focus();
  target.dispatchEvent(new Event("input", { bubbles: true }));
}

function wrapSourceSelection(openingTag, closingTag) {
  const start = sourceEditor.selectionStart;
  const end = sourceEditor.selectionEnd;
  const selected = sourceEditor.value.slice(start, end);
  insertAtSelection(
    sourceEditor,
    `${openingTag}${selected}${closingTag}`,
    selected ? undefined : openingTag.length,
  );
}

document.querySelectorAll("[data-command]").forEach((button) => {
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () => {
    const command = button.dataset.command;
    const inlineTags = {
      bold: "strong",
      italic: "em",
      underline: "u",
    };
    if (inlineTags[command]) {
      const tag = inlineTags[command];
      wrapSourceSelection(`<${tag}>`, `</${tag}>`);
      return;
    }
    if (command === "insertUnorderedList" || command === "insertOrderedList") {
      const tag = command === "insertUnorderedList" ? "ul" : "ol";
      const start = sourceEditor.selectionStart;
      const end = sourceEditor.selectionEnd;
      const selected = sourceEditor.value.slice(start, end);
      const items = (selected || "").split(/\r\n|\r|\n/)
        .map((line) => `<li>${line}</li>`)
        .join("\n");
      const opening = `<${tag}>\n`;
      insertAtSelection(
        sourceEditor,
        `${opening}${items}\n</${tag}>`,
        selected ? undefined : opening.length + "<li>".length,
      );
      return;
    }
    if (command === "formatBlock" && ["blockquote", "h2", "p"].includes(button.dataset.value)) {
      const tag = button.dataset.value;
      wrapSourceSelection(`<${tag}>`, `</${tag}>`);
    }
  });
});

document.getElementById("insert-link").addEventListener("click", () => {
  const href = linkField.value.trim();
  const safeHref = safeUrlValue(href);
  if (!safeHref) {
    status.textContent = "请输入 HTTP(S) 地址、本地词条链接或页内锚点。";
    linkField.focus();
    return;
  }
  const selected = sourceEditor.value.slice(
    sourceEditor.selectionStart,
    sourceEditor.selectionEnd,
  );
  if (!selected) {
    status.textContent = "请先在正文中选中要添加链接的文字。";
    sourceEditor.focus();
    return;
  }
  insertAtSelection(
    sourceEditor,
    `<a href="${escapeVisualAttribute(safeHref)}">${selected}</a>`,
  );
});

async function uploadAsset(file, kind) {
  if (file.size > 20_000_000) {
    status.textContent = "附件不能超过 20 MB。";
    return;
  }
  status.textContent = `正在上传 ${file.name}…`;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    const response = await fetch("/api/assets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group: document.getElementById("group").value.trim() || "[Default]",
        filename: file.name,
        content: btoa(binary),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `上传失败 (${response.status})`);
    const source = escapeVisualAttribute(result.filename);
    const markup = kind === "image"
      ? `<img src="${source}" alt="${escapeVisualAttribute(file.name)}">`
      : `<${kind} controls src="${source}"></${kind}>`;
    insertAtSelection(sourceEditor, markup);
    status.textContent = `已将附件保存到分组资源目录：${result.filename}`;
  } catch (error) {
    status.textContent = error.message || "附件上传失败。";
  }
}

function extractClipboardFragment(source) {
  const markedFragment = source.match(
    /<!--StartFragment-->([\s\S]*?)<!--EndFragment-->/i,
  );
  if (markedFragment) source = markedFragment[1];
  return new DOMParser().parseFromString(source, "text/html").body.innerHTML;
}

async function requestPasteCleaning(source, mode, group, localizeImages) {
  const response = await fetch("/api/paste", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ html: source, mode, group, localize_images: localizeImages }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `粘贴清理失败 (${response.status})`);
  if (typeof result.html !== "string") {
    throw new Error("粘贴清理服务返回了无效内容。");
  }
  if (!Array.isArray(result.warnings) || !result.warnings.every((warning) => typeof warning === "string")) {
    throw new Error("粘贴清理服务返回了无效警告信息。");
  }
  if (!Number.isInteger(result.localized_images) || result.localized_images < 0) {
    throw new Error("粘贴清理服务返回了无效图片处理结果。");
  }
  if (typeof result.localization_enabled !== "boolean") {
    throw new Error("粘贴清理服务返回了无效图片本地化选项。");
  }
  return {
    html: result.html,
    warnings: result.warnings,
    localizedImages: result.localized_images,
    localizationEnabled: result.localization_enabled,
  };
}

function pasteResultDetails(result) {
  const localizedMessage = result.localizedImages
    ? `已本地化 ${result.localizedImages} 张图片。`
    : "";
  const warningMessage = result.warnings.length
    ? result.warnings.join(" ")
    : "";
  const disabledMessage = result.localizationEnabled
    ? ""
    : "图片自动本地化已关闭。";
  return [localizedMessage, warningMessage, disabledMessage].filter(Boolean).join(" ");
}

async function handleHtmlPaste(event) {
  if (editorMode !== "source" || event.defaultPrevented) return;
  const clipboard = event.clipboardData;
  const htmlSource = clipboard?.getData("text/html");
  const source = htmlSource || clipboard?.getData("text/plain");
  if (!source) return;

  event.preventDefault();
  const sourceSnapshot = sourceEditor.value;
  const start = sourceEditor.selectionStart;
  const end = sourceEditor.selectionEnd;
  const mode = pasteModeField.value;
  const localizeImages = localizeImagesField?.checked ?? true;
  const group = document.getElementById("group").value.trim() || "[Default]";
  try {
    const result = await requestPasteCleaning(
      htmlSource ? extractClipboardFragment(source) : source,
      mode,
      group,
      localizeImages,
    );
    if (sourceEditor.value !== sourceSnapshot) {
      status.textContent = "清理剪贴板期间源码已变化，未插入内容；请重新粘贴。";
      return;
    }
    if (!result.html) {
      status.textContent = "剪贴内容经清理后为空，正文未更改。";
      return;
    }
    sourceEditor.setRangeText(result.html, start, end, "end");
    sourceEditor.dispatchEvent(new Event("input", { bubbles: true }));
    status.textContent = [
      `已按“${pasteModeField.selectedOptions[0].textContent}”模式粘贴并清理 HTML。`,
      pasteResultDetails(result),
    ].filter(Boolean).join(" ");
  } catch (error) {
    status.textContent = `剪贴内容清理失败：${error.message || error}`;
  }
}

cleanSourceButton.addEventListener("click", async () => {
  const sourceSnapshot = sourceEditor.value;
  const mode = pasteModeField.value;
  const localizeImages = localizeImagesField?.checked ?? true;
  const group = document.getElementById("group").value.trim() || "[Default]";
  cleanSourceButton.disabled = true;
  try {
    const result = await requestPasteCleaning(sourceSnapshot, mode, group, localizeImages);
    if (sourceEditor.value !== sourceSnapshot) {
      status.textContent = "清理期间源码已变化，未覆盖编辑内容。";
      return;
    }
    sourceEditor.value = result.html;
    sourceEditor.dispatchEvent(new Event("input", { bubbles: true }));
    status.textContent = result.html
      ? [
        `已按“${pasteModeField.selectedOptions[0].textContent}”模式整理代码。`,
        pasteResultDetails(result),
      ].filter(Boolean).join(" ")
      : "清理结果为空，当前源码已清空。";
  } catch (error) {
    status.textContent = `当前源码清理失败：${error.message || error}`;
  } finally {
    cleanSourceButton.disabled = false;
  }
});

async function handleImagePaste(event) {
  if (event.defaultPrevented) return;
  const image = [...(event.clipboardData?.items || [])]
    .find((item) => item.kind === "file" && item.type.startsWith("image/"))
    ?.getAsFile();
  if (!image) return;
  event.preventDefault();
  if (editorMode === "visual") {
    sourceToggle.click();
    const end = sourceEditor.value.length;
    sourceEditor.setSelectionRange(end, end);
  }
  await uploadAsset(image, "image");
}

editor.addEventListener("paste", handleImagePaste);
sourceEditor.addEventListener("paste", handleHtmlPaste);
sourceEditor.addEventListener("paste", handleImagePaste);

document.querySelectorAll("[data-upload-kind]").forEach((input) => {
  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (file) await uploadAsset(file, input.dataset.uploadKind);
    input.value = "";
  });
});

function replaceText(replaceAll) {
  const search = document.getElementById("find-text").value;
  if (!search) {
    status.textContent = "请输入要查找的内容。";
    return;
  }
  const replacement = document.getElementById("replace-text").value;
  const target = editorMode === "source" ? sourceEditor : editor;
  let replacements = 0;
  if (replaceAll) {
    const parts = target.value.split(search);
    replacements = parts.length - 1;
    if (replacements) target.value = parts.join(replacement);
  } else {
    const start = target.selectionStart;
    let index = target.value.indexOf(search, start);
    if (index < 0) index = target.value.indexOf(search);
    if (index >= 0) {
      target.setRangeText(replacement, index, index + search.length, "end");
      replacements = 1;
    }
  }
  if (replacements) {
    target.dispatchEvent(new Event("input", { bubbles: true }));
  }
  status.textContent = replacements ? `已替换 ${replacements} 处。` : "没有找到匹配内容。";
}

document.getElementById("replace-next").addEventListener("click", () => replaceText(false));
document.getElementById("replace-all").addEventListener("click", () => replaceText(true));

form.addEventListener("submit", (event) => {
  const maxLength = Number(bodyField.dataset.maxLength);
  if (editorMode === "visual" && editor.value.length > maxLength) {
    event.preventDefault();
    status.textContent = "内容超过长度限制，请缩短后再保存。";
    editor.focus();
    return;
  }
  syncBody();
  if (bodyField.value.length > maxLength) {
    event.preventDefault();
    status.textContent = "内容超过长度限制，请缩短后再保存。";
    (editorMode === "source" ? sourceEditor : editor).focus();
    return;
  }
  const submitButton = form.querySelector('button[type="submit"]');
  rememberGroupSelection();
  if (submitButton) submitButton.disabled = true;
  status.textContent = "正在保存笔记…";
});

syncBody();
