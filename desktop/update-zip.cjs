'use strict';
// Inspect ZIP metadata before handing an authenticated archive to macOS ditto.
// No third-party executable/library is downloaded by the updater.
const fs = require('node:fs/promises');
const path = require('node:path');
const {inflateRawSync} = require('node:zlib');
const decoder = new TextDecoder('utf-8',{fatal:true});
const invalid = message => new Error(`Небезопасный ZIP обновления: ${message}`);
function safeNumber(value) {
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw invalid('слишком большое смещение');
  return Number(value);
}
async function readAt(file, offset, size) {
  const buffer = Buffer.alloc(size);
  const {bytesRead} = await file.read(buffer,0,size,offset);
  if (bytesRead !== size) throw invalid('обрезанный архив');
  return buffer;
}
function entryName(buffer) {
  let name;
  try { name = decoder.decode(buffer); } catch { throw invalid('имя файла не UTF-8'); }
  if (!name || /[\\\0\r\n]/.test(name) || name.startsWith('/') || name.includes(':')) throw invalid('путь файла');
  const components = name.replace(/\/$/,'').split('/');
  if (components.some(part => !part || part === '.' || part === '..')) throw invalid('выход за каталог');
  return name;
}
function zip64Values(extra, needed) {
  for (let offset = 0; offset + 4 <= extra.length;) {
    const tag = extra.readUInt16LE(offset), size = extra.readUInt16LE(offset+2);
    offset += 4;
    if (offset+size > extra.length) throw invalid('повреждённое дополнительное поле');
    if (tag === 1) {
      if (size < needed*8) throw invalid('обрезанный ZIP64');
      return Array.from({length:needed},(_,i) => safeNumber(extra.readBigUInt64LE(offset+i*8)));
    }
    offset += size;
  }
  throw invalid('нет метаданных ZIP64');
}
async function inspectZip(filename) {
  const file = await fs.open(filename,'r');
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 22) throw invalid('не файл ZIP');
    const tailSize = Math.min(stat.size,65557), tailStart = stat.size-tailSize;
    const tail = await readAt(file,tailStart,tailSize);
    let end = -1;
    for (let i = tail.length-22; i >= 0; i--) {
      if (tail.readUInt32LE(i) === 0x06054b50 && i+22+tail.readUInt16LE(i+20) === tail.length) { end=i; break; }
    }
    if (end < 0 || tail.readUInt16LE(end+4) !== 0 || tail.readUInt16LE(end+6) !== 0)
      throw invalid('нет единого центрального каталога');
    let count = tail.readUInt16LE(end+10), centralSize=tail.readUInt32LE(end+12), centralStart=tail.readUInt32LE(end+16);
    if (count === 0xffff || centralSize === 0xffffffff || centralStart === 0xffffffff) {
      const locator = await readAt(file,tailStart+end-20,20);
      if (locator.readUInt32LE(0) !== 0x07064b50 || locator.readUInt32LE(4) !== 0 || locator.readUInt32LE(16) !== 1)
        throw invalid('многотомный ZIP64');
      const zip64 = await readAt(file,safeNumber(locator.readBigUInt64LE(8)),56);
      if (zip64.readUInt32LE(0) !== 0x06064b50 || zip64.readUInt32LE(16) !== 0 || zip64.readUInt32LE(20) !== 0)
        throw invalid('центральный каталог ZIP64');
      count=safeNumber(zip64.readBigUInt64LE(32));
      centralSize=safeNumber(zip64.readBigUInt64LE(40));
      centralStart=safeNumber(zip64.readBigUInt64LE(48));
    }
    if (!count || centralStart < 0 || centralStart+centralSize > tailStart+end || count*46 > centralSize)
      throw invalid('границы центрального каталога');
    const entries = [], names = new Set();
    let cursor=centralStart, uncompressedBytes=0;
    for (let i=0; i<count; i++) {
      const header=await readAt(file,cursor,46);
      if (header.readUInt32LE(0)!==0x02014b50) throw invalid('повреждённый центральный каталог');
      const flags=header.readUInt16LE(8), method=header.readUInt16LE(10);
      if (flags & 1 || ![0,8].includes(method) || header.readUInt16LE(34)!==0) throw invalid('шифрование/формат сжатия');
      const nameSize=header.readUInt16LE(28), extraSize=header.readUInt16LE(30), commentSize=header.readUInt16LE(32);
      if (cursor+46+nameSize+extraSize+commentSize>centralStart+centralSize) throw invalid('границы записи');
      const nameBytes=await readAt(file,cursor+46,nameSize), name=entryName(nameBytes);
      const extra=await readAt(file,cursor+46+nameSize,extraSize);
      let compressed=header.readUInt32LE(20), size=header.readUInt32LE(24), local=header.readUInt32LE(42);
      const needed=[size,compressed,local].filter(value=>value===0xffffffff).length;
      if (needed) {
        const values=zip64Values(extra,needed);
        if (size===0xffffffff) size=values.shift();
        if (compressed===0xffffffff) compressed=values.shift();
        if (local===0xffffffff) local=values.shift();
      }
      const key=name.replace(/\/$/,'').normalize('NFC').toLowerCase();
      if (names.has(key)) throw invalid('совпадающие имена файлов');
      names.add(key);
      const mode=header.readUInt32LE(38)>>>16, type=mode&0o170000;
      if (![0,0o040000,0o100000,0o120000].includes(type)) throw invalid('специальный файл');
      const directory=name.endsWith('/');
      if ((type===0o040000 && !directory) || (type===0o120000 && directory)) throw invalid('тип файла');
      if (!Number.isSafeInteger(uncompressedBytes+=size)) throw invalid('размер распаковки');
      // Compare local headers too: extraction must not see a different name.
      const localHeader=await readAt(file,local,30);
      if (localHeader.readUInt32LE(0)!==0x04034b50 || localHeader.readUInt16LE(6)!==flags ||
          localHeader.readUInt16LE(8)!==method) throw invalid('несогласованные заголовки');
      const localNameSize=localHeader.readUInt16LE(26), localExtraSize=localHeader.readUInt16LE(28);
      if (!(await readAt(file,local+30,localNameSize)).equals(nameBytes)) throw invalid('подмена локального пути');
      const dataStart=local+30+localNameSize+localExtraSize;
      if (local<0 || dataStart+compressed>centralStart) throw invalid('границы данных');
      const item={name,directory,symlink:type===0o120000,size};
      if (item.symlink) {
        if (size>4096 || compressed>8192 || size===0) throw invalid('длина символической ссылки');
        const raw=await readAt(file,dataStart,compressed);
        const value=method===0?raw:inflateRawSync(raw,{maxOutputLength:4096});
        if (value.length!==size) throw invalid('данные символической ссылки');
        try { item.target=decoder.decode(value); } catch { throw invalid('кодировка символической ссылки'); }
      }
      entries.push(item);
      cursor+=46+nameSize+extraSize+commentSize;
    }
    if (cursor!==centralStart+centralSize) throw invalid('лишние записи каталога');
    const roots=new Set(entries.filter(item=>!item.name.startsWith('__MACOSX/')).map(item=>item.name.split('/')[0]));
    if (roots.size!==1 || ![...roots][0].endsWith('.app')) throw invalid('требуется ровно один пакет .app');
    const bundle=[...roots][0], symlinks=new Set(), linkTargets=new Map();
    for (const item of entries) {
      if (!item.name.startsWith(bundle+'/') && item.name!==bundle+'/' && !item.name.startsWith('__MACOSX/'))
        throw invalid('файл вне приложения');
      if (item.symlink) {
        if (item.name.startsWith('__MACOSX/') || /[\\\0\r\n]/.test(item.target) ||
            path.posix.isAbsolute(item.target) || item.target.includes(':')) throw invalid('внешняя ссылка');
        const resolved=path.posix.normalize(path.posix.join(path.posix.dirname(item.name),item.target));
        if (!resolved.startsWith(bundle+'/')) throw invalid('ссылка выходит из приложения');
        symlinks.add(item.name.normalize('NFC').toLowerCase());
        linkTargets.set(item.name.normalize('NFC').toLowerCase(),item.target);
      }
    }
    // Do not let ditto write a later member through any earlier symlink.
    for (const item of entries) {
      const components=item.name.replace(/\/$/,'').normalize('NFC').toLowerCase().split('/');
      for (let length=1; length<components.length; length++) {
        if (symlinks.has(components.slice(0,length).join('/'))) throw invalid('запись через символическую ссылку');
      }
    }
    // A chain of individually relative links can still escape when ".." is
    // applied *after* following another link. Resolve component-by-component,
    // not only with lexical path.normalize(), and reject cycles.
    for (const item of entries.filter(entry=>entry.symlink)) {
      let current=path.posix.dirname(item.name).split('/');
      let remaining=item.target.split('/'), followed=0;
      while (remaining.length) {
        const next=remaining.shift();
        if (!next || next==='.') continue;
        if (next==='..') {
          if (current.length<=1) throw invalid('цепочка ссылок выходит из приложения');
          current.pop(); continue;
        }
        current.push(next);
        const destination=linkTargets.get(current.join('/').normalize('NFC').toLowerCase());
        if (destination!==undefined) {
          if (++followed>40) throw invalid('циклическая цепочка ссылок');
          current.pop();
          remaining=destination.split('/').concat(remaining);
        }
      }
    }
    if (!entries.some(item=>item.name===bundle+'/Contents/Info.plist' && !item.symlink))
      throw invalid('нет Info.plist');
    return {bundle,entries,uncompressedBytes};
  } finally { await file.close(); }
}
module.exports={inspectZip};
