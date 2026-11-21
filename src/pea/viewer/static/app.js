(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const POLL_INTERVAL_MS = 3000;

  const state = {
    runs: [],
    selectedRunId: null,
    detail: null,
    selectedCaptureIndex: 0,
    pollTimer: null,
  };

  const els = {
    healthPill: document.getElementById("health-pill"),
    autoRefresh: document.getElementById("auto-refresh"),
    refreshBtn: document.getElementById("refresh-btn"),
    runCount: document.getElementById("run-count"),
    runsList: document.getElementById("runs-list"),
    emptyDetail: document.getElementById("empty-detail"),
    runDetail: document.getElementById("run-detail"),
    detailPhase: document.getElementById("detail-phase"),
    detailSource: document.getElementById("detail-source"),
    detailProvBadge: document.getElementById("detail-provenance-badge"),
    detailTitle: document.getElementById("detail-title"),
    detailRunId: document.getElementById("detail-run-id"),
    detailUpdated: document.getElementById("detail-updated"),
    detailArtifactCount: document.getElementById("detail-artifact-count"),
    detailCalcCount: document.getElementById("detail-calc-count"),
    detailMessage: document.getElementById("detail-message"),
    detailError: document.getElementById("detail-error"),
    progressWrap: document.getElementById("progress-wrap"),
    progressText: document.getElementById("progress-text"),
    progressFill: document.getElementById("progress-fill"),
    downloadsBar: document.getElementById("downloads-bar"),
    captureCaption: document.getElementById("capture-caption"),
    captureSelect: document.getElementById("capture-select"),
    toggleOcr: document.getElementById("toggle-ocr"),
    ocrBoxCount: document.getElementById("ocr-box-count"),
    annotatedToggleWrap: document.getElementById("annotated-toggle-wrap"),
    toggleAnnotated: document.getElementById("toggle-annotated"),
    captureStage: document.getElementById("capture-stage"),
    captureImg: document.getElementById("capture-img"),
    ocrOverlay: document.getElementById("ocr-overlay"),
    ocrListWrap: document.getElementById("ocr-list-wrap"),
    ocrChips: document.getElementById("ocr-chips"),
    captureEmpty: document.getElementById("capture-empty"),
    figuresCount: document.getElementById("figures-count"),
    figuresGrid: document.getElementById("figures-grid"),
    figuresEmpty: document.getElementById("figures-empty"),
    provSummaryText: document.getElementById("provenance-summary-text"),
    provErrors: document.getElementById("provenance-errors"),
    provTbody: document.getElementById("provenance-tbody"),
  };

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function formatTime(isoStr) {
    if (!isoStr) return "—";
    const dt = new Date(isoStr);
    if (Number.isNaN(dt.getTime())) return isoStr;
    return dt.toLocaleString();
  }

  function phasePillClass(phase) {
    switch (phase) {
      case "complete":
        return "pill pill-ok";
      case "running":
        return "pill pill-info";
      case "failed":
        return "pill pill-err";
      case "cancelled":
      case "interrupted":
        return "pill pill-warn";
      default:
        return "pill pill-neutral";
    }
  }

  function sourcePillClass(source) {
    switch (source) {
      case "simulator_captured":
        return "pill pill-ok";
      case "bench_manual":
        return "pill pill-warn";
      case "model_computed":
        return "pill pill-info";
      default:
        return "pill pill-neutral";
    }
  }

  async function fetchJson(url) {
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status} for ${url}`);
    }
    return resp.json();
  }

  async function refreshHealth() {
    try {
      const health = await fetchJson("/api/health");
      els.healthPill.className = "pill pill-ok";
      els.healthPill.textContent = `${health.loopback_host}:${health.viewer_port} · v${health.version}`;
    } catch (_err) {
      els.healthPill.className = "pill pill-err";
      els.healthPill.textContent = "Disconnected";
    }
  }

  function renderRunsList() {
    const runs = state.runs;
    els.runCount.textContent = `${runs.length} ${runs.length === 1 ? "run" : "runs"}`;
    clearChildren(els.runsList);

    if (!runs.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No runs found yet. Start a session with pea-mcp.";
      els.runsList.appendChild(empty);
      return;
    }

    for (const run of runs) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "run-item" + (run.run_id === state.selectedRunId ? " active" : "");
      btn.setAttribute("role", "listitem");

      const top = document.createElement("div");
      top.className = "run-item-top";

      const label = document.createElement("span");
      label.className = "run-item-label";
      label.textContent = run.label || run.run_id;
      top.appendChild(label);

      const phase = document.createElement("span");
      phase.className = phasePillClass(run.phase);
      phase.textContent = run.phase;
      top.appendChild(phase);

      const idLine = document.createElement("div");
      idLine.className = "run-item-id mono";
      idLine.textContent = run.run_id;

      const tags = document.createElement("div");
      tags.className = "run-item-tags";

      const provTag = document.createElement("span");
      provTag.className = "mini-tag";
      provTag.textContent = `${run.provenance_entries} artifacts`;
      tags.appendChild(provTag);

      if (run.has_pdf) {
        const t = document.createElement("span");
        t.className = "mini-tag";
        t.textContent = "PDF";
        tags.appendChild(t);
      }
      if (run.has_docx) {
        const t = document.createElement("span");
        t.className = "mini-tag";
        t.textContent = "DOCX";
        tags.appendChild(t);
      }
      if (run.has_evidence_zip) {
        const t = document.createElement("span");
        t.className = "mini-tag";
        t.textContent = "ZIP";
        tags.appendChild(t);
      }

      btn.appendChild(top);
      btn.appendChild(idLine);
      btn.appendChild(tags);
      btn.addEventListener("click", () => {
        state.selectedRunId = run.run_id;
        state.selectedCaptureIndex = 0;
        els.toggleAnnotated.checked = false;
        renderRunsList();
        void loadRunDetail(run.run_id);
      });
      els.runsList.appendChild(btn);
    }
  }

  function renderDownloads(downloads) {
    clearChildren(els.downloadsBar);
    const entries = [
      { label: "Report PDF", url: downloads.pdf_url, primary: true, download: false },
      { label: "Editable DOCX", url: downloads.docx_url, primary: false, download: true },
      { label: "Evidence ZIP", url: downloads.evidence_zip_url, primary: false, download: true },
      { label: "Teacher Evidence PDF", url: downloads.teacher_packet_url, primary: false, download: false },
    ];

    for (const item of entries) {
      const a = document.createElement("a");
      a.textContent = item.label;
      if (item.url) {
        a.href = item.download ? `${item.url}?download=1` : item.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.className = item.primary ? "btn btn-primary" : "btn btn-secondary";
      } else {
        a.className = "btn btn-disabled";
        a.setAttribute("aria-disabled", "true");
      }
      els.downloadsBar.appendChild(a);
    }

    if (Array.isArray(downloads.extra_reports)) {
      for (const extra of downloads.extra_reports) {
        const a = document.createElement("a");
        a.href = extra.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.className = "btn btn-secondary";
        a.textContent = extra.name;
        els.downloadsBar.appendChild(a);
      }
    }
  }

  function getActiveCapture(detail) {
    const shots = Array.isArray(detail.screenshots) ? detail.screenshots : [];
    if (shots.length) {
      const idx = Math.min(Math.max(state.selectedCaptureIndex, 0), shots.length - 1);
      state.selectedCaptureIndex = idx;
      const item = shots[idx];
      return {
        relpath: item.relpath,
        url: item.url,
        annotated_url: item.annotated_url,
        ocr_boxes: Array.isArray(item.ocr_boxes) ? item.ocr_boxes : [],
        source: item.source,
      };
    }
    if (detail.latest_capture) {
      return {
        relpath: detail.latest_capture.screenshot_relpath,
        url: detail.latest_capture.screenshot_url,
        annotated_url: detail.latest_capture.annotated_url,
        ocr_boxes: Array.isArray(detail.latest_capture.ocr_boxes)
          ? detail.latest_capture.ocr_boxes
          : [],
        source: detail.latest_capture.source,
      };
    }
    return null;
  }

  function renderCapture(detail) {
    const shots = Array.isArray(detail.screenshots) ? detail.screenshots : [];
    clearChildren(els.captureSelect);
    if (shots.length > 1) {
      els.captureSelect.classList.remove("hidden");
      shots.forEach((shot, idx) => {
        const opt = document.createElement("option");
        opt.value = String(idx);
        opt.textContent = shot.relpath;
        if (idx === state.selectedCaptureIndex) opt.selected = true;
        els.captureSelect.appendChild(opt);
      });
    } else {
      els.captureSelect.classList.add("hidden");
    }

    const active = getActiveCapture(detail);
    if (!active) {
      els.captureStage.classList.add("hidden");
      els.captureEmpty.classList.remove("hidden");
      els.annotatedToggleWrap.classList.add("hidden");
      els.ocrBoxCount.textContent = "0";
      els.captureCaption.textContent = "No captures recorded for this run yet.";
      return;
    }

    els.captureEmpty.classList.add("hidden");
    els.captureStage.classList.remove("hidden");

    const hasAnnotated = Boolean(active.annotated_url);
    els.annotatedToggleWrap.classList.toggle("hidden", !hasAnnotated);
    if (!hasAnnotated) {
      els.toggleAnnotated.checked = false;
    }

    const useAnnotated = hasAnnotated && els.toggleAnnotated.checked;
    const imgUrl = useAnnotated ? active.annotated_url : active.url;
    if (els.captureImg.getAttribute("src") !== imgUrl) {
      els.captureImg.src = imgUrl;
    }
    els.captureCaption.textContent = `${active.relpath} (${active.source})`;

    const boxes = active.ocr_boxes || [];
    els.ocrBoxCount.textContent = String(boxes.length);

    clearChildren(els.ocrOverlay);
    clearChildren(els.ocrChips);

    const showOcr = els.toggleOcr.checked && !useAnnotated && boxes.length > 0;
    els.ocrOverlay.classList.toggle("hidden", !showOcr);
    els.ocrListWrap.classList.toggle("hidden", !(els.toggleOcr.checked && boxes.length > 0));

    if (showOcr) {
      boxes.forEach((box) => {
        if (!Array.isArray(box.bbox_norm) || box.bbox_norm.length !== 4) return;
        const [x1, y1, x2, y2] = box.bbox_norm;
        const rect = document.createElementNS(SVG_NS, "rect");
        rect.setAttribute("class", "ocr-rect");
        rect.setAttribute("x", String(Math.max(0, x1 * 1000)));
        rect.setAttribute("y", String(Math.max(0, y1 * 1000)));
        rect.setAttribute("width", String(Math.max(1, (x2 - x1) * 1000)));
        rect.setAttribute("height", String(Math.max(1, (y2 - y1) * 1000)));
        els.ocrOverlay.appendChild(rect);
      });
    }

    if (els.toggleOcr.checked && boxes.length > 0) {
      boxes.forEach((box, idx) => {
        const chip = document.createElement("span");
        chip.className = "ocr-chip";

        const idxEl = document.createElement("span");
        idxEl.className = "ocr-chip-idx mono";
        idxEl.textContent = `[${idx}]`;

        const txtEl = document.createElement("span");
        txtEl.textContent = String(box.text ?? "");

        const confEl = document.createElement("span");
        confEl.className = "ocr-chip-conf mono";
        const confNum = typeof box.confidence === "number" ? (box.confidence * 100).toFixed(0) : "?";
        confEl.textContent = `${confNum}%`;

        chip.appendChild(idxEl);
        chip.appendChild(txtEl);
        chip.appendChild(confEl);
        els.ocrChips.appendChild(chip);
      });
    }
  }

  function renderFigures(figures) {
    const list = Array.isArray(figures) ? figures : [];
    els.figuresCount.textContent = `${list.length} ${list.length === 1 ? "figure" : "figures"}`;
    clearChildren(els.figuresGrid);

    if (!list.length) {
      els.figuresEmpty.classList.remove("hidden");
      return;
    }
    els.figuresEmpty.classList.add("hidden");

    for (const fig of list) {
      const tile = document.createElement("div");
      tile.className = "figure-tile";

      const img = document.createElement("img");
      img.src = fig.png_url || fig.svg_url;
      img.alt = fig.figure_id;
      img.loading = "lazy";
      tile.appendChild(img);

      const foot = document.createElement("div");
      foot.className = "figure-tile-foot";

      const name = document.createElement("span");
      name.className = "figure-tile-name mono";
      name.textContent = fig.figure_id;
      foot.appendChild(name);

      const links = document.createElement("div");
      links.className = "figure-tile-links";
      if (fig.svg_url) {
        const a = document.createElement("a");
        a.href = fig.svg_url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = "SVG";
        links.appendChild(a);
      }
      if (fig.png_url) {
        const a = document.createElement("a");
        a.href = fig.png_url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = "PNG";
        links.appendChild(a);
      }
      foot.appendChild(links);
      tile.appendChild(foot);
      els.figuresGrid.appendChild(tile);
    }
  }

  function renderProvenance(detail) {
    const ver = detail.provenance_verification || { ok: true, checked_entries: 0, errors: [] };
    const entries = Array.isArray(detail.provenance) ? detail.provenance : [];

    els.provSummaryText.textContent = `${entries.length} ${entries.length === 1 ? "entry" : "entries"}`;

    if (ver.ok) {
      els.detailProvBadge.className = "pill pill-ok";
      els.detailProvBadge.textContent = `SHA-256 Verified (${ver.checked_entries})`;
      els.provErrors.classList.add("hidden");
      els.provErrors.textContent = "";
    } else {
      els.detailProvBadge.className = "pill pill-err";
      els.detailProvBadge.textContent = `Provenance Mismatch (${ver.errors.length})`;
      els.provErrors.classList.remove("hidden");
      els.provErrors.textContent = ver.errors.join(" | ");
    }

    clearChildren(els.provTbody);
    if (!entries.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "empty-state";
      td.textContent = "No provenance artifacts logged yet.";
      tr.appendChild(td);
      els.provTbody.appendChild(tr);
      return;
    }

    for (const entry of entries) {
      const tr = document.createElement("tr");

      const tdPath = document.createElement("td");
      tdPath.className = "mono path-col";
      const link = document.createElement("a");
      link.href = `/runs/${encodeURIComponent(detail.run_id)}/${entry.relpath
        .split("/")
        .map(encodeURIComponent)
        .join("/")}`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = entry.relpath;
      tdPath.appendChild(link);

      const tdKind = document.createElement("td");
      tdKind.textContent = entry.kind;

      const tdSource = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = sourcePillClass(entry.source);
      pill.textContent = entry.source;
      tdSource.appendChild(pill);

      const tdSize = document.createElement("td");
      tdSize.className = "num-col mono";
      tdSize.textContent = String(entry.size_bytes);

      const tdSha = document.createElement("td");
      tdSha.className = "mono sha-col";
      tdSha.title = entry.sha256;
      tdSha.textContent = `${entry.sha256.slice(0, 14)}…`;

      tr.appendChild(tdPath);
      tr.appendChild(tdKind);
      tr.appendChild(tdSource);
      tr.appendChild(tdSize);
      tr.appendChild(tdSha);
      els.provTbody.appendChild(tr);
    }
  }

  function renderRunDetail(detail) {
    state.detail = detail;
    els.emptyDetail.classList.add("hidden");
    els.runDetail.classList.remove("hidden");

    const st = detail.status;
    els.detailPhase.className = phasePillClass(st.phase);
    els.detailPhase.textContent = st.phase;

    const src = st.source || "unspecified";
    els.detailSource.className = sourcePillClass(src);
    els.detailSource.textContent = src;

    els.detailTitle.textContent = st.label || st.run_id;
    els.detailRunId.textContent = st.run_id;
    els.detailUpdated.textContent = formatTime(st.updated_at);
    els.detailArtifactCount.textContent = String(detail.provenance_summary?.total ?? 0);
    els.detailCalcCount.textContent = String(detail.calculations_count ?? 0);

    if (st.message) {
      els.detailMessage.textContent = st.message;
      els.detailMessage.classList.remove("hidden");
    } else {
      els.detailMessage.classList.add("hidden");
    }

    if (st.error) {
      els.detailError.textContent = st.error;
      els.detailError.classList.remove("hidden");
    } else {
      els.detailError.classList.add("hidden");
    }

    if (typeof st.progress_total === "number" && st.progress_total > 0) {
      const cur = typeof st.progress_current === "number" ? st.progress_current : 0;
      const pct = Math.min(100, Math.max(0, Math.round((cur / st.progress_total) * 100)));
      els.progressWrap.classList.remove("hidden");
      els.progressText.textContent = `${cur} / ${st.progress_total} (${pct}%)`;
      els.progressFill.style.width = `${pct}%`;
    } else {
      els.progressWrap.classList.add("hidden");
    }

    renderDownloads(detail.downloads || {});
    renderCapture(detail);
    renderFigures(detail.figures || []);
    renderProvenance(detail);
  }

  async function loadRunDetail(runId) {
    if (!runId) return;
    try {
      const detail = await fetchJson(`/api/runs/${encodeURIComponent(runId)}`);
      if (state.selectedRunId === runId) {
        renderRunDetail(detail);
      }
    } catch (_err) {
      // Keep prior view if a transient poll fails.
    }
  }

  async function refreshAll() {
    await refreshHealth();
    try {
      const payload = await fetchJson("/api/runs");
      state.runs = Array.isArray(payload.runs) ? payload.runs : [];
      if (!state.selectedRunId && state.runs.length > 0) {
        state.selectedRunId = state.runs[0].run_id;
      } else if (
        state.selectedRunId &&
        !state.runs.some((r) => r.run_id === state.selectedRunId)
      ) {
        state.selectedRunId = state.runs.length > 0 ? state.runs[0].run_id : null;
      }
      renderRunsList();
      if (state.selectedRunId) {
        await loadRunDetail(state.selectedRunId);
      } else {
        els.emptyDetail.classList.remove("hidden");
        els.runDetail.classList.add("hidden");
      }
    } catch (_err) {
      // Health pill already reflects disconnect state.
    }
  }

  function syncPolling() {
    if (state.pollTimer !== null) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (els.autoRefresh.checked) {
      state.pollTimer = window.setInterval(() => {
        void refreshAll();
      }, POLL_INTERVAL_MS);
    }
  }

  els.refreshBtn.addEventListener("click", () => {
    void refreshAll();
  });
  els.autoRefresh.addEventListener("change", syncPolling);
  els.toggleOcr.addEventListener("change", () => {
    if (state.detail) renderCapture(state.detail);
  });
  els.toggleAnnotated.addEventListener("change", () => {
    if (state.detail) renderCapture(state.detail);
  });
  els.captureSelect.addEventListener("change", () => {
    const idx = Number.parseInt(els.captureSelect.value, 10);
    state.selectedCaptureIndex = Number.isNaN(idx) ? 0 : idx;
    els.toggleAnnotated.checked = false;
    if (state.detail) renderCapture(state.detail);
  });

  void refreshAll();
  syncPolling();
})();
