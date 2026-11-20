/*
 * 素材维护页：任务清单与标签、静态检查、按画面体检、换图、提交推送、重启实例、导出补丁包。
 *
 * 接口在 /custom/assets 下。截屏借用标注工具的会话（/tool/annotator/api）：标注工具同一时间只有一个会话，
 * 谁新建会话谁就把对方的关掉，所以本页只在用户点"开始截屏"时才建会话。去标注工具框选之后，本页的会话视为已被接管，
 * 回到本页时丢掉它，下次"开始截屏"再建一个新的；关闭本页时尽力关掉自己的会话。
 * 标签的改动先留在页面上，点"保存标签"时整份 PUT 一次（后端立刻提交并推送）。上传替换的图同样由后端换完立刻提交并推送：
 * 不提交的图会在下次启动服务时被启动器还原。"提交并推送"留给重试，以及标注工具等别处留下的改动。
 */
(function () {
  "use strict";

  const API = "/custom/assets";
  const ANNOTATOR_API = "/tool/annotator/api";
  const THEME_KEY = "oas_annotator_theme_v1";       // 标注工具的主题设置（同源共享），本页只读
  const UNCLASSIFIED = "未分类";
  const FRAME_RATE = 2;
  const STATUS_POLL_MS = 1000;
  const STATUS_POLL_LIMIT = 60;
  const RULE_COLUMNS = 11;          // 清单表的列数，同 index.html 的表头
  const TYPE_LABELS = {
    image: "图片", ocr: "文字识别", click: "点击", long_click: "长按", swipe: "滑动", list: "列表", unknown: "无法解析",
  };
  const ISSUE_LABELS = {
    missing_image: "缺图", json_error: "JSON 错误", duplicate_item: "重名条目", roi_out_of_bounds: "ROI 越界",
    assets_outdated: "assets.py 过期",
  };
  const SESSION_LOST_TEXT = "截屏会话已失效（闲置超过 10 分钟，或标注工具页面接管了会话）。"
    + "请先关闭标注工具页面，再点“开始截屏”。";
  const ANNOTATOR_CONFIRM_TEXT = "标注工具同一时间只能有一个会话：打开它会关闭本页的截屏会话，两个页面不要同时使用。\n"
    + "框选完成后，请先关闭标注工具页面，再回到本页重新点“开始截屏”。\n\n现在打开标注工具？";
  const UNSAVED_TAGS_NOTE = "（标签有未保存的改动：这次体检按已保存的标签划分画面，保存后再体检才按页面上的）";

  const el = {};
  const state = {
    task: "",
    screens: [],
    rules: [],
    tags: {},
    tagsLoaded: false,
    tagsDirty: false,
    tagsEdits: 0,               // 每改一次标签加一：保存、读取期间又改了的话，不能把页面标成"已保存"或用读回的覆盖
    loadSeq: 0,                // 每次读取任务加一：读取期间切换了任务，旧的结果就丢掉
    thumbVersion: Date.now(),   // 缩略图地址带上它，重新读取时不用浏览器缓存里的旧图
    sessionId: "",
    captureConfig: "",
    pollTimer: 0,
    pollGeneration: 0,          // stopPolling() 每调一次加一：旧的状态轮询看到变了就停下，同一时刻只有一条轮询链
    annotatorOpened: false,
    lastCommits: {},            // 任务 -> 本页最近一次上传替换或"提交并推送"得到的提交，导出补丁包用
  };

  // ---------------------------------------------------------------------------------------------------------------
  // 通用
  // ---------------------------------------------------------------------------------------------------------------

  function applyTheme() {
    let theme = "dark";
    try {
      theme = window.localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
    } catch (error) {
      // 读不到本地存储就用默认的深色
    }
    document.documentElement.setAttribute("data-theme", theme);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function taskUrl(task, tail) {
    return API + "/" + encodeURIComponent(task) + "/" + tail;
  }

  async function api(path, options) {
    const request = Object.assign({}, options || {});
    request.headers = Object.assign({}, request.headers || {});
    if (request.json !== undefined) {
      request.body = JSON.stringify(request.json);
      request.headers["Content-Type"] = "application/json";
      delete request.json;
    }
    const response = await fetch(path, request);
    const payload = await readPayload(response);
    if (!response.ok) {
      throw requestError(response.status, payload);
    }
    return payload;
  }

  async function readPayload(response) {
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      return text;
    }
  }

  function requestError(status, payload) {
    let message = "请求失败（HTTP " + status + "）";
    let code = "";
    const detail = isObject(payload) ? payload.detail : undefined;
    if (typeof detail === "string") {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = "请求参数不对：" + detail.map((item) => (item.loc || []).join(".")).join("、");
    } else if (isObject(detail)) {
      code = String(detail.code || "");
      message = detail.message || code || message;
    } else if (isObject(payload) && payload.message) {
      message = payload.message;
    }
    const error = new Error(message);
    error.code = code;
    error.status = status;
    return error;
  }

  function showMessage(text, kind) {
    el.pageMessage.textContent = text;
    el.pageMessage.className = "page-message" + (kind ? " " + kind : "");
  }

  function setStatus(element, text, kind) {
    element.textContent = text;
    element.className = "status-text" + (kind ? " " + kind : "");
  }

  function reportError(error) {
    console.error(error);
    if (error && error.code === "invalid_session") {
      dropSession();
      showMessage(SESSION_LOST_TEXT, "error");
      return;
    }
    showMessage((error && error.message) || String(error), "error");
  }

  // 按钮触发的操作：进行中按钮置灰并转圈，出错显示在页面顶部的消息栏
  function action(button, run) {
    return async function (event) {
      if (button) {
        button.disabled = true;
        button.classList.add("btn-loading");
      }
      try {
        await run(event);
      } catch (error) {
        reportError(error);
      } finally {
        if (button) {
          button.disabled = false;
          button.classList.remove("btn-loading");
        }
        updateTagsView();
      }
    };
  }

  function cell(row, text) {
    const td = document.createElement("td");
    if (text !== undefined) {
      td.textContent = text;
    }
    row.appendChild(td);
    return td;
  }

  function line(text, className) {
    const span = document.createElement("span");
    span.className = "cell-line" + (className ? " " + className : "");
    span.textContent = text;
    return span;
  }

  function muted(text) {
    const span = document.createElement("span");
    span.className = "muted";
    span.textContent = text;
    return span;
  }

  function fillSelect(select, pairs) {
    const fragment = document.createDocumentFragment();
    for (const [value, label] of pairs) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      fragment.appendChild(option);
    }
    select.replaceChildren(fragment);
  }

  function shortSha(sha) {
    return sha ? sha.slice(0, 7) : "";
  }

  function commitNote(result) {
    return result.commit ? "（提交 " + shortSha(result.commit) + "）" : "";
  }

  // 提交类接口的结果：推上 fork 才说"已保存到 fork"，否则显示后端给的原因
  function describeCommit(result, subject, actionName) {
    if (result.pushed) {
      return [subject + "已保存到 fork" + commitNote(result), "ok"];
    }
    if (result.error) {
      return [actionName + "：" + result.error, "error"];
    }
    return [subject + "没有改动，不需要提交", "ok"];
  }

  // 上传替换的结果：后端换完图立刻提交并推送，replaced 说明图写下了没有
  function describeReplace(result) {
    if (!result.replaced) {
      return ["没有替换：" + (result.error || "原因不明"), "error"];
    }
    if (result.pushed) {
      return ["已替换并保存到 fork" + commitNote(result) + "，重启实例后生效", "ok"];
    }
    if (result.commit) {
      return ["图已替换并在本地提交，但没有推送到 fork：" + result.error, "error"];
    }
    if (result.error) {
      return ["图已替换，但没有提交成功：" + result.error
        + "。点\"提交并推送\"重试；不提交的话下次启动服务会还原这张图", "error"];
    }
    return ["图已替换，内容和 fork 上的一样，不需要提交", "ok"];
  }

  // ---------------------------------------------------------------------------------------------------------------
  // 任务、清单与标签
  // ---------------------------------------------------------------------------------------------------------------

  async function loadTasks() {
    const data = await api(API + "/tasks?all=" + (el.showAllTasks.checked ? "1" : "0"));
    const tasks = (data && data.tasks) || [];
    fillSelect(el.taskSelect, tasks.map((name) => [name, name]));
    if (!tasks.length) {
      state.task = "";
      state.rules = [];
      renderRulesPlaceholder("没有可以维护的任务");
      return;
    }
    el.taskSelect.value = tasks.includes(state.task) ? state.task : tasks[0];
    if (el.taskSelect.value !== state.task) {
      await loadTask();
    }
  }

  async function loadTask() {
    const task = el.taskSelect.value;
    const seq = ++state.loadSeq;
    state.task = task;
    state.thumbVersion = Date.now();
    clearCheck();
    renderRulesPlaceholder("正在读取…");
    el.staticResult.replaceChildren(line("正在检查…", "muted"));
    const [rules, check] = await Promise.allSettled([
      api(taskUrl(task, "rules")),
      api(taskUrl(task, "static-check")),
    ]);
    if (seq !== state.loadSeq) {
      return;
    }
    if (rules.status === "fulfilled") {
      state.screens = rules.value.screens || [];
      state.rules = rules.value.rules || [];
      renderRules();
      fillScreens();
    } else {
      state.rules = [];
      renderRulesPlaceholder("清单读取失败：" + rules.reason.message);
      reportError(rules.reason);
    }
    if (check.status === "fulfilled") {
      renderStatic(check.value.issues || []);
    } else {
      el.staticResult.replaceChildren(line("静态检查失败：" + check.reason.message, "text-danger"));
    }
  }

  async function refreshStatic() {
    const task = state.task;
    const seq = state.loadSeq;
    try {
      const data = await api(taskUrl(task, "static-check"));
      if (seq === state.loadSeq) {
        renderStatic(data.issues || []);
      }
    } catch (error) {
      if (seq === state.loadSeq) {
        el.staticResult.replaceChildren(line("静态检查失败：" + error.message, "text-danger"));
      }
    }
  }

  async function loadTags() {
    const edits = state.tagsEdits;
    try {
      const tags = await api(API + "/tags");
      if (edits !== state.tagsEdits) {
        return;                     // 读取期间改了标签：保留页面上的改动
      }
      state.tags = isObject(tags) ? tags : {};
      state.tagsLoaded = true;
      state.tagsDirty = false;
    } catch (error) {
      state.tagsLoaded = false;     // 读不到（比如文件损坏）就不许改，免得保存时把它整个覆盖掉
      throw error;
    } finally {
      updateTagsView();
    }
  }

  // 有没保存的标签改动时保留页面上的标签，只重新读清单和静态检查
  async function reload() {
    if (!state.tagsDirty) {
      try {
        await loadTags();
      } catch (error) {
        reportError(error);
      }
    }
    await loadTask();
  }

  // 同后端 assets_check._tag_of：periodic 只认 true，screen 是非空字符串，否则用默认值
  function tagOf(task, file, name) {
    const files = state.tags[task];
    const items = isObject(files) ? files[file] : null;
    const tag = isObject(items) && name ? items[name] : null;
    const screen = isObject(tag) ? tag.screen : null;
    return {
      periodic: isObject(tag) && tag.periodic === true,
      screen: typeof screen === "string" && screen ? screen : UNCLASSIFIED,
    };
  }

  function setTag(task, file, name, change) {
    const current = tagOf(task, file, name);
    if (!isObject(state.tags[task])) {
      state.tags[task] = {};
    }
    if (!isObject(state.tags[task][file])) {
      state.tags[task][file] = {};
    }
    const raw = state.tags[task][file][name];
    state.tags[task][file][name] = Object.assign({}, isObject(raw) ? raw : {}, current, change);
    state.tagsEdits += 1;
    state.tagsDirty = true;
    updateTagsView();
  }

  function updateTagsView() {
    el.saveTagsBtn.disabled = !(state.tagsLoaded && state.tagsDirty) || el.saveTagsBtn.classList.contains("btn-loading");
    if (!state.tagsLoaded) {
      setStatus(el.tagsStatus, "标签文件没有读到，暂时不能修改标签", "error");
    } else if (state.tagsDirty) {
      setStatus(el.tagsStatus, "有未保存的标签改动", "error");
    } else {
      setStatus(el.tagsStatus, "", "");
    }
  }

  async function saveTags() {
    if (!state.tagsLoaded || !state.tagsDirty) {
      return;
    }
    const edits = state.tagsEdits;
    const result = await api(API + "/tags", { method: "PUT", json: state.tags });
    if ((result.commit || !result.error) && edits === state.tagsEdits) {
      state.tagsDirty = false;      // 文件已经写下（有提交，或者内容本来就一样）；没推上去的提交，"提交并推送"会补推
    }
    const [text, kind] = describeCommit(result, "标签", "保存标签");
    showMessage(text, kind);
  }

  function renderRulesPlaceholder(text) {
    const tr = document.createElement("tr");
    const td = cell(tr, text);
    td.colSpan = RULE_COLUMNS;
    td.className = "muted";
    el.rulesBody.replaceChildren(tr);
  }

  function renderRules() {
    const fragment = document.createDocumentFragment();
    for (const rule of state.rules) {
      const items = rule.items || [];
      if (!items.length) {
        fragment.appendChild(emptyFileRow(rule));
      }
      for (const item of items) {
        fragment.appendChild(ruleRow(state.task, rule, item));
      }
    }
    if (!fragment.childNodes.length) {
      renderRulesPlaceholder("这个任务没有素材文件");
      return;
    }
    el.rulesBody.replaceChildren(fragment);
  }

  function emptyFileRow(rule) {
    const tr = document.createElement("tr");
    cell(tr).appendChild(muted("—"));
    cell(tr).appendChild(muted(rule.rule_type === "unknown" ? "解析不了这个文件，见静态检查" : "这个文件里没有条目"));
    cell(tr, TYPE_LABELS[rule.rule_type] || rule.rule_type);
    cell(tr, rule.file);
    while (tr.childNodes.length < RULE_COLUMNS) {
      cell(tr);
    }
    return tr;
  }

  // json 条目里的字符串字段，没有或为空时显示 "—"
  function textOf(value) {
    return typeof value === "string" && value.trim() ? value.trim() : "—";
  }

  function ruleRow(task, rule, item) {
    const name = typeof item.itemName === "string" ? item.itemName : "";
    const imageName = typeof item.imageName === "string" ? item.imageName : "";
    const tag = tagOf(task, rule.file, name);
    const editable = Boolean(name) && state.tagsLoaded;
    const tr = document.createElement("tr");

    const thumbCell = cell(tr);
    thumbCell.appendChild(imageName ? thumbnail(task, rule.file, imageName, state.thumbVersion) : muted("—"));

    cell(tr, name || "（没有 itemName）");
    cell(tr, TYPE_LABELS[rule.rule_type] || rule.rule_type);
    cell(tr, rule.file);

    const periodic = document.createElement("input");
    periodic.type = "checkbox";
    periodic.checked = tag.periodic;
    periodic.disabled = !editable;
    periodic.title = "每期活动都要换的图";
    periodic.addEventListener("change", () => setTag(task, rule.file, name, { periodic: periodic.checked }));
    cell(tr).appendChild(periodic);

    const screen = document.createElement("select");
    const known = state.screens.includes(tag.screen);
    const values = known ? state.screens : state.screens.concat([tag.screen]);
    fillSelect(screen, values.map((value) => [value, value === tag.screen && !known ? value + "（不在可选范围）" : value]));
    screen.value = tag.screen;
    screen.disabled = !editable;
    screen.addEventListener("change", () => setTag(task, rule.file, name, { screen: screen.value }));
    cell(tr).appendChild(screen);

    const roi = cell(tr);
    roi.className = "roi-text";
    if (typeof item.roiFront === "string" && item.roiFront) {
      roi.appendChild(line("roiFront " + item.roiFront));
    }
    if (typeof item.roiBack === "string" && item.roiBack) {
      roi.appendChild(line("roiBack " + item.roiBack, "muted"));
    }
    if (!roi.childNodes.length) {
      roi.appendChild(muted("—"));
    }

    cell(tr, typeof item.threshold === "number" ? String(item.threshold) : "—");
    cell(tr, textOf(item.method));
    const description = document.createElement("span");      // 描述可能很长：截断显示，悬停看全文
    description.className = "clip";
    description.textContent = textOf(item.description);
    description.title = description.textContent;
    cell(tr).appendChild(description);
    cell(tr).appendChild(imageName ? replaceControl(task, rule.file, imageName, thumbCell) : muted("—"));
    return tr;
  }

  function previewUrl(task, file, imageName, version) {
    const query = new URLSearchParams({ task_name: task, json_relpath: file, image_name: imageName, v: String(version) });
    return ANNOTATOR_API + "/rules/image-preview?" + query.toString();
  }

  function thumbnail(task, file, imageName, version) {
    const box = document.createElement("a");
    box.className = "thumb";
    box.href = previewUrl(task, file, imageName, version);
    box.target = "_blank";
    box.rel = "noopener";
    box.title = imageName;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = imageName;
    img.addEventListener("error", () => {
      box.classList.add("missing");
      box.removeAttribute("href");
      box.title = "图片不存在：" + imageName;
      box.replaceChildren(document.createTextNode("缺图"));
    });
    img.src = box.href;
    box.appendChild(img);
    return box;
  }

  function relativeImagePath(file, imageName) {
    const slash = file.lastIndexOf("/");
    return slash < 0 ? imageName : file.slice(0, slash + 1) + imageName;
  }

  function readAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("读取文件失败：" + file.name));
      reader.readAsDataURL(file);
    });
  }

  function replaceControl(task, file, imageName, thumbCell) {
    const wrap = document.createElement("span");
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".png,.jpg,.jpeg,.bmp";
    input.className = "hidden";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "上传替换";
    button.addEventListener("click", () => input.click());
    input.addEventListener("change", action(button, async () => {
      const upload = input.files && input.files[0];
      input.value = "";             // 同一个文件再选一次也会触发 change
      if (!upload) {
        return;
      }
      if (!window.confirm("用 " + upload.name + " 替换 " + relativeImagePath(file, imageName) + "？")) {
        return;
      }
      const content = await readAsDataUrl(upload);
      const result = await api(taskUrl(task, "replace-image"), {
        method: "POST",
        json: { json_relpath: file, image_name: imageName, content_base64: content },
      });
      if (result.commit) {
        state.lastCommits[task] = result.commit;
      }
      const [text, kind] = describeReplace(result);
      showMessage(text, kind);
      if (!result.replaced) {
        return;
      }
      thumbCell.replaceChildren(thumbnail(task, file, imageName, Date.now()));
      if (task === state.task) {
        await refreshStatic();
      }
    }));
    wrap.append(input, button);
    return wrap;
  }

  function renderStatic(issues) {
    if (!issues.length) {
      el.staticResult.replaceChildren(line("没有发现问题", "text-ok"));
      return;
    }
    const list = document.createElement("ul");
    list.className = "issue-list";
    for (const issue of issues) {
      const li = document.createElement("li");
      const kind = document.createElement("span");
      kind.className = "issue-kind";
      kind.textContent = ISSUE_LABELS[issue.kind] || issue.kind;
      const where = [issue.file, issue.item].filter(Boolean).join(" · ");
      li.append(kind, document.createTextNode((where ? where + "：" : "") + issue.detail));
      list.appendChild(li);
    }
    el.staticResult.replaceChildren(line("发现 " + issues.length + " 个问题", "text-danger"), list);
  }

  // ---------------------------------------------------------------------------------------------------------------
  // 按画面体检
  // ---------------------------------------------------------------------------------------------------------------

  async function loadInstances() {
    const data = await api(ANNOTATOR_API + "/configs");
    const configs = (data && data.configs) || [];
    fillSelect(el.instanceSelect, configs.length ? configs.map((name) => [name, name]) : [["", "没有实例配置"]]);
  }

  function fillScreens() {
    const previous = el.screenSelect.value;
    fillSelect(el.screenSelect, state.screens.map((screen) => [screen, screen]));
    if (state.screens.includes(previous)) {
      el.screenSelect.value = previous;
    }
  }

  // 停掉状态轮询：取消等着的下一轮，代数加一，让正在等状态返回的那一轮回来后自己停下（见 pollStatus）
  function stopPolling() {
    state.pollGeneration += 1;
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = 0;
    }
  }

  // 丢掉本页的截屏会话（已失效，或者已被标注工具接管）：下次"开始截屏"时新建
  function dropSession() {
    stopPolling();
    state.sessionId = "";
    state.captureConfig = "";
    el.sessionId.textContent = "-";
    setStatus(el.captureStatus, "未开始截屏", "");
  }

  // 尽力关掉本页的会话，不等结果；页面卸载时用 sendBeacon
  function closeSession(reason, onUnload) {
    if (!state.sessionId) {
      return;
    }
    const path = ANNOTATOR_API + "/session/" + encodeURIComponent(state.sessionId);
    const query = "?reason=" + encodeURIComponent(reason);
    if (onUnload && navigator.sendBeacon) {
      navigator.sendBeacon(path + "/close" + query, "");
      return;
    }
    fetch(path + query, { method: "DELETE", keepalive: true }).catch(() => {
      // 尽力而为
    });
  }

  async function ensureSession() {
    if (state.sessionId) {
      return state.sessionId;
    }
    const data = await api(ANNOTATOR_API + "/session", { method: "POST" });
    const sessionId = (data && data.session && data.session.session_id) || "";
    if (!sessionId) {
      throw new Error("标注工具没有返回会话");
    }
    state.sessionId = sessionId;
    el.sessionId.textContent = sessionId;
    return sessionId;
  }

  async function startCapture() {
    const config = el.instanceSelect.value;
    if (!config) {
      throw new Error("先选择实例");
    }
    stopPolling();
    const sessionId = await ensureSession();
    await api(ANNOTATOR_API + "/emulator/start", {
      method: "POST",
      json: { session_id: sessionId, config_name: config, frame_rate: FRAME_RATE },
    });
    state.captureConfig = config;
    setStatus(el.captureStatus, "正在连接 " + config + " 的模拟器…", "");
    pollStatus(sessionId, 0);
  }

  // 开始截屏之后轮询到有画面（或出错）为止。同一时刻只有一条轮询链：链记下开始时的代数，每次 await 回来都核对，
  // stopPolling() 改过代数（又点了"开始截屏"、会话被丢掉）就停下，不改状态、不报错、不再续下一轮
  function pollStatus(sessionId, round) {
    const generation = state.pollGeneration;
    const stale = () => generation !== state.pollGeneration || sessionId !== state.sessionId;
    state.pollTimer = window.setTimeout(async () => {
      state.pollTimer = 0;
      if (stale()) {
        return;
      }
      let status;
      try {
        const data = await api(ANNOTATOR_API + "/emulator/status?session_id=" + encodeURIComponent(sessionId));
        status = (data && data.emulator) || {};
      } catch (error) {
        if (!stale()) {
          reportError(error);
        }
        return;
      }
      if (stale()) {
        return;
      }
      if (status.state === "running" && status.last_frame_at) {
        setStatus(el.captureStatus, "截屏就绪（" + state.captureConfig + "）：把游戏切到要检查的画面，再点“体检”", "ok");
      } else if (status.state === "error") {
        setStatus(el.captureStatus, "截屏失败：" + (status.error || "原因不明"), "error");
      } else if (status.state === "stopped") {
        setStatus(el.captureStatus, "截屏已停止，请重新点“开始截屏”", "error");
      } else if (round + 1 >= STATUS_POLL_LIMIT) {
        setStatus(el.captureStatus, "模拟器一直没有画面：确认模拟器已经启动后，重新点“开始截屏”", "error");
      } else {
        const retry = status.retry_count ? "（重试 " + status.retry_count + "/" + status.max_retries + "）" : "";
        setStatus(el.captureStatus, "正在连接 " + state.captureConfig + " 的模拟器…" + retry, "");
        pollStatus(sessionId, round + 1);
      }
    }, STATUS_POLL_MS);
  }

  function clearCheck() {
    el.checkHint.textContent = "";
    el.checkHint.classList.add("hidden");
    el.checkBody.replaceChildren();
    el.checkWrap.classList.add("hidden");
    el.captureLink.classList.add("hidden");
  }

  function showCapture(url) {
    el.captureLink.href = url;
    el.capturePreview.src = url;
    el.captureLink.classList.remove("hidden");
  }

  async function runScreenCheck() {
    if (!state.sessionId || !state.captureConfig) {
      throw new Error("先选择实例，点“开始截屏”");
    }
    const task = state.task;
    const screen = el.screenSelect.value;
    if (!task || !screen) {
      throw new Error("先选择任务和画面");
    }
    const capture = await api(ANNOTATOR_API + "/emulator/capture", {
      method: "POST",
      json: { session_id: state.sessionId },
    });
    const image = (capture && capture.image) || {};
    const unsavedTags = state.tagsDirty;      // 后端按标签文件里已保存的标签划分画面，页面上没保存的改动不算
    const data = await api(taskUrl(task, "screen-check"), {
      method: "POST",
      json: { session_id: state.sessionId, image_id: image.id, screen: screen },
    });
    if (task !== state.task) {
      showMessage("体检期间切换了任务，结果没有显示，请重新体检", "error");
      return;
    }
    clearCheck();
    showCapture(image.url);
    renderCheck(data.results || [], data.hint || "");
    const expected = (data.results || []).filter((result) => result.expected);
    const passed = expected.filter((result) => result.matched && !result.error);
    const note = unsavedTags ? UNSAVED_TAGS_NOTE : "";
    if (expected.length) {
      showMessage("体检完成：" + screen + " 的 " + expected.length + " 个条目通过 " + passed.length + " 个" + note,
        passed.length === expected.length ? "ok" : "error");
    } else {
      showMessage((data.hint || "体检完成") + note, "ok");
    }
  }

  function renderCheck(results, hint) {
    el.checkHint.textContent = hint;
    el.checkHint.classList.toggle("hidden", !hint);
    const ordered = results.slice().sort((a, b) => Number(Boolean(b.expected)) - Number(Boolean(a.expected)));
    const fragment = document.createDocumentFragment();
    for (const result of ordered) {
      fragment.appendChild(checkRow(result));
    }
    el.checkBody.replaceChildren(fragment);
    el.checkWrap.classList.toggle("hidden", !ordered.length);
  }

  function checkRow(result) {
    const tr = document.createElement("tr");
    if (!result.expected) {
      tr.className = "row-muted";
    }
    cell(tr, result.item || "—");
    cell(tr, result.file);
    cell(tr, typeof result.similarity === "number" ? result.similarity.toFixed(3) : "—");
    cell(tr, typeof result.threshold === "number" ? String(result.threshold) : "—");
    const outcome = cell(tr);
    if (!result.expected) {
      outcome.appendChild(line("不在本画面"));
    } else if (!result.error) {
      outcome.appendChild(line(result.matched ? "通过" : "未通过", result.matched ? "text-ok" : "text-danger"));
    }
    if (result.error) {
      outcome.appendChild(line("出错：" + result.error, "text-danger"));
    }
    return tr;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // 提交与生效
  // ---------------------------------------------------------------------------------------------------------------

  async function commitPush() {
    const task = state.task;
    if (!task) {
      throw new Error("先选择任务");
    }
    const result = await api(taskUrl(task, "commit-push"), {
      method: "POST",
      json: { message: el.commitMessage.value },
    });
    if (result.commit) {
      state.lastCommits[task] = result.commit;
    }
    if (result.commit && !result.error) {
      el.commitMessage.value = "";
    }
    const [text, kind] = describeCommit(result, task + " 的素材", "提交并推送");
    showMessage(text, kind);
  }

  async function restartInstance() {
    const name = el.instanceSelect.value;
    if (!name) {
      throw new Error("先在“按画面体检”里选择实例");
    }
    if (!window.confirm("重启实例 " + name + "？它正在执行的任务会中断。")) {
      return;
    }
    const result = await api("/custom/instance/" + encodeURIComponent(name) + "/restart", { method: "POST" });
    if (result && result.ok) {
      showMessage("已重启实例 " + name, "ok");
    } else {
      showMessage("没有重启：" + name + " 不是已有的实例，或者服务启动后它还没运行过", "error");
    }
  }

  function attachmentName(header) {
    const match = /filename="([^"]+)"/.exec(header || "");
    return match ? match[1] : "";
  }

  async function exportPatch() {
    const task = state.task;
    const commit = state.lastCommits[task];
    if (!commit) {
      throw new Error("先提交，再导出补丁包");
    }
    const response = await fetch(taskUrl(task, "export") + "?commit=" + encodeURIComponent(commit));
    if (!response.ok) {
      throw requestError(response.status, await readPayload(response));
    }
    const blob = await response.blob();
    const name = attachmentName(response.headers.get("content-disposition")) || "assets-patch-" + shortSha(commit) + ".zip";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    showMessage("已导出 " + name + "：解压到仓库根目录即可覆盖对应文件", "ok");
  }

  function openAnnotator() {
    if (!window.confirm(ANNOTATOR_CONFIRM_TEXT)) {
      return;
    }
    const opened = window.open("/tool/annotator", "_blank");
    if (!opened) {
      showMessage("浏览器拦截了新标签页：请允许本页打开弹出窗口后重试", "error");
      return;
    }
    state.annotatorOpened = true;
    showMessage("已在新标签页打开标注工具。回到本页时会重新读取清单，截屏需要重新开始。", "ok");
  }

  function onVisibilityChange() {
    if (document.visibilityState !== "visible" || !state.annotatorOpened) {
      return;
    }
    state.annotatorOpened = false;
    closeSession("assets_check_after_annotator", false);
    dropSession();
    showMessage("已从标注工具返回：本页的截屏会话已交给标注工具，清单已重新读取。"
      + "要体检，请先关闭标注工具页面，再点“开始截屏”。", "ok");
    reload().catch(reportError);
  }

  // ---------------------------------------------------------------------------------------------------------------
  // 启动
  // ---------------------------------------------------------------------------------------------------------------

  function collectElements() {
    const ids = [
      "pageMessage", "sessionId", "taskSelect", "showAllTasks", "reloadBtn", "tagsStatus", "saveTagsBtn", "rulesBody",
      "staticResult", "instanceSelect", "startCaptureBtn", "captureStatus", "screenSelect", "screenCheckBtn",
      "checkHint", "captureLink", "capturePreview", "checkWrap", "checkBody", "commitMessage", "commitPushBtn",
      "restartBtn", "exportBtn", "annotatorBtn",
    ];
    for (const id of ids) {
      el[id] = document.getElementById(id);
    }
  }

  function bindEvents() {
    el.taskSelect.addEventListener("change", action(null, loadTask));
    el.showAllTasks.addEventListener("change", action(null, loadTasks));
    el.reloadBtn.addEventListener("click", action(el.reloadBtn, reload));
    el.saveTagsBtn.addEventListener("click", action(el.saveTagsBtn, saveTags));
    el.startCaptureBtn.addEventListener("click", action(el.startCaptureBtn, startCapture));
    el.screenCheckBtn.addEventListener("click", action(el.screenCheckBtn, runScreenCheck));
    el.commitPushBtn.addEventListener("click", action(el.commitPushBtn, commitPush));
    el.restartBtn.addEventListener("click", action(el.restartBtn, restartInstance));
    el.exportBtn.addEventListener("click", action(el.exportBtn, exportPatch));
    el.annotatorBtn.addEventListener("click", openAnnotator);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", () => closeSession("assets_check_pagehide", true));
    window.addEventListener("beforeunload", (event) => {
      if (state.tagsDirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  async function init() {
    applyTheme();
    collectElements();
    bindEvents();
    updateTagsView();
    const results = await Promise.allSettled([loadTags(), loadInstances()]);
    for (const result of results) {
      if (result.status === "rejected") {
        reportError(result.reason);
      }
    }
    await loadTasks();
  }

  init().catch(reportError);
})();
