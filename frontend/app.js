(() => {
  "use strict";

  const API = "/api/v1";
  const SVG = "http://www.w3.org/2000/svg";
  const M = window.VioletModel;
  const D = window.VioletDrafting;
  const C = window.VioletCadMapping;
  const CW = window.VioletCadWorkspace;
  const H = window.VioletProjectHistory;
  const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  const af = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const state = {
    projects: [], project: null, validation: null, baseRevision: 0, baseJson: "",
    dirty: false, editorDirty: false, selectedFloorId: null, selection: null,
    undo: [], redo: [], tool: "select", polygon: null, rectangle: null, guideDraft: null, guideMode: "vertical", measureDraft: null, snapVisual: null, view: null, viewFloorId: null,
    dragging: null, partitionDraft: null, openingPreview: null, openingSettings: {kind: "door", width_mm: 900, height_mm: 2100, sill_mm: 0}, suppressPlanClick: false, loading: 0, entityType: null, source: null, catalog: null,
    catalogValid: false, validateTimer: null, engineeringStale: true,
    sourceMode: "select", sourceDraftPoints: [], calibrationClicks: [], sourceSelection: [], sourcePan: null, sourceSuppressClick: false, activeTab:"model",
  };
  const dom = {};
  let vendorLibrary = null, modelLibrary = null, recoveryStore = null, recoveryEntry = null, recoveryError = "", detachStepRequest = null;
  let stepViewer = null, wallMount = null, batchMount = null, mountSections = null, mountAssemblies = null, specialAssemblies = null, wholeWall = null, roomAssembly = null, buildingAssembly = null, boxSelectButton = null, cadViewSequence = 0, sourceLoadSequence = 0;
  let projectListSequence = 0, projectSelectSequence = 0, trashSequence = 0, deleteRequest = null, trashBusyId = null;
  let trashProjects = [], emptyTrashRequest = null;
  let baseSnapshot = null, jsonEditorReference = "", jsonEditorProject = null, jsonEditorSerial = -1;
  let modelChangeSerial = 0, mountPreparationTask = null, lastMountPreparation = null, saveInFlight = false, backgroundBuilds = null;
  let finishPresentation = null;

  class ApiError extends Error {
    constructor(status, payload) {
      const info = payload && payload.error;
      super(info && info.message ? info.message : `HTTP ${status}`);
      this.status = status;
      this.code = info && info.code;
      this.details = info && info.details;
    }
  }

  function $(id) { return document.getElementById(id); }
  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined && text !== null) result.textContent = String(text);
    return result;
  }
  function svg(tag, attrs = {}) {
    const result = document.createElementNS(SVG, tag);
    Object.entries(attrs).forEach(([key, value]) => result.setAttribute(key, String(value)));
    return result;
  }
  function arrays(value) { return Array.isArray(value) ? value : []; }
  function number(value, fallback = 0) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : fallback; }
  function json(value) { return JSON.stringify(value, null, 2); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function safeFilename(value) { return String(value || "project").replace(/[^a-zA-Zа-яА-ЯёЁ0-9_-]+/g, "_").slice(0, 80) || "project"; }

  function cacheDom() {
    document.querySelectorAll("[id]").forEach((element) => { dom[element.id] = element; });
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const init = { ...options, headers };
    if (init.body !== undefined && typeof init.body !== "string" && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(init.body);
    }
    let response;
    try { response = await fetch(`${API}${path}`, init); }
    catch (_error) { connection(false); throw new Error("Локальный API недоступен."); }
    connection(true);
    const text = await response.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch (_error) { payload = { error: { message: text } }; } }
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }

  async function requestBlob(path, options = {}) {
    let response;
    try { response = await fetch(`${API}${path}`, options); }
    catch (_error) { connection(false); throw new Error("Локальный API недоступен."); }
    connection(true);
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch (_error) { /* empty */ }
      throw new ApiError(response.status, payload);
    }
    return response.blob();
  }

  async function busy(label, operation) {
    state.loading += 1; dom.loadingLabel.textContent = label; dom.loadingOverlay.classList.remove("is-hidden");
    try { return await operation(); }
    finally { state.loading -= 1; if (!state.loading) dom.loadingOverlay.classList.add("is-hidden"); }
  }

  function connection(ok) {
    dom.connectionDot.classList.toggle("is-online", ok); dom.connectionDot.classList.toggle("is-offline", !ok);
    dom.connectionLabel.textContent = ok ? "Локальный API доступен" : "API недоступен";
  }
  function toast(message, kind = "info", duration = 4500) {
    const item = node("div", `toast ${kind}`, message); dom.toastRegion.append(item); setTimeout(() => item.remove(), duration);
  }
  function errorText(error, fallback) {
    let message = error && error.message ? error.message : fallback;
    const details = error && error.details;
    if (details && Array.isArray(details.issues) && details.issues[0] && details.issues[0].message) message += ` ${details.issues[0].message}`;
    return message;
  }
  function showError(error, fallback = "Операция не выполнена") { toast(errorText(error, fallback), "error", 8000); }
  function inlineError(element, error, fallback) { element.textContent = errorText(error, fallback); element.classList.remove("is-hidden"); }
  function clearInlineError(element) { element.textContent = ""; element.classList.add("is-hidden"); }

  function bind() {
    dom.newProjectButton.addEventListener("click", openNewProject);
    dom.emptyNewProjectButton.addEventListener("click", openNewProject);
    dom.demoButton.addEventListener("click", createDemo); dom.emptyDemoButton.addEventListener("click", createDemo);
    dom.refreshProjectsButton.addEventListener("click", () => loadProjects(false));
    dom.projectTrashButton.addEventListener("click", openProjectTrash);
    dom.refreshTrashButton.addEventListener("click", loadProjectTrash);
    dom.emptyProjectTrashButton.addEventListener("click", beginEmptyProjectTrash);
    dom.cancelEmptyProjectTrashButton.addEventListener("click", cancelEmptyProjectTrash);
    dom.confirmEmptyProjectTrashButton.addEventListener("click", emptyProjectTrash);
    dom.closeProjectTrashButton.addEventListener("click", () => { if (!trashBusyId) dom.projectTrashDialog.close(); });
    dom.projectTrashDialog.addEventListener("cancel", event => { if (trashBusyId) event.preventDefault(); });
    dom.projectTrashDialog.addEventListener("close", () => { trashSequence++; cancelEmptyProjectTrash(); dom.projectTrashButton.focus(); });
    dom.deleteProjectForm.addEventListener("submit", deleteProject);
    [dom.cancelDeleteProjectButton, dom.closeDeleteProjectButton].forEach(button => button.addEventListener("click", cancelProjectDeletion));
    dom.deleteProjectDialog.addEventListener("cancel", event => { event.preventDefault(); cancelProjectDeletion(); });
    dom.deleteProjectDraftAcknowledgement.addEventListener("change", syncProjectDeleteButtons);
    dom.downloadDeletingDraftButton.addEventListener("click", downloadDeletingDraft);
    dom.importButton.addEventListener("click", () => dom.importFileInput.click()); dom.importFileInput.addEventListener("change", importProject);
    dom.newProjectForm.addEventListener("submit", createProject); dom.entityForm.addEventListener("submit", submitEntity);
    dom.saveProjectButton.addEventListener("click", saveDraft); dom.exportButton.addEventListener("click", () => downloadJson(state.project, `${safeFilename(state.project.metadata.name)}.json`));
    dom.retryAssembliesButton?.addEventListener("click", async () => {
      if (state.dirty || mountPreparationHasUnappliedInputs()) return toast("Сначала сохраните изменения проекта.", "error");
      try { await backgroundBuilds?.start({force:true}); } catch (error) { showError(error, "Не удалось запустить расчёт"); }
    });
    dom.historyButton.addEventListener("click", openHistory); dom.closeHistoryButton.addEventListener("click", () => dom.historyDialog.close());
    dom.undoButton.addEventListener("click", undo); dom.redoButton.addEventListener("click", redo);
    dom.reloadRemoteButton.addEventListener("click", reloadRemote); dom.downloadDraftButton.addEventListener("click", () => downloadJson(state.project, `${safeFilename(state.project.metadata.name)}_draft.json`));
    document.querySelectorAll(".tab[data-tab]").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
    dom.floorSelect.addEventListener("change", () => selectFloor(dom.floorSelect.value));
    dom.sourceFloorSelect.addEventListener("change", () => selectFloor(dom.sourceFloorSelect.value));
    dom.addBuildingButton.addEventListener("click", () => openEntity("building")); dom.addFloorButton.addEventListener("click", () => openEntity("floor"));
    dom.quickAddFloorButton.addEventListener("click", () => openEntity("floor"));
    dom.quickBatchMountButton.addEventListener("click", () => openBatchMount(defaultBatchMountScope()));
    dom.quickExteriorMountButton.addEventListener("click",()=>{const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;if(floor)openBatchMount({kind:"exterior_floor",id:floor.id});});
    dom.prepareSpecialButton.addEventListener("click", () => openSpecialAssemblies(defaultBatchMountScope()));
    dom.prepareWholeWallButton?.addEventListener("click", () => openWholeWall(defaultBatchMountScope()));
    dom.quickWholeWallButton?.addEventListener("click", () => openWholeWall(defaultBatchMountScope()));
    dom.quickRoomAssemblyButton?.addEventListener("click", () => openRoomAssembly(defaultBatchMountScope()));
    dom.quickBuildingAssemblyButton?.addEventListener("click", () => openBuildingAssembly());
    dom.sourceAddFloorButton.addEventListener("click", () => openEntity("floor"));
    dom.addRectangleButton.addEventListener("click", startRectangle); dom.startPolygonButton.addEventListener("click", startPolygon);
    dom.addOpeningButton.addEventListener("click", startOpening); dom.addZoneButton.addEventListener("click", () => openEntity("zone"));
    dom.addPanelSystemButton.addEventListener("click", () => openEntity("panel")); dom.addMeasurementButton.addEventListener("click", () => openEntity("measurement"));
    dom.selectToolButton.addEventListener("click", () => activateTool("select")); dom.rectangleToolButton.addEventListener("click", startRectangle); dom.panToolButton.addEventListener("click", () => activateTool("pan")); dom.polygonToolButton.addEventListener("click", startPolygon); dom.guideToolButton.addEventListener("click", startGuide); dom.measureToolButton.addEventListener("click", startMeasure);
    dom.partitionToolButton.addEventListener("click", startPartition); dom.openingToolButton.addEventListener("click", startOpening);
    dom.splitWallsButton.addEventListener("click", splitWalls);
    const panelToggle = (id, className) => dom[id].addEventListener("click", () => { document.body.classList.toggle(className); syncPanelButtons(); });
    panelToggle("toggleProjectsButton", "projects-open"); panelToggle("toggleStructureButton", "structure-open"); panelToggle("toggleInspectorButton", "inspector-hidden"); panelToggle("editorFocusButton", "editor-focus");
    [dom.closeProjectsButton, dom.projectsBackdrop].forEach((button) => button.addEventListener("click", () => { document.body.classList.remove("projects-open"); syncPanelButtons(); }));
    dom.toggleInspectorButton.addEventListener("click", () => { if (document.body.classList.contains("editor-focus")) { document.body.classList.remove("editor-focus", "inspector-hidden"); syncPanelButtons(); } });
    document.querySelectorAll(".setup-tools button").forEach((button) => button.addEventListener("click", () => { const menu = button.closest("details"); if (menu) menu.open = false; }));
    document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog)?.close()));
    syncPanelButtons();
    dom.finishPolygonButton.addEventListener("click", finishPolygon); dom.cancelPolygonButton.addEventListener("click", cancelPolygon);
    dom.zoomInButton.addEventListener("click", () => zoomPlan(0.8)); dom.zoomOutButton.addEventListener("click", () => zoomPlan(1.25)); dom.fitPlanButton.addEventListener("click", () => { state.view = null; renderPlan(); });
    dom.snapInput.addEventListener("change", () => { if (!(number(dom.snapInput.value) > 0)) dom.snapInput.value = "100"; state.snapVisual = null; renderPlan(); });
    [dom.gridToggle, dom.snapToggle].forEach((control) => control.addEventListener("change", () => { state.snapVisual = null; renderPlan(); }));
    dom.planCanvas.addEventListener("dblclick", (event) => { if (state.polygon) { event.preventDefault(); finishPolygon(); } });
    dom.jsonEditor.addEventListener("input", () => { state.editorDirty = dom.jsonEditor.value !== jsonEditorReference; renderDraftState(); });
    dom.formatJsonButton.addEventListener("click", formatJson); dom.validateJsonButton.addEventListener("click", validateJsonText);
    dom.resetJsonButton.addEventListener("click", () => syncJsonEditor(true)); dom.applyJsonButton.addEventListener("click", applyJson);
    dom.inspectSourceButton.addEventListener("click", inspectSource); dom.convertSourceButton.addEventListener("click", convertSource);
    dom.sourcePageSelect.addEventListener("change", () => { if (!state.source) return; if(!canChangeSource()){dom.sourcePageSelect.value=String(state.source.page);return;} state.source.page = number(dom.sourcePageSelect.value, 1); state.source.previewWidth = null; state.source.previewHeight = null; state.source.overlayBlocked = false; state.sourceDraftPoints = []; state.calibrationClicks = [];state.sourceSelection=[];state.sourceRegionPoints=[]; dom.markupPoints.value = ""; renderSourceInspection();renderSourcePreview(); renderMarkupList(); });
    dom.loadAttachedSourceButton.addEventListener("click", loadAttachedSource);
    dom.markupModeButton.addEventListener("click", () => setSourceMode("markup")); dom.calibrationModeButton.addEventListener("click", () => setSourceMode("calibration"));
    dom.sourceSelectModeButton.addEventListener("click", () => setSourceMode("select")); dom.sourcePanModeButton.addEventListener("click", () => setSourceMode("pan"));
    dom.sourceAssignButton.addEventListener("click", assignSourceSelection); dom.sourceClearSelectionButton.addEventListener("click", () => { state.sourceSelection = []; paintSourceOverlay(); renderSourceSelection(); });
    dom.sourceJoinButton.addEventListener("click", joinSourceSelection);
    dom.sourceBuildModelButton.addEventListener("click", buildSourceModel);
    dom.sourceEntityLayerFilter.addEventListener("change", () => { state.sourceSelection = []; if(isWorkspaceDxf()) loadCadDxfView(state.source.region || null); else {paintSourceOverlay(); renderSourceSelection();} });
    dom.cadRegionButton.addEventListener("click", () => { if(!isWorkspaceDxf())return; state.sourceRegionPoints=[];setSourceMode(state.sourceMode === "region" ? "select" : "region");paintSourceOverlay(); });
    dom.cadFullViewButton.addEventListener("click", () => loadCadDxfView(null));
    dom.cadRefreshViewButton.addEventListener("click", () => { const b=state.sourceViewBox;if(b)loadCadDxfView([b.x,-b.y-b.h,b.x+b.w,-b.y]); });
    dom.cadRecentRefreshButton.addEventListener("click", loadCadRecent);
    dom.cadRecentOpenButton.addEventListener("click", () => openCadAsset(dom.cadRecentSelect.value));
    dom.attachStepButton.addEventListener("click", attachStepAsset);
    dom.detachStepButton.addEventListener("click", detachStepAsset);
    dom.sourceFileInput.addEventListener("change",()=>{sourceLoadSequence++;});
    dom.sourceZoomInButton.addEventListener("click", () => zoomSource(.8)); dom.sourceZoomOutButton.addEventListener("click", () => zoomSource(1.25)); dom.sourceZoomFitButton.addEventListener("click", () => { state.sourceViewBox = {...state.sourceFitBox}; updateSourceView(); });
    dom.sourceEntityKind.addEventListener("change", () => { const windowKind = dom.sourceEntityKind.value === "window"; dom.sourceOpeningHeightInput.value = windowKind ? "1500" : "2100"; dom.sourceOpeningSillInput.value = windowKind ? "900" : "0"; });
    dom.markupKind.addEventListener("change", () => { if(["door","window","passage","opening"].includes(dom.markupKind.value)){ const windowKind=dom.markupKind.value === "window";dom.sourceOpeningHeightInput.value=windowKind ? "1500" : "2100";dom.sourceOpeningSillInput.value=windowKind ? "900" : "0";} });
    dom.clearSourceDraftButton.addEventListener("click", () => { state.sourceDraftPoints = []; state.calibrationClicks = []; dom.markupPoints.value = ""; paintSourceOverlay(); });
    dom.resetSourceAttachmentButton.addEventListener("click", resetSourceAttachment);
    dom.addMarkupButton.addEventListener("click", addMarkup); dom.saveCalibrationButton.addEventListener("click", saveCalibration);
    dom.validateCatalogButton.addEventListener("click", validateCatalog); dom.attachCatalogButton.addEventListener("click", attachCatalog);
    dom.refreshEngineeringButton.addEventListener("click", loadEngineering);
    dom.documentFormat.addEventListener("change", changeDocumentFormat);
    dom.sheetPreviousButton.addEventListener("click", () => showAdjacentSheet(-1));
    dom.sheetNextButton.addEventListener("click", () => showAdjacentSheet(1));
    dom.sheetZoom.addEventListener("change",()=>{dom.sheetPreview.style.setProperty("--sheet-zoom",`${Number(dom.sheetZoom.value)||100}%`);});
    dom.previewPdfButton.addEventListener("click", async () => {if (await prepareDocumentAssemblies()) setTab("engineering");}); dom.downloadPdfButton.addEventListener("click", () => downloadDocument("pdf")); dom.downloadDxfButton.addEventListener("click", () => downloadDocument("dxf")); dom.downloadZipButton.addEventListener("click", () => downloadDocument("zip"));
    window.addEventListener("beforeunload", (event) => { if (state.dirty || mountPreparationHasUnappliedInputs()) { event.preventDefault(); event.returnValue = ""; } });
    document.addEventListener("keydown", (event) => {
      if ([dom.newProjectDialog, dom.entityDialog, dom.historyDialog, dom.deleteProjectDialog, dom.projectTrashDialog, dom.wallMountDialog, dom.batchMountDialog, dom.mountSectionsDialog, dom.mountAssembliesDialog, dom.specialAssembliesDialog, dom.wholeWallDialog, dom.roomAssemblyDialog, dom.buildingAssemblyDialog, dom.finishPresentationDialog].some((dialog) => dialog?.open)) return;
      if (state.activeTab !== "model") {
        if (state.activeTab === "sources" && event.key === "Escape" && !isTyping(event.target)) { state.sourceSelection=[];state.sourceDraftPoints=[];state.calibrationClicks=[];state.sourceRegionPoints=[];if(state.sourceMode==="region")setSourceMode("select");dom.markupPoints.value="";paintSourceOverlay();renderSourceSelection();event.preventDefault(); }
        if (state.activeTab === "sources" && event.key === "Enter" && !isTyping(event.target) && state.sourceDraftPoints.length) {event.preventDefault();addMarkup();}
        if ((event.ctrlKey || event.metaKey) && event.code === "KeyS" && state.project && !isTyping(event.target)) {event.preventDefault();saveDraft();}
        return;
      }
      if (event.key === "Escape" && document.body.classList.contains("projects-open")) { document.body.classList.remove("projects-open"); syncPanelButtons(); event.preventDefault(); return; }
      if (event.key === "Escape" && state.project) { event.preventDefault(); cancelActiveDraft(); return; }
      if (event.key === "Enter" && state.polygon && isTyping(event.target) && event.target.tagName !== "TEXTAREA") { event.preventDefault(); finishPolygon(); return; }
      if (event.key === "Enter" && (!hasToolDraft() || event.target?.closest?.("summary"))) return;
      if ((event.key === " " || event.key === "Delete" || event.key === "Backspace") && event.target?.closest?.("button, a")) return;
      if (event.key === "Enter" && event.target?.closest?.("button, a") && (!hasToolDraft() || !event.target.closest('[role="toolbar"]'))) return;
      const modifier = event.ctrlKey || event.metaKey;
      const physicalKey = /^Key[A-Z]$/.test(event.code || "") ? event.code.slice(3).toLowerCase() : event.key.toLowerCase();
      if (modifier && physicalKey === "s" && state.project && !isTyping(event.target)) { event.preventDefault(); saveDraft(); }
      if (modifier && physicalKey === "z" && !event.shiftKey && !isTyping(event.target)) { event.preventDefault(); undo(); }
      if (modifier && (physicalKey === "y" || (event.shiftKey && physicalKey === "z")) && !isTyping(event.target)) { event.preventDefault(); redo(); }
      if (isTyping(event.target) || modifier || event.altKey) return;
      if (event.key === "F7" || event.key === "F9") { const control = event.key === "F7" ? dom.gridToggle : dom.snapToggle; control.checked = !control.checked; state.snapVisual = null; renderPlan(); event.preventDefault(); return; }
      if (state.polygon && physicalKey === "c") { finishPolygon(); event.preventDefault(); return; }
      if (state.polygon && (event.key === "Backspace" || physicalKey === "u")) { state.polygon.points.pop(); state.polygon.preview = null; renderPlan(); renderInspector(); event.preventDefault(); return; }
      if (physicalKey === "v") activateTool("select"); else if (physicalKey === "b") activateTool("box"); else if (physicalKey === "r") startRectangle(); else if (physicalKey === "p") startPolygon(); else if (physicalKey === "l") startGuide(); else if (physicalKey === "m") startMeasure(); else if (physicalKey === "h") activateTool("pan"); else if (physicalKey === "w") startPartition(); else if (physicalKey === "o") startOpening();
      else if (event.key === "Enter") commitActiveDraft(); else if (event.key === "Escape") cancelActiveDraft(); else if (event.key === "Delete" || event.key === "Backspace") deleteSelection(); else return;
      event.preventDefault();
    });
  }
  function isTyping(target) { return target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable); }
  function syncPanelButtons() {
    dom.toggleProjectsButton.setAttribute("aria-expanded", String(document.body.classList.contains("projects-open")));
    dom.toggleStructureButton.setAttribute("aria-expanded", String(document.body.classList.contains("structure-open")));
    dom.toggleInspectorButton.setAttribute("aria-expanded", String(!document.body.classList.contains("inspector-hidden")));
    dom.editorFocusButton.setAttribute("aria-pressed", String(document.body.classList.contains("editor-focus")));
    dom.editorFocusButton.textContent = document.body.classList.contains("editor-focus") ? "Вернуть панели" : "Развернуть чертёж";
  }
  function showInspector() { document.body.classList.remove("inspector-hidden"); syncPanelButtons(); }

  async function init() {
    if (!M || !D || !C) { document.body.textContent = "Не загружены модули редактора. Обновите страницу."; return; }
    cacheDom(); bind(); renderEmpty(); initializeRecovery();
    if (window.VioletEngineeringJobs) backgroundBuilds = window.VioletEngineeringJobs.create({
      request, getProject: () => state.project, getRevision: () => state.baseRevision,
      canApply: () => !state.dirty && !mountPreparationHasUnappliedInputs() && !saveInFlight && !wallMount?.hasDraft?.() && !batchMount?.hasDraft?.(),
      onStatus: (job, message) => {
        const active = window.VioletEngineeringJobs.ACTIVE.has(job.status);
        mountPreparationStatus(active ? `${message.startsWith("Проект сохранён.") ? "" : "Проект сохранён. "}${message}${job.total ? ` · ${job.completed || 0} из ${job.total}` : ""}` : message, active ? "working" : job.status === "ready" ? "ready" : "warning");
        if (dom.retryAssembliesButton) {dom.retryAssembliesButton.hidden = active || job.status === "ready" || job.requires_reload === true; dom.retryAssembliesButton.disabled = state.dirty;}
        if (job.requires_reload) {dom.conflictMessage.textContent = message; dom.conflictBanner.classList.remove("is-hidden");}
        if (dom.assemblyJobIssues) {
          const issues = arrays(job.outcomes).filter(item => item.status !== "ready");
          dom.assemblyJobDetails.classList.toggle("is-hidden", !issues.length);
          dom.assemblyJobIssues.replaceChildren(...issues.map(item => node("li", "", `${item.label || (item.kind === "wall" ? "Стена" : "Узел")}: ${item.message || item.reason || "Расчёт не завершён"}`)));
        }
      },
      onResult: result => {
        // Derived geometry has no revision of its own and must not make a clean
        // project dirty, reset the view, or replace a user's unfinished inputs.
        state.project = window.VioletEngineeringJobs.mergeDerived(state.project, result.properties);
        setProjectBaseline(state.project); state.engineeringStale = true;
        syncJsonEditor(); renderDraftState(); modelLibrary?.refreshProject();
        for (const panel of [wallMount, batchMount, mountSections, mountAssemblies, specialAssemblies, wholeWall]) {
          if (!panel?.hasDraft?.()) panel?.refresh?.();
        }
        if (dom.sheetPreview) dom.sheetPreview.classList.add("is-stale");
      },
    });
    boxSelectButton=node("button","tool-button","▧ Рамка B");boxSelectButton.type="button";boxSelectButton.setAttribute("aria-label","Рамочное выделение B");boxSelectButton.setAttribute("aria-pressed","false");boxSelectButton.addEventListener("click",()=>activateTool("box"));dom.selectToolButton.insertAdjacentElement("afterend",boxSelectButton);
    if (window.ResizeObserver) new ResizeObserver(() => { if (state.planSvg && state.view) updateGridBounds(); }).observe(dom.planCanvas);
    if (window.VioletVendorLibrary) vendorLibrary = window.VioletVendorLibrary.create({host:dom.libraryPanel,request,getProject:()=>state.project,mutate,toast,canMutate:()=>{
      if(state.editorDirty || hasActiveDraft()){toast("Сначала примените JSON или завершите текущее построение.","error");return false;}return Boolean(state.project);
    }});
    if (window.VioletWallMount) {
      wallMount = window.VioletWallMount.create(dom.wallMountHost, {
        getProject: () => state.project, getSelection: () => state.selection, request, mutate, toast,
        onApplied: (_scope, onStatus) => deferMountPreparation(onStatus),
        onClose: () => { dom.wallMountDialog.close(); state.planSvg?.focus({preventScroll:true}); },
        onPrepareAssemblies: scope => openMountAssemblies(scope),
        openModel: async asset => {
          if (!wallMount.close() || !canChangeSource()) return;
          setTab("sources");
          try { await openLibraryAsset({id: asset.source_id || asset.id, filename: asset.filename}); }
          catch (error) { showError(error, "Не удалось открыть STEP из библиотеки"); }
        },
      });
      dom.wallMountDialog.addEventListener("cancel", event => { event.preventDefault(); wallMount.close(); });
    }
    if (window.VioletBatchMount) {
      batchMount = window.VioletBatchMount.create(dom.batchMountHost, {
        getProject: () => state.project, request, mutate, toast,
        onApplied: (_scope, onStatus) => deferMountPreparation(onStatus),
        onClose: () => {dom.batchMountDialog.close(); state.planSvg?.focus({preventScroll:true});},
        onInspectWall: (wallId, faceId) => openWallMount(wallId, faceId),
        onPrepareSections: scope => openMountSections(scope),
        onPrepareAssemblies: scope => openMountAssemblies(scope),
      });
      dom.batchMountDialog.addEventListener("cancel", event => {event.preventDefault(); batchMount.close();});
    }
    if (window.VioletMountSections) {
      mountSections = window.VioletMountSections.create(dom.mountSectionsHost, {
        getProject: () => state.project, request, mutate, toast,
        onClose: () => {dom.mountSectionsDialog.close(); state.planSvg?.focus({preventScroll:true});},
      });
      dom.mountSectionsDialog.addEventListener("cancel", event => {event.preventDefault(); mountSections.close();});
    }
    if (window.VioletMountAssemblies) {
      mountAssemblies = window.VioletMountAssemblies.create(dom.mountAssembliesHost, {
        getProject: () => state.project, request, mutate, toast,
        onClose: () => {dom.mountAssembliesDialog.close(); state.planSvg?.focus({preventScroll:true});},
      });
      dom.mountAssembliesDialog.addEventListener("cancel", event => {event.preventDefault(); mountAssemblies.close();});
    }
    if (window.VioletSpecialAssemblies) {
      specialAssemblies = window.VioletSpecialAssemblies.create(dom.specialAssembliesHost, {
        getProject: () => state.project, request, mutate, toast,
        onClose: () => {dom.specialAssembliesDialog.close(); state.planSvg?.focus({preventScroll:true});},
      });
      dom.specialAssembliesDialog.addEventListener("cancel", event => {event.preventDefault(); specialAssemblies.close();});
    }
    if (window.VioletWholeWall) {
      wholeWall = window.VioletWholeWall.create(dom.wholeWallHost, {
        getProject: () => state.project, request, mutate, toast,
        onClose: () => {dom.wholeWallDialog.close(); state.planSvg?.focus({preventScroll:true});},
        downloadStep: async assembly => {
          const blob = await requestBlob("/whole-wall/export-step", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({assembly})});
          downloadBlob(blob, `${safeFilename(assembly.label)}_стена.step`);
        },
      });
      dom.wholeWallDialog.addEventListener("cancel", event => {event.preventDefault(); wholeWall.close();});
    }
    if (window.VioletRoomAssembly) {
      let storage = null; try {storage = window.sessionStorage;} catch (_) { /* Private mode still retains jobs in memory. */ }
      roomAssembly = window.VioletRoomAssembly.create(dom.roomAssemblyHost, {
        getProject: () => state.project,
        getContext: () => state.project ? {project_id:state.project.id,source_revision:state.baseRevision,dirty:state.dirty || state.editorDirty || mountPreparationHasUnappliedInputs()} : null,
        request, requestBlob, download:downloadBlob, storage,
        downloadUrl: url => {const link=document.createElement("a");link.href=url;link.download="";link.click();},
        onClose: () => {dom.roomAssemblyDialog.close(); state.planSvg?.focus({preventScroll:true});},
      });
      dom.roomAssemblyDialog.addEventListener("cancel", event => {event.preventDefault(); roomAssembly.close();});
      window.addEventListener("pagehide", () => roomAssembly?.close());
    }
    if (window.VioletBuildingAssembly) {
      let storage = null; try {storage = window.sessionStorage;} catch (_) { /* Jobs still work without session storage. */ }
      buildingAssembly = window.VioletBuildingAssembly.create(dom.buildingAssemblyHost, {
        getProject: () => state.project,
        getContext: () => state.project ? {project_id:state.project.id,source_revision:state.baseRevision,dirty:state.dirty || state.editorDirty || mountPreparationHasUnappliedInputs()} : null,
        request, storage, canCancel:true,
        onClose: () => {dom.buildingAssemblyDialog.close(); state.planSvg?.focus({preventScroll:true});},
      });
      dom.buildingAssemblyDialog.addEventListener("cancel", event => {event.preventDefault(); buildingAssembly.close();});
      window.addEventListener("pagehide", () => buildingAssembly?.close());
    }
    if (window.VioletFinishPresentation) {
      finishPresentation = window.VioletFinishPresentation.create(dom.finishPresentationHost, {
        getProject: () => state.project, request, mutate, download: downloadBlob,
        onClose: () => {dom.finishPresentationDialog.close(); state.planSvg?.focus({preventScroll:true});},
        onTechnical: scope => scope.kind === "space" ? openRoomAssembly(scope, true) : openBuildingAssembly(true, scope),
      });
      dom.finishPresentationDialog.addEventListener("cancel", event => {event.preventDefault(); finishPresentation.close();});
      window.addEventListener("pagehide", () => finishPresentation?.close());
    }
    await Promise.all([loadProjects(true), loadSourceCapabilities()]);
  }

  async function loadProjects(autoOpen) {
    const sequence = ++projectListSequence;
    try {
      const result = await request("/projects"); if (sequence !== projectListSequence) return; state.projects = arrays(result.projects); renderProjectList();
      if (autoOpen && !state.project && state.projects[0]) await selectProject(state.projects[0].id, true);
    } catch (error) { if (sequence !== projectListSequence) return; showError(error, "Не удалось загрузить проекты"); }
  }

  function initializeRecovery() {
    try { if(window.VioletDraftRecovery) recoveryStore=window.VioletDraftRecovery.createStore({storage:window.localStorage,writerId:M.id()}); }
    catch (_error) { recoveryError="Локальное восстановление недоступно. Сохраняйте проект или скачивайте JSON перед закрытием."; }
  }
  function checkpointDraft() {
    if(!state.project || !recoveryStore)return;
    if(!state.dirty){if(recoveryEntry){const removed=recoveryStore.discard(recoveryEntry);if(removed.ok)recoveryEntry=null;}return;}
    // Back up editable inputs, not tens of megabytes of reproducible STEP.
    // Saved framing settings are retained by sourceProject as lightweight data.
    const compact = window.VioletEngineeringJobs?.sourceProject || (project => project);
    const result=recoveryStore.checkpoint(compact(JSON.parse(state.baseJson)),compact(state.project));
    if(result.ok){recoveryEntry=result.entry;recoveryError="";}else recoveryError=result.error;
    renderRecovery();
  }
  function renderRecovery() {
    if(!dom.draftRecovery || !state.project)return;
    const panel=dom.draftRecovery;panel.replaceChildren();
    const result=recoveryStore?.list(JSON.parse(state.baseJson)) || {entries:[],errors:[]};
    const entries=result.entries.filter(entry=>entry.token!==recoveryEntry?.token);
    const errors=[recoveryError,...result.errors].filter(Boolean);panel.classList.toggle("is-hidden",!entries.length&&!errors.length);
    errors.forEach(error=>panel.append(node("p","warning-text",error)));
    entries.forEach(entry=>{
      const row=node("div","draft-recovery-row"),info=node("div");info.append(node("strong","",`Локальный черновик · ${formatDate(entry.savedAt)}`),node("p","form-hint",entry.canRestore?`На основе r${entry.baseRevision}. Восстановление не изменит серверную версию.`:`Черновик от r${entry.baseRevision}, на сервере r${state.baseRevision}. Доступно только скачивание — автоматического объединения нет.`));
      const download=node("button","mini-button","Скачать JSON");download.type="button";download.addEventListener("click",()=>downloadJson(entry.project,`${safeFilename(entry.project.metadata.name)}_recovered.json`));
      const discard=node("button","mini-button","Удалить копию");discard.type="button";let armed=false;discard.addEventListener("click",()=>{if(!armed){armed=true;discard.textContent="Подтвердить удаление копии";return;}const removed=recoveryStore.discard(entry);if(!removed.ok)toast(removed.error,"error");renderRecovery();});
      row.append(info);if(entry.canRestore){const restore=node("button","mini-button","Восстановить в редактор");restore.type="button";restore.disabled=state.dirty||state.editorDirty||hasActiveDraft();restore.addEventListener("click",()=>{
        if(state.dirty||state.editorDirty||hasActiveDraft())return toast("Сначала сохраните текущие правки или скачайте их отдельной копией.","error");
        const ok=mutate("восстановление локального черновика",project=>{Object.keys(project).forEach(key=>delete project[key]);Object.assign(project,M.clone(entry.project));});
        if(ok&&recoveryEntry&&JSON.stringify(recoveryEntry.project)===JSON.stringify(state.project)){const removed=recoveryStore.discard(entry);if(!removed.ok)toast(removed.error,"error");}renderRecovery();
      });row.append(restore);}row.append(download,discard);panel.append(row);
    });
  }

  function renderProjectList() {
    dom.projectList.replaceChildren();
    if (!state.projects.length) { dom.projectList.append(node("p", "sidebar-empty", "Проектов пока нет")); return; }
    state.projects.forEach((project) => {
      const row = node("div", "project-list-row");
      const button = node("button", `project-list-button${state.project && state.project.id === project.id ? " is-active" : ""}`);
      button.type = "button"; const name = node("strong", "", project.name || "Без названия"); const meta = node("span", "", `r${project.revision} · ${formatDate(project.updated_at)}`);
      button.append(name, meta); button.addEventListener("click", () => selectProject(project.id));
      const remove = node("button", "project-delete-button", "Удалить"); remove.type = "button"; remove.setAttribute("aria-label", `Удалить проект «${project.name || "Без названия"}»`);
      remove.addEventListener("click", () => openProjectDeletion(project, remove)); row.append(button, remove); dom.projectList.append(row);
    });
  }

  function openProjectDeletion(project, trigger) {
    if (deleteRequest || state.loading || trashBusyId) return;
    const active = state.project?.id === project.id;
    const hasDraft = active && (state.dirty || state.editorDirty || hasActiveDraft());
    deleteRequest = {id:project.id, name:project.name || "Без названия", revision:active ? state.baseRevision : project.revision,
      hasDraft, trigger, busy:false, conflict:false};
    dom.deleteProjectName.textContent = deleteRequest.name;
    dom.deleteProjectDraftWarning.classList.toggle("is-hidden", !hasDraft);
    dom.deleteProjectDraftAcknowledgement.checked = false;
    clearInlineError(dom.deleteProjectError); syncProjectDeleteButtons();
    dom.deleteProjectDialog.showModal(); dom.cancelDeleteProjectButton.focus();
  }
  function syncProjectDeleteButtons() {
    const task = deleteRequest, busyNow = Boolean(task?.busy);
    dom.confirmDeleteProjectButton.disabled = !task || busyNow || task.conflict || (task.hasDraft && !dom.deleteProjectDraftAcknowledgement.checked);
    dom.confirmDeleteProjectButton.textContent = busyNow ? "Перемещаем…" : "Переместить в корзину";
    dom.cancelDeleteProjectButton.disabled = busyNow; dom.closeDeleteProjectButton.disabled = busyNow;
    dom.downloadDeletingDraftButton.disabled = busyNow; dom.deleteProjectDraftAcknowledgement.disabled = busyNow;
    dom.deleteProjectDialog.setAttribute("aria-busy", String(busyNow));
  }
  function cancelProjectDeletion() {
    if (deleteRequest?.busy) return;
    const trigger = deleteRequest?.trigger; deleteRequest = null; dom.deleteProjectDialog.close();
    (trigger?.isConnected ? trigger : dom.toggleProjectsButton).focus();
  }
  function downloadDeletingDraft() {
    if (!deleteRequest || deleteRequest.busy || state.project?.id !== deleteRequest.id) return;
    if (state.editorDirty) downloadBlob(new Blob([dom.jsonEditor.value], {type:"application/json"}), `${safeFilename(deleteRequest.name)}_unapplied.json`);
    else downloadJson(state.project, `${safeFilename(deleteRequest.name)}_draft.json`);
  }
  async function deleteProject(event) {
    event.preventDefault(); const task = deleteRequest;
    if (!task || task.busy || task.conflict || (task.hasDraft && !dom.deleteProjectDraftAcknowledgement.checked)) return;
    task.busy = true; syncProjectDeleteButtons(); clearInlineError(dom.deleteProjectError);
    // Pending list/open responses must not resurrect a project after deletion.
    projectListSequence++; projectSelectSequence++;
    try {
      await request(`/projects/${encodeURIComponent(task.id)}`, {method:"DELETE", body:{expected_revision:task.revision}});
      if (deleteRequest !== task) return;
      // Keep local recovery copies, including other tabs' work. After restoration
      // an older-revision copy remains downloadable instead of being overwritten.
      if (state.project?.id === task.id) clearDeletedProject();
      state.projects = state.projects.filter(project => project.id !== task.id); renderProjectList();
      task.busy = false; cancelProjectDeletion(); dom.projectTrashButton.focus();
      toast(`«${task.name}» в корзине. Проект можно восстановить.`, "success"); await loadProjects(false);
    } catch (error) {
      if (deleteRequest !== task) return;
      task.busy = false; task.conflict = error.status === 409 || error.status === 404;
      inlineError(dom.deleteProjectError, task.conflict ? new Error("Проект изменился или уже удалён в другом окне. Закройте это окно и обновите список проектов. Ваш черновик не изменён.") : error, "Не удалось удалить проект");
      syncProjectDeleteButtons(); dom.cancelDeleteProjectButton.focus();
    }
  }
  function clearDeletedProject() {
    modelChangeSerial++; lastMountPreparation = null;
    clearTimeout(state.validateTimer); state.validateTimer = null;
    [wallMount, batchMount, mountSections, mountAssemblies, specialAssemblies].forEach(component => component?.close({force:true}));
    resetSourceWorkspace(); clearToolDrafts(); recoveryEntry = null; recoveryError = "";
    Object.assign(state, {project:null, validation:null, baseRevision:0, baseJson:"", dirty:false, editorDirty:false,
      selectedFloorId:null, selection:null, undo:[], redo:[], dragging:null, view:null, viewFloorId:null, tool:"select",
      sheetContext:null, engineeringSheets:[], selectedSheetNumber:null, engineeringStale:true, engineeringSequence:(state.engineeringSequence || 0) + 1});
    baseSnapshot = null; clearJsonEditor(); dom.draftRecovery.replaceChildren(); dom.draftRecovery.classList.add("is-hidden");
    dom.conflictBanner.classList.add("is-hidden"); dom.sheetList.replaceChildren(); dom.sheetPreview.replaceChildren();
    renderEmpty(); renderProjectList();
  }
  async function openProjectTrash() {
    if (deleteRequest || state.loading || trashBusyId) return;
    clearInlineError(dom.projectTrashError); dom.projectTrashDialog.showModal(); dom.closeProjectTrashButton.focus(); await loadProjectTrash();
  }
  async function loadProjectTrash() {
    if (trashBusyId || !dom.projectTrashDialog.open) return;
    cancelEmptyProjectTrash(); trashProjects = []; dom.emptyProjectTrashButton.disabled = true;
    const sequence = ++trashSequence; dom.refreshTrashButton.disabled = true; clearInlineError(dom.projectTrashError);
    dom.projectTrashList.replaceChildren(node("p", "muted", "Загрузка корзины…"));
    try {
      const result = await request("/projects/trash"); if (sequence !== trashSequence || !dom.projectTrashDialog.open) return;
      trashProjects = arrays(result.projects).map(project => ({...project}));
      dom.projectTrashList.replaceChildren();
      arrays(result.projects).forEach(project => {
        const row = node("div", "project-trash-row"), info = node("div", "project-trash-info");
        info.append(node("strong", "", project.name || "Без названия"), node("p", "form-hint", `r${project.revision} · Удалён ${formatDate(project.deleted_at)}`));
        const restore = node("button", "button button-secondary", "Восстановить"); restore.type = "button";
        restore.setAttribute("aria-label", `Восстановить проект «${project.name || "Без названия"}»`); restore.addEventListener("click", () => undeleteProject(project));
        row.append(info, restore); dom.projectTrashList.append(row);
      });
      if (!arrays(result.projects).length) dom.projectTrashList.append(node("p", "muted", "Корзина пуста"));
      dom.emptyProjectTrashButton.disabled = !trashProjects.length;
    } catch (error) { if (sequence === trashSequence && dom.projectTrashDialog.open) { dom.projectTrashList.replaceChildren(); inlineError(dom.projectTrashError, error, "Не удалось загрузить корзину"); } }
    finally { if (sequence === trashSequence) dom.refreshTrashButton.disabled = false; }
  }
  async function undeleteProject(project) {
    if (trashBusyId || emptyTrashRequest || !dom.projectTrashDialog.open) return;
    trashBusyId = project.id; trashSequence++; clearInlineError(dom.projectTrashError);
    dom.projectTrashDialog.setAttribute("aria-busy", "true"); dom.closeProjectTrashButton.disabled = true; dom.refreshTrashButton.disabled = true;
    dom.emptyProjectTrashButton.disabled = true;
    dom.projectTrashList.querySelectorAll("button").forEach(button => {button.disabled = true;});
    try {
      await request(`/projects/${encodeURIComponent(project.id)}/undelete`, {method:"POST", body:{expected_revision:project.revision}});
      // Restoration deliberately does not open the project or replace the current draft.
      toast(`«${project.name || "Без названия"}» восстановлен. Найдите его в списке проектов.`, "success");
      await loadProjects(false); trashBusyId = null; await loadProjectTrash();
    } catch (error) { inlineError(dom.projectTrashError, error.status === 409 ? new Error("Проект уже изменился. Обновите корзину и попробуйте снова.") : error, "Не удалось восстановить проект"); }
    finally {
      trashBusyId = null; dom.projectTrashDialog.setAttribute("aria-busy", "false"); dom.closeProjectTrashButton.disabled = false; dom.refreshTrashButton.disabled = false;
      dom.projectTrashList.querySelectorAll("button").forEach(button => {button.disabled = false;}); dom.closeProjectTrashButton.focus();
      dom.emptyProjectTrashButton.disabled = !trashProjects.length;
    }
  }

  function beginEmptyProjectTrash() {
    if (trashBusyId || !dom.projectTrashDialog.open || !trashProjects.length) return;
    emptyTrashRequest = trashProjects.map(({id, revision}) => ({id, revision}));
    clearInlineError(dom.projectTrashError);
    dom.emptyProjectTrashCount.textContent = `Будут удалены проекты: ${trashProjects.length}. Вместе с ними удалятся сохранённые версии, история и рассчитанные 3D-сборки.`;
    dom.emptyProjectTrashConfirmation.classList.remove("is-hidden");
    dom.emptyProjectTrashButton.disabled = true;
    dom.projectTrashList.querySelectorAll("button").forEach(button => {button.disabled = true;});
    dom.cancelEmptyProjectTrashButton.focus();
  }
  function cancelEmptyProjectTrash() {
    if (trashBusyId) return;
    emptyTrashRequest = null;
    dom.emptyProjectTrashConfirmation.classList.add("is-hidden");
    dom.projectTrashList.querySelectorAll("button").forEach(button => {button.disabled = false;});
    dom.emptyProjectTrashButton.disabled = !trashProjects.length;
  }
  async function emptyProjectTrash() {
    if (trashBusyId || !emptyTrashRequest || !dom.projectTrashDialog.open) return;
    const projects = emptyTrashRequest.map(project => ({...project}));
    trashBusyId = "empty-trash"; trashSequence++; clearInlineError(dom.projectTrashError);
    dom.projectTrashDialog.setAttribute("aria-busy", "true");
    [dom.closeProjectTrashButton, dom.refreshTrashButton, dom.emptyProjectTrashButton, dom.cancelEmptyProjectTrashButton, dom.confirmEmptyProjectTrashButton].forEach(button => {button.disabled = true;});
    dom.confirmEmptyProjectTrashButton.textContent = "Очищаем…";
    try {
      const result = await request("/projects/trash", {method:"DELETE", body:{confirmation:"empty-project-trash", projects}});
      trashProjects = []; trashBusyId = null; cancelEmptyProjectTrash();
      toast(`Корзина очищена. Удалено проектов: ${result.deleted_count}. Восстановить их из корзины нельзя.`, "success");
      arrays(result.cache_cleanup?.warnings).forEach(message => toast(message, "warning", 8000));
      await loadProjectTrash();
    } catch (error) {
      if (error.status === 409) { trashBusyId = null; cancelEmptyProjectTrash(); trashProjects = []; }
      inlineError(dom.projectTrashError, error.status === 409 ? new Error("Корзина изменилась в другом окне. Нажмите «Обновить корзину» и подтвердите очистку заново.") : error, "Не удалось очистить корзину");
    } finally {
      trashBusyId = null; dom.projectTrashDialog.setAttribute("aria-busy", "false");
      [dom.closeProjectTrashButton, dom.refreshTrashButton, dom.cancelEmptyProjectTrashButton, dom.confirmEmptyProjectTrashButton].forEach(button => {button.disabled = false;});
      dom.confirmEmptyProjectTrashButton.textContent = "Удалить навсегда";
      dom.emptyProjectTrashButton.disabled = !trashProjects.length || Boolean(emptyTrashRequest);
      dom.closeProjectTrashButton.focus();
    }
  }

  function hasActiveDraft() { return hasToolDraft() || Boolean(state.dragging && state.dragging.before) || state.sourceDraftPoints.length>0 || state.calibrationClicks.length>0 || Boolean(stepViewer?.hasDraft?.()) || Boolean(wallMount?.hasDraft?.()) || Boolean(batchMount?.hasDraft?.()) || Boolean(mountSections?.hasDraft?.()) || Boolean(mountAssemblies?.hasDraft?.()) || Boolean(specialAssemblies?.hasDraft?.()) || Boolean(wholeWall?.hasDraft?.()); }
  function canLeave() { return !(state.dirty || state.editorDirty || hasActiveDraft()) || window.confirm("В текущем проекте есть несохранённые изменения или незавершённое построение. Перейти и потерять их?"); }
  async function selectProject(id, force = false) {
    if (deleteRequest || trashBusyId) return;
    if (!force && state.project && state.project.id === id) return;
    if (!force && !canLeave()) return;
    const sequence = ++projectSelectSequence;
    try { const result = await busy("Загрузка проекта…", () => request(`/projects/${encodeURIComponent(id)}`)); if (sequence === projectSelectSequence) acceptServer(result); }
    catch (error) { if (sequence === projectSelectSequence) showError(error, "Не удалось открыть проект"); }
  }
  function acceptServer(result) {
    modelChangeSerial++; lastMountPreparation = null;
    dom.mountPreparationStatus?.classList.add("is-hidden");
    wallMount?.close({force:true});
    batchMount?.close({force:true});
    mountSections?.close({force:true});
    mountAssemblies?.close({force:true});
    specialAssemblies?.close({force:true});
    wholeWall?.close({force:true});
    roomAssembly?.close();
    buildingAssembly?.close();
    recoveryEntry=null;
    const changedProject=state.project?.id !== result.project.id;
    const retainedFloor=!changedProject && M.findFloor(result.project,state.selectedFloorId)?.floor;
    if(changedProject || (state.selectedFloorId && !retainedFloor)) resetSourceWorkspace();
    state.project = H ? H.clone(result.project) : M.clone(result.project); state.validation = result.validation || null; state.baseRevision = state.project.revision; setProjectBaseline(state.project);
    state.dirty = false; state.editorDirty = false; state.undo = []; state.redo = []; state.selection = null; state.dragging = null; state.polygon = null; state.rectangle = null; state.guideDraft = null; state.measureDraft = null; state.snapVisual = null; state.view = null; state.viewFloorId = null; state.engineeringStale = true; state.tool = "select";
    state.partitionDraft = null; state.openingPreview = null; document.body.classList.remove("projects-open"); syncPanelButtons();
    const floors = M.floorEntries(state.project); state.selectedFloorId = retainedFloor ? retainedFloor.id : floors[0] ? floors[0].floor.id : null;
    dom.conflictBanner.classList.add("is-hidden"); dom.drawingBar.classList.add("is-hidden"); renderAll(); renderRecovery(); setTool("select"); loadProjects(false);
    backgroundBuilds?.watch(result.engineering, {included: result.assemblies_included === true});
  }
  function resetSourceWorkspace() {
    sourceLoadSequence++;cadViewSequence++;stepViewer?.dispose();stepViewer=null;
    state.source=null;state.sourceSelection=[];state.sourceDraftPoints=[];state.calibrationClicks=[];state.sourceRegionPoints=[];state.sourceOverlay=null;state.sourceDrawing=null;state.sourceViewBox=null;state.sourceFitBox=null;state.sourcePan=null;
    dom.stepWorkspace.replaceChildren();dom.stepWorkspace.classList.add("is-hidden");dom.sourcesPanel.classList.remove("is-step");dom.attachStepButton.classList.add("is-hidden");dom.detachStepButton.classList.add("is-hidden");detachStepRequest=null;dom.cadRegionTools.classList.add("is-hidden");
    dom.sourcePreview.replaceChildren(node("p","","Выберите исходник для этого проекта."));dom.sourceInspectionResult.replaceChildren();dom.markupPoints.value="";dom.sourcePageSelect.replaceChildren();dom.sourcePageSelect.disabled=true;renderSourceSelection();
  }
  function renderEmpty() {
    const active = Boolean(state.project); dom.emptyWorkspace.classList.toggle("is-hidden", active); dom.projectWorkspace.classList.toggle("is-hidden", !active);
  }
  function renderAll() {
    renderEmpty(); if (!state.project) return;
    dom.projectTitle.textContent = state.project.metadata.name; dom.projectAddress.textContent = state.project.metadata.address || "Адрес не указан"; dom.revisionBadge.textContent = `r${state.baseRevision}`;
    renderDraftState(); renderFloorSelect(); renderSourceFloorSelect(); renderStats(); renderTree(); renderPlan(); renderInspector(); renderValidation(); renderAttachedSources(); renderMarkupList(); paintSourceOverlay();
    if (!state.editorDirty) syncJsonEditor();
    if (state.activeTab === "library") vendorLibrary?.refreshProject();
    modelLibrary?.refreshProject();
    renderStepAttachment();
    wallMount?.refresh();
    batchMount?.refresh();
    mountSections?.refresh();
    mountAssemblies?.refresh();
    specialAssemblies?.refresh();
    wholeWall?.refresh();
    roomAssembly?.refresh();
    buildingAssembly?.refresh();
    finishPresentation?.refresh();
    if(isStepSource()){stepViewer?.refreshContexts(cadSectionContexts());stepViewer?.refreshSavedSections(arrays(state.project?.properties?.cad_assets).find(a=>a.source_id===state.source.id)?.sections || []);}
  }
  function renderDraftState() {
    dom.dirtyBadge.classList.toggle("is-hidden", !state.dirty); dom.saveProjectButton.disabled = saveInFlight || !state.dirty || state.editorDirty;
    dom.saveProjectButton.textContent = saveInFlight ? "Сохраняем…" : "Проверить и сохранить";
    if (dom.retryAssembliesButton) dom.retryAssembliesButton.disabled = state.dirty || saveInFlight;
    dom.undoButton.disabled = !state.undo.length; dom.redoButton.disabled = !state.redo.length;
    dom.documentsDirtyWarning.classList.toggle("is-hidden", !state.dirty);
    if (state.editorDirty) { dom.editorStatus.textContent = "Текст JSON ещё не применён к черновику"; dom.editorStatus.className = "editor-status is-dirty"; }
    else { dom.editorStatus.textContent = state.dirty ? "Совпадает с локальным черновиком" : "Совпадает с серверной версией"; dom.editorStatus.className = "editor-status"; }
  }

  function openNewProject() { clearInlineError(dom.newProjectFormError); dom.newProjectForm.reset(); dom.newProjectDialog.showModal(); setTimeout(() => dom.newProjectForm.elements.name.focus(), 0); }
  async function createProject(event) {
    event.preventDefault(); if (event.submitter?.value === "cancel") { dom.newProjectDialog.close(); return; } clearInlineError(dom.newProjectFormError); const data = new FormData(dom.newProjectForm);
    if (!canLeave()) return;
    try { const result = await busy("Создание проекта…", () => request("/projects", { method: "POST", body: { name: data.get("name"), address: data.get("address"), author: data.get("author") } })); dom.newProjectDialog.close(); acceptServer(result); toast("Проект создан", "success"); }
    catch (error) { inlineError(dom.newProjectFormError, error, "Не удалось создать проект"); }
  }
  async function createDemo() {
    if (!canLeave()) return;
    try { const result = await busy("Создание демо…", () => request("/demo", { method: "POST", body: {} })); acceptServer(result); toast("Создан отдельный учебный проект", "success"); }
    catch (error) { showError(error, "Не удалось создать демо"); }
  }
  async function importProject(event) {
    const file = event.target.files[0]; event.target.value = ""; if (!file || !canLeave()) return;
    try { const documentValue = JSON.parse(await file.text()); const result = await busy("Импорт копии…", () => request("/import", { method: "POST", body: { project: documentValue } })); acceptServer(result); toast("Импортирована новая копия проекта", "success"); }
    catch (error) { showError(error, "Не удалось импортировать JSON"); }
  }

  function snapshot() { return H ? H.capture(state.project) : JSON.stringify(state.project); }
  function restoreSnapshot(value) { return H ? H.restore(value) : JSON.parse(value); }
  function sameSnapshot(a, b) { return H ? H.equal(a, b) : a === b; }
  function setProjectBaseline(project) { baseSnapshot = H ? H.capture(project) : null; state.baseJson = baseSnapshot ? baseSnapshot.json : JSON.stringify(project); }
  function projectIsDirty() { return baseSnapshot ? !sameSnapshot(snapshot(), baseSnapshot) : JSON.stringify(state.project) !== state.baseJson; }
  function mutate(label, operation, options = {}) {
    if (options.mergeIntoLastUndo && (options.expectedChangeSerial !== modelChangeSerial || !state.undo.length)) {
      if (options.rethrow) throw new Error("Черновик изменился. Автоматическая сборка не добавлена."); return false;
    }
    const before = snapshot();
    try { operation(state.project); }
    catch (error) { state.project = restoreSnapshot(before); normalizeSelection(); renderAll(); if (options.rethrow) throw error; showError(error, "Правка не применена"); return false; }
    const after = snapshot(); if (sameSnapshot(before, after)) return false;
    if (!options.mergeIntoLastUndo) {state.undo.push({ label, json: before }); if (state.undo.length > 80) state.undo.shift();} state.redo = []; state.dirty = projectIsDirty(); state.editorDirty = false; markEngineeringStale();
    syncJsonEditor(); renderAll(); checkpointDraft(); scheduleValidation(); return true;
  }
  function undo() {
    if (state.dragging && ["corner", "room"].includes(state.dragging.kind)) { cancelLiveGeometry(); return; }
    if (hasToolDraft()) { toast("Сначала завершите построение Enter или отмените его Esc.", "error"); return; }
    const entry = state.undo.pop(); if (!entry) return; state.redo.push({ label: entry.label, json: snapshot() }); state.project = restoreSnapshot(entry.json);
    state.dirty = projectIsDirty(); state.editorDirty = false; markEngineeringStale(); syncJsonEditor(); normalizeSelection(); renderAll(); checkpointDraft(); scheduleValidation(); toast(`Отменено: ${entry.label}`);
  }
  function redo() {
    if (state.dragging && ["corner", "room"].includes(state.dragging.kind)) { cancelLiveGeometry(); return; }
    if (hasToolDraft()) { toast("Сначала завершите построение Enter или отмените его Esc.", "error"); return; }
    const entry = state.redo.pop(); if (!entry) return; state.undo.push({ label: entry.label, json: snapshot() }); state.project = restoreSnapshot(entry.json);
    state.dirty = projectIsDirty(); state.editorDirty = false; markEngineeringStale(); syncJsonEditor(); normalizeSelection(); renderAll(); checkpointDraft(); scheduleValidation(); toast(`Повторено: ${entry.label}`);
  }
  function normalizeSelection() { if (!M.findFloor(state.project, state.selectedFloorId)) state.selectedFloorId = M.floorEntries(state.project)[0]?.floor.id || null; const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;if(state.selection?.type==="vertex"){if(!floor || !arrays(floor.walls).some(w=>[w.start,w.end].some(p=>Math.hypot(p.x-state.selection.point.x,p.y-state.selection.point.y)<1e-6)))state.selection=null;return;}setSelectionItems(selectionItems().filter(item=>item.type==="guide"?guideCollection(floor).some(g=>g.id===item.id):M.findEntity(state.project,item.id,item.type))); }
  function scheduleValidation() { clearTimeout(state.validateTimer); state.validateTimer = setTimeout(validateDraft, 350); }
  function markEngineeringStale() {
    modelChangeSerial++; lastMountPreparation = null;
    state.engineeringStale = true;
    if (dom.engineeringSummary) setResult(dom.engineeringSummary, "Модель изменена. Показанные расчёты и листы относятся к последней сохранённой версии; обновите их после сохранения.", false);
    if (dom.sheetPreview) dom.sheetPreview.classList.add("is-stale");
  }
  function mountPreparationHasUnappliedInputs() {
    const form = dom.inspectorContent?.querySelector("form"), changed = form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)));
    return state.editorDirty || hasActiveDraft() || changed;
  }
  function mountPreparationStatus(message, phase = "working") {
    const element = dom.mountPreparationStatus; if (!element) return;
    element.textContent = message; element.classList.remove("is-hidden"); element.setAttribute("data-status", phase); element.setAttribute("aria-busy", String(phase === "working"));
  }
  function getPendingMountPreparation() {return mountPreparationTask?.promise || null;}
  function deferMountPreparation(onStatus = () => {}) {
    const message = "Монтаж применён к черновику. Нажмите «Сохранить»: проект сохранится сразу, узлы и стены рассчитаются в фоне.";
    mountPreparationStatus(message, "warning"); onStatus(message, "warning");
    return {status: "deferred"};
  }
  async function prepareMountAssembliesForProject({scope = null, mergeIntoLastUndo = false, onStatus = () => {}, force = false} = {}) {
    const A = window.VioletMountAssemblies, project = state.project;
    if (!project || !arrays(project.properties?.wall_mounting?.walls).length) return {status: "unchanged", ready: 0, warnings: [], skipped: []};
    if (!A?.preparedAssemblies) return {status: "error", ready: 0, message: "Модуль STEP-сборок не загрузился. Обновите страницу."};
    const serial = modelChangeSerial, source = H ? H.clone(project) : M.clone(project), sourceKey = snapshot(), scopeKey = JSON.stringify(scope);
    if (mountPreparationTask?.project === project && mountPreparationTask.serial === serial && sameSnapshot(mountPreparationTask.sourceKey, sourceKey)) {
      if (mountPreparationTask.scopeKey === scopeKey || mountPreparationTask.scopeKey === "null") {mountPreparationTask.listeners.add(onStatus); onStatus(mountPreparationTask.message, "working"); return mountPreparationTask.promise;}
      await mountPreparationTask.promise; return prepareMountAssembliesForProject({scope, mergeIntoLastUndo, onStatus, force});
    }
    if (!force && lastMountPreparation?.serial === serial && sameSnapshot(lastMountPreparation.sourceKey, sourceKey) && (lastMountPreparation.scopeKey === scopeKey || lastMountPreparation.scopeKey === "null")) {onStatus(lastMountPreparation.message, lastMountPreparation.phase); return lastMountPreparation.result;}
    const task = {project, serial, sourceKey, scopeKey, listeners: new Set([onStatus]), message: "Монтаж применён. Автоматически собираем STEP-узлы и сечения для документации…", promise: null};
    const current = () => state.project === project && modelChangeSerial === serial && sameSnapshot(snapshot(), sourceKey) && !mountPreparationHasUnappliedInputs();
    const notify = (message, phase) => {task.message = message; if (mountPreparationTask === task && state.project?.id === task.project.id) mountPreparationStatus(message, phase); task.listeners.forEach(listener => listener(message, phase));};
    mountPreparationTask = task; notify(task.message, "working");
    task.promise = (async () => {
      try {
        // Native operations remain bounded per request. Continue an explicitly
        // resumable pass locally, then apply the whole result as one undo step.
        // The live draft is never replaced while a later pass is still running.
        const working = H ? H.clone(source) : M.clone(source); let response, prepared, totalReady = 0, initialReused = 0;
        for (let pass = 0; pass < 4; pass++) {
          const preparingProject = window.VioletWallMount.requestProject ? window.VioletWallMount.requestProject(working, {keepLocalAssemblies:true}) : working;
          response = await request("/wall-mount/assembly-prepare", {method: "POST", body: {project: H ? H.clone(preparingProject) : M.clone(preparingProject), ...(scope ? {scope} : {})}});
          if (!current()) {const message = "Черновик изменился во время сборки. Поздний результат не добавлен; повторите подготовку узлов перед экспортом."; notify(message, "stale"); return {status: "stale", ready: 0, message};}
          prepared = A.preparedAssemblies(working, response);
          if (!pass) initialReused = Number(response.reused) || 0;
          totalReady += Number(response.ready) || 0;
          const previousIds = new Set(A.collection(working).items.map(item => item.id));
          const madeProgress = prepared.items.some(item => !previousIds.has(item.id));
          working.properties ||= {}; working.properties.mount_assemblies = prepared;
          if (response.continuation_required !== true || !madeProgress || pass === 3) break;
          notify(`Собрано новых узлов: ${totalReady}. Проверяем оставшиеся посадки и пересечения…`, "working");
        }
        response = {...response, ready: totalReady, reused: initialReused};
        const previous = A.collection(source);
        let changed = false;
        if (!window.VioletWallMount.equal(prepared, previous)) changed = mutate("автоматическая сборка монтажных узлов", target => {
          if (!current()) throw new Error("Черновик изменился. Повторите автоматическую сборку."); target.properties ||= {}; target.properties.mount_assemblies = prepared;
        }, {mergeIntoLastUndo, expectedChangeSerial: serial, rethrow: true});
        const skipped = arrays(response.skipped), failed = arrays(response.outcomes).filter(item => !["ready", "reused"].includes(item.status)), warnings = arrays(response.warnings);
        const reasons = [...skipped, ...failed].map(item => item.message || item.reason).filter(Boolean).slice(0, 3), incomplete = skipped.length + failed.length, message = `STEP-сборки: добавлено ${Number(response.ready) || 0}, уже готово ${Number(response.reused) || 0}.${incomplete ? ` Не собрано вариантов: ${incomplete}. ${reasons.join(" ")} Подробности — в «Сборках узлов STEP».` : " Сечения готовы для документации."}${warnings.length ? ` Примечаний: ${warnings.length}.` : ""}`;
        const result = {...response, status: changed ? "applied" : "unchanged"}, phase = incomplete || warnings.length ? "warning" : "ready";
        lastMountPreparation = failed.length ? null : {serial: modelChangeSerial, sourceKey: snapshot(), scopeKey, result, phase, message}; notify(message, phase); return result;
      } catch (error) {const message = `Автоматическая сборка не завершена: ${errorText(error)}. Монтаж стен сохранён в черновике; повторите подготовку перед экспортом.`; notify(message, "error"); return {status: "error", ready: 0, message};}
      finally {if (mountPreparationTask === task) mountPreparationTask = null;}
    })();
    return task.promise;
  }
  async function validateDraft() {
    if (!state.project) return null;
    const validatingJson = snapshot(), current = () => sameSnapshot(snapshot(), validatingJson);
    const source = window.VioletEngineeringJobs ? window.VioletEngineeringJobs.sourceProject(state.project) : state.project;
    try { const report = await request("/validate", { method: "POST", body: { project: source } }); if (!current()) return null; state.validation = report; renderValidation(); renderStats(); return report; }
    catch (error) { if (!current()) return null; state.validation = { valid: false, issues: [{ severity: "error", code: error.code || "SCHEMA", message: error.message, path: "project" }] }; renderValidation(); return state.validation; }
  }
  async function saveDraft() {
    if (!state.project || !state.dirty || saveInFlight) return;
    if(batchMount?.hasDraft?.()){toast("Сначала примените настройки массового монтажа или сбросьте их в окне монтажа.","error");return;}
    if(mountSections?.hasDraft?.()){toast("Сначала добавьте построенные STEP-сечения в проект или закройте их окно без добавления.","error");return;}
    if(mountAssemblies?.hasDraft?.()){toast("Сначала добавьте построенные STEP-сборки в проект или закройте их окно без добавления.","error");return;}
    if(specialAssemblies?.hasDraft?.()){toast("Сначала добавьте изделия в проект или закройте окно дверей, люков и подсветки.","error");return;}
    if(wholeWall?.hasDraft?.()){toast("Сначала добавьте сборки стен в проект или закройте окно сборки без добавления.","error");return;}
    if(wallMount?.hasDraft?.()){toast("Сначала примените настройки монтажа к стене или сбросьте их в окне монтажа.","error");return;}
    if(stepViewer?.hasDraft?.()){setTab("sources");toast("Сначала сохраните правки сечения в проект или сбросьте их в редакторе сечения.","error");return;}
    if(state.sourceDraftPoints.length || state.calibrationClicks.length) {setTab("sources");toast("Сначала добавьте текущую отметку или сохраните калибровку. Esc отменяет незавершённую обводку.","error",8000);return;}
    if (hasActiveDraft()) { setTab("model"); toast("Сначала завершите построение клавишей Enter или отмените его клавишей Esc.", "error", 8000); return; }
    if (state.editorDirty) { setTab("json"); toast("Сначала примените или отмените изменения в тексте JSON.", "error"); return; }
    if (mountPreparationHasUnappliedInputs()) {toast("Сначала примените или сбросьте изменённые параметры в панели свойств, затем сохраните проект.", "error", 10000); return;}
    const savingProject=M.clone(window.VioletEngineeringJobs ? window.VioletEngineeringJobs.sourceProject(state.project) : state.project),savingJson=snapshot(),savingRevision=state.baseRevision,savingRecovery=recoveryEntry,savingUndo=state.undo;
    saveInFlight = true; clearTimeout(state.validateTimer); renderDraftState();
    try {
      // The server validates source geometry once. STEP generation and collision
      // checks are a durable background job, never a prerequisite for saving.
      const result = await request(`/projects/${encodeURIComponent(savingProject.id)}`, { method: "PUT", body: { project: savingProject, expected_revision: savingRevision, defer_assemblies: true, message: "Изменения из локального редактора" } });
      if(savingRecovery)recoveryStore?.discard(savingRecovery);
      if(!sameSnapshot(snapshot(),savingJson) || state.baseRevision!==savingRevision || state.undo!==savingUndo || mountPreparationHasUnappliedInputs()) {
        const canAdvance = state.project?.id === savingProject.id && result.project?.id === savingProject.id && state.baseRevision === savingRevision && state.undo === savingUndo && Number.isInteger(result.project?.revision) && result.project.revision === savingRevision + 1;
        if (canAdvance) {
          // Acknowledge only the completed version. Do not replace newer local
          // geometry, inspector fields, JSON text or its recovery token.
          state.project.revision = result.project.revision; state.baseRevision = result.project.revision; setProjectBaseline(result.project); state.dirty = true;
          markEngineeringStale(); if (!state.editorDirty) syncJsonEditor(); if (dom.revisionBadge) dom.revisionBadge.textContent = `r${state.baseRevision}`; renderDraftState(); checkpointDraft();
        }
        toast(canAdvance ? "Предыдущие правки сохранены. Более новые изменения остались в черновике — нажмите «Сохранить» ещё раз." : "Предыдущая версия сохранена, но текущий проект или его серверная версия уже изменились. Текущий черновик не заменён.", "error", 12000); return;
      }
      if(recoveryStore)recoveryError="";
      acceptServer(result);toast("Проект сохранён. Сборки рассчитываются отдельно в фоне.", "success");
    } catch (error) {
      if (error.status === 409 && state.project?.id === savingProject.id) { dom.conflictMessage.textContent = error.message || "Серверная версия новее. Черновик не потерян."; dom.conflictBanner.classList.remove("is-hidden"); }
      showError(error, "Не удалось сохранить проект");
    } finally {saveInFlight = false; renderDraftState();}
  }
  async function reloadRemote() { if (!window.confirm("Заменить локальный черновик серверной версией? Сначала можно скачать черновик.")) return; await selectProject(state.project.id, true); }

  function downloadJson(value, filename) { downloadBlob(new Blob([json(value)], { type: "application/json" }), filename); }
  function downloadBlob(blob, filename) { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 2000); }
  function formatDate(value) { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }); }

  function setTab(tab) {
    state.activeTab=tab;
    document.querySelectorAll(".tab[data-tab]").forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle("is-active", active); button.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll(".tab-panel[data-panel]").forEach((panel) => panel.classList.toggle("is-hidden", panel.dataset.panel !== tab));
    if (tab === "engineering") loadEngineering(); if (tab === "documents") dom.documentsDirtyWarning.classList.toggle("is-hidden", !state.dirty);
    if (tab === "library") vendorLibrary?.load();
    if (tab === "json" && !state.editorDirty) syncJsonEditor();
    else if (tab !== "json" && !state.editorDirty) clearJsonEditor();
    if (tab === "sources") {loadCadRecent();if(!modelLibrary)modelLibrary=CW.mountLibrary(dom.modelLibraryHost,{request,getAttachedIds:()=>arrays(state.project?.properties?.cad_assets).map(a=>a.source_id),open:openLibraryAsset});else modelLibrary.refreshProject();}
  }

  function renderFloorSelect() {
    const entries = M.floorEntries(state.project); dom.floorSelect.replaceChildren();
    dom.floorSelect.disabled = !entries.length;
    if (!entries.length) { const option = node("option", "", "Нет этажей"); option.value = ""; dom.floorSelect.append(option); state.selectedFloorId = null; return; }
    if (!entries.some((entry) => entry.floor.id === state.selectedFloorId)) state.selectedFloorId = entries[0].floor.id;
    entries.forEach(({ building, floor }) => { const option = node("option", "", `${building.name} · ${floor.name}`); option.value = floor.id; option.selected = floor.id === state.selectedFloorId; dom.floorSelect.append(option); });
  }
  function renderSourceFloorSelect() {dom.sourceFloorSelect.replaceChildren();const entries=M.floorEntries(state.project);if(!entries.length){const empty=node("option","","Сначала добавьте этаж на вкладке «Чертёж»");empty.value="";dom.sourceFloorSelect.append(empty);}for(const entry of entries){const option=node("option","",`${entry.building.name} · ${entry.floor.name}`);option.value=entry.floor.id;option.selected=entry.floor.id===state.selectedFloorId;dom.sourceFloorSelect.append(option);}}
  function selectFloor(id) { if (hasActiveDraft() && !window.confirm("Сменить этаж и отменить незавершённое построение или обводку?")) { renderFloorSelect();renderSourceFloorSelect(); return; } state.sourceDraftPoints=[];state.calibrationClicks=[];state.sourceSelection=[];dom.markupPoints.value="";state.selectedFloorId = id || null; state.selection = id ? { type: "floor", id } : null; state.view = null; state.viewFloorId = null; activateTool("select", true); renderAll(); if(state.source) {renderSourceInspection();renderSourcePreview();}renderSourceSelection(); }

  function counts() {
    const result = { buildings: arrays(state.project.buildings).length, floors: 0, rooms: 0, walls: 0, openings: 0, zones: 0 };
    M.floorEntries(state.project).forEach(({ floor }) => { result.floors += 1; result.rooms += arrays(floor.spaces).length; result.walls += arrays(floor.walls).length; result.openings += arrays(floor.openings).length; result.zones += arrays(floor.cladding_zones).length; });
    return result;
  }
  function renderStats() {
    const c = counts(); const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; const area = floor ? arrays(floor.spaces).reduce((sum, room) => sum + M.area(M.roomPolygon(floor, room) || []), 0) / 1e6 : 0;
    const items = [["Здания", c.buildings], ["Этажи", c.floors], ["Помещения", c.rooms], ["Стены", c.walls], ["Проёмы", c.openings], ["Площадь этажа", `${af.format(area)} м²`, "по опорным линиям"]];
    dom.statsGrid.replaceChildren(...items.map(([label, value, note]) => { const card = node("div", "stat-card"); card.append(node("span", "stat-label", label), node("strong", "", value)); if (note) card.append(node("small", "", note)); return card; }));
  }

  function treeButton(type, entity, label, detail, floorId = null) {
    const button = node("button", `tree-item tree-${type}${selected(type,entity.id) ? " is-selected" : ""}`); button.type = "button";
    button.append(node("span", "tree-label", label), node("span", "tree-detail", detail || "")); button.addEventListener("click", event => { if(!activateTool("select"))return;const found = type === "guide" ? null : M.findEntity(state.project, entity.id, type),targetFloor=floorId||found?.floor?.id;const additive=event.shiftKey&&(!targetFloor||targetFloor===state.selectedFloorId)&&["room","wall","opening","guide","zone"].includes(type);if(targetFloor)state.selectedFloorId=targetFloor;choose(type,entity.id,additive);renderAll(); }); return button;
  }
  function guideCollection(floor, create = false) {
    if (!floor) return [];
    if (!floor.properties) floor.properties = {};
    if (!floor.properties.editor_guides && create) floor.properties.editor_guides = { schema_version: "editor-guides/1.0", guides: [] };
    return arrays(floor.properties.editor_guides && floor.properties.editor_guides.guides);
  }
  function selectedGuide() { const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; return state.selection?.type === "guide" ? guideCollection(floor).find((guide) => guide.id === state.selection.id) || null : null; }
  function renderTree() {
    dom.hierarchyTree.replaceChildren();
    arrays(state.project.buildings).forEach((building) => {
      const group = node("div", "tree-group"); group.append(treeButton("building", building, building.name, `${arrays(building.floors).length} эт.`));
      arrays(building.floors).forEach((floor) => {
        const floorWrap = node("div", "tree-floor"); floorWrap.append(treeButton("floor", floor, floor.name, `${arrays(floor.spaces).length} пом.`));
        arrays(floor.spaces).forEach((room) => floorWrap.append(treeButton("room", room, `${room.code} · ${room.name}`, `${nf.format(M.area(M.roomPolygon(floor, room) || []) / 1e6)} м²`)));
        arrays(floor.walls).filter((wall) => wall.properties?.editor_kind === "partition").forEach((wall) => floorWrap.append(treeButton("wall", wall, wall.name, "перегородка")));
        arrays(floor.openings).forEach((opening) => floorWrap.append(treeButton("opening", opening, opening.name, ({door:"дверь",window:"окно",passage:"проход",hatch:"люк",custom:"проём"})[opening.kind] || opening.kind)));
        arrays(floor.cladding_zones).forEach((zone) => floorWrap.append(treeButton("zone", zone, zone.name, "зона")));
        guideCollection(floor).forEach((guide) => floorWrap.append(treeButton("guide", guide, guide.name, "направляющая", floor.id)));
        group.append(floorWrap);
      }); dom.hierarchyTree.append(group);
    });
    if (arrays(state.project.panel_systems).length) { const group = node("div", "tree-group"); group.append(node("p", "tree-section", "Панельные системы")); arrays(state.project.panel_systems).forEach((item) => group.append(treeButton("panelSystem", item, item.name, item.status))); dom.hierarchyTree.append(group); }
    if (arrays(state.project.measurements).length) { const group = node("div", "tree-group"); group.append(node("p", "tree-section", "Замеры")); arrays(state.project.measurements).forEach((item) => group.append(treeButton("measurement", item, item.property_name, `${nf.format(item.value_mm)} мм`))); dom.hierarchyTree.append(group); }
    if (!dom.hierarchyTree.children.length) dom.hierarchyTree.append(node("p", "empty-note", "Добавьте здание и этаж."));
  }

  function fitView(floor) {
    const bounds = M.boundsForFloor(floor); const pad = Math.max(800, Math.max(bounds.width, bounds.height) * 0.12);
    return { x: bounds.minX - pad, y: bounds.minY - pad, w: bounds.width + pad * 2, h: bounds.height + pad * 2 };
  }
  // The canonical model is Cartesian (Y points up); SVG screen coordinates
  // point down. Keep the model untouched and reflect only the presentation.
  function viewBoxText() { return `${state.view.x} ${-(state.view.y + state.view.h)} ${state.view.w} ${state.view.h}`; }
  function renderCanvasStart(floor) {
    dom.planCanvas.querySelector(".canvas-start")?.remove();
    if (floor && (state.tool !== "select" || arrays(floor.walls).length || arrays(floor.spaces).length || guideCollection(floor).length || arrays(floor.obstacles).length)) return;
    const wrap = node("div", "canvas-start"), card = node("section", "canvas-start-card");
    card.setAttribute("aria-label", floor ? "Начало работы на этаже" : "Начало работы с проектом");
    const icon = node("div", "canvas-start-icon", floor ? "⌑" : "+"); icon.setAttribute("aria-hidden", "true");
    card.append(icon, node("h2", "", floor ? "Добавьте первое помещение" : "Начните с первого этажа"), node("p", "", floor ? "Нарисуйте комнату на плане или загрузите готовый чертёж." : "Укажите название и высоту помещений. После этого можно рисовать план или импортировать чертёж."));
    const actions = node("div", "canvas-start-actions"), primary = node("button", "button button-primary", floor ? "Нарисовать комнату" : "Добавить первый этаж");
    primary.type = "button"; primary.addEventListener("click", () => floor ? startRectangle() : openEntity("floor")); actions.append(primary);
    if (floor) { const secondary = node("button", "button button-secondary", "Импортировать чертёж"); secondary.type = "button"; secondary.addEventListener("click", () => { setTab("sources"); dom.sourceFileInput.focus(); }); actions.append(secondary); }
    card.append(actions);
    if (!floor && !arrays(state.project?.buildings).length) card.append(node("small", "", "Здание создадим автоматически вместе с этажом."));
    wrap.append(card); dom.planCanvas.append(wrap);
  }
  function renderPlan() {
    const entry = M.findFloor(state.project, state.selectedFloorId);
    if (!entry) { dom.planCanvas.replaceChildren(); state.planSvg = null; dom.planTitle.textContent = "План этажа"; dom.toolHelp.textContent = "Первый шаг — добавьте этаж кнопкой на чертеже."; renderCanvasStart(null); return; }
    const floor = entry.floor; dom.planTitle.textContent = `${floor.name} · опорные линии`;
    if (!state.view || state.viewFloorId !== floor.id) { state.view = fitView(floor); state.viewFloorId = floor.id; }
    // Keep the SVG event target alive throughout a pointer gesture. Replacing it
    // on every move loses capture/click identity and makes completion unreliable.
    const drawing = state.planSvg?.isConnected ? state.planSvg : svg("svg", { role: "img", tabindex: "0" });
    const isNew = drawing !== state.planSvg || !drawing.isConnected; state.planSvg = drawing;
    drawing.replaceChildren(); drawing.setAttribute("class", `floor-plan tool-${state.tool}`); drawing.setAttribute("viewBox", viewBoxText()); drawing.setAttribute("aria-label", `План ${floor.name}`);
    const step = Math.max(1, number(dom.snapInput.value, 100));
    const viewport = dom.planCanvas.getBoundingClientRect(), visible = D.visibleModelBounds(state.view, viewport.width, viewport.height);
    const defs = svg("defs"); const pattern = svg("pattern", { id: "grid", width: step, height: step, patternUnits: "userSpaceOnUse" }); pattern.append(svg("path", { d: `M ${step} 0 L 0 0 0 ${step}`, class: "grid-line" })); defs.append(pattern); drawing.append(defs, svg("rect", { x: visible.x, y: visible.y, width: visible.w, height: visible.h, fill: dom.gridToggle.checked ? "url(#grid)" : "transparent", class: "plan-background" }));
    const geometry = svg("g", { class: "plan-geometry", transform: "scale(1 -1)" }); const labels = svg("g", { class: "plan-labels" }); const background = drawing.lastElementChild; background.remove(); geometry.append(background); drawing.append(geometry, labels);
    arrays(floor.spaces).forEach((room) => {
      const points = M.roomPolygon(floor, room); if (!points || points.length < 3) return;
      const polygon = svg("polygon", { points: points.map((p) => `${p.x},${p.y}`).join(" "), class: `room-shape${selected("room", room.id) ? " is-selected" : ""}`, tabindex: "0" });
      polygon.addEventListener("pointerdown", (event) => { if (state.tool === "select" && event.button === 0 && !event.altKey) startRoomDrag(event, floor, room); }); polygon.addEventListener("click", (event) => selectableClick(event, "room", room.id)); geometry.append(polygon);
      const center = points.reduce((acc, p) => ({ x: acc.x + number(p.x) / points.length, y: acc.y + number(p.y) / points.length }), { x: 0, y: 0 }); const label = svg("text", { x: center.x, y: -center.y, class: "room-label" }); label.textContent = `${room.code} · ${af.format(M.area(points) / 1e6)} м²`; labels.append(label);
    });
    const wallPresentation = D.wallPresentation(floor);
    const wallFill = wallPresentation.pieces.map(piece => `M ${piece.points.map(p => `${p.x},${p.y}`).join(" L ")} Z`).join(" ");
    geometry.append(svg("path", { d: wallFill, class: "wall-envelope", fill: "#cbd3dc", "fill-rule": "nonzero", stroke: "none", "pointer-events": "none" }));
    // Paint all joined bodies before any axis: a later wall must not cover
    // the preceding wall's reference line and create a false end/notch.
    const wallOutline = wallPresentation.edges.map(edge => `M ${edge.start.x},${edge.start.y} L ${edge.end.x},${edge.end.y}`).join(" ");
    geometry.append(svg("path", { d: wallOutline, class: "wall-envelope-outline", fill: "none", stroke: "#75818f", "stroke-width": .8, "vector-effect": "non-scaling-stroke", "pointer-events": "none" }));
    arrays(floor.walls).forEach((wall) => {
      const line = svg("line", { x1: wall.start.x, y1: wall.start.y, x2: wall.end.x, y2: wall.end.y, class: `wall-line${selected("wall", wall.id) ? " is-selected" : ""}`, "stroke-width": 3 });
      line.dataset.wallId = wall.id;
      const hit = svg("line", { x1: wall.start.x, y1: wall.start.y, x2: wall.end.x, y2: wall.end.y, stroke: "transparent", "stroke-width": 16, "vector-effect": "non-scaling-stroke", "pointer-events": "stroke" }); hit.dataset.wallId = wall.id; hit.addEventListener("click", (event) => selectableClick(event, "wall", wall.id)); geometry.append(hit);
      line.addEventListener("click", (event) => selectableClick(event, "wall", wall.id)); geometry.append(line);
      const mounts = arrays(state.project.properties?.wall_mounting?.walls).filter(item => item.wall_id === wall.id);
      const length = M.wallLength(wall);
      if (length > 0) mounts.forEach(mount => {
        const face = arrays(wall.faces).find(item => item.id === mount.face_id); if (!face) return;
        const side = face.side === "left" ? 1 : -1, distance = (number(wall.thickness_mm) / 2 + pixelsToModel(5)) * side;
        const dx = -(wall.end.y-wall.start.y)/length * distance, dy = (wall.end.x-wall.start.x)/length * distance;
        const band = svg("line", {x1:wall.start.x+dx,y1:wall.start.y+dy,x2:wall.end.x+dx,y2:wall.end.y+dy,stroke:"#8b65c9","stroke-width":3,"stroke-dasharray":"6 4","vector-effect":"non-scaling-stroke","pointer-events":"none",class:"wall-mount-band"});
        geometry.append(band);
      });
    });
    arrays(floor.openings).forEach((opening) => {
      const wall = arrays(floor.walls).find((item) => item.id === opening.wall_id); if (!wall) return; const length = M.wallLength(wall); if (!length) return;
      const ux = (wall.end.x - wall.start.x) / length; const uy = (wall.end.y - wall.start.y) / length; const start = number(opening.offset_mm); const end = start + number(opening.width_mm);
      const line = svg("line", { x1: wall.start.x + ux * start, y1: wall.start.y + uy * start, x2: wall.start.x + ux * end, y2: wall.start.y + uy * end, class: `opening-line${selected("opening", opening.id) ? " is-selected" : ""}` });
      line.addEventListener("click", (event) => selectableClick(event, "opening", opening.id)); geometry.append(line);
    });
    guideCollection(floor).forEach((guide) => { const ends = extendedGuide(guide.start, guide.end); const line = svg("line", { x1: ends.start.x, y1: ends.start.y, x2: ends.end.x, y2: ends.end.y, class: `guide-line${selected("guide", guide.id) ? " is-selected" : ""}` }); line.dataset.guideId=guide.id; line.addEventListener("click", (event) => selectableClick(event, "guide", guide.id)); geometry.append(line); });
    if (state.polygon) {
      const points = state.polygon.points; const previewPoints = state.polygon.preview ? [...points, state.polygon.preview] : points;
      if (previewPoints.length) geometry.append(svg("polyline", { points: previewPoints.map((p) => `${p.x},${p.y}`).join(" "), class: "draft-polyline", "pointer-events": "none" }));
      points.forEach((p, index) => { const handle = svg("circle", { cx: p.x, cy: p.y, r: pixelsToModel(index === 0 ? 7 : 5), class: `draft-point${index === 0 && points.length >= 3 ? " can-close" : ""}` }); handle.dataset.draftPoint = String(index); const title = svg("title"); title.textContent = index === 0 && points.length >= 3 ? "Замкнуть и создать помещение" : `Вершина ${index + 1}`; handle.append(title); geometry.append(handle); });
    }
    if (state.rectangle && (state.rectangle.preview || state.rectangle.phase !== "awaitingFirst") && D.validateTwoPoint(state.rectangle.a,state.rectangle.b,"rectangle").valid) { const corners = rectanglePoints(state.rectangle); geometry.append(svg("polygon", { points: corners.map((point) => `${point.x},${point.y}`).join(" "), class: "draft-rectangle", "pointer-events":"none" })); }
    if (state.guideDraft) {
      for (const reference of state.guideDraft.references || []) { const ends=extendedGuide(reference.start,reference.end); geometry.append(svg("line",{x1:ends.start.x,y1:ends.start.y,x2:ends.end.x,y2:ends.end.y,class:"guide-reference","pointer-events":"none"})); }
      if (state.guideDraft.preview || ["dragging","awaitingSecond","ready"].includes(state.guideDraft.phase)) { const ends = extendedGuide(state.guideDraft.start, state.guideDraft.end); geometry.append(svg("line", { x1: ends.start.x, y1: ends.start.y, x2: ends.end.x, y2: ends.end.y, class: "guide-line is-draft", "pointer-events":"none" })); }
    }
    if (state.partitionDraft && state.partitionDraft.phase !== "awaitingFirst") { const d = state.partitionDraft; geometry.append(svg("line", { x1:d.start.x, y1:d.start.y, x2:d.end.x, y2:d.end.y, stroke:"#6654d9", "stroke-width":d.thickness_mm, opacity:.35, "pointer-events":"none" }), svg("line", {x1:d.start.x,y1:d.start.y,x2:d.end.x,y2:d.end.y,class:"draft-polyline", "pointer-events":"none"})); }
    if (state.measureDraft && state.measureDraft.phase !== "awaitingFirst") { const measure = state.measureDraft; geometry.append(svg("line", { x1: measure.start.x, y1: measure.start.y, x2: measure.end.x, y2: measure.end.y, class: "measure-line" })); const label = svg("text", { x: (measure.start.x + measure.end.x) / 2, y: -(measure.start.y + measure.end.y) / 2, class: "measure-label" }); label.textContent = `${nf.format(Math.hypot(measure.end.x - measure.start.x, measure.end.y - measure.start.y))} мм`; labels.append(label); }
    if (state.openingPreview?.placement) { const p = state.openingPreview.placement, wall = floor.walls.find((w) => w.id === p.wall_id), length = M.wallLength(wall), ux = (wall.end.x-wall.start.x)/length, uy = (wall.end.y-wall.start.y)/length; geometry.append(svg("line", {x1:wall.start.x+ux*p.offset_mm, y1:wall.start.y+uy*p.offset_mm, x2:wall.start.x+ux*(p.offset_mm+p.width_mm), y2:wall.start.y+uy*(p.offset_mm+p.width_mm), class:"opening-line", opacity:.55, "pointer-events":"none"})); }
    renderPlacementDimensions(geometry, labels, floor);
    if(state.dragging?.kind==="box"){const bounds=D.boundsBetween(state.dragging.start,state.dragging.end);geometry.append(svg("rect",{x:bounds.x,y:bounds.y,width:bounds.w,height:bounds.h,class:`selection-box${state.dragging.end.x<state.dragging.start.x?" is-crossing":""}`,"pointer-events":"none"}));}
    renderCornerHandles(geometry, floor);
    appendSnapMarker(geometry, labels);
    if (isNew) { drawing.addEventListener("click", planClick); drawing.addEventListener("wheel", planWheel, { passive: false }); drawing.addEventListener("pointerdown", planPointerDown); drawing.addEventListener("pointermove", planPointerMove); drawing.addEventListener("pointerup", planPointerUp); drawing.addEventListener("pointercancel", planPointerCancel); drawing.addEventListener("pointerleave", () => { if (!state.dragging) { state.snapVisual = null; updateSnapMarkerOnly(); } }); dom.planCanvas.replaceChildren(drawing); }
    renderCanvasStart(floor);
    updateZoomLabel(); updateToolHelp();
  }
  function selectionItems() { return state.selection?.type==="multiple" ? state.selection.items : state.selection ? [state.selection] : []; }
  function setSelectionItems(items) { const unique=[...new Map(items.map(item=>[`${item.type}:${item.id}`,item])).values()];state.selection=unique.length>1?{type:"multiple",items:unique}:unique[0]||null; }
  function selectedWalls() { const ids=new Set();for(const item of selectionItems()){const found=M.findEntity(state.project,item.id,item.type);if(found?.type==="wall")ids.add(found.entity.id);if(found?.type==="room")arrays(found.entity.boundary).forEach(e=>ids.add(e.wall_id));}return [...ids].map(id=>M.findEntity(state.project,id,"wall")).filter(Boolean); }
  function selected(type, id) { return selectionItems().some(item=>item.type===type&&item.id===id); }
  function stop(event) { event.stopPropagation(); }
  function selectableClick(event, type, id) { if (!["select","box"].includes(state.tool) || state.suppressPlanClick) return; stop(event); choose(type, id, event.shiftKey); }
  function choose(type, id, additive=false) { if (!["select","box"].includes(state.tool)) return; if(additive){const items=selectionItems().filter(item=>item.type!=="vertex");setSelectionItems(selected(type,id)?items.filter(item=>item.type!==type||item.id!==id):[...items,{type,id}]);}else state.selection = { type, id }; state.planSvg?.focus({preventScroll:true});showInspector(); renderTree(); renderPlan(); renderInspector(); }
  function objectsInSelectionBox(box,crossing){
    const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;if(!floor)return [];const result=[];const add=(type,id,points,closed=false)=>{if(D.geometryInBox(points,box,crossing,closed))result.push({type,id});};
    for(const room of arrays(floor.spaces))add("room",room.id,M.roomPolygon(floor,room)||[],true);
    for(const wall of arrays(floor.walls))add("wall",wall.id,[wall.start,wall.end]);
    for(const opening of arrays(floor.openings)){const wall=floor.walls.find(w=>w.id===opening.wall_id),length=wall&&M.wallLength(wall);if(!length)continue;add("opening",opening.id,[opening.offset_mm,opening.offset_mm+opening.width_mm].map(offset=>({x:wall.start.x+(wall.end.x-wall.start.x)*offset/length,y:wall.start.y+(wall.end.y-wall.start.y)*offset/length})));}
    for(const guide of guideCollection(floor)){const ends=extendedGuide(guide.start,guide.end);if(crossing)add("guide",guide.id,[ends.start,ends.end]);}
    return result;
  }
  function pixelsToModel(pixels) { const matrix = state.planSvg?.getScreenCTM(); const scale = matrix ? Math.hypot(matrix.a, matrix.b) : 0; return scale > 0 ? pixels / scale : pixels * (state.view?.w || 10000) / 800; }
  function updateGridBounds() {
    const background=state.planSvg?.querySelector(".plan-background"); if (!background || !state.view) return;
    const viewport=state.planSvg.getBoundingClientRect(), bounds=D.visibleModelBounds(state.view,viewport.width,viewport.height);
    for (const [attribute,key] of [["x","x"],["y","y"],["width","w"],["height","h"]]) background.setAttribute(attribute,bounds[key]);
  }
  function planPoint(event) { const point = state.planSvg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; const local = point.matrixTransform(state.planSvg.getScreenCTM().inverse()); return { x: local.x, y: -local.y }; }
  function snapAt(point, excludeIds = [], event = {}) {
    const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; if (!floor) return { point, snapped: false, kind: "none", target_ids: [] };
    const matrix = state.planSvg?.getScreenCTM(); const pixelsPerMm = matrix ? Math.hypot(matrix.a, matrix.b) : 0; const tolerance = window.VioletSnapping && pixelsPerMm > 0 ? window.VioletSnapping.worldTolerance(12, pixelsPerMm) : pixelsPerMm > 0 ? 12 / pixelsPerMm : Math.max(1, state.view.w / 80);
    const excludedOwners=[...excludeIds,...arrays(floor.openings).filter(o=>excludeIds.includes(o.wall_id)).map(o=>o.id)];
    const points = D.characteristicPoints(floor).filter(p=>!excludedOwners.some(id=>p.id.startsWith(id+":")||p.owner_ids?.includes(id)));
    const segments = arrays(floor.walls).map((wall) => ({ id: wall.id, start: wall.start, end: wall.end })); const guides = guideCollection(floor).map((guide) => ({ id: guide.id, a: guide.start, b: guide.end }));
    let result;
    if (event.ctrlKey || event.metaKey) result={point:D.gridPoint(point,Math.max(1,number(dom.snapInput.value,100))),snapped:true,kind:"grid",target_ids:[]};
    else if (window.VioletSnapping) result = window.VioletSnapping.snapPoint(point, { tolerance_mm: tolerance, grid_enabled: dom.snapToggle.checked, grid_mm: Math.max(1, number(dom.snapInput.value, 100)), points, segments, guides, exclude_ids: excludeIds });
    else if (dom.snapToggle.checked) { const step = Math.max(1, number(dom.snapInput.value, 100)); result = { point: { x: Math.round(point.x / step) * step, y: Math.round(point.y / step) * step }, snapped: true, kind: "grid", target_ids: [] }; }
    else result = { point, snapped: false, kind: "none", target_ids: [] };
    state.snapVisual = result.snapped ? { ...result.point, kind: result.kind } : null; return result;
  }
  function snapped(point, excludeIds = [], event = {}) { return snapAt(point, excludeIds, event).point; }
  function snapHint(kind) { return ({ point: "точка", corner: "угол грани", wall_axis: "опорный угол", midpoint: "середина", opening_edge: "край проёма", endpoint: "конец стены", intersection: "пересечение", guide: "направляющая", segment: "линия стены", grid: "сетка", square: "квадрат", orthogonal: "ортогонально", close:"замкнуть контур" })[kind] || ""; }
  function appendSnapMarker(geometry, labels) {
    if (!state.snapVisual || !geometry || !labels) return; const p = state.snapVisual, size = pixelsToModel(6); const group = svg("g", { class: "snap-marker", "pointer-events": "none" }); group.append(svg("line", { x1: p.x - size, y1: p.y, x2: p.x + size, y2: p.y }), svg("line", { x1: p.x, y1: p.y - size, x2: p.x, y2: p.y + size })); geometry.append(group); const label = svg("text", { x: p.x + size * 1.7, y: -p.y - size * 1.2, class: "snap-label", style:`font-size:${pixelsToModel(12)}px;stroke-width:${pixelsToModel(3)}px` }); label.textContent = snapHint(p.kind); labels.append(label);
  }
  function renderPlacementDimensions(geometry, labels, floor) {
    const draft = state.partitionDraft?.phase !== "awaitingFirst" ? state.partitionDraft : null;
    const selectedEntity=state.selection && M.findEntity(state.project,state.selection.id,state.selection.type);
    const opening=state.openingPreview?.placement || (selectedEntity?.type === "opening" ? selectedEntity.entity : null);
    const wall=opening ? floor.walls.find(w=>w.id===opening.wall_id) : selectedEntity?.type === "wall" ? selectedEntity.entity : null;
    const reference=draft || wall; if(!reference)return;
    const label=(p,text,dy=0)=>{const el=svg("text",{x:p.x,y:-p.y+pixelsToModel(dy),class:"placement-label","pointer-events":"none",style:`font-size:${pixelsToModel(12)}px;stroke-width:${pixelsToModel(4)}px`});el.textContent=text;labels.append(el);};
    const marker=(p,text)=>{geometry.append(svg("circle",{cx:p.x,cy:p.y,r:pixelsToModel(4),fill:"#6654d9","pointer-events":"none"}));label(p,text,-12);};
    const length=Math.hypot(reference.end.x-reference.start.x,reference.end.y-reference.start.y); if(length<1e-6)return;
    marker(reference.start,`A · начало (${nf.format(reference.start.x)}; ${nf.format(reference.start.y)})`); marker(reference.end,"B · конец");
    const ux=(reference.end.x-reference.start.x)/length,uy=(reference.end.y-reference.start.y)/length;
    const dimension=(from,to,text,offsetPx)=>{
      const offset=pixelsToModel(offsetPx),a={x:reference.start.x+ux*from-uy*offset,y:reference.start.y+uy*from+ux*offset},b={x:reference.start.x+ux*to-uy*offset,y:reference.start.y+uy*to+ux*offset};
      geometry.append(svg("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"placement-dimension","pointer-events":"none"}));
      for(const p of [a,b])geometry.append(svg("line",{x1:p.x+uy*pixelsToModel(4),y1:p.y-ux*pixelsToModel(4),x2:p.x-uy*pixelsToModel(4),y2:p.y+ux*pixelsToModel(4),class:"placement-dimension","pointer-events":"none"}));
      label({x:(a.x+b.x)/2,y:(a.y+b.y)/2},text,-6);
    };
    if(opening){dimension(0,opening.offset_mm,`От A: ${nf.format(opening.offset_mm)} мм`,28);dimension(opening.offset_mm,opening.offset_mm+opening.width_mm,`Проём ${nf.format(opening.width_mm)} мм`,48);}
    else dimension(0,length,`A → B: ${nf.format(length)} мм`,28);
    if(draft)label({x:(reference.start.x+reference.end.x)/2,y:(reference.start.y+reference.end.y)/2},`ΔX ${nf.format(reference.end.x-reference.start.x)} · ΔY ${nf.format(reference.end.y-reference.start.y)} мм`,30);
  }
  function rectanglePoints(draft) { return [{ x: draft.a.x, y: draft.a.y }, { x: draft.b.x, y: draft.a.y }, { x: draft.b.x, y: draft.b.y }, { x: draft.a.x, y: draft.b.y }]; }
  function extendedGuide(start, end) { const dx = number(end.x) - number(start.x), dy = number(end.y) - number(start.y), length = Math.hypot(dx, dy); if (length < 1e-9) return { start, end }; const viewport=dom.planCanvas.getBoundingClientRect(), visible=D.visibleModelBounds(state.view,viewport.width,viewport.height); const center = { x: visible.x + visible.w / 2, y: visible.y + visible.h / 2 }; const t = ((center.x - start.x) * dx + (center.y - start.y) * dy) / (length * length); const projected = { x: start.x + dx * t, y: start.y + dy * t }; const span = Math.hypot(visible.w, visible.h) * 2; return { start: { x: projected.x - dx / length * span, y: projected.y - dy / length * span }, end: { x: projected.x + dx / length * span, y: projected.y + dy / length * span } }; }
  function renderCornerHandles(drawing, floor) {
    let points = []; const selectedEntity = state.selection && M.findEntity(state.project, state.selection.id, state.selection.type);
    if(state.selection?.type==="vertex")points=[state.selection.point];
    if (selectedEntity?.type === "wall" && selectedEntity.floor.id === floor.id) points = [selectedEntity.entity.start, selectedEntity.entity.end];
    if (selectedEntity?.type === "room" && selectedEntity.floor.id === floor.id) points = M.roomPolygon(floor, selectedEntity.entity) || [];
    const unique = []; points.forEach((point) => { if (!unique.some((item) => Math.hypot(item.x - point.x, item.y - point.y) < 1e-6)) unique.push(point); });
    unique.forEach((point) => { const handle = svg("circle", { cx: point.x, cy: point.y, r: Math.max(65, state.view.w / 250), class: "corner-handle" }); handle.addEventListener("pointerdown", (event) => startCornerDrag(event, floor, point)); drawing.append(handle); });
  }
  function coincidentEndpoints(floor, point) { const tolerance = Math.max(1e-6, number(state.project.settings?.geometry_tolerance_mm, .1)); const result = []; arrays(floor.walls).forEach((wall) => { for (const key of ["start", "end"]) if (Math.hypot(wall[key].x - point.x, wall[key].y - point.y) <= tolerance) result.push({ wall, key }); }); return result; }
  function startCornerDrag(event, floor, point) { if (state.tool !== "select" || event.button !== 0 || event.altKey) return; event.preventDefault(); event.stopPropagation();state.planSvg.focus({preventScroll:true}); const incident = coincidentEndpoints(floor, point); state.dragging = { kind: "corner", before: snapshot(), label: `перемещение узла (${incident.length} концов стен)`, incident, floorId:floor.id,origin: M.clone(point), clientX: event.clientX, clientY: event.clientY, moved: false, excludeIds: incident.flatMap((item) => [item.wall.id, `${item.wall.id}:${item.key}`]) }; state.planSvg.setPointerCapture(event.pointerId); }
  function moveCornerDrag(event) { const drag = state.dragging; drag.moved ||= Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) > 2; if(!drag.moved)return; const target = snapped(planPoint(event), drag.excludeIds, event); drag.incident.forEach((item) => { item.wall[item.key].x = target.x; item.wall[item.key].y = target.y; }); renderPlan(); recapturePointer(event); }
  function startRoomDrag(event, floor, room) { if (state.tool !== "select" || event.button !== 0) return; if(event.shiftKey){event.preventDefault();event.stopPropagation();state.suppressPlanClick=true;choose("room",room.id,true);return;}state.selection = { type: "room", id: room.id }; state.planSvg.focus({preventScroll:true});showInspector();const tolerance = Math.max(1e-6, number(state.project.settings?.geometry_tolerance_mm, .1)); const analysis = M.analyzeRoomMobility(floor, room, tolerance); if (!analysis.movable) { dom.toolHelp.textContent = `${analysis.reason} Перемещение заблокировано, чтобы не разорвать топологию.`; renderTree(); renderInspector(); return; } event.preventDefault(); event.stopPropagation(); const anchor = M.clone(analysis.walls[0].start); state.dragging = { kind: "room", before: snapshot(), label: `перемещение помещения ${room.code}`, analysis, startPointer: planPoint(event), anchor, lastDx: 0, lastDy: 0, clientX: event.clientX, clientY: event.clientY, moved: false, excludeIds: analysis.walls.flatMap((wall) => [wall.id, `${wall.id}:start`, `${wall.id}:end`]) }; state.planSvg.setPointerCapture(event.pointerId); renderTree(); renderInspector(); }
  function moveRoomDrag(event) { const drag = state.dragging; drag.moved ||= Math.hypot(event.clientX - drag.clientX, event.clientY - drag.clientY) > 2; if(!drag.moved)return; const raw = planPoint(event); const unsnappedTarget = { x: drag.anchor.x + raw.x - drag.startPointer.x, y: drag.anchor.y + raw.y - drag.startPointer.y }; const target = snapped(unsnappedTarget, drag.excludeIds, event); const dx = target.x - drag.anchor.x, dy = target.y - drag.anchor.y; M.translateRoomGeometry(drag.analysis, dx - drag.lastDx, dy - drag.lastDy); drag.lastDx = dx; drag.lastDy = dy; renderPlan(); recapturePointer(event); }
  function finishLiveGeometry(drag) { state.dragging = null; if (!drag.moved || sameSnapshot(drag.before, snapshot())) { state.project = restoreSnapshot(drag.before); renderAll(); return; } state.undo.push({ label: drag.label, json: drag.before }); if (state.undo.length > 80) state.undo.shift(); state.redo = []; state.dirty = projectIsDirty(); state.editorDirty = false; markEngineeringStale(); syncJsonEditor(); renderAll(); checkpointDraft(); scheduleValidation(); }
  function cancelLiveGeometry() { const drag = state.dragging; if (!drag || !drag.before) return; state.project = restoreSnapshot(drag.before); state.dragging = null; normalizeSelection(); renderAll(); toast("Перемещение отменено"); }
  function constrainedEnd(start, point, event, kind) { if (!event.shiftKey) return point; const dx = point.x - start.x, dy = point.y - start.y; let result; if (kind === "rectangle") { const side = Math.max(Math.abs(dx), Math.abs(dy)); result = { x: start.x + (dx < 0 ? -side : side), y: start.y + (dy < 0 ? -side : side) }; state.snapVisual = { ...result, kind: "square" }; } else { result = window.VioletSnapping ? window.VioletSnapping.orthogonalPoint(start, point).point : Math.abs(dx) >= Math.abs(dy) ? { x: point.x, y: start.y } : { x: start.x, y: point.y }; state.snapVisual = { ...result, kind: "orthogonal" }; } return result; }
  function planClick(event) {
    if (state.suppressPlanClick) { state.suppressPlanClick = false; return; }
    if(["select","box"].includes(state.tool) && !event.target?.closest?.("[data-wall-id],.room-shape,.opening-line,.corner-handle,.guide-line")) {if(!event.shiftKey)state.selection=null;renderTree();renderInspector();renderPlan();return;}
    if (state.tool === "opening") { placeOpening(event); return; }
    if (!state.polygon || state.dragging || event.detail > 1) return;
    const raw = planPoint(event), points = state.polygon.points;
    // Closing is a screen-distance gesture, independent of grid snapping.
    const action = D.polygonPointAction(points, raw, {closeRadiusMm:pixelsToModel(12)});
    if (action.action === "close") { finishPolygon(); return; }
    const candidate = points.length ? constrainedEnd(points.at(-1), snapped(raw,[],event), event, "line") : snapped(raw,[],event);
    const next = D.polygonPointAction(points, candidate, {closeRadiusMm:1e-6});
    if (next.action === "close") { finishPolygon(); return; }
    if (next.action === "append") { points.push(next.point); state.polygon.activePoint = points.length - 1; }
    state.polygon.preview = null; renderPlan(); renderInspector();
  }
  function activeTwoPoint() {
    if(state.tool==="rectangle" && state.rectangle?.mode==="dimensions")return null;
    if(state.tool === "guide" && state.guideDraft?.mode !== "two_points")return null;
    const key = ({rectangle:"rectangle",guide:"guideDraft",measure:"measureDraft",partition:"partitionDraft"})[state.tool];
    return key && state[key] ? {draft:state[key],startKey:state.tool === "rectangle" ? "a" : "start",endKey:state.tool === "rectangle" ? "b" : "end"} : null;
  }
  function completeTwoPoint() {
    const current = activeTwoPoint(); if (!current) return;
    const check = D.validateTwoPoint(current.draft[current.startKey], current.draft[current.endKey], state.tool === "rectangle" ? "rectangle" : "line");
    if (!check.valid) { current.draft.phase = "awaitingSecond"; toast(check.reason, "error"); renderPlan(); renderInspector(); return; }
    current.draft.phase = "ready"; renderInspector();
    if (state.tool === "rectangle") finishRectangle(); else if (state.tool === "guide") finishGuide(); else if (state.tool === "partition") finishPartition(); else { renderPlan(); renderInspector(); }
  }
  function planPointerDown(event) {
    state.suppressPlanClick = false;
    const emptySelect = state.tool === "select" && event.button === 0 && !event.target?.closest?.("[data-wall-id],.room-shape,.opening-line,.corner-handle,.guide-line,[data-entity-id]");
    if(event.button===0&&!event.altKey&&(state.tool==="box"||(emptySelect&&event.shiftKey))){event.preventDefault();state.planSvg.focus({preventScroll:true});state.dragging={kind:"box",start:planPoint(event),end:planPoint(event),x:event.clientX,y:event.clientY,additive:Boolean(event.shiftKey),previous:selectionItems(),moved:false};return;}
    if ((state.tool === "pan" && event.button === 0) || event.button === 1 || (event.altKey&&event.button===0) || emptySelect) { event.preventDefault(); state.planSvg.focus({preventScroll:true}); state.snapVisual = null; updateSnapMarkerOnly(); state.dragging = { kind: "pan", x: event.clientX, y: event.clientY, view: { ...state.view }, moved:false, explicit:Boolean(!emptySelect||event.altKey) }; state.planSvg.style.cursor = "grabbing"; state.planSvg.setPointerCapture(event.pointerId); return; }
    if (event.button !== 0) return;
    if(state.tool==="rectangle"&&state.rectangle?.mode==="dimensions"){event.preventDefault();state.planSvg.focus({preventScroll:true});state.suppressPlanClick=true;if(!D.rectangleHasSize(state.rectangle)){toast("Введите положительные ширину и длину; очистите оба размера для рисования мышью.","error");return;}positionRectangle(planPoint(event),event);state.rectangle.phase="ready";finishRectangle(false);return;}
    if(state.tool === "guide" && state.guideDraft?.mode !== "two_points") { event.preventDefault();state.planSvg.focus({preventScroll:true});state.suppressPlanClick=true;placeGuide(event);return; }
    if (state.tool === "polygon" || state.tool === "opening") { state.planSvg.focus({preventScroll:true}); return; }
    const current = activeTwoPoint(); if (!current) return;
    event.preventDefault(); state.planSvg.focus({preventScroll:true}); const point = snapped(planPoint(event),[],event); const {draft,startKey,endKey} = current;
    // A measured line may be started anew, unlike a pending construction.
    const action = D.twoPointDown(state.tool === "measure" && draft.phase === "ready" ? "awaitingFirst" : draft.phase);
    if (action === "finish") { draft[endKey] = constrainedEnd(draft[startKey], point, event, state.tool); state.suppressPlanClick = true; completeTwoPoint(); return; }
    if (action !== "begin") return;
    draft[startKey] = point; draft[endKey] = point; draft.phase = "dragging"; state.dragging = { kind: state.tool, x: event.clientX, y: event.clientY, pointerId: event.pointerId }; recapturePointer(event); renderPlan(); renderInspector();
  }
  function planPointerMove(event) {
    const raw = planPoint(event); dom.cursorCoordinates.textContent = `X ${nf.format(raw.x)} · Y ${nf.format(raw.y)} мм`;
    if (!state.dragging) {
      if(state.tool==="rectangle"&&state.rectangle?.phase==="awaitingFirst"){positionRectangle(raw,event);renderPlan();return;}
      if(state.tool==="rectangle"&&state.rectangle?.mode==="dimensions"){renderPlan();return;}
      if (state.tool === "opening") { updateOpeningPreview(raw, event.target?.dataset?.wallId, event); return; }
      if(state.tool === "guide" && state.guideDraft?.mode !== "two_points") { updateGuidePreview(raw,event);renderPlan();return; }
      const current = activeTwoPoint();
      if (current?.draft.phase === "awaitingSecond") { current.draft[current.endKey] = constrainedEnd(current.draft[current.startKey], snapped(raw,[],event), event, state.tool); renderPlan(); renderInspector(); return; }
      if (state.polygon) {
        const first = state.polygon.points[0]; const close = state.polygon.points.length >= 3 && Math.hypot(raw.x-first.x,raw.y-first.y) <= pixelsToModel(12);
        state.polygon.preview = close ? {...first} : state.polygon.points.length ? constrainedEnd(state.polygon.points.at(-1), snapped(raw,[],event), event, "line") : snapped(raw,[],event);
        if (close) state.snapVisual = {...first,kind:"close"}; renderPlan(); return;
      }
      if (state.tool !== "pan") { snapAt(raw,[],event); updateSnapMarkerOnly(); } return;
    }
    if(state.dragging.kind==="box"){state.dragging.end=raw;state.dragging.moved ||= Math.hypot(event.clientX-state.dragging.x,event.clientY-state.dragging.y)>3;if(state.dragging.moved){recapturePointer(event);renderPlan();}return;}
    if (state.dragging.kind === "pan") { const matrix = state.planSvg.getScreenCTM(); const scaleX = Math.hypot(matrix.a, matrix.b) || 1, scaleY = Math.hypot(matrix.c, matrix.d) || scaleX; state.dragging.moved ||= Math.hypot(event.clientX-state.dragging.x,event.clientY-state.dragging.y)>3; state.view.x = state.dragging.view.x - (event.clientX - state.dragging.x) / scaleX; state.view.y = state.dragging.view.y + (event.clientY - state.dragging.y) / scaleY; state.planSvg.setAttribute("viewBox", viewBoxText()); updateGridBounds();return; }
    if (state.dragging.kind === "corner") { moveCornerDrag(event); return; }
    if (state.dragging.kind === "room") { moveRoomDrag(event); return; }
    const current = activeTwoPoint(); if (!current) return; current.draft[current.endKey] = constrainedEnd(current.draft[current.startKey], snapped(raw,[],event), event, state.tool); renderPlan(); renderInspector();
  }
  function planPointerUp(event) {
    if (!state.dragging) return; const drag = state.dragging;
    if(drag.kind==="box"){state.dragging=null;if(drag.moved){drag.end=planPoint(event);const items=objectsInSelectionBox(D.boundsBetween(drag.start,drag.end),drag.end.x<drag.start.x);setSelectionItems(drag.additive?[...drag.previous,...items]:items);state.suppressPlanClick=true;showInspector();renderPlan();renderInspector();renderTree();}return;}
    // Pointer capture retargets the following click to the SVG root, even for
    // a stationary room click. Consume it before it can clear the selection.
    if (drag.kind === "corner" || drag.kind === "room") { state.suppressPlanClick=true;if(drag.kind==="corner"&&!drag.moved)state.selection={type:"vertex",id:`vertex:${drag.origin.x}:${drag.origin.y}`,floor_id:drag.floorId,point:M.clone(drag.origin)};finishLiveGeometry(drag); return; }
    state.dragging = null; state.suppressPlanClick = true; if (drag.kind === "pan") { state.suppressPlanClick=Boolean(drag.moved || drag.explicit);state.planSvg.style.cursor = ""; renderPlan(); return; }
    const current = activeTwoPoint(); if (!current) return;
    const {draft,startKey,endKey} = current; draft[endKey] = constrainedEnd(draft[startKey], snapped(planPoint(event),[],event), event, state.tool);
    const result = D.twoPointUp({kind:state.tool === "rectangle" ? "rectangle" : "line",start:draft[startKey],end:draft[endKey],clientStart:{x:drag.x,y:drag.y},clientEnd:{x:event.clientX,y:event.clientY}});
    draft.phase = result.phase;
    if (result.action === "commit") completeTwoPoint(); else { if (result.action === "reject") toast(result.reason,"error"); renderPlan(); renderInspector(); }
  }
  function planPointerCancel() { if (state.dragging && ["corner", "room"].includes(state.dragging.kind)) cancelLiveGeometry(); else { const current = activeTwoPoint(); if (current?.draft.phase === "dragging") current.draft.phase = "awaitingSecond"; state.dragging = null; if(state.planSvg?.style) state.planSvg.style.cursor=""; state.suppressPlanClick = true; renderPlan(); renderInspector(); } }
  function updateSnapMarkerOnly() { if (!state.planSvg) return; state.planSvg.querySelectorAll(".snap-marker,.snap-label").forEach((item) => item.remove()); const geometry = state.planSvg.querySelector(".plan-geometry"), labels = state.planSvg.querySelector(".plan-labels"); if (geometry && labels) appendSnapMarker(geometry, labels); }
  function recapturePointer(event) { try { if (state.planSvg && event.pointerId !== undefined) state.planSvg.setPointerCapture(event.pointerId); } catch (_error) { /* Pointer may already have ended. */ } }
  function planWheel(event) { event.preventDefault(); const before = planPoint(event); const ratioX = (before.x - state.view.x) / state.view.w, ratioY = (before.y - state.view.y) / state.view.h; const factor = event.deltaY > 0 ? 1.12 : 0.89; state.view.w *= factor; state.view.h *= factor; state.view.x = before.x - ratioX * state.view.w; state.view.y = before.y - ratioY * state.view.h; renderPlan(); }
  function zoomPlan(factor) { if (!state.view) return; state.view.x += state.view.w * (1 - factor) / 2; state.view.y += state.view.h * (1 - factor) / 2; state.view.w *= factor; state.view.h *= factor; renderPlan(); }
  function updateZoomLabel() { const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; if (!floor || !state.view) return; const fit = fitView(floor); dom.zoomLabel.textContent = `${Math.round(fit.w / state.view.w * 100)}%`; }
  function setTool(tool) { state.tool = tool; [[dom.selectToolButton, "select"], [dom.rectangleToolButton, "rectangle"], [dom.polygonToolButton, "polygon"], [dom.guideToolButton, "guide"], [dom.measureToolButton, "measure"], [dom.panToolButton, "pan"], [dom.partitionToolButton,"partition"], [dom.openingToolButton,"opening"], [boxSelectButton,"box"]].forEach(([button, name]) => { if(!button)return;button.classList.toggle("is-active", tool === name); button.setAttribute("aria-pressed",String(tool === name)); }); renderPlan(); }
  function updateToolHelp() {
    const current = activeTwoPoint(), second = current && current.draft.phase !== "awaitingFirst";
    const help = {select:"Выберите объект. Тяните угловые точки для изменения формы; свойства — справа. Delete — удалить, Ctrl/⌘ Z — отменить.",rectangle:second ? "Укажите противоположный угол — помещение создастся сразу. Shift — квадрат. Enter — принять числа, Esc — отменить." : "Укажите первый угол, затем противоположный; или протяните диагональ мышью. Размеры можно задать в свойствах.",polygon:"Укажите вершины. Щёлкните первую точку, нажмите C / Enter или «Замкнуть контур». Backspace — убрать последнюю точку.",guide:second ? "Укажите вторую точку — вспомогательная линия сохранится. Shift — горизонталь/вертикаль." : "Укажите первую точку вспомогательной линии; затем вторую. Это не стена.",partition:second ? "Укажите конец перегородки — она появится на плане. Shift — горизонталь/вертикаль." : "Укажите начало перегородки, затем конец; или протяните отрезок. Толщина и высота — в свойствах.",opening:state.openingPreview?.error || "Наведите на нужную стену и щёлкните в центре будущего проёма. После размещения измените его размеры справа.",measure:"Укажите две точки. Расстояние показано на плане; линейка временная. Esc — убрать.",pan:"Тяните план мышью. Колесо — масштаб; средняя кнопка — перемещение в любом инструменте."};
    if(state.tool === "select") help.select="Щелчок — выбор. Shift + щелчок — добавить / убрать. B — рамка; Shift + протяжка пустого места — добавить рамкой. Пустое место / Alt + протяжка — сдвиг. Delete — удалить выбранное.";
    help.box="Протяните рамку: слева направо — целиком внутри, справа налево — всё пересечённое. Shift — добавить. Alt + протяжка или средняя кнопка — сдвиг; V — обычный выбор.";
    if(state.tool==="rectangle"&&state.rectangle?.mode==="dimensions")help.rectangle=state.rectangle.phase==="awaitingFirst"?"Введите ширину и длину справа: контур следует за курсором. Щёлкните, чтобы поставить нижний левый угол. Очистите оба размера для рисования мышью.":"Первый угол закреплён. Размеры справа меняют контур; щёлкните на плане или нажмите Enter, чтобы создать помещение.";
    if(state.tool==="guide")help.guide=guideInstruction();
    if(["rectangle","polygon","partition"].includes(state.tool))help[state.tool]+=" Ctrl / ⌘ — принудительно по сетке. Команда повторяется; Esc / V — выбор.";
    if(state.tool==="opening")help.opening=state.openingPreview?.error || "Щёлкните по центру проёма. Отступ до его ближнего края измеряется от A по стене к B. Сетка / Ctrl / ⌘ / Shift — шаг отступа. Esc / V — выбор.";
    dom.toolHelp.textContent = help[state.tool] || "";
    if (state.polygon) { dom.drawingBar.classList.remove("is-hidden"); dom.finishPolygonButton.disabled = state.polygon.points.length < 3; } else dom.drawingBar.classList.add("is-hidden");
  }
  function guideInstruction() {
    const d=state.guideDraft;if(!d)return "Выберите способ построения в свойствах справа.";
    if(d.error)return d.error;
    const rest=" Ctrl / ⌘ — по сетке. После установки команда повторяется; Esc / V — выбор.";
    if(d.mode==="vertical")return "Вертикальная линия следует за курсором. Один щелчок — установить; либо введите X справа."+rest;
    if(d.mode==="horizontal")return "Горизонтальная линия следует за курсором. Один щелчок — установить; либо введите Y справа."+rest;
    if(d.mode==="two_points")return "Укажите две точки линии. Shift — горизонталь / вертикаль."+rest;
    const remaining=guideReferenceCount(d.mode)-d.references.length;
    if(remaining>0)return d.mode==="bisector"?`Биссектриса: выберите ${d.references.length?"вторую":"первую"} стену или вспомогательную линию.`:`Выберите опорную стену или вспомогательную линию для ${d.mode==="parallel"?"параллельной":"перпендикулярной"}.`;
    return (d.mode==="bisector"?"Наведите курсор в нужный угол и щёлкните: для пересекающихся линий выберется ветвь биссектрисы, для параллельных — средняя линия.":"Линия следует за курсором. Щёлкните в нужном месте или задайте точку / отступ справа.")+rest;
  }
  function hasToolDraft() { return D.hasConstruction(state); }
  function confirmReplaceDraft() { return !hasToolDraft() || window.confirm("Отменить текущее незавершённое построение?"); }
  function clearToolDrafts() { state.polygon = null; state.rectangle = null; state.guideDraft = null; state.measureDraft = null; state.partitionDraft = null; state.openingPreview = null; state.snapVisual = null; dom.drawingBar.classList.add("is-hidden"); }
  function activateTool(tool, force = false) { if (!force && hasToolDraft() && !confirmReplaceDraft()) return false; if (state.dragging && ["corner", "room"].includes(state.dragging.kind)) cancelLiveGeometry(); state.dragging = null; clearToolDrafts(); setTool(tool); renderInspector(); return true; }

  function roomDraftDefaults() { const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; return { code: nextRoomCode(), name: "Помещение", height_mm: floor?.default_height_mm || 3000, thickness_mm: 150 }; }
  function startRectangle() {
    if (!state.selectedFloorId) return toast("Сначала добавьте и выберите этаж.", "error");
    if (!confirmReplaceDraft()) return; clearToolDrafts(); state.rectangle = { ...roomDraftDefaults(), floor_id: state.selectedFloorId, mode:"two_points",width_mm:0,depth_mm:0,a: { x: 0, y: 0 }, b: { x: 0, y: 0 }, x_sign: 1, y_sign: 1, phase: "awaitingFirst", preview:false }; state.selection = null; setTab("model"); setTool("rectangle"); showInspector(); renderInspector();
  }
  function positionRectangle(raw,event={}){const d=state.rectangle;if(!d||d.phase!=="awaitingFirst")return;d.a=snapped(raw,[],event);d.b={x:d.a.x+(d.mode==="dimensions"?d.width_mm:0),y:d.a.y+(d.mode==="dimensions"?d.depth_mm:0)};d.preview=d.mode==="dimensions"&&D.rectangleHasSize(d);}
  function finishRectangle(sync=true) {
    if(sync!==false)syncRectangleInputs(); const draft = state.rectangle; if (!draft) return;if(draft.phase==="awaitingFirst"){state.planSvg?.focus({preventScroll:true});return toast(draft.mode==="dimensions"?"Наведите на чертёж и щёлкните, чтобы поставить помещение.":"Укажите первый угол на чертеже или введите ширину и длину.");} const width = Math.abs(draft.b.x - draft.a.x), depth = Math.abs(draft.b.y - draft.a.y); if (!(width > 0 && depth > 0 && Number.isFinite(width) && Number.isFinite(depth)) || (draft.mode==="dimensions" && !D.rectangleHasSize(draft))) {return toast("Ширина и длина прямоугольника должны быть больше нуля.", "error");}
    const points = rectanglePoints(draft); const copy = M.clone(draft); const ok = mutate("добавление прямоугольного помещения", (project) => M.createPolygonRoom(project, draft.floor_id, copy, points)); if (ok) { state.rectangle={...D.repeatRectangle(copy),code:nextRoomCode()};state.selection=null;state.snapVisual=null;renderAll();toast("Помещение создано. Рисуйте следующее или Esc / V — выбор и свойства.","success"); }
  }
  function startGuide() {
    if (!state.selectedFloorId) return toast("Сначала выберите этаж.", "error"); if (!confirmReplaceDraft()) return; const existing = guideCollection(M.findFloor(state.project, state.selectedFloorId)?.floor); if (existing.length >= 200) return toast("На этаже уже 200 направляющих — это предел схемы.", "error"); clearToolDrafts(); resetGuideDraft(); state.selection = null; setTab("model"); setTool("guide"); showInspector(); renderInspector();
  }
  function resetGuideDraft() { const existing=guideCollection(M.findFloor(state.project,state.selectedFloorId)?.floor);state.guideDraft={id:M.id(),floor_id:state.selectedFloorId,name:`Направляющая ${existing.length+1}`,mode:state.guideMode||"vertical",references:[],branch:"auto",start:{x:0,y:0},end:{x:1000,y:0},locked:false,phase:"awaitingFirst",preview:false}; }
  function nearestGuideReference(pointValue) {
    const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;let best=null;
    const candidates=[...arrays(floor?.walls).map(w=>({...w,referenceType:"wall"})),...guideCollection(floor).map(g=>({...g,referenceType:"guide"}))];
    for(const item of candidates){const projected=D.projectOnLine(item,pointValue,item.referenceType==="wall");if(projected.distance<=pixelsToModel(16)&&(!best||projected.distance<best.distance))best={...item,distance:projected.distance};}return best;
  }
  function guideReferenceCount(mode) {return mode==="bisector"?2:["parallel","perpendicular"].includes(mode)?1:0;}
  function updateGuidePreview(raw,event={}) {
    const draft=state.guideDraft;if(!draft||draft.mode==="two_points")return;
    draft.preview=false;draft.error="";
    if(draft.references.length<guideReferenceCount(draft.mode))return;
    try{let p=snapped(raw,[],event);if(["vertical","horizontal"].includes(draft.mode)&&!event.ctrlKey&&!event.metaKey){const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;const points=[...D.characteristicPoints(floor),...guideCollection(floor).flatMap(g=>[{...g.start,id:g.id+":A"},{...g.end,id:g.id+":B"}])];const axis=D.axisSnapPoint(raw,points,draft.mode==="vertical"?"x":"y",pixelsToModel(12));if(axis){p=axis.point;state.snapVisual={...axis.target,kind:axis.target_kind};}}const line=D.guideLine(draft.mode,p,draft.references,draft.branch);draft.start=line.start;draft.end=line.end;draft.preview=true;draft.cursor=p;}
    catch(error){draft.error=error.message;}
  }
  function placeGuide(event) {
    const draft=state.guideDraft;if(!draft)return;const raw=planPoint(event);
    if(draft.references.length<guideReferenceCount(draft.mode)){
      const reference=nearestGuideReference(raw);if(!reference)return toast("Выберите стену или существующую вспомогательную линию.","error");
      if(draft.references.some(r=>r.id===reference.id))return toast("Выберите другую опорную линию.","error");
      if(draft.mode==="bisector"&&draft.references.length===1){try{D.guideLine("bisector",raw,[draft.references[0],reference]);}catch(error){return toast(error.message,"error");}}
      draft.references.push({id:reference.id,name:reference.name,start:M.clone(reference.start),end:M.clone(reference.end)});draft.phase="position";renderPlan();renderInspector();return;
    }
    updateGuidePreview(raw,event);if(draft.error)return toast(draft.error,"error");draft.phase="ready";finishGuide(false);
  }
  function finishGuide() {
    const draft = state.guideDraft; if (!draft) return;
    if(draft.mode==="two_points")syncGuideInputs();
    if(draft.references.length<guideReferenceCount(draft.mode))return toast("Сначала выберите опорные линии на чертеже.","error");
    if(draft.phase!=="ready"&&draft.mode!=="two_points")return toast("Укажите положение линии щелчком или числом в свойствах.","error");
    if(guideCollection(M.findFloor(state.project,draft.floor_id)?.floor).length>=200)return toast("На этаже уже 200 направляющих — это предел схемы.","error");
    if (Math.hypot(draft.end.x - draft.start.x, draft.end.y - draft.start.y) < 1e-6) return toast("Две точки направляющей должны различаться.", "error"); const saved = { id: draft.id, name: draft.name.trim() || "Направляющая", start: M.clone(draft.start), end: M.clone(draft.end), locked: Boolean(draft.locked) }; if (mutate("добавление направляющей", () => guideCollection(M.findFloor(state.project, draft.floor_id).floor, true).push(saved))) { resetGuideDraft();state.selection=null;state.snapVisual=null;renderAll(); }
  }
  function startMeasure() { if (!state.selectedFloorId) return toast("Сначала выберите этаж.", "error"); if (!confirmReplaceDraft()) return; clearToolDrafts(); state.measureDraft = { floor_id: state.selectedFloorId, start: { x: 0, y: 0 }, end: { x: 1000, y: 0 }, phase: "awaitingFirst" }; state.selection = null; setTab("model"); setTool("measure"); showInspector(); renderInspector(); }
  function startPartition() {
    const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; if (!floor) return toast("Сначала выберите этаж.","error");
    if (!confirmReplaceDraft()) return; clearToolDrafts(); state.partitionDraft = {floor_id:floor.id,name:"Перегородка",start:{x:0,y:0},end:{x:2000,y:0},height_mm:floor.default_height_mm,thickness_mm:100,base_mm:0,phase:"awaitingFirst"}; state.selection = null; setTab("model"); setTool("partition"); showInspector(); renderInspector();
  }
  function finishPartition() {
    const draft = state.partitionDraft; if (!draft) return; const form = dom.inspectorContent.querySelector("form"); if (form) updatePartitionFromForm(form);
    let wall; const ok = mutate("добавление перегородки", (project) => { wall = M.createPartition(project,draft.floor_id,draft.start,draft.end,{name:draft.name,thickness_mm:draft.thickness_mm,height_mm:draft.height_mm,base_mm:draft.base_mm}); });
    if (ok) { state.partitionDraft={...M.clone(draft),phase:"awaitingFirst"};state.selection=null;state.snapVisual=null;renderAll();toast("Перегородка добавлена. Укажите следующую; Esc / V — выбор и свойства.","success",6000); }
  }
  function startOpening() {
    const floor = M.findFloor(state.project,state.selectedFloorId)?.floor; if (!floor?.walls?.length) return toast("Сначала нарисуйте стены помещения или перегородку.","error");
    if (!activateTool("opening")) return; state.selection = null; setTab("model"); showInspector(); renderInspector();
  }
  function nearestWall(point, preferredId) {
    const floor = M.findFloor(state.project,state.selectedFloorId)?.floor; let best = null;
    arrays(floor?.walls).forEach((wall) => { const length = M.wallLength(wall); if (!length) return; const dx=wall.end.x-wall.start.x,dy=wall.end.y-wall.start.y; const t=Math.max(0,Math.min(1,((point.x-wall.start.x)*dx+(point.y-wall.start.y)*dy)/(length*length))); const projected={x:wall.start.x+t*dx,y:wall.start.y+t*dy}; const distance=Math.hypot(point.x-projected.x,point.y-projected.y); if (distance <= Math.max(pixelsToModel(16),wall.thickness_mm/2) && (!best || distance < best.distance || wall.id === preferredId)) best={wall,projected,distance}; });
    return best;
  }
  function openingPoint(wall, point, event={}) {
    const anchor=D.openingAnchor(wall,point,number(state.openingSettings.width_mm),Math.max(1,number(dom.snapInput.value,100)),dom.snapToggle.checked||event.ctrlKey||event.metaKey||event.shiftKey);
    state.snapVisual=anchor.snapped?{...anchor.point,kind:"grid"}:null;return anchor.point;
  }
  function updateOpeningPreview(point, preferredId, event={}) {
    state.snapVisual = null; const nearest = nearestWall(point,preferredId); const floor=M.findFloor(state.project,state.selectedFloorId)?.floor;
    if (!nearest) state.openingPreview = null;
    else { try { state.openingPreview = {placement:M.openingPlacement(floor,nearest.wall.id,openingPoint(nearest.wall,point,event),state.openingSettings)}; } catch(error) { state.openingPreview = {error:error.message}; } }
    renderPlan();
  }
  function placeOpening(event) {
    const point=planPoint(event),nearest=nearestWall(point,event.target?.dataset?.wallId); if (!nearest) return toast("Щёлкните по стене в месте будущего проёма.","error");
    const ok=mutate("размещение проёма",project=>M.createOpeningAt(project,state.selectedFloorId,nearest.wall.id,openingPoint(nearest.wall,point,event),state.openingSettings));
    if(ok){state.openingPreview=null;state.selection=null;state.snapVisual=null;renderAll();toast("Проём добавлен. Укажите следующий; Esc / V — выбор и изменение размеров.","success");}
  }
  function commitActiveDraft() { if (state.polygon) finishPolygon(); else if (state.rectangle) finishRectangle(); else if (state.guideDraft) finishGuide(); else if (state.partitionDraft) finishPartition(); else if (state.measureDraft) toast(`Расстояние ${nf.format(Math.hypot(state.measureDraft.end.x - state.measureDraft.start.x, state.measureDraft.end.y - state.measureDraft.start.y))} мм. Линейка временная и не сохраняется.`); }
  function cancelActiveDraft() { if (state.dragging && ["corner", "room"].includes(state.dragging.kind)) return cancelLiveGeometry(); state.dragging = null; clearToolDrafts(); setTool("select"); renderInspector(); }
  function deleteSelection() {
    if (hasToolDraft()) { cancelActiveDraft(); return; }
    if (!state.selection) return; const selection = { ...state.selection };
    if(selection.type==="vertex"){const changed=mutate("удаление лишней вершины",project=>{const result=M.removeCollinearVertex(project,selection.floor_id,selection.point);if(!result.ok)throw new Error(result.message);});if(changed){state.selection=null;renderAll();}return;}
    const selections=selectionItems();if(selections.some(item=>!["opening","zone","measurement","room","wall","guide"].includes(item.type)))return toast("В выделении есть объекты, которые нельзя безопасно удалить. Выберите помещения, свободные стены, проёмы или вспомогательные линии.","error");
    const changed=mutate(`удаление выбранных объектов (${selections.length})`,project=>{
      const floor=M.findFloor(project,state.selectedFloorId)?.floor;
      const guides=selections.filter(item=>item.type==="guide");for(const item of guides){const guide=guideCollection(floor).find(g=>g.id===item.id);if(!guide)throw new Error("Выбранная направляющая больше не найдена.");if(guide.locked)throw new Error("В выделении есть заблокированная направляющая. Ничего не удалено.");}
      const entities=selections.filter(item=>item.type!=="guide");if(entities.length){const result=M.deleteEntitiesCascade(project,entities);if(!result.ok)throw new Error(result.message);}
      if(guides.length){const current=M.findFloor(project,state.selectedFloorId)?.floor;current.properties.editor_guides.guides=guideCollection(current).filter(g=>!guides.some(item=>item.id===g.id));}
    });
    if (changed) { state.selection = null; renderAll(); }
  }

  function startPolygon() {
    if (!state.selectedFloorId) { toast("Сначала добавьте и выберите этаж.", "error"); return; }
    if (!confirmReplaceDraft()) return; clearToolDrafts();
    state.polygon = { floor_id: state.selectedFloorId, points: [], activePoint: 0, code: nextRoomCode(), name: "Помещение", height_mm: M.findFloor(state.project, state.selectedFloorId).floor.default_height_mm, thickness_mm: 150 };
    state.selection = null; state.tool = "polygon"; dom.drawingBar.classList.remove("is-hidden"); setTab("model"); setTool("polygon"); showInspector(); renderInspector();
  }
  function cancelPolygon() { state.polygon = null; dom.drawingBar.classList.add("is-hidden"); setTool("select"); renderInspector(); }
  function finishPolygon() {
    syncPolygonPointInputs();
    if (!state.polygon || state.polygon.points.length < 3) { toast("Для контура нужны минимум три точки.", "error"); return; }
    const draft = M.clone(state.polygon); let room; const ok = mutate("добавление помещения по точкам", (project) => { room = M.createPolygonRoom(project, draft.floor_id, draft, draft.points); });
    if (ok) { state.polygon={...draft,code:nextRoomCode(),points:[],preview:null,activePoint:null};state.selection=null;state.snapVisual=null;renderAll();toast("Контур замкнут. Рисуйте следующий или Esc / V — выбор.","success"); }
  }
  function nextRoomCode() { const count = M.floorEntries(state.project).reduce((sum, { floor }) => sum + arrays(floor.spaces).length, 0); return String(count + 1).padStart(3, "0"); }

  function renderValidation() {
    const report = state.validation; const issues = arrays(report && report.issues); dom.issueList.replaceChildren();
    if (!report) { dom.validationState.textContent = "не проверено"; dom.validationState.className = "validation-state"; dom.issueList.append(node("p", "empty-note", "Проверка ещё не запускалась.")); return; }
    dom.validationState.textContent = report.valid ? (issues.length ? `Допустимо · ${issues.length}` : "Без ошибок") : `Есть ошибки · ${issues.length}`; dom.validationState.className = `validation-state ${report.valid ? "is-valid" : "is-invalid"}`;
    if (!issues.length) { dom.issueList.append(node("p", "empty-note success", "Модель прошла проверку.")); return; }
    issues.forEach((issue) => { const item = node("button", `issue-item ${issue.severity || "warning"}`); item.type = "button"; item.append(node("strong", "", issue.code || issue.severity), node("span", "", issue.message || "Ошибка"), node("small", "", issue.path || "")); const target = arrays(issue.entity_ids)[0]; if (target) item.addEventListener("click", () => { const found = M.findEntity(state.project, target); if (found) { state.selection = { type: found.type, id: target }; if (found.floor) state.selectedFloorId = found.floor.id; renderAll(); } }); dom.issueList.append(item); });
  }

  function field(label, name, value, options = {}) {
    const wrap = node("label", `field${options.full ? " field-full" : ""}`); wrap.append(node("span", "", label)); let control;
    if (options.choices) { control = node("select"); options.choices.forEach(([optionValue, text]) => { const option = node("option", "", text); option.value = optionValue; option.selected = String(optionValue) === String(value ?? ""); control.append(option); }); }
    else if (options.textarea) { control = node("textarea"); control.rows = options.rows || 3; control.value = value ?? ""; }
    else { control = node("input"); control.type = options.type || "text"; control.value = value ?? ""; if (control.type === "number") control.step = options.step || "any"; if (options.min !== undefined) control.min = options.min; if (options.required) control.required = true; }
    control.name = name; wrap.append(control); return wrap;
  }
  function formValue(form, name) { return form.elements[name] ? form.elements[name].value : ""; }
  function formNumber(form, name) { return number(formValue(form, name)); }
  function renderEditForm(title, fields, apply, note) {
    dom.inspectorTitle.textContent = title; const form = node("form", "inspector-form"); fields.forEach((item) => form.append(field(...item))); if (note) form.append(node("p", "form-hint field-full", note)); const actions = node("div", "inspector-actions field-full"); const save = node("button", "button button-primary", "Применить к черновику"); save.type = "submit"; actions.append(save); form.append(actions); form.addEventListener("submit", (event) => { event.preventDefault(); apply(form); }); dom.inspectorContent.replaceChildren(form);
    form.editFields = fields.map(item => item[1]);
    form.initialEditValues = JSON.stringify(form.editFields.map(name => formValue(form, name)));
  }

  function renderInspector() {
    renderBatchMountButton();
    if (!state.project) return;
    if(state.selection?.type==="multiple"){dom.inspectorTitle.textContent=`Выбрано объектов: ${state.selection.items.length}`;const wrap=node("section","multiple-selection");wrap.append(node("p","form-hint","Shift + щелчок добавляет или убирает объект. Delete удаляет всё выбранное одним отменяемым действием. Стены, которые остаются границами помещений, отдельно удалить нельзя."));const list=node("ul");for(const item of state.selection.items.slice(0,50)){const entity=item.type==="guide"?guideCollection(M.findFloor(state.project,state.selectedFloorId)?.floor).find(guide=>guide.id===item.id):M.findEntity(state.project,item.id,item.type)?.entity;list.append(node("li","",`${({wall:"Стена",room:"Помещение",opening:"Проём",guide:"Вспомогательная линия"})[item.type]||item.type}: ${entity?.name||entity?.code||(item.type==="guide"?"Направляющая":item.id)}`));}wrap.append(list);const remove=node("button","button button-secondary button-wide","Удалить выбранное · Delete");remove.type="button";remove.addEventListener("click",deleteSelection);wrap.append(remove);const scope=defaultBatchMountScope();if(scope)wrap.append(batchMountCard(scope));dom.inspectorContent.replaceChildren(wrap);return;}
    if(state.selection?.type==="vertex"){dom.inspectorTitle.textContent="Вершина стены";const wrap=node("section","vertex-selection");wrap.append(node("p","form-hint",`X ${nf.format(state.selection.point.x)} · Y ${nf.format(state.selection.point.y)} мм. Лишнюю точку на прямой можно убрать, объединив два соседних отрезка. Угол, примыкание третьей стены, разные конструкции или связанные обмеры будут сохранены.`));const remove=node("button","button button-secondary button-wide","Убрать лишнюю вершину · Delete");remove.type="button";remove.addEventListener("click",deleteSelection);wrap.append(remove);dom.inspectorContent.replaceChildren(wrap);return;}
    if (state.polygon) { renderPolygonInspector(); return; }
    if (state.rectangle) { renderRectangleInspector(); return; }
    if (state.guideDraft) { renderGuideDraftInspector(); return; }
    if (state.measureDraft) { renderMeasureInspector(); return; }
    if (state.partitionDraft) { renderPartitionInspector(); return; }
    if (state.tool === "opening") { renderOpeningToolInspector(); return; }
    if (state.selection?.type === "guide") { const guide = selectedGuide(); if (guide) { renderGuideInspector(guide); return; } }
    const found = state.selection ? M.findEntity(state.project, state.selection.id, state.selection.type) : null;
    if (!found) { renderProjectInspector(); return; }
    const entity = found.entity;
    if (found.type === "building") renderEditForm("Здание", [["Название", "name", entity.name, { full: true, required: true }]], (form) => mutate("правка здания", () => { entity.name = formValue(form, "name").trim(); }));
    else if (found.type === "floor") renderFloorInspector(found);
    else if (found.type === "room") renderRoomInspector(found);
    else if (found.type === "wall") renderWallInspector(found);
    else if (found.type === "opening") renderOpeningInspector(found);
    else if (found.type === "zone") renderZoneInspector(found);
    else if (found.type === "panelSystem") renderPanelInspector(found);
    else if (found.type === "measurement") renderMeasurementInspector(found);
    else renderProjectInspector();
  }
  function renderDraftForm(title, fields, onInput, onSubmit, note) {
    dom.inspectorTitle.textContent = title; const form = node("form", "inspector-form"); fields.forEach((item) => form.append(field(...item))); if (note) form.append(node("p", "form-hint field-full", note)); const actions = node("div", "inspector-actions field-full"); const cancel = node("button", "button button-secondary", "Отмена"); cancel.type = "button"; cancel.addEventListener("click", cancelActiveDraft); const commit = node("button", "button button-primary", "Создать · Enter"); commit.type = "submit"; actions.append(cancel, commit); form.append(actions); form.addEventListener("input", () => { onInput(form); renderPlan(); }); form.addEventListener("submit", (event) => { event.preventDefault(); onInput(form); onSubmit(); }); dom.inspectorContent.replaceChildren(form);
  }
  function renderRectangleInspector() {
    const draft = state.rectangle; const width = Math.abs(draft.b.x - draft.a.x), depth = Math.abs(draft.b.y - draft.a.y);
    const dimensions=draft.mode==="dimensions";
    const fields=[["Код", "code", draft.code, { required: true }], ["Название", "name", draft.name, { required: true }], ["Ширина по X, мм", "width", dimensions?draft.width_mm:width, { type: "number", min: "0", step: "any" }], ["Длина по Y, мм", "depth", dimensions?draft.depth_mm:depth, { type: "number", min: "0", step: "any" }], ["Высота помещения, мм", "height", draft.height_mm, { type: "number", min: "1" }], ["Толщина стен, мм", "thickness", draft.thickness_mm, { type: "number", min: "1" }]];
    if(draft.show_coordinates)fields.push(["A · X, мм", "ax", draft.a.x, { type: "number", step: "any" }], ["A · Y, мм", "ay", draft.a.y, { type: "number", step: "any" }], ["B · X, мм", "bx", draft.b.x, { type: "number", step: "any" }], ["B · Y, мм", "by", draft.b.y, { type: "number", step: "any" }]);
    renderDraftForm("Прямоугольное помещение",fields,updateRectangleFromForm,()=>finishRectangle(false),"Мышью: щёлкните первый угол, затем второй — или протяните диагональ. Числами: введите ширину и длину, затем щёлкните место установки. Если первый угол уже выбран, размеры меняются от него. Shift — квадрат. Размеры — по опорным линиям стен, вверх на экране — +Y.");
    const commit=dom.inspectorContent.querySelector('button[type="submit"]');if(commit)commit.textContent=draft.phase==="awaitingFirst"?"Перейти к размещению на плане":"Создать · Enter";
    const coordinates=node("button","button button-secondary button-wide",draft.show_coordinates?"Скрыть координаты вершин":"Координаты вершин");coordinates.type="button";coordinates.setAttribute("aria-expanded",String(Boolean(draft.show_coordinates)));coordinates.addEventListener("click",()=>{draft.show_coordinates=!draft.show_coordinates;renderInspector();});dom.inspectorContent.append(coordinates);
    const reset=node("button","button button-secondary button-wide","Сбросить размеры · рисовать мышью");reset.type="button";reset.addEventListener("click",()=>{Object.assign(draft,{mode:"two_points",width_mm:0,depth_mm:0,b:{...draft.a},phase:"awaitingFirst",preview:false,x_sign:1,y_sign:1});state.dragging=null;renderInspector();renderPlan();state.planSvg?.focus({preventScroll:true});});dom.inspectorContent.append(reset);
  }
  function updatePartitionFromForm(form) { const draft=state.partitionDraft; if(!draft)return; draft.name=formValue(form,"name"); draft.start={x:formNumber(form,"x1"),y:formNumber(form,"y1")}; draft.end={x:formNumber(form,"x2"),y:formNumber(form,"y2")}; draft.thickness_mm=formNumber(form,"thickness"); draft.height_mm=formNumber(form,"height"); draft.base_mm=formNumber(form,"base"); if(["x1","y1","x2","y2"].includes(document.activeElement?.name))draft.phase="ready"; }
  function renderPartitionInspector() { const d=state.partitionDraft; renderDraftForm("Новая перегородка", [["Название","name",d.name,{full:true}],["A · начало X, мм","x1",d.start.x,{type:"number",step:"any"}],["A · начало Y, мм","y1",d.start.y,{type:"number",step:"any"}],["B · конец X, мм","x2",d.end.x,{type:"number",step:"any"}],["B · конец Y, мм","y2",d.end.y,{type:"number",step:"any"}],["Толщина, мм","thickness",d.thickness_mm,{type:"number",min:"1"}],["Высота, мм","height",d.height_mm,{type:"number",min:"1"}],["Низ, мм","base",d.base_mm,{type:"number",step:"any"}]],updatePartitionFromForm,finishPartition,"X / Y — координаты от общего начала (0; 0) этажа. Длина A → B — от первого щелчка до второго; A и B подписаны на плане. Shift — горизонталь / вертикаль, Ctrl / ⌘ — точная сетка. После создания можно сразу рисовать следующую перегородку; Esc / V — выбор. Перегородка пока не разделяет помещение на две комнаты."); }
  function renderOpeningToolInspector() {
    dom.inspectorTitle.textContent="Разместить проём"; const d=state.openingSettings, form=node("form","inspector-form");
    [["Вид","kind",d.kind,{full:true,choices:[["door","Дверь"],["window","Окно"],["passage","Проход"],["hatch","Люк"]]}],["Ширина, мм","width",d.width_mm,{type:"number",min:"1"}],["Высота, мм","height",d.height_mm,{type:"number",min:"1"}],["Низ проёма, мм","sill",d.sill_mm,{type:"number",min:"0",full:true}]].forEach(args=>form.append(field(...args)));
    form.append(node("p","form-hint field-full","Щёлкните по стене в центре будущего проёма. На плане A — начало стены, B — конец; отступ идёт от A до ближнего края проёма, а не до его центра. Сетка округляет этот отступ вдоль стены. Ctrl / ⌘ или Shift включает её временно. У края проём сдвигается внутрь. Команда повторяется; Esc / V — выбор и изменение готового проёма."));
    form.addEventListener("submit",event=>event.preventDefault()); form.addEventListener("input",event=>{if(event.target.name==="kind"){const kind=formValue(form,"kind"),windowKind=kind==="window",hatchKind=kind==="hatch"; form.elements.height.value=hatchKind?600:windowKind?1500:2100;form.elements.width.value=hatchKind?600:windowKind?1200:900;form.elements.sill.value=hatchKind?400:windowKind?900:0;} state.openingSettings={kind:formValue(form,"kind"),width_mm:formNumber(form,"width"),height_mm:formNumber(form,"height"),sill_mm:formNumber(form,"sill")};state.openingPreview=null;renderPlan();}); dom.inspectorContent.replaceChildren(form);
  }
  function updateRectangleFromForm(form) {
    const draft = state.rectangle; if (!draft) return;const active=document.activeElement?.name;
    draft.code=formValue(form,"code");draft.name=formValue(form,"name");draft.height_mm=formNumber(form,"height");draft.thickness_mm=formNumber(form,"thickness");
    if(["width","depth"].includes(active)){
      draft.x_sign=draft.b.x<draft.a.x?-1:1;draft.y_sign=draft.b.y<draft.a.y?-1:1;
      D.rectangleSizeInput(draft,formNumber(form,"width"),formNumber(form,"depth"));
    }else if(["ax","ay","bx","by"].includes(active)&&form.elements.ax){
      draft.a={x:formNumber(form,"ax"),y:formNumber(form,"ay")};draft.b={x:formNumber(form,"bx"),y:formNumber(form,"by")};draft.mode="two_points";draft.phase="awaitingSecond";draft.preview=true;
      form.elements.width.value=Math.abs(draft.b.x-draft.a.x);form.elements.depth.value=Math.abs(draft.b.y-draft.a.y);
    }
    if(form.elements.ax){for(const [name,value]of [["ax",draft.a.x],["ay",draft.a.y],["bx",draft.b.x],["by",draft.b.y]])if(name!==active)form.elements[name].value=value;}
  }
  function syncRectangleInputs() { if (!state.rectangle) return; const form = dom.inspectorContent.querySelector("form"); if (form) updateRectangleFromForm(form); }
  function renderGuideDraftInspector() {
    const draft=state.guideDraft;dom.inspectorTitle.textContent="Вспомогательная линия";
    const wrap=node("div","guide-editor"), modes=node("div","guide-modes");modes.setAttribute("role","group");modes.setAttribute("aria-label","Способ построения вспомогательной линии");
    for(const [value,title] of [["vertical","Вертикальная"],["horizontal","Горизонтальная"],["parallel","Параллельная"],["perpendicular","Перпендикулярная"],["bisector","Биссектриса"],["two_points","По двум точкам"]]){
      const button=node("button",`button button-secondary${draft.mode===value?" is-active":""}`,title);button.type="button";button.setAttribute("aria-pressed",String(draft.mode===value));
      button.addEventListener("click",()=>{state.guideMode=value;resetGuideDraft();renderPlan();renderInspector();});modes.append(button);
    }
    wrap.append(modes,node("p","form-hint",guideInstruction()));
    const form=node("form","inspector-form");form.append(field("Название","name",draft.name,{full:true}));
    for(const [index,reference] of draft.references.entries())form.append(node("p","guide-reference-name field-full",`Опора ${index+1}: ${reference.name || reference.id}`));
    const canPosition=draft.references.length>=guideReferenceCount(draft.mode), coordinates=draft.mode==="two_points";
    if(coordinates){for(const args of [["A · X, мм","x1",draft.start.x],["A · Y, мм","y1",draft.start.y],["B · X, мм","x2",draft.end.x],["B · Y, мм","y2",draft.end.y]])form.append(field(...args,{type:"number",step:"any"}));}
    else if(canPosition){
      if(draft.mode==="bisector")form.append(field("Ветвь биссектрисы","branch",draft.branch,{full:true,choices:[["auto","По положению курсора"],["1","Первая ветвь"],["2","Вторая ветвь"]]}));
      else if(draft.mode==="vertical")form.append(field("Координата X, мм","x1",draft.start.x,{type:"number",step:"any",full:true}));
      else if(draft.mode==="horizontal")form.append(field("Координата Y, мм","y1",draft.start.y,{type:"number",step:"any",full:true}));
      else {form.append(field("Через точку X, мм","x1",draft.start.x,{type:"number",step:"any"}),field("Через точку Y, мм","y1",draft.start.y,{type:"number",step:"any"}));if(draft.mode==="parallel")form.append(field("Отступ от опоры, мм (+ слева от A → B)","offset",D.projectOnLine(draft.references[0],draft.start).signedOffset,{type:"number",step:"any",full:true}));}
    }
    const update=(event)=>{
      draft.name=formValue(form,"name");if(!canPosition||event?.target?.name==="name")return;
      try{
        if(coordinates){draft.start={x:formNumber(form,"x1"),y:formNumber(form,"y1")};draft.end={x:formNumber(form,"x2"),y:formNumber(form,"y2")};}
        else {let p={x:form.elements.x1?formNumber(form,"x1"):draft.start.x,y:form.elements.y1?formNumber(form,"y1"):draft.start.y};
          if(event?.target?.name==="offset"){const ref=draft.references[0],length=Math.hypot(ref.end.x-ref.start.x,ref.end.y-ref.start.y),offset=formNumber(form,"offset");p={x:ref.start.x-(ref.end.y-ref.start.y)/length*offset,y:ref.start.y+(ref.end.x-ref.start.x)/length*offset};form.elements.x1.value=p.x;form.elements.y1.value=p.y;}
          if(form.elements.branch)draft.branch=formValue(form,"branch");const line=D.guideLine(draft.mode,p,draft.references,draft.branch);draft.start=line.start;draft.end=line.end;
          if(form.elements.offset&&event?.target?.name!=="offset")form.elements.offset.value=D.projectOnLine(draft.references[0],p).signedOffset;
        }
        draft.preview=true;draft.phase="ready";draft.error="";renderPlan();
      }catch(error){draft.error=error.message;toast(error.message,"error");}
    };
    form.addEventListener("input",update);form.addEventListener("submit",event=>{event.preventDefault();update();if(!draft.error)finishGuide();});
    const actions=node("div","inspector-actions field-full"),cancel=node("button","button button-secondary","Завершить · Esc"),commit=node("button","button button-primary","Установить линию");cancel.type="button";cancel.addEventListener("click",cancelActiveDraft);commit.type="submit";commit.disabled=!canPosition;actions.append(cancel,commit);form.append(actions,node("p","form-hint field-full","Вспомогательная линия бесконечна, помогает привязкам и не печатается в документации. Положение существующей линии можно изменить в режиме выбора."));wrap.append(form);dom.inspectorContent.replaceChildren(wrap);
  }
  function syncGuideInputs() { if (!state.guideDraft) return; const form = dom.inspectorContent.querySelector("form"); if (form) { state.guideDraft.name = formValue(form, "name"); state.guideDraft.start = { x: formNumber(form, "x1"), y: formNumber(form, "y1") }; state.guideDraft.end = { x: formNumber(form, "x2"), y: formNumber(form, "y2") }; } }
  function renderMeasureInspector() { const draft = state.measureDraft; const distance = Math.hypot(draft.end.x - draft.start.x, draft.end.y - draft.start.y); dom.inspectorTitle.textContent = "Линейка"; const form = node("form", "inspector-form"); [["A · X, мм", "x1", draft.start.x, { type: "number", step: "any" }], ["A · Y, мм", "y1", draft.start.y, { type: "number", step: "any" }], ["B · X, мм", "x2", draft.end.x, { type: "number", step: "any" }], ["B · Y, мм", "y2", draft.end.y, { type: "number", step: "any" }]].forEach((item) => form.append(field(...item))); const output = node("strong", "measurement-output field-full", `${nf.format(distance)} мм`); form.append(output, node("p", "form-hint field-full", "Временное измерение не сохраняется и не печатается. Для обмерных данных используйте объект «Замер».")); form.addEventListener("input", () => { draft.start = { x: formNumber(form, "x1"), y: formNumber(form, "y1") }; draft.end = { x: formNumber(form, "x2"), y: formNumber(form, "y2") }; output.textContent = `${nf.format(Math.hypot(draft.end.x - draft.start.x, draft.end.y - draft.start.y))} мм`; draft.phase = "ready"; renderPlan(); }); dom.inspectorContent.replaceChildren(form); }
  function renderGuideInspector(guide) { renderEditForm("Направляющая", [["Название", "name", guide.name, { full: true }], ["A · X, мм", "x1", guide.start.x, { type: "number", step: "any" }], ["A · Y, мм", "y1", guide.start.y, { type: "number", step: "any" }], ["B · X, мм", "x2", guide.end.x, { type: "number", step: "any" }], ["B · Y, мм", "y2", guide.end.y, { type: "number", step: "any" }], ["Блокировка", "locked", guide.locked ? "true" : "false", { choices: [["false", "Можно редактировать"], ["true", "Заблокирована"]] }]], (form) => mutate("правка направляющей", () => { const desiredLocked = formValue(form, "locked") === "true"; const end = { x: formNumber(form, "x2"), y: formNumber(form, "y2") }, start = { x: formNumber(form, "x1"), y: formNumber(form, "y1") }; const geometryChanged = !same(start, guide.start) || !same(end, guide.end) || formValue(form, "name").trim() !== guide.name; if (guide.locked && geometryChanged) throw new Error("Направляющая заблокирована. Сначала отдельным сохранением снимите блокировку."); if (Math.hypot(end.x - start.x, end.y - start.y) < 1e-6) throw new Error("Точки направляющей должны различаться."); guide.name = formValue(form, "name").trim() || "Направляющая"; guide.start = start; guide.end = end; guide.locked = desiredLocked; }), "Delete удаляет выбранную направляющую только из локального черновика; действие можно отменить."); }
  function renderProjectInspector() {
    const meta = state.project.metadata;
    renderEditForm("Проект", [["Название", "name", meta.name, { full: true, required: true }], ["Адрес", "address", meta.address, { full: true }], ["Заказчик", "customer", meta.customer, { full: true }], ["Автор", "author", meta.author, { full: true }], ["Стадия", "stage", meta.stage], ["Описание", "description", meta.description, { full: true, textarea: true }]], (form) => mutate("правка реквизитов", () => { ["name", "address", "customer", "author", "stage", "description"].forEach((key) => { meta[key] = formValue(form, key).trim(); }); }));
  }
  function renderRoomInspector(found) {
    const room = found.entity; const points = M.roomPolygon(found.floor, room) || []; const wallMap = new Map(arrays(found.floor.walls).map((wall) => [wall.id, wall])); const walls = arrays(room.boundary).map((edge) => wallMap.get(edge.wall_id)).filter(Boolean); const thicknesses = [...new Set(walls.map((wall) => number(wall.thickness_mm)))]; const shared = walls.filter((wall) => arrays(found.floor.spaces).some((other) => other.id !== room.id && arrays(other.boundary).some((edge) => edge.wall_id === wall.id)));
    const rectangle=M.rectangleRoomGeometry(found.floor,room);
    const note = `Площадь ${af.format(M.area(points) / 1e6)} м² и периметр ${nf.format(M.perimeter(points))} мм — по опорным линиям стен.${rectangle?" Ширина и длина изменяются от нижнего левого угла; размеры проёмов сохраняются. Изменение связанных стен проверяется перед применением.":" Для произвольного контура меняйте вершины на плане."}${shared.length ? ` Общих стен: ${shared.length}; ${rectangle?"для изменения геометрии связанных помещений редактируйте общие стены отдельно":"изменение общей толщины затронет соседнее помещение"}.` : ""}`;
    const fields=[["Код", "code", room.code, { required: true }], ["Название", "name", room.name, { required: true }]];
    if(rectangle)fields.push(["Ширина по X, мм","width",rectangle.width_mm,{type:"number",min:"1",step:"any"}],["Длина по Y, мм","depth",rectangle.depth_mm,{type:"number",min:"1",step:"any"}]);
    fields.push(["Высота помещения, мм", "height", room.height_mm, { type: "number", min: "1" }], ["Толщина всех стен, мм", "thickness", thicknesses.length === 1 ? thicknesses[0] : "", { type: "number", min: "1" }]);
    renderEditForm(rectangle?"Прямоугольное помещение":"Помещение",fields, (form) => mutate("правка помещения", (project) => {
      const thickness=formValue(form,"thickness")===""?null:formNumber(form,"thickness");
      if(rectangle)M.resizeRectangleRoom(project,found.floor.id,room.id,{width_mm:formNumber(form,"width"),depth_mm:formNumber(form,"depth"),...(formNumber(form,"height")===room.height_mm?{}:{height_mm:formNumber(form,"height")}),...(thickness===null?{}:{thickness_mm:thickness})});
      else{room.height_mm=formNumber(form,"height");if(thickness!==null)walls.forEach(wall=>{wall.thickness_mm=thickness;});}
      room.code=formValue(form,"code").trim();room.name=formValue(form,"name").trim();
    }),note);
    dom.inspectorContent.prepend(batchMountCard({kind:"space",id:room.id}));
  }
  function renderFloorInspector(found) {
    const floor=found.entity,levels=M.finishedLevels(floor);
    const fields=[["Название","name",floor.name,{full:true,required:true}],["Отметка этажа, мм","elevation",floor.elevation_mm,{type:"number",step:"any"}],["Высота по умолчанию, мм","height",floor.default_height_mm,{type:"number",min:"1"}]];
    if(!levels.ambiguous)fields.push(["у.ч.п. — абсолютная отметка, мм","finishedFloor",levels.floor,{type:"number",full:true}],["ур. чист. пот. — абсолютная отметка, мм","finishedCeiling",levels.ceiling,{type:"number",full:true}]);
    renderEditForm("Этаж",fields,form=>mutate("правка этажа и чистовых уровней",project=>{
      if(!levels.ambiguous)M.setFinishedLevels(project,floor.id,{floor:formValue(form,"finishedFloor").trim()?Number(formValue(form,"finishedFloor")):null,ceiling:formValue(form,"finishedCeiling").trim()?Number(formValue(form,"finishedCeiling")):null});
      floor.name=formValue(form,"name").trim();floor.elevation_mm=formNumber(form,"elevation");floor.default_height_mm=formNumber(form,"height");
    }),levels.ambiguous?"В проекте несколько чистовых уровней одного вида. Они сохраняются раздельно; для изменения используйте JSON.":"Чистовые отметки заданы от общего нуля проекта, в мм (например, 3400 и 6200). Пустое поле — по геометрии этажа и помещения; отдельный уровень удаляется только при очистке его поля. Изменение отметки не перемещает стены и проёмы.");
    dom.inspectorContent.prepend(batchMountCard({kind:"floor",id:floor.id}));
  }
  function renderWallInspector(found) {
    const wall = found.entity; const left = arrays(wall.faces).find((item) => item.side === "left"); const right = arrays(wall.faces).find((item) => item.side === "right");
    renderEditForm("Стена", [["Название", "name", wall.name, { full: true }], ["Начало X, мм", "x1", wall.start.x, { type: "number", step: "any" }], ["Начало Y, мм", "y1", wall.start.y, { type: "number", step: "any" }], ["Конец X, мм", "x2", wall.end.x, { type: "number", step: "any" }], ["Конец Y, мм", "y2", wall.end.y, { type: "number", step: "any" }], ["Толщина, мм", "thickness", wall.thickness_mm, { type: "number", min: "1" }], ["Низ, мм", "base", wall.base_mm, { type: "number", step: "any" }], ["Высота, мм", "height", wall.height_mm, { type: "number", min: "1" }], ["Основание", "substrate", wall.substrate, { full: true }], ["Отделка слева", "left", left?.finish || "", { full: true }], ["Отделка справа", "right", right?.finish || "", { full: true }]], (form) => mutate("правка стены", () => { wall.name = formValue(form, "name").trim(); const oldStart = M.clone(wall.start), oldEnd = M.clone(wall.end), newStart = { x: formNumber(form, "x1"), y: formNumber(form, "y1") }, newEnd = { x: formNumber(form, "x2"), y: formNumber(form, "y2") }; const atStart = coincidentEndpoints(found.floor, oldStart), atEnd = coincidentEndpoints(found.floor, oldEnd); atStart.forEach((item) => { item.wall[item.key] = M.clone(newStart); }); atEnd.forEach((item) => { item.wall[item.key] = M.clone(newEnd); }); wall.thickness_mm = formNumber(form, "thickness"); wall.base_mm = formNumber(form, "base"); wall.height_mm = formNumber(form, "height"); wall.substrate = formValue(form, "substrate").trim(); if (left) left.finish = formValue(form, "left"); if (right) right.finish = formValue(form, "right"); }), `Длина по опорной линии: ${nf.format(M.wallLength(wall))} мм. Изменение координаты переносит все совпадающие концы стен, чтобы сохранить стык.`);
    const card = node("section", "wall-mount-entry");
    const bindings = arrays(state.project.properties?.wall_mounting?.walls).filter(item => item.wall_id === wall.id);
    card.append(node("strong", "", "Панели и монтаж"), node("p", "form-hint", bindings.length ? `Настроено сторон: ${bindings.length}. После изменения стены обновите раскладку.` : "Выберите материал и толщину панели. Сразу увидите раскладку, проёмы и места монтажных узлов."));
    const button = node("button", "button button-primary button-wide", bindings.length ? "Открыть монтаж стены" : "Настроить монтаж стены");
    button.type = "button"; button.addEventListener("click", () => openWallMount(wall.id)); card.append(button);
    addMountRemovalControl(card, {kind: "wall", id: wall.id});
    dom.inspectorContent.prepend(card);
  }
  function openWallMount(wallId, faceId) {
    if (specialAssemblies?.hasDraft?.()) return toast("Сначала завершите добавление дверей, люков и подсветки.", "error");
    if (mountAssemblies?.hasDraft?.()) return toast("Сначала завершите добавление STEP-сборок.", "error");
    if (!wallMount) return toast("Модуль монтажа не загрузился. Обновите страницу.", "error");
    if (mountSections?.hasDraft?.()) return toast("Сначала завершите добавление STEP-сечений.", "error");
    const form = dom.inspectorContent.querySelector("form");
    if (form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)))) {
      toast("Размеры или свойства стены ещё не применены. Нажмите «Применить к черновику» в карточке стены, затем откройте монтаж.", "error", 8000);
      return;
    }
    if (state.editorDirty || hasToolDraft() || state.dragging?.before || state.sourceDraftPoints.length || state.calibrationClicks.length || stepViewer?.hasDraft?.() || batchMount?.hasDraft?.()) return toast("Сначала завершите построение, разметку исходника или примените несохранённые параметры / текст JSON.", "error");
    if (wallMount.open(wallId, faceId) && !dom.wallMountDialog.open) dom.wallMountDialog.showModal();
  }
  function defaultBatchMountScope() {
    if (!state.project) return null;
    const rooms = selectionItems().filter(item => item.type === "room" && M.findEntity(state.project,item.id,"room"));
    if (rooms.length === 1) return {kind:"space",id:rooms[0].id};
    const floor = M.findFloor(state.project,state.selectedFloorId)?.floor;
    return floor ? {kind:"floor",id:floor.id} : null;
  }
  function renderBatchMountButton() {
    if (!dom.quickBatchMountButton) return;
    const scope = defaultBatchMountScope(), floor = scope?.kind === "floor" ? M.findFloor(state.project,scope.id)?.floor : null;
    dom.quickBatchMountButton.disabled = !scope || (scope.kind === "floor" && !arrays(floor?.walls).length);
    if(dom.quickExteriorMountButton)dom.quickExteriorMountButton.disabled=!state.project||!arrays(M.findFloor(state.project,state.selectedFloorId)?.floor?.walls).length;
    dom.quickBatchMountButton.textContent = scope?.kind === "space" ? "Монтаж помещения" : "Монтаж всего этажа";
    dom.quickBatchMountButton.title = !scope || dom.quickBatchMountButton.disabled ? "Сначала создайте помещение или перегородку" : scope.kind === "space" ? "Панели и узлы стен и внутренних перегородок помещения" : "Панели и узлы стен помещений и перегородок этажа";
    if (dom.quickRoomAssemblyButton) dom.quickRoomAssemblyButton.disabled = !arrays(M.findFloor(state.project,state.selectedFloorId)?.floor?.spaces).length;
    if (dom.quickBuildingAssemblyButton) dom.quickBuildingAssemblyButton.disabled = !arrays(state.project?.buildings).length;
  }
  function batchMountCard(scope) {
    const room = scope.kind === "space", card = node("section","wall-mount-entry"), floor = !room && M.findFloor(state.project,scope.id)?.floor;
    card.append(node("strong","",room ? "Монтаж всего помещения" : "Монтаж всего этажа"),node("p","form-hint",room ? "Панели и узлы для стен и внутренних перегородок этой комнаты." : "Внутренние стороны стен помещений и перегородки этажа. Существующие назначения по умолчанию сохраняются."));
    const button = node("button","button button-primary button-wide",room ? "Настроить монтаж помещения" : "Настроить монтаж всего этажа");button.type="button";button.disabled=!room&&!arrays(floor?.walls).length;button.addEventListener("click",()=>openBatchMount(scope));card.append(button);
    const special=node("button","button button-secondary button-wide","Двери, люки и подсветка");special.type="button";special.addEventListener("click",()=>openSpecialAssemblies(scope));card.append(special);
    if (room) {const preview=node("button","button button-secondary button-wide","3D комнаты · просмотр и STEP");preview.type="button";preview.addEventListener("click",()=>openRoomAssembly(scope));card.append(preview);}
    addMountRemovalControl(card,scope);return card;
  }
  function addMountRemovalControl(host, scope) {
    const W = window.VioletWallMount, B = window.VioletBatchMount; if (!W?.createRemovalControl || !B?.removalTargets) return;
    const scopeName = scope.kind === "wall" ? "всей стены (обе стороны)" : scope.kind === "space" ? "всего помещения" : "всего этажа";
    return W.createRemovalControl(host, {
      getProject: () => state.project, getTargets: () => scope.kind === "wall" ? W.wallRemovalTargets(state.project, scope.id) : B.removalTargets(state.project, scope),
      label: "Удалить монтаж…", description: `Удалить монтаж ${scopeName}`, mutationLabel: `удаление монтажа ${scopeName}`, hideEmpty: true, mutate, toast,
      blockReason: () => {
        const form = dom.inspectorContent.querySelector("form"), changed = form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)));
        return state.editorDirty || hasActiveDraft() || changed ? "Сначала завершите построение и примените или сбросьте несохранённые параметры." : "";
      },
    });
  }
  function openBatchMount(scope) {
    if (specialAssemblies?.hasDraft?.()) return toast("Сначала завершите добавление дверей, люков и подсветки.", "error");
    if (mountAssemblies?.hasDraft?.()) return toast("Сначала завершите добавление STEP-сборок.", "error");
    if (!batchMount) return toast("Модуль массового монтажа не загрузился. Обновите страницу.","error");
    if (mountSections?.hasDraft?.()) return toast("Сначала завершите добавление STEP-сечений.", "error");
    if (!scope) return toast("Сначала создайте и выберите помещение или этаж.","error");
    const form=dom.inspectorContent.querySelector("form");
    if(form?.editFields && form.initialEditValues!==JSON.stringify(form.editFields.map(name=>formValue(form,name))))return toast("Размеры или свойства ещё не применены. Нажмите «Применить к черновику» в карточке объекта, затем откройте монтаж.","error",8000);
    if(state.editorDirty || hasToolDraft() || state.dragging?.before || state.sourceDraftPoints.length || state.calibrationClicks.length || stepViewer?.hasDraft?.() || wallMount?.hasDraft?.())return toast("Сначала завершите построение, разметку исходника или примените несохранённые параметры / текст JSON.","error");
    if(batchMount.open(scope) && !dom.batchMountDialog.open)dom.batchMountDialog.showModal();
  }
  function openMountSections(scope) {
    if (!mountSections) return toast("Модуль сечений не загрузился. Обновите страницу.", "error");
    const form = dom.inspectorContent.querySelector("form");
    const fieldsChanged = form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)));
    if (state.editorDirty || hasActiveDraft() || fieldsChanged) return toast("Сначала завершите построение и примените несохранённые параметры.", "error");
    if (!dom.mountSectionsDialog.open) dom.mountSectionsDialog.showModal();
    mountSections.open(scope);
  }
  function openMountAssemblies(scope) {
    if (!mountAssemblies) return toast("Модуль сборок не загрузился. Обновите страницу.", "error");
    const form = dom.inspectorContent.querySelector("form");
    const fieldsChanged = form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)));
    if (state.editorDirty || hasActiveDraft() || fieldsChanged) return toast("Сначала завершите построение и примените несохранённые параметры.", "error");
    if (!scope || !["space", "floor", "exterior_floor", "exterior_building"].includes(scope.kind) || !scope.id) return toast("Выберите область монтажа для сборок.", "error");
    if (!dom.mountAssembliesDialog.open) dom.mountAssembliesDialog.showModal();
    mountAssemblies.open(scope);
  }
  function openSpecialAssemblies(scope) {
    if (!specialAssemblies) return toast("Модуль изделий не загрузился. Обновите страницу.", "error");
    const form=dom.inspectorContent.querySelector("form");
    const fieldsChanged=form?.editFields&&form.initialEditValues!==JSON.stringify(form.editFields.map(name=>formValue(form,name)));
    if(state.editorDirty||hasActiveDraft()||fieldsChanged)return toast("Сначала завершите построение и примените изменённые свойства.","error");
    if(!scope)return toast("Сначала выберите этаж или помещение.","error");
    if(specialAssemblies.open(scope))dom.specialAssembliesDialog.showModal();
  }
  function openWholeWall(scope) {
    if (!wholeWall) return toast("Модуль сборки стен не загрузился. Обновите страницу.", "error");
    const form = dom.inspectorContent.querySelector("form");
    const changed = form?.editFields && form.initialEditValues !== JSON.stringify(form.editFields.map(name => formValue(form, name)));
    if (state.editorDirty || hasActiveDraft() || changed) return toast("Сначала завершите построение и примените изменённые свойства.", "error");
    if (!scope) return toast("Сначала выберите этаж или помещение.", "error");
    if (wholeWall.open(scope)) dom.wholeWallDialog.showModal();
  }
  async function openRoomAssembly(scope, technical = false) {
    if (!technical && finishPresentation && state.project && scope) {
      if (state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return toast("Сначала завершите построение и примените изменённые параметры.", "error");
      if (finishPresentation.open(scope)) dom.finishPresentationDialog.showModal();
      return;
    }
    if (!roomAssembly) return toast("Модуль 3D комнаты не загрузился. Обновите страницу.", "error");
    if (!state.project || !scope) return toast("Сначала создайте помещение на этаже.", "error");
    if (state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return toast("Сначала завершите построение и примените изменённые параметры. Они не будут сброшены при открытии 3D.", "error", 8000);
    if (saveInFlight) return toast("Сохранение уже выполняется. Откройте 3D после его завершения.");
    const projectId=state.project.id, floorId=state.selectedFloorId, spaceId=scope.kind === "space" ? scope.id : null;
    if (state.dirty) {toast("Сохраняем проект перед 3D-сборкой комнаты…");await saveDraft();}
    if (state.project?.id !== projectId || state.dirty || state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return;
    if (roomAssembly.open({floor_id:floorId,space_id:spaceId}) && !dom.roomAssemblyDialog.open) dom.roomAssemblyDialog.showModal();
  }
  async function openBuildingAssembly(technical = false, target = null) {
    if (!technical && finishPresentation && state.project?.buildings.length) {
      if (state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return toast("Сначала завершите построение и примените изменённые параметры.", "error");
      const building = state.project.buildings.find(b => b.floors.some(f => f.id === state.selectedFloorId)) || state.project.buildings[0];
      if (finishPresentation.open({kind:"building", id:building.id})) dom.finishPresentationDialog.showModal();
      return;
    }
    if (!buildingAssembly) return toast("Модуль 3D здания не загрузился. Обновите страницу.", "error");
    if (!state.project || !arrays(state.project.buildings).length) return toast("Сначала добавьте здание.", "error");
    if (state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return toast("Завершите построение и примените изменённые параметры перед открытием 3D.", "error", 8000);
    if (saveInFlight) return toast("Сохранение уже выполняется. Откройте 3D после его завершения.");
    const projectId = state.project.id;
    const building = state.project.buildings.find(item => target?.kind === "building" ? item.id === target.id : arrays(item.floors).some(floor => floor.id === (target?.id || state.selectedFloorId))) || state.project.buildings[0];
    if (state.dirty) {toast("Сохраняем проект перед 3D-сборкой здания…"); await saveDraft();}
    if (state.project?.id !== projectId || state.dirty || state.editorDirty || hasActiveDraft() || mountPreparationHasUnappliedInputs()) return;
    if (buildingAssembly.open({building_id:building.id}) && !dom.buildingAssemblyDialog.open) dom.buildingAssemblyDialog.showModal();
  }
  function renderOpeningInspector(found) {
    const opening = found.entity, door = opening.door || {};
    const wallChoices = arrays(found.floor.walls).map((wall) => [wall.id, `${wall.name} · ${nf.format(M.wallLength(wall))} мм`]);
    const doorFields = [
      ["Суммарная ширина полотен, мм", "doorLeaf", door.leaf_width_mm, { type: "number", min: "0.01", full: true }],
      ["Наружная ширина коробки, мм", "doorFrame", door.frame_width_mm, { type: "number", min: "0.01", full: true }],
      ["Петли / створки", "doorHanding", door.handing || "unspecified", { full: true, choices: [["unspecified", "Не заданы"], ["left", "Петли у края к A"], ["right", "Петли у края к B"], ["double", "Две створки"]] }],
      ["Открывание относительно A → B", "doorSide", door.opening_side || "unspecified", { full: true, choices: [["unspecified", "Не задано"], ["left", "Влево от направления A → B"], ["right", "Вправо от направления A → B"]] }],
      ["Коробка", "doorConcealed", String(door.concealed_frame === true), { full: true, choices: [["false", "Обычная / не уточнена"], ["true", "Скрытая"]] }],
    ];
    renderEditForm("Проём", [["Название", "name", opening.name, { full: true }], ["Стена", "wall", opening.wall_id, { full: true, choices: wallChoices }], ["Вид", "kind", opening.kind, { choices: [["door", "Дверь"], ["window", "Окно"], ["passage", "Проход"], ["hatch", "Люк"], ["custom", "Другой"]] }], ["Отступ от A до края проёма, мм", "offset", opening.offset_mm, { type: "number", min: "0", full: true }], ["Ширина проёма, мм", "width", opening.width_mm, { type: "number", min: "1" }], ["Высота низа / подоконник, мм", "sill", opening.sill_mm, { type: "number", min: "0" }], ["Высота проёма, мм", "height", opening.height_mm, { type: "number", min: "1" }], ["Отделка откосов", "finish", opening.reveal_finish, { full: true }], ...doorFields], (form) => {
      const kind = formValue(form, "kind");
      const parameters = {
        leaf_width_mm: formValue(form, "doorLeaf").trim() ? Number(formValue(form, "doorLeaf")) : null,
        frame_width_mm: formValue(form, "doorFrame").trim() ? Number(formValue(form, "doorFrame")) : null,
        handing: formValue(form, "doorHanding"), opening_side: formValue(form, "doorSide"), concealed_frame: formValue(form, "doorConcealed") === "true",
      };
      const hasParameters = opening.door != null || parameters.leaf_width_mm != null || parameters.frame_width_mm != null || parameters.handing !== "unspecified" || parameters.opening_side !== "unspecified" || parameters.concealed_frame;
      mutate("правка проёма", (project) => M.updateOpening(project, found.floor.id, opening.id, { name: formValue(form, "name").trim(), wall_id: formValue(form, "wall"), kind, offset_mm: formNumber(form, "offset"), width_mm: formNumber(form, "width"), sill_mm: formNumber(form, "sill"), height_mm: formNumber(form, "height"), reveal_finish: formValue(form, "finish"), door: kind === "door" && hasParameters ? parameters : null }));
    }, "A — начало выбранной стены, B — её конец; обе точки подписаны на плане. Отступ — от A до ближнего края проёма, не до центра. Размеры коробки и полотен задаются отдельно от размера проёма.");
    const form = dom.inspectorContent.querySelector("form"), group = node("details", "field-full");
    group.open = true; group.append(node("summary", "", "Параметры двери"));
    const fields = node("div", "form-grid");
    doorFields.forEach((item) => fields.append(form.elements[item[1]].closest("label")));
    const dimensionsHint = node("div", "door-dimension-notes"); dimensionsHint.setAttribute("aria-live", "polite");
    group.append(fields, node("p", "form-hint", "Для двух створок укажите их суммарную ширину; коробка — её общий наружный габарит, не ширина стойки. Сторона открывания: встаньте в A лицом к B — слева или справа от вас. Неизвестные размеры и направления оставьте пустыми: программа их не назначает."), dimensionsHint);
    form.insertBefore(group, form.querySelector(".inspector-actions"));
    const syncKind = () => { group.hidden = formValue(form, "kind") !== "door"; doorFields.forEach(item => {form.elements[item[1]].disabled = group.hidden;}); };
    form.elements.kind.addEventListener("change", syncKind); syncKind();
    const syncDimensions = () => {const messages = M.doorDimensionWarnings({kind: "door", width_mm: formValue(form, "width"), door: {leaf_width_mm: formValue(form, "doorLeaf"), frame_width_mm: formValue(form, "doorFrame")}}); dimensionsHint.replaceChildren(...messages.map(message => node("p", "form-hint", message)));};
    ["width", "doorLeaf", "doorFrame"].forEach(name => form.elements[name].addEventListener("input", syncDimensions)); syncDimensions();
    if(opening.kind==="hatch"||opening.kind==="door"){
      const action=node("button","button button-secondary button-wide","Собрать двери, люки и подсветку");action.type="button";
      action.addEventListener("click",()=>openSpecialAssemblies({kind:"floor",id:found.floor.id}));dom.inspectorContent.append(action);
      dom.inspectorContent.append(node("p","form-hint","Сначала примените размеры проёма. Для скрытой двери выберите коробку «Скрытая» и укажите направление петель."));
    }
  }
  function renderZoneInspector(found) {
    const zone = found.entity; const faceChoices = arrays(found.floor.walls).flatMap((wall) => arrays(wall.faces).map((face) => [face.id, `${wall.name} · ${face.side}`])); const systems = arrays(state.project.panel_systems).map((item) => [item.id, item.name]);
    renderEditForm("Зона облицовки", [["Название", "name", zone.name, { full: true }], ["Грань стены", "face", zone.wall_face_id, { full: true, choices: faceChoices }], ["Панельная система", "system", zone.panel_system_id, { full: true, choices: systems }], ["Смещение, мм", "offset", zone.offset_mm, { type: "number", min: "0" }], ["Ширина, мм", "width", zone.width_mm, { type: "number", min: "1" }], ["Низ, мм", "base", zone.base_mm, { type: "number", min: "0" }], ["Высота, мм", "height", zone.height_mm, { type: "number", min: "1" }]], (form) => mutate("правка зоны облицовки", () => { zone.name = formValue(form, "name"); zone.wall_face_id = formValue(form, "face"); zone.panel_system_id = formValue(form, "system"); zone.offset_mm = formNumber(form, "offset"); zone.width_mm = formNumber(form, "width"); zone.base_mm = formNumber(form, "base"); zone.height_mm = formNumber(form, "height"); }));
  }
  function renderPanelInspector(found) {
    const item = found.entity;
    renderEditForm("Панельная система", [["Название", "name", item.name, { full: true }], ["Версия", "version", item.version], ["Статус", "status", item.status, { choices: [["draft", "Черновик"], ["approved", "Одобрена"], ["retired", "Архив"]] }], ["Панель, мм", "panel", item.panel_thickness_mm, { type: "number", min: "1" }], ["Подоснова, мм", "backing", item.backing_thickness_mm, { type: "number", min: "0" }], ["Шов, мм", "joint", item.joint_mm, { type: "number", min: "0" }], ["Стандартные ширины, мм", "widths", arrays(item.standard_widths_mm).join(", "), { full: true }], ["Максимальная высота, мм", "maxHeight", item.maximum_height_mm ?? "", { type: "number", min: "1" }], ["Минимальный подрез, мм", "minCut", item.minimum_cut_width_mm, { type: "number", min: "1" }], ["Коды профилей", "profiles", arrays(item.profile_codes).join(", "), { full: true }]], (form) => mutate("правка панельной системы", () => { item.name = formValue(form, "name"); item.version = formValue(form, "version"); item.status = formValue(form, "status"); item.panel_thickness_mm = formNumber(form, "panel"); item.backing_thickness_mm = formNumber(form, "backing"); item.joint_mm = formNumber(form, "joint"); item.standard_widths_mm = csvNumbers(formValue(form, "widths")); item.maximum_height_mm = formValue(form, "maxHeight") ? formNumber(form, "maxHeight") : null; item.minimum_cut_width_mm = formNumber(form, "minCut"); item.profile_codes = csvStrings(formValue(form, "profiles")); }));
  }
  function renderMeasurementInspector(found) {
    const item = found.entity; const targets = M.allEntities(state.project).filter((entry) => entry.entity.id !== item.id).map((entry) => [entry.entity.id, `${entry.type} · ${entry.entity.name || entry.entity.code || entry.entity.id.slice(0, 8)}`]);
    renderEditForm("Замер", [["Объект", "target", item.target_id, { full: true, choices: targets }], ["Свойство", "property", item.property_name, { choices: dimensionChoices() }], ["Значение, мм", "value", item.value_mm, { type: "number", step: "any" }], ["Источник", "source", item.source, { full: true }], ["Автор", "author", item.author, { full: true }], ["Допуск, мм", "tolerance", item.tolerance_mm, { type: "number", min: "0" }], ["Статус", "status", item.status, { choices: [["raw", "Исходный"], ["verified", "Проверен"], ["accepted", "Принят"], ["rejected", "Отклонён"], ["superseded", "Заменён"]] }], ["Комментарий", "comment", item.comment, { full: true, textarea: true }]], (form) => mutate("правка замера", () => { item.target_id = formValue(form, "target"); item.property_name = formValue(form, "property"); item.value_mm = formNumber(form, "value"); item.source = formValue(form, "source"); item.author = formValue(form, "author"); item.tolerance_mm = formNumber(form, "tolerance"); item.status = formValue(form, "status"); item.comment = formValue(form, "comment"); }));
  }
  function renderPolygonInspector() {
    dom.inspectorTitle.textContent = "Новый контур"; const wrap = node("div", "polygon-editor"); const meta = node("div", "form-grid");
    [["Код", "code", state.polygon.code], ["Название", "name", state.polygon.name], ["Высота, мм", "height_mm", state.polygon.height_mm, { type: "number" }], ["Толщина, мм", "thickness_mm", state.polygon.thickness_mm, { type: "number" }]].forEach((args) => { const item = field(...args); item.querySelector("input").addEventListener("input", (event) => { state.polygon[args[1]] = args[1].endsWith("_mm") ? number(event.target.value) : event.target.value; }); meta.append(item); }); wrap.append(meta);
    wrap.append(node("p", "form-hint", "Точки задаются точно в миллиметрах. Самопересечение будет отклонено проверкой.")); const table = node("div", "point-table");
    state.polygon.points.forEach((point, index) => { const row = node("div", `point-row${state.polygon.activePoint === index ? " is-active" : ""}`); row.append(node("span", "point-number", String(index + 1))); const x = node("input"); x.type = "number"; x.step = "any"; x.value = point.x; x.dataset.pointIndex = String(index); x.dataset.coordinate = "x"; x.setAttribute("aria-label", `X точки ${index + 1}`); const y = node("input"); y.type = "number"; y.step = "any"; y.value = point.y; y.dataset.pointIndex = String(index); y.dataset.coordinate = "y"; y.setAttribute("aria-label", `Y точки ${index + 1}`); const remove = node("button", "icon-button", "×"); remove.type = "button"; remove.setAttribute("aria-label", `Удалить точку ${index + 1}`); [x, y].forEach((input) => input.addEventListener("input", () => { state.polygon.points[index][input.dataset.coordinate] = number(input.value); renderPlan(); })); remove.addEventListener("click", () => { syncPolygonPointInputs(); state.polygon.points.splice(index, 1); renderPlan(); renderInspector(); }); row.append(x, y, remove); table.append(row); }); wrap.append(table);
    const add = node("button", "button button-secondary button-wide", "+ Добавить точку числом"); add.type = "button"; add.addEventListener("click", () => { syncPolygonPointInputs(); const previous = state.polygon.points.at(-1) || { x: 0, y: 0 }; state.polygon.points.push({ x: previous.x + number(dom.snapInput.value, 100), y: previous.y }); state.polygon.activePoint = state.polygon.points.length - 1; renderPlan(); renderInspector(); }); wrap.append(add);
    const finish=node("button","button button-primary button-wide","Замкнуть и создать помещение"); finish.type="button"; finish.disabled=state.polygon.points.length<3; finish.addEventListener("click",finishPolygon); wrap.append(finish); dom.inspectorContent.replaceChildren(wrap);
  }
  function syncPolygonPointInputs() {
    if (!state.polygon) return;
    dom.inspectorContent.querySelectorAll("input[data-point-index][data-coordinate]").forEach((input) => {
      const point = state.polygon.points[number(input.dataset.pointIndex, -1)];
      if (point) point[input.dataset.coordinate] = number(input.value);
    });
  }

  function csvNumbers(value) { return String(value).split(/[,;\s]+/).filter(Boolean).map(Number).filter(Number.isFinite); }
  function csvStrings(value) { return String(value).split(/[,;]+/).map((item) => item.trim()).filter(Boolean); }
  function dimensionChoices() { return [["length_mm", "Длина"], ["width_mm", "Ширина"], ["height_mm", "Высота"], ["thickness_mm", "Толщина"], ["offset_mm", "Смещение"], ["elevation_mm", "Отметка"], ["sill_mm", "Подоконник"]]; }

  function openEntity(type) {
    if (!state.project) return;
    if (type === "floor" && hasActiveDraft()) return toast("Сначала завершите текущее построение или сохраните сечение. Затем добавьте этаж.", "error");
    if (type === "floor" && state.editorDirty) return toast("Сначала примените или отмените изменения JSON, затем добавьте этаж.", "error");
    clearInlineError(dom.entityFormError); state.entityType = type; dom.entityFormFields.replaceChildren(); dom.entityFormHint.textContent = "Изменение попадёт в локальный черновик. Сохранение выполняется отдельно.";
    dom.entitySubmitButton.textContent = type === "floor" ? "Добавить этаж" : "Добавить в черновик";
    dom.entityDialogKicker.textContent = type === "floor" ? "План помещения" : "Локальный черновик";
    const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; const buildings = arrays(state.project.buildings);
    const add = (...args) => dom.entityFormFields.append(field(...args));
    if (type === "building") { dom.entityDialogTitle.textContent = "Здание"; add("Название", "name", `Здание ${buildings.length + 1}`, { full: true, required: true }); }
    else if (type === "floor") { renderFloorForm(); }
    else if (type === "rectangle") { if (!floor) return toast("Сначала выберите этаж.", "error"); dom.entityDialogTitle.textContent = "Прямоугольное помещение"; add("Код", "code", nextRoomCode(), { required: true }); add("Название", "name", "Помещение", { required: true }); add("X, мм", "x", 0, { type: "number" }); add("Y, мм", "y", 0, { type: "number" }); add("Ширина, мм", "width", 6000, { type: "number", min: "1" }); add("Глубина, мм", "depth", 4500, { type: "number", min: "1" }); add("Высота, мм", "height", floor.default_height_mm, { type: "number", min: "1" }); add("Толщина стен, мм", "thickness", 150, { type: "number", min: "1" }); }
    else if (type === "opening") { if (!floor || !arrays(floor.walls).length) return toast("На выбранном этаже нет стен.", "error"); dom.entityDialogTitle.textContent = "Проём"; add("Название", "name", "Проём", { full: true }); add("Стена", "wall", selected("wall", state.selection?.id) ? state.selection.id : floor.walls[0].id, { full: true, choices: floor.walls.map((wall) => [wall.id, `${wall.name} · ${nf.format(M.wallLength(wall))} мм`]) }); add("Вид", "kind", "door", { choices: [["door", "Дверь"], ["window", "Окно"], ["passage", "Проход"], ["hatch", "Люк"], ["custom", "Другой"]] }); add("Смещение, мм", "offset", 500, { type: "number", min: "0" }); add("Ширина, мм", "width", 900, { type: "number", min: "1" }); add("Подоконник, мм", "sill", 0, { type: "number", min: "0" }); add("Высота, мм", "height", 2100, { type: "number", min: "1" }); add("Отделка откоса", "finish", "", { full: true }); }
    else if (type === "zone") { const faces = floor && floor.walls.flatMap((wall) => arrays(wall.faces).map((face) => [face.id, `${wall.name} · ${face.side}`])); const systems = arrays(state.project.panel_systems); if (!floor || !faces.length || !systems.length) return toast("Для зоны нужны грань стены и панельная система.", "error"); dom.entityDialogTitle.textContent = "Зона облицовки"; add("Название", "name", "Зона облицовки", { full: true }); add("Грань", "face", faces[0][0], { full: true, choices: faces }); add("Система", "system", systems[0].id, { full: true, choices: systems.map((item) => [item.id, item.name]) }); add("Смещение, мм", "offset", 0, { type: "number", min: "0" }); add("Ширина, мм", "width", 1000, { type: "number", min: "1" }); add("Низ, мм", "base", 0, { type: "number", min: "0" }); add("Высота, мм", "height", floor.default_height_mm, { type: "number", min: "1" }); }
    else if (type === "panel") { dom.entityDialogTitle.textContent = "Панельная система"; add("Название", "name", "Панельная система", { full: true }); add("Версия", "version", "1"); add("Толщина панели, мм", "panel", 12, { type: "number", min: "1" }); add("Подоснова, мм", "backing", 0, { type: "number", min: "0" }); add("Шов, мм", "joint", 10, { type: "number", min: "0" }); add("Ширины панелей, мм", "widths", "600, 1200", { full: true }); add("Минимальный подрез, мм", "minCut", 100, { type: "number", min: "1" }); }
    else if (type === "measurement") { const targets = M.allEntities(state.project).map((entry) => [entry.entity.id, `${entry.type} · ${entry.entity.name || entry.entity.code || entry.entity.id.slice(0, 8)}`]); if (!targets.length) return toast("Нет объекта для замера.", "error"); dom.entityDialogTitle.textContent = "Замер"; add("Объект", "target", state.selection?.id || targets[0][0], { full: true, choices: targets }); add("Свойство", "property", "length_mm", { choices: dimensionChoices() }); add("Значение, мм", "value", 0, { type: "number" }); add("Источник", "source", "Обмер на объекте", { full: true }); add("Автор", "author", "", { full: true }); add("Допуск, мм", "tolerance", state.project.settings.measurement_tolerance_mm, { type: "number", min: "0" }); add("Комментарий", "comment", "", { full: true }); }
    dom.entityDialog.showModal();
    if (type === "floor") { dom.entityForm.elements.name.focus(); dom.entityForm.elements.name.select(); }
  }

  function renderFloorForm() {
    const buildings = arrays(state.project.buildings), activeBuilding = state.selection?.type === "building" ? state.selection.id : M.findFloor(state.project, state.selectedFloorId)?.building.id;
    let defaults = M.floorCreationDefaults(state.project, activeBuilding || buildings[0]?.id);
    dom.entityDialogTitle.textContent = M.floorEntries(state.project).length ? "Добавить этаж" : "Добавить первый этаж";
    dom.entityFormHint.textContent = "Этаж появится в черновике. Изменения можно отменить; для записи версии нажмите «Проверить и сохранить».";
    if (buildings.length > 1) dom.entityFormFields.append(field("Здание", "building", defaults.building_id, { full: true, choices: buildings.map(b => [b.id, b.name]) }));
    else { const hidden = node("input"); hidden.type = "hidden"; hidden.name = "building"; hidden.value = defaults.building_id || ""; dom.entityFormFields.append(hidden); }
    dom.entityFormFields.append(field("Название этажа", "name", defaults.name, { full: true, required: true }), field("Высота помещений, мм", "height", defaults.default_height_mm, { full: true, type: "number", min: "1", required: true }));
    dom.entityFormFields.append(node("p", "form-hint field-full", "Например, 3000 мм — это 3 метра. Высоту отдельной комнаты можно изменить позже."));
    const details = node("details", "floor-extra field-full"); details.append(node("summary", "", "Отметка этажа и здание"));
    if (defaults.elevation_mm === null) details.open = true;
    details.append(field("Отметка пола этажа, мм", "elevation", defaults.elevation_mm, { type: "number" }), node("p", "form-hint", "Высота пола относительно нулевого уровня здания. Для первого этажа обычно 0; для подвала можно указать отрицательное значение. Следующая отметка — предварительная, уточните её по проекту."));
    details.append(node("p", "form-hint", defaults.creates_building ? "Будет создано «Здание». Переименовать его можно в структуре проекта." : buildings.length === 1 ? `Здание: ${buildings[0].name}.` : "Этаж будет добавлен в выбранное выше здание."));
    dom.entityFormFields.append(details);
    if (buildings.length > 1) dom.entityForm.elements.building.addEventListener("change", () => {
      const next = M.floorCreationDefaults(state.project, formValue(dom.entityForm, "building"));
      for (const [name, key] of [["name", "name"], ["height", "default_height_mm"], ["elevation", "elevation_mm"]]) { const control = dom.entityForm.elements[name]; if (String(control.value) === String(defaults[key] ?? "")) control.value = next[key] ?? ""; }
      if (next.elevation_mm === null) details.open = true;
      defaults = next;
    });
  }

  function submitEntity(event) {
    event.preventDefault(); if (event.submitter?.value === "cancel") { dom.entityDialog.close(); return; } clearInlineError(dom.entityFormError); const form = dom.entityForm; const type = state.entityType; const floor = M.findFloor(state.project, state.selectedFloorId)?.floor;
    try {
      let selected = null;
      const success = mutate(type === "floor" ? "добавление этажа" : `добавление: ${type}`, (project) => {
        if (type === "building") { selected = { id: M.id(), properties: {}, name: formValue(form, "name").trim(), floors: [] }; project.buildings.push(selected); }
        else if (type === "floor") { selected = M.addFloor(project, { building_id: formValue(form, "building") || null, name: formValue(form, "name"), elevation_mm: formValue(form, "elevation"), default_height_mm: formValue(form, "height") }).floor; state.selectedFloorId = selected.id; }
        else if (type === "rectangle") { const x = formNumber(form, "x"), y = formNumber(form, "y"), w = formNumber(form, "width"), d = formNumber(form, "depth"); selected = M.createPolygonRoom(project, state.selectedFloorId, { code: formValue(form, "code"), name: formValue(form, "name"), height_mm: formNumber(form, "height"), thickness_mm: formNumber(form, "thickness") }, [{ x, y }, { x: x + w, y }, { x: x + w, y: y + d }, { x, y: y + d }]); }
        else if (type === "opening") { selected = { id: M.id(), properties: {}, name: formValue(form, "name"), wall_id: formValue(form, "wall"), kind: formValue(form, "kind"), offset_mm: formNumber(form, "offset"), width_mm: formNumber(form, "width"), sill_mm: formNumber(form, "sill"), height_mm: formNumber(form, "height"), reveal_finish: formValue(form, "finish"), door: formValue(form, "kind") === "door" ? { leaf_width_mm: null, frame_width_mm: null, handing: "unspecified", opening_side: "unspecified", concealed_frame: false } : null }; floor.openings.push(selected); }
        else if (type === "zone") { selected = { id: M.id(), properties: {}, name: formValue(form, "name"), wall_face_id: formValue(form, "face"), panel_system_id: formValue(form, "system"), offset_mm: formNumber(form, "offset"), width_mm: formNumber(form, "width"), base_mm: formNumber(form, "base"), height_mm: formNumber(form, "height"), excluded_opening_ids: [] }; floor.cladding_zones.push(selected); }
        else if (type === "panel") { selected = { id: M.id(), properties: {}, name: formValue(form, "name"), version: formValue(form, "version"), panel_thickness_mm: formNumber(form, "panel"), backing_thickness_mm: formNumber(form, "backing"), joint_mm: formNumber(form, "joint"), standard_widths_mm: csvNumbers(formValue(form, "widths")), maximum_height_mm: null, minimum_cut_width_mm: formNumber(form, "minCut"), profile_codes: [], status: "draft" }; project.panel_systems.push(selected); }
        else if (type === "measurement") { selected = { id: M.id(), properties: {}, target_id: formValue(form, "target"), property_name: formValue(form, "property"), value_mm: formNumber(form, "value"), source: formValue(form, "source"), author: formValue(form, "author"), measured_at: new Date().toISOString(), tolerance_mm: formNumber(form, "tolerance"), status: "raw", comment: formValue(form, "comment") }; project.measurements.push(selected); }
      }, { rethrow: true });
      if (success) { dom.entityDialog.close(); if (selected) { const found = M.findEntity(state.project, selected.id); if (found) state.selection = { type: found.type, id: selected.id }; } if (type === "floor") { state.view = null; state.viewFloorId = null; clearToolDrafts();setTool("select"); } renderAll(); if (type === "floor") { toast(`${selected.name} добавлен. Теперь можно нарисовать помещение или импортировать чертёж.`, "success", 6500); if (state.activeTab === "sources" && state.source) { renderSourceInspection(); renderSourcePreview(); } } }
    } catch (error) { inlineError(dom.entityFormError, error, "Объект не добавлен"); }
  }

  function clearJsonEditor() { dom.jsonEditor.value = ""; jsonEditorReference = ""; jsonEditorProject = null; jsonEditorSerial = -1; }
  function syncJsonEditor(force = false) {
    if (!state.project) return;
    if (state.activeTab !== "json" && force !== true) { if (!state.editorDirty) clearJsonEditor(); return; }
    if (force === true || jsonEditorProject !== state.project || jsonEditorSerial !== modelChangeSerial) {
      jsonEditorReference = json(state.project); jsonEditorProject = state.project; jsonEditorSerial = modelChangeSerial;
    }
    if (dom.jsonEditor.value !== jsonEditorReference) dom.jsonEditor.value = jsonEditorReference;
    state.editorDirty = false; renderDraftState();
  }
  function parseEditor() { try { return JSON.parse(dom.jsonEditor.value); } catch (error) { throw new Error(`JSON не разобран: ${error.message}`); } }
  function formatJson() { try { dom.jsonEditor.value = json(parseEditor()); state.editorDirty = dom.jsonEditor.value !== jsonEditorReference; renderDraftState(); } catch (error) { showEditorResult(error.message, false); } }
  async function validateJsonText() { try { const value = parseEditor(); const report = await busy("Проверка JSON…", () => request("/validate", { method: "POST", body: { project: value } })); showEditorReport(report); } catch (error) { showEditorResult(errorText(error, "JSON не прошёл проверку"), false); } }
  function applyJson() { try { const value = parseEditor(); const changed = mutate("правка JSON", (project) => { Object.keys(project).forEach((key) => delete project[key]); Object.assign(project, value); }); if (!changed) { state.editorDirty = false; syncJsonEditor(true); } normalizeSelection(); showEditorResult(changed ? "JSON применён к локальному черновику. Сохранение ещё не выполнено." : "JSON уже соответствует текущему черновику. Новых изменений нет.", true); } catch (error) { showEditorResult(error.message, false); } }
  function showEditorResult(message, valid) { dom.editorValidationResult.replaceChildren(node("p", valid ? "success-text" : "error-text", message)); }
  function showEditorReport(report) { const box = node("div"); box.append(node("p", report.valid ? "success-text" : "error-text", report.valid ? "JSON соответствует модели." : "JSON содержит ошибки.")); arrays(report.issues).forEach((issue) => box.append(node("p", `editor-issue ${issue.severity}`, `${issue.code}: ${issue.message} (${issue.path})`))); dom.editorValidationResult.replaceChildren(box); }

  async function openHistory() {
    try { const result = await busy("Загрузка истории…", () => request(`/projects/${encodeURIComponent(state.project.id)}/history`)); dom.historyList.replaceChildren(); arrays(result.history).slice().reverse().forEach((entry) => { const row = node("div", "history-row"); const info = node("div"); info.append(node("strong", "", `r${entry.revision} · ${entry.message || "Версия"}`), node("span", "", `${formatDate(entry.created_at)}${entry.actor ? ` · ${entry.actor}` : ""}`)); const restore = node("button", "button button-secondary", "Восстановить как новую версию"); restore.type = "button"; restore.disabled = entry.revision === state.baseRevision; restore.addEventListener("click", () => restoreRevision(entry.revision)); row.append(info, restore); dom.historyList.append(row); }); dom.historyDialog.showModal(); }
    catch (error) { showError(error, "Не удалось загрузить историю"); }
  }
  async function restoreRevision(revision) {
    if (!canLeave()) return;
    try { const result = await busy("Восстановление версии…", () => request(`/projects/${encodeURIComponent(state.project.id)}/restore`, { method: "POST", body: { source_revision: revision, expected_revision: state.baseRevision } })); dom.historyDialog.close(); acceptServer(result); toast(`Версия r${revision} восстановлена как новая`, "success"); }
    catch (error) { showError(error, "Не удалось восстановить версию"); }
  }

  async function fileBase64(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onerror = () => reject(reader.error || new Error("Не удалось прочитать файл")); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.readAsDataURL(file); }); }
  function isCadSource() { return ["dxf", "dwg"].includes(state.source?.inspection?.format); }
  function isWorkspaceDxf() { return state.source?.backend === "cad_workspace" && isCadSource(); }
  function isStepSource() { return state.source?.inspection?.format === "step" || state.source?.inspection?.schema === "step-inspection/1"; }
  async function loadSourceCapabilities() {
    try { const result = await request("/sources/capabilities"); dom.sourceCapabilitiesResult.replaceChildren();
      arrays(result.formats).forEach(item => dom.sourceCapabilitiesResult.append(node("p", "", `${item.name || arrays(item.extensions).join(", ")}: ${item.description || item.message || item.status}`)));
      if(result.dwg?.message) dom.sourceCapabilitiesResult.append(node("p",result.dwg.available ? "success-text" : "warning-text",result.dwg.message));
    } catch (_error) { setResult(dom.sourceCapabilitiesResult, "Статус конвертеров недоступен. DXF читается напрямую; для DWG нужен локальный конвертер. Из SolidWorks экспортируйте 2D DXF.", false); }
  }
  function splitWalls() {
    if (!state.selectedFloorId) return toast("Выберите этаж.","error");
    if (hasActiveDraft()) return toast("Сначала завершите или отмените текущее построение.","error");
    let report; const ok=mutate("деление стен в примыканиях", project => { report=M.splitWallsAtIntersections(project,state.selectedFloorId); });
    if (ok || report) toast(report.split_count ? `Разделено стен: ${report.split_count}; добавлено участков: ${report.added_wall_count}.` : "Все пересечения уже разделены; новых точек деления нет.","success");
  }
  async function inspectSource() {
    const file = dom.sourceFileInput.files[0]; if (!file) return setResult(dom.sourceInspectionResult, "Выберите файл.", false);
    const workspaceFile=/\.(dxf|step|stp)$/i.test(file.name), limit=(workspaceFile?64:8)*1024*1024;
    if(file.size>limit)return setResult(dom.sourceInspectionResult,`Файл больше ${workspaceFile?64:8} МиБ.`,false);
    if(!canChangeSource()) return;
    const token=++sourceLoadSequence;
    try {
      let result;
      if(workspaceFile) {result=await busy("Загрузка CAD-файла и проверка геометрии…",async()=>{const response=await fetch(`${API}/cad-assets/upload?filename=${encodeURIComponent(file.name)}`,{method:"POST",headers:{"Content-Type":"application/octet-stream"},body:file});const payload=await response.json();if(!response.ok)throw new ApiError(response.status,payload);return payload;});}
      else { const body = { filename: file.name, content_base64: await fileBase64(file) }; if (dom.sourceUnitsOverride.value) body.units_override = dom.sourceUnitsOverride.value; result = await busy("Инспекция исходника…", () => request("/sources/inspect", { method: "POST", body })); }
      if(token!==sourceLoadSequence)return;
      adoptSource(result,file.name,workspaceFile?"cad_workspace":null,1);loadCadRecent();
    }
    catch (error) { if(token===sourceLoadSequence)setResult(dom.sourceInspectionResult, errorText(error, "Инспекция не выполнена") + (state.source ? ` На подложке остаётся предыдущий файл: ${state.source.filename}.` : ""), false); }
  }
  function adoptSource(result,filename,backend,page=1) {
    cadViewSequence++;stepViewer?.dispose();stepViewer=null;
    state.source={id:result.source_id || result.source?.id,filename:result.source?.filename || filename,backend,inspection:{...result.inspection,filename:result.source?.filename || filename,byte_length:result.source?.byte_length || result.inspection?.byte_length},page,previewWidth:null,previewHeight:null,overlayBlocked:false};
    state.sourceDraftPoints=[];state.calibrationClicks=[];state.sourceRegionPoints=[];state.sourceSelection=[];dom.markupPoints.value="";
    renderSourceInspection();renderSourcePages();renderSourcePreview();renderMarkupList();
  }
  async function loadCadRecent() {
    try {const result=await request("/cad-assets/sources"),previous=dom.cadRecentSelect.value;dom.cadRecentSelect.replaceChildren();const attachments=arrays(state.project?.properties?.cad_assets);arrays(result.sources).forEach(s=>{const id=s.id||s.source_id,attached=attachments.some(a=>a.source_id===id),o=node("option","",`${attached?"✓ ":""}${s.filename}`);o.value=id;o.selected=id===previous;dom.cadRecentSelect.append(o);});if(!dom.cadRecentSelect.options.length){const o=node("option","","Пока нет загруженных CAD-файлов");o.value="";dom.cadRecentSelect.append(o);}dom.cadRecentOpenButton.disabled=!dom.cadRecentSelect.value;}catch(_e){dom.cadRecentOpenButton.disabled=true;}
  }
  async function openCadAsset(id,page=1) {
    if(!id||!canChangeSource())return;
    const token=++sourceLoadSequence;
    try {const result=await busy("Открываем загруженный CAD-файл…",()=>request(`/cad-assets/${encodeURIComponent(id)}/inspect`));if(token!==sourceLoadSequence)return;adoptSource({...result,source_id:id},result.source?.filename||"CAD", "cad_workspace",page);}catch(e){if(token===sourceLoadSequence)setResult(dom.sourceInspectionResult,errorText(e,"CAD-файл недоступен"),false);}
  }
  async function openLibraryAsset(asset) {
    if(!canChangeSource())return false;
    const token=++sourceLoadSequence;
    const result=await busy("Открываем модель из библиотеки…",()=>request(`/model-library/${encodeURIComponent(asset.id)}/open`,{method:"POST",body:{}}));
    if(token!==sourceLoadSequence)return false;
    adoptSource(result,asset.filename,"cad_workspace",1);loadCadRecent();return true;
  }
  function attachStepAsset() {
    if(!isStepSource()||!state.project)return;
    const source=state.source;
    const changed=mutate("приложение STEP к проекту",project=>{project.properties ||= {};project.properties.cad_assets ||= [];if(!project.properties.cad_assets.some(a=>a.source_id===source.id)){if(project.properties.cad_assets.length>=50)throw new Error("К проекту можно приложить до 50 STEP-файлов.");project.properties.cad_assets.push({source_id:source.id,filename:source.filename,format:"step",sections:[]});}});
    if(changed)toast("STEP приложен к локальному черновику. Сохранение проекта — отдельной кнопкой.","success");loadCadRecent();
  }
  function renderStepAttachment() {
    const step=isStepSource(),asset=arrays(state.project?.properties?.cad_assets).find(a=>a.source_id===state.source?.id);
    dom.attachStepButton.classList.toggle("is-hidden",!step);dom.attachStepButton.disabled=Boolean(asset);dom.attachStepButton.textContent=asset?`STEP в проекте · сечений: ${arrays(asset.sections).length}`:"Приложить STEP к черновику проекта";
    dom.detachStepButton.classList.toggle("is-hidden",!step||!asset);
    if(detachStepRequest&&(detachStepRequest.projectId!==state.project?.id||detachStepRequest.sourceId!==state.source?.id||detachStepRequest.assetJson!==JSON.stringify(asset)))detachStepRequest=null;
    dom.detachStepButton.textContent=detachStepRequest?`Подтвердить: убрать STEP и ${arrays(asset?.sections).length} сечений`:"Убрать STEP из проекта";
  }
  function detachStepAsset() {
    if(!isStepSource()||!state.project)return;
    if(stepViewer?.hasDraft?.()||state.editorDirty)return toast("Сначала сохраните или сбросьте незавершённые правки сечения и JSON.","error");
    const asset=arrays(state.project.properties?.cad_assets).find(a=>a.source_id===state.source.id);if(!asset)return;
    const current={projectId:state.project.id,sourceId:state.source.id,assetJson:JSON.stringify(asset)};
    if(!same(detachStepRequest,current)){detachStepRequest=current;renderStepAttachment();toast("Повторное нажатие уберёт только приложение и его сечения из черновика. Исходный файл остаётся в библиотеке; правку можно отменить.","info",9000);return;}
    detachStepRequest=null;
    const changed=mutate("удаление приложения STEP из проекта",project=>{project.properties.cad_assets=arrays(project.properties.cad_assets).filter(item=>item.source_id!==current.sourceId);});
    if(changed)toast("STEP убран из черновика проекта. Оригинал сохранён, действие можно отменить.","success");loadCadRecent();
  }
  function setResult(element, text, ok = true) { element.textContent = text; element.classList.toggle("is-error", !ok); element.classList.toggle("is-success", ok); }
  function canChangeSource() {if(stepViewer?.hasDraft?.()){toast("Сначала сохраните или сбросьте правки сечения. Текущая модель остаётся открытой.","error");return false;}if(!(state.sourceDraftPoints.length || state.calibrationClicks.length))return true;toast("Сначала завершите отметку (Enter) или отмените текущие точки (Esc). Сохранённая разметка останется на месте.","error");return false;}
  function renderSourceInspection() {
    const info = state.source.inspection; const wrap = node("div", "inspection-detail"); wrap.append(node("strong", "", `${info.filename} · ${info.format || info.media_type || "формат"}`), node("span", "", `${nf.format((info.byte_length || 0) / 1024)} КБ`));
    const unitsText = info.units === "mm" ? "мм" : info.units?.requires_override ? "не заданы — требуется явный выбор" : info.units && (info.units.effective_name || info.units.declared_name || info.units.name || info.units.unit || info.units.value) || "не определены"; wrap.append(node("span", "", `Единицы: ${unitsText}`));
    if (arrays(info.warnings).length && !isStepSource()) {const details=node("details","source-warning-details");details.append(node("summary","",`Ограничения исходника · ${info.warnings.length}`));arrays(info.warnings).forEach(warning=>details.append(node("p","warning-text",warning)));wrap.append(details);}
    if (arrays(info.missing_semantics).length) arrays(info.missing_semantics).slice(0, 3).forEach((warning) => wrap.append(node("span", "warning-text", warning)));
    const supported = arrays(info.candidates).filter((item) => item.supported); if (supported.length) wrap.append(node("span", "", `Поддерживаемые замкнутые контуры: ${supported.length}. Выберите нужные на чертеже.`));
    dom.convertHandlesInput.value = ""; state.sourceSelection = [];
    const calibration=sourceMarkup(false)?.calibration;
    [["calAx",calibration?.source_a?.x],["calAy",calibration?.source_a?.y],["calBx",calibration?.source_b?.x],["calBy",calibration?.source_b?.y],["calDistance",calibration?.distance_mm],["calOriginX",calibration?.origin_mm?.x ?? 0],["calOriginY",calibration?.origin_mm?.y ?? 0]].forEach(([id,value])=>dom[id].value=value ?? "");
    dom.sourceEntityLayerFilter.replaceChildren(); const all = node("option", "", "Все слои"); all.value=""; dom.sourceEntityLayerFilter.append(all);
    const layoutLayers=isWorkspaceDxf()?arrays(arrays(info.layouts).find(l=>l.page===state.source.page)?.layers):[];
    [...new Set(isWorkspaceDxf() ? layoutLayers.map(l=>l.name) : arrays(info.selectable_entities).map(item=>item.layer))].sort().forEach(layer=>{const option=node("option","",layoutLayers.find(l=>l.name===layer)?.display_name || layer); option.value=layer; dom.sourceEntityLayerFilter.append(option);});
    setSourceMode(isCadSource() ? "select" : "markup"); renderSourceSelection();
    dom.sourceInspectionResult.replaceChildren(wrap); dom.sourceInspectionResult.className = "inline-result is-success";
  }
  function renderSourcePages() {
    if(isWorkspaceDxf()){dom.sourcePageSelect.replaceChildren();arrays(state.source.inspection.layouts).forEach(layout=>{const option=node("option","",`${layout.name} · ${nf.format(layout.entity_count)} объектов`);option.value=String(layout.page);option.selected=layout.page===state.source.page;dom.sourcePageSelect.append(option);});dom.sourcePageSelect.disabled=dom.sourcePageSelect.options.length<2;return;}
    const count = Math.max(1, number(state.source?.inspection?.page_count, 1)); dom.sourcePageSelect.replaceChildren();
    for (let page = 1; page <= count; page += 1) { const option = node("option", "", String(page)); option.value = String(page); option.selected = page === number(state.source?.page, 1); dom.sourcePageSelect.append(option); }
    dom.sourcePageSelect.disabled = !state.source || count < 2;
  }
  function cadSectionContexts() {
    const floors=M.floorEntries(state.project);
    return {spaces:floors.flatMap(({building,floor})=>arrays(floor.spaces).map(room=>({id:room.id,label:`${building.name} · ${floor.name} · ${room.code || ""} ${room.name}`}))),zones:floors.flatMap(({building,floor})=>arrays(floor.cladding_zones).map(zone=>({id:zone.id,label:`${building.name} · ${floor.name} · ${zone.name || zone.id}`})))};
  }
  function renderSourcePreview() {
    const step=isStepSource();dom.sourcesPanel.classList.toggle("is-step",step);dom.stepWorkspace.classList.toggle("is-hidden",!step);dom.attachStepButton.classList.toggle("is-hidden",!step);dom.cadRegionTools.classList.toggle("is-hidden",!isWorkspaceDxf());dom.convertSourceButton.disabled=Boolean(state.source?.backend);stepViewer?.dispose();stepViewer=null;
    dom.sourcePreview.replaceChildren(); state.sourceOverlay=null; state.sourceDrawing=null; state.sourcePan=null; dom.resetSourceAttachmentButton.disabled = true; const info = state.source.inspection;
    renderStepAttachment();
    if(step){
      const currentSource=state.source,currentProjectId=state.project?.id;
      const assertContext=()=>{if(state.project?.id!==currentProjectId || state.source!==currentSource)throw new Error("Проект или исходник изменился. Откройте STEP заново перед сохранением сечения.");if(state.editorDirty)throw new Error("Сначала примените или отмените изменения JSON.");};
      const {spaces,zones}=cadSectionContexts();
      stepViewer=CW.mountStep(dom.stepWorkspace,info,{sourceId:currentSource.id,request,download:downloadBlob,spaces,zones,savedSections:arrays(state.project?.properties?.cad_assets).find(a=>a.source_id===currentSource.id)?.sections || [],saveSection:(spec,sectionId)=>{
        assertContext();let saved=null;
        const changed=mutate("сохранение сечения STEP с геометрией",project=>{project.properties ||= {};project.properties.cad_assets ||= [];let asset=project.properties.cad_assets.find(a=>a.source_id===currentSource.id);if(!asset){if(project.properties.cad_assets.length>=50)throw new Error("Можно приложить до 50 STEP-файлов.");asset={source_id:currentSource.id,filename:currentSource.filename,format:"step",sections:[]};project.properties.cad_assets.push(asset);}asset.sections ||= [];
          if(sectionId){const index=asset.sections.findIndex(s=>s.id===sectionId);if(index<0)throw new Error("Сечение уже удалено. Сохраните его как новое.");saved={id:sectionId,...M.clone(spec)};asset.sections[index]=saved;}
          else{if(asset.sections.length>=30)throw new Error("Для одного STEP можно сохранить до 30 сечений.");saved={id:M.id(),...M.clone(spec)};asset.sections.push(saved);}
        },{rethrow:true});return changed?saved:null;
      },deleteSection:sectionId=>{assertContext();return mutate("удаление сечения STEP",project=>{const asset=arrays(project.properties?.cad_assets).find(a=>a.source_id===currentSource.id);if(asset)asset.sections=arrays(asset.sections).filter(s=>s.id!==sectionId);},{rethrow:true});}});return;
    }
    if(isWorkspaceDxf()){loadCadDxfView(null);return;}
    if (isCadSource() || arrays(info.preview_segments).length) {
      const segments = arrays(info.preview_segments); if (!segments.length) { dom.sourcePreview.append(node("p", "", "В DXF нет доступных прямых сегментов предпросмотра.")); return; }
      const displaySegments = segments.map((segment) => ({ start: { x: number(segment.start.x), y: -number(segment.start.y) }, end: { x: number(segment.end.x), y: -number(segment.end.y) } }));
      const points = displaySegments.flatMap((segment) => [segment.start, segment.end]); const xs = points.map((p) => p.x), ys = points.map((p) => p.y); const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys); const pad = Math.max(1, Math.max(maxX - minX, maxY - minY) * .05); const drawing = svg("svg", { viewBox: `${minX - pad} ${minY - pad} ${Math.max(1, maxX - minX + pad * 2)} ${Math.max(1, maxY - minY + pad * 2)}`, class: "source-lines", "aria-label": "CAD: ось Y показана вверх" }); displaySegments.forEach((segment,index) => { const line=svg("line", { x1: segment.start.x, y1: segment.start.y, x2: segment.end.x, y2: segment.end.y }); line.dataset.layer=segments[index].layer || ""; drawing.append(line); }); dom.sourcePreview.append(drawing); state.sourceDrawing=drawing;
      const attachment = sourceMarkup(false); const actualSpace = info.units && info.units.coordinate_units === "mm" ? "dxf_world_mm" : "dxf_source_units";
      if (attachment && attachment.coordinate_space && attachment.coordinate_space !== actualSpace) { state.source.overlayBlocked = true; dom.sourcePreview.append(node("p", "overlay-blocked", `Разметка сохранена в ${attachment.coordinate_space}, а предпросмотр открыт в ${actualSpace}. Укажите те же единицы, что при исходной инспекции.`)); return; }
      if(attachment && ((attachment.unit_factor != null && attachment.unit_factor !== info.units?.millimetre_factor) || (attachment.unit_factor == null && info.units?.declared_code === 0 && attachment.coordinate_space === "dxf_world_mm" && attachment.marks?.length))) {state.source.overlayBlocked=true;dom.sourcePreview.append(node("p","overlay-blocked","Масштаб единиц не совпадает с сохранённой разметкой либо не был записан в старой версии. Откройте исходник с исходными единицами; старую неподтверждённую разметку не используйте для модели."));return;}
      state.source.overlayBlocked = false; installSourceOverlay(dom.sourcePreview, { x: minX - pad, y: minY - pad, w: Math.max(1, maxX - minX + pad * 2), h: Math.max(1, maxY - minY + pad * 2) }, "dxf");
    } else {
      const sourceAtLoad=state.source, pageAtLoad=state.source.page;
      const stage = node("div", "source-stage"); const image = node("img"); image.alt = `Предпросмотр ${state.source.filename}`;
      image.src = `${API}/sources/${encodeURIComponent(state.source.id)}/preview.png?page=${encodeURIComponent(state.source.page || 1)}`;
      image.addEventListener("load", () => {
        if(state.source !== sourceAtLoad || state.source.page !== pageAtLoad || !stage.isConnected) return;
        state.source.previewWidth = image.naturalWidth; state.source.previewHeight = image.naturalHeight; const attachment = sourceMarkup(false);
        if (attachment && attachment.preview_width_px && (attachment.preview_width_px !== image.naturalWidth || attachment.preview_height_px !== image.naturalHeight)) { state.source.overlayBlocked = true; dom.resetSourceAttachmentButton.disabled = false; dom.sourcePreview.append(node("p", "overlay-blocked", `Размер предпросмотра изменился: было ${attachment.preview_width_px}×${attachment.preview_height_px}, сейчас ${image.naturalWidth}×${image.naturalHeight}. Разметка скрыта. Сбросьте разметку страницы и откалибруйте её заново.`)); return; }
        const box={x:0,y:0,w:image.naturalWidth,h:image.naturalHeight},drawing=svg("svg",{class:"source-lines",viewBox:`0 0 ${box.w} ${box.h}`});
        drawing.append(svg("image",{href:image.src,x:0,y:0,width:box.w,height:box.h})); image.replaceWith(drawing); state.sourceDrawing=drawing;
        state.source.overlayBlocked = false; installSourceOverlay(stage,box,"raster");
      }); image.addEventListener("error", () => { if(state.source === sourceAtLoad && state.source.page === pageAtLoad) setResult(dom.sourcePreview, "Предпросмотр недоступен.", false); }); stage.append(image); dom.sourcePreview.append(stage);
    }
  }
  async function loadCadDxfView(region) {
    if(!isWorkspaceDxf())return;
    const source=state.source,page=source.page,token=++cadViewSequence;
    source.region=region;source.overlayBlocked=true;source.loadedViewPage=null;source.layoutName=arrays(source.inspection.layouts).find(l=>l.page===page)?.name || "";source.inspection.selectable_entities=[];state.sourceSelection=[];state.sourceRegionPoints=[];state.sourcePan=null;state.sourceOverlay=null;state.sourceDrawing=null;
    dom.cadViewWarnings.classList.add("is-hidden");dom.cadViewWarningsContent.replaceChildren();
    const conflict=CW.attachmentConflict(sourceMarkup(false));
    if(conflict){dom.sourcePreview.replaceChildren(node("p","overlay-blocked",conflict));dom.cadViewStatus.textContent=conflict;renderSourceSelection();return;}
    dom.sourcePreview.replaceChildren(node("p","","Готовим обзор DXF… Большие пространства могут загружаться до минуты."));
    dom.cadViewStatus.textContent="Загрузка выбранного пространства и доступной геометрии…";renderSourceSelection();
    try {
      const result=await request(`/cad-assets/${encodeURIComponent(source.id)}/dxf-view`,{method:"POST",body:{layout:page,region,layers:dom.sourceEntityLayerFilter.value?[dom.sourceEntityLayerFilter.value]:null}});
      if(token!==cadViewSequence || state.source!==source || source.page!==page)return;
      if(result.page!==page)throw new Error("Сервер вернул другое пространство DXF. Повторите открытие нужного листа.");
      const box=CW.rasterBox(result.bounds),preview=result.preview;
      if(preview?.mime_type!=="image/png" || !preview.data_base64)throw new Error("Сервер не вернул PNG-предпросмотр выбранного пространства.");
      const conflict=CW.attachmentConflict(sourceMarkup(false));if(conflict)throw new Error(conflict);
      source.inspection.selectable_entities=arrays(result.selectable_entities);source.layoutName=result.layout_name;source.loadedViewPage=page;source.overlayBlocked=false;
      const drawing=svg("svg",{class:"source-lines",viewBox:`${box.x} ${box.y} ${box.w} ${box.h}`,preserveAspectRatio:"xMidYMid meet","aria-label":`${result.layout_name} · исходные координаты DXF, ось Y вверх`});
      drawing.append(svg("image",{href:`data:image/png;base64,${preview.data_base64}`,x:box.x,y:box.y,width:box.w,height:box.h,preserveAspectRatio:"none"}));
      dom.sourcePreview.replaceChildren(drawing);state.sourceDrawing=drawing;installSourceOverlay(dom.sourcePreview,box,"dxf");setSourceMode(state.sourceMode==="region"?"select":state.sourceMode);
      const text=[`${result.layout_name} · выбор: ${arrays(result.selectable_entities).length} из ${result.matching_entity_count ?? "—"}.`,result.selection_truncated?"Для остальных объектов детализируйте меньшую область.":"", "Перед построением задайте масштаб фрагмента A–B."];
      const warnings=[...new Set(arrays(result.warnings))];dom.cadViewWarnings.classList.toggle("is-hidden",!warnings.length);warnings.forEach(w=>dom.cadViewWarningsContent.append(node("p","warning-text",w)));
      dom.cadViewStatus.textContent=text.filter(Boolean).join(" ");renderSourceSelection();paintSourceOverlay();
    } catch(e) {if(token!==cadViewSequence||state.source!==source)return;dom.sourcePreview.replaceChildren(node("p","error-text",errorText(e,"Предпросмотр DXF не построен")));dom.cadViewStatus.textContent="Не удалось загрузить вид. Сохранённые отметки не изменены. Уменьшите область или повторите загрузку всего пространства.";}
  }
  function setSourceMode(mode) {
    if(mode!=="region")state.sourceRegionPoints=[];
    state.sourceMode = mode; dom.markupModeButton.classList.toggle("is-active", mode === "markup"); dom.calibrationModeButton.classList.toggle("is-active", mode === "calibration");
    dom.sourceSelectModeButton.classList.toggle("is-active",mode === "select"); dom.sourcePanModeButton.classList.toggle("is-active",mode === "pan");
    [[dom.markupModeButton,"markup"],[dom.calibrationModeButton,"calibration"],[dom.sourceSelectModeButton,"select"],[dom.sourcePanModeButton,"pan"]].forEach(([button,name])=>button.setAttribute("aria-pressed",String(mode === name)));
    dom.cadRegionButton.setAttribute("aria-pressed",String(mode==="region"));
    dom.sourceModeHint.textContent = ({markup:"Щёлкайте точки. Enter — сохранить отметку, Esc — отменить. Помещение можно замкнуть щелчком по первой точке; для проёма укажите две границы на оси стены.",calibration:"Щёлкните A, затем B. Введите известное расстояние и начало координат в карточке калибровки ниже.",select:"Щёлкните линию или блок. Shift — выбрать несколько. Назначьте тип; пустое место можно тянуть для перемещения вида.",pan:"Зажмите и тяните исходник. Колесо — масштаб. Средняя кнопка перемещает вид в любом режиме.",region:"Укажите два противоположных угла области. После второго щелчка загрузятся детали внутри. Esc — отменить выбор области; сохранённые отметки не меняются."})[mode];
    if(state.sourceOverlay) state.sourceOverlay.style.cursor=mode === "pan" ? "grab" : mode === "select" ? "default" : "crosshair";
    if(mode==="calibration")dom.calDistance.closest("details").open=true;
  }
  function installSourceOverlay(stage, box, kind) {
    const old = stage.querySelector(".source-overlay"); if (old) old.remove(); const overlay = svg("svg", { class: "source-overlay", viewBox: `${box.x} ${box.y} ${box.w} ${box.h}`, preserveAspectRatio: "xMidYMid meet", "aria-label": "Ручная разметка исходника" });
    overlay.addEventListener("click", sourceOverlayClick); overlay.addEventListener("pointermove", sourceOverlayMove);
    overlay.addEventListener("pointerdown",sourcePointerDown); overlay.addEventListener("pointerup",sourcePointerUp); overlay.addEventListener("pointercancel",sourcePointerUp); overlay.addEventListener("wheel",sourceWheel,{passive:false});
    stage.append(overlay); state.sourceOverlay = overlay; state.sourceViewBox = {...box}; state.sourceFitBox={...box}; state.sourceCoordinateKind = kind; setSourceMode(state.sourceMode);
    let note = stage.querySelector(".source-coordinate-note"); if (!note) { note = node("span", "source-coordinate-note", "x — · y —"); stage.append(note); } state.sourceCoordinateNote = note; paintSourceOverlay();
  }
  function sourceOverlayPoint(event) {
    const point = state.sourceOverlay.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; const local = point.matrixTransform(state.sourceOverlay.getScreenCTM().inverse()); return M.displayToSource(local, state.sourceCoordinateKind);
  }
  function roundedPoint(point) { return { x: Math.round(point.x * 100) / 100, y: Math.round(point.y * 100) / 100 }; }
  function sourceOverlayMove(event) {
    if(state.sourcePan) { const drag=state.sourcePan, scale=drag.scale; state.sourceViewBox={...drag.box,x:drag.box.x-(event.clientX-drag.x)/scale,y:drag.box.y-(event.clientY-drag.y)/scale}; drag.moved ||= Math.hypot(event.clientX-drag.x,event.clientY-drag.y)>3; updateSourceView(); return; }
    if (!state.sourceCoordinateNote) return; const result=sourceSnapped(event),point=result.point;state.sourceRegionHover=point; state.sourceSnap=state.sourceMode === "markup" && result.snapped ? point : null; state.sourceCoordinateNote.textContent = `x ${nf.format(point.x)} · y ${nf.format(point.y)}${state.sourceSnap ? " · привязка" : " (система исходника)"}`; paintSourceOverlay();
  }
  function sourceSnapped(event) { const raw=sourceOverlayPoint(event); if(state.sourceMode !== "markup") return {point:raw,snapped:false}; const matrix=state.sourceOverlay.getScreenCTM(); return C.snapPoint(arrays(state.source.inspection.selectable_entities),arrays(sourceMarkup(false)?.marks),raw,10/(Math.hypot(matrix.a,matrix.b)||1),dom.sourceEntityLayerFilter.value); }
  function sourceHit(event) { const matrix=state.sourceOverlay.getScreenCTM(); return C.hitEntity(arrays(state.source.inspection.selectable_entities),sourceOverlayPoint(event),10/(Math.hypot(matrix.a,matrix.b)||1),dom.sourceEntityLayerFilter.value); }
  function sourcePointerDown(event) {
    state.sourceSuppressClick=false;
    if(event.button !== 1 && !(event.button === 0 && (state.sourceMode === "pan" || (state.sourceMode === "select" && !sourceHit(event))))) return;
    event.preventDefault(); const matrix=state.sourceOverlay.getScreenCTM(); state.sourcePan={x:event.clientX,y:event.clientY,scale:Math.hypot(matrix.a,matrix.b)||1,box:{...state.sourceViewBox},moved:false}; state.sourceOverlay.setPointerCapture(event.pointerId); state.sourceOverlay.style.cursor="grabbing";
  }
  function sourcePointerUp(event) { if(!state.sourcePan) return; state.sourceSuppressClick=state.sourcePan.moved || state.sourceMode === "pan" || event.type === "pointercancel"; state.sourcePan=null; setSourceMode(state.sourceMode); }
  function updateSourceView() { if(!state.sourceOverlay || !state.sourceViewBox) return; const b=state.sourceViewBox,text=`${b.x} ${b.y} ${b.w} ${b.h}`; state.sourceOverlay.setAttribute("viewBox",text); state.sourceDrawing?.setAttribute("viewBox",text); paintSourceOverlay(); }
  function zoomSource(factor,point) { if(!state.sourceOverlay || !state.sourceViewBox) return; const b=state.sourceViewBox, p=point || {x:b.x+b.w/2,y:b.y+b.h/2}; if(b.w*factor<.01 || b.w*factor>1e10) return; state.sourceViewBox={x:p.x-(p.x-b.x)*factor,y:p.y-(p.y-b.y)*factor,w:b.w*factor,h:b.h*factor}; updateSourceView(); }
  function sourceWheel(event) { event.preventDefault(); const model=sourceOverlayPoint(event); zoomSource(event.deltaY>0 ? 1.12 : 1/1.12,toDisplaySourcePoint(model)); }
  function sourceOverlayClick(event) {
    if(state.sourceSuppressClick) { state.sourceSuppressClick=false; return; }
    if(state.sourceMode === "pan" || state.source.overlayBlocked) return;
    dom.sourcePreview.focus({preventScroll:true});
    if(state.sourceMode === "region") {state.sourceRegionPoints ||= [];state.sourceRegionPoints.push(sourceOverlayPoint(event));if(state.sourceRegionPoints.length===2){const region=CW.regionFromPoints(...state.sourceRegionPoints);state.sourceRegionPoints=[];if(region)loadCadDxfView(region);else toast("Укажите область ненулевой ширины и высоты.","error");}paintSourceOverlay();return;}
    if(state.sourceMode === "select") { const entity=sourceHit(event); if(!event.shiftKey) state.sourceSelection=[]; if(entity) { const index=state.sourceSelection.indexOf(entity.handle); if(index>=0) state.sourceSelection.splice(index,1); else state.sourceSelection.push(entity.handle); } paintSourceOverlay(); renderSourceSelection(); return; }
    const result=sourceSnapped(event),point=result.snapped ? result.point : roundedPoint(result.point);
    if (state.sourceMode === "calibration") { if (state.calibrationClicks.length >= 2) state.calibrationClicks = []; state.calibrationClicks.push(point); const first = state.calibrationClicks.length === 1; if (first) { dom.calAx.value = point.x; dom.calAy.value = point.y; } else { dom.calBx.value = point.x; dom.calBy.value = point.y; } }
    else {const first=state.sourceDraftPoints[0],matrix=state.sourceOverlay.getScreenCTM(),tolerance=10/(Math.hypot(matrix.a,matrix.b)||1);if(dom.markupKind.value==="room"&&state.sourceDraftPoints.length>=3&&Math.hypot(point.x-first.x,point.y-first.y)<=tolerance){addMarkup();return;}state.sourceDraftPoints.push(point); dom.markupPoints.value = state.sourceDraftPoints.map((item) => `${item.x},${item.y}`).join("; "); }
    paintSourceOverlay();
  }
  function paintSourceOverlay() {
    const overlay = state.sourceOverlay; if (!overlay || !overlay.isConnected) return; overlay.replaceChildren();
    const layer=dom.sourceEntityLayerFilter.value;
    state.sourceDrawing?.querySelectorAll("line[data-layer]").forEach(line=>line.style.opacity=layer && line.dataset.layer !== layer ? ".1" : "1");
    arrays(state.source?.inspection?.selectable_entities).filter(entity=>state.sourceSelection.includes(entity.handle)).forEach(entity=>{
      const group=svg("g",{class:"source-entity is-selected","pointer-events":"none"}); arrays(entity.segments).forEach(segment=>{const a=toDisplaySourcePoint(segment.start),b=toDisplaySourcePoint(segment.end); group.append(svg("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,"vector-effect":"non-scaling-stroke"}));}); overlay.append(group);
    });
    const source = sourceMarkup(false); arrays(source && source.marks).forEach((mark) => drawSourcePolyline(overlay, arrays(mark.points), mark.label, `saved ${mark.kind}`));
    if(state.sourceMode==="region" && state.sourceRegionPoints?.length && state.sourceRegionHover){const region=CW.regionFromPoints(state.sourceRegionPoints[0],state.sourceRegionHover);if(region)overlay.append(svg("rect",{x:region[0],y:-region[3],width:region[2]-region[0],height:region[3]-region[1],class:"source-region","pointer-events":"none"}));}
    if(state.sourceMode === "markup" && state.sourceSnap) {const p=toDisplaySourcePoint(state.sourceSnap);overlay.append(svg("circle",{cx:p.x,cy:p.y,r:sourceMarkerRadius()/2,style:"fill:#ddffee;stroke:#168457","pointer-events":"none"}));}
    drawSourcePolyline(overlay, state.sourceDraftPoints, "Текущая отметка", "draft");
    const calibration = source && source.calibration; const calPoints = state.calibrationClicks.length ? state.calibrationClicks : calibration ? [calibration.source_a, calibration.source_b] : [];
    if (calPoints.length) { const display = calPoints.map(toDisplaySourcePoint); if (display.length > 1) overlay.append(svg("line", { x1: display[0].x, y1: display[0].y, x2: display[1].x, y2: display[1].y, class: "calibration-line" })); display.forEach((point, index) => { overlay.append(svg("circle", { cx: point.x, cy: point.y, r: sourceMarkerRadius() })); const label = svg("text", { x: point.x + sourceMarkerRadius() * 1.5, y: point.y - sourceMarkerRadius() * 1.5 }); label.textContent = index ? "B" : "A"; overlay.append(label); }); }
  }
  function sourceMarkerRadius() { const box = state.sourceViewBox || { w: 100, h: 100 }; return Math.max(box.w, box.h) / 90; }
  function drawSourcePolyline(overlay, points, label, className) {
    if (!points.length) return; const display = points.map(toDisplaySourcePoint); if (display.length > 1) overlay.append(svg(className.split(" ").includes("room")?"polygon":"polyline", { points: display.map((point) => `${point.x},${point.y}`).join(" "), class: className })); display.forEach((point) => overlay.append(svg("circle", { cx: point.x, cy: point.y, r: sourceMarkerRadius()/3, class: className }))); const text = svg("text", { x: display[0].x + sourceMarkerRadius(), y: display[0].y - sourceMarkerRadius(),style:`font-size:${sourceMarkerRadius()*2}px;stroke-width:${sourceMarkerRadius()/3}px` }); text.textContent = label; overlay.append(text);
  }
  function toDisplaySourcePoint(point) { return M.sourceToDisplay(point, state.sourceCoordinateKind); }
  function renderSourceSelection() {
    const entities=arrays(state.source?.inspection?.selectable_entities).filter(e=>state.sourceSelection.includes(e.handle));
    dom.sourceSelectionSummary.textContent=entities.length ? `Выбрано: ${entities.length}. ${entities.slice(0,4).map(e=>`${e.block_name || e.entity_type} · ${e.layer}`).join("; ")}` : "Щёлкните объект на исходнике. Shift — добавить к выбору.";
    dom.sourceAssignButton.disabled=!entities.length; dom.convertHandlesInput.value=entities.filter(e=>e.room_supported).map(e=>e.handle).join(" ");
    dom.sourceJoinButton.disabled=entities.length<2;
  }
  function joinSourceSelection() {
    if(!state.source || state.source.overlayBlocked)return;
    if(!["room","wall"].includes(dom.sourceEntityKind.value))return toast("Для соединения линий выберите тип «Стена» или «Контур помещения».","error");
    const entities=arrays(state.source.inspection.selectable_entities).filter(e=>state.sourceSelection.includes(e.handle));
    try {const mark=C.markConnectedEntities(entities,dom.sourceEntityKind.value,{label:dom.markupLabel.value.trim(),height_mm:number(dom.sourceOpeningHeightInput.value,2100),sill_mm:number(dom.sourceOpeningSillInput.value,0)});const ok=mutate("соединение линий исходника в контур",()=>{const source=sourceMarkup(true);if(source.marks.some(m=>arrays(m.cad_handles).some(h=>mark.cad_handles.includes(h))))throw new Error("Часть выбранных линий уже размечена. Удалите ненужную отметку или выберите другие линии.");source.marks.push(mark);});if(ok){state.sourceSelection=[];renderSourceSelection();paintSourceOverlay();toast("Линии соединены в одну отметку. Проверьте масштаб и создайте объекты из разметки.","success");}}catch(e){showError(e,"Линии не соединены");}
  }
  function assignSourceSelection() {
    if(!state.selectedFloorId) return toast("Сначала создайте или выберите этаж.","error");
    if(state.source?.overlayBlocked) return toast("Исправьте единицы исходника перед разметкой.","error");
    const selected=arrays(state.source?.inspection?.selectable_entities).filter(e=>state.sourceSelection.includes(e.handle)); if(!selected.length) return;
    const kind=dom.sourceEntityKind.value;
    const ok=mutate("назначение типов CAD-объектам",()=>{const source=sourceMarkup(true); for(const entity of selected) {
      const old=source.marks.find(mark=>arrays(mark.cad_handles).includes(entity.handle));
      if(old?.model_ids?.length) throw new Error("По этому объекту уже создана модель. Измените объект на плане или отмените его создание перед повторным назначением.");
      const mark=C.markEntity(entity,kind,{height_mm:number(dom.sourceOpeningHeightInput.value),sill_mm:number(dom.sourceOpeningSillInput.value)});
      if(!(mark.height_mm>0) || mark.sill_mm<0) throw new Error("Укажите положительную высоту и неотрицательную отметку низа.");
      if(old) source.marks[source.marks.indexOf(old)]=mark; else source.marks.push(mark);
    }});
    if(ok) { state.sourceSelection=[]; renderSourceSelection(); paintSourceOverlay(); toast("Тип назначен. Разметка сохранена в черновике; создание геометрии — отдельной кнопкой.","success"); }
  }
  function buildSourceModel() {
    if(state.sourceDraftPoints.length || state.calibrationClicks.length) return toast("Сначала добавьте текущую отметку или сохраните калибровку; Esc отменяет незавершённую обводку.","error");
    const source=sourceMarkup(false); if(!source) return toast("Сначала разметьте исходник на выбранном этаже.","error");
    if(state.source?.overlayBlocked) return toast("Исправьте единицы или калибровку перед созданием объектов.","error");
    let report; const ok=mutate("создание модели по ручной разметке",project=>{report=C.buildMarkup(project,state.selectedFloorId,source,{wall_height_mm:number(dom.convertHeightInput.value,3000),wall_thickness_mm:number(dom.convertThicknessInput.value,150)});});
    if(ok) { state.view=null; renderPlan(); toast(`Создано объектов: ${report.created_count}. Результат доступен на вкладке «Чертёж»; проверьте размеры и сохраните проект.`,"success",8000); }
  }
  async function convertSource() {
    if(state.source?.backend === "cad_workspace")return setResult(dom.convertResult,"Для этого DXF используйте разметку и «Создать объекты из разметки». Перед этим откалибруйте нужный фрагмент.",false);
    if (!state.source) return setResult(dom.convertResult, "Сначала откройте CAD-файл.", false); if (!isCadSource()) return setResult(dom.convertResult, "Конвертация доступна только для явно выбранных CAD-контуров.", false);
    const handles = dom.convertHandlesInput.value.split(/[\s,;]+/).filter(Boolean); if (!handles.length) return setResult(dom.convertResult, "Укажите хотя бы один handle.", false); if (!canLeave()) return;
    const body = { handles, project_name: `${state.source.filename} — импорт`, room_height_mm: number(dom.convertHeightInput.value, 3000), wall_thickness_mm: number(dom.convertThicknessInput.value, 150) }; if (dom.convertLayerInput.value.trim()) body.layer = dom.convertLayerInput.value.trim(); if (dom.sourceUnitsOverride.value) body.units_override = dom.sourceUnitsOverride.value;
    try { const result = await busy("Создание отдельного проекта…", () => request(`/sources/${encodeURIComponent(state.source.id)}/convert`, { method: "POST", body })); acceptServer(result); setTab("model"); setResult(dom.convertResult, `Создан отдельный проект. Импортировано контуров: ${result.import_report?.imported_count ?? handles.length}.`, true); }
    catch (error) { setResult(dom.convertResult, errorText(error, "Конвертация не выполнена"), false); }
  }
  function sourceMarkup(create = false) {
    if(create && isWorkspaceDxf() && (state.source.overlayBlocked || state.source.loadedViewPage!==state.source.page))throw new Error("Дождитесь загрузки выбранного пространства DXF. Разметка и калибровка пока недоступны.");
    const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; if (!floor || !state.source) return null; if (!floor.properties) floor.properties = {};
    let root = floor.properties.source_markup; if (!root && create) root = floor.properties.source_markup = { schema_version: "source-markup/1.0", sources: [] }; if (!root) return null;
    const page = number(state.source.page, 1); let source = arrays(root.sources).find((item) => item.source_id === state.source.id && number(item.page, 1) === page);
    if(create && isWorkspaceDxf()){const conflict=CW.attachmentConflict(source);if(conflict)throw new Error(conflict);}
    if (!source && create) {
      const dxf = isCadSource(); const resolvedMm = dxf && state.source.inspection.units && state.source.inspection.units.coordinate_units === "mm";
      source = { source_id: state.source.id, filename: state.source.filename, page, coordinate_space: dxf ? (resolvedMm && !isWorkspaceDxf() ? "dxf_world_mm" : "dxf_source_units") : "preview_pixels_v1", preview_version: "1", marks: [] };
      if(isWorkspaceDxf()){source.backend="cad_workspace";source.layout_name=arrays(state.source.inspection.layouts).find(l=>l.page===page)?.name || state.source.layoutName || "";source.requires_calibration=true;}
      if(dxf) {source.units_override=state.source.inspection.units?.override || null;source.unit_factor=state.source.inspection.units?.millimetre_factor || null;}
      if (!dxf && state.source.previewWidth > 0 && state.source.previewHeight > 0) { source.preview_width_px = state.source.previewWidth; source.preview_height_px = state.source.previewHeight; }
      root.sources.push(source);
    }
    if (source && create) {
      source.preview_version = source.preview_version || "1";
      if (!source.coordinate_space) source.coordinate_space = isCadSource() ? (state.source.inspection.units?.coordinate_units === "mm" ? "dxf_world_mm" : "dxf_source_units") : "preview_pixels_v1";
      if (source.coordinate_space === "preview_pixels_v1" && state.source.previewWidth > 0 && state.source.previewHeight > 0) { source.preview_width_px = source.preview_width_px || state.source.previewWidth; source.preview_height_px = source.preview_height_px || state.source.previewHeight; }
    }
    return source;
  }
  function parsePoints(value) { const result = String(value).split(";").map((part) => part.trim()).filter(Boolean).map((part) => { const pair = part.split(",").map((item) => Number(item.trim())); if (pair.length !== 2 || !pair.every(Number.isFinite)) throw new Error(`Не удалось разобрать точку «${part}».`); return { x: pair[0], y: pair[1] }; }); if (!result.length) throw new Error("Укажите хотя бы одну точку."); return result; }
  function addMarkup() {
    if(isStepSource())return;
    if (!state.source) return toast("Сначала инспектируйте исходник.", "error"); if (!state.selectedFloorId) return toast("Выберите этаж.", "error");
    if (state.source.overlayBlocked) return toast("Размер предпросмотра изменился. Сначала выполните повторную калибровку и создайте новую разметку.", "error");
    if (!isCadSource() && !(state.source.previewWidth > 0 && state.source.previewHeight > 0)) return toast("Дождитесь загрузки предпросмотра страницы.", "error");
    try { const mark = { id: M.id(), kind: dom.markupKind.value, label: dom.markupLabel.value.trim() || C.kinds[dom.markupKind.value], points: parsePoints(dom.markupPoints.value), note: dom.markupNote.value.trim(), height_mm:number(dom.sourceOpeningHeightInput.value,2100),sill_mm:number(dom.sourceOpeningSillInput.value,0),exact_geometry:true,closed:dom.markupKind.value === "room",cad_handles:[],model_ids:[] }; const minimum = { room: 3, wall:2,opening: 2,door:2,window:2,passage:2,ignore:1, note: 1 }[mark.kind]; if (mark.points.length < minimum) throw new Error(`Для типа «${C.kinds[mark.kind]}» нужно не менее ${minimum} точек.`); if(!(mark.height_mm>0) || mark.sill_mm<0) throw new Error("Проверьте высоту и отметку низа проёма."); mutate("добавление разметки исходника", () => sourceMarkup(true).marks.push(mark)); state.sourceDraftPoints = []; dom.markupPoints.value = ""; dom.markupNote.value = ""; paintSourceOverlay(); }
    catch (error) { showError(error, "Отметка не добавлена"); }
  }
  function saveCalibration() {
    if(state.source?.overlayBlocked || (isWorkspaceDxf() && state.source.loadedViewPage!==state.source.page))return toast("Сначала дождитесь загрузки выбранного листа или устраните несовместимость старой разметки. Калибровка не изменена.","error");
    if (!state.source || !state.selectedFloorId) return toast("Нужны исходник и выбранный этаж.", "error"); const values = ["calAx", "calAy", "calBx", "calBy", "calDistance", "calOriginX", "calOriginY"].map((id) => number(dom[id].value, NaN)); if (!values.every(Number.isFinite) || !(values[4] > 0)) return toast("Заполните две точки и положительное расстояние.", "error");
    if (values[0] === values[2] && values[1] === values[3]) return toast("Точки A и B должны различаться.", "error");
    if(arrays(sourceMarkup(false)?.marks).some(mark=>mark.model_ids?.length)) return toast("По этой калибровке уже создана модель. Сначала отмените её создание, чтобы менять масштаб.","error");
    mutate("калибровка исходника", () => { sourceMarkup(true).calibration = { source_a: { x: values[0], y: values[1] }, source_b: { x: values[2], y: values[3] }, distance_mm: values[4], origin_mm: { x: values[5], y: values[6] }, y_axis_down: !isCadSource() }; }); state.calibrationClicks = []; paintSourceOverlay();
  }
  function renderAttachedSources() {
    if (!dom.attachedSourceSelect) return; const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; const sources = arrays(floor?.properties?.source_markup?.sources); dom.attachedSourceSelect.replaceChildren();
    if (!sources.length) { const option = node("option", "", "Нет приложенных исходников"); option.value = ""; dom.attachedSourceSelect.append(option); dom.loadAttachedSourceButton.disabled = true; return; }
    sources.forEach((source, index) => { const option = node("option", "", `${source.filename} · ${source.layout_name || `стр. ${source.page}`}`); option.value = String(index); dom.attachedSourceSelect.append(option); }); dom.loadAttachedSourceButton.disabled = false;
  }
  async function loadAttachedSource() {
    if(!canChangeSource()) return;
    const floor = M.findFloor(state.project, state.selectedFloorId)?.floor; const attachments = arrays(floor?.properties?.source_markup?.sources); const attachment = attachments[number(dom.attachedSourceSelect.value, -1)]; if (!attachment) return;
    if(attachment.backend === "cad_workspace")return openCadAsset(attachment.source_id,attachment.page);
    const token=++sourceLoadSequence;
    try { const override = attachment.units_override || dom.sourceUnitsOverride.value; dom.sourceUnitsOverride.value=override || ""; const query = override ? `?units_override=${encodeURIComponent(override)}` : ""; const result = await busy("Загрузка приложенного исходника…", () => request(`/sources/${encodeURIComponent(attachment.source_id)}${query}`));if(token!==sourceLoadSequence)return;adoptSource({...result,source_id:attachment.source_id},result.source.filename || attachment.filename,null,attachment.page); }
    catch (error) { if(token===sourceLoadSequence)setResult(dom.sourceInspectionResult, errorText(error, "Не удалось открыть приложенный исходник"), false); }
  }
  function renderMarkupList() {
    if (!dom.markupList) return; dom.markupList.replaceChildren(); const source = sourceMarkup(false); if (!source || !arrays(source.marks).length) { dom.markupList.append(node("p", "empty-note", "На текущем этаже нет отметок этого исходника.")); return; }
    if(isWorkspaceDxf()){const conflict=CW.attachmentConflict(source);if(conflict){dom.markupList.append(node("p","warning-text",conflict));return;}}
    arrays(source.marks).forEach((mark) => { const row = node("div", "compact-row"); row.append(node("span", "", `${C.kinds[mark.kind]}: ${mark.label} · ${mark.points.length} т.${mark.model_ids?.length ? " · создано в модели" : mark.exact_geometry === false ? " · нужна обводка" : ""}`));
      const kind=node("select"); kind.setAttribute("aria-label",`Тип отметки ${mark.label}`); Object.entries(C.kinds).forEach(([value,label])=>{const option=node("option","",label);option.value=value;option.selected=value===mark.kind;kind.append(option);}); kind.disabled=Boolean(mark.model_ids?.length); kind.addEventListener("change",()=>mutate("изменение типа отметки",()=>{const minimum=kind.value === "room" ? 3 : ["note","ignore"].includes(kind.value) ? 1 : 2;if(mark.points.length<minimum) throw new Error(`Этому типу нужно минимум ${minimum} точек. Создайте новую обводку.`); mark.kind=kind.value;}));
      const remove = node("button", "icon-button", "×"); remove.type = "button"; remove.title="Удалить только отметку; созданная геометрия останется"; remove.disabled=Boolean(mark.model_ids?.length); remove.addEventListener("click", () => mutate("удаление отметки", () => { source.marks = source.marks.filter((item) => item.id !== mark.id); })); row.append(kind,remove); dom.markupList.append(row); });
  }
  function resetSourceAttachment() {
    const attachment = sourceMarkup(false); if (!attachment || !state.source || isCadSource()) return;
    if (!window.confirm("Удалить сохранённые отметки и калибровку этой страницы из локального черновика? Действие можно отменить общей кнопкой «Отменить».")) return;
    mutate("сброс разметки изменившейся страницы", () => { attachment.marks = []; attachment.calibration = null; attachment.coordinate_space = "preview_pixels_v1"; attachment.preview_version = "1"; attachment.preview_width_px = state.source.previewWidth; attachment.preview_height_px = state.source.previewHeight; }); state.source.overlayBlocked = false; renderSourcePreview();
  }

  async function validateCatalog() {
    const file = dom.catalogFileInput.files[0]; state.catalog = null; state.catalogValid = false; dom.attachCatalogButton.disabled = true; if (!file) return setResult(dom.catalogResult, "Выберите JSON каталога.", false);
    try { const catalog = JSON.parse(await file.text()); const result = await busy("Проверка каталога…", () => request("/node-catalog/validate", { method: "POST", body: { catalog } })); state.catalog = result.normalized || null; state.catalogValid = result.valid === true && Boolean(result.normalized); dom.attachCatalogButton.disabled = !state.catalogValid; const issues = arrays(result.issues); const sampleNote = result.normalized && result.normalized.sample ? " Это учебный образец: он не даёт готовность к выпуску." : ""; const preview = result.normalized ? ` «${result.normalized.name}», ревизия ${result.normalized.revision}, узлов: ${arrays(result.normalized.nodes).length}.` : ""; setResult(dom.catalogResult, state.catalogValid ? `Кандидат прошёл проверку.${preview}${issues.length ? ` Замечаний: ${issues.length}.` : ""} Он ещё не приложен к проекту.${sampleNote}` : `Кандидат отклонён: ${issues[0]?.message || "ошибка схемы"}`, state.catalogValid); }
    catch (error) { setResult(dom.catalogResult, errorText(error, "Каталог не прошёл проверку"), false); }
  }
  function attachCatalog() { if (!state.catalogValid || !state.catalog) return; mutate("приложение кандидата каталога", (project) => { project.properties = project.properties || {}; project.properties.node_catalog = M.clone(state.catalog); }); setResult(dom.catalogResult, "Проверенный кандидат приложен к локальному черновику. Сохраните проект, чтобы зафиксировать его.", true); }

  function changeDocumentFormat() {
    [dom.prepareSpecialButton,dom.prepareWholeWallButton].forEach(button=>{if(button)button.hidden=dom.documentFormat.value==="PANELS";});
    state.sheetContext = null; state.engineeringStale = true;
    dom.sheetPreview.replaceChildren(node("p", "empty-note", "Загружаем листы выбранного комплекта…"));
    dom.sheetList.replaceChildren();
    if (dom.sheetPreviewTitle) dom.sheetPreviewTitle.textContent = "Загружаем выбранный комплект…";
    if (dom.sheetPreviousButton) dom.sheetPreviousButton.disabled = true;
    if (dom.sheetNextButton) dom.sheetNextButton.disabled = true;
    return loadEngineering();
  }
  async function loadEngineering() {
    if (!state.project) return;
    const projectId = state.project.id, revision = state.baseRevision, format = dom.documentFormat.value, sequence = state.engineeringSequence = (state.engineeringSequence || 0) + 1;
    const query = `?revision=${encodeURIComponent(revision)}`, current = () => state.project?.id === projectId && state.baseRevision === revision && dom.documentFormat.value === format && state.engineeringSequence === sequence;
    try { const [report, sheets] = await busy("Расчёт развёрток…", () => Promise.all([format === "PANELS" ? Promise.resolve({issues:[],readiness:{purpose:"client_presentation"}}) : request(`/projects/${encodeURIComponent(projectId)}/engineering${query}`), request(`/projects/${encodeURIComponent(projectId)}/sheets${query}&format=${encodeURIComponent(format)}`)])); if (!current()) return; state.sheetContext = { projectId, revision, format }; state.engineeringStale = state.dirty; renderEngineering(report, sheets); }
    catch (error) { if (current()) setResult(dom.engineeringSummary, errorText(error, "Производные данные недоступны"), false); }
  }
  function renderEngineering(report, sheetData) {
    dom.engineeringJson.textContent = json(report);
    const issues = arrays(report.issues); const summary = report.issue_summary || {};
    const errors = issues.filter((issue) => issue.severity === "error").length || number(summary.error_count, 0);
    const warnings = issues.filter((issue) => issue.severity === "warning").length || number(summary.warning_count, 0);
    const readiness = report.readiness || sheetData.readiness || {};
    const productionReady = readiness.production_ready === true || report.production_ready === true;
    const blockingCodes = [...new Set([...arrays(readiness.blocking_issue_codes), ...issues.filter((issue) => issue.severity === "error").map((issue) => issue.code)].filter(Boolean))];
    dom.engineeringSummary.replaceChildren(node("p", "", `Сохранённая версия r${state.baseRevision} · Листов: ${arrays(sheetData.sheets).length} · Замечания: ${errors + warnings}.`));
    dom.engineeringSummary.classList.remove("is-error", "is-success");
    dom.engineeringSummary.setAttribute("data-production-ready", String(productionReady));
    if (state.dirty) dom.engineeringSummary.append(node("p", "form-hint", "Есть несохранённые изменения. Листы рассчитаны по сохранённой версии."));
    if (issues.length || blockingCodes.length || errors || warnings) {
      const notes = node("details", "engineering-notes"); notes.append(node("summary", "", `Примечания к проверке · ${errors + warnings}`));
      issues.forEach(issue => {const message = node("p", "form-hint", issue.message || issue.code || "Уточните данные проекта"); message.setAttribute("data-issue-code", issue.code || ""); message.setAttribute("data-issue-severity", issue.severity || ""); notes.append(message);});
      if (blockingCodes.length) {const codes = node("details"); codes.append(node("summary", "", "Коды проверок"), node("p", "form-hint", blockingCodes.join(", "))); notes.append(codes);}
      dom.engineeringSummary.append(notes);
    }
    const sheetKinds={cad_section:"Сечение из STEP",wall_mount:"Развёртки монтажа",wall_mount_assembly:"Сборка узла · сечение",wall_mount_detail:"Узел примыкания · схема",wall_mount_nodes:"Ведомость узлов",wall_mount_quantities:"Ведомость облицовки",wall_mount_register:"Ведомость стен",wall_mount_requirements:"Условия монтажа",wall_mount_pieces:"Участки панелей",assembly_components:"Состав сборок",assembly_machining:"Пазы и выборки панелей",assembly_assumptions:"Посадки и происхождение",assembly_notes:"Примечания к сборкам",assembly_decisions:"Проектные решения",assembly_stale:"Сборки, требующие пересчёта",cover:"Обложка",contents:"Содержание",general:"Общие данные",plan:"План помещений",elevation:"Развёртка",elevations:"Развёртки стен",openings:"Двери, окна и проёмы",mount_checks:"Проверки монтажа",issues:"Замечания к проекту",table:"Ведомость",specification:"Спецификация",nodes:"Узлы",node:"Узел",node_candidate:"Узел из библиотеки",manual:"Методические указания",readiness:"Проверки документации"};
    const sheets = state.engineeringSheets = arrays(sheetData.sheets); state.selectedSheetNumber = null;
    if (dom.sheetListTitle) dom.sheetListTitle.textContent = `Листы · ${sheets.length}`;
    Object.assign(sheetKinds,{client_cover:"Для заказчика",client_plan:"План раскладок",client_elevations:"Цветные раскладки панелей",client_schedule:"Ведомость панелей"});
    dom.sheetList.replaceChildren(); dom.sheetList.scrollTop = 0; sheets.forEach((sheet) => { const button = node("button", "sheet-item"); button.type = "button"; button.setAttribute("data-sheet-number", String(sheet.number)); button.setAttribute("aria-current", "false"); button.setAttribute("aria-controls", "sheetPreview"); button.append(node("strong", "", `${sheet.number}. ${sheet.title}`), node("span", "", sheetKinds[sheet.kind] || "Лист документации")); button.addEventListener("click", () => showSheet(sheet, {reveal: true})); dom.sheetList.append(button); });
    if (sheets.length) showSheet(sheets[0]);
    else {dom.sheetList.append(node("p", "empty-note", "Листы не сформированы.")); dom.sheetPreview?.replaceChildren(node("p", "empty-note", "В выбранном комплекте пока нет листов.")); if (dom.sheetPreviewTitle) dom.sheetPreviewTitle.textContent = "Нет листов"; if (dom.sheetPreviousButton) dom.sheetPreviousButton.disabled = true; if (dom.sheetNextButton) dom.sheetNextButton.disabled = true;}
  }
  function showSheet(sheet, options = {}) {
    const context = state.sheetContext;
    if (!context || !state.project || context.projectId !== state.project.id || context.revision !== state.baseRevision || context.format !== dom.documentFormat.value) { setResult(dom.sheetPreview, "Список листов изменился. Обновите листы выбранного комплекта.", false); return; }
    const sheets = state.engineeringSheets || [], position = sheets.findIndex(item => String(item.number) === String(sheet.number)); state.selectedSheetNumber = sheet.number;
    if (dom.sheetPreviewTitle) dom.sheetPreviewTitle.textContent = `${position >= 0 ? `${position + 1} / ${sheets.length} · ` : ""}${sheet.title}`;
    if (dom.sheetPreviousButton) dom.sheetPreviousButton.disabled = position <= 0;
    if (dom.sheetNextButton) dom.sheetNextButton.disabled = position < 0 || position >= sheets.length - 1;
    (dom.sheetList?.querySelectorAll?.("button[data-sheet-number]") || []).forEach(button => button.setAttribute("aria-current", button.getAttribute("data-sheet-number") === String(sheet.number) ? "page" : "false"));
    dom.sheetPreview.replaceChildren(); dom.sheetPreview.classList.toggle("is-stale", state.dirty);
    const image = node("img"); image.alt = `Лист ${sheet.number}: ${sheet.title}`;
    const source = sheet.svg_url && sheet.svg_url.startsWith("/api/") ? sheet.svg_url : `${API}/projects/${encodeURIComponent(context.projectId)}/sheets/${encodeURIComponent(sheet.number)}.svg`;
    const url = new URL(source, window.location.origin); url.searchParams.set("revision", context.revision); url.searchParams.set("format", context.format); image.src = url.pathname + url.search;
    image.addEventListener("error", () => {if (state.sheetContext === context && dom.sheetPreview.contains(image)) setResult(dom.sheetPreview, "Лист не удалось загрузить.", false);});
    dom.sheetPreview.append(image); if (state.dirty) dom.sheetPreview.append(node("span", "stale-label", "Сохранённая версия — локальный черновик не включён"));
    if (options.reveal) dom.sheetPreviewTitle?.scrollIntoView?.({block: "nearest", behavior: "smooth"});
  }
  function showAdjacentSheet(direction) {
    const sheets = state.engineeringSheets || [], index = sheets.findIndex(sheet => String(sheet.number) === String(state.selectedSheetNumber)), next = index + direction;
    if (index < 0 || next < 0 || next >= sheets.length) return;
    showSheet(sheets[next], {reveal: true});
    const control = [...(dom.sheetList?.querySelectorAll?.("button[data-sheet-number]") || [])].find(button => button.getAttribute("data-sheet-number") === String(state.selectedSheetNumber));
    if (control?.getBoundingClientRect && dom.sheetList?.getBoundingClientRect) {
      const row = control.getBoundingClientRect(), list = dom.sheetList.getBoundingClientRect();
      if (row.top < list.top) dom.sheetList.scrollTop += row.top - list.top;
      else if (row.bottom > list.bottom) dom.sheetList.scrollTop += row.bottom - list.bottom;
    }
  }
  async function prepareDocumentAssemblies() {
    if (!state.project) return false;
    if (state.dirty || state.editorDirty || mountPreparationHasUnappliedInputs()) {
      toast("Сначала примените правки и нажмите «Проверить и сохранить». Тогда в альбом попадёт текущий монтаж.", "error", 10000);
      return false;
    }
    // Customer layouts need no native CAD or background engineering jobs.
    if (dom.documentFormat.value === "PANELS") return true;
    const id = state.project.id;
    if (backgroundBuilds) {
      try {
        const job = await backgroundBuilds.start();
        if (!job || state.project?.id !== id || state.dirty || state.editorDirty || mountPreparationHasUnappliedInputs()) return false;
        if (job.requires_reload) {toast(job.message, "error", 12000); return false;}
        if (!window.VioletEngineeringJobs.COMPLETE.has(job.status)) {
          toast("Проект сохранён. Альбом будет доступен после фоновой сборки узлов и стен; ход расчёта показан над чертежом.", "info", 10000);
          return false;
        }
        if (job.status === "partial") toast("Часть сборок не рассчитана. Причины показаны в статусе расчёта и в отчёте проекта.", "error", 10000);
        return true;
      } catch (error) {showError(error, "Не удалось проверить готовность сборок"); return false;}
    }
    const prepared = await prepareMountAssembliesForProject({scope: null});
    if (state.project?.id !== id) return false;
    if (["error", "stale"].includes(prepared.status)) {
      toast(prepared.message || "Подготовка узлов не завершена. Повторите экспорт.", "error", 12000);
      return false;
    }
    if (prepared.status === "applied") {
      // Only the guarded derived geometry was added to the clean snapshot.
      // Persist it as an ordinary version before requesting revision-bound
      // sheets; no hidden live-CAD work is performed by the PDF/DXF exporter.
      await saveDraft();
      if (state.project?.id !== id || state.dirty) return false;
    }
    return !state.dirty && !state.editorDirty;
  }
  async function downloadDocument(kind) {
    if (!state.project || !["pdf", "dxf", "zip"].includes(kind)) return;
    if (!await prepareDocumentAssemblies()) return;
    const projectId = state.project.id; const revision = state.baseRevision; const projectName = state.project.metadata.name; const format = dom.documentFormat.value;
    try { const blob = await busy(`Формирование ${kind.toUpperCase()}…`, () => requestBlob(`/projects/${encodeURIComponent(projectId)}/documents.${kind}?format=${encodeURIComponent(format)}&revision=${encodeURIComponent(revision)}`)); downloadBlob(blob, `${safeFilename(projectName)}_r${revision}_${format}.${kind}`); }
    catch (error) { showError(error, `Не удалось скачать ${kind.toUpperCase()}`); }
  }

  init();
})();
