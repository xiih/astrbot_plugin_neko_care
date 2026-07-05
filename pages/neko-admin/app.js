(() => {
  const HTTP_API = "/astrbot_plugin_neko_care/page";
  const state = {
    config: null,
    summary: {},
    users: [],
    filteredUsers: [],
    currentUser: null,
    options: { personalities: [], body_types: [] },
    dirty: false,
    userDirty: false,
  };

  const saveState = document.getElementById("saveState");
  const saveBtn = document.getElementById("saveBtn");
  const resetBtn = document.getElementById("resetBtn");
  const toast = document.getElementById("toast");
  const userSearch = document.getElementById("userSearch");
  const usersList = document.getElementById("usersList");
  const usersCount = document.getElementById("usersCount");
  const emptyUserState = document.getElementById("emptyUserState");
  const userEditor = document.getElementById("userEditor");
  const saveUserBtn = document.getElementById("saveUser");
  const deleteUserBtn = document.getElementById("deleteUser");

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    });
    document.addEventListener("input", handleFieldChange);
    document.addEventListener("change", handleFieldChange);
    document.addEventListener("click", handleAction);
    saveBtn.addEventListener("click", saveConfig);
    resetBtn.addEventListener("click", resetConfig);
    document.getElementById("refreshUsers").addEventListener("click", loadUsers);
    document.getElementById("addUser").addEventListener("click", createUserDraft);
    saveUserBtn.addEventListener("click", saveUser);
    deleteUserBtn.addEventListener("click", deleteCurrentUser);
    userSearch.addEventListener("input", filterUsers);
    document.getElementById("catgirlEnabled").addEventListener("change", handleUserFieldChange);
    document.getElementById("pendingWorkEnabled").addEventListener("change", handleUserFieldChange);
    document.getElementById("editUid").addEventListener("input", handleUserFieldChange);
    document.getElementById("editWallet").addEventListener("input", handleUserFieldChange);
    document.querySelectorAll("[data-user-path], [data-cat-path], [data-work-path]").forEach((input) => {
      input.addEventListener("input", handleUserFieldChange);
      input.addEventListener("change", handleUserFieldChange);
    });
    ["signJson", "catJson", "itemsJson", "pendingJson"].forEach((id) => {
      document.getElementById(id).addEventListener("input", handleJsonChange);
    });
    loadConfig();
    loadUsers();
  }

  async function loadConfig() {
    setStatus("读取中", false);
    try {
      const payload = await apiGet("config");
      state.config = payload.config;
      state.summary = payload.summary || {};
      state.options = payload.options || state.options;
      state.dirty = false;
      renderAll();
      setStatus("已同步", false);
    } catch (error) {
      setStatus("读取失败", true);
      showToast(error.message || "读取失败", true);
    }
  }

  async function saveConfig() {
    if (!state.config) return;
    setStatus("保存中", false);
    saveBtn.disabled = true;
    try {
      const payload = await apiPost("config/save", { config: state.config });
      state.config = payload.config;
      state.summary = payload.summary || {};
      state.dirty = false;
      renderAll();
      setStatus("已同步", false);
      showToast(payload.message || "配置已保存");
    } catch (error) {
      setStatus("保存失败", true);
      showToast(error.message || "保存失败", true);
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function resetConfig() {
    if (!await confirmDialog("恢复默认运行参数？")) return;
    setStatus("重置中", false);
    resetBtn.disabled = true;
    try {
      const payload = await apiPost("config/reset", {});
      state.config = payload.config;
      state.summary = payload.summary || {};
      state.dirty = false;
      renderAll();
      setStatus("已同步", false);
      showToast(payload.message || "已恢复默认");
    } catch (error) {
      setStatus("重置失败", true);
      showToast(error.message || "重置失败", true);
    } finally {
      resetBtn.disabled = false;
    }
  }

  function renderAll() {
    if (!state.config) return;
    renderOverview();
    renderUserOptions();
    renderDailyEvents();
    renderFoods();
    renderJobs();
    renderShopItems();
    renderCareServices();
    renderInteractions();
    renderPersonalities();
    renderDailyWishes();
    renderEvents();
    syncFields(document);
  }

  async function loadUsers() {
    try {
      const payload = await apiGet("users");
      state.users = payload.users || [];
      state.options = payload.options || state.options;
      state.summary = payload.summary || state.summary || {};
      filterUsers();
      renderUserOptions();
      if (state.config) renderOverview();
    } catch (error) {
      showToast(error.message || "用户列表读取失败", true);
    }
  }

  function filterUsers() {
    const keyword = userSearch.value.trim().toLowerCase();
    state.filteredUsers = state.users.filter((row) => {
      if (!keyword) return true;
      return [row.uid, row.cat_name, row.personality, row.stage, row.status]
        .map((value) => String(value || "").toLowerCase())
        .some((value) => value.includes(keyword));
    });
    renderUsersList();
  }

  function renderUsersList() {
    usersCount.textContent = `共 ${state.filteredUsers.length} / ${state.users.length} 个用户`;
    if (!state.filteredUsers.length) {
      usersList.innerHTML = `<div class="empty-mini">暂无匹配用户</div>`;
      return;
    }
    usersList.innerHTML = state.filteredUsers
      .map((row) => {
        const active = state.currentUser && state.currentUser.uid === row.uid ? " active" : "";
        const title = row.has_catgirl ? row.cat_name || "未命名猫娘" : "未收养猫娘";
        const meta = row.has_catgirl
          ? `${row.stage}｜${row.status}｜饱食 ${formatNumber(row.satiety, 0)}`
          : row.has_pending_adoption
          ? "有待确认收养"
          : "只有基础数据";
        return `
          <button class="user-row${active}" data-action="select-user" data-uid="${escapeAttr(row.uid)}" type="button">
            <span>
              <strong>${escapeHtml(title)}</strong>
              <small>${escapeHtml(row.uid)}</small>
            </span>
            <span>
              <b>${escapeHtml(row.wallet)}</b>
              <small>${escapeHtml(meta)}</small>
            </span>
          </button>`;
      })
      .join("");
  }

  async function selectUser(uid) {
    if (!uid) return;
    if (state.userDirty && !await confirmDialog("当前用户数据还没保存，切换后会丢弃未保存修改。继续切换？")) return;
    try {
      const payload = await apiGet("users/detail", { uid });
      state.currentUser = payload.user;
      state.options = payload.options || state.options;
      state.userDirty = false;
      renderUserOptions();
      renderUserEditor();
      renderUsersList();
    } catch (error) {
      showToast(error.message || "用户详情读取失败", true);
    }
  }

  async function createUserDraft() {
    if (state.userDirty && !await confirmDialog("当前用户数据还没保存，创建新用户会丢弃未保存修改。继续？")) return;
    const now = Math.floor(Date.now() / 1000);
    state.currentUser = {
      uid: "",
      wallet: 0,
      sign: {},
      catgirl_enabled: true,
      catgirl: {
        schema_version: 2,
        weight_unit: "斤",
        user: "",
        name: "新猫娘",
        personality: firstOption("personalities", "温柔"),
        stage: 0,
        growth: 0,
        intimacy: 0,
        satiety: 80,
        mood: 85,
        health: 90,
        energy: 80,
        body_type: firstOption("body_types", "匀称"),
        ideal_weight: 60,
        weight: 60,
        created_at: now,
        last_decay: now,
        care_stats: {},
        unlocks: { work_jobs: [] },
        buffs: {},
        care_cooldowns: {},
        gift_stats: {},
      },
      cat_summary: {},
      pending_work_enabled: false,
      items: {},
      pending_adoption: null,
    };
    state.userDirty = true;
    renderUserEditor();
    renderUsersList();
  }

  function renderUserOptions() {
    fillSelect(document.getElementById("personalitySelect"), state.options.personalities || []);
    fillSelect(document.getElementById("bodyTypeSelect"), state.options.body_types || []);
  }

  function renderUserEditor() {
    const user = state.currentUser;
    if (!user) {
      emptyUserState.classList.remove("hidden");
      userEditor.classList.add("hidden");
      return;
    }
    emptyUserState.classList.add("hidden");
    userEditor.classList.remove("hidden");
    document.getElementById("editorTitle").textContent = user.uid ? `用户 ${user.uid}` : "新用户";
    document.getElementById("editorSub").textContent = user.catgirl_enabled && user.catgirl ? `${user.catgirl.name || "猫娘"}｜${user.cat_summary?.stage_name || "阶段待计算"}` : "未启用猫娘档案";
    document.getElementById("editUid").value = user.uid || "";
    document.getElementById("editUid").readOnly = Boolean(user.uid && state.users.some((row) => row.uid === user.uid));
    document.getElementById("editWallet").value = user.wallet ?? 0;
    document.getElementById("catgirlEnabled").checked = Boolean(user.catgirl_enabled);
    document.getElementById("pendingWorkEnabled").checked = Boolean(user.pending_work_enabled || user.catgirl?.pending_work);
    document.getElementById("catgirlFields").classList.toggle("disabled-block", !user.catgirl_enabled);
    document.getElementById("pendingWorkFields").classList.toggle("disabled-block", !document.getElementById("pendingWorkEnabled").checked);

    syncUserFields();
    syncCatFields();
    syncWorkFields();
    syncAdvancedJson();
    renderCatSummary();
  }

  function renderCatSummary() {
    const cat = state.currentUser?.catgirl;
    const summary = state.currentUser?.cat_summary || {};
    const values = cat
      ? [
          ["阶段", summary.stage_name || cat.stage || "-"],
          ["状态", summary.status || "-"],
          ["羁绊", summary.bond_score ?? "-"],
          ["相伴", `${summary.companion_days ?? 0} 天`],
        ]
      : [["档案", "未启用"]];
    document.getElementById("catSummary").innerHTML = values
      .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
  }

  function syncUserFields() {
    const user = state.currentUser || {};
    document.querySelectorAll("[data-user-path]").forEach((input) => {
      const value = getPath(user, input.dataset.userPath);
      setInputValue(input, value);
    });
  }

  function syncCatFields() {
    const cat = ensureCatDraft(false);
    document.querySelectorAll("[data-cat-path]").forEach((input) => {
      const value = cat ? getPath(cat, input.dataset.catPath) : "";
      setInputValue(input, value);
    });
  }

  function syncWorkFields() {
    const work = ensurePendingWork(false);
    document.querySelectorAll("[data-work-path]").forEach((input) => {
      const value = work ? getPath(work, input.dataset.workPath) : "";
      setInputValue(input, value);
    });
  }

  function syncAdvancedJson() {
    const user = state.currentUser || {};
    document.getElementById("signJson").value = stringifyJson(user.sign || {});
    document.getElementById("catJson").value = stringifyJson(user.catgirl || {});
    document.getElementById("itemsJson").value = stringifyJson(user.items || {});
    document.getElementById("pendingJson").value = stringifyJson(user.pending_adoption || {});
  }

  function handleUserFieldChange(event) {
    if (!state.currentUser) return;
    const target = event.target;
    if (target.id === "editUid") {
      state.currentUser.uid = target.value.trim();
      if (state.currentUser.catgirl) state.currentUser.catgirl.user = state.currentUser.uid;
    } else if (target.id === "editWallet") {
      state.currentUser.wallet = readInputValue(target);
    } else if (target.id === "catgirlEnabled") {
      state.currentUser.catgirl_enabled = target.checked;
      if (target.checked) ensureCatDraft(true);
    } else if (target.id === "pendingWorkEnabled") {
      state.currentUser.pending_work_enabled = target.checked;
      if (target.checked) ensurePendingWork(true);
      else if (state.currentUser.catgirl) delete state.currentUser.catgirl.pending_work;
    } else if (target.dataset.userPath) {
      setPath(state.currentUser, target.dataset.userPath, readInputValue(target));
    } else if (target.dataset.catPath) {
      const cat = ensureCatDraft(true);
      setPath(cat, target.dataset.catPath, readInputValue(target));
      if (state.currentUser.uid) cat.user = state.currentUser.uid;
    } else if (target.dataset.workPath) {
      const work = ensurePendingWork(true);
      setPath(work, target.dataset.workPath, readInputValue(target));
    }
    state.userDirty = true;
    if (target.id === "catgirlEnabled") {
      document.getElementById("catgirlFields").classList.toggle("disabled-block", !state.currentUser.catgirl_enabled);
    }
    if (target.id === "pendingWorkEnabled") {
      document.getElementById("pendingWorkFields").classList.toggle("disabled-block", !target.checked);
      syncWorkFields();
    }
    document.getElementById("editorTitle").textContent = state.currentUser.uid ? `用户 ${state.currentUser.uid}` : "新用户";
    document.getElementById("editorSub").textContent = state.currentUser.catgirl_enabled && state.currentUser.catgirl ? `${state.currentUser.catgirl.name || "猫娘"}｜保存后重新计算阶段` : "未启用猫娘档案";
    syncAdvancedJson();
    renderCatSummary();
  }

  function handleJsonChange(event) {
    if (!state.currentUser) return;
    const id = event.target.id;
    try {
      const parsed = parseJsonText(event.target.value, id === "pendingJson" ? {} : {});
      event.target.classList.remove("invalid");
      if (id === "signJson") state.currentUser.sign = parsed;
      if (id === "catJson") {
        state.currentUser.catgirl = parsed;
        state.currentUser.catgirl_enabled = Object.keys(parsed).length > 0;
        state.currentUser.pending_work_enabled = Boolean(parsed.pending_work);
        document.getElementById("catgirlEnabled").checked = state.currentUser.catgirl_enabled;
        document.getElementById("pendingWorkEnabled").checked = state.currentUser.pending_work_enabled;
        document.getElementById("catgirlFields").classList.toggle("disabled-block", !state.currentUser.catgirl_enabled);
        document.getElementById("pendingWorkFields").classList.toggle("disabled-block", !state.currentUser.pending_work_enabled);
        syncCatFields();
        syncWorkFields();
        renderCatSummary();
      }
      if (id === "itemsJson") state.currentUser.items = parsed;
      if (id === "pendingJson") state.currentUser.pending_adoption = Object.keys(parsed).length ? parsed : null;
      state.userDirty = true;
    } catch (error) {
      event.target.classList.add("invalid");
    }
  }

  async function saveUser() {
    if (!state.currentUser) return;
    let user;
    try {
      user = collectUserPayload();
    } catch (error) {
      showToast(error.message || "用户数据格式错误", true);
      return;
    }
    if (!user.uid) {
      showToast("用户 ID 不能为空", true);
      return;
    }
    saveUserBtn.disabled = true;
    try {
      const payload = await apiPost("users/save", user);
      state.currentUser = payload.user;
      state.summary = payload.summary || state.summary || {};
      state.userDirty = false;
      showToast(payload.message || "用户数据已保存");
      await loadUsers();
      renderUserEditor();
    } catch (error) {
      showToast(error.message || "用户保存失败", true);
    } finally {
      saveUserBtn.disabled = false;
    }
  }

  async function deleteCurrentUser() {
    const uidInput = document.getElementById("editUid");
    const uid = String(uidInput?.value || state.currentUser?.uid || "").trim();
    if (!uid) {
      state.currentUser = null;
      state.userDirty = false;
      renderUserEditor();
      showToast("没有可删除的用户数据");
      return;
    }
    const exists = state.users.some((row) => row.uid === uid);
    if (!exists) {
      state.currentUser = null;
      state.userDirty = false;
      renderUserEditor();
      showToast("未保存的用户草稿已关闭");
      return;
    }
    if (!await confirmDialog(`确定删除用户 ${uid} 的养猫插件数据？`)) return;
    deleteUserBtn.disabled = true;
    try {
      const payload = await apiPost("users/delete", { uid });
      state.currentUser = null;
      state.summary = payload.summary || state.summary || {};
      state.userDirty = false;
      showToast(payload.message || "用户数据已删除");
      await loadUsers();
      renderUserEditor();
    } catch (error) {
      showToast(error.message || "删除失败", true);
    } finally {
      deleteUserBtn.disabled = false;
    }
  }

  function collectUserPayload() {
    const user = state.currentUser;
    const sign = parseJsonText(document.getElementById("signJson").value, {});
    const catgirl = parseJsonText(document.getElementById("catJson").value, {});
    const items = parseJsonText(document.getElementById("itemsJson").value, {});
    const pending = parseJsonText(document.getElementById("pendingJson").value, {});
    const uid = document.getElementById("editUid").value.trim();
    const catEnabled = document.getElementById("catgirlEnabled").checked;
    if (catEnabled) {
      catgirl.user = uid;
      catgirl.pending_work = document.getElementById("pendingWorkEnabled").checked ? catgirl.pending_work || {} : undefined;
      if (catgirl.pending_work === undefined) delete catgirl.pending_work;
    }
    return {
      uid,
      wallet: Number(document.getElementById("editWallet").value || user.wallet || 0),
      sign,
      catgirl_enabled: catEnabled,
      catgirl,
      pending_work_enabled: document.getElementById("pendingWorkEnabled").checked,
      items,
      pending_adoption_enabled: Object.keys(pending).length > 0,
      pending_adoption: Object.keys(pending).length > 0 ? pending : null,
    };
  }

  function renderOverview() {
    if (!state.config) return;
    const summary = state.summary || {};
    const config = state.config;
    const metrics = [
      ["钱包用户", summary.wallet_users || 0],
      ["猫娘档案", summary.catgirls || 0],
      ["启用打工地点", `${summary.enabled_jobs || 0}/${summary.jobs || 0}`],
      ["启用商店道具", `${summary.enabled_shop_items || 0}/${summary.shop_items || 0}`],
      ["启用护理服务", `${summary.enabled_care_services || 0}/${summary.care_services || 0}`],
      ["启用互动动作", `${summary.enabled_interactions || 0}/${summary.interactions || 0}`],
      ["启用性格效果", `${summary.enabled_personalities || 0}/${summary.personalities || 0}`],
      ["启用食物", `${summary.enabled_foods || 0}/${summary.foods || 0}`],
      ["启用心愿模板", `${summary.enabled_daily_wish_templates || 0}/${summary.daily_wish_templates || 0}`],
      ["钱包总额", summary.wallet_total || 0],
      ["最高余额", summary.wallet_max || 0],
      ["待确认收养", summary.pending_adoptions || 0],
    ];
    document.getElementById("metrics").innerHTML = metrics
      .map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");

    const care = config.care || {};
    const wish = config.wish || {};
    const rules = [
      ["货币", config.economy?.coin_name || "宝石"],
      ["签到奖励", `${config.economy?.sign_min_reward || 0} - ${config.economy?.sign_max_reward || 0}`],
      ["每日打工奖励", `${config.economy?.daily_work_min_reward || 0} - ${config.economy?.daily_work_max_reward || 0}`],
      ["许愿概率", `${Math.round(Number(wish.probability || 0) * 100)}%`],
      ["许愿保底", `${wish.pity || 1} 次`],
      ["形象价格", wish.appearance_change_price || 0],
      ["图片比例", `${Math.round(Number(config.render?.output_image_scale ?? 0.85) * 100)}%`],
      ["饱食耗尽", `${formatHours(care.satiety_decay_minutes || 0)}`],
      ["离家判定", `${care.runaway_after_zero_hours || 0} 小时`],
      ["互动软上限", Number(care.interaction_daily_limit || 0) ? `${care.interaction_daily_limit} 次/天` : "不限"],
      ["互动冷却", Number(care.interaction_cooldown_seconds || 0) ? `${care.interaction_cooldown_seconds} 秒` : "无"],
      ["高精力打工", `${care.work_high_energy_threshold || 0}+ 精力`],
      ["礼物软上限", Number(config.shop?.gift_daily_limit || 0) ? `${config.shop.gift_daily_limit} 次/天` : "不限"],
      ["每日心愿", config.daily_wishes?.enabled === false ? "关闭" : "开启"],
      ["心愿刷新", `${config.daily_wishes?.refresh_at_hour ?? 4}:00`],
    ];
    document.getElementById("ruleGrid").innerHTML = rules
      .map(([label, value]) => `<div class="rule"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
      .join("");
  }

  function renderDailyEvents() {
    const events = state.config.economy.daily_work_events || [];
    document.getElementById("dailyEvents").innerHTML = events
      .map(
        (_, index) => `
          <div class="text-row">
            <input data-path="economy.daily_work_events.${index}" type="text" maxlength="80" />
            <button class="icon-button" data-action="remove" data-list="economy.daily_work_events" data-index="${index}" type="button" title="删除">X</button>
          </div>`
      )
      .join("");
  }

  function renderFoods() {
    const rows = state.config.feed.foods || [];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="feed.foods.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="feed.foods.${index}.name" type="text" maxlength="30" /></td>
            <td><input data-path="feed.foods.${index}.verb" type="text" maxlength="8" class="narrow" /></td>
            <td><input data-path="feed.foods.${index}.cost_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="feed.foods.${index}.cost_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="feed.foods" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("foodsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>名称</th><th>动作</th><th>费用下限</th><th>费用上限</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderJobs() {
    const rows = state.config.work.jobs || [];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="work.jobs.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="work.jobs.${index}.id" type="text" maxlength="40" /></td>
            <td><input data-path="work.jobs.${index}.name" type="text" maxlength="40" /></td>
            <td><input data-path="work.jobs.${index}.reward_min" type="number" min="1" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.reward_max" type="number" min="1" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.duration_minutes" type="number" min="1" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.unlock_cost" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.min_stage" type="number" min="0" max="6" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.energy_cost" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.satiety_cost" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.mood_cost" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.growth_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.growth_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.intimacy_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.intimacy_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="work.jobs.${index}.mood_reward" type="number" min="0" step="0.1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="work.jobs" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("jobsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>地点</th><th>报酬下限</th><th>报酬上限</th><th>分钟</th><th>解锁费</th><th>阶段</th><th>精力</th><th>饱食</th><th>心情</th><th>成长下限</th><th>成长上限</th><th>亲密下限</th><th>亲密上限</th><th>完成心情</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderShopItems() {
    const rows = state.config.shop?.items || [];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="shop.items.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="shop.items.${index}.id" type="text" maxlength="40" /></td>
            <td><input data-path="shop.items.${index}.name" type="text" maxlength="40" /></td>
            <td><input data-path="shop.items.${index}.category" type="text" maxlength="20" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.price" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.effect" type="text" maxlength="24" class="narrow" /></td>
            <td><textarea data-path="shop.items.${index}.description" maxlength="120"></textarea></td>
            <td><input data-path="shop.items.${index}.satiety_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.mood_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.mood_min" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.mood_max" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.health_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.energy_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.growth_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.growth_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.intimacy_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.intimacy_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.multiplier" type="number" min="0" step="0.01" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.daily_limit" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.items.${index}.weekly_limit" type="number" min="0" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="shop.items" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("shopItemsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>名称</th><th>分类</th><th>价格</th><th>效果</th><th>描述</th><th>饱食</th><th>心情</th><th>礼物心情下限</th><th>礼物心情上限</th><th>健康</th><th>精力</th><th>成长下限</th><th>成长上限</th><th>亲密下限</th><th>亲密上限</th><th>倍率</th><th>日限</th><th>周限</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderCareServices() {
    const rows = state.config.shop?.care_services || [];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="shop.care_services.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="shop.care_services.${index}.id" type="text" maxlength="40" /></td>
            <td><input data-path="shop.care_services.${index}.name" type="text" maxlength="40" /></td>
            <td><input data-path="shop.care_services.${index}.category" type="text" maxlength="20" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.base_price" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.price_per_missing" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.target_health" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.satiety_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.mood_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.health_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.energy_add" type="number" min="-100" max="100" step="1" class="narrow" /></td>
            <td><input data-path="shop.care_services.${index}.cooldown_seconds" type="number" min="0" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="shop.care_services" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("careServicesTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>名称</th><th>分类</th><th>基础价</th><th>缺失价</th><th>目标健康</th><th>饱食</th><th>心情</th><th>健康</th><th>精力</th><th>冷却秒</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderInteractions() {
    const rows = state.config.interactions.effects || [];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="interactions.effects.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="interactions.effects.${index}.command" type="text" maxlength="20" /></td>
            <td><textarea data-path="interactions.effects.${index}.text" maxlength="120"></textarea></td>
            <td><input data-path="interactions.effects.${index}.mood_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.mood_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.intimacy_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.intimacy_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.growth_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.growth_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.energy_cost" type="number" min="0" max="100" step="1" class="narrow" /></td>
            <td><input data-path="interactions.effects.${index}.min_stage" type="number" min="0" max="6" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="interactions.effects" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("interactionsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>命令</th><th>文本</th><th>心情下限</th><th>心情上限</th><th>亲密下限</th><th>亲密上限</th><th>成长下限</th><th>成长上限</th><th>精力</th><th>阶段</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderPersonalities() {
    const rows = state.config.personalities?.effects || [];
    const columns = [
      ["satiety_decay_multiplier", "饱食消耗"],
      ["mood_decay_multiplier", "心情下降"],
      ["energy_recovery_multiplier", "精力恢复"],
      ["health_recovery_multiplier", "健康恢复"],
      ["feed_satiety_multiplier", "喂食饱食"],
      ["feed_mood_multiplier", "喂食心情"],
      ["feed_growth_multiplier", "喂食成长"],
      ["feed_intimacy_multiplier", "喂食亲密"],
      ["work_reward_multiplier", "打工报酬"],
      ["work_energy_cost_multiplier", "打工精力"],
      ["work_growth_multiplier", "打工成长"],
      ["work_intimacy_multiplier", "打工亲密"],
      ["interaction_mood_multiplier", "互动心情"],
      ["interaction_growth_multiplier", "互动成长"],
      ["interaction_intimacy_multiplier", "互动亲密"],
      ["interaction_energy_cost_multiplier", "互动精力"],
    ];
    const head = `<tr><th>启用</th><th>性格</th>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>`;
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="personalities.effects.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="personalities.effects.${index}.name" type="text" readonly /></td>
            ${columns
              .map(
                ([key]) => `<td><input data-path="personalities.effects.${index}.${key}" type="number" min="0" step="0.01" class="narrow" /></td>`
              )
              .join("")}
          </tr>`
      )
      .join("");
    document.getElementById("personalitiesTable").innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  function renderDailyWishes() {
    const rows = state.config.daily_wishes?.templates || [];
    const wishTypeOptions = ["feed", "interact", "cat_work", "buy_gift", "care"];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="daily_wishes.templates.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="daily_wishes.templates.${index}.id" type="text" maxlength="40" /></td>
            <td><input data-path="daily_wishes.templates.${index}.name" type="text" maxlength="40" /></td>
            <td><select data-path="daily_wishes.templates.${index}.type">${wishTypeOptions.map((t) => `<option value="${escapeAttr(t)}">${escapeHtml(t)}</option>`).join("")}</select></td>
            <td><input data-path="daily_wishes.templates.${index}.target_name" type="text" maxlength="40" placeholder="可留空" /></td>
            <td><input data-path="daily_wishes.templates.${index}.target" type="number" min="1" step="1" class="narrow" /></td>
            <td><textarea data-path="daily_wishes.templates.${index}.text" maxlength="140"></textarea></td>
            <td><input data-path="daily_wishes.templates.${index}.reward_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.reward_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.intimacy_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.intimacy_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.growth_min" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.growth_max" type="number" min="0" step="1" class="narrow" /></td>
            <td><input data-path="daily_wishes.templates.${index}.min_stage" type="number" min="0" max="6" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="daily_wishes.templates" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("dailyWishesTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>名称</th><th>类型</th><th>具体目标</th><th>目标次数</th><th>心愿文本</th><th>宝石下限</th><th>宝石上限</th><th>亲密下限</th><th>亲密上限</th><th>成长下限</th><th>成长上限</th><th>阶段</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderEvents() {
    renderEventItems();
    renderWorkFinishTexts();
    renderPersonalityEvents();
  }

  function renderEventItems() {
    const rows = state.config.events?.items || [];
    const anchorOptions = ["sign", "daily_work", "feed", "cat_work_finish", "interact"];
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="events.items.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="events.items.${index}.id" type="text" maxlength="40" /></td>
            <td><select data-path="events.items.${index}.anchor">${anchorOptions.map((a) => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join("")}</select></td>
            <td><input data-path="events.items.${index}.prob" type="number" min="0" max="1" step="0.01" class="narrow" /></td>
            <td><textarea data-path="events.items.${index}.text" maxlength="160"></textarea></td>
            <td><input data-path="events.items.${index}.coin_min" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.coin_max" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.mood" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.energy" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.intimacy" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.growth" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.items.${index}.min_stage" type="number" min="0" max="6" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="events.items" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("eventsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>锚点</th><th>概率</th><th>文本</th><th>宝石下限</th><th>宝石上限</th><th>心情</th><th>精力</th><th>亲密</th><th>成长</th><th>阶段</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function renderWorkFinishTexts() {
    renderTextList("workSurpriseTexts", "events.work_finish.surprise.texts", 160);
    renderTextList("workAccidentTexts", "events.work_finish.accident.texts", 160);
  }

  function renderTextList(containerId, path, maxLength) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const rows = getPath(state.config, path);
    const values = Array.isArray(rows) ? rows : [];
    container.innerHTML = values
      .map(
        (_, index) => `
          <div class="text-row">
            <input data-path="${escapeAttr(path)}.${index}" type="text" maxlength="${maxLength}" />
            <button class="icon-button" data-action="remove" data-list="${escapeAttr(path)}" data-index="${index}" type="button" title="删除">X</button>
          </div>`
      )
      .join("");
  }

  function renderPersonalityEvents() {
    const rows = state.config.events?.personality_events || [];
    const personalities = state.options?.personalities || [];
    const anchorOptions = ["interact", "feed", "cat_work_finish"];
    const personalityCells = personalities.length
      ? personalities.map((p) => `<option value="${escapeAttr(p)}">${escapeHtml(p)}</option>`).join("")
      : `<option value=""></option>`;
    const body = rows
      .map(
        (_, index) => `
          <tr>
            <td class="checkbox-cell"><input data-path="events.personality_events.${index}.enabled" type="checkbox" /></td>
            <td><input data-path="events.personality_events.${index}.id" type="text" maxlength="40" /></td>
            <td><select data-path="events.personality_events.${index}.personality">${personalityCells}</select></td>
            <td><select data-path="events.personality_events.${index}.anchor">${anchorOptions.map((a) => `<option value="${escapeAttr(a)}">${escapeHtml(a)}</option>`).join("")}</select></td>
            <td><input data-path="events.personality_events.${index}.prob" type="number" min="0" max="1" step="0.01" class="narrow" /></td>
            <td><textarea data-path="events.personality_events.${index}.text" maxlength="160"></textarea></td>
            <td><input data-path="events.personality_events.${index}.mood" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.personality_events.${index}.energy" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.personality_events.${index}.intimacy" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.personality_events.${index}.growth" type="number" step="1" class="narrow" /></td>
            <td><input data-path="events.personality_events.${index}.min_stage" type="number" min="0" max="6" step="1" class="narrow" /></td>
            <td><button class="icon-button" data-action="remove" data-list="events.personality_events" data-index="${index}" type="button" title="删除">X</button></td>
          </tr>`
      )
      .join("");
    document.getElementById("personalityEventsTable").innerHTML = `
      <table>
        <thead><tr><th>启用</th><th>ID</th><th>性格</th><th>锚点</th><th>概率</th><th>文本</th><th>心情</th><th>精力</th><th>亲密</th><th>成长</th><th>阶段</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  function handleFieldChange(event) {
    const target = event.target;
    if (!target || !target.dataset || !target.dataset.path || !state.config) return;
    const value = readInputValue(target);
    setPath(state.config, target.dataset.path, value);
    markDirty();
  }

  function handleAction(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "select-user") {
      selectUser(button.dataset.uid).catch((e) => showToast(e.message || "操作失败", true));
      return;
    }
    if (!state.config) return;
    if (action === "remove") {
      removeAt(button.dataset.list, Number(button.dataset.index));
      renderAll();
      markDirty();
    }
  }

  document.getElementById("addDailyEvent").addEventListener("click", () => {
    ensureArray("economy.daily_work_events").push("你认真完成了今天的打工。");
    renderAll();
    markDirty();
  });

  document.getElementById("addFood").addEventListener("click", () => {
    ensureArray("feed.foods").push({ name: "新食物", cost_min: 22, cost_max: 40, verb: "吃", enabled: true });
    renderAll();
    markDirty();
  });

  document.getElementById("addJob").addEventListener("click", () => {
    const id = `job_${Date.now()}`;
    ensureArray("work.jobs").push({
      id,
      name: "新打工地点",
      reward_min: 80,
      reward_max: 150,
      duration_minutes: 45,
      energy_cost: 18,
      satiety_cost: 7,
      mood_cost: 2,
      growth_min: 4,
      growth_max: 7,
      intimacy_min: 1,
      intimacy_max: 2,
      mood_reward: 1,
      unlock_cost: 0,
      min_stage: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addShopItem").addEventListener("click", () => {
    const id = `item_${Date.now()}`;
    ensureArray("shop.items").push({
      id,
      name: "新道具",
      category: "道具",
      price: 120,
      description: "自定义商店道具。",
      effect: "instant",
      satiety_add: 0,
      mood_add: 0,
      mood_min: 0,
      mood_max: 0,
      health_add: 0,
      energy_add: 0,
      growth_min: 0,
      growth_max: 0,
      intimacy_min: 0,
      intimacy_max: 0,
      multiplier: 1,
      daily_limit: 0,
      weekly_limit: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addCareService").addEventListener("click", () => {
    const id = `care_${Date.now()}`;
    ensureArray("shop.care_services").push({
      id,
      name: "新护理服务",
      category: "护理",
      base_price: 180,
      price_per_missing: 0,
      target_health: 0,
      satiety_add: 0,
      mood_add: 6,
      health_add: 0,
      energy_add: 20,
      cooldown_seconds: 28800,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addInteraction").addEventListener("click", () => {
    ensureArray("interactions.effects").push({
      command: "新互动",
      text: "你陪她玩了一会儿。",
      mood_min: 4,
      mood_max: 8,
      intimacy_min: 2,
      intimacy_max: 5,
      growth_min: 2,
      growth_max: 4,
      energy_cost: 0,
      min_stage: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addDailyWish").addEventListener("click", () => {
    const id = `wish_${Date.now()}`;
    ensureArray("daily_wishes.templates").push({
      id,
      name: "新心愿",
      type: "interact",
      target_name: "新互动",
      target: 1,
      text: "她今天有一个小心愿。",
      reward_min: 30,
      reward_max: 60,
      intimacy_min: 2,
      intimacy_max: 5,
      growth_min: 1,
      growth_max: 3,
      min_stage: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addWorkSurpriseText").addEventListener("click", () => {
    ensureArray("events.work_finish.surprise.texts").push("她今天遇到了惊喜奇遇，带回了额外报酬。");
    renderAll();
    markDirty();
  });

  document.getElementById("addWorkAccidentText").addEventListener("click", () => {
    ensureArray("events.work_finish.accident.texts").push("她今天打工时遇到了一点小意外，拿到补偿后心情有些低落。");
    renderAll();
    markDirty();
  });

  document.getElementById("addEvent").addEventListener("click", () => {
    const id = `event_${Date.now()}`;
    ensureArray("events.items").push({
      id,
      anchor: "sign",
      prob: 0.10,
      text: "今日奇遇发生了。",
      coin_min: 0,
      coin_max: 0,
      mood: 0,
      energy: 0,
      intimacy: 0,
      growth: 0,
      min_stage: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  document.getElementById("addPersonalityEvent").addEventListener("click", () => {
    const id = `pevent_${Date.now()}`;
    ensureArray("events.personality_events").push({
      id,
      personality: firstOption("personalities", "温柔"),
      anchor: "interact",
      prob: 0.08,
      text: "性格专属奇遇。",
      mood: 2,
      energy: 0,
      intimacy: 2,
      growth: 1,
      min_stage: 0,
      enabled: true,
    });
    renderAll();
    markDirty();
  });

  function switchTab(tabName) {
    document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tabName));
    document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabName));
  }

  function syncFields(root) {
    root.querySelectorAll("[data-path]").forEach((input) => {
      const value = getPath(state.config, input.dataset.path);
      if (input.type === "checkbox") {
        input.checked = Boolean(value);
      } else {
        input.value = value ?? "";
      }
    });
  }

  function readInputValue(input) {
    if (input.type === "checkbox") return input.checked;
    if (input.type === "number") {
      const value = input.value.trim();
      return value === "" ? 0 : Number(value);
    }
    return input.value;
  }

  function setInputValue(input, value) {
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
    } else {
      input.value = value ?? "";
    }
  }

  function ensureCatDraft(create) {
    if (!state.currentUser) return null;
    if (!state.currentUser.catgirl && create) {
      const now = Math.floor(Date.now() / 1000);
      state.currentUser.catgirl = {
        schema_version: 2,
        weight_unit: "斤",
        user: state.currentUser.uid || "",
        name: "猫娘",
        personality: firstOption("personalities", "温柔"),
        stage: 0,
        growth: 0,
        intimacy: 0,
        satiety: 80,
        mood: 85,
        health: 90,
        energy: 80,
        body_type: firstOption("body_types", "匀称"),
        ideal_weight: 60,
        weight: 60,
        created_at: now,
        last_decay: now,
        care_stats: {},
        unlocks: { work_jobs: [] },
        buffs: {},
        care_cooldowns: {},
        gift_stats: {},
      };
    }
    return state.currentUser.catgirl || null;
  }

  function ensurePendingWork(create) {
    const cat = ensureCatDraft(create);
    if (!cat) return null;
    if (!cat.pending_work && create) {
      const now = Math.floor(Date.now() / 1000);
      cat.pending_work = {
        job: "猫咖服务员",
        started_at: now,
        finish_at: now + 2700,
        duration: 2700,
        reward: 80,
        growth_add: 4,
        intimacy_add: 1,
        mood_reward: 1,
      };
    }
    return cat.pending_work || null;
  }

  function fillSelect(select, rows) {
    if (!select) return;
    const current = select.value;
    select.innerHTML = (rows || []).map((value) => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`).join("");
    if (current) select.value = current;
  }

  function firstOption(key, fallback) {
    const rows = state.options?.[key] || [];
    return rows[0] || fallback;
  }

  function stringifyJson(value) {
    return JSON.stringify(value || {}, null, 2);
  }

  function parseJsonText(text, fallback) {
    const raw = String(text || "").trim();
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("JSON 必须是对象");
    }
    return parsed;
  }

  function formatNumber(value, digits = 0) {
    const num = Number(value || 0);
    if (!Number.isFinite(num)) return "0";
    return num.toFixed(digits);
  }

  function ensureArray(path) {
    let value = getPath(state.config, path);
    if (!Array.isArray(value)) {
      value = [];
      setPath(state.config, path, value);
    }
    return value;
  }

  function removeAt(path, index) {
    const rows = getPath(state.config, path);
    if (Array.isArray(rows) && index >= 0 && index < rows.length) {
      rows.splice(index, 1);
    }
  }

  function getPath(root, path) {
    return parsePath(path).reduce((current, key) => (current == null ? undefined : current[key]), root);
  }

  function setPath(root, path, value) {
    const parts = parsePath(path);
    let current = root;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const key = parts[i];
      const nextKey = parts[i + 1];
      if (current[key] == null) {
        current[key] = typeof nextKey === "number" ? [] : {};
      }
      current = current[key];
    }
    current[parts[parts.length - 1]] = value;
  }

  function parsePath(path) {
    return String(path)
      .split(".")
      .map((part) => (/^\d+$/.test(part) ? Number(part) : part));
  }

  function markDirty() {
    state.dirty = true;
    setStatus("未保存", true);
  }

  function setStatus(text, dirty) {
    saveState.textContent = text;
    saveState.classList.toggle("dirty", Boolean(dirty));
  }

  function showToast(message, error = false) {
    toast.textContent = message;
    toast.classList.toggle("error", Boolean(error));
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2600);
  }

  // 内联确认对话框（替代 window.confirm，因 iframe sandbox 缺少 allow-modals）
  function confirmDialog(message) {
    return new Promise((resolve) => {
      const existing = document.querySelector(".confirm-overlay");
      if (existing) existing.remove();

      const overlay = document.createElement("div");
      overlay.className = "confirm-overlay";

      const card = document.createElement("div");
      card.className = "confirm-card";

      const msg = document.createElement("p");
      msg.className = "confirm-message";
      msg.textContent = message;

      const buttons = document.createElement("div");
      buttons.className = "confirm-buttons";

      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "ghost-button";
      cancelBtn.textContent = "取消";

      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "danger-button";
      okBtn.textContent = "确定";

      buttons.appendChild(cancelBtn);
      buttons.appendChild(okBtn);
      card.appendChild(msg);
      card.appendChild(buttons);
      overlay.appendChild(card);
      document.body.appendChild(overlay);

      // 动画入场
      requestAnimationFrame(() => overlay.classList.add("open"));

      const closeWith = (result) => {
        overlay.classList.remove("open");
        overlay.addEventListener("transitionend", () => overlay.remove(), { once: true });
        // 没有 transitionend 时的回退
        setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, 260);
        resolve(result);
      };

      okBtn.addEventListener("click", () => closeWith(true));
      cancelBtn.addEventListener("click", () => closeWith(false));
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeWith(false);
      });
    });
  }

  function formatHours(minutes) {
    const hours = Number(minutes || 0) / 60;
    if (!Number.isFinite(hours)) return "0 小时";
    return `${hours.toFixed(hours % 1 === 0 ? 0 : 1)} 小时`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  async function apiGet(path, params) {
    return apiRequest(path, "GET", undefined, params);
  }

  async function apiPost(path, body) {
    return apiRequest(path, "POST", body);
  }

  async function apiRequest(path, method, body, params) {
    const bridge = await waitForBridge();
    let payload;
    if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
      const endpoint = `page/${String(path).replace(/^\/+/, "")}`.replace(/\/+/g, "/");
      payload = method === "GET" ? await bridge.apiGet(endpoint, params || {}) : await bridge.apiPost(endpoint, body || {});
    } else if (new URLSearchParams(window.location.search).get("debug_http") === "1") {
      const url = new URL(`${HTTP_API}/${String(path).replace(/^\/+/, "")}`, window.location.origin);
      Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, value));
      const response = await fetch(url.toString(), {
        method,
        cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      payload = await response.json();
    } else {
      throw new Error("未检测到 AstrBot 插件页面桥接");
    }
    return unwrapPayload(payload);
  }

  async function waitForBridge() {
    for (let i = 0; i < 24; i += 1) {
      const bridge = getBridge();
      if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") return bridge;
      await sleep(80);
    }
    return null;
  }

  function getBridge() {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    try {
      if (window.parent && window.parent !== window && window.parent.AstrBotPluginPage) {
        return window.parent.AstrBotPluginPage;
      }
    } catch (error) {
      return null;
    }
    return null;
  }

  function unwrapPayload(payload) {
    if (typeof payload === "string") {
      payload = JSON.parse(payload);
    }
    if (!payload || payload.success === false) {
      throw new Error(payload?.error || "请求失败");
    }
    return payload.data ?? payload;
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }
})();
