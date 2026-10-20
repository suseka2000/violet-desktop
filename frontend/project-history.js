(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VioletProjectHistory = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const collections = ["mount_assemblies", "whole_wall_assemblies"];
  const payloadFields = ["geometry", "geometry_snapshot", "assembled_step", "plan", "spec",
    "bodies", "collisions", "fastening_checks", "intentional_engagements", "fastener_assembly", "contact_checks"];
  const sealed = new WeakSet();
  const payloadRoots = new WeakSet(), canonicalPayloads = new WeakMap();
  function seal(value) {
    if (!value || typeof value !== "object" || sealed.has(value)) return value;
    sealed.add(value);
    for (const key of Object.keys(value)) seal(value[key]);
    return Object.freeze(value);
  }
  function capture(project) {
    if (!project) return {json: JSON.stringify(project), payloads: []};
    const properties = {...project.properties}, payloads = [];
    for (const name of collections) {
      const collection = properties[name];
      if (!collection || !Array.isArray(collection.items)) continue;
      properties[name] = {...collection, items: collection.items.map((record, index) => {
        if (!record || typeof record !== "object" || Array.isArray(record)) return record;
        const item = {...record};
        for (const field of payloadFields) if (Object.hasOwn(item, field)) {
          // Native results are replaced as a whole. Share their immutable data
          // between undo entries; configurations/labels remain editable copies.
          const value = seal(item[field]);
          if (value && typeof value === "object") payloadRoots.add(value);
          payloads.push({name, index, field, value});
          delete item[field];
        }
        return item;
      })};
    }
    return {json: JSON.stringify({...project, properties}), payloads};
  }
  function restore(snapshot) {
    if (typeof snapshot === "string") return JSON.parse(snapshot);
    const project = JSON.parse(snapshot.json);
    for (const {name, index, field, value} of snapshot.payloads) project.properties[name].items[index][field] = value;
    return project;
  }
  function equal(a, b) {
    if (typeof a === "string" || typeof b === "string") return a === b;
    return a.json === b.json && a.payloads.length === b.payloads.length && a.payloads.every((item, index) => {
      const other = b.payloads[index];
      // Explicit JSON editing can replace a payload with identical data. The
      // normal edit/undo path stays O(1); compare values only for new references.
      return item.name === other.name && item.index === other.index && item.field === other.field &&
        (item.value === other.value || stableKey(item.value) === stableKey(other.value));
    });
  }
  function clone(project) {return restore(capture(project));}
  function stableKey(input) {
    const active = new WeakSet();
    function serialize(value, key) {
      if (value && typeof value === "object" && canonicalPayloads.has(value)) return canonicalPayloads.get(value);
      const original = value;
      if (value && typeof value.toJSON === "function") value = value.toJSON(key);
      if (!value || typeof value !== "object") return JSON.stringify(value);
      if (active.has(value)) throw new TypeError("Circular project value");
      active.add(value);
      let result;
      if (Array.isArray(value)) result = `[${Array.from({length:value.length},(_,index) => serialize(value[index],String(index)) ?? "null").join(",")}]`;
      else {
        const entries = [];
        // JSON enumerates integer property names numerically even after the
        // canonical replacer inserted all keys in lexical order.
        const keys = Object.keys(Object.fromEntries(Object.keys(value).sort().map(name => [name, 0])));
        for (const name of keys) {
          const item = serialize(value[name], name);
          if (item !== undefined) entries.push(`${JSON.stringify(name)}:${item}`);
        }
        result = `{${entries.join(",")}}`;
      }
      active.delete(value);
      if (original && typeof original === "object" && payloadRoots.has(original)) canonicalPayloads.set(original, result);
      return result;
    }
    return serialize(input, "");
  }
  return {capture, restore, equal, clone, stableKey};
});
