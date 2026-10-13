// Adapted for Ocswarm. See ../THIRD_PARTY_NOTICES.md.
/**
 * HTML rendering for `Ocswarm dashboard`.
 *
 * Pure string-builder: takes the DashboardData envelope produced by
 * `./data.ts` and returns a fully self-contained HTML document. No
 * imports of node:fs, no fetches, no DOM dependencies — every test
 * can call this with a fixture and inspect the output.
 *
 * Dependency on the network is limited to one CDN-hosted script tag
 * (vis-network ~150 KB minified). Picked vis-network over D3 because:
 *
 *   - One include, one constructor call — no D3 layout boilerplate.
 *   - Handles 1k+ nodes with reasonable physics out of the box.
 *   - Built-in zoom / pan / drag / hover tooltip.
 *
 * The graphify-shape snapshot is transformed into vis-network's
 * `{ nodes, edges }` here (not in `./data.ts`) so the data layer stays
 * data-shape-agnostic and the renderer owns its own UI concerns.
 *
 * Security: every string that originates from outside (project name,
 * repo_key, snapshot JSON, file paths) goes through `escHtml` before
 * landing in markup, or through `safeJsonForScript` before landing in
 * an embedded `<script type="application/json">` block. We never use
 * `<script>VAR = ...</script>` injection — the embedded JSON is parsed
 * by a separate inline script, so a malicious `</script>` substring in
 * data can't escape the JSON container even if it's not pre-escaped
 * (we escape it anyway as defense-in-depth).
 */

import type { DashboardData, DashboardKpis } from "./data.js";

const VIS_NETWORK_CDN = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js";

/**
 * Node-kind colors aligned to graphify-style palette. Each kind gets a
 * distinct hue at low saturation so the dark background is comfortable
 * to look at for more than a few seconds.
 */
const KIND_COLORS: Record<string, string> = {
  function: "#7aa2f7", // soft blue
  class: "#bb9af7",    // purple
  method: "#9ece6a",   // green
  interface: "#e0af68", // amber
  type_alias: "#7dcfff", // cyan
  enum: "#f7768e",      // pink
  const: "#9d7cd8",     // muted purple
  module: "#565f89",    // slate
};
const DEFAULT_NODE_COLOR = "#565f89";

interface VisNode {
  id: string;
  label: string;
  title: string;
  group?: string;
  color?: { background: string; border: string };
}

interface VisEdge {
  from: string;
  to: string;
  title: string;
  /** Showing edge labels gets noisy past ~50 edges; we omit by default. */
  label?: string;
}

interface RawSnapshot {
  nodes?: unknown;
  links?: unknown;
}

interface RawNode {
  id?: unknown;
  label?: unknown;
  kind?: unknown;
  source_file?: unknown;
  source_location?: unknown;
}

interface RawEdge {
  source?: unknown;
  target?: unknown;
  relation?: unknown;
  confidence?: unknown;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function asString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** Build the vis-network `{ nodes, edges }` payload from a graphify-shape
 *  snapshot. Defensive: any node missing an `id` is dropped; any edge
 *  whose endpoint id doesn't match a known node is dropped. The
 *  rendered graph stays consistent even if the snapshot was generated
 *  by an older schema. */
export function transformSnapshotToVis(snapshot: unknown): { nodes: VisNode[]; edges: VisEdge[] } {
  if (!isObject(snapshot)) return { nodes: [], edges: [] };
  const raw = snapshot as RawSnapshot;

  const visNodes: VisNode[] = [];
  const ids = new Set<string>();
  if (Array.isArray(raw.nodes)) {
    for (const n of raw.nodes) {
      if (!isObject(n)) continue;
      const node = n as RawNode;
      const id = asString(node.id);
      if (!id) continue;
      if (ids.has(id)) continue; // dedup defensively — snapshot.ts sorts but doesn't dedup
      ids.add(id);

      const label = asString(node.label) ?? id;
      const kind = asString(node.kind);
      const sourceFile = asString(node.source_file);
      const sourceLoc = asString(node.source_location);
      const titleParts: string[] = [];
      if (kind) titleParts.push(kind);
      if (sourceFile) {
        const loc = sourceLoc ? `${sourceFile}:${sourceLoc}` : sourceFile;
        titleParts.push(loc);
      }
      const color = kind && KIND_COLORS[kind] ? KIND_COLORS[kind] : DEFAULT_NODE_COLOR;
      // vis-network renders `title` as HTML, NOT text — so any markup
      // in a snapshot id / source_file / source_location would execute
      // on hover. Pre-escape every component that lands in a tooltip
      // string. Labels are passed to the canvas-rendered node face
      // and are text-only by vis design, so they don't need this.
      visNodes.push({
        id,
        label,
        title: titleParts.length > 0
          ? titleParts.map(escHtml).join(" · ")
          : escHtml(id),
        group: kind ?? undefined,
        color: { background: color, border: color },
      });
    }
  }

  const visEdges: VisEdge[] = [];
  if (Array.isArray(raw.links)) {
    for (const l of raw.links) {
      if (!isObject(l)) continue;
      const edge = l as RawEdge;
      const from = asString(edge.source);
      const to = asString(edge.target);
      if (!from || !to) continue;
      // Don't filter on `ids.has(...)` — Phase 1 emits edges with
      // unresolved targets (cross-file calls), and surfacing those as
      // dangling endpoints is useful information for the viewer.
      // vis-network auto-creates ghost nodes for unknown endpoints.
      const relation = asString(edge.relation);
      const confidence = asString(edge.confidence);
      const titleParts: string[] = [];
      if (relation) titleParts.push(relation);
      if (confidence) titleParts.push(`[${confidence}]`);
      // Same HTML-in-tooltip concern as nodes; escape every component
      // that ends up in vis-network's `title`. The fallback string
      // includes node ids, which are also potentially attacker-controlled.
      visEdges.push({
        from,
        to,
        title: titleParts.length > 0
          ? titleParts.map(escHtml).join(" ")
          : `${escHtml(from)} → ${escHtml(to)}`,
      });
    }
  }

  return { nodes: visNodes, edges: visEdges };
}

/** HTML-attribute / text-content escape. Covers the five chars browsers
 *  may interpret inside markup. Skips `'` deliberately — every attribute
 *  in the rendered output uses double-quote delimiters. */
export function escHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Serialize a value for embedding inside
 *  `<script type="application/json">...</script>`. The standard JSON
 *  output is HTML-safe EXCEPT for three substrings the HTML parser
 *  can latch onto:
 *
 *    - `</` could close the script tag → escape to `<\/` (a valid
 *      JSON string escape that parses back to "</")
 *    - `<!--` and `-->` could be reinterpreted as an HTML comment
 *      inside the script element by legacy / lenient parsers → use
 *      `!` and `>` (unicode escapes; valid JSON; parse back
 *      to literal `<!--` and `-->` so callers see the original
 *      string)
 *
 *  Earlier version used `<\!--` and `--\>` literals; those are NOT
 *  valid JSON escapes (`\!` and `\>` aren't defined) so any snapshot
 *  string containing `<!--` or `-->` made JSON.parse fail in the
 *  browser. Codex review on this commit caught it. */
export function safeJsonForScript(value: unknown): string {
  return JSON.stringify(value)
    .replace(/<\//g, "<\\/")
    .replace(/<!--/g, "<\\u0021--")
    .replace(/-->/g, "--\\u003e");
}

/** 1234 → "1.2k", 12345 → "12.3k", 1234567 → "1.2M". Caller decides
 *  whether to prepend "~". 0 and negative produce "0" so the UI never
 *  shows a misleading "-1k saved". */
export function formatTokensCompact(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return `${Math.round(n)}`;
  if (n < 100_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** 42000 → "42,000". en-US grouping; matches primary-banner's body line. */
export function formatInt(n: number): string {
  if (!Number.isFinite(n)) return "0";
  return Math.round(n).toLocaleString("en-US");
}

function renderKpiCards(kpis: DashboardKpis, swarm?: DashboardData["swarm"]): string {
  if (swarm) {
    const cards = [
      { label: "Running", value: formatInt(swarm.running), sub: swarm.phase || "phase", id: "kpi-running" },
      { label: "Waiting", value: formatInt(swarm.waiting), sub: "no task", id: "kpi-waiting" },
      { label: "Done", value: formatInt(swarm.done), sub: "finished tasks", id: "kpi-done" },
      { label: "Total", value: formatInt(swarm.total), sub: "done + live + queued", id: "kpi-total" },
    ];
    return cards.map(c => `
        <div class="kpi">
          <div class="kpi-label">${escHtml(c.label)}</div>
          <div class="kpi-value" id="${c.id}">${escHtml(c.value)}</div>
          <div class="kpi-sub">${escHtml(c.sub)}</div>
        </div>`).join("");
  }
  const tokensValue = kpis.tokensSaved == null
    ? "—"
    : `~${formatTokensCompact(kpis.tokensSaved)}`;
  const tokensSub = (() => {
    if (kpis.tokensSource === "org") {
      return kpis.userTokensSaved != null
        ? `Org-wide · you ~${formatTokensCompact(kpis.userTokensSaved)}`
        : "Org-wide";
    }
    if (kpis.tokensSource === "local") return "Local (this machine)";
    return "Run a session to start tracking";
  })();

  const memoryValue = kpis.memorySearches > 0
    ? formatInt(kpis.memorySearches)
    : kpis.tokensSource === "none" ? "—" : "0";

  const sessionsValue = kpis.sessionsCount == null
    ? "—"
    : formatInt(kpis.sessionsCount);

  const cards = [
    {
      label: "Tokens saved",
      value: tokensValue,
      sub: tokensSub,
    },
    {
      label: "Skills created",
      value: formatInt(kpis.skillsCreated),
      sub: "~/.claude/skills/",
    },
    {
      label: "Memory recalls",
      value: memoryValue,
      sub: kpis.tokensSource === "org" ? "Org-wide" : kpis.tokensSource === "local" ? "Local" : "",
    },
    {
      label: "Sessions",
      value: sessionsValue,
      sub: kpis.tokensSource === "org" ? "Org-wide" : kpis.tokensSource === "local" ? "Local" : "",
    },
  ];

  return cards.map(c => `
        <div class="kpi">
          <div class="kpi-label">${escHtml(c.label)}</div>
          <div class="kpi-value">${escHtml(c.value)}</div>
          <div class="kpi-sub">${escHtml(c.sub)}</div>
        </div>`).join("");
}

function renderSwarmTable(data: DashboardData): string {
  const swarm = data.swarm;
  if (!swarm) return "";
  const rows = swarm.slots.map(slot => {
    const state = ["run", "wait", "fail", "armed"].includes(slot.state) ? slot.state : "other";
    return `
        <tr>
          <td>${escHtml(slot.name)}</td>
          <td>${escHtml(slot.role)}</td>
          <td class="st-${state}">${escHtml(slot.state)}</td>
          <td>${escHtml(slot.model || "")}</td>
          <td>${Math.max(0, slot.elapsed || 0)}s</td>
          <td>${escHtml(slot.item)}</td>
          <td>${escHtml(slot.activity)}</td>
        </tr>`;
  }).join("");
  return `
      <div class="graph-card swarm-table">
        <h2>Slots</h2>
        <div class="graph-meta" id="swarm-message">${escHtml(swarm.message)}</div>
        <table class="slots">
          <thead><tr><th>Name</th><th>Role</th><th>State</th><th>Model</th><th>Elapsed</th><th>Task</th><th>Activity</th></tr></thead>
          <tbody id="slots">${rows}</tbody>
        </table>
      </div>
      <script>
        (function () {
          function paint(data) {
            var running = document.getElementById("kpi-running");
            var waiting = document.getElementById("kpi-waiting");
            var done = document.getElementById("kpi-done");
            var total = document.getElementById("kpi-total");
            var message = document.getElementById("swarm-message");
            var stamp = document.getElementById("swarm-updated");
            if (running) running.textContent = String(data.running);
            if (waiting) waiting.textContent = String(data.waiting);
            if (done) done.textContent = String(data.done);
            if (total) total.textContent = String(data.total);
            if (message) message.textContent = data.message || "";
            if (stamp) stamp.textContent = data.updated || "";
            var body = document.getElementById("slots");
            if (!body) return;
            body.replaceChildren();
            (data.slots || []).forEach(function (slot) {
              var tr = document.createElement("tr");
              ["name", "role", "state", "model", "elapsed", "item", "activity"].forEach(function (key) {
                var td = document.createElement("td");
                td.textContent = key === "elapsed" ? String(slot[key] || 0) + "s" : slot[key] || "";
                if (key === "state") {
                  var state = slot.state || "";
                  td.className = (state === "run" || state === "wait" || state === "fail" || state === "armed") ? "st-" + state : "st-other";
                }
                tr.appendChild(td);
              });
              body.appendChild(tr);
            });
          }
          setInterval(function () {
            fetch("/live.json", { cache: "no-store" }).then(function (res) { return res.json(); }).then(paint).catch(function () {});
          }, 2000);
        }());
      </script>`;
}

function renderGraphSection(data: DashboardData): string {
  const heading = data.swarm ? "Workers" : "Codebase graph";
  if (data.graph == null) {
    const empty = data.swarm
      ? "No workers on the board yet."
      : `No graph snapshot yet for this repo.<br>
          Start a swarm to generate its worker graph.`;
    return `
      <div class="graph-card">
        <h2>${heading}</h2>
        <div class="empty">
          ${empty}
        </div>
      </div>`;
  }

  const visPayload = transformSnapshotToVis(data.graph.snapshot);
  const commitLabel = data.graph.commitSha
    ? `commit ${data.graph.commitSha.slice(0, 12)}`
    : "no commit (loose dir)";
  const meta = data.swarm
    ? `${formatInt(data.graph.nodeCount)} workers · ${escHtml(data.swarm.phase)} · ${escHtml(data.swarm.task)}`
    : `${formatInt(data.graph.nodeCount)} nodes · ${formatInt(data.graph.edgeCount)} edges · ${commitLabel}`;

  return `
      <div class="graph-card">
        <h2>${heading}</h2>
        <div class="graph-meta">${data.swarm ? meta : escHtml(meta)}</div>
        <div id="graph"${data.swarm ? ` class="graph-swarm"` : ""}></div>
      </div>
      <script type="application/json" id="hm-graph-data">${safeJsonForScript(visPayload)}</script>
      <script src="${data.swarm ? "/assets/vis-network.js" : VIS_NETWORK_CDN}"></script>
      <script>
        (function () {
          var holder = document.getElementById('hm-graph-data');
          var container = document.getElementById('graph');
          if (!holder || !container || typeof vis === 'undefined') return;
          var payload;
          try { payload = JSON.parse(holder.textContent); }
          catch (e) { container.textContent = 'graph payload parse failed'; return; }
          if (!payload || !Array.isArray(payload.nodes) || payload.nodes.length === 0) {
            container.textContent = 'snapshot has no nodes';
            return;
          }
          var graphNodes = new vis.DataSet(payload.nodes);
          var graphEdges = new vis.DataSet(payload.edges);
          var network = new vis.Network(container, {nodes: graphNodes, edges: graphEdges}, {
            nodes: {
              shape: 'dot',
              size: container.classList.contains('graph-swarm') ? 13 : 9,
              font: { color: '#d8f6d7', size: 11, face: 'ui-monospace, SFMono-Regular, Consolas, monospace' },
              borderWidth: 1,
            },
            edges: {
              color: { color: 'rgba(120, 130, 150, 0.45)', highlight: '#f5b80a', hover: '#e8eaed' },
              arrows: { to: { enabled: true, scaleFactor: 0.45 } },
              smooth: { enabled: true, type: 'continuous', roundness: 0.2 },
              width: 1,
            },
            layout: container.classList.contains('graph-swarm')
              ? { hierarchical: { direction: 'UD', sortMethod: 'directed', nodeSpacing: 115, levelSeparation: 90, treeSpacing: 150 } }
              : undefined,
            physics: container.classList.contains('graph-swarm')
              ? { stabilization: { iterations: 80 }, hierarchicalRepulsion: { nodeDistance: 115, springLength: 100 } }
              : { stabilization: { iterations: 120 }, barnesHut: { gravitationalConstant: -2200, springLength: 80, springConstant: 0.04 } },
            interaction: { hover: true, dragNodes: true, tooltipDelay: 120 },
          });
          if (container.classList.contains('graph-swarm')) {
            function escapeTitle(value) { return String(value || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
            function paintWorkers(data) {
              graphNodes.update((data.slots || []).map(function(slot){
                var color=slot.state==='run'?'#83ff78':slot.state==='fail'?'#ff5964':slot.role==='orchestrator'?'#38e890':slot.state==='armed'?'#b0ff54':'#315b3d';
                return {id:slot.name,label:slot.name,title:escapeTitle(slot.model)+' · '+escapeTitle(slot.item)+' · '+escapeTitle(slot.activity),color:{background:color,border:color}};
              }));
            }
            paintWorkers({slots: payload.nodes.map(function(node){return {name:node.id,state:node.source_location,role:node.id==='orchestrator'?'orchestrator':'worker',item:node.source_file};})});
            setInterval(function () {
              fetch('/live.json', {cache:'no-store'}).then(function(r){return r.json();}).then(paintWorkers).catch(function(){});
            },2000);
            network.on('selectNode',function(event){
              var select=document.getElementById('worker-log');
              if(select && event.nodes.length){select.value=event.nodes[0];select.dispatchEvent(new Event('change'));}
            });
          }
        }());
      </script>`;
}

const STYLES = `
        :root {
          color-scheme: dark;
          --bg: #0b0d10;
          --fg: #e8eaed;
          --muted: #8b9099;
          --accent: #f5b80a;
          --card: #15181d;
          --border: #22272e;
        }
        body.swarm-dashboard {
          --bg: #030805;
          --fg: #dcf8df;
          --muted: #70a77a;
          --accent: #65ff74;
          --card: #050d08;
          --border: #174523;
          display: grid;
          grid-template-columns: minmax(0, 1.65fr) minmax(290px, 0.85fr);
          grid-template-rows: auto auto minmax(410px, 1fr) auto auto;
          gap: 14px;
          min-height: 100vh;
          padding: 16px 20px 24px;
          background: radial-gradient(ellipse at 38% -24%, #12371c 0%, #07130a 35%, #030805 72%);
          font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
        }
        body.swarm-dashboard::before { content: "OCSWARM  /  LIVE OPERATIONS"; position: fixed; right: 20px; bottom: 10px; color: #2e6739; font-size: 9px; letter-spacing: .16em; pointer-events: none; }
        .swarm-dashboard .header { grid-column: 1 / -1; margin: 0; padding: 2px 2px 12px; align-items: center; border-bottom: 1px solid var(--border); }
        .swarm-dashboard .brand { color: var(--accent); font-size: 17px; letter-spacing: .1em; text-transform: uppercase; }
        .swarm-dashboard .brand .bee { filter: grayscale(1); opacity: .8; }
        .swarm-dashboard .brand .repo { color: #d4ebd5; font: inherit; font-size: 12px; letter-spacing: 0; text-transform: none; }
        .swarm-dashboard .header .ts { padding: 6px 10px; border: 1px solid #215c30; border-radius: 2px; color: var(--accent); font-size: 11px; }
        .swarm-dashboard .kpi-grid { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 0; }
        .swarm-dashboard .kpi { position: relative; overflow: hidden; min-height: 84px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 2px; background: linear-gradient(130deg, #09150c, #050a07 70%); box-shadow: inset 0 0 22px rgb(49 255 84 / 3%); }
        .swarm-dashboard .kpi::after { content: ""; position: absolute; right: 0; bottom: 0; width: 34%; height: 2px; background: var(--accent); opacity: .72; }
        .swarm-dashboard .kpi-label { color: #70a77a; font-size: 9px; letter-spacing: .14em; }
        .swarm-dashboard .kpi-value { margin-top: 2px; color: #8aff83; font-size: 25px; text-shadow: 0 0 16px rgb(101 255 116 / 18%); }
        .swarm-dashboard .kpi-sub { margin-top: 0; color: #588765; font-size: 10px; }
        .swarm-dashboard .graph-card { min-width: 0; padding: 13px; border: 1px solid var(--border); border-radius: 2px; background: linear-gradient(145deg, rgb(7 17 10 / 96%), rgb(3 8 5 / 98%)); box-shadow: 0 10px 28px rgb(0 0 0 / 22%); }
        .swarm-dashboard .graph-card h2 { margin-bottom: 6px; color: #9bffa1; font-size: 11px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; }
        .swarm-dashboard .graph-meta { color: #5e9d68; font-size: 10px; }
        .swarm-dashboard #graph { height: 100%; min-height: 360px; max-height: 560px; border: 1px solid #153d20; border-radius: 1px; background: radial-gradient(ellipse, #07170b 0%, #030805 72%); }
        .swarm-dashboard #graph.graph-swarm { height: 100%; min-height: 380px; max-height: 560px; }
        .swarm-dashboard .swarm-table { grid-column: 1 / -1; grid-row: 4; margin: 0; overflow: hidden; }
        .swarm-dashboard table.slots { table-layout: fixed; }
        .swarm-dashboard table.slots th, .swarm-dashboard table.slots td { padding: 7px 9px; border-bottom: 1px solid #102d18; font-size: 10px; overflow-wrap: anywhere; }
        .swarm-dashboard table.slots th { color: #578f61; font-size: 8px; letter-spacing: .14em; }
        .swarm-dashboard table.slots tbody tr { transition: background .15s ease; }
        .swarm-dashboard table.slots tbody tr:hover { background: #0b1d0f; }
        .swarm-dashboard table.slots td:first-child { color: #a4ffa6; font-weight: 700; }
        .swarm-dashboard .st-run { color: #87ff78; text-shadow: 0 0 8px rgb(135 255 120 / 20%); }
        .swarm-dashboard .st-wait { color: #88a66e; }
        .swarm-dashboard .st-fail { color: #ff6470; }
        .swarm-dashboard .st-armed { color: #c3ff55; }
        .swarm-dashboard .footer { grid-column: 1 / -1; margin-top: -7px; text-align: left; color: #3e7148; font: 9px ui-monospace, monospace; }
        .swarm-dashboard .live-panel { grid-column: 2; grid-row: 3; margin: 0 !important; min-height: 0; display: flex; flex-direction: column; }
        .swarm-dashboard .live-panel .live-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .swarm-dashboard .live-panel .signal { color: #78ff78; font-size: 9px; letter-spacing: .12em; }
        .swarm-dashboard .live-panel label { color: #8bd493; font-size: 10px; }
        .swarm-dashboard .live-panel select { max-width: 100%; margin: 6px 0 10px; padding: 7px 10px; border: 1px solid #245a2f; border-radius: 2px; background: #07110a; color: #bdf6c0; font: inherit; }
        .swarm-dashboard .live-panel pre { flex: 1; min-height: 130px; margin: 0; padding: 10px; overflow: auto; border: 1px solid #153b20; background: #020604; color: #84d88b; font: 10px/1.5 ui-monospace, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
        .swarm-dashboard .live-panel details { margin-top: 8px; border-top: 1px solid #153b20; }
        .swarm-dashboard .live-panel summary { padding: 7px 0; color: #7dbd85; cursor: pointer; font-size: 10px; }
        .swarm-dashboard .live-panel #live-mission { max-height: 130px; overflow: auto; color: #83b88a; font-size: 10px; }
        .swarm-dashboard .live-panel #live-traces, .swarm-dashboard .live-panel #live-events { max-height: 180px; }
        .swarm-dashboard .telemetry-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; margin: 8px 0; }
        .swarm-dashboard .telemetry { min-width: 0; padding: 7px 8px; border: 1px solid #12351a; background: #040b06; }
        .swarm-dashboard .telemetry span { display: block; overflow: hidden; color: #578f61; font-size: 8px; letter-spacing: .1em; text-overflow: ellipsis; white-space: nowrap; }
        .swarm-dashboard .telemetry b { display: block; overflow: hidden; margin-top: 3px; color: #a4ffa6; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
        .swarm-dashboard .telemetry b.good { color: #83ff78; }
        .swarm-dashboard .telemetry b.bad { color: #ff6470; }
        .swarm-dashboard .telemetry b.pending { color: #e0af68; }
        .swarm-dashboard .task-filters { display: flex; flex-wrap: wrap; gap: 5px; margin: 4px 0 8px; }
        .swarm-dashboard .task-filters button { padding: 5px 8px; border: 1px solid #174523; border-radius: 2px; background: #06100a; color: #73a87b; font: 9px ui-monospace, monospace; cursor: pointer; }
        .swarm-dashboard .task-filters button:hover, .swarm-dashboard .task-filters button.active { border-color: #65ff74; color: #a4ffa6; background: #0b1d0f; }
        .swarm-dashboard .task-board { display: grid; gap: 7px; max-height: 360px; overflow: auto; }
        .swarm-dashboard .task-card { padding: 9px; border: 1px solid #14391d; background: linear-gradient(125deg, #07120a, #030805); }
        .swarm-dashboard .task-card-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
        .swarm-dashboard .task-card code { color: #69cf76; font-size: 9px; }
        .swarm-dashboard .task-card h3 { margin: 6px 0 4px; color: #c3f8c6; font-size: 10px; line-height: 1.4; }
        .swarm-dashboard .task-card p { margin: 4px 0; color: #75a87b; font-size: 9px; white-space: pre-wrap; overflow-wrap: anywhere; }
        .swarm-dashboard .task-card .task-facts { margin-top: 6px; color: #4d8057; font-size: 8px; }
        .swarm-dashboard .task-state { padding: 2px 5px; border: 1px solid #315b3d; color: #88a66e; font-size: 8px; text-transform: uppercase; }
        .swarm-dashboard .task-state.state-running { border-color: #338344; color: #83ff78; }
        .swarm-dashboard .task-state.state-done { border-color: #338344; color: #83ff78; }
        .swarm-dashboard .task-state.state-failed { border-color: #87343d; color: #ff6470; }
        .swarm-dashboard .task-state.state-pending { color: #e0af68; }
        .swarm-dashboard .task-card details { border-top: 1px solid #14391d; }
        .swarm-dashboard .task-card summary { padding: 5px 0; color: #7dbd85; cursor: pointer; font-size: 8px; }
        .swarm-dashboard #task-count { color: #578f61; font-size: 9px; font-weight: 400; letter-spacing: 0; }
        .swarm-dashboard #event-filters { margin-top: 5px; }
        .swarm-dashboard .agent-overview { min-height: 0; margin: 10px 0 4px; }
        .swarm-dashboard .agent-overview-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .swarm-dashboard .agent-overview-heading h3 { margin: 0; color: #a3f3a8; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; }
        .swarm-dashboard .agent-overview-heading p { margin: 3px 0 0; color: #578f61; font-size: 8px; }
        .swarm-dashboard #agent-count { color: #578f61; font-size: 8px; }
        .swarm-dashboard .agent-controls { display: grid; grid-template-columns: minmax(0, 1fr); gap: 3px; margin: 7px 0; }
        .swarm-dashboard .agent-controls .task-filters { margin: 0; }
        .swarm-dashboard .agent-search-label { color: #60986a; font-size: 8px; }
        .swarm-dashboard #agent-search { width: 100%; min-width: 0; padding: 7px 8px; border: 1px solid #174523; border-radius: 2px; outline: none; background: #040b06; color: #bdf6c0; font: 9px ui-monospace, monospace; }
        .swarm-dashboard #agent-search:focus { border-color: #65ff74; box-shadow: 0 0 0 1px rgb(101 255 116 / 20%); }
        .swarm-dashboard .agent-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; max-height: 310px; padding-right: 3px; overflow: auto; }
        .swarm-dashboard .agent-card { min-width: 0; padding: 8px; border: 1px solid #14391d; border-radius: 2px; background: linear-gradient(130deg, #07120a, #030805); color: #89cb91; text-align: left; cursor: pointer; }
        .swarm-dashboard .agent-card:hover, .swarm-dashboard .agent-card.selected { border-color: #51b660; background: #0a190d; }
        .swarm-dashboard .agent-card.state-run { border-left: 2px solid #73ec70; }
        .swarm-dashboard .agent-card.state-fail { border-left: 2px solid #ff6470; }
        .swarm-dashboard .agent-card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 4px; }
        .swarm-dashboard .agent-card-top strong { min-width: 0; overflow: hidden; color: #b6ffb8; font-size: 9px; text-overflow: ellipsis; }
        .swarm-dashboard .agent-card .task-state { flex: none; padding: 2px 4px; font-size: 7px; }
        .swarm-dashboard .agent-role, .swarm-dashboard .agent-task, .swarm-dashboard .agent-activity, .swarm-dashboard .agent-elapsed { display: block; overflow: hidden; margin-top: 4px; color: #5f9869; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
        .swarm-dashboard .agent-task { color: #8dc993; }
        .swarm-dashboard .agent-activity { color: #b0d9b4; }
        .swarm-dashboard .agent-elapsed { color: #44754e; font-size: 7px; }
        @media (max-width: 850px) {
          body.swarm-dashboard { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto; padding: 12px; }
          .swarm-dashboard .live-panel, .swarm-dashboard .swarm-table { grid-column: 1; grid-row: auto; }
          .swarm-dashboard .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .swarm-dashboard #graph, .swarm-dashboard #graph.graph-swarm { min-height: 330px; max-height: 460px; }
          .swarm-dashboard .swarm-table { overflow-x: auto; }
          .swarm-dashboard table.slots { min-width: 760px; }
        }
        @media (max-width: 480px) {
          body.swarm-dashboard .brand .repo { display: block; margin: 4px 0 0 28px; }
          .swarm-dashboard .kpi-grid { gap: 7px; }
          .swarm-dashboard .kpi { padding: 10px; }
          .swarm-dashboard .agent-cards { grid-template-columns: minmax(0, 1fr); max-height: 420px; }
          .swarm-dashboard .kpi-value { font-size: 21px; }
        }
        * { box-sizing: border-box; }
        html, body { margin: 0; padding: 0; }
        body {
          font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: var(--bg);
          color: var(--fg);
          padding: 24px;
        }
        .header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 24px; gap: 16px; flex-wrap: wrap; }
        .brand { font-weight: 600; font-size: 18px; }
        .brand .bee { color: var(--accent); margin-right: 4px; }
        .brand .repo { color: var(--muted); font-weight: 400; margin-left: 8px; }
        .header .ts { color: var(--muted); font-size: 12px; }
        .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 32px; }
        .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; }
        .kpi-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
        .kpi-value { font-size: 28px; font-weight: 600; margin-top: 6px; line-height: 1.1; }
        .kpi-sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
        .graph-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
        .graph-card h2 { margin: 0 0 8px; font-size: 15px; font-weight: 500; }
        .graph-meta { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
        #graph { height: 70vh; border: 1px solid var(--border); border-radius: 4px; background: #0e1116; }
        #graph.graph-swarm { height: 42vh; }
        .swarm-table { margin-top: 16px; }
        table.slots { width: 100%; border-collapse: collapse; }
        table.slots th, table.slots td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
        table.slots th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 500; }
        .st-run { color: #9ece6a; }
        .st-wait { color: #e0af68; }
        .st-fail { color: #f7768e; }
        .st-armed { color: #7aa2f7; }
        .empty { padding: 48px 16px; text-align: center; color: var(--muted); }
        .empty code { background: #1c2128; padding: 2px 6px; border-radius: 3px; color: var(--fg); font-family: ui-monospace, "SFMono-Regular", monospace; }
        .footer { color: var(--muted); font-size: 11px; margin-top: 24px; text-align: right; }
`;

/**
 * Build the complete dashboard HTML document. Pure: deterministic on
 * the input (no Date.now() reads — `data.generatedAt` is the only
 * timestamp surfaced, and the caller pre-stamps it).
 */
export function renderDashboardHtml(data: DashboardData): string {
  const title = `Ocswarm Dashboard · ${data.repoProject}`;
  return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escHtml(title)}</title>
    <style>${STYLES}</style>
  </head>
  <body${data.swarm ? ` class="swarm-dashboard"` : ""}>
    <div class="header">
      <div class="brand">
        <span class="bee">\u{1F41D}</span>Ocswarm dashboard
        <span class="repo">/ ${escHtml(data.repoProject)}</span>
      </div>
      <div class="ts"${data.swarm ? ` id="swarm-updated"` : ""}>${escHtml(data.generatedAt)}</div>
    </div>
    <div class="kpi-grid">${renderKpiCards(data.kpis, data.swarm)}
    </div>
    ${renderGraphSection(data)}
    ${renderSwarmTable(data)}
    <div class="footer">repo_key ${escHtml(data.repoKey)}</div>
  </body>
</html>
`;
}
