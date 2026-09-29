/* 跨区域移交页面操作：发起 / 接受 / 拒绝 / 撤销，全部走独立的 transfers API。
   规则、数据、权限均在服务端校验，这里只负责收集输入与展示状态。 */
const STATUS_LABEL = {pending: "待受理", accepted: "已接受", rejected: "已拒绝", canceled: "已撤销", stale: "已失效"};

function authHeaders(extra) {
  const h = {"X-User-Id": document.getElementById("user").value,
             "X-Role": document.getElementById("role").value};
  const region = document.getElementById("region").value.trim();
  if (region) h["X-Region"] = region;
  return Object.assign(h, extra || {});
}

async function call(method, url, body) {
  const opt = {method, headers: authHeaders({"Content-Type": "application/json"})};
  if (body) opt.body = JSON.stringify(body);
  const res = await fetch(url, opt);
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || data.error || "操作失败");
  return data;
}

async function createTransfer(form) {
  const caseId = form.elements["case_id"].value.trim();
  const body = {
    target_region: form.elements["target_region"].value.trim().toUpperCase(),
    reason: form.elements["reason"].value.trim(),
    read_version: Number(form.elements["read_version"].value),
  };
  try {
    await call("POST", `/api/cases/${encodeURIComponent(caseId)}/transfers`, body);
    await refresh();
  } catch (e) { alert(e.message); }
}

async function respond(id, action) {
  let body = {};
  if (action === "reject") {
    const reason = prompt("请填写拒绝理由");
    if (reason === null) return;
    if (!reason.trim()) { alert("拒绝理由必填"); return; }
    body = {reason: reason.trim()};
  }
  try {
    await call("POST", `/api/transfers/${id}/${action}`, body);
    await refresh();
  } catch (e) { alert(e.message); }
}

async function cancelTransfer(id) {
  if (!confirm("确认撤销该移交申请？")) return;
  try {
    await call("POST", `/api/transfers/${id}/cancel`, {});
    await refresh();
  } catch (e) { alert(e.message); }
}

async function refresh() {
  const data = await call("GET", "/api/transfers");
  const panel = document.getElementById("transfer-panel");
  const rows = (data.transfers || []).map(t => {
    const actions = t.status === "pending"
      ? `<button onclick="respond(${t.id},'accept')">接受</button>
         <button onclick="respond(${t.id},'reject')">拒绝</button>
         <button onclick="cancelTransfer(${t.id})">管理员撤销</button>`
      : "";
    const note = t.response_reason ? `；理由：${escapeHtml(t.response_reason)}`
      : t.closed_reason ? `；${escapeHtml(t.closed_reason)}` : "";
    return `<tr><td>${t.id}</td><td>${t.case_id}</td><td>${t.from_region} → ${t.to_region}</td>
      <td>${t.read_version}</td><td><span class="tag">${STATUS_LABEL[t.status] || t.status}</span>${escapeHtml(note)}</td>
      <td>${escapeHtml(t.requested_by)}</td><td>${actions}</td></tr>`;
  }).join("");
  panel.innerHTML = `
    <form onsubmit="event.preventDefault();createTransfer(this)">
      案例ID <input name="case_id" style="width:80px"> 目标区域 <input name="target_region" style="width:80px">
      读取版本 <input name="read_version" type="number" min="1" style="width:90px">
      原因 <input name="reason" placeholder="例如：错录区域">
      <button type="submit">发起移交</button>
    </form>
    <table><thead><tr><th>#</th><th>案例</th><th>区域</th><th>读取版本</th><th>状态</th><th>发起人</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7">暂无移交申请</td></tr>'}</tbody></table>`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

window.renderTransfers = async function () {
  try { await refresh(); } catch (e) {
    document.getElementById("transfer-panel").textContent = e.message;
  }
};
