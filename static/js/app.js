(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  const AS_OF = "2026-11-16";

  let tab = "batches";
  let templates = [];
  let batches = [];
  let reports = [];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;

  let directory = null;
  let refillTable = "";
  let refillValue = "";
  let refillPreview = null;
  let refillResult = null;   // {ok:true,...} 或 {ok:false,error,reason_chip}
  let refillRuns = [];
  let deadLetters = [];
  let replayState = {};      // runId -> replay result
  let attachment = null;     // 批次附页
  let attachmentFor = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || ("HTTP " + r.status));
      err.payload = j;
      err.reasonChip = j.reason_chip || "";
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function chip(text, kind = "") {
    if (!text) return "";
    return `<span class="chip ${kind}">${esc(text)}</span>`;
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
    directory = (await api("/api/missing-directory?as_of_date=" + AS_OF)).directory;
    refillRuns = (await api("/api/refill/runs")).items || [];
    deadLetters = (await api("/api/dead-letters")).items || [];
    if (attachmentFor) {
      try {
        attachment = await api("/api/batches/" + attachmentFor + "/attachment");
      } catch (e) {
        attachment = null;
        attachmentFor = null;
      }
    }
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${t.version_no}</td>
            <td>${chip(t.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderAttachment() {
    if (!attachment) return "";
    const b = attachment.batch;
    const items = attachment.items || [];
    const dead = attachment.dead_letters || [];
    return `
      <div class="cardish attach" id="attach-panel">
        <h3>批次附页 · ${esc(b.run_code)}</h3>
        <p class="muted">
          ${esc(b.table_name)} · ${esc(b.biz_date)} · ${chip(b.status)}
          ${b.readonly ? chip("留存只读", "warn") : ""}
          ${b.watermark_value ? "水印 " + esc(b.watermark_value) : "未盖印"}
        </p>
        <h4>补写痕迹</h4>
        ${
          items.length
            ? `<table>
            <thead><tr><th>补写编号</th><th>结论</th><th>拒因</th><th>落档状态</th></tr></thead>
            <tbody>${items
              .map(
                (i) => `<tr>
              <td>#${i.refill_run_id}</td>
              <td>${chip(i.outcome)}</td>
              <td>${chip(i.reason_code || "", "warn")}</td>
              <td>${esc(i.run_status || "")}</td>
            </tr>`
              )
              .join("")}</tbody></table>`
            : `<p class="muted">该批次没有补写档案。</p>`
        }
        <h4>死信痕迹</h4>
        ${
          dead.length
            ? `<ul>${dead
                .map(
                  (d) => `<li>死信 #${d.id} · ${chip(d.reason_code, "bad")} · ${esc(d.message)}</li>`
                )
                .join("")}</ul>`
            : `<p class="muted">无死信记录。</p>`
        }
      </div>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账 → 漏印补写</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td>${chip(b.status, b.status === "已盖印" ? "ok" : "")}</td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>
              ${b.status === "待盖印" ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>` : ""}
              <button type="button" class="ghost" data-attach="${b.id}">附页</button>
            </td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }
      ${attachment && attachmentFor ? renderAttachment() : ""}`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src.map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`).join("")}</ul>
        <h3>下游</h3>
        ${
          down.length
            ? `<ul>${down
                .map(
                  (d) =>
                    `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
                )
                .join("")}</ul>`
            : `<p class="muted">没有下游批次。</p>`
        }
        <p class="muted">
          源 ${traceResult.source_count || src.length} ·
          边 ${traceResult.edge_count || 0} ·
          下游 ${traceResult.downstream_count || down.length}
        </p>
      </div>`
          : ""
      }`;
  }

  function renderDirectory() {
    if (!directory) return `<p class="muted">漏印目录读取中…</p>`;
    const items = directory.items || [];
    return `
      <h3>漏印目录 · 截至 ${esc(directory.as_of_date)}</h3>
      <p class="muted">
        应盖印 ${directory.expected_count} · 已盖印 ${directory.stamped_count} ·
        漏印 ${directory.missing_count}
      </p>
      <table>
        <thead><tr><th>表</th><th>应盖印</th><th>已盖印</th><th>漏印</th><th>漏印批次</th></tr></thead>
        <tbody>
          ${
            items.length
              ? items
                  .map(
                    (t) => `<tr>
              <td>${esc(t.table_name)}</td>
              <td>${t.expected}</td>
              <td>${t.stamped}</td>
              <td>${chip(String(t.missing), t.missing ? "warn" : "ok")}</td>
              <td>${esc((t.missing_run_codes || []).join("、") || "-")}</td>
            </tr>`
                  )
                  .join("")
              : `<tr><td colspan="5" class="muted">没有漏印。</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderRefillCard() {
    const p = refillPreview;
    return `
      <h3>补写卡片</h3>
      <label>目标表
        <select id="refill-table">
          <option value="">（选择表）</option>
          ${templates
            .filter((t) => t.status === "现行")
            .map(
              (t) =>
                `<option value="${esc(t.table_name)}" ${refillTable === t.table_name ? "selected" : ""}>${esc(t.table_name)}</option>`
            )
            .join("")}
        </select>
      </label>
      <label>水印值<input id="refill-input" value="${esc(refillValue)}" placeholder="补写所用印值" /></label>
      <div class="row">
        <button type="button" class="ghost" id="btn-preview">查看范围</button>
        <button type="button" id="btn-refill" ${!p || !p.scope_count ? "disabled" : ""}>一次落档补写</button>
      </div>
      ${
        p
          ? `<div class="scope ${p.scope_count ? "on" : "off"}" id="scope-badge">
        <p>范围标记：
          ${chip(p.scope_count ? "补写 " + p.scope_count + " 行" : "无漏印", p.scope_count ? "warn" : "ok")}
          ${chip("留存只读 " + p.retained_count, "mute")}
          ${chip("范围指纹 " + p.scope_token, "mute")}
        </p>
        <p class="muted">补写批次：${esc((p.missing_items || []).map((m) => m.run_code).join("、") || "-")}</p>
        <p class="muted">留存只读批次：${esc((p.retained_ids || []).join("、") || "-")}</p>
      </div>`
          : ""
      }
      ${
        refillResult
          ? `<div class="result ${refillResult.ok ? "ok" : "bad"}" id="refill-result">
        ${
          refillResult.ok
            ? `<p>${chip("已落档", "ok")} 补写 ${refillResult.applied_count} 行，留存 ${refillResult.retained_count} 行只读</p>
               <p class="muted">范围指纹 ${esc(refillResult.scope_token)} · 补写编号 #${refillResult.refill_run_id}</p>`
            : `<p>${chip(refillResult.reason_chip || "拒绝", "bad")} ${esc(refillResult.error)}</p>
               <p class="muted">${refillResult.dead_lettered ? "整笔已回滚并写入死信队列" : ""}
               ${refillResult.refill_run_id ? " · 补写编号 #" + refillResult.refill_run_id : ""}</p>`
        }
      </div>`
          : ""
      }`;
  }

  function renderRuns() {
    return `
      <h3>补写档案</h3>
      ${
        refillRuns.length
          ? `<table>
        <thead><tr><th>#</th><th>表</th><th>结论</th><th>拒因</th><th>补写</th><th>留存</th><th>范围</th><th></th></tr></thead>
        <tbody>${refillRuns
          .map(
            (r) => `<tr>
          <td>${r.id}</td>
          <td>${esc(r.table_name)}</td>
          <td>${chip(r.status, r.status === "已落档" ? "ok" : "bad")}</td>
          <td>${r.reason_code ? chip(r.reason_chip || r.reason_code, "bad") : "-"}</td>
          <td>${r.applied_count}</td>
          <td>${r.retained_count}</td>
          <td class="muted">${esc(r.scope_token)}</td>
          <td><button type="button" class="ghost" data-replay="${r.id}">回放</button></td>
        </tr>`
          )
          .join("")}</tbody></table>`
          : `<p class="muted">还没有补写档案。</p>`
      }`;
  }

  function renderReplay() {
    const ids = Object.keys(replayState);
    if (!ids.length) return "";
    return `<h3>回放进度</h3>${ids
      .map((id) => {
        const r = replayState[id];
        const rounds = r.rounds || [];
        return `<div class="cardish replay">
        <p>
          补写 #${r.refill_run_id} · ${chip(r.run_status, r.run_status === "已落档" ? "ok" : "bad")}
          ${r.reason_code ? chip("拒因 " + (r.reason_chip || r.reason_code), "bad") : ""}
          ${chip(r.two_rounds_aligned ? "两轮数字对齐" : "两轮待复核", r.two_rounds_aligned ? "ok" : "warn")}
        </p>
        <p class="muted">本轮：补写 ${r.applied_count} · 留存 ${r.retained_count} · 范围 ${r.expected_count}
          ${r.aligned ? chip("本轮对齐", "ok") : chip("本轮不符", "bad")}</p>
        <table>
          <thead><tr><th>轮次</th><th>补写</th><th>留存</th><th>范围</th><th>对齐</th><th>与上轮一致</th></tr></thead>
          <tbody>${rounds
            .map(
              (x) => `<tr>
            <td>${x.round_no}</td>
              <td>${x.applied_count}</td>
              <td>${x.retained_count}</td>
              <td>${x.expected_count}</td>
              <td>${x.aligned ? chip("对齐", "ok") : chip("不符", "bad")}</td>
              <td>${x.matches_previous === null ? "-" : x.matches_previous ? chip("一致", "ok") : chip("不一致", "bad")}</td>
            </tr>`
            )
            .join("")}</tbody>
        </table>
      </div>`;
      })
      .join("")}`;
  }

  function renderDeadLetters() {
    return `
      <h3>死信队列</h3>
      ${
        deadLetters.length
          ? `<table>
        <thead><tr><th>#</th><th>表</th><th>拒因</th><th>说明</th><th>涉及批次</th></tr></thead>
        <tbody>${deadLetters
          .map(
            (d) => `<tr>
          <td>${d.id}</td>
          <td>${esc(d.table_name)}</td>
          <td>${chip(d.reason_chip || d.reason_code, "bad")}</td>
          <td>${esc(d.message)}</td>
          <td class="muted">${esc(
            Array.isArray(d.batch_ids) ? d.batch_ids.join("、") : d.batch_ids
          )}</td>
        </tr>`
          )
          .join("")}</tbody></table>`
          : `<p class="muted">死信队列为空。</p>`
      }`;
  }

  function renderGaps() {
    const latest = reports[0];
    return `
      <h2>缺口对账</h2>
      <div class="row"><button type="button" id="btn-gap">生成对账</button></div>
      ${
        latest
          ? `<div class="cardish">
        <p>${chip("差额账与实印同源", latest.consistent ? "ok" : "bad")}
          ${latest.consistent ? "" : chip("目录与差额不同源", "bad")}</p>
        <p class="muted">应盖印 ${latest.expected_count} · 已盖印 ${latest.stamped_count} ·
          缺口 ${latest.gap_count} · 漏印 ${latest.missing_count}</p>
      </div>`
          : ""
      }
      ${renderDirectory()}
      <h3>对账记录</h3>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>漏印</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${r.gap_count}</td>
            <td>${r.missing_count}</td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderRefill() {
    return `
      <h2>漏印补写</h2>
      <p class="muted">四处同看：漏印目录 → 补写卡片 → 批次附页 → 回放进度</p>
      ${renderDirectory()}
      ${renderRefillCard()}
      ${renderReplay()}
      ${renderRuns()}
      ${renderDeadLetters()}`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else if (tab === "gaps") view.innerHTML = renderGaps();
    else view.innerHTML = renderRefill();

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    view.querySelectorAll("[data-attach]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.attach);
        try {
          attachment = await api("/api/batches/" + id + "/attachment");
          attachmentFor = id;
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.reasonChip ? e.reasonChip + "：" + e.message : e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: AS_OF }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const tableSel = document.getElementById("refill-table");
    if (tableSel) {
      tableSel.onchange = () => {
        refillTable = tableSel.value;
        refillPreview = null;
        refillResult = null;
        render();
      };
    }
    const refillInput = document.getElementById("refill-input");
    if (refillInput) {
      refillInput.oninput = () => (refillValue = refillInput.value);
    }
    const previewBtn = document.getElementById("btn-preview");
    if (previewBtn) {
      previewBtn.onclick = async () => {
        setMsg("");
        refillResult = null;
        try {
          refillPreview = await api(
            "/api/refill/preview?table_name=" + encodeURIComponent(refillTable) +
              "&as_of_date=" + AS_OF
          );
          if (!refillValue && refillPreview.scope_count) {
            refillValue = "WM-FILL-" + refillTable.toUpperCase();
          }
          setMsg("已按漏印目录取范围");
        } catch (e) {
          refillPreview = null;
          setMsg(e.reasonChip ? e.reasonChip + "：" + e.message : e.message);
        }
        render();
      };
    }
    const refillBtn = document.getElementById("btn-refill");
    if (refillBtn) {
      refillBtn.onclick = async () => {
        setMsg("");
        try {
          refillResult = await api("/api/batches/refill", {
            method: "POST",
            body: JSON.stringify({
              table_name: refillTable,
              watermark_value: refillValue,
              scope_ids: refillPreview ? refillPreview.scope_ids : [],
              as_of_date: AS_OF,
            }),
          });
          setMsg("补写一次落档");
        } catch (e) {
          refillResult = {
            ok: false,
            error: e.message,
            reason_chip: e.reasonChip,
            dead_lettered: !!(e.payload && e.payload.dead_lettered),
            refill_run_id: e.payload && e.payload.refill_run_id,
          };
          setMsg("补写被拒绝");
        }
        refillPreview = null;
        await refresh();
        render();
      };
    }
    view.querySelectorAll("[data-replay]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.replay);
        setMsg("");
        try {
          replayState[id] = await api("/api/refill/runs/" + id + "/replay", { method: "POST" });
          const r = replayState[id];
          setMsg(
            r.two_rounds_aligned
              ? "回放两轮数字已对齐"
              : "回放已完成，需再回放一轮复核"
          );
          render();
        } catch (e) {
          setMsg(e.reasonChip ? e.reasonChip + "：" + e.message : e.message);
        }
      };
    });
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = async () => {
      tab = btn.dataset.tab;
      syncDock();
      try {
        await refresh();
      } catch (e) {
        setMsg(e.message);
      }
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
