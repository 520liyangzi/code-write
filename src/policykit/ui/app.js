"use strict";

const API = Object.freeze({
  status: "/api/status",
  review: "/api/review",
  rawReview: "/api/review/raw",
  importDocuments: "/api/documents/import",
  prepare: "/api/prepare",
  decision: "/api/review/decision",
  activate: "/api/activate",
  search: "/api/search",
});

const DECISIONS = Object.freeze({
  approved: "批准并启用",
  modified: "修改后接受",
  rejected: "拒绝",
  pending_review: "暂缓处理",
});

const state = {
  status: {},
  rules: [],
  rawReview: "",
  files: [],
  drafts: new Map(),
  loadingCount: 0,
  searchReady: false,
  searchHasRun: false,
};

class ApiRequestError extends Error {
  constructor(message, status = 0, code = "", details = null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const dom = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheDom();
  bindEvents();
  renderSkeletons();
  refreshAll();
});

function cacheDom() {
  const ids = [
    "connectionPill", "connectionText", "refreshButton", "pageError", "pageErrorText",
    "dismissError", "statCandidates", "statCandidatesHint", "statApproved", "statPending",
    "statVersion", "statIndexHint", "documentScope", "dropZone", "fileInput",
    "chooseFilesButton", "fileQueue", "fileQueueEmpty", "fileSummary", "importButton",
    "prepareButton", "ruleSearch", "decisionFilter", "severityFilter", "scopeFilter",
    "categoryFilter", "clearFiltersButton", "ruleCountText", "ruleList", "activationApproved",
    "activationPending", "activationRejected", "activationForm", "policyVersion", "activateButton", "searchForm",
    "searchQuery", "searchFile", "searchCode", "searchScopes", "searchCategories",
    "searchLimit", "searchButton", "searchResultCount", "searchIndexBadge", "searchResults",
    "reloadRawButton", "copyRawButton", "rawReviewText", "lastUpdated", "loadingRail",
    "toastRegion",
  ];
  ids.forEach((id) => { dom[id] = document.getElementById(id); });
}

function bindEvents() {
  dom.refreshButton.addEventListener("click", refreshAll);
  dom.dismissError.addEventListener("click", hidePageError);
  dom.chooseFilesButton.addEventListener("click", (event) => {
    event.stopPropagation();
    dom.fileInput.click();
  });
  dom.dropZone.addEventListener("click", (event) => {
    if (event.target !== dom.chooseFilesButton) dom.fileInput.click();
  });
  dom.dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      dom.fileInput.click();
    }
  });
  dom.fileInput.addEventListener("change", () => addFiles(dom.fileInput.files));

  ["dragenter", "dragover"].forEach((name) => {
    dom.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dom.dropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    dom.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      dom.dropZone.classList.remove("is-dragging");
    });
  });
  dom.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  dom.importButton.addEventListener("click", importDocuments);
  dom.prepareButton.addEventListener("click", prepareCandidates);

  [dom.ruleSearch, dom.decisionFilter, dom.severityFilter, dom.scopeFilter, dom.categoryFilter]
    .forEach((element) => element.addEventListener("input", renderRules));
  dom.clearFiltersButton.addEventListener("click", clearRuleFilters);

  dom.activationForm.addEventListener("submit", activateRules);
  dom.searchForm.addEventListener("submit", runSearch);
  dom.reloadRawButton.addEventListener("click", loadRawReview);
  dom.copyRawButton.addEventListener("click", copyRawReview);

  document.querySelectorAll("[data-nav]").forEach((link) => {
    link.addEventListener("click", () => setActiveNav(link.dataset.nav));
  });

  const observedSections = document.querySelectorAll("main section[id]");
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible) setActiveNav(visible.target.id);
    }, { rootMargin: "-22% 0px -66% 0px", threshold: [0.05, 0.25] });
    observedSections.forEach((section) => observer.observe(section));
  }
}

async function refreshAll() {
  hidePageError();
  setButtonBusy(dom.refreshButton, true);
  try {
    const [statusResult, reviewResult, rawResult] = await Promise.allSettled([
      request(API.status),
      request(API.review),
      requestRaw(API.rawReview),
    ]);

    if (statusResult.status === "fulfilled") {
      state.status = objectOrEmpty(statusResult.value);
      renderStatus();
      setConnection(true);
    } else {
      setConnection(false);
      throw statusResult.reason;
    }

    if (reviewResult.status === "fulfilled") {
      state.rules = normalizeRules(reviewResult.value);
      initializeDrafts();
      renderRules();
      renderActivationSummary();
    } else {
      renderRulesError(reviewResult.reason);
    }

    if (rawResult.status === "fulfilled") {
      state.rawReview = rawResult.value;
      renderRawReview();
    } else {
      state.rawReview = "无法读取 REVIEW_ME：" + errorMessage(rawResult.reason);
      renderRawReview();
    }
    dom.lastUpdated.textContent = "最近同步：" + new Date().toLocaleString("zh-CN");
  } catch (error) {
    showPageError(errorMessage(error));
  } finally {
    setButtonBusy(dom.refreshButton, false);
  }
}

async function loadStatus() {
  state.status = objectOrEmpty(await request(API.status));
  renderStatus();
  setConnection(true);
}

async function loadReview() {
  state.rules = normalizeRules(await request(API.review));
  initializeDrafts();
  renderRules();
  renderActivationSummary();
}

async function loadRawReview() {
  setButtonBusy(dom.reloadRawButton, true);
  try {
    state.rawReview = await requestRaw(API.rawReview);
    renderRawReview();
    toast("已刷新", "REVIEW_ME 原始内容已重新读取。", "success");
  } catch (error) {
    showPageError(errorMessage(error));
  } finally {
    setButtonBusy(dom.reloadRawButton, false);
  }
}

function renderStatus() {
  const status = state.status;
  const counts = objectOrEmpty(
    status.counts || status.decision_counts || status.review_counts || status.stats || status.summary,
  );
  const reviewCounts = countDecisions(state.rules);

  const candidates = firstNumber(
    status.candidate_count, status.candidates_count, status.candidates,
    counts.candidates, counts.candidate_count, state.rules.length,
  );
  const approved = firstNumber(
    status.approved_count, status.active_rule_count, status.approved_rules,
    counts.approved, counts.active, reviewCounts.approved,
  );
  const pending = firstNumber(
    status.pending_count, status.pending_review_count,
    counts.pending, counts.pending_review, reviewCounts.pending_review,
  );
  const version = firstText(
    status.policy_version, status.activated_version, status.version,
    status.activation && status.activation.policy_version,
    status.active_policy && status.active_policy.version,
    "未激活",
  );
  const indexReady = firstBoolean(
    status.index_ready, status.search_index_ready, status.index_exists,
    status.activated, status.active,
    status.search_index && status.search_index.ready,
  );
  const explicitlyActive = firstBoolean(status.activated, status.active, status.policy_active);
  const indexError = firstText(status.index_error);
  state.searchReady = explicitlyActive === true
    || (explicitlyActive === null && indexReady === true && version !== "未激活");

  dom.statCandidates.textContent = formatCount(candidates);
  dom.statApproved.textContent = formatCount(approved);
  dom.statPending.textContent = formatCount(pending);
  dom.statVersion.textContent = version;
  dom.statVersion.title = version;
  dom.statCandidatesHint.textContent = state.rules.length
    ? "当前审阅视图载入 " + state.rules.length + " 条"
    : "尚未生成候选规则";
  dom.statIndexHint.textContent = state.searchReady
    ? "已激活正式索引可用"
    : indexError ? "索引不可用：" + indexError
      : indexReady === false ? "检索索引尚未生成" : "索引状态未知";
  dom.searchIndexBadge.textContent = state.searchReady ? "已激活正式索引" : "正式索引未激活";
  dom.searchIndexBadge.classList.toggle("is-ready", state.searchReady);
  dom.searchButton.dataset.locked = String(!state.searchReady);
  dom.searchButton.disabled = !state.searchReady;
  dom.searchButton.title = state.searchReady ? "查询已激活正式索引" : "请先批准并激活规则库";
  if (!state.searchReady && !state.searchHasRun) {
    dom.searchResultCount.textContent = "等待激活正式索引";
    dom.searchResults.replaceChildren(emptyState(
      indexError ? "正式索引与规则包不一致" : "正式索引尚未激活",
      indexError
        ? "请重新激活规则库；在 bundle_id 恢复一致前检索会保持关闭。"
        : "先完成候选审阅并激活规则库，检索沙盒才会开放。",
      "◇",
    ));
  }
  if (!dom.policyVersion.value && version !== "未激活") dom.policyVersion.value = version;
}

function renderSkeletons() {
  dom.ruleList.replaceChildren(...Array.from({ length: 3 }, () => element("div", "skeleton-card")));
}

function addFiles(fileList) {
  const incoming = Array.from(fileList || []);
  let rejected = 0;
  incoming.forEach((file) => {
    const lowerName = file.name.toLowerCase();
    const markdown = lowerName.endsWith(".md") || lowerName.endsWith(".markdown");
    if (!markdown) {
      rejected += 1;
      return;
    }
    const key = file.name + ":" + file.size + ":" + file.lastModified;
    if (!state.files.some((item) => item.key === key)) state.files.push({ key, file });
  });
  dom.fileInput.value = "";
  renderFileQueue();
  if (rejected) toast("已忽略非 Markdown 文件", rejected + " 个文件未加入队列。", "warning");
}

function renderFileQueue() {
  dom.fileQueue.replaceChildren();
  if (!state.files.length) {
    const empty = element("div", "empty-inline", "尚未选择文件");
    dom.fileQueue.append(empty);
  } else {
    state.files.forEach((entry) => {
      const row = element("div", "queued-file");
      row.append(element("span", "file-type", "MD"));
      row.append(element("span", "", entry.file.name));
      row.append(element("small", "", formatBytes(entry.file.size)));
      const remove = element("button", "icon-button", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", "移除 " + entry.file.name);
      remove.addEventListener("click", () => {
        state.files = state.files.filter((item) => item.key !== entry.key);
        renderFileQueue();
      });
      row.append(remove);
      dom.fileQueue.append(row);
    });
  }
  dom.fileSummary.textContent = state.files.length + " 个文件待导入";
  dom.importButton.disabled = state.files.length === 0;
}

async function importDocuments() {
  if (!state.files.length) return;
  setButtonBusy(dom.importButton, true);
  try {
    const files = await Promise.all(state.files.map(async (entry) => ({
      name: entry.file.name,
      content: await entry.file.text(),
    })));
    await request(API.importDocuments, {
      method: "POST",
      body: { scope: dom.documentScope.value, files },
    });
    const count = state.files.length;
    state.files = [];
    renderFileQueue();
    toast("文档已导入", count + " 个 Markdown 文件已写入本地规范源。", "success");
    await loadStatus();
  } catch (error) {
    const duplicate = error.status === 409;
    const message = duplicate
      ? "检测到同名规范文件；为避免覆盖，导入已拒绝。请重命名文件或先人工处理原文件。"
      : errorMessage(error);
    showPageError(message);
    toast(duplicate ? "存在同名文件" : "导入失败", message, "error");
  } finally {
    setButtonBusy(dom.importButton, false);
  }
}

async function prepareCandidates() {
  const counts = countDecisionsFromDrafts();
  const resettable = Array.from(state.drafts.values()).filter((draft) =>
    draft.decision !== "pending_review"
      || Boolean(draft.edited_statement.trim())
      || Boolean(draft.notes.trim())
      || draft.dirty,
  ).length;
  let confirmReset = false;
  if (resettable > 0) {
    const confirmed = window.confirm(
      "重新生成候选会重写 REVIEW_ME，并清空当前所有审批决定。\n\n"
      + "当前已有：批准/修改后接受 " + (counts.approved + counts.modified)
      + " 条，拒绝 " + counts.rejected + " 条；待处理备注或未保存草稿也会清除。\n\n"
      + "确定继续生成候选吗？",
    );
    if (!confirmed) return;
    confirmReset = true;
  }
  setButtonBusy(dom.prepareButton, true);
  try {
    try {
      await request(API.prepare, { method: "POST", body: { confirm_reset: confirmReset } });
    } catch (error) {
      if (error.status !== 409 || error.code !== "review_decisions_exist" || confirmReset) throw error;
      const serverCounts = objectOrEmpty(error.details && error.details.decision_counts);
      const confirmed = window.confirm(
        "服务端检测到已有审批决定。重新生成候选会重写 REVIEW_ME，并清空这些决定。\n\n"
        + "批准：" + firstNumber(serverCounts.approved, 0) + " 条；"
        + "修改后接受：" + firstNumber(serverCounts.modified, 0) + " 条；"
        + "拒绝：" + firstNumber(serverCounts.rejected, 0) + " 条。\n\n"
        + "确定继续吗？",
      );
      if (!confirmed) return;
      await request(API.prepare, { method: "POST", body: { confirm_reset: true } });
    }
    state.drafts = new Map();
    await Promise.all([loadStatus(), loadReview(), loadRawReview()]);
    toast("候选已生成", "请逐条审阅；此操作没有激活任何规则。", "success");
    document.getElementById("review").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showPageError(errorMessage(error));
    toast("生成失败", errorMessage(error), "error");
  } finally {
    setButtonBusy(dom.prepareButton, false);
  }
}

function initializeDrafts() {
  const next = new Map();
  state.rules.forEach((rule) => {
    const previous = state.drafts.get(rule.id);
    next.set(rule.id, previous && previous.dirty ? previous : {
      decision: normalizeDecision(rule),
      edited_statement: firstText(
        rule.edited_statement,
        rule.decision_data && rule.decision_data.edited_statement,
        "",
      ),
      notes: firstText(
        rule.notes, rule.reviewer_notes,
        rule.decision_data && rule.decision_data.notes,
        "",
      ),
      dirty: false,
    });
  });
  state.drafts = next;
}

function renderRules() {
  dom.ruleList.setAttribute("aria-busy", "false");
  const filtered = filteredRules();
  dom.ruleList.replaceChildren();
  dom.ruleCountText.textContent = "显示 " + filtered.length + " / " + state.rules.length + " 条候选规则";

  if (!state.rules.length) {
    dom.ruleList.append(emptyState("尚无候选规则", "先导入 Markdown，再点击“生成候选规则”。", "◇"));
    renderActivationSummary();
    renderStatus();
    return;
  }
  if (!filtered.length) {
    dom.ruleList.append(emptyState("没有符合筛选条件的规则", "清除部分筛选条件后重试。", "⌕"));
    return;
  }

  const fragment = document.createDocumentFragment();
  filtered.forEach((rule) => fragment.append(createRuleCard(rule)));
  dom.ruleList.append(fragment);
  renderActivationSummary();
  renderStatus();
}

function createRuleCard(rule) {
  const draft = state.drafts.get(rule.id) || {
    decision: "pending_review", edited_statement: "", notes: "", dirty: false,
  };
  const card = element("article", "rule-card");
  card.dataset.ruleId = rule.id;
  card.dataset.decision = draft.decision;

  const main = element("div", "rule-main");
  const head = element("div", "rule-head");
  const titleWrap = element("div");
  titleWrap.append(element("span", "rule-id", rule.id || "NO-ID"));
  titleWrap.append(element("h3", "rule-title", firstText(rule.title, rule.statement, "未命名规则")));
  const stateLabel = element("span", "decision-state", DECISIONS[draft.decision] || "待处理");
  head.append(titleWrap, stateLabel);
  main.append(head);

  const badges = element("div", "badges");
  const severity = normalizeToken(rule.severity, "unknown");
  badges.append(badge(severity.toUpperCase(), "badge-" + severity));
  badges.append(badge(scopeLabel(rule.scope)));
  badges.append(badge(firstText(rule.category, "未分类")));
  const confidence = normalizeConfidence(rule.confidence);
  if (confidence !== null) badges.append(badge("置信度 " + Math.round(confidence * 100) + "%"));
  checkerLabels(rule).forEach((label) => badges.append(badge(label, "badge-checker")));
  main.append(badges);

  main.append(element("p", "rule-statement", firstText(rule.statement, "候选规则正文为空")));
  main.append(createRuleDetails(rule));
  card.append(main, createReviewEditor(rule, draft, card, stateLabel));
  return card;
}

function createRuleDetails(rule) {
  const details = element("details", "rule-details");
  const summary = element("summary", "", "查看来源原文与 checker 详情");
  const grid = element("div", "detail-grid");
  const source = objectOrEmpty(rule.source);

  const sourceBox = element("div", "detail-box");
  sourceBox.append(element("h4", "", "来源原文"));
  sourceBox.append(element("p", "", firstText(source.quote, rule.source_quote, "没有可显示的原文摘录")));
  sourceBox.append(element("p", "source-meta", formatSource(source)));

  const checkerBox = element("div", "detail-box");
  checkerBox.append(element("h4", "", "执行候选 / checker"));
  const checker = extractChecker(rule);
  const checkerText = checker === null
    ? checkerLabels(rule).join(" · ") || "AI review（尚无确定性 checker）"
    : safeJson(checker);
  checkerBox.append(element("pre", "", checkerText));

  grid.append(sourceBox, checkerBox);
  details.append(summary, grid);
  return details;
}

function createReviewEditor(rule, draft, card, stateLabel) {
  const editor = element("div", "review-editor");
  const group = element("div", "decision-group");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "对 " + rule.id + " 作出决定");

  Object.entries(DECISIONS).forEach(([value, label]) => {
    const button = element("button", "decision-button", label);
    button.type = "button";
    button.dataset.decision = value;
    button.setAttribute("aria-pressed", String(draft.decision === value));
    button.addEventListener("click", () => {
      draft.decision = value;
      draft.dirty = true;
      card.dataset.decision = value;
      stateLabel.textContent = DECISIONS[value];
      group.querySelectorAll("button").forEach((item) => {
        item.setAttribute("aria-pressed", String(item.dataset.decision === value));
      });
      modifiedField.hidden = value !== "modified";
      saveState.textContent = "有未保存的决定";
      saveState.classList.add("is-dirty");
      renderActivationSummary();
    });
    group.append(button);
  });

  const fields = element("div", "editor-fields");
  const modifiedField = element("label", "field modified-field");
  modifiedField.hidden = draft.decision !== "modified";
  modifiedField.append(element("span", "", "修改后的完整规则正文"));
  const edited = document.createElement("textarea");
  edited.rows = 4;
  edited.value = draft.edited_statement;
  edited.placeholder = "选择“修改后接受”时必须填写完整规则正文";
  edited.addEventListener("input", () => {
    draft.edited_statement = edited.value;
    markDraftDirty(draft, saveState);
  });
  modifiedField.append(edited);

  const notesField = element("label", "field");
  notesField.append(element("span", "", "审阅备注（可选）"));
  const notes = document.createElement("textarea");
  notes.rows = 4;
  notes.value = draft.notes;
  notes.placeholder = "记录修改原因、适用边界或后续事项";
  notes.addEventListener("input", () => {
    draft.notes = notes.value;
    markDraftDirty(draft, saveState);
  });
  notesField.append(notes);
  fields.append(modifiedField, notesField);

  const actions = element("div", "review-actions");
  const saveState = element("span", "save-state", draft.dirty ? "有未保存的决定" : "决定已与服务端同步");
  if (draft.dirty) saveState.classList.add("is-dirty");
  const save = element("button", "button button-primary", "保存本条决定");
  save.type = "button";
  save.addEventListener("click", async () => {
    if (draft.decision === "modified" && !draft.edited_statement.trim()) {
      toast("缺少规则正文", "“修改后接受”必须填写完整规则正文。", "warning");
      edited.focus();
      return;
    }
    setButtonBusy(save, true);
    try {
      const saved = await request(API.decision, {
        method: "POST",
        body: {
          rule_id: rule.id,
          decision: draft.decision,
          edited_statement: draft.edited_statement,
          notes: draft.notes,
          review_hash: firstText(rule.review_hash, rule.decision_data && rule.decision_data.review_hash),
          decision_hash: firstText(rule.decision_hash, rule.decision_data && rule.decision_data.decision_hash),
        },
      });
      const savedRule = objectOrEmpty(saved && saved.rule);
      if (savedRule.decision_hash) rule.decision_hash = savedRule.decision_hash;
      if (savedRule.review_hash) rule.review_hash = savedRule.review_hash;
      rule.decision = draft.decision;
      draft.dirty = false;
      saveState.textContent = "决定已保存";
      saveState.classList.remove("is-dirty");
      toast("决定已保存", rule.id + " · " + DECISIONS[draft.decision], "success");
      await Promise.all([loadStatus(), loadRawReview()]);
    } catch (error) {
      const staleReview = error.status === 409 && error.code === "stale_review";
      const staleDecision = error.status === 409 && error.code === "stale_decision";
      const stale = staleReview || staleDecision;
      const message = staleDecision
        ? "该规则已在其他页面更新，请刷新后再做决定。"
        : (staleReview ? "候选已变化，请刷新后重新审阅。" : errorMessage(error));
      saveState.textContent = stale ? "内容已变化，需要刷新" : "保存失败，请重试";
      saveState.classList.add("is-dirty");
      toast(stale ? "候选已变化" : "保存失败", message, "error");
      if (stale) showPageError(message);
    } finally {
      setButtonBusy(save, false);
    }
  });
  actions.append(saveState, save);
  editor.append(group, fields, actions);
  return editor;
}

function markDraftDirty(draft, label) {
  draft.dirty = true;
  label.textContent = "有未保存的内容";
  label.classList.add("is-dirty");
}

function filteredRules() {
  const query = dom.ruleSearch.value.trim().toLocaleLowerCase("zh-CN");
  const decision = dom.decisionFilter.value;
  const severity = dom.severityFilter.value;
  const scope = dom.scopeFilter.value;
  const category = dom.categoryFilter.value.trim().toLocaleLowerCase("zh-CN");

  return state.rules.filter((rule) => {
    const draft = state.drafts.get(rule.id);
    const haystack = [
      rule.id, rule.title, rule.statement, rule.category, rule.scope,
      rule.source && rule.source.document,
      rule.source && rule.source.section,
      rule.source && rule.source.quote,
    ].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN");
    return (!query || haystack.includes(query))
      && (decision === "all" || (draft && draft.decision === decision))
      && (severity === "all" || normalizeToken(rule.severity) === severity)
      && (scope === "all" || normalizeToken(rule.scope, "unknown") === scope)
      && (!category || firstText(rule.category).toLocaleLowerCase("zh-CN").includes(category));
  });
}

function clearRuleFilters() {
  dom.ruleSearch.value = "";
  dom.decisionFilter.value = "all";
  dom.severityFilter.value = "all";
  dom.scopeFilter.value = "all";
  dom.categoryFilter.value = "";
  renderRules();
  dom.ruleSearch.focus();
}

function renderActivationSummary() {
  const counts = countDecisionsFromDrafts();
  dom.activationApproved.textContent = String(counts.approved + counts.modified);
  dom.activationPending.textContent = String(counts.pending_review);
  dom.activationRejected.textContent = String(counts.rejected);
}

async function activateRules(event) {
  event.preventDefault();
  const version = dom.policyVersion.value.trim();
  if (!version) {
    toast("请输入策略版本", "激活前需要一个可审计的版本号。", "warning");
    dom.policyVersion.focus();
    return;
  }
  const dirty = Array.from(state.drafts.values()).filter((draft) => draft.dirty).length;
  if (dirty) {
    toast("仍有未保存决定", "请先保存 " + dirty + " 条规则的决定，再激活。", "warning");
    return;
  }
  const counts = countDecisionsFromDrafts();
  const confirmed = window.confirm(
    "确认激活策略版本 “" + version + "” 吗？\n\n"
    + "批准/修改后接受：" + (counts.approved + counts.modified) + " 条\n"
    + "待处理：" + counts.pending_review + " 条\n"
    + "已拒绝：" + counts.rejected + " 条\n\n"
    + "只有批准和修改后接受的规则会进入正式索引。",
  );
  if (!confirmed) return;
  setButtonBusy(dom.activateButton, true);
  try {
    await request(API.activate, { method: "POST", body: { policy_version: version } });
    await Promise.all([loadStatus(), loadReview(), loadRawReview()]);
    toast("规则库已激活", "策略版本：" + version, "success");
  } catch (error) {
    showPageError(errorMessage(error));
    toast("激活失败", errorMessage(error), "error");
  } finally {
    setButtonBusy(dom.activateButton, false);
  }
}

async function runSearch(event) {
  event.preventDefault();
  if (!state.searchReady) {
    toast("正式索引尚未激活", "请先完成候选审阅并激活规则库。", "warning");
    return;
  }
  const query = dom.searchQuery.value.trim();
  if (!query) {
    dom.searchQuery.focus();
    return;
  }
  state.searchHasRun = true;
  setButtonBusy(dom.searchButton, true);
  dom.searchResults.setAttribute("aria-busy", "true");
  dom.searchResultCount.textContent = "正在查询…";
  dom.searchResults.replaceChildren(...Array.from({ length: 2 }, () => element("div", "skeleton-card")));
  try {
    const payload = await request(API.search, {
      method: "POST",
      body: {
        query,
        file: dom.searchFile.value.trim(),
        code: dom.searchCode.value,
        limit: clampNumber(dom.searchLimit.value, 1, 50, 12),
        scopes: commaList(dom.searchScopes.value),
        categories: commaList(dom.searchCategories.value),
      },
    });
    renderSearchResults(payload);
  } catch (error) {
    dom.searchResults.replaceChildren(emptyState("检索失败", errorMessage(error), "!"));
    dom.searchResultCount.textContent = "查询失败";
    toast("检索失败", errorMessage(error), "error");
  } finally {
    dom.searchResults.setAttribute("aria-busy", "false");
    setButtonBusy(dom.searchButton, false);
  }
}

function renderSearchResults(payload) {
  const results = normalizeSearchResults(payload);
  const backend = firstText(payload && payload.index_backend).toUpperCase();
  const version = firstText(payload && payload.policy_version);
  if (backend) {
    dom.searchIndexBadge.textContent = backend + " 正式索引" + (version ? " · " + version : "");
    dom.searchIndexBadge.title = firstText(payload && payload.index_path, "当前检索使用已激活索引");
  }
  dom.searchResults.replaceChildren();
  dom.searchResultCount.textContent = results.length + " 条结果";
  if (!results.length) {
    dom.searchResults.append(emptyState("没有召回规则", "尝试增加目标路径、代码片段，或放宽范围/分类过滤。", "⌕"));
    return;
  }
  const fragment = document.createDocumentFragment();
  results.forEach((item, index) => {
    const rule = objectOrEmpty(item.rule || item.item || item);
    const card = element("article", "search-result-card");
    const titleRow = element("div", "result-title-row");
    const title = element("div");
    title.append(element("span", "rule-id", firstText(rule.id, item.rule_id, "RESULT-" + (index + 1))));
    title.append(element("h3", "", firstText(rule.title, rule.statement, item.title, "未命名规则")));
    const score = firstFinite(item.score, item.rank_score, item.similarity);
    titleRow.append(title, element("span", "score-pill", score === null ? "#" + (index + 1) : "score " + formatScore(score)));
    card.append(titleRow);

    const miniBadges = element("div", "badges");
    if (rule.severity) miniBadges.append(badge(String(rule.severity).toUpperCase(), "badge-" + normalizeToken(rule.severity)));
    if (rule.scope) miniBadges.append(badge(scopeLabel(rule.scope)));
    if (rule.category) miniBadges.append(badge(String(rule.category)));
    if (item.applicable) miniBadges.append(badge("CHECKER 直接适用"));
    arrayOf(item.checkers).forEach((checker) => miniBadges.append(badge(String(checker))));
    if (miniBadges.childNodes.length) card.append(miniBadges);

    card.append(element("p", "result-statement", firstText(rule.statement, item.statement, "无可显示规则正文")));
    const reasons = arrayOf(item.reasons || item.match_reasons || item.matched_terms);
    if (reasons.length) {
      const list = element("ul", "reason-list");
      reasons.forEach((reason) => list.append(element("li", "", String(reason))));
      card.append(list);
    }
    const source = objectOrEmpty(rule.source || item.source);
    card.append(element("p", "result-source", "来源：" + formatSource(source)));
    fragment.append(card);
  });
  dom.searchResults.append(fragment);
}

function renderRawReview() {
  dom.rawReviewText.textContent = state.rawReview || "尚未生成 REVIEW_ME。";
}

async function copyRawReview() {
  if (!state.rawReview) {
    toast("没有可复制内容", "请先生成候选规则。", "warning");
    return;
  }
  try {
    await copyText(state.rawReview);
    toast("已复制", "REVIEW_ME 全文已复制到剪贴板。", "success");
  } catch (error) {
    toast("复制失败", errorMessage(error), "error");
  }
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("浏览器拒绝访问剪贴板");
}

async function request(path, options = {}) {
  beginLoading();
  try {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const method = String(options.method || "GET").toUpperCase();
    const init = { method, headers };
    if (method === "POST") headers.set("X-PolicyKit-Studio", "1");
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json; charset=utf-8");
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, init);
    const text = await response.text();
    const data = parseMaybeJson(text);
    if (!response.ok || (data && typeof data === "object" && data.ok === false)) {
      throw apiRequestError(data, response.status);
    }
    return data;
  } catch (error) {
    if (error instanceof TypeError) setConnection(false);
    throw error;
  } finally {
    endLoading();
  }
}

async function requestRaw(path) {
  beginLoading();
  try {
    const response = await fetch(path, { headers: { Accept: "text/plain, application/json" } });
    const text = await response.text();
    const parsed = parseMaybeJson(text);
    if (!response.ok || (parsed && typeof parsed === "object" && parsed.ok === false)) {
      throw apiRequestError(parsed, response.status);
    }
    if (parsed && typeof parsed === "object") {
      for (const key of ["raw", "content", "text", "review"]) {
        if (Object.prototype.hasOwnProperty.call(parsed, key) && typeof parsed[key] === "string") {
          return parsed[key];
        }
      }
      return "";
    }
    return typeof parsed === "string" ? parsed : text;
  } finally {
    endLoading();
  }
}

function normalizeRules(payload) {
  if (Array.isArray(payload)) return payload.map(normalizeRule);
  const source = objectOrEmpty(payload);
  const candidates = source.rules || source.candidates || source.items
    || (source.review && source.review.rules)
    || (source.data && (source.data.rules || source.data.candidates));
  return arrayOf(candidates).map(normalizeRule);
}

function normalizeRule(raw, index) {
  const envelope = objectOrEmpty(raw);
  const rule = objectOrEmpty(envelope.candidate || envelope.rule || raw);
  const metadata = objectOrEmpty(rule.metadata);
  return {
    ...rule,
    id: firstText(rule.id, envelope.rule_id, "RULE-" + String(index + 1).padStart(3, "0")),
    title: firstText(rule.title, envelope.title, "未命名候选规则"),
    statement: firstText(rule.statement, envelope.statement, rule.text, ""),
    scope: firstText(rule.scope, metadata.scope, "unknown"),
    category: firstText(rule.category, metadata.category, "uncategorized"),
    severity: firstText(rule.severity, metadata.severity, "advisory"),
    confidence: rule.confidence ?? metadata.confidence,
    source: objectOrEmpty(envelope.source || rule.source),
    checker: envelope.checker ?? rule.checker,
    decision: envelope.decision ?? rule.decision,
    edited_statement: envelope.edited_statement ?? rule.edited_statement,
    notes: envelope.notes ?? rule.notes,
    review_hash: envelope.review_hash ?? rule.review_hash,
    decision_hash: envelope.decision_hash ?? rule.decision_hash,
    decision_data: objectOrEmpty(envelope.decision_data || rule.decision_data),
    metadata,
  };
}

function normalizeSearchResults(payload) {
  if (Array.isArray(payload)) return payload;
  const source = objectOrEmpty(payload);
  return arrayOf(source.results || source.items || source.matches || (source.data && source.data.results));
}

function normalizeDecision(rule) {
  const value = firstText(
    rule.decision,
    rule.review_decision,
    rule.decision_data && rule.decision_data.decision,
    rule.metadata && rule.metadata.review_decision,
    rule.status,
    "pending_review",
  ).toLowerCase();
  if (value === "pending") return "pending_review";
  if (value === "active") return "approved";
  return Object.prototype.hasOwnProperty.call(DECISIONS, value) ? value : "pending_review";
}

function extractChecker(rule) {
  const metadata = objectOrEmpty(rule.metadata);
  return rule.checker ?? rule.checks ?? metadata.checker ?? metadata.checks ?? null;
}

function checkerLabels(rule) {
  const checker = extractChecker(rule);
  const candidates = arrayOf(rule.enforcement_candidates || rule.enforcement || rule.checker_types);
  const labels = [];
  if (checker !== null) {
    const checkerItems = Array.isArray(checker) ? checker : [checker];
    checkerItems.forEach((item) => {
      const type = typeof item === "string" ? item : firstText(item && item.type, item && item.checker);
      if (type) labels.push(type);
    });
  }
  candidates.forEach((candidate) => labels.push(String(candidate)));
  return Array.from(new Set(labels)).slice(0, 5);
}

function countDecisions(rules) {
  const counts = { approved: 0, modified: 0, rejected: 0, pending_review: 0 };
  rules.forEach((rule) => { counts[normalizeDecision(rule)] += 1; });
  return counts;
}

function countDecisionsFromDrafts() {
  const counts = { approved: 0, modified: 0, rejected: 0, pending_review: 0 };
  state.drafts.forEach((draft) => { counts[draft.decision] += 1; });
  return counts;
}

function renderRulesError(error) {
  dom.ruleList.setAttribute("aria-busy", "false");
  dom.ruleList.replaceChildren(emptyState("候选规则读取失败", errorMessage(error), "!"));
  dom.ruleCountText.textContent = "读取失败";
}

function setConnection(online) {
  dom.connectionPill.dataset.state = online ? "online" : "offline";
  dom.connectionText.textContent = online ? "本地服务已连接" : "本地服务不可用";
}

function setActiveNav(id) {
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === id;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}

function beginLoading() {
  state.loadingCount += 1;
  dom.loadingRail.classList.add("is-loading");
  dom.loadingRail.setAttribute("aria-hidden", "false");
}

function endLoading() {
  state.loadingCount = Math.max(0, state.loadingCount - 1);
  if (state.loadingCount === 0) {
    dom.loadingRail.classList.remove("is-loading");
    dom.loadingRail.setAttribute("aria-hidden", "true");
  }
}

function setButtonBusy(button, busy) {
  button.disabled = busy || button.dataset.locked === "true";
  button.classList.toggle("is-loading", busy);
  button.setAttribute("aria-busy", String(busy));
}

function showPageError(message) {
  dom.pageErrorText.textContent = message;
  dom.pageError.hidden = false;
}

function hidePageError() {
  dom.pageError.hidden = true;
  dom.pageErrorText.textContent = "";
}

function toast(title, message, kind = "info") {
  const item = element("div", "toast");
  item.dataset.kind = kind;
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  const copy = element("div");
  copy.append(element("strong", "", title));
  copy.append(element("p", "", message));
  item.append(copy);
  dom.toastRegion.append(item);
  window.setTimeout(() => item.remove(), kind === "error" ? 7000 : 4300);
}

function emptyState(title, message, icon) {
  const wrapper = element("div", "empty-state");
  wrapper.append(element("span", "", icon));
  wrapper.append(element("strong", "", title));
  wrapper.append(element("p", "", message));
  return wrapper;
}

function badge(text, extraClass = "") {
  return element("span", ("badge " + extraClass).trim(), text);
}

function element(tag, className = "", text = undefined) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function formatSource(source) {
  const documentName = firstText(source.document, source.file, source.name, "未知文档");
  const section = firstText(source.section, source.heading);
  const start = source.line_start ?? source.line;
  const end = source.line_end;
  const line = start === undefined || start === null || start === ""
    ? ""
    : " · 行 " + start + (end && String(end) !== String(start) ? "–" + end : "");
  return documentName + (section ? " · " + section : "") + line;
}

function scopeLabel(scope) {
  const value = normalizeToken(scope, "unknown");
  return ({ company: "公司级", department: "部门级", project: "项目级", unknown: "范围未识别" })[value] || String(scope);
}

function normalizeToken(value, fallback = "") {
  const token = firstText(value, fallback).toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  return token || fallback;
}

function normalizeConfidence(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  if (number > 1 && number <= 100) return number / 100;
  return Math.max(0, Math.min(1, number));
}

function formatScore(value) {
  if (!Number.isFinite(value)) return "—";
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(3);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1024) return String(bytes || 0) + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function formatCount(value) {
  return Number.isFinite(value) ? new Intl.NumberFormat("zh-CN").format(value) : "—";
}

function commaList(value) {
  return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

function parseMaybeJson(text) {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_) {
    return text;
  }
}

function apiRequestError(data, status) {
  const payload = objectOrEmpty(data);
  const nested = objectOrEmpty(payload.error);
  const message = firstText(
    nested.message, payload.detail, payload.message,
    typeof data === "string" ? data : "",
    "HTTP " + status,
  );
  const code = firstText(nested.code, payload.code);
  const details = nested.details ?? payload.details ?? null;
  return new ApiRequestError(message, status, code, details);
}

function errorMessage(error) {
  return firstText(error && error.message, error, "未知错误");
}

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function arrayOf(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  return [value];
}

function firstText(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== "") return String(value);
  }
  return "";
}

function firstNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (value !== "" && value !== null && value !== undefined && Number.isFinite(number)) return number;
  }
  return NaN;
}

function firstFinite(...values) {
  const number = firstNumber(...values);
  return Number.isFinite(number) ? number : null;
}

function firstBoolean(...values) {
  for (const value of values) {
    if (typeof value === "boolean") return value;
    if (value === 1 || value === "true" || value === "ready") return true;
    if (value === 0 || value === "false" || value === "missing") return false;
  }
  return null;
}
