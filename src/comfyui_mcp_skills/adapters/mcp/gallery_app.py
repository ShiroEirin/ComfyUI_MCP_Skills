"""Static MCP App for the ComfyUI output gallery.

Served with the app MIME type and read through the MCP resource layer, so
every data access keeps the session's scope and ownership checks. The gallery
lists the caller's recent Jobs from ``comfyui://gallery/jobs`` and lazily
resolves per-job details and binary output previews on demand; no server-side
state and no dependencies.
"""

from __future__ import annotations

GALLERY_URI = "ui://comfyui/gallery.html"
GALLERY_DATA_URI = "comfyui://gallery/jobs"


def gallery_html() -> str:
    """Static, dependency-free output gallery served with the app MIME type."""
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>ComfyUI 输出图库</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;
       padding:0 1rem;color:#1f2933}
  h1{font-size:1.25rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
        gap:1rem;margin-top:1rem}
  .card{border:1px solid #e2e8f0;border-radius:8px;padding:.75rem;cursor:pointer}
  .card:hover{border-color:#94a3b8}
  .status{display:inline-block;padding:.1rem .5rem;border-radius:999px;
          font-size:.75rem;background:#f1f5f9}
  .status.completed{background:#dcfce7;color:#166534}
  .status.error,.status.failed{background:#fee2e2;color:#991b1b}
  pre{background:#f5f7fa;padding:1rem;overflow:auto;border-radius:6px;font-size:.8rem}
  .out{font-size:.8rem;color:#475569;white-space:nowrap;overflow:hidden;
       text-overflow:ellipsis}
  img{max-width:100%;border-radius:6px;margin-top:.5rem}
  a{color:#2563eb;cursor:pointer}
  button{margin-top:.5rem;padding:.5rem 1rem}
</style>
</head>
<body>
<h1>ComfyUI 输出图库</h1>
<p>最近任务与输出（经 <code>comfyui://gallery/jobs</code> 资源读取，仅当前会话可见范围）。</p>
<pre id="status">加载中…</pre>
<div class="grid" id="grid"></div>
<div id="pager"></div>
<script>
const grid = document.getElementById("grid");
const statusEl = document.getElementById("status");
const pager = document.getElementById("pager");
let cursor = "";

async function readText(uri) {
  const result = await client.readResource({uri});
  const part = result && result.contents && result.contents[0];
  if (!part) throw new Error("empty resource " + uri);
  if (part.text !== undefined) return part.text;
  if (part.blob !== undefined) {
    const bytes = atob(part.blob);
    const array = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) array[i] = bytes.charCodeAt(i);
    return new TextDecoder().decode(array);
  }
  throw new Error("unsupported resource content for " + uri);
}

async function loadPage() {
  statusEl.textContent = "读取任务列表…";
  try {
    const uri = "comfyui://gallery/jobs" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : "");
    const text = await readText(uri);
    const data = JSON.parse(text);
    grid.textContent = "";
    if (data.available === false) {
      statusEl.textContent = "任务列表不可用: " + (data.reason || "未知原因");
      pager.textContent = "";
      return;
    }
    if (!data.items || !data.items.length) {
      statusEl.textContent = "暂无任务";
      pager.textContent = "";
      return;
    }
    for (const item of data.items) {
      const card = document.createElement("div");
      card.className = "card";
      const header = document.createElement("div");
      const statusBadge = document.createElement("span");
      statusBadge.className = "status";
      const statusClass = {completed: true, error: true, failed: true}[item.status];
      if (statusClass) statusBadge.classList.add(item.status);
      statusBadge.textContent = item.status || "?";
      const title = document.createElement("strong");
      title.textContent = item.workflow_id || "?";
      header.appendChild(statusBadge);
      header.appendChild(document.createTextNode(" "));
      header.appendChild(title);
      card.appendChild(header);
      const idLine = document.createElement("div");
      idLine.className = "out";
      idLine.textContent = item.job_id || "";
      card.appendChild(idLine);
      const metaLine = document.createElement("div");
      metaLine.className = "out";
      metaLine.textContent = "server: " + (item.server_id || "") +
        " · " + (item.created_at || "");
      card.appendChild(metaLine);
      const pre = document.createElement("pre");
      pre.hidden = true;
      card.appendChild(pre);
      card.addEventListener("click", () => expandCard(card, item));
      grid.appendChild(card);
    }
    cursor = data.next_cursor || "";
    pager.textContent = "";
    if (cursor) {
      const next = document.createElement("button");
      next.textContent = "下一页";
      next.addEventListener("click", loadPage);
      pager.appendChild(next);
    }
    statusEl.textContent = data.items.length + " 个任务";
  } catch (error) {
    statusEl.textContent = "读取失败: " + (error && error.message ? error.message : String(error));
  }
}

async function expandCard(card, item) {
  const pre = card.querySelector("pre");
  const strip = card.querySelector(".media-strip");
  if (pre.textContent || strip) {
    pre.hidden = !pre.hidden;
    if (strip) strip.hidden = !strip.hidden;
    return;
  }
  pre.hidden = false;
  pre.textContent = "读取任务详情…";
  try {
    const job = JSON.parse(await readText("comfyui://jobs/" + encodeURIComponent(item.job_id)));
    const outputs = job.outputs && job.outputs.length ? job.outputs : [];
    let html = "状态: " + (job.status || "?") + "\\n";
    if (!outputs.length) { html += "无输出\\n"; }
    for (let i = 0; i < outputs.length; i++) {
      const out = outputs[i];
      html += "输出[" + i + "] " + (out.filename || "") +
              " (" + (out.media_type || out.mime_type || "") + ")\\n";
    }
    pre.textContent = html;
    if (outputs.length) {
      const strip = document.createElement("div");
      strip.className = "media-strip";
      strip.style.cssText = "margin-top:.5rem;display:grid;gap:.5rem";
      pre.after(strip);
      outputs.forEach((out, index) => {
        renderOutput(strip, out, item, index);
      });
    }
  } catch (error) {
    pre.textContent = "读取失败: " + (error && error.message ? error.message : String(error));
  }
}

async function renderOutput(strip, out, item, index) {
  const uri = out.legacy_uri || ("comfyui://outputs/" +
    encodeURIComponent(item.server_id || "") + "/" +
    encodeURIComponent(out.prompt_id || item.job_id || "") + "/" + index);
  const box = document.createElement("div");
  box.style.cssText = "border-top:1px solid #e2e8f0;padding-top:.5rem";
  const label = document.createElement("div");
  label.className = "out";
  label.textContent = "[" + index + "] " + (out.filename || uri) +
    " (" + (out.media_type || out.mime_type || "") + ")";
  box.appendChild(label);
  try {
    const result = await client.readResource({uri});
    const part = result && result.contents && result.contents[0];
    if (!part || part.blob === undefined) {
      box.appendChild(document.createTextNode("（无二进制预览）"));
      strip.appendChild(box);
      return;
    }
    const bytes = atob(part.blob);
    const array = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) array[i] = bytes.charCodeAt(i);
    const blob = new Blob([array], {type: out.mime_type || "application/octet-stream"});
    const mime = out.mime_type || "";
    const url = URL.createObjectURL(blob);
    if (mime.startsWith("image/")) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = out.filename || "output";
      img.loading = "lazy";
      box.appendChild(img);
    } else if (mime.startsWith("video/")) {
      const video = document.createElement("video");
      video.src = url;
      video.controls = true;
      video.preload = "metadata";
      video.style.maxWidth = "100%";
      box.appendChild(video);
    } else if (mime.startsWith("audio/")) {
      const audio = document.createElement("audio");
      audio.src = url;
      audio.controls = true;
      box.appendChild(audio);
    } else {
      box.appendChild(document.createTextNode("（" + mime + "）"));
    }
    strip.appendChild(box);
  } catch (error) {
    const fail = document.createElement("div");
    fail.className = "preview-fail";
    fail.textContent = "预览失败: " + (error && error.message ? error.message : String(error));
    box.appendChild(fail);
    strip.appendChild(box);
  }
}

loadPage();
</script>
</body>
</html>
"""
