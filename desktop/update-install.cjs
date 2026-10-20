'use strict';
const fs = require('node:fs/promises');
const constants = require('node:fs').constants;
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn,execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {inspectZip} = require('./update-zip.cjs');
const command=promisify(execFile);
const APP_ID='com.violet.interior-rd';
const plans=new WeakSet();
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const platformName=value=>({darwin:'darwin',mac:'darwin',win32:'win32',win:'win32'}[value]);
function within(child,parent) {
  const relative=path.relative(parent,child);
  return relative!=='' && !relative.startsWith('..'+path.sep) && relative!=='..' && !path.isAbsolute(relative);
}
function validateInfo(info,platform,arch) {
  if (!info || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(info.version) ||
      platformName(info.platform)!==platform || info.arch!==arch || !['arm64','x64'].includes(arch) ||
      !Number.isSafeInteger(info.size) || info.size<=0 || !/^[a-f0-9]{64}$/i.test(info.sha256) ||
      typeof info.filename!=='string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]+$/.test(info.filename) ||
      !info.filename.endsWith(platform==='darwin'?'.zip':'.exe'))
    throw new Error('Не совпадают версия, архитектура или контрольная сумма обновления.');
  return Object.freeze({...info,sha256:info.sha256.toLowerCase()});
}
async function regularFile(filename) {
  const st=await fs.lstat(filename);
  if (!st.isFile() || st.isSymbolicLink() || st.nlink!==1) throw new Error('Файл обновления должен быть обычным файлом без ссылок.');
  return st;
}
async function verifyFile(filename,info) {
  await regularFile(filename);
  const handle=await fs.open(filename,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
  try {
    const before=await handle.stat(), sha=crypto.createHash('sha256');
    if (!before.isFile() || before.nlink!==1 || before.size!==info.size) throw new Error('Изменился размер обновления.');
    for await (const chunk of handle.createReadStream({autoClose:false})) sha.update(chunk);
    const after=await handle.stat();
    if (before.size!==after.size || before.mtimeMs!==after.mtimeMs || sha.digest('hex')!==info.sha256)
      throw new Error('Контрольная сумма обновления не совпадает; установка отменена.');
  } finally { await handle.close(); }
}
function bundleFromExecutable(executable) {
  let cursor=path.resolve(executable);
  while (cursor!==path.dirname(cursor)) {
    if (cursor.endsWith('.app')) return cursor;
    cursor=path.dirname(cursor);
  }
  throw new Error('Обновление доступно только из установленного приложения .app.');
}
async function plistValue(bundle,key) {
  const {stdout}=await command('/usr/bin/plutil',['-extract',key,'raw','-o','-',path.join(bundle,'Contents/Info.plist')],{timeout:10000,maxBuffer:65536});
  return stdout.trim();
}
async function validateBundle(bundle,version,arch) {
  if (await plistValue(bundle,'CFBundleIdentifier')!==APP_ID) throw new Error('В архиве другое приложение.');
  if (version && await plistValue(bundle,'CFBundleShortVersionString')!==version) throw new Error('Версия .app не совпадает с подписанным выпуском.');
  const binary=await plistValue(bundle,'CFBundleExecutable');
  if (!binary || path.basename(binary)!==binary || /[\\\0\r\n]/.test(binary)) throw new Error('Некорректное имя исполняемого файла .app.');
  const executable=path.join(bundle,'Contents/MacOS',binary);
  await regularFile(executable);
  if (arch) {
    const {stdout}=await command('/usr/bin/lipo',['-archs',executable],{timeout:10000,maxBuffer:65536});
    if (!stdout.trim().split(/\s+/).includes(arch==='x64'?'x86_64':'arm64')) throw new Error('Приложение собрано для другого процессора.');
  }
  return executable;
}
async function privateDirectory(directory) {
  await fs.mkdir(directory,{recursive:true,mode:0o700});
  const real=await fs.realpath(directory);
  const st=await fs.lstat(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || path.resolve(directory)!==real) throw new Error('Каталог обновления не должен быть символической ссылкой.');
  return real;
}
async function writeJSON(filename,value) {
  const temporary=filename+'.'+crypto.randomBytes(8).toString('hex')+'.tmp';
  await fs.writeFile(temporary,JSON.stringify(value),{flag:'wx',mode:0o600});
  await fs.rename(temporary,filename);
}
async function readJSON(filename) {
  const stat=await regularFile(filename);
  if (stat.size>65536) throw new Error('Некорректное состояние установки.');
  return JSON.parse(await fs.readFile(filename,'utf8'));
}
async function prepareUpdate({file,info,appPath,execPath,userData,currentPid=process.pid,platform=process.platform,arch=process.arch}) {
  platform=platformName(platform);
  if (!platform || platform!==process.platform) throw new Error('Обновление устанавливается только на текущей ОС.');
  info=validateInfo(info,platform,arch);
  if (!Number.isInteger(currentPid) || currentPid<=0 || currentPid!==process.pid) throw new Error('Некорректный процесс приложения.');
  file=path.resolve(file);
  await verifyFile(file,info);
  const data=await fs.realpath(userData), executable=await fs.realpath(execPath);
  let target=platform==='darwin'?bundleFromExecutable(executable):path.dirname(executable);
  if (within(data,target) || data===target || within(target,data)) throw new Error('Проекты должны храниться отдельно от программы.');
  if (!within(path.resolve(appPath),target)) throw new Error('Не найден установленный пакет приложения.');
  await fs.access(target,constants.W_OK);
  if (platform==='darwin') {
    if (target.includes('/AppTranslocation/')) throw new Error('Сначала перенесите Violet из образа в папку Программы и откройте оттуда.');
    await fs.access(path.dirname(target),constants.W_OK);
    if (await validateBundle(target)!==executable) throw new Error('Исполняемый файл не относится к установленному приложению.');
  }
  const updates=await privateDirectory(path.join(data,'updates','installations'));
  const id=crypto.randomUUID(), token=crypto.randomBytes(32).toString('hex');
  const transaction=path.join(updates,id);
  await fs.mkdir(transaction,{mode:0o700});
  const archive=path.join(transaction,info.filename);
  await fs.copyFile(file,archive,constants.COPYFILE_EXCL);
  await verifyFile(archive,info);
  const plan={
    schema:1,id,token,platform,arch,info,transaction,target,execPath:executable,archive,
    userData:data,pid:currentPid,parentTimeout:60,readyTimeout:180,
    receipt:path.join(transaction,'ready.json'),pending:path.join(updates,'pending.json')
  };
  if (platform==='darwin') {
    const zip=await inspectZip(archive);
    const stats=await fs.statfs(path.dirname(target));
    if (stats.bavail*stats.bsize<zip.uncompressedBytes+32*1024*1024) throw new Error('Недостаточно свободного места для безопасной распаковки.');
    try { plan.staging=await fs.mkdtemp(path.join(path.dirname(target),'.violet-update-')); }
    catch { throw new Error('Папка приложения недоступна для записи. Закройте образ диска и перенесите Violet в доступную вам папку.'); }
    await fs.chmod(plan.staging,0o700);
    await command('/usr/bin/ditto',['-x','-k',archive,plan.staging],{timeout:600000,maxBuffer:1048576});
    plan.staged=path.join(plan.staging,zip.bundle);
    await validateBundle(plan.staged,info.version,arch);
    plan.backup=path.join(path.dirname(target),`.Violet-backup-${id}.app`);
    plan.failed=path.join(path.dirname(target),`.Violet-failed-${id}.app`);
    const {stdout}=await command('/bin/ps',['-p',String(currentPid),'-o','lstart='],{timeout:10000});
    plan.parentStart=stdout.trim();
    if (!plan.parentStart) throw new Error('Не удалось определить процесс приложения.');
    plan.helper=path.join(transaction,'mac-install.sh');
    await fs.copyFile(path.join(__dirname,'update-helpers/mac-install.sh'),plan.helper,constants.COPYFILE_EXCL);
  } else {
    // Per-user NSIS only: never invoke elevate.exe, RunAs or an all-users install.
    plan.helper=path.join(transaction,'windows-install.ps1');
    await fs.copyFile(path.join(__dirname,'update-helpers/windows-install.ps1'),plan.helper,constants.COPYFILE_EXCL);
  }
  await fs.chmod(plan.helper,0o700);
  await writeJSON(path.join(transaction,'plan.json'),plan);
  plans.add(plan);
  return Object.freeze(plan);
}
async function launchUpdate(plan) {
  if (!plans.has(plan)) throw new Error('Установка не была подготовлена этим экземпляром приложения.');
  await verifyFile(plan.archive,plan.info);
  if (plan.platform==='darwin') await validateBundle(plan.staged,plan.info.version,plan.arch);
  // The pending pointer contains only a random transaction ID, never a path.
  // The startup acknowledgement checks version, executable, nonce and ID.
  await writeJSON(plan.pending,{schema:1,id:plan.id});
  let child;
  if (plan.platform==='darwin') {
    child=spawn('/bin/sh',[plan.helper,plan.transaction,plan.target,plan.staged,plan.backup,plan.failed,
      String(plan.pid),plan.parentStart,plan.info.version,plan.token,String(plan.parentTimeout),
      String(plan.readyTimeout),plan.archive,plan.info.sha256],{detached:true,stdio:'ignore'});
  } else {
    const script=await fs.readFile(plan.helper,'utf8');
    const config=Buffer.from(JSON.stringify(plan),'utf8').toString('base64');
    const source=`$violetPlanJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${config}'))\r\n${script}`;
    const powershell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
    child=spawn(powershell,['-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
      '-EncodedCommand',Buffer.from(source,'utf16le').toString('base64')],{detached:true,stdio:'ignore',windowsHide:true});
  }
  try {
    let exited=false, failure;
    child.once('error',error=>{failure=error;});
    child.once('exit',()=>{exited=true;});
    const deadline=Date.now()+15000;
    while (Date.now()<deadline) {
      if (failure) throw failure;
      let armed;
      try { armed=await readJSON(path.join(plan.transaction,'armed.json')); } catch(error) { if(error.code!=='ENOENT') throw error; }
      if (armed?.token===plan.token && armed?.id===plan.id) {
        plans.delete(plan); child.unref(); return {id:plan.id,platform:plan.platform};
      }
      if (exited) throw new Error('Помощник обновления отказался начинать установку. Текущая версия сохранена.');
      await delay(100);
    }
    throw new Error('Помощник обновления не запустился вовремя; текущая версия сохранена.');
  } catch(error) {
    // Disarm *every* launch failure, including malformed acknowledgement or I/O
    // errors. Otherwise a surviving helper could install on a later plain quit.
    await fs.writeFile(path.join(plan.transaction,'cancel'),'',{flag:'wx',mode:0o600}).catch(()=>{});
    child.kill();
    throw error;
  }
}
async function acknowledgeUpdateReady({userData,version,execPath}) {
  const data=await fs.realpath(userData);
  const updates=path.join(data,'updates','installations');
  let pending;
  try { pending=await readJSON(path.join(updates,'pending.json')); } catch(error) { if(error.code==='ENOENT') return false; throw error; }
  if (pending.schema!==1 || !/^[0-9a-f-]{36}$/.test(pending.id)) return false;
  const transaction=path.join(updates,pending.id);
  if ((await fs.realpath(transaction))!==transaction) return false;
  try { await fs.access(path.join(transaction,'cancel')); return false; } catch(error) { if(error.code!=='ENOENT') throw error; }
  const plan=await readJSON(path.join(transaction,'plan.json'));
  const executable=await fs.realpath(execPath);
  if (plan.schema!==1 || plan.id!==pending.id || plan.transaction!==transaction || plan.userData!==data ||
      !/^[0-9a-f]{64}$/.test(plan.token) || plan.info?.version!==version || plan.execPath!==executable ||
      !['darwin','win32'].includes(plan.platform) || plan.platform!==process.platform ||
      plan.receipt!==path.join(transaction,'ready.json') || !within(executable,plan.target))
    return false;
  const armed=await readJSON(path.join(transaction,'armed.json'));
  if (armed.id!==plan.id || armed.token!==plan.token) return false;
  const status=await fs.readFile(path.join(transaction,'status'),'utf8');
  if (!['starting-new-version','waiting-for-new-version','installing','ready'].includes(status.trim())) return false;
  await writeJSON(path.join(transaction,'ready.json'),{
    schema:1,id:plan.id,token:plan.token,version,execPath:executable,pid:process.pid,readyAt:new Date().toISOString()
  });
  return true;
}
module.exports={prepareUpdate,launchUpdate,acknowledgeUpdateReady,verifyFile,validateInfo,bundleFromExecutable};
