/* Godot PR review throughput — page logic.
   Loads data/index.json once, per-label files on demand (cached).
   State (label, range, rolling avg) mirrors into location.hash. */

"use strict";

const state = { label: null, range: 24, avg: true };
const labelCache = new Map();
let indexData = null;
const charts = {};

const COLORS = {
  opened: "#8c959f",
  closed: "#0969da",
};

// Sediment layers, longest horizon first (drawn bottom-most, painted over by
// the shorter ones). One-hue ordinal ramp, lightest (top surface) to darkest
// (bottom band = fastest resolutions).
const RES_HORIZONS = [
  { key: "d960", label: "within 32 months", color: "#86b6ef" },
  { key: "d480", label: "within 16 months", color: "#6da7ec" },
  { key: "d240", label: "within 8 months", color: "#5598e7" },
  { key: "d120", label: "within 4 months", color: "#3987e5" },
  { key: "d60", label: "within 2 months", color: "#2a78d6" },
  { key: "d30", label: "within 1 month", color: "#256abf" },
  { key: "d14", label: "within 2 weeks", color: "#1c5cab" },
  { key: "d7", label: "within 1 week", color: "#184f95" },
  { key: "d3", label: "within 3 days", color: "#104281" },
  { key: "d1", label: "within 1 day", color: "#0d366b" },
];

// ---- state <-> hash ---------------------------------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (params.get("l")) state.label = params.get("l");
  if (params.get("r")) state.range = params.get("r") === "all" ? "all" : +params.get("r");
  if (params.get("avg")) state.avg = params.get("avg") === "1";
}

function writeHash() {
  const params = new URLSearchParams();
  params.set("l", state.label);
  params.set("r", state.range);
  params.set("avg", state.avg ? "1" : "0");
  history.replaceState(null, "", "#" + params.toString());
}

// ---- data -------------------------------------------------------------------

async function fetchJson(url) {
  // no-cache = revalidate against the server, so a monthly data update is
  // picked up even by browsers that visited the page before it.
  const resp = await fetch(url, { cache: "no-cache" });
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  return resp.json();
}

async function loadLabel(entry) {
  if (!labelCache.has(entry.file)) {
    labelCache.set(entry.file, await fetchJson("data/" + entry.file));
  }
  return labelCache.get(entry.file);
}

function labelEntry(name) {
  return indexData.labels.find((l) => l.name === name);
}

// ---- view computation -------------------------------------------------------

function rolling3(arr) {
  return arr.map((v, i) => {
    if (v === null) return null;  // never smooth a gap into existence
    const window = arr.slice(Math.max(0, i - 2), i + 1).filter((x) => x !== null);
    return Math.round((window.reduce((a, b) => a + b, 0) / window.length) * 10) / 10;
  });
}

function computeView(data) {
  const months = indexData.months;
  const rangeLen = state.range === "all" ? months.length : state.range;
  const firstSeenIdx = Math.max(0, months.indexOf(data.firstSeen));
  const start = Math.max(months.length - rangeLen, firstSeenIdx);
  const clamped = start > months.length - rangeLen;

  const slice = (arr) => arr.slice(start);
  // Smooth BEFORE slicing so the visible range's first points still average
  // over the months just outside the window.
  const series = (arr) => slice(state.avg ? rolling3(arr) : arr);
  return {
    months: slice(months),
    clamped,
    firstSeen: data.firstSeen,
    opened: series(data.opened),
    closed: series(data.merged.map((v, i) => v + data.closedUnmerged[i])),
    res: Object.fromEntries(RES_HORIZONS.map((h) =>
      [h.key, resolutionSeries(data, h.key, slice)])),
  };
}

// Rate per month from raw counts; months past `through` are too young to
// judge. With averaging on, pool the 3-month cohort (weighted) instead of
// averaging percentages — this also fills empty months whose window isn't.
// Returns {data, counts}; counts feed the tooltip's absolute numbers.
function resolutionSeries(data, horizon, slice) {
  const { closed, through } = data.resolution[horizon];
  const opened = data.opened;
  const counts = opened.map((v, i) => {
    if (i > through) return null;
    let o = v, c = closed[i];
    if (state.avg) {
      o = 0; c = 0;
      for (let j = Math.max(0, i - 2); j <= i; j++) { o += opened[j]; c += closed[j]; }
    }
    return { o, c };
  });
  return {
    data: slice(counts.map((x) => (x && x.o ? Math.round(1000 * x.c / x.o) / 10 : null))),
    counts: slice(counts),
  };
}

// ---- charts -----------------------------------------------------------------

const BASE_OPTS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  interaction: { mode: "index", intersect: false },
  scales: {
    x: { ticks: { autoSkip: true, maxTicksLimit: 16, maxRotation: 0 } },
    y: { beginAtZero: true },
  },
};

function line(label, color, extra = {}) {
  return Object.assign({
    label, borderColor: color, backgroundColor: color,
    borderWidth: 2, pointRadius: 0, pointHitRadius: 6, spanGaps: false,
  }, extra);
}

function createCharts() {
  charts.flow = new Chart(document.getElementById("chart-flow"), {
    type: "line",
    data: { labels: [], datasets: [
      line("opened", COLORS.opened),
      line("closed", COLORS.closed, {
        // Band between the lines, colored by sign: red while opened > closed
        // (backlog growing), green while closed > opened (digesting).
        fill: { target: "-1", above: "rgba(26,127,55,.18)", below: "rgba(207,34,46,.14)" },
      }),
    ]},
    options: Object.assign({}, BASE_OPTS, {
      plugins: { tooltip: { callbacks: { footer: netFooter } } },
    }),
  });

  charts.resolution = new Chart(document.getElementById("chart-resolution"), {
    type: "line",
    data: { labels: [], datasets: RES_HORIZONS.map((h) => ({
      label: h.label,
      backgroundColor: h.color,
      fill: "origin",
      borderColor: "#fff",
      borderWidth: 1,
      pointRadius: 0,
      pointHitRadius: 6,
      spanGaps: false,
      data: [],
    })) },
    options: Object.assign({}, BASE_OPTS, {
      scales: {
        x: BASE_OPTS.scales.x,
        y: { min: 0, max: 100, ticks: { callback: (v) => v + "%" } },
      },
      plugins: {
        legend: { labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: resolutionLabel } },
      },
    }),
  });
}

let currentView = null;

function netFooter(items) {
  if (!currentView || !items.length) return "";
  const i = items[0].dataIndex;
  const net = currentView.opened[i] - currentView.closed[i];
  return `net ${net > 0 ? "+" : ""}${Math.round(net * 10) / 10}`;
}

function resolutionLabel(item) {
  const key = RES_HORIZONS[item.datasetIndex].key;
  const x = currentView.res[key].counts[item.dataIndex];
  const scope = state.avg ? " over 3 mo" : "";
  return `${item.dataset.label}: ${item.formattedValue}% — ` +
         `${x.c} of ${x.o} PRs${scope}`;
}

function updateCharts(view) {
  currentView = view;
  charts.flow.data.labels = view.months;
  const [opened, closed] = charts.flow.data.datasets;
  opened.data = view.opened;
  closed.data = view.closed;
  charts.flow.update();

  charts.resolution.data.labels = view.months;
  charts.resolution.data.datasets.forEach((ds, i) => {
    ds.data = view.res[RES_HORIZONS[i].key].data;
  });
  charts.resolution.update();
}

// ---- tables -----------------------------------------------------------------

function renderTable(tableId, rows, columns) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector("tbody");
  tbody.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const [i, col] of columns.entries()) {
      const td = document.createElement("td");
      const value = col.fmt ? col.fmt(row) : row[col.key];
      if (i === 0 && !String(value).startsWith("(")) {
        const a = document.createElement("a");
        a.href = `https://github.com/${value}`;
        a.textContent = value;
        td.appendChild(a);
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function makeSortable(tableId, rowsRef, columns) {
  const headers = document.querySelectorAll(`#${tableId} th`);
  headers.forEach((th, i) => {
    th.addEventListener("click", () => {
      const key = columns[i].key;
      const rows = i === 0
        ? alphabetical(rowsRef.rows)
        : [...rowsRef.rows].sort((a, b) => b[key] - a[key]);
      renderTable(tableId, rows, columns);
      markSorted(tableId, i, i === 0);
    });
  });
}

const reviewerCols = [
  { key: "login" },
  { key: "prs", fmt: (r) => `${r.prs} (${r.pctOfReviewed}%)` },
  { key: "decided", fmt: (r) => `${r.decided} (${r.pctOfDecided}%)` },
];
const authorCols = [
  { key: "login" },
  { key: "open", fmt: (r) => `${r.open} (${r.pctOpen}%)` },
];
const tableRows = { reviewers: { rows: [] }, authors: { rows: [] } };

// Default order is alphabetical: a roster with counts, not a pecking order.
// Magnitude sorts stay one header-click away. The "(N others)" row stays last.
function alphabetical(rows) {
  const people = rows.filter((r) => !r.login.startsWith("("));
  const rest = rows.filter((r) => r.login.startsWith("("));
  return people.sort((a, b) => a.login.localeCompare(b.login)).concat(rest);
}

function markSorted(tableId, index, asc) {
  document.querySelectorAll(`#${tableId} th`).forEach((th, i) => {
    th.classList.toggle("sorted", i === index);
    th.classList.toggle("asc", i === index && asc);
  });
}

function updateTables(data) {
  tableRows.reviewers.rows = data.reviewers12m;
  tableRows.authors.rows = data.authors12m;
  renderTable("reviewers-table", alphabetical(data.reviewers12m), reviewerCols);
  renderTable("authors-table", alphabetical(data.authors12m), authorCols);
  markSorted("reviewers-table", 0, true);
  markSorted("authors-table", 0, true);
}

// ---- overview ---------------------------------------------------------------

const overviewCols = [
  { key: "name", render: (r) => labelPill(r.name) },
  { key: "openNow" },
  { key: "net", render: (r) => {
    const wrap = document.createElement("span");
    const net = document.createElement("strong");
    net.textContent = (r.net > 0 ? "+" : "") + r.net;
    net.style.color = r.net > 0 ? "#cf222e" : r.net < 0 ? "#1a7f37" : "#57606a";
    const volume = document.createElement("span");
    volume.textContent = ` of ${r.opened12}`;
    volume.style.color = "#57606a";
    wrap.append(net, volume);
    return wrap;
  } },
  { key: "res60", fmt: (r) => (r.res60 === null ? "—" : r.res60 + "%") },
  { key: "topDecider", render: (r) => {
    const span = document.createElement("span");
    if (r.topDecider === null) {
      // Too few decisions to judge concentration — the most starved state,
      // surfaced instead of hidden behind a dash.
      span.textContent = `only ${r.topDeciderN} decision${r.topDeciderN === 1 ? "" : "s"}`;
      span.style.color = "#9a6700";
    } else {
      span.textContent = `${r.topDecider}% of ${r.topDeciderN}`;
    }
    return span;
  } },
];
const ovSort = { key: "res60", asc: true };
let ovRows = [];

function sortedOvRows() {
  const { key, asc } = ovSort;
  // Starved rows (no concentration %) sort as maximal concentration: near-zero
  // decisions is the extreme of fragility, not its absence.
  const value = (r) =>
    key === "topDecider" && r[key] === null ? Infinity : r[key];
  return [...ovRows].sort((a, b) => {
    const [va, vb] = [value(a), value(b)];
    if (va === null || vb === null) return (va === null) - (vb === null);
    const cmp = key === "name" ? a.name.localeCompare(b.name) : va - vb;
    return asc ? cmp : -cmp;
  });
}

function labelPill(name) {
  const entry = labelEntry(name);
  const span = document.createElement("span");
  span.textContent = name;
  if (entry && entry.color) {
    span.className = "label-pill";
    span.style.background = "#" + entry.color;
    const [r, g, b] = [0, 2, 4].map((o) => parseInt(entry.color.slice(o, o + 2), 16));
    span.style.color = (r * 299 + g * 587 + b * 114) / 1000 > 150 ? "#000" : "#fff";
  }
  return span;
}

function renderOverview() {
  const tbody = document.querySelector("#overview-table tbody");
  tbody.replaceChildren();
  for (const row of sortedOvRows()) {
    const tr = document.createElement("tr");
    tr.addEventListener("click", () => {
      state.label = row.name;
      render();
      document.querySelector(".controls").scrollIntoView({ behavior: "smooth" });
    });
    for (const col of overviewCols) {
      const td = document.createElement("td");
      if (col.render) {
        td.appendChild(col.render(row));
      } else {
        td.textContent = col.fmt ? col.fmt(row) : row[col.key];
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  document.querySelectorAll("#overview-table th").forEach((th, i) => {
    th.classList.toggle("sorted", overviewCols[i].key === ovSort.key);
    th.classList.toggle("asc", overviewCols[i].key === ovSort.key && ovSort.asc);
  });
}

function renderStatusTable(rows) {
  const tbody = document.querySelector("#status-table tbody");
  tbody.replaceChildren();
  for (const row of rows.sort((a, b) => b.openNow - a.openNow)) {
    const tr = document.createElement("tr");
    tr.addEventListener("click", () => {
      state.label = row.name;
      render();
      document.querySelector(".controls").scrollIntoView({ behavior: "smooth" });
    });
    for (const col of overviewCols.slice(0, 3)) {
      const td = document.createElement("td");
      if (col.render) {
        td.appendChild(col.render(row));
      } else {
        td.textContent = col.fmt ? col.fmt(row) : row[col.key];
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function initOverview() {
  // Outcome-applied labels (salvageable, cherrypick:*, ...) get their label at
  // close/merge time; their cohort stats would pollute a service-quality ranking.
  // Status labels mark a PR's state, not an area — listed separately, without
  // service columns, since their resolution restates the label's meaning.
  renderStatusTable(indexData.labels.filter((e) => e.status && !e.outcome)
    .map((e) => Object.assign({ net: e.opened12 - e.closed12 }, e)));
  ovRows = indexData.labels.filter((e) => !e.outcome && !e.status).map((e) =>
    Object.assign({ net: e.opened12 - e.closed12 }, e));
  document.querySelectorAll("#overview-table th").forEach((th, i) => {
    th.addEventListener("click", () => {
      const key = overviewCols[i].key;
      ovSort.asc = ovSort.key === key ? !ovSort.asc
                 : ["name", "res60"].includes(key);
      ovSort.key = key;
      renderOverview();
    });
  });
  renderOverview();
}

// ---- controls ---------------------------------------------------------------

function buildSelector() {
  const select = document.getElementById("label-select");
  const groups = { special: "—", topic: "topic", other: "other" };
  for (const key of Object.keys(groups)) {
    const entries = indexData.labels.filter((l) => l.group === key);
    if (!entries.length) continue;
    const optgroup = document.createElement("optgroup");
    optgroup.label = groups[key];
    for (const entry of entries) {
      const opt = document.createElement("option");
      opt.value = entry.name;
      opt.textContent = `${entry.name} (${entry.openNow} open)`;
      optgroup.appendChild(opt);
    }
    select.appendChild(optgroup);
  }
  select.addEventListener("change", () => {
    state.label = select.value;
    render();
  });
}

function bindControls() {
  for (const btn of document.querySelectorAll(".range-group button")) {
    btn.addEventListener("click", () => {
      state.range = btn.dataset.range === "all" ? "all" : +btn.dataset.range;
      render();
    });
  }
  document.getElementById("avg-toggle").addEventListener("change", (e) => {
    state.avg = e.target.checked;
    render();
  });
}

function syncControls() {
  document.getElementById("label-select").value = state.label;
  for (const btn of document.querySelectorAll(".range-group button")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.range) === String(state.range));
  }
  document.getElementById("avg-toggle").checked = state.avg;
}

// ---- main -------------------------------------------------------------------

let renderSeq = 0;

async function render() {
  const entry = labelEntry(state.label) || indexData.labels[0];
  state.label = entry.name;
  const seq = ++renderSeq;
  const errorBox = document.getElementById("error");
  let data;
  try {
    data = await loadLabel(entry);
  } catch (err) {
    errorBox.hidden = false;
    errorBox.textContent = `Could not load ${entry.name} (${err.message}).`;
    return;
  }
  if (seq !== renderSeq) return;  // a newer selection superseded this render
  errorBox.hidden = true;
  const view = computeView(data);

  syncControls();
  writeHash();
  updateCharts(view);
  updateTables(data);
  document.getElementById("tables-heading").textContent =
    `Last 12 months — ${state.label}`;

  const note = document.getElementById("clamp-note");
  note.hidden = !view.clamped;
  if (view.clamped) {
    note.textContent = `label first seen ${view.firstSeen} — range clamped`;
  }
}

async function init() {
  try {
    indexData = await fetchJson("data/index.json");
  } catch (err) {
    const box = document.getElementById("error");
    box.hidden = false;
    box.textContent = `Could not load data (${err.message}). ` +
      "If viewing locally, serve the docs/ directory: python3 -m http.server -d docs";
    return;
  }

  document.getElementById("data-through").textContent =
    indexData.months[indexData.months.length - 1];
  document.getElementById("updated").textContent = indexData.generatedAt.slice(0, 10);

  buildSelector();
  initOverview();
  bindControls();
  createCharts();
  makeSortable("reviewers-table", tableRows.reviewers, reviewerCols);
  makeSortable("authors-table", tableRows.authors, authorCols);

  state.label = "all";
  readHash();
  render();
}

init();
