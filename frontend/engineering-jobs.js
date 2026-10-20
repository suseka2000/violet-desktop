(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletEngineeringJobs = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const DERIVED_KEYS = ["mount_assemblies", "whole_wall_assemblies"];
  const ACTIVE = new Set(["queued", "running", "pending"]);
  const COMPLETE = new Set(["ready", "partial"]);
  function sourceProject(project) {
    if (!project) return project;
    const properties = {...project.properties};
    const targets = new Set((properties.wall_mounting?.walls || []).map(item => `${item.wall_id}:${item.face_id}`));
    const settings = new Map((properties.whole_wall_settings?.walls || []).filter(item => targets.has(`${item.wall_id}:${item.face_id}`)).map(item => [`${item.wall_id}:${item.face_id}`, item]));
    for (const item of properties.whole_wall_assemblies?.items || []) {
      const key = `${item.wall_id}:${item.face_id}`;
      if (targets.has(key) && item.configuration && !settings.has(key)) settings.set(key, {wall_id:item.wall_id,face_id:item.face_id,configuration:item.configuration});
    }
    if (settings.size || properties.whole_wall_settings) properties.whole_wall_settings = {schema_version:"whole-wall-settings/1.0",walls:[...settings.values()]};
    for (const key of DERIVED_KEYS) delete properties[key];
    return {...project, properties};
  }
  function mergeDerived(project, properties) {
    const result = sourceProject(project);
    for (const key of DERIVED_KEYS) {
      if (properties?.[key]) result.properties[key] = properties[key];
    }
    return result;
  }
  function message(job) {
    if (job.message) return job.message;
    if (ACTIVE.has(job.status)) return "Проект сохранён. Узлы и стены рассчитываются в фоне — можно продолжать работу.";
    if (job.status === "ready") return "Узлы и стены готовы. Документацию можно скачать.";
    if (job.status === "partial") return "Расчёт завершён частично. Проверьте причины в сборках узлов и стен.";
    if (job.status === "failed") return "Проект сохранён, но расчёт сборок не завершён. Можно повторить расчёт.";
    return "Сохранённый монтаж ещё не рассчитан. Запустите подготовку сборок.";
  }
  function create({request, getProject, getRevision, canApply, onResult, onStatus,
    schedule = callback => setTimeout(callback, 2000), cancel = clearTimeout} = {}) {
    let sequence = 0, timer = null, observed = null, applied = null;
    function stop() {sequence++; if (timer !== null) cancel(timer); timer = null; observed = null; applied = null;}
    function remoteRevisionConflict(remoteRevision, expectedRevision) {
      sequence++; if (timer !== null) cancel(timer); timer = null; applied = null;
      observed = {status:"revision_conflict",requires_reload:true,revision:remoteRevision,expected_revision:expectedRevision,
        message:`Проект на сервере изменён${Number.isInteger(remoteRevision) ? ` до r${remoteRevision}` : ""}. Загрузите актуальную версию проекта. Текущий черновик не заменён; перед загрузкой его можно скачать.`};
      onStatus(observed, message(observed)); return observed;
    }
    function watch(initial, {included = false} = {}) {
      stop();
      const project = getProject(); if (!project) return;
      const token = sequence, id = project.id, revision = getRevision();
      const current = () => token === sequence && getProject()?.id === id && getRevision() === revision;
      const path = `/projects/${encodeURIComponent(id)}`;
      if (included && initial?.revision === revision && COMPLETE.has(initial.status)
          && initial.input_signature && initial.job_id) {
        applied = `${revision}:${initial.input_signature}:${initial.job_id}:${initial.status}`;
      }
      const later = () => {if (current()) timer = schedule(() => poll());};
      async function poll(first) {
        if (!current()) return null;
        try {
          const job = first || await request(`${path}/assembly-job`);
          if (!current()) return null;
          if (Number.isInteger(job.revision) && job.revision !== revision) return remoteRevisionConflict(job.revision, revision);
          observed = job;
          if (COMPLETE.has(job.status) && job.input_signature && canApply()) {
            const key = `${revision}:${job.input_signature}:${job.job_id || ""}:${job.status}`;
            if (applied !== key) {
              const result = await request(`${path}/assembly-result`);
              if (!current()) return null;
              if (Number.isInteger(result.revision) && result.revision !== revision) return remoteRevisionConflict(result.revision, revision);
              if (Number.isInteger(result.engineering?.revision) && result.engineering.revision !== revision) return remoteRevisionConflict(result.engineering.revision, revision);
              if (!canApply()) {later(); return {...job, status:"waiting_to_apply"};}
              const finished = result.engineering;
              if (result.revision !== revision || result.input_signature !== job.input_signature || !COMPLETE.has(finished?.status) || finished.input_signature !== job.input_signature || (job.job_id && finished.job_id !== job.job_id)) {
                observed = finished || {status:"running",message:"Ожидаем актуальный результат сборки."};
                onStatus(observed, message(observed)); later(); return {...observed,status:ACTIVE.has(observed.status) ? observed.status : "waiting_to_apply"};
              }
              onResult(result); applied = key;
            }
          }
          onStatus(job, message(job));
          if (current() && (ACTIVE.has(job.status) || (COMPLETE.has(job.status) && !canApply()))) later();
          return job;
        } catch (error) {
          if (current()) {
            onStatus({status: "failed"}, `Не удалось проверить расчёт: ${error.message}. Сохранённый проект не потерян.`);
            timer = schedule(() => poll());
          }
          return null;
        }
      }
      return poll(initial);
    }
    async function start({force = false} = {}) {
      const project = getProject(); if (!project) return null;
      const id = project.id, revision = getRevision();
      let job;
      try {job = await request(`/projects/${encodeURIComponent(id)}/assembly-job`, {method: "POST", body: {expected_revision: revision, ...(force ? {force:true} : {})}});}
      catch (error) {
        if (getProject()?.id === id && getRevision() === revision && error.status === 409 && error.code === "revision_conflict") return remoteRevisionConflict(error.details?.actual_revision, revision);
        throw error;
      }
      if (getProject()?.id !== id || getRevision() !== revision) return null;
      return watch(job);
    }
    return {watch, start, stop, status: () => observed};
  }
  return {DERIVED_KEYS, ACTIVE, COMPLETE, sourceProject, mergeDerived, message, create};
});
