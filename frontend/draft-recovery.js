(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletDraftRecovery = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const PREFIX = "violet-rd:draft:1:";
  const MAX_BYTES = 3 * 1024 * 1024;
  const MAX_RECORDS = 20;
  const IDENTIFIER = /^[a-zA-Z0-9_-]{1,100}$/;
  const error = message => ({ok:false, error:message});
  const isProject = p => p && typeof p === "object" && !Array.isArray(p)
    && typeof p.id === "string" && IDENTIFIER.test(p.id)
    && Number.isInteger(p.revision) && p.revision >= 0
    && p.metadata && typeof p.metadata === "object" && !Array.isArray(p.metadata)
    && Array.isArray(p.buildings);
  const byteLength = text => new TextEncoder().encode(text).length;

  // This is a local recovery copy, not a database version or a full backup.
  // Every tab has its own key. Never evict a different tab's unsaved work.
  function createStore({storage, writerId, now = () => Date.now()} = {}) {
    if (!IDENTIFIER.test(writerId || "")) throw new Error("Некорректный идентификатор вкладки");
    const keyFor = id => `${PREFIX}${id}:${writerId}`;
    const keys = () => {
      const result = [];
      if (!storage) throw new Error("Хранилище браузера недоступно");
      if (storage.length > 10000) throw new Error("Слишком много записей в хранилище браузера");
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        if (typeof key === "string" && key.startsWith(PREFIX)) result.push(key);
      }
      return result;
    };
    const parse = (key, token) => {
      if (!token || byteLength(token) > MAX_BYTES) throw new Error("Неверный размер черновика");
      const value = JSON.parse(token);
      if (value.schema_version !== "draft-recovery/1.0" || !isProject(value.project)
          || !IDENTIFIER.test(value.writer_id || "") || !Number.isInteger(value.base_revision)
          || value.base_revision < 0 || value.project.revision !== value.base_revision
          || !Number.isSafeInteger(value.saved_at) || value.saved_at < 0
          || key !== `${PREFIX}${value.project.id}:${value.writer_id}`) {
        throw new Error("Неверный формат черновика");
      }
      return {key, token, project:value.project, baseRevision:value.base_revision,
        savedAt:value.saved_at, writerId:value.writer_id};
    };
    function list(serverProject) {
      const entries = [], errors = [];
      if (!isProject(serverProject)) return {entries, errors:["Проект ещё не загружен"]};
      try {
        for (const key of keys()) {
          if (!key.startsWith(`${PREFIX}${serverProject.id}:`)) continue;
          try {
            const entry = parse(key, storage.getItem(key));
            entry.canRestore = entry.baseRevision === serverProject.revision;
            entries.push(entry);
          } catch (_) {
            errors.push("Повреждённая локальная копия пропущена и оставлена в хранилище.");
          }
        }
      } catch (_) { errors.push("Хранилище браузера недоступно. Скачайте черновик JSON перед закрытием."); }
      entries.sort((a,b) => b.savedAt - a.savedAt || a.key.localeCompare(b.key));
      return {entries, errors};
    }
    function checkpoint(baseProject, draft) {
      if (!isProject(baseProject) || !isProject(draft) || baseProject.id !== draft.id
          || baseProject.revision !== draft.revision) return error("Черновик не соответствует исходной версии проекта.");
      try {
        const projectJson = JSON.stringify(draft);
        const key = keyFor(draft.id), previous = storage?.getItem(key);
        if (previous) {
          try {
            const existing = parse(key, previous);
            if (existing.baseRevision === baseProject.revision && JSON.stringify(existing.project) === projectJson)
              return {ok:true, entry:existing, unchanged:true};
            // A delayed callback from an older project version must not replace a newer copy.
            if (existing.baseRevision > baseProject.revision) return error("Более новый черновик уже сохранён. Скачайте текущие изменения отдельно.");
          } catch (_) {
            return error("Предыдущая локальная копия повреждена и не перезаписана. Скачайте текущий черновик JSON.");
          }
        }
        if (!previous && keys().length >= MAX_RECORDS) return error("Лимит локальных черновиков достигнут. Скачайте JSON или удалите ненужные копии через восстановление.");
        const token = JSON.stringify({schema_version:"draft-recovery/1.0", writer_id:writerId,
          base_revision:baseProject.revision, saved_at:now(), project:JSON.parse(projectJson)});
        if (byteLength(token) > MAX_BYTES) return error("Черновик слишком большой для автоматической копии (3 МиБ). Скачайте JSON или сохраните проект.");
        // setItem is atomic: a quota/security failure leaves the preceding copy untouched.
        storage.setItem(key, token);
        return {ok:true, entry:parse(key, token)};
      } catch (_) { return error("Не удалось сохранить локальную копию. Скачайте JSON или сохраните проект перед закрытием."); }
    }
    function discard(entry) {
      try {
        if (!entry || !entry.key?.startsWith(PREFIX)) return error("Некорректная локальная копия.");
        parse(entry.key, entry.token);
        const current = storage.getItem(entry.key);
        if (current === null) return {ok:true};
        if (current !== entry.token) return error("Черновик уже изменился. Обновите список, прежде чем удалять его.");
        storage.removeItem(entry.key);
        return {ok:true};
      } catch (_) { return error("Не удалось удалить локальную копию."); }
    }
    function clearSaved(project) {
      if (!isProject(project)) return error("Проект ещё не загружен.");
      try {
        const key = keyFor(project.id), token = storage.getItem(key);
        if (token === null) return {ok:true};
        const entry = parse(key, token);
        // Prefer discard(the entry captured when Save began) for asynchronous saves.
        return entry.baseRevision < project.revision ? discard(entry) : {ok:true};
      } catch (_) { return error("Не удалось убрать сохранённый черновик."); }
    }
    return {checkpoint, list, discard, clearSaved};
  }
  return {createStore, MAX_BYTES, MAX_RECORDS};
});
