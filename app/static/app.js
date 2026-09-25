/* Address Matcher - vanilla JS front end. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const STATUS_LABEL = {
    auto_matched: "自动命中",
    needs_review: "待确认",
    no_match: "无匹配",
    reviewed: "已复核",
};

const DEMO_QUERIES = [
    "123 Main Street, New York, NY 10001",
    "1000 Lake Dr, Austin, TX",
    "45 Hillcrest Rd, Dallas, Texas 75201",
    "99 SW 26th Ave Unit 7B, Portland OR",
    "北京東路８８號 3楼, 上海",
    "742 Evergreen Tce, Springfield",
].join("\n");

// ------------------------------------------------------------------ helpers

function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function api(path, options = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
}

function pct(value) {
    return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

// ------------------------------------------------------- review claim leases

// Tokens acquired in this browser session, keyed by match log id.  Persisted
// to localStorage so a page refresh does not lose a lease the server still
// considers ours.
function loadClaimTokens() {
    try {
        return JSON.parse(localStorage.getItem("reviewClaimTokens") || "{}");
    } catch (err) {
        return {};
    }
}

const claimTokens = loadClaimTokens();

function saveClaimTokens() {
    localStorage.setItem("reviewClaimTokens", JSON.stringify(claimTokens));
}

function reviewerName() {
    const input = $("#reviewer-name");
    return input ? input.value.trim() : "";
}

function ensureReviewerName() {
    const name = reviewerName();
    if (name) return name;
    const input = $("#reviewer-name");
    if (input) {
        input.focus();
        alert("请先在页面上方输入复核员姓名，再领取待确认记录。");
    }
    return "";
}

function fmtLeaseTime(iso) {
    if (!iso) return "";
    // Leases are absolute UTC instants; render them in the viewer's timezone.
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
        ? iso
        : d.toLocaleString([], { hour12: false });
}

function claimRemainingMs(item) {
    if (!item || !item.claim_expires_at) return 0;
    return new Date(item.claim_expires_at).getTime() - Date.now();
}

// Occupancy state of a pending item:
//   free       - no live lease
//   mine       - live lease held by this browser (its token matches ours)
//   claimed    - live lease held by someone else, or our stored token is stale
function claimState(item) {
    if (!item || !item.claim_expires_at || claimRemainingMs(item) <= 0) return "free";
    return item.claim_token && claimTokens[item.id] === item.claim_token
        ? "mine"
        : "claimed";
}

async function claimRecord(id) {
    const name = ensureReviewerName();
    if (!name) return false;
    const data = await api(`/api/matches/${id}/claim`, {
        method: "POST",
        body: JSON.stringify({ reviewer: name }),
    });
    claimTokens[id] = data.token;
    saveClaimTokens();
    return true;
}

async function releaseRecord(id, token) {
    await api(`/api/matches/${id}/release`, {
        method: "POST",
        body: JSON.stringify({ token }),
    });
    delete claimTokens[id];
    saveClaimTokens();
}

// Lease bar rendered above the action buttons of every pending record.
function renderClaimBar(item) {
    if (item.status !== "needs_review") return "";
    const state = claimState(item);
    if (state === "free") {
        return `
        <div class="claim-bar free" data-claim-id="${item.id}">
            <span class="claim-state free">未领取</span>
            <button class="tiny confirm btn-claim" type="button">🔒 领取十分钟租约</button>
        </div>`;
    }
    const mine = state === "mine";
    return `
    <div class="claim-bar ${mine ? "mine" : "other"}" data-claim-id="${item.id}"
         data-expires="${escapeHtml(item.claim_expires_at)}">
        <span class="claim-state ${mine ? "mine" : "other"}">
            ${mine ? "🔓 你已领取" : `🔒 由「${escapeHtml(item.claimed_by)}」领取`}
        </span>
        <span class="muted claim-expiry" title="租约到期时间 ${escapeHtml(item.claim_expires_at)}">
            到期 ${fmtLeaseTime(item.claim_expires_at)}（剩余 <span class="claim-remain">…</span>）
        </span>
        ${mine ? `<button class="tiny reject btn-release" type="button">释放</button>`
               : `<span class="muted">等待释放或租约到期后可领取</span>`}
    </div>`;
}

function refreshClaimCountdowns() {
    $$(".claim-bar[data-claim-id]").forEach((bar) => {
        const remain = bar.querySelector(".claim-remain");
        if (!remain) return;
        const ms = new Date(bar.dataset.expires).getTime() - Date.now();
        if (ms <= 0) {
            remain.textContent = "已过期";
            return;
        }
        const totalSec = Math.ceil(ms / 1000);
        const mm = Math.floor(totalSec / 60);
        const ss = String(totalSec % 60).padStart(2, "0");
        remain.textContent = `${mm}分${ss}秒`;
    });
}

setInterval(refreshClaimCountdowns, 1000);

// ------------------------------------------------------------------- render

function renderTokens(cand) {
    // Group token chips by component so street and locality hits are distinct.
    const groups = [
        { key: "street_tokens", label: "道路", tokens: cand.street_tokens },
        { key: "locality_tokens", label: "地区", tokens: cand.locality_tokens },
    ];
    const rendered = new Set();
    const parts = [];
    groups.forEach((group) => {
        if (!group.tokens) return;
        const exact = new Set(group.tokens.matched.map((m) => m.candidate));
        group.tokens.matched.forEach((m) => {
            const cls = m.similarity >= 0.999 ? "tok" : "tok fuzzy";
            const title = `${group.label}${m.similarity >= 0.999 ? "完全命中" : "近似命中"}: “${m.query}” → “${m.candidate}” (${pct(m.similarity)})`;
            rendered.add(m.candidate);
            parts.push(`<span class="${cls}" title="${escapeHtml(title)}">${escapeHtml(m.candidate)}</span>`);
        });
        group.tokens.query_only.forEach((t) => {
            parts.push(`<span class="tok miss-q" title="${group.label}：仅查询中出现">${escapeHtml(t)}</span>`);
        });
        group.tokens.candidate_only.forEach((t) => {
            if (rendered.has(t)) return;
            parts.push(`<span class="tok miss-c" title="${group.label}：仅标准地址中出现">${escapeHtml(t)}</span>`);
            rendered.add(t);
        });
    });
    return `<div class="tokens">${parts.join("") || '<span class="muted">无命中词元</span>'}</div>`;
}

function renderConflicts(conflicts) {
    if (!conflicts || conflicts.length === 0) return "";
    // Deduplicate identical messages.
    const seen = new Set();
    return conflicts.filter((c) => {
        const key = `${c.field}:${c.query}:${c.candidate}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    }).map((c) => `
        <div class="conflict">
            ⚠ ${escapeHtml(c.message)}：查询 <b>${escapeHtml(c.query)}</b>
            ↔ 候选 <b>${escapeHtml(c.candidate)}</b>
            （相似度 ${pct(c.similarity)}）
        </div>`).join("");
}

const CONSENSUS_STATE_HINT = {
    hit: "共识命中",
    conflict: "历史结论冲突",
    target_gone: "共识地址已失效",
    not_candidate: "共识地址不在候选",
    not_applied: "共识未改写原判",
    insufficient: "",
};

function renderConsensus(consensus) {
    if (!consensus) return "";
    const n = consensus.evidence_count || 0;
    const targetText = consensus.target
        ? `标准地址 #${consensus.target.id} ${escapeHtml(consensus.target.full_text || "")}`
        : (consensus.target_id ? `标准地址 #${consensus.target_id}` : "");
    if (consensus.applied) {
        return `
        <div class="consensus hit">
            <span class="consensus-badge">✅ 复核共识命中</span>
            <span class="consensus-meta">证据 <b>${n}</b> 条已复核记录一致确认 → ${targetText}</span>
            <div class="consensus-reason">${escapeHtml(consensus.reason || "")}</div>
        </div>`;
    }
    const label = CONSENSUS_STATE_HINT[consensus.state];
    if (!label) return "";
    return `
    <div class="consensus blocked">
        <span class="consensus-badge">⛔ ${escapeHtml(label)}</span>
        <span class="consensus-meta">证据 <b>${n}</b> 条${consensus.rejected_count ? `，拒绝 <b>${consensus.rejected_count}</b> 条` : ""}，至少需 <b>${consensus.required || 2}</b> 条同向确认</span>
        <div class="consensus-reason">${escapeHtml(consensus.reason || "")}</div>
    </div>`;
}

function renderCandidate(cand, rank, opts = {}) {
    const a = cand.address;
    const bestCls = rank === 0 ? " best" : "";
    const actionAttrs = opts.reviewable
        ? `data-log="${opts.logId}" data-address="${a.id}"`
        : "";
    // Records claimed by someone else show their candidates but cannot be
    // reviewed from this browser until the lease is released or expires.
    const disabledAttr = opts.reviewDisabled ? " disabled" : "";
    let ribbon = "";
    if (opts.consensusTargetId === a.id) {
        ribbon = `<span class="target-ribbon">共识指向</span>`;
    } else if (opts.targetRibbon && opts.targetRibbon.id === a.id) {
        ribbon = `<span class="target-ribbon">${escapeHtml(opts.targetRibbon.label)}</span>`;
    }
    return `
    <div class="candidate${bestCls}">
        <div class="addr">#${rank + 1} ${escapeHtml(a.full_text)} ${ribbon}</div>
        <div class="meta">
            标准地址 ID ${a.id}${a.external_id ? ` · 外部编号 ${escapeHtml(a.external_id)}` : ""}
            ${a.house ? ` · 门牌号 ${escapeHtml(a.house)}` : ""}
        </div>
        <div class="scores">
            <span class="score-chip">综合 <b>${pct(cand.score)}</b></span>
            <span class="score-chip">道路词元 <b>${pct(cand.street_f1)}</b></span>
            <span class="score-chip">地区词元 ${pct(cand.locality_f1)}</span>
            <span class="score-chip">门牌号 <b>${pct(cand.house_score)}</b></span>
        </div>
        ${renderTokens(cand)}
        ${renderConflicts(cand.conflicts)}
        ${opts.reviewable ? `
        <div class="review-actions">
            <button class="tiny confirm candidate-confirm" ${actionAttrs}${disabledAttrs}>确认此候选</button>
            ${rank !== 0 ? `<button class="tiny override candidate-override" ${actionAttrs}${disabledAttrs}>改派为该候选</button>` : ""}
        </div>` : ""}
    </div>`;
}

function renderResult(item, opts = {}) {
    const consensusTargetId = item.consensus && item.consensus.applied
        ? item.consensus.target_id
        : null;
    // Fresh match results are not claimed; re-rendered review items may be.
    const itemState = opts.reviewable ? claimState(item) : "free";
    const canReview = opts.reviewable && itemState === "mine";
    const reviewLocked = opts.reviewable && itemState === "claimed";
    const candidates = (item.candidates || [])
        .map((c, i) => renderCandidate(c, i, {
            ...opts,
            consensusTargetId,
            logId: item.match_id,
            reviewDisabled: opts.reviewable && !canReview,
        }))
        .join("");
    return `
    <div class="result">
        <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
        <div class="norm">
            归一化：${escapeHtml(item.normalized.text || "（空）")}
            · 门牌号 <b>${escapeHtml(item.normalized.house || "无")}</b>
            ${item.match_id ? ` · 记录 #${item.match_id}` : ""}
        </div>
        <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
        <span class="muted">${escapeHtml(item.reason)}</span>
        ${renderConsensus(item.consensus)}
        ${item.runner_up_id ? `<div class="muted" style="margin-top:4px">次高分候选 #${item.runner_up_id}：${pct(item.runner_up_score)}（领先 ${pct((item.best_score ?? 0) - (item.runner_up_score ?? 0))}）</div>` : ""}
        ${opts.reviewable ? renderClaimBar(item) : ""}
        ${candidates || '<div class="meta muted">标准地址表中没有任何候选</div>'}
        ${opts.reviewable && item.candidates.length > 0 ? `
        <div class="review-actions">
            <button class="tiny reject candidate-reject" data-log="${item.match_id}"${canReview ? "" : " disabled"}>标记为无匹配</button>
            ${!canReview ? `<span class="muted">${reviewLocked
                ? "该记录正被他人领取，暂不能复核"
                : "请先领取该记录，领取后十分钟内可提交复核"}</span>` : ""}
        </div>` : ""}
    </div>`;
}

// -------------------------------------------------------------------- tabs

$$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
        $$(".tab").forEach((b) => b.classList.remove("active"));
        $$(".panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        $(`#tab-${btn.dataset.tab}`).classList.add("active");
        if (btn.dataset.tab === "table") loadAddresses();
        if (btn.dataset.tab === "review") loadReview();
        if (btn.dataset.tab === "replay") loadReviewReplay();
        if (btn.dataset.tab === "gaps") loadCoverageGaps();
        if (btn.dataset.tab === "audit") loadAddressAudit();
        if (btn.dataset.tab === "experiment") {
            renderProfileEditor();
            loadExperimentHistory();
        }
    });
});

// ------------------------------------------------------------------ matching

$("#btn-demo").addEventListener("click", () => { $("#query").value = DEMO_QUERIES; });

// Items currently rendered in #results (match tab), keyed by match_id, so
// claim/release actions can refresh lease state without re-querying.
let resultItems = [];

function renderResultsList(items) {
    resultItems = items;
    $("#results").innerHTML = items
        .map((item) => renderResult(item, {
            reviewable: item.status === "needs_review",
        }))
        .join("");
}

$("#btn-match").addEventListener("click", async () => {
    const raw = $("#query").value.trim();
    if (!raw) return;
    const queries = raw.split("\n").map((q) => q.trim()).filter(Boolean);
    const btn = $("#btn-match");
    btn.disabled = true;
    $("#results").innerHTML = '<p class="muted">匹配中…</p>';
    try {
        if (queries.length === 1) {
            const item = await api("/api/match", {
                method: "POST",
                body: JSON.stringify({ query: queries[0], save: true }),
            });
            resultItems = [item];
            $("#results").innerHTML = renderResult(item, {
                reviewable: item.status === "needs_review",
            });
            $("#match-summary").textContent = "";
        } else {
            const data = await api("/api/match/batch", {
                method: "POST",
                body: JSON.stringify({ queries, save: true }),
            });
            const s = data.summary;
            $("#match-summary").innerHTML =
                `<span class="muted">共 ${s.total} 条：自动命中 ${s.auto_matched}，` +
                `待确认 ${s.needs_review}，无匹配 ${s.no_match}</span>`;
            renderResultsList(data.results);
        }
        refreshHealth();
        refreshGapsBadge();
    } catch (err) {
        $("#results").innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    } finally {
        btn.disabled = false;
    }
});

// Delegate review clicks from freshly rendered match results.
$("#results").addEventListener("click", onResultActionClick);

async function onReviewClick(event) {
    const btn = event.target.closest("button[data-log]");
    if (!btn || btn.disabled) return;
    const logId = Number(btn.dataset.log);
    const token = claimTokens[logId];
    if (!token) {
        alert("请先领取该记录，再提交复核。");
        return;
    }
    let body;
    if (btn.classList.contains("candidate-reject")) {
        body = { action: "rejected", token };
    } else {
        body = {
            action: btn.classList.contains("candidate-override") ? "overridden" : "confirmed",
            selected_id: Number(btn.dataset.address),
            token,
        };
    }
    btn.disabled = true;
    try {
        await api(`/api/matches/${logId}/review`, { method: "POST", body: JSON.stringify(body) });
        delete claimTokens[logId];
        saveClaimTokens();
        btn.textContent = "✓ 已记录";
        btn.classList.add("confirm");
        await refreshHealth();
        refreshReplayBadge();
        // If this card lives in the match-tab #results, refresh it to the
        // reviewed state rather than leaving the "已记录" button behind.
        if ($("#tab-match").classList.contains("active")) {
            const card = $("#results").querySelector(
                `[data-log="${logId}"]`)?.closest(".result");
            if (card) await refreshResultCard(logId);
        };
    } catch (err) {
        btn.disabled = false;
        alert(err.message);
        // A 409 (expired/stale token, or state drift): refresh the card from
        // the server so the buttons reflect the current occupancy.
        if (/409|令牌|租约|领取|重复/.test(err.message)) {
            refreshResultCard(logId);
        }
    }
}

// Re-fetch one record (used after a lease 409) and re-render its result card.
async function refreshResultCard(logId) {
    try {
        const item = await api(`/api/matches/${logId}`);
        // Match results need the normalized block; recover it from the cache.
        const cached = resultItems.find((it) => it.match_id === logId);
        const merged = cached && cached.normalized
            ? { ...cached, ...item }
            : item;
        resultItems = resultItems.map((it) =>
            it.match_id === logId ? merged : it);
        const cards = $$("#results .result");
        // Cards render in the same order as resultItems.
        const idx = resultItems.findIndex((it) => it.match_id === logId);
        if (cards[idx]) {
            const wrapper = document.createElement("div");
            wrapper.innerHTML = renderResult(merged, {
                reviewable: item.status === "needs_review",
            });
            cards[idx].replaceWith(wrapper.firstElementChild);
        }
    } catch (err) {
        // best effort
    }
}

// Claim / release buttons inside rendered match-result cards.
async function onResultActionClick(event) {
    const claimBtn = event.target.closest(".btn-claim");
    const releaseBtn = event.target.closest(".btn-release");
    if (!claimBtn && !releaseBtn) {
        await onReviewClick(event);
        return;
    }
    const bar = event.target.closest(".claim-bar");
    const logId = Number(bar.dataset.claimId);
    const btn = claimBtn || releaseBtn;
    btn.disabled = true;
    try {
        if (claimBtn) {
            const ok = await claimRecord(logId);
            if (!ok) {
                btn.disabled = false;
                return;
            }
        } else {
            await releaseRecord(logId, claimTokens[logId]);
        }
        await refreshResultCard(logId);
    } catch (err) {
        btn.disabled = false;
        alert(err.message);
        await refreshResultCard(logId);
    }
}

// ----------------------------------------------------------- address table

async function loadAddresses() {
    const wrap = $("#address-table-wrap");
    wrap.innerHTML = '<p class="muted">加载中…</p>';
    try {
        const data = await api("/api/addresses?limit=200");
        if (data.items.length === 0) {
            wrap.innerHTML = '<p class="muted">标准地址表为空，请上传 CSV 或加载示例数据。</p>';
            return;
        }
        const rows = data.items.map((a) => `
            <tr>
                <td>${a.id}</td>
                <td>${escapeHtml(a.full_text)}</td>
                <td>${escapeHtml(a.house || "")}</td>
                <td>${escapeHtml(a.postal_code || "")}</td>
                <td>${escapeHtml(a.external_id || "")}</td>
            </tr>`).join("");
        wrap.innerHTML = `
            <table>
                <thead><tr><th>ID</th><th>标准地址</th><th>门牌号</th><th>邮编</th><th>外部编号</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <p class="muted">共 ${data.total} 条${data.total > 200 ? "（仅显示前 200 条）" : ""}</p>`;
    } catch (err) {
        wrap.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-table").addEventListener("click", loadAddresses);

$("#btn-sample").addEventListener("click", async () => {
    try {
        const s = await api("/api/addresses/load-sample", { method: "POST" });
        $("#table-msg").innerHTML =
            `<span class="ok-msg">新增 ${s.inserted} 条，跳过重复 ${s.duplicates} 条</span>`;
        loadAddresses();
        refreshHealth();
        refreshAuditBadge();
        refreshReplayBadge();
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
    }
});

// ---------------------------------------------------- CSV preview / confirm

// The file picked for the current pending preview (kept so confirm can
// re-submit the exact same bytes to the server).
let pendingFile = null;
let pendingToken = null;

function clearPreview() {
    pendingFile = null;
    pendingToken = null;
    $("#csv-preview-panel").classList.add("hidden");
    $("#csv-preview-panel").innerHTML = "";
}

$("#csv-preview-mode").addEventListener("change", () => {
    clearPreview();
    $("#csv-file").value = "";
});

function csvForm(file) {
    const form = new FormData();
    form.append("file", file);
    return form;
}

function renderPreview(data) {
    const lines = data.invalid_lines.length
        ? data.invalid_lines.map((n) => `第 ${n} 行`).join("、")
        : "无";
    $("#csv-preview-panel").innerHTML = `
        <div class="preview-head">
            <span class="preview-title">📄 ${escapeHtml(data.filename)} 预览结果（共 ${data.total} 行）</span>
        </div>
        <div class="preview-counts">
            <span class="pv-chip valid">有效 ${data.valid}</span>
            <span class="pv-chip dup">重复 ${data.duplicates}</span>
            <span class="pv-chip invalid">无效 ${data.invalid}</span>
        </div>
        <div class="preview-lines">无效行（CSV 物理行号，表头为第 1 行）：${escapeHtml(lines)}</div>
        <p class="hint">
            确认时需重新上传同一文件；若文件内容或标准地址表在预览后发生变化，
            服务器将拒绝写入，需要重新预览。可先做<b>导入影响预演</b>：用本次有效行
            对最近 100 条不同查询做内存干跑（不写入任何数据）。
        </p>
        <div class="row">
            <button id="btn-csv-confirm" class="primary"${data.valid === 0 ? " disabled" : ""}>
                确认写入 ${data.valid} 条有效记录
            </button>
            <button id="btn-csv-impact" class="ghost"${data.valid === 0 ? " disabled" : ""}>
                🔬 影响预演
            </button>
            <button id="btn-csv-cancel" class="ghost">取消</button>
            <button id="btn-csv-repreview" class="ghost hidden">重新预览</button>
            <span id="csv-confirm-msg"></span>
        </div>
        <div id="csv-impact-panel" class="impact-panel hidden"></div>`;
    $("#csv-preview-panel").classList.remove("hidden");
}

async function createPreview(file) {
    const panel = $("#csv-preview-panel");
    panel.classList.remove("hidden");
    panel.innerHTML = '<p class="muted">正在校验预览…</p>';
    const res = await fetch("/api/addresses/preview", {
        method: "POST",
        body: csvForm(file),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "预览失败");
    pendingFile = file;
    pendingToken = data.preview_token;
    renderPreview(data);
}

$("#csv-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $("#table-msg").innerHTML = "";
    clearPreview();
    try {
        if ($("#csv-preview-mode").checked) {
            await createPreview(file);
            // Keep the filename in the input so users can see what is pending;
            // confirm re-reads it and re-submits the bytes.
        } else {
            await directUpload(file);
        }
    } catch (err) {
        $("#table-msg").innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
        clearPreview();
        event.target.value = "";
    }
});

async function directUpload(file) {
    const res = await fetch("/api/addresses/upload", {
        method: "POST",
        body: csvForm(file),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "上传失败");
    $("#table-msg").innerHTML =
        `<span class="ok-msg">${escapeHtml(file.name)}：新增 ${data.inserted} 条，` +
        `重复 ${data.duplicates} 条，无效 ${data.invalid} 条</span>`;
    $("#csv-file").value = "";
    loadAddresses();
    refreshHealth();
    refreshAuditBadge();
    refreshReplayBadge();
}

$("#csv-preview-panel").addEventListener("click", async (event) => {
    if (event.target.closest("#btn-csv-cancel")) {
        clearPreview();
        $("#csv-file").value = "";
        return;
    }
    if (event.target.closest("#btn-csv-repreview")) {
        if (pendingFile) await createPreview(pendingFile);
        return;
    }
    if (event.target.closest("#btn-csv-impact")) {
        await loadImpact();
        return;
    }
    const confirmBtn = event.target.closest("#btn-csv-confirm");
    if (!confirmBtn) return;

    // Confirm must re-submit the file alongside the preview token.
    const file = $("#csv-file").files[0];
    const msg = $("#csv-confirm-msg");
    if (!file) {
        msg.innerHTML = '<span class="err-msg">请重新选择同一个文件后再确认</span>';
        return;
    }
    confirmBtn.disabled = true;
    const form = csvForm(file);
    form.append("preview_token", pendingToken || "");
    try {
        const res = await fetch("/api/addresses/confirm", {
            method: "POST",
            body: form,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            // Digest/snapshot mismatch or expired voucher: require re-preview.
            msg.innerHTML =
                `<span class="err-msg">${escapeHtml(data.detail || "确认失败")}，` +
                `请点击「重新预览」后再确认。</span>`;
            $("#btn-csv-confirm").classList.add("hidden");
            $("#btn-csv-repreview").classList.remove("hidden");
            return;
        }
        const lines = (data.invalid_lines || []).length
            ? `，无效行：${data.invalid_lines.map((n) => `第 ${n} 行`).join("、")}`
            : "";
        $("#table-msg").innerHTML =
            `<span class="ok-msg">${escapeHtml(data.filename)}：确认写入 ${data.inserted} 条，` +
            `重复 ${data.duplicates} 条，无效 ${data.invalid} 条${lines}</span>`;
        clearPreview();
        $("#csv-file").value = "";
        loadAddresses();
        refreshHealth();
        refreshAuditBadge();
        refreshReplayBadge();
    } catch (err) {
        msg.innerHTML = `<span class="err-msg">${escapeHtml(err.message)}</span>`;
        confirmBtn.disabled = false;
    }
});

// ------------------------------------------------- import impact dry-run

const IMPACT_CATEGORY_LABEL = {
    improved: "改善",
    regressed: "退化",
    retargeted: "目标改派",
    unchanged: "不变",
};

function impactTargetText(target) {
    if (!target) return "无自动命中目标";
    if (target.kind === "new") return `CSV 第 ${target.csv_line} 行「${target.full_text}」`;
    return `#${target.id}「${target.full_text}」`;
}

function renderImpactVerdict(label, verdict) {
    const target = verdict.target
        ? `${escapeHtml(impactTargetText(verdict.target))}（综合 ${pct(verdict.score)}）`
        : "无自动命中目标";
    return `
    <div class="impact-verdict">
        <span class="impact-side">${label}</span>
        <span class="status ${verdict.status}">${STATUS_LABEL[verdict.status] || verdict.status}</span>
        ${verdict.consensus_applied ? '<span class="impact-consensus" title="复核共识命中改指">共识</span>' : ""}
        <span class="muted">${target}</span>
    </div>`;
}

function renderImpact(data) {
    const s = data.summary;
    const newAddr = data.new_addresses.map((a) =>
        `<span class="impact-new-chip">第 ${a.csv_line} 行 · ${escapeHtml(a.full_text)}</span>`
    ).join("");
    const items = data.items.map((it) => `
        <div class="impact-item">
            <div class="impact-head">
                <span class="impact-cat ${it.category}">${IMPACT_CATEGORY_LABEL[it.category] || it.category}</span>
                <span class="impact-query">🔍 ${escapeHtml(it.query)}</span>
                <span class="muted">最近记录 #${it.latest_match_id}</span>
            </div>
            <div class="impact-verdicts">
                ${renderImpactVerdict("导入前", it.before)}
                <span class="impact-arrow">→</span>
                ${renderImpactVerdict("导入后（预演）", it.after)}
            </div>
            <div class="impact-reason">判定：${escapeHtml(it.reason)}</div>
        </div>`).join("");
    $("#csv-impact-panel").innerHTML = `
        <div class="impact-summary">
            <span class="impact-cat improved">改善 ${s.improved}</span>
            <span class="impact-cat regressed">退化 ${s.regressed}</span>
            <span class="impact-cat retargeted">目标改派 ${s.retargeted}</span>
            <span class="impact-cat unchanged">不变 ${s.unchanged}</span>
            <span class="muted">共预演 ${s.total} 条不同查询（按日志时间倒序，上限 ${data.query_limit} 条）
                · ${escapeHtml(data.generated_at)}</span>
        </div>
        <div class="impact-new">
            <div class="muted">参与预演的 ${data.valid_rows} 条有效新地址（以 CSV 行号标识；无效及重复行不参与）：</div>
            <div class="impact-new-chips">${newAddr || '<span class="muted">无</span>'}</div>
        </div>
        ${items || '<p class="muted">暂无匹配日志可供预演；产生匹配记录后即可查看本次导入的影响。</p>'}
        <p class="hint">预演仅在内存中进行，不写入地址和匹配日志；确认导入仍走原有文件校验与事务。</p>`;
}

async function loadImpact() {
    const panel = $("#csv-impact-panel");
    panel.classList.remove("hidden");
    panel.innerHTML = '<p class="muted">正在用最近 100 条不同查询预演本次导入的影响…</p>';
    const btn = $("#btn-csv-impact");
    btn.disabled = true;
    try {
        const res = await fetch(
            `/api/addresses/preview/${encodeURIComponent(pendingToken || "")}/impact`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            // 404（凭证不存在/过期）或 409（地址表已变化）：凭证不可用，需重新预览。
            panel.innerHTML =
                `<p class="err-msg">${escapeHtml(data.detail || "预演失败")}，` +
                `请点击「重新预览」。</p>`;
            if (res.status === 404 || res.status === 409) {
                $("#btn-csv-confirm").classList.add("hidden");
                $("#btn-csv-impact").classList.add("hidden");
                $("#btn-csv-repreview").classList.remove("hidden");
            }
            return;
        }
        renderImpact(data);
    } catch (err) {
        panel.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    } finally {
        btn.disabled = false;
    }
}

// ------------------------------------------------------------- review queue

async function loadReview() {
    const status = $("#review-filter").value;
    const list = $("#review-list");
    list.innerHTML = '<p class="muted">加载中…</p>';
    if (status === "needs_review") {
        loadReviewGroups();
    } else {
        $("#review-groups").innerHTML = "";
    }
    try {
        const data = await api(`/api/matches?status=${encodeURIComponent(status)}&limit=100`);
        if (data.items.length === 0) {
            list.innerHTML = '<p class="muted">暂无记录。</p>';
            return;
        }
        list.innerHTML = data.items.map((item) => {
            const best = item.candidates[0];
            const consensusTargetId = item.consensus && item.consensus.applied
                ? item.consensus.target_id
                : null;
            const pending = item.status === "needs_review";
            const state = pending ? claimState(item) : "free";
            const canReview = pending && state === "mine";
            const heldByOther = pending && state === "claimed";
            const candidateHtml = item.candidates.length
                ? item.candidates.map((c, i) => renderCandidate(c, i, {
                    reviewable: pending,
                    reviewDisabled: !canReview,
                    logId: item.id,
                    consensusTargetId,
                })).join("")
                : '<div class="meta muted">无候选</div>';
            return `
            <div class="review-item">
                <div class="query-line">🔍 ${escapeHtml(item.query)}</div>
                <div class="muted">记录 #${item.id} · ${escapeHtml(item.created_at || "")}</div>
                <span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>
                ${item.review_action ? `<span class="muted">复核动作：${escapeHtml(item.review_action)}</span>` : ""}
                ${renderConsensus(item.consensus)}
                ${pending ? renderClaimBar(item) : ""}
                ${candidateHtml}
                ${pending ? `
                <div class="review-actions">
                    <button class="tiny reject candidate-reject" data-log="${item.id}"${canReview ? "" : " disabled"}>标记为无匹配</button>
                    ${!canReview ? `<span class="muted">${heldByOther
                        ? "该记录正被他人领取，暂不能复核"
                        : "请先领取该记录，领取后十分钟内可提交复核"}</span>` : ""}
                </div>` : ""}
            </div>`;
        }).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-review").addEventListener("click", loadReview);
$("#review-filter").addEventListener("change", loadReview);

// Reviewer name persists between reloads.
(function initReviewerName() {
    const saved = localStorage.getItem("reviewerName");
    if (saved) $("#reviewer-name").value = saved;
    $("#reviewer-name").addEventListener("change", (event) => {
        localStorage.setItem("reviewerName", event.target.value.trim());
    });
})();

$("#review-list").addEventListener("click", async (event) => {
    const claimBtn = event.target.closest(".btn-claim");
    const releaseBtn = event.target.closest(".btn-release");
    if (claimBtn || releaseBtn) {
        const bar = event.target.closest(".claim-bar");
        const logId = Number(bar.dataset.claimId);
        const btn = claimBtn || releaseBtn;
        btn.disabled = true;
        try {
            if (claimBtn) {
                const ok = await claimRecord(logId);
                if (!ok) {
                    btn.disabled = false;
                    return;
                }
            } else {
                await releaseRecord(logId, claimTokens[logId]);
            }
        } catch (err) {
            alert(err.message);
        }
        loadReview();
        return;
    }
    await onReviewClick(event);
    if (event.target.closest("button[data-log]")) loadReview();
});

// ------------------------------------------------- same-case merged review

// Groups fetched by the last loadReviewGroups() call, keyed lookup for actions.
let reviewGroups = [];

function renderGroup(group) {
    const queries = group.queries.map((m) => `
        <div class="gq">🔍 ${escapeHtml(m.query)}
            <span class="muted">记录 #${m.id}</span>
            ${m.claimed_by ? `
            <span class="group-claim" title="租约到期 ${escapeHtml(m.claim_expires_at || "")}">
                🔒 由「${escapeHtml(m.claimed_by)}」领取至 ${fmtLeaseTime(m.claim_expires_at)}
            </span>` : ""}
        </div>`).join("");
    const candidates = group.candidates
        .map((c, i) => renderCandidate(c, i, {}))
        .join("") || '<div class="meta muted">无候选</div>';
    const best = group.candidates[0];
    const options = group.candidates.map((c) =>
        `<option value="${c.address.id}">#${c.address.id} · ${pct(c.score)} · ` +
        `${escapeHtml(c.address.full_text)}</option>`).join("");
    const claimed = group.claimed_members || [];
    const groupWarn = claimed.length ? `
        <div class="group-claim-warn">
            ⛔ 组内有 ${claimed.length} 条记录仍被领取，整组复核将被服务端拒绝（避免绕过占用）：
            ${claimed.map((c) =>
                `#${c.match_id}「${escapeHtml(c.claimed_by)}」至 ${fmtLeaseTime(c.claim_expires_at)}`
            ).join("；")}
        </div>` : "";
    return `
    <div class="review-group" data-group="${escapeHtml(group.group_key)}">
        <div class="group-head">
            <span class="group-badge">同案组 × ${group.count}</span>
            <span class="muted">
                归一化：道路 [${escapeHtml(group.street_tokens.join(" ") || "—")}]
                · 地区 [${escapeHtml(group.locality_tokens.join(" ") || "—")}]
                · 门牌号 ${escapeHtml(group.house || "无")}
            </span>
        </div>
        <div class="group-queries">${queries}</div>
        ${groupWarn}
        ${candidates}
        <div class="review-actions">
            ${best ? `<button class="tiny confirm group-action" data-action="confirmed">
                整组确认首选（#${best.address.id}）</button>` : ""}
            ${options ? `
                <select class="group-select">${options}</select>
                <button class="tiny override group-action" data-action="overridden">整组改派所选候选</button>` : ""}
            <button class="tiny reject group-action" data-action="rejected">整组判无匹配</button>
        </div>
    </div>`;
}

async function loadReviewGroups() {
    const box = $("#review-groups");
    box.innerHTML = '<p class="muted">加载同案组…</p>';
    try {
        const data = await api("/api/review-groups");
        reviewGroups = data.groups || [];
        const merged = reviewGroups.filter((g) => g.count >= 2);
        if (merged.length === 0) {
            box.innerHTML = '<p class="muted">暂无可合并的同案组：归一化后道路/地区词元、' +
                '门牌号一致且候选（ID、顺序、得分）完全相同的待确认记录会归到同组。</p>';
            return;
        }
        box.innerHTML =
            `<h3 class="group-title">同案合并复核（${merged.length} 组）</h3>` +
            merged.map(renderGroup).join("");
    } catch (err) {
        box.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#review-groups").addEventListener("click", async (event) => {
    const btn = event.target.closest("button.group-action");
    if (!btn) return;
    const card = btn.closest(".review-group");
    const key = card.dataset.group;
    const group = reviewGroups.find((g) => g.group_key === key);
    if (!group) return;
    const action = btn.dataset.action;
    const body = { group_key: key, match_ids: group.match_ids, action };
    if (action === "overridden") {
        body.selected_id = Number(card.querySelector(".group-select").value);
    }
    // UX guard: live leases mean the server will reject the whole group.
    const claimed = group.claimed_members || [];
    if (claimed.length) {
        const stillLive = claimed.filter((c) =>
            new Date(c.claim_expires_at).getTime() > Date.now());
        if (stillLive.length) {
            alert(
                "组内有记录仍被领取，整组复核将被整体拒绝：\n" +
                stillLive.map((c) =>
                    `#${c.match_id} 由「${c.claimed_by}」领取至 ${fmtLeaseTime(c.claim_expires_at)}`
                ).join("\n") +
                "\n\n请等待租约释放或到期后再操作。"
            );
            loadReview();
            return;
        }
    }
    btn.disabled = true;
    try {
        const res = await api("/api/review-groups/review", {
            method: "POST",
            body: JSON.stringify(body),
        });
        await refreshHealth();
        refreshReplayBadge();
        loadReview();
    } catch (err) {
        // 409：组签名或成员集合已变化，一条也未写入，刷新展示最新状态。
        alert(err.message);
        loadReview();
    }
});

// ----------------------------------------------------- review verdict replay

const REPLAY_CATEGORY_LABEL = {
    consistent: "一致",
    conflict: "冲突",
    missed: "漏命中",
    false_hit: "误命中",
    unknown: "原选择未知",
};

// The replay re-scores every reviewed record on each call, so (like the gaps
// manifest and the audit) it is only recomputed once the tab was opened.
let replayEverLoaded = false;

function renderReplayItem(item) {
    let original;
    if (item.review_action === "rejected") {
        original = "<b>判无匹配</b>";
    } else if (item.selected_known && item.selected_id !== null && item.selected_id !== undefined) {
        original = `<b>${escapeHtml(item.review_action_label)}</b> → ` +
            `#${item.selected_id} ${escapeHtml(item.selected_full_text || "")}` +
            `${item.selected_missing ? "（已删除）" : ""}` +
            `<span class="muted">（复核得分 ${pct(item.review_score)}）</span>`;
    } else {
        // Legacy positive review whose chosen address was deleted before the
        // review-time snapshot existed: the original choice is genuinely lost.
        original = `<b>${escapeHtml(item.review_action_label)}</b> → ` +
            `<span class="replay-unknown">原选择未知（所选地址已删除且无复核时快照）</span>` +
            `<span class="muted">（复核得分 ${pct(item.review_score)}）</span>`;
    }
    const bestLine = item.replay_best_id !== null && item.replay_best_id !== undefined
        ? `当前首选：<b>#${item.replay_best_id}</b> ${escapeHtml(item.replay_best_full_text || "")}` +
          `<span class="muted">（综合 ${pct(item.replay_best_score)} · ${escapeHtml(item.replay_reason || "")}）</span>`
        : '<span class="muted">当前标准地址表中没有任何候选</span>';
    const targetRibbon = item.selected_id !== null && item.selected_id !== undefined
        ? { id: item.selected_id, label: "人工所选" }
        : null;
    const candidates = (item.replay.candidates || [])
        .map((c, i) => renderCandidate(c, i, { targetRibbon }))
        .join("");
    return `
    <div class="replay-item">
        <div class="replay-head">
            <span class="replay-cat ${item.category}">${REPLAY_CATEGORY_LABEL[item.category] || item.category}</span>
            <span class="replay-query">🔍 ${escapeHtml(item.query)}</span>
        </div>
        <div class="replay-meta muted">
            记录 #${item.match_id} · 复核于 ${escapeHtml(item.reviewed_at || "")}
            · 原结论：${original}
        </div>
        <div class="replay-current">
            当前回放：<span class="status ${item.replay_status}">${STATUS_LABEL[item.replay_status] || item.replay_status}</span>
            ${bestLine}
        </div>
        <div class="replay-reason">判定：${escapeHtml(item.verdict_reason)}</div>
        <details class="replay-details">
            <summary>本次回放候选明细（${(item.replay.candidates || []).length}）</summary>
            ${candidates || '<div class="meta muted">无候选</div>'}
        </details>
    </div>`;
}

async function loadReviewReplay() {
    const filter = $("#replay-filter").value;
    const list = $("#replay-list");
    list.innerHTML = '<p class="muted">正在用当前标准地址表逐条回放复核结论…</p>';
    try {
        const qs = filter ? `?category=${encodeURIComponent(filter)}` : "";
        const data = await api(`/api/review-replay${qs}`);
        replayEverLoaded = true;
        $("#replay-count").textContent = data.risk_count || "";
        $("#replay-summary").innerHTML =
            `<span class="muted">共 <b>${data.total_reviewed}</b> 条已复核记录：` +
            `<span class="replay-cat consistent">一致 ${data.consistent}</span> ` +
            `<span class="replay-cat conflict">冲突 ${data.conflict}</span> ` +
            `<span class="replay-cat missed">漏命中 ${data.missed}</span> ` +
            `<span class="replay-cat false_hit">误命中 ${data.false_hit}</span> ` +
            `<span class="replay-cat unknown">原选择未知 ${data.unknown || 0}</span>` +
            ` · 回放时间 ${escapeHtml(data.generated_at)}</span>`;
        if (data.items.length === 0) {
            list.innerHTML = filter
                ? '<p class="muted">当前筛选条件下没有记录。</p>'
                : '<p class="muted">暂无已复核记录，复核待确认记录后即可在此回放对照。</p>';
            return;
        }
        list.innerHTML = data.items.map(renderReplayItem).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-replay").addEventListener("click", loadReviewReplay);
$("#replay-filter").addEventListener("change", loadReviewReplay);

async function refreshReplayBadge() {
    // Reviews or table changes alter the replay; recompute only after the tab
    // has been opened at least once (each call re-scores every reviewed record).
    if (!replayEverLoaded) return;
    await loadReviewReplay();
}

// -------------------------------------------------------- coverage gaps

// Full manifest from the last load; the badge is refreshed from these
// responses only after the tab has been opened at least once (the endpoint
// re-runs every representative query, so we don't poll it from health).
let gapsData = null;
let gapsEverLoaded = false;

function renderGap(group) {
    const covered = group.coverage === "covered";
    const badge = covered
        ? '<span class="gap-state covered">✓ 已覆盖</span>'
        : '<span class="gap-state open">⛏ 待补录</span>';
    const rerun = group.rerun || {};
    const best = (rerun.candidates || [])[0] || null;

    const headLine = covered
        ? `<div class="gap-target">
               代表查询现已自动命中 → <b>#${rerun.best_id}</b>
               ${best ? escapeHtml(best.address.full_text) : ""}
           </div>`
        : best
            ? `<div class="gap-target">当前首选候选：<b>#${best.address.id}</b>
               ${escapeHtml(best.address.full_text)}
               <span class="muted">（综合 ${pct(best.score)} · ${escapeHtml(rerun.reason || "")}）</span></div>`
            : '<div class="gap-target muted">当前标准地址表中没有任何候选</div>';

    const examples = group.examples.map((ex) => `
        <div class="gq">🔍 ${escapeHtml(ex.query)} <span class="muted">记录 #${ex.id}</span></div>
    `).join("");
    const more = group.example_truncated
        ? `<div class="muted">另有 ${group.distinct_query_count - group.examples.length} 条不同写法未展开</div>`
        : "";

    const targetRibbon = covered && best
        ? { id: best.address.id, label: "已覆盖目标" }
        : null;
    const candidates = (rerun.candidates || [])
        .map((c, i) => renderCandidate(c, i, { targetRibbon }))
        .join("");

    return `
    <div class="gap-item${covered ? " covered" : ""}">
        <div class="gap-head">
            ${badge}
            <span class="group-badge gap-count">出现 ${group.count} 次</span>
            <span class="muted">
                归一化：道路 [${escapeHtml(group.street_tokens.join(" ") || "—")}]
                · 地区 [${escapeHtml(group.locality_tokens.join(" ") || "—")}]
                · 门牌号 <b>${escapeHtml(group.house || "无")}</b>
            </span>
        </div>
        <div class="gap-meta muted">
            代表查询：🔍 ${escapeHtml(group.representative_query)}
            （记录 #${group.representative_match_id}）
            · 最近出现 ${escapeHtml(group.last_seen_at || "")}
            · 首次出现 ${escapeHtml(group.first_seen_at || "")}
        </div>
        <div class="group-queries gap-examples">${examples}${more}</div>
        ${headLine}
        <details class="gap-details">
            <summary>本次重跑候选明细（${(rerun.candidates || []).length}）</summary>
            ${candidates || '<div class="meta muted">无候选</div>'}
        </details>
    </div>`;
}

async function loadCoverageGaps() {
    const filter = $("#gaps-filter").value;
    const list = $("#gaps-list");
    list.innerHTML = '<p class="muted">正在用当前标准地址表重跑各组代表查询…</p>';
    try {
        const qs = filter ? `?status=${encodeURIComponent(filter)}` : "";
        gapsData = await api(`/api/coverage-gaps${qs}`);
        gapsEverLoaded = true;
        $("#gaps-count").textContent = gapsData.open || "";
        $("#gaps-summary").innerHTML =
            `<span class="muted">共 <b>${gapsData.total_groups}</b> 个缺口键` +
            `（源自 ${gapsData.source_records} 条无匹配日志）：` +
            `<span class="gap-state open">待补录 ${gapsData.open}</span>` +
            ` <span class="gap-state covered">已覆盖 ${gapsData.covered}</span>` +
            ` · 重跑时间 ${escapeHtml(gapsData.generated_at)}</span>`;
        if (gapsData.groups.length === 0) {
            list.innerHTML = '<p class="muted">当前筛选条件下没有缺口记录。</p>';
            return;
        }
        list.innerHTML = gapsData.groups.map(renderGap).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-gaps").addEventListener("click", loadCoverageGaps);
$("#gaps-filter").addEventListener("change", loadCoverageGaps);

// -------------------------------------------------------- self-match audit

const AUDIT_CATEGORY_LABEL = {
    self_score_low: "自身分数不足",
    best_drifted: "首选偏移",
    margin_ambiguous: "分差歧义",
};

// The audit scores every address against the whole table on each call, so
// (like the gaps manifest) it is only recomputed once the tab was opened.
let auditEverLoaded = false;

function renderAuditItem(item) {
    const a = item.address;
    const bestLine = item.best_id !== null && item.best_id !== a.id
        ? `<div class="audit-best">当前首选：<b>#${item.best_id}</b> ` +
          `${escapeHtml(item.best_full_text || "")}` +
          `<span class="muted">（综合 ${pct(item.best_score)}）</span></div>`
        : "";
    const blockers = item.blockers.length
        ? item.blockers.map((c, i) => {
            const delta = c.score - item.own_score;
            const rel = delta >= 0 ? `高于自身 ${pct(delta)}` : `低于自身 ${pct(-delta)}`;
            const ribbon = c.address.id === item.best_id
                ? { id: item.best_id, label: "当前首选" }
                : null;
            return `<div class="audit-blocker">
                <div class="audit-blocker-meta muted">阻碍候选 #${i + 1} · 综合 ${pct(c.score)}（${rel}）</div>
                ${renderCandidate(c, i, { targetRibbon: ribbon })}
            </div>`;
        }).join("")
        : '<div class="meta muted">无阻碍候选：没有其他地址得分达到「自身分数 − 默认分差阈值」。</div>';
    return `
    <div class="audit-item">
        <div class="audit-head">
            <span class="audit-cat ${item.category}">${AUDIT_CATEGORY_LABEL[item.category] || item.category}</span>
            <span class="audit-addr">原地址 #${a.id} ${escapeHtml(a.full_text)}</span>
        </div>
        <div class="audit-meta muted">
            自身分数 <b>${pct(item.own_score)}</b>
            ${a.house ? ` · 门牌号 ${escapeHtml(a.house)}` : ""}
            ${a.external_id ? ` · 外部编号 ${escapeHtml(a.external_id)}` : ""}
            · 匹配状态 <span class="status ${item.match_status}">${STATUS_LABEL[item.match_status] || item.match_status}</span>
        </div>
        <div class="audit-reason">判定原因：${escapeHtml(item.audit_reason)}
            <span class="muted">（匹配器判定：${escapeHtml(item.match_reason)}）</span></div>
        ${bestLine}
        <div class="audit-blockers">
            <div class="muted">阻碍候选 ${item.blockers.length} 个
                （得分 ≥ 自身分数 − 默认分差阈值，即 ≥ ${pct(item.blocker_floor)}，不受前五截断）：</div>
            ${blockers}
        </div>
    </div>`;
}

async function loadAddressAudit() {
    const filter = $("#audit-filter").value;
    const list = $("#audit-list");
    list.innerHTML = '<p class="muted">正在用当前标准地址表逐条自匹配巡检…</p>';
    try {
        const qs = filter ? `?category=${encodeURIComponent(filter)}` : "";
        const data = await api(`/api/address-audit${qs}`);
        auditEverLoaded = true;
        $("#audit-count").textContent = data.risk_count || "";
        $("#audit-summary").innerHTML =
            `<span class="muted">共 <b>${data.total_addresses}</b> 条标准地址：` +
            `自动命中自身 <b>${data.auto_hit_self}</b> · 风险 <b>${data.risk_count}</b>` +
            `（<span class="audit-cat best_drifted">首选偏移 ${data.best_drifted}</span>` +
            ` <span class="audit-cat self_score_low">自身分数不足 ${data.self_score_low}</span>` +
            ` <span class="audit-cat margin_ambiguous">分差歧义 ${data.margin_ambiguous}</span>）` +
            ` · 巡检时间 ${escapeHtml(data.generated_at)}</span>`;
        if (data.items.length === 0) {
            list.innerHTML = filter
                ? '<p class="muted">当前筛选条件下没有风险记录。</p>'
                : '<p class="ok-msg">✓ 全部标准地址都能以自身全文自动命中自身 ID，未发现风险。</p>';
            return;
        }
        list.innerHTML = data.items.map(renderAuditItem).join("");
    } catch (err) {
        list.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-refresh-audit").addEventListener("click", loadAddressAudit);
$("#audit-filter").addEventListener("change", loadAddressAudit);

async function refreshAuditBadge() {
    // Table changed (upload / sample load): recompute the audit, but only
    // after the tab has been opened at least once -- each call scores every
    // address against the whole table, so we don't poll it from elsewhere.
    if (!auditEverLoaded) return;
    await loadAddressAudit();
}

// ------------------------------------------------------------------ health

async function refreshGapsBadge() {
    // Keep the tab badge current after new matches; the endpoint still
    // re-runs each representative query, so this only happens once the user
    // has opened the tab at least once.
    if (!gapsEverLoaded) return;
    try {
        const data = await api("/api/coverage-gaps?status=open");
        $("#gaps-count").textContent = data.open || "";
        // While the gaps tab is open on the "open" filter (e.g. right after
        // a batch match), keep its list in sync with the fresh re-run too.
        if ($("#tab-gaps").classList.contains("active")
                && $("#gaps-filter").value === "open") {
            gapsData = data;
            $("#gaps-summary").innerHTML =
                `<span class="muted">共 <b>${data.total_groups}</b> 个缺口键` +
                `（源自 ${data.source_records} 条无匹配日志）：` +
                `<span class="gap-state open">待补录 ${data.open}</span>` +
                ` <span class="gap-state covered">已覆盖 ${data.covered}</span>` +
                ` · 重跑时间 ${escapeHtml(data.generated_at)}</span>`;
            $("#gaps-list").innerHTML = data.groups.length
                ? data.groups.map(renderGap).join("")
                : '<p class="muted">当前筛选条件下没有缺口记录。</p>';
        }
    } catch (err) {
        // Badge refresh is best-effort; ignore failures.
    }
}

async function refreshHealth() {
    try {
        const h = await api("/api/health");
        $("#health").textContent = `标准地址 ${h.address_count} 条 · 待确认 ${h.needs_review}`;
        $("#health").className = "badge ok";
        $("#addr-count").textContent = h.address_count;
        $("#review-count").textContent = h.needs_review;
        if (h.default_thresholds) window.__defaultThresholds = h.default_thresholds;
    } catch (err) {
        $("#health").textContent = "服务不可用";
        $("#health").className = "badge bad";
    }
}

refreshHealth();

// ========================================================== A/B experiments

const AB_FIELDS = [
    { key: "auto_threshold", label: "自动命中阈值", step: "0.01" },
    { key: "reject_threshold", label: "拒绝阈值", step: "0.01" },
    { key: "margin_threshold", label: "分差阈值", step: "0.01" },
    { key: "token_weight", label: "词元权重", step: "0.05" },
];

const AB_DEMO = {
    profiles: [
        { name: "基线A", auto_threshold: 0.8, reject_threshold: 0.35, margin_threshold: 0.06, token_weight: 0.7 },
        { name: "激进B", auto_threshold: 0.7, reject_threshold: 0.2, margin_threshold: 0.02, token_weight: 0.8 },
    ],
    queries: [
        "123 Main Street, New York, NY 10001",
        "1010 Lake Dr, Austin TX",
        "Hillcrest Road, Dallas",
        "北京東路８８號 3楼, 上海",
        "1 Milky Way, Mars Base",
    ].join("\n"),
};

// The editor keeps at most 4 profile cards; initialised lazily on first open.
let abProfiles = null;

function defaultThresholds() {
    return window.__defaultThresholds ||
        { auto_threshold: 0.8, reject_threshold: 0.35, margin_threshold: 0.06, token_weight: 0.7 };
}

function renderProfileEditor() {
    if (abProfiles === null) {
        const t = defaultThresholds();
        abProfiles = [
            { name: "基线A", ...t },
            { name: "宽松B", auto_threshold: 0.7, reject_threshold: 0.25,
              margin_threshold: 0.03, token_weight: t.token_weight },
        ];
    }
    const box = $("#ab-profiles");
    box.innerHTML = abProfiles.map((p, i) => `
        <div class="ab-profile" data-index="${i}">
            <div class="ab-profile-head">
                <span class="ab-letter">${"ABCD"[i]}</span>
                <input class="ab-name" type="text" maxlength="40"
                       value="${escapeHtml(p.name)}" placeholder="档案名称">
                ${abProfiles.length > 2 ?
                    `<button class="tiny reject ab-remove" type="button">移除</button>` : ""}
            </div>
            <div class="ab-fields">
                ${AB_FIELDS.map((f) => `
                <label class="ab-field">
                    <span>${f.label}</span>
                    <input type="number" min="0" max="1" step="${f.step}"
                           data-field="${f.key}" value="${p[f.key]}">
                </label>`).join("")}
            </div>
        </div>`).join("");
    $("#btn-ab-add").disabled = abProfiles.length >= 4;
}

function readProfileEditor() {
    return $$("#ab-profiles .ab-profile").map((card) => {
        const profile = { name: card.querySelector(".ab-name").value };
        AB_FIELDS.forEach((f) => {
            profile[f.key] = Number(card.querySelector(`input[data-field="${f.key}"]`).value);
        });
        return profile;
    });
}

$("#ab-profiles").addEventListener("click", (event) => {
    const btn = event.target.closest(".ab-remove");
    if (!btn) return;
    const idx = Number(btn.closest(".ab-profile").dataset.index);
    abProfiles = readProfileEditor();
    abProfiles.splice(idx, 1);
    renderProfileEditor();
});

$("#btn-ab-add").addEventListener("click", () => {
    abProfiles = readProfileEditor();
    if (abProfiles.length >= 4) return;
    abProfiles.push({
        name: `规则${"ABCD"[abProfiles.length]}`,
        ...defaultThresholds(),
    });
    renderProfileEditor();
});

$("#btn-ab-demo").addEventListener("click", () => {
    abProfiles = AB_DEMO.profiles.map((p) => ({ ...p }));
    $("#ab-queries").value = AB_DEMO.queries;
    renderProfileEditor();
});

function renderExperimentErrors(errors) {
    if (!errors || errors.length === 0) {
        $("#ab-errors").innerHTML = "";
        return;
    }
    const items = errors.map((e) => {
        const where = e.type === "profile"
            ? `第 ${e.index + 1} 档档案${e.field ? ` · ${fieldLabel(e.field)}` : ""}`
            : e.type === "query"
                ? `第 ${e.index + 1} 条查询`
                : (e.type === "profiles" ? "档案列表" : "查询列表");
        return `<li><b>${escapeHtml(where)}</b>：${escapeHtml(e.message)}</li>`;
    }).join("");
    $("#ab-errors").innerHTML =
        `<div class="ab-errors"><div class="ab-errors-head">⛔ 校验失败，实验未执行（共 ${errors.length} 项）：</div><ul>${items}</ul></div>`;
}

function fieldLabel(key) {
    const f = AB_FIELDS.find((x) => x.key === key);
    return f ? f.label : key;
}

// ------------------------------------------------------------ result render

function triggerBlock(th) {
    const rows = [
        ["auto_threshold", "自动命中阈值"],
        ["reject_threshold", "拒绝阈值"],
        ["margin_threshold", "前两名分差"],
    ].map(([key, label]) => {
        const block = th[key];
        const actual = block.actual === null || block.actual === undefined ? "—" : pct(block.actual);
        return `<div class="ab-thr${block.triggered ? " fired" : ""}">
            <span>${label}</span>
            <span class="muted">${pct(block.value)} / 实际 ${actual}</span>
            <span>${block.triggered ? "⚠ 触发" : "未触发"}</span>
        </div>`;
    }).join("");
    const decisive = th.decisive
        ? `<div class="ab-decisive">决定性阈值：<b>${escapeHtml(fieldLabel(th.decisive))}</b></div>`
        : `<div class="ab-decisive ok">得分达到自动命中阈值且领先明显，无阈值拦截</div>`;
    return `<div class="ab-thr-grid">${rows}</div>${decisive}
        <div class="muted">词元权重 ${pct(th.token_weight.value)} / 门牌号权重 ${pct(1 - th.token_weight.value)}</div>`;
}

function renderProfileColumn(pr) {
    const delta = pr.score_delta_vs_first;
    const deltaTxt = (delta !== null && pr.index > 0)
        ? ` <span class="${delta === 0 ? "muted" : delta > 0 ? "ab-delta-up" : "ab-delta-down"}">` +
          `(${delta > 0 ? "+" : ""}${pct(delta)})</span>`
        : "";
    const runner = pr.runner_up_id
        ? `<div class="muted">次高分 #${pr.runner_up_id}：${pct(pr.runner_up_score)}</div>` : "";
    return `
    <div class="ab-col">
        <div class="ab-col-head"><span class="ab-letter sm">${"ABCD"[pr.index]}</span> ${escapeHtml(pr.profile)}</div>
        <span class="status ${pr.status}">${STATUS_LABEL[pr.status] || pr.status}</span>
        <div class="ab-best">首选 #${pr.best_id ?? "—"} · <b>${pr.best_score === null ? "—" : pct(pr.best_score)}</b>${deltaTxt}</div>
        ${runner}
        <div class="muted">${escapeHtml(pr.reason)}</div>
        ${triggerBlock(pr.thresholds)}
    </div>`;
}

function renderDiffTable(qr) {
    const diff = qr.candidate_diff;
    if (!diff || diff.ranking.length === 0) return '<div class="muted">无候选</div>';
    const heads = qr.profiles.map((pr) =>
        `<th>${"ABCD"[pr.index]} ${escapeHtml(pr.profile)}</th>`).join("");
    const bestIds = new Set(qr.profiles.map((pr) => pr.best_id));
    const rows = diff.ranking.map((row) => {
        const cells = row.cells.map((cell) => {
            if (!cell.match) return '<td class="ab-cell absent">—</td>';
            const isBest = bestIds.has(row.address_id) && cell.match.rank === 1;
            return `<td class="ab-cell${isBest ? " best" : ""}">` +
                `#${cell.match.rank} · ${pct(cell.match.score)}</td>`;
        }).join("");
        return `<tr>
            <td class="ab-addr-cell">#${row.address_id} ${escapeHtml(row.full_text)}
                ${row.house ? `<span class="muted">（门牌 ${escapeHtml(row.house)}）</span>` : ""}</td>
            ${cells}
        </tr>`;
    }).join("");
    return `<div class="table-wrap ab-diff-wrap"><table class="ab-diff">
        <thead><tr><th>候选地址（各档排名 · 得分）</th>${heads}</tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

function renderExperimentResult(exp) {
    const s = exp.summary;
    const profHeads = s.profiles.map((p) => {
        return `<div class="ab-sum-cell">
            <b>${escapeHtml(p.name)}</b><br>
            <span class="status auto_matched">${p.auto_matched}</span>
            <span class="status needs_review">${p.needs_review}</span>
            <span class="status no_match">${p.no_match}</span>
        </div>`;
    }).join("");
    const snapshot = typeof exp.table_snapshot === "string"
        ? JSON.parse(exp.table_snapshot) : exp.table_snapshot;

    const queries = exp.results.map((qr) => {
        const da = qr.disagreement;
        const range = da.best_score_range.delta === null ? "" :
            ` · 首选分差区间 ${pct(da.best_score_range.min)}~${pct(da.best_score_range.max)}` +
            `（极差 ${pct(da.best_score_range.delta)}）`;
        const groups = Object.entries(da.status_groups).map(([st, names]) =>
            `<span class="status ${st}">${STATUS_LABEL[st] || st}：${escapeHtml(names.join("、"))}</span>`
        ).join(" ");
        return `
        <div class="ab-query${da.conclusion_differs ? " disagree" : ""}">
            <div class="query-line">
                ${da.conclusion_differs ? '<span class="ab-flag">⚠ 结论不一致</span>' : '<span class="ab-flag agree">✓ 各档一致</span>'}
                #${qr.index + 1} ${escapeHtml(qr.query)}
            </div>
            <div class="muted">归一化：${escapeHtml(qr.profiles[0].normalized.text || "（空）")}
                · 门牌号 ${escapeHtml(qr.profiles[0].normalized.house || "无")}${range}</div>
            <div class="ab-groups">${groups}</div>
            <div class="ab-cols">${qr.profiles.map(renderProfileColumn).join("")}</div>
            <details class="ab-details" ${da.conclusion_differs ? "open" : ""}>
                <summary>候选差异（${qr.candidate_diff.ranking.length} 个候选进入并集）</summary>
                ${renderDiffTable(qr)}
                <div class="ab-fulldiff">
                    ${qr.profiles.map((pr) => `
                    <details class="ab-cand-detail">
                        <summary>${"ABCD"[pr.index]} ${escapeHtml(pr.profile)} 的完整候选（${pr.candidates.length}）</summary>
                        ${pr.candidates.map((c, i) => renderCandidate(c, i, {})).join("")}
                    </details>`).join("")}
                </div>
            </details>
        </div>`;
    }).join("");

    $("#ab-result").innerHTML = `
    <div class="card ab-result-card">
        <div class="ab-result-head">
            <h2>实验结果 <span class="ab-eid">${escapeHtml(exp.id)}</span></h2>
            <span class="muted">${escapeHtml(exp.created_at)} · 地址表快照 ${snapshot.count} 条
                · digest ${escapeHtml(String(snapshot.digest).slice(0, 12))}…</span>
        </div>
        <div class="ab-summary">
            <div class="ab-sum-cell"><b>查询</b><br><span class="muted">共 ${s.query_count} 条</span><br>
                <span class="${s.disagreement_count ? "ab-flag" : "ab-flag agree"}">不一致 ${s.disagreement_count}</span></div>
            ${profHeads}
        </div>
        <p class="hint">状态计数依次为：自动命中 / 待确认 / 无匹配。</p>
        ${queries}
    </div>`;
}

async function submitExperiment() {
    const profiles = readProfileEditor();
    const queries = $("#ab-queries").value.split("\n").map((q) => q.trim()).filter(Boolean);
    $("#ab-errors").innerHTML = "";
    $("#ab-result").innerHTML = "";
    const btn = $("#btn-ab-run");
    btn.disabled = true;
    $("#ab-run-summary").innerHTML = '<span class="muted">实验运行中…</span>';
    try {
        const res = await fetch("/api/experiments", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ profiles, queries }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderExperimentErrors(data.errors || [{ type: "request", index: null, field: null, message: data.detail || "请求失败" }]);
            $("#ab-run-summary").innerHTML = "";
            return;
        }
        $("#ab-run-summary").innerHTML =
            `<span class="ok-msg">实验 ${escapeHtml(data.id)} 完成：${data.summary.query_count} 条查询，` +
            `结论不一致 ${data.summary.disagreement_count} 条，已持久化</span>`;
        renderExperimentResult(data);
        loadExperimentHistory();
    } catch (err) {
        renderExperimentErrors([{ type: "request", index: null, field: null, message: err.message }]);
        $("#ab-run-summary").innerHTML = "";
    } finally {
        btn.disabled = false;
    }
}

$("#btn-ab-run").addEventListener("click", submitExperiment);

// -------------------------------------------------------------- history

// Cached history items, also used to fill the regression-compare selectors.
let abHistoryItems = [];

async function loadExperimentHistory() {
    const box = $("#ab-history");
    if (!box) return;
    try {
        const data = await api("/api/experiments?limit=50");
        abHistoryItems = data.items;
        refreshCompareSelects();
        if (data.items.length === 0) {
            box.innerHTML = '<p class="muted">暂无实验记录。</p>';
            return;
        }
        box.innerHTML = `<div class="table-wrap"><table>
            <thead><tr><th>实验编号</th><th>提交时间</th><th>档案</th><th>查询</th><th>不一致</th><th>回归对比</th></tr></thead>
            <tbody>${data.items.map((it) => `<tr>
                <td><a href="#" class="ab-open" data-id="${escapeHtml(it.id)}">${escapeHtml(it.id)}</a></td>
                <td>${escapeHtml(it.created_at)}</td>
                <td>${it.profiles.map((p) => escapeHtml(p.name)).join(" / ")}</td>
                <td>${it.query_count}</td>
                <td>${it.summary.disagreement_count ?
                    `<span class="ab-flag">${it.summary.disagreement_count}</span>` :
                    '<span class="ab-flag agree">0</span>'}</td>
                <td class="cmp-pick">
                    <button class="tiny ghost cmp-set" data-role="base" data-id="${escapeHtml(it.id)}">→基准</button>
                    <button class="tiny ghost cmp-set" data-role="target" data-id="${escapeHtml(it.id)}">→对比</button>
                </td>
            </tr>`).join("")}</tbody></table></div>
            ${data.total > 50 ? `<p class="muted">共 ${data.total} 个实验，仅显示最近 50 个</p>` : ""}`;
    } catch (err) {
        box.innerHTML = `<p class="err-msg">${escapeHtml(err.message)}</p>`;
    }
}

$("#btn-ab-refresh-history").addEventListener("click", loadExperimentHistory);

$("#ab-history").addEventListener("click", async (event) => {
    const link = event.target.closest(".ab-open");
    if (!link) return;
    event.preventDefault();
    try {
        const data = await api(`/api/experiments/${encodeURIComponent(link.dataset.id)}`);
        renderExperimentResult(data);
        window.scrollTo({ top: $("#ab-result").offsetTop - 60, behavior: "smooth" });
    } catch (err) {
        alert(err.message);
    }
});

// ====================================== historical regression comparison

const CMP_CATEGORIES = [
    { key: "improved", label: "改善" },
    { key: "regressed", label: "退化" },
    { key: "score_only", label: "仅分数变化" },
    { key: "unchanged", label: "无变化" },
];
const CMP_CATEGORY_LABEL = Object.fromEntries(CMP_CATEGORIES.map((c) => [c.key, c.label]));

// Last comparison response and the active category filter ("all" = no filter).
let cmpResult = null;
let cmpFilter = "all";

function cmpOptionLabel(it) {
    const names = it.profiles.map((p) => p.name).join(" / ");
    return `${it.id}（${names} · ${it.query_count} 条查询）`;
}

function refreshCompareSelects() {
    const baseSel = $("#cmp-base");
    const targetSel = $("#cmp-target");
    if (!baseSel || !targetSel) return;
    const prevBase = baseSel.value;
    const prevTarget = targetSel.value;
    const options = abHistoryItems
        .map((it) => `<option value="${escapeHtml(it.id)}">${escapeHtml(cmpOptionLabel(it))}</option>`)
        .join("");
    baseSel.innerHTML = options;
    targetSel.innerHTML = options;
    const has = (id) => abHistoryItems.some((it) => it.id === id);
    // History is newest-first: default to "previous run -> latest run".
    baseSel.value = has(prevBase) ? prevBase
        : (abHistoryItems[1] || abHistoryItems[0] || {}).id || "";
    targetSel.value = has(prevTarget) ? prevTarget
        : (abHistoryItems[0] || {}).id || "";
}

$("#ab-history").addEventListener("click", (event) => {
    const btn = event.target.closest(".cmp-set");
    if (!btn) return;
    event.preventDefault();
    $(btn.dataset.role === "base" ? "#cmp-base" : "#cmp-target").value = btn.dataset.id;
    $("#cmp-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
});

$("#btn-cmp-swap").addEventListener("click", () => {
    const base = $("#cmp-base").value;
    $("#cmp-base").value = $("#cmp-target").value;
    $("#cmp-target").value = base;
});

function renderCompareErrors(errors) {
    if (!errors || errors.length === 0) {
        $("#cmp-errors").innerHTML = "";
        return;
    }
    const items = errors.map((e) => {
        const where = e.type === "profiles" ? "档案对齐"
            : e.type === "queries" ? "查询对齐"
                : e.type === "base_id" ? "基准实验"
                    : e.type === "target_id" ? "对比实验" : "请求";
        return `<li><b>${escapeHtml(where)}</b>：${escapeHtml(e.message)}</li>`;
    }).join("");
    $("#cmp-errors").innerHTML =
        `<div class="ab-errors"><div class="ab-errors-head">⛔ 校验失败，未执行对比（共 ${errors.length} 项）：</div><ul>${items}</ul></div>`;
}

function cmpFilteredCells() {
    if (!cmpResult) return [];
    if (cmpFilter === "all") return cmpResult.cells;
    return cmpResult.cells.filter((c) => c.category === cmpFilter);
}

function cmpCellRow(cell) {
    const b = cell.base;
    const t = cell.target;
    const delta = cell.score_delta;
    const deltaTxt = delta === null ? "—"
        : `<span class="${delta === 0 ? "muted" : delta > 0 ? "ab-delta-up" : "ab-delta-down"}">` +
          `${delta > 0 ? "+" : ""}${pct(delta)}</span>`;
    return `<tr>
        <td class="cmp-profile-cell">${escapeHtml(cell.profile)}</td>
        <td><span class="cmp-cat ${cell.category}">${escapeHtml(cell.category_label)}</span></td>
        <td><span class="status ${b.status}">${STATUS_LABEL[b.status] || b.status}</span>
            <span class="muted">#${b.best_id ?? "—"} · ${b.best_score === null ? "—" : pct(b.best_score)}</span></td>
        <td><span class="status ${t.status}">${STATUS_LABEL[t.status] || t.status}</span>
            <span class="muted">#${t.best_id ?? "—"} · ${t.best_score === null ? "—" : pct(t.best_score)}</span></td>
        <td>${deltaTxt}</td>
        <td class="muted">${escapeHtml(cell.reason)}</td>
    </tr>`;
}

function renderCompareCells() {
    const cells = cmpFilteredCells();
    if (cells.length === 0) {
        return '<p class="muted">当前筛选条件下没有差异记录。</p>';
    }
    // Cells arrive ordered by (query, profile); group consecutive queries.
    const groups = [];
    cells.forEach((cell) => {
        const last = groups[groups.length - 1];
        if (last && last.query === cell.query) {
            last.cells.push(cell);
        } else {
            groups.push({ query: cell.query, cells: [cell] });
        }
    });
    return groups.map((g) => `
        <div class="cmp-query">
            <div class="query-line">🔍 ${escapeHtml(g.query)}</div>
            <div class="table-wrap"><table class="cmp-table">
                <thead><tr><th>档案</th><th>分类</th><th>基准（旧）</th><th>对比（新）</th><th>得分差</th><th>说明</th></tr></thead>
                <tbody>${g.cells.map(cmpCellRow).join("")}</tbody>
            </table></div>
        </div>`).join("");
}

function renderCompareResult() {
    const data = cmpResult;
    if (!data) return;
    const s = data.summary;
    const snapWarn = data.snapshot.comparable ? "" : `
        <div class="cmp-warning">
            ⚠ ${escapeHtml(data.snapshot.note)}<br>
            <span class="muted">基准 digest ${escapeHtml(String(data.snapshot.base_digest).slice(0, 12))}…
            ↔ 对比 digest ${escapeHtml(String(data.snapshot.target_digest).slice(0, 12))}…</span>
        </div>`;
    const unpaired = [];
    if (data.queries.only_in_base.length) {
        unpaired.push(`仅基准实验有 ${data.queries.only_in_base.length} 条：${data.queries.only_in_base.map((q) => `“${q}”`).join("、")}`);
    }
    if (data.queries.only_in_target.length) {
        unpaired.push(`仅对比实验有 ${data.queries.only_in_target.length} 条：${data.queries.only_in_target.map((q) => `“${q}”`).join("、")}`);
    }
    const unpairedNote = unpaired.length
        ? `<div class="cmp-unpaired">⚠ ${data.queries.common} 条共同查询参与对比；未配对查询不参与统计 —— ${unpaired.map(escapeHtml).join("；")}</div>`
        : "";

    const filterBtn = (key, label, count) => `
        <button class="tiny cmp-filter${cmpFilter === key ? " active" : ""}" data-cat="${key}">
            ${label} ${count}</button>`;
    const filterBar = `
        <div class="cmp-filterbar">
            ${filterBtn("all", "全部", s.total_cells)}
            ${CMP_CATEGORIES.map((c) => filterBtn(c.key, c.label, s[c.key])).join("")}
            <span class="cmp-filter-spacer"></span>
            <button id="btn-cmp-csv" class="tiny ghost">⬇ 下载 CSV（当前筛选）</button>
        </div>`;

    $("#cmp-result").innerHTML = `
        <div class="cmp-result-inner">
            <div class="ab-result-head">
                <h2>对比结果</h2>
                <span class="muted">基准 <span class="ab-eid">${escapeHtml(data.base.id)}</span>
                    （${escapeHtml(data.base.created_at)}） →
                    对比 <span class="ab-eid">${escapeHtml(data.target.id)}</span>
                    （${escapeHtml(data.target.created_at)}）</span>
            </div>
            <div class="muted">对齐档案 ${data.aligned_profiles.length} 档：${data.aligned_profiles.map(escapeHtml).join(" / ")}
                · 共同查询 ${data.queries.common} 条 · 共 ${s.total_cells} 个对比单元</div>
            ${snapWarn}
            ${unpairedNote}
            ${filterBar}
            <div id="cmp-cells">${renderCompareCells()}</div>
        </div>`;
}

async function submitComparison() {
    const baseId = $("#cmp-base").value;
    const targetId = $("#cmp-target").value;
    $("#cmp-errors").innerHTML = "";
    $("#cmp-result").innerHTML = "";
    cmpResult = null;
    if (!baseId || !targetId) {
        renderCompareErrors([{ type: "request", message: "请先在「历史实验」中运行或选择两次实验" }]);
        return;
    }
    const btn = $("#btn-cmp-run");
    btn.disabled = true;
    try {
        const res = await fetch("/api/experiments/compare", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ base_id: baseId, target_id: targetId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            renderCompareErrors(data.errors || [{ type: "request", message: data.detail || "请求失败" }]);
            return;
        }
        cmpResult = data;
        cmpFilter = "all";
        renderCompareResult();
    } catch (err) {
        renderCompareErrors([{ type: "request", message: err.message }]);
    } finally {
        btn.disabled = false;
    }
}

$("#btn-cmp-run").addEventListener("click", submitComparison);

// Category filter clicks (delegated: the bar is re-rendered with the result).
$("#cmp-result").addEventListener("click", (event) => {
    const filterBtn = event.target.closest(".cmp-filter");
    if (filterBtn) {
        cmpFilter = filterBtn.dataset.cat;
        renderCompareResult();
        return;
    }
    if (event.target.closest("#btn-cmp-csv")) {
        downloadCompareCsv();
    }
});

function csvEscape(value) {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function downloadCompareCsv() {
    if (!cmpResult) return;
    const header = ["查询", "档案", "分类", "基准状态", "基准首选ID", "基准得分",
        "对比状态", "对比首选ID", "对比得分", "得分差", "说明"];
    const rows = cmpFilteredCells().map((c) => [
        c.query,
        c.profile,
        CMP_CATEGORY_LABEL[c.category] || c.category,
        STATUS_LABEL[c.base.status] || c.base.status,
        c.base.best_id ?? "",
        c.base.best_score ?? "",
        STATUS_LABEL[c.target.status] || c.target.status,
        c.target.best_id ?? "",
        c.target.best_score ?? "",
        c.score_delta ?? "",
        c.reason,
    ]);
    // BOM so Excel opens the UTF-8 file with the right encoding.
    const csv = "﻿" + [header, ...rows]
        .map((row) => row.map(csvEscape).join(","))
        .join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `regression_${cmpResult.base.id}_vs_${cmpResult.target.id}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
}
