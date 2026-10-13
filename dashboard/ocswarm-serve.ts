// Adapted for Ocswarm. See ../THIRD_PARTY_NOTICES.md.
// Live Ocswarm dashboard for one ocswarm directory. Edit these two values to change the bind.
const UI_HOST = "127.0.0.1";
const UI_PORT = 8124;

import { spawn } from "node:child_process";
import { openSync, closeSync, readSync, statSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { basename, join, resolve, sep } from "node:path";
import type { DashboardData, SwarmSlot, SwarmTaskRecord } from "./data.ts";
import { renderDashboardHtml } from "./render.js";

function argValue(flag: string): string {
	const index = process.argv.indexOf(flag);
	return index >= 0 ? process.argv[index + 1] || "" : "";
}

const root = process.argv.slice(2).find(arg => arg && !arg.startsWith("--")) || "";
const port = Number(argValue("--port") || UI_PORT);
const openBrowser = process.argv.includes("--open");
if (!root || !Number.isFinite(port)) {
	console.error("usage: node ocswarm-serve.ts DIRECTORY [--port N] [--open]");
	process.exit(1);
}

function kindFor(slot: SwarmSlot): string {
	if (slot.role === "orchestrator") return "class";
	if (slot.state === "run") return "method";
	if (slot.state === "wait") return "interface";
	if (slot.state === "fail") return "enum";
	if (slot.state === "armed") return "function";
	return "const";
}

function readBoard(): { board: Record<string, unknown>; error: string } {
	const path = join(root, ".ocswarm", "board.json");
	let raw = "";
	try {
		raw = readFileSync(path, "utf8");
	} catch (err) {
		return { board: {}, error: err instanceof Error ? err.message : String(err) };
	}
	try {
		const board = JSON.parse(raw) as Record<string, unknown>;
		if (!board || typeof board !== "object" || Array.isArray(board)) return { board: {}, error: "board.json is not an object" };
		return { board, error: "" };
	} catch (err) {
		return { board: {}, error: err instanceof Error ? err.message : String(err) };
	}
}

function slotOf(raw: unknown): SwarmSlot {
	const row = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
	return {
		name: String(row.name || ""),
		role: String(row.role || ""),
		state: String(row.state || ""),
		item: String(row.item || ""),
		activity: String(row.activity || ""),
    model: String(row.model || ""),
    elapsed: Number(row.elapsed || 0),
    log: String(row.log || ""),
	};
}

function taskRecordOf(raw: unknown): SwarmTaskRecord | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const row = raw as Record<string, unknown>;
  const id = String(row.id || "");
  if (!id) return null;
  const text = (key: string) => typeof row[key] === "string" ? row[key] as string : undefined;
  const strings = (key: string) => Array.isArray(row[key]) ? (row[key] as unknown[]).filter((value): value is string => typeof value === "string") : undefined;
  return {
    id,
    title: String(row.title || id),
    state: String(row.state || "pending"),
    role: text("role"),
    brief: text("brief"),
    summary: text("summary"),
    last_error: text("last_error"),
    owns: strings("owns"),
    files: strings("files"),
    evidence: strings("evidence"),
    model: text("model"),
    time: text("time"),
    tests_passed: typeof row.tests_passed === "boolean" ? row.tests_passed : undefined,
  };
}

function taskRecords(value: unknown): SwarmTaskRecord[] {
  return Array.isArray(value) ? value.map(taskRecordOf).filter((task): task is SwarmTaskRecord => task !== null) : [];
}

function load(): { page: DashboardData; live: Record<string, unknown> } {
	const { board, error } = readBoard();
	const slots = Array.isArray(board.slots) ? board.slots.map(slotOf) : [];
	const running = slots.filter(slot => slot.state === "run").length;
	const waiting = slots.filter(slot => slot.state === "wait").length;
	const done = Number(board.done || 0);
	const total = Number(board.total || 0);
	const phase = String(board.phase || "");
	const message = error || String(board.message || "");
	const task = String(board.task || root);
	const title = basename(resolve(root)) + " В· continuous reverse engineering";
  let taskState: { tasks: SwarmTaskRecord[]; history: SwarmTaskRecord[] } = { tasks: [], history: [] };
  try {
    const parsed = JSON.parse(readFileSync(join(root, ".ocswarm", "state-free.json"), "utf8"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      taskState = { tasks: taskRecords(parsed.tasks), history: taskRecords(parsed.history) };
    }
  } catch { /* State is optional while the swarm starts. */ }
	const updated = String(board.updated || "");
	const nodes = slots.map(slot => ({
		id: slot.name,
		label: slot.name,
		kind: kindFor(slot),
		source_file: slot.item,
		source_location: slot.state,
	}));
	const links = slots.filter(slot => slot.role !== "orchestrator" && slot.name).map(slot => ({
		source: "orchestrator",
		target: slot.name,
		relation: slot.state,
	}));
	const page: DashboardData = {
		repoKey: "ocswarm",
		repoProject: title,
		generatedAt: updated || new Date().toISOString(),
		kpis: { tokensSaved: null, tokensSource: "none", skillsCreated: 0, memorySearches: 0, sessionsCount: null, userTokensSaved: null },
		graph: nodes.length ? { commitSha: null, snapshotPath: join(root, ".ocswarm", "board.json"), nodeCount: nodes.length, edgeCount: links.length, snapshot: { nodes, links } } : null,
		swarm: { phase, message, task: title, done, total, running, waiting, slots, tasks: taskState.tasks, history: taskState.history },
	};
  return { page, live: { buildVersion: 3, directory: root, updated, phase, message, task, done, total, running, waiting, slots,
    workerModel: board.worker_model, liveLimit: board.live_limit, cost: board.cost || 0,
    requestsToday: board.providers && typeof board.providers === "object" ? Object.values(board.providers as Record<string, { requests_today?: number }>).reduce((sum, row) => sum + Number(row.requests_today || 0), 0) : board.requests_today || 0, baselineOk: board.baseline_ok,
    taskState,
    milestone: board.milestone || "",
    provider: board.provider || {},
    providers: board.providers || {},
    router: board.router || {},
    events: tail(join(root, ".ocswarm", "events.jsonl"), 18000),
    traces: tail(join(root, ".ocswarm", "traces.jsonl"), 18000),
  } };
}

function tail(path: string, limit: number): string {
  try {
    const size = statSync(path).size;
    const buffer = Buffer.alloc(Math.min(limit, size));
    const fd = openSync(path, "r");
    try { readSync(fd, buffer, 0, buffer.length, Math.max(0, size - buffer.length)); }
    finally { closeSync(fd); }
    return buffer.toString("utf8").replace(/nvapi-[A-Za-z0-9_-]{10,}/g, "[REDACTED]");
  } catch { return ""; }
}

function detailsHtml(): string {
  return `<section class="graph-card live-panel">
    <div class="live-heading"><h2>Activity stream</h2><span class="signal">в—Џ LIVE</span></div>
    <div class="telemetry-grid">
      <div class="telemetry"><span>MODEL</span><b id="telemetry-model">вЂ”</b></div>
      <div class="telemetry"><span>WORKERS</span><b id="telemetry-cap">вЂ”</b></div>
      <div class="telemetry"><span>REQUESTS TODAY</span><b id="telemetry-requests">вЂ”</b></div>
      <div class="telemetry"><span>PROVIDER ERRORS</span><b id="telemetry-errors">вЂ”</b></div>
      <div class="telemetry"><span>TEST GATE</span><b id="telemetry-tests">вЂ”</b></div>
      <div class="telemetry"><span>MILESTONE</span><b id="telemetry-milestone">вЂ”</b></div>
    </div>
    <div id="provider-summary" class="graph-meta">Free inference endpoints</div>
    <section aria-labelledby="free-providers-title"><h3 id="free-providers-title">Free providers</h3><div id="free-provider-table"></div><p id="router-status" class="graph-meta"></p></section>
    <label for="worker-log">Inspect worker</label><select id="worker-log"></select>
    <pre id="live-log" style="max-height:260px;overflow:auto;white-space:pre-wrap"></pre>
    <section class="agent-overview" aria-labelledby="agent-overview-title">
      <div class="agent-overview-heading"><div><h3 id="agent-overview-title">Agent roster</h3><p>Inspect active and idle workers</p></div><span id="agent-count"></span></div>
      <div class="agent-controls">
        <div class="task-filters" role="group" aria-label="Filter agents">
          <button type="button" data-agent-filter="all" class="active">All</button>
          <button type="button" data-agent-filter="run">Active</button>
          <button type="button" data-agent-filter="wait">Waiting</button>
          <button type="button" data-agent-filter="fail">Failed</button>
        </div>
        <label class="agent-search-label" for="agent-search">Find a worker</label>
        <input id="agent-search" type="search" placeholder="Name, task, or activity" autocomplete="off">
      </div>
      <div id="agent-cards" class="agent-cards"></div>
    </section>
    <details><summary>Mission</summary><p id="live-mission"></p></details>
    <details open><summary>Task board <span id="task-count"></span></summary>
      <div class="task-filters" role="group" aria-label="Filter tasks">
        <button type="button" data-task-filter="all" class="active">All</button>
        <button type="button" data-task-filter="running">Active</button>
        <button type="button" data-task-filter="pending">Queued</button>
        <button type="button" data-task-filter="done">Done</button>
        <button type="button" data-task-filter="failed">Failed</button>
      </div><div id="task-board" class="task-board"></div>
    </details>
    <details><summary>Accepted changes</summary><pre id="live-traces" style="max-height:300px;overflow:auto;white-space:pre-wrap"></pre></details>
    <details><summary>Supervisor events</summary><div id="event-filters" class="task-filters">
      <button type="button" data-event-filter="all" class="active">All events</button>
      <button type="button" data-event-filter="task">Tasks</button>
      <button type="button" data-event-filter="accept">Accepted</button>
      <button type="button" data-event-filter="fail">Failures</button>
    </div><pre id="live-events" style="max-height:300px;overflow:auto;white-space:pre-wrap"></pre></details>
  </section><script>(function(){
    var select=document.getElementById('worker-log');
    var taskFilter='all'; var eventFilter='all'; var agentFilter='all'; var latestData=null;
    function esc(value){return String(value||'').replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c];});}
    function taskRows(d){var state=d.taskState||{};var all=(state.tasks||[]).concat((state.history||[]).slice().reverse());var unique=[];var seen={};all.forEach(function(t){if(!seen[t.id]){seen[t.id]=true;unique.push(t);}});return unique;}
    function paintTasks(d){var rows=taskRows(d);var shown=rows.filter(function(t){return taskFilter==='all'||t.state===taskFilter||(taskFilter==='running'&&t.state==='running');});document.getElementById('task-count').textContent=rows.length+' total';document.getElementById('task-board').innerHTML=shown.length?shown.map(function(t){var state=t.state||'pending';var details=(t.summary||t.last_error||t.brief||'No task details yet.');var facts=[t.model,t.time?'Updated '+t.time:'',t.tests_passed===true?'checks pass':t.tests_passed===false?'checks failed':''].filter(Boolean).map(esc).join(' В· ');return '<article class="task-card"><div class="task-card-top"><code>'+esc(t.id)+'</code><span class="task-state state-'+esc(state)+'">'+esc(state)+'</span></div><h3>'+esc(t.title)+'</h3><p>'+esc(details)+'</p><div class="task-facts">'+facts+'</div>'+(t.files&&t.files.length?'<details><summary>Files and evidence</summary><p>'+esc(t.files.join(', '))+'</p><p>'+esc((t.evidence||[]).join(', '))+'</p></details>':'')+'</article>';}).join(''):'<div class="empty">No tasks match this filter.</div>';}
    function filterEvents(d){var rows=(d.events||'').split(String.fromCharCode(10)).filter(Boolean).map(function(line){try{return JSON.parse(line);}catch(e){return null;}}).filter(Boolean);if(eventFilter!=='all')rows=rows.filter(function(e){var k=String(e.kind||'').toLowerCase();return eventFilter==='task'?/plan|dispatch|retry|wake/.test(k):eventFilter==='accept'?/accept|merge/.test(k):/fail|reject|error|blocked/.test(k);});return rows.map(function(e){var when=e.time?new Date(e.time).toLocaleTimeString():'';var detail=e.item||e.slot||e.model||e.reason||e.summary||'';return when+'  '+String(e.kind||'event').toUpperCase()+'  '+detail;}).join(String.fromCharCode(10));}
    document.querySelectorAll('[data-task-filter]').forEach(function(button){button.addEventListener('click',function(){taskFilter=button.dataset.taskFilter;document.querySelectorAll('[data-task-filter]').forEach(function(b){b.classList.toggle('active',b===button);});if(latestData)paintTasks(latestData);});});
    document.querySelectorAll('[data-event-filter]').forEach(function(button){button.addEventListener('click',function(){eventFilter=button.dataset.eventFilter;document.querySelectorAll('[data-event-filter]').forEach(function(b){b.classList.toggle('active',b===button);});if(latestData)document.getElementById('live-events').textContent=filterEvents(latestData);});});
    document.querySelectorAll('[data-agent-filter]').forEach(function(button){button.addEventListener('click',function(){agentFilter=button.dataset.agentFilter;document.querySelectorAll('[data-agent-filter]').forEach(function(b){b.classList.toggle('active',b===button);});if(latestData)paintAgents(latestData);});});
    document.getElementById('agent-search').addEventListener('input',function(){if(latestData)paintAgents(latestData);});
    function paintAgents(d){var query=document.getElementById('agent-search').value.trim().toLowerCase();var slots=(d.slots||[]).filter(function(slot){var matchesState=agentFilter==='all'||slot.state===agentFilter;var searchable=[slot.name,slot.role,slot.item,slot.activity,slot.model].join(' ').toLowerCase();return matchesState&&(!query||searchable.indexOf(query)!==-1);});var host=document.getElementById('agent-cards');document.getElementById('agent-count').textContent=slots.length+' / '+((d.slots||[]).length)+' agents';host.replaceChildren();slots.forEach(function(slot){var card=document.createElement('button');card.type='button';card.className='agent-card state-'+(['run','wait','fail','armed'].includes(slot.state)?slot.state:'other');card.dataset.slotName=slot.name;card.setAttribute('aria-label','Inspect '+slot.name+', '+(slot.state||'unknown')+' status');var top=document.createElement('span');top.className='agent-card-top';var name=document.createElement('strong');name.textContent=slot.name;var state=document.createElement('span');state.className='task-state state-'+(slot.state||'other');state.textContent=slot.state||'unknown';top.append(name,state);var role=document.createElement('span');role.className='agent-role';role.textContent=(slot.role||'agent')+(slot.model?' В· '+slot.model:'');var item=document.createElement('span');item.className='agent-task';item.textContent=slot.item||'No assigned task';var activity=document.createElement('span');activity.className='agent-activity';activity.textContent=slot.activity||'Waiting for work';var elapsed=document.createElement('span');elapsed.className='agent-elapsed';elapsed.textContent=Math.max(0,Number(slot.elapsed)||0)+'s active';card.append(top,role,item,activity,elapsed);card.addEventListener('click',function(){select.value=slot.name;select.dispatchEvent(new Event('change'));document.querySelectorAll('.agent-card').forEach(function(other){other.classList.toggle('selected',other===card);});});host.appendChild(card);});if(!slots.length){var empty=document.createElement('div');empty.className='empty';empty.textContent='No agents match this filter.';host.appendChild(empty);}}
    function paintProviders(d){var rows=Object.entries(d.providers||{});var host=document.getElementById('free-provider-table');if(!rows.length){host.textContent='Provider budgets appear after the supervisor starts.';return;}var table=document.createElement('table');table.style.width='100%';var head=document.createElement('tr');['Provider','Active / cap','Requests today','Daily budget','State'].forEach(function(label){var th=document.createElement('th');th.textContent=label;th.style.textAlign='left';head.appendChild(th);});table.appendChild(head);rows.forEach(function(pair){var name=pair[0],p=pair[1],row=document.createElement('tr');var values=[name,String(p.active||0)+' / '+String(p.live_limit||0),String(p.requests_today||0),Number(p.used||0).toLocaleString()+' / '+Number(p.limit||0).toLocaleString()+' '+String(p.unit||''),p.enabled===false?'Disabled':p.ready?'Ready':String(p.reason||'Waiting')+(p.retry_after?' В· '+String(p.retry_after)+'s':'')];values.forEach(function(value){var cell=document.createElement('td');cell.textContent=value;cell.style.padding='8px 10px 8px 0';row.appendChild(cell);});table.appendChild(row);});host.replaceChildren(table);var router=d.router||{};document.getElementById('router-status').textContent=router.model?router.model+' В· '+String(router.decisions||0)+' decisions В· '+String(router.fallbacks||0)+' fallbacks В· '+String(router.choice||router.reason||'Waiting'):'';}
    function refresh(){fetch('/live.json',{cache:'no-store'}).then(r=>r.json()).then(function(d){latestData=d;
      document.getElementById('telemetry-model').textContent=d.workerModel||'No active model';document.getElementById('telemetry-cap').textContent=(d.slots||[]).filter(function(s){return s.role==='worker'&&s.state==='run';}).length+' / '+(d.liveLimit||0);document.getElementById('telemetry-requests').textContent=String(d.requestsToday||0);var errors=d.provider&&d.provider.errors||0;document.getElementById('telemetry-errors').textContent=String(errors);document.getElementById('telemetry-tests').textContent=d.baselineOk===true?'PASS':d.baselineOk===false?'FAIL':'PENDING';document.getElementById('telemetry-tests').className=d.baselineOk===true?'good':d.baselineOk===false?'bad':'pending';document.getElementById('telemetry-milestone').textContent=d.milestone||d.phase||'вЂ”';
      document.getElementById('provider-summary').textContent='Phase '+(d.phase||'unknown')+' / tracked cost '+Number(d.cost||0).toFixed(2)+' USD / updated '+(d.updated||'unknown');
      var current=select.value; var keys=(d.slots||[]).map(s=>s.name).join('|');
      if(select.dataset.keys!==keys){select.replaceChildren();(d.slots||[]).forEach(function(s){var o=document.createElement('option');o.value=s.name;o.textContent=s.name;select.appendChild(o);});select.dataset.keys=keys;if(current)select.value=current;}
      document.getElementById('live-traces').textContent=d.traces||'No accepted changes yet.';
      document.getElementById('live-events').textContent=filterEvents(d);
      document.getElementById('live-mission').textContent=d.task||'';
      paintTasks(d);
      paintAgents(d);
      paintProviders(d);
      return fetch('/api/log?slot='+encodeURIComponent(select.value),{cache:'no-store'});
    }).then(r=>r.json()).then(d=>{document.getElementById('live-log').textContent=d.activity||d.log||'No log yet.';}).catch(function(){});}
    select.addEventListener('change',refresh);refresh();setInterval(refresh,3000);
  }());</script>`;
}

function readableActivity(raw: string): string {
  const lines: string[] = [];
  for (const line of raw.split("\n")) {
    let event: Record<string, any>;
    try { event = JSON.parse(line); } catch { continue; }
    const part = event.part || {};
    const stamp = event.timestamp ? new Date(Number(event.timestamp)).toLocaleTimeString() : "";
    if (event.type === "tool_use") {
      const state = part.state || {};
      const input = state.input || {};
      const target = input.path || input.filePath || input.pattern || "";
      lines.push(stamp + "  " + (part.tool || "tool") + "  " + (state.status || "") + "  " + String(target));
      if (state.status === "error") lines.push(String(state.error || "Tool failed."));
    } else if (event.type === "text" || event.type === "reasoning") {
      if (part.text) lines.push(stamp + "  " + String(part.text).slice(-6000));
    } else if (event.type === "error") {
      lines.push(stamp + "  " + JSON.stringify(event.error || "Request failed."));
    }
  }
  return lines.slice(-40).join("\n");
}

const server = createServer((req, res) => {
	const path = (req.url || "/").split("?")[0];
  if (req.method === "GET" && path === "/assets/vis-network.js") {
    try {
      const asset = join(process.env.OCSWARM_ASSET_DIR || join(process.cwd(), "runtime"), "vis-network.min.js");
      res.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "max-age=86400" });
      res.end(readFileSync(asset));
    } catch { res.writeHead(404); res.end("Graph asset is unavailable."); }
    return;
  }
  if (req.method === "GET" && path === "/api/log") {
    const slotName = new URL(req.url || "/", "http://localhost").searchParams.get("slot");
    const data = load().live;
    const slot = (data.slots as SwarmSlot[]).find(row => row.name === slotName);
    const logsRoot = resolve(root, ".ocswarm", "logs") + sep;
    const logPath = slot?.log ? resolve(slot.log) : "";
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
    const log = logPath.startsWith(logsRoot) ? tail(logPath, 128000) : "";
    res.end(JSON.stringify({ log, activity: readableActivity(log) }));
    return;
  }
	if (req.method === "GET" && path === "/live.json") {
		res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
		res.end(JSON.stringify(load().live));
		return;
	}
	if (req.method === "GET" && (path === "/" || path === "/index.html")) {
		res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    res.end(renderDashboardHtml(load().page).replace("</body>", detailsHtml() + "</body>"));
		return;
	}
	res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
	res.end("Not found. The dashboard lives at /.\n");
});

server.listen(port, UI_HOST, () => {
	const url = "http://" + UI_HOST + ":" + port + "/";
	console.log(url);
	if (!openBrowser) return;
	const child = process.platform === "win32"
		? spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore" })
		: spawn("xdg-open", [url], { detached: true, stdio: "ignore" });
	child.unref();
});
