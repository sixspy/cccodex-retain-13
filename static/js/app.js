(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "待放行"
        ? "warn"
        : state === "已完成" ||
            state === "已归档" ||
            state === "已销毁" ||
            state === "已放行"
          ? "ok"
          : state === "已失败" || state === "已拒绝"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${esc(p.domain_name)}</td>
              <td class="mono">${esc(p.domain_code)}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。放行计数与放行队列、对账详情同源；已销毁空分区不可放行。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th><th>放行（待/准/拒）</th><th>操作</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${esc(p.domain_code)}</td>
              <td class="mono">${esc(p.biz_date)}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${esc(p.archive_uri) || "—"}</td>
              <td class="mono">${p.release_pending} / ${p.release_approved} / ${p.release_rejected}</td>
              <td><button type="button" class="secondary" data-rl="${p.id}">开放行单</button></td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-rl]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api("/api/release-orders", {
            method: "POST",
            body: JSON.stringify({ partition_id: Number(btn.dataset.rl) }),
          });
        } catch (e) {
          alert(e.message);
        }
        await show("release");
      };
    });
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${esc(j.as_of_date)}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${esc(j.message)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。执行结果写回分区存储态与执行流水。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${esc(j.decided_action)}</td>
              <td>${tag(j.status)}</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="7" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        await renderJudgments();
      };
    });
  }

  function summaryCards(s) {
    return `
      <div class="cards">
        <div class="card"><b>${s.pending_count}</b><span>待放行</span></div>
        <div class="card ok"><b>${s.approved_count}</b><span>已放行</span></div>
        <div class="card bad"><b>${s.rejected_count}</b><span>已拒绝</span></div>
        <div class="card ${s.stale_pending_count ? "bad" : ""}"><b>${s.stale_pending_count}</b><span>悬空待放行（分区已销毁/空）</span></div>
      </div>`;
  }

  async function renderRelease() {
    const data = await api("/api/release-orders");
    main.innerHTML = `
      ${summaryCards(data.summary)}
      <section class="panel">
        <h2>下游入库放行队列</h2>
        <p class="sub">闸口判定与分区态对齐：仅「在线且有量」可放行；已销毁/空分区、已归档、裁撤中一律拦截，拒绝原因与判定时分区态留痕在放行单上。</p>
        <table>
          <thead><tr>
            <th>单号</th><th>主题域</th><th>业务日</th><th>申请快照</th>
            <th>分区当前态</th><th>闸口结果</th><th>拒绝原因 / 留痕</th><th>操作人</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (o) => `<tr>
              <td class="mono">${esc(o.order_no)}</td>
              <td class="mono">${esc(o.domain_code)}</td>
              <td class="mono">${esc(o.biz_date)}</td>
              <td>${tag(o.snapshot_state)}<span class="mono sub"> ${o.snapshot_row_count}行</span></td>
              <td>${tag(o.live_state)}<span class="mono sub"> ${o.live_row_count}行</span></td>
              <td>${tag(o.status)}${o.decided_state ? `<div class="sub mono">判定态:${esc(o.decided_state)}</div>` : ""}</td>
              <td class="reason">${esc(o.reject_reason) || "—"}</td>
              <td>${esc(o.operator) || "—"}</td>
              <td>${
                o.status === "待放行"
                  ? `<button type="button" data-decide="${o.id}">过闸放行</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无放行单，可在实物分区页开单</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-decide]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          const r = await api(`/api/release-orders/${btn.dataset.decide}/decide`, {
            method: "POST",
          });
          if (r.order.status === "已拒绝") {
            alert(`闸口拦截：${r.order.reject_reason}（分区态：${r.order.decided_state}）`);
          }
        } catch (e) {
          alert(e.message);
        }
        await renderRelease();
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    const latest = reports.items[0];
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr>
            <th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th>
            <th>待裁决</th><th>错位</th><th>放行待/准/拒</th><th>悬空放行</th><th>摘要</th>
          </tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td class="mono">${r.release_pending_count}/${r.release_approved_count}/${r.release_rejected_count}</td>
              <td>${r.release_stale_count ? `<span class="tag bad">${r.release_stale_count}</span>` : 0}</td>
              <td>${esc(r.summary)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>
      ${
        latest
          ? `<section class="panel">
          <h2>最新报告详情（编号 ${latest.id}）</h2>
          <p class="sub">放行闸口对账：待放行但分区实物已销毁/为空的单据必须拦截，不得当入库源。</p>
          <div id="rec-detail" class="sub">加载详情中…</div>
        </section>`
          : ""
      }`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
    if (latest) {
      const detail = await api(`/api/reconcile-reports/${latest.id}`);
      const stale = detail.report.stale_release_orders || [];
      const el = document.getElementById("rec-detail");
      if (el) {
        el.classList.remove("sub");
        el.innerHTML = stale.length
          ? `<table>
              <thead><tr><th>放行单号</th><th>主题域</th><th>业务日</th><th>申请快照</th><th>分区当前态</th></tr></thead>
              <tbody>
                ${stale
                  .map(
                    (o) => `<tr>
                  <td class="mono">${esc(o.order_no)}</td>
                  <td class="mono">${esc(o.domain_code)}</td>
                  <td class="mono">${esc(o.biz_date)}</td>
                  <td>${tag(o.snapshot_state)} <span class="mono sub">${o.snapshot_row_count}行</span></td>
                  <td>${tag(o.live_state)} <span class="mono sub">${o.live_row_count}行</span></td>
                </tr>`
                  )
                  .join("")}
              </tbody>
            </table>
            <p class="sub" style="color:var(--bad)">以上单据挂在待放行队列，但分区实物已不可放行，过闸将被拦截。</p>`
          : `<p class="sub">无悬空待放行单；放行待 ${latest.release_pending_count} · 已准 ${latest.release_approved_count} · 已拒 ${latest.release_rejected_count}。</p>`;
      }
    }
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    release: renderRelease,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
