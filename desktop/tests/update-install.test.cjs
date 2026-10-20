'use strict';
const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const {EventEmitter}=require('node:events');
const vm=require('node:vm');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const run=promisify(execFile);
const {inspectZip}=require('../update-zip.cjs');
const {verifyFile,validateInfo,bundleFromExecutable,launchUpdate,acknowledgeUpdateReady}=require('../update-install.cjs');
const sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const quote=value=>"'"+value.replaceAll("'","'\\''")+"'";
const mac=process.platform==='darwin';

function zipBytes(members) {
  const local=[],central=[]; let offset=0;
  for(const item of members) {
    const name=Buffer.from(item.name), data=Buffer.from(item.data||''), h=Buffer.alloc(30);
    h.writeUInt32LE(0x04034b50); h.writeUInt16LE(20,4); h.writeUInt16LE(0x800,6);
    h.writeUInt32LE(data.length,18); h.writeUInt32LE(data.length,22); h.writeUInt16LE(name.length,26);
    const c=Buffer.alloc(46);
    c.writeUInt32LE(0x02014b50); c.writeUInt16LE(0x0314,4); c.writeUInt16LE(20,6);
    c.writeUInt16LE(0x800,8); c.writeUInt32LE(data.length,20); c.writeUInt32LE(data.length,24);
    c.writeUInt16LE(name.length,28); c.writeUInt32LE(((item.symlink?0o120777:0o100644)<<16)>>>0,38);
    c.writeUInt32LE(offset,42);
    local.push(h,name,data); central.push(c,name); offset+=h.length+name.length+data.length;
  }
  const end=Buffer.alloc(22), directory=Buffer.concat(central);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(members.length,8); end.writeUInt16LE(members.length,10);
  end.writeUInt32LE(directory.length,12); end.writeUInt32LE(offset,16);
  return Buffer.concat([...local,directory,end]);
}
async function temporary(t) {
  const directory=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'Violet update test ')));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  return directory;
}
const base=[{name:'Violet.app/Contents/Info.plist',data:'fixture'}];

test('integrity uses signed bytes, not the downloaded filename; changed bytes refuse installation',async t=>{
  const directory=await temporary(t), file=path.join(directory,'hash-prefix-Violet-0.5.0-mac-arm64.zip'), data=Buffer.from('zipfixture');
  await fs.writeFile(file,data);
  const info={version:'0.5.0',platform:'darwin',arch:'arm64',filename:'Violet-0.5.0-mac-arm64.zip',size:data.length,sha256:sha(data)};
  await verifyFile(file,info);
  await fs.writeFile(file,Buffer.from('badfixture'));
  await assert.rejects(verifyFile(file,info),/сумма|размер/);
  assert.throws(()=>validateInfo({...info,filename:'../evil.zip'},'darwin','arm64'));
  assert.throws(()=>validateInfo(info,'darwin','x64'));
  await assert.rejects(launchUpdate({archive:file,info}),/не была подготовлена/);
});

test('bundle lookup preserves installation paths containing spaces',()=>{
  const bundle=path.resolve('Applications','My Tools','Violet.app');
  assert.equal(bundleFromExecutable(path.join(bundle,'Contents','MacOS','Violet')),bundle);
  assert.throws(()=>bundleFromExecutable('/usr/bin/node'));
});

test('ZIP permits normal internal macOS framework links',async t=>{
  const directory=await temporary(t), file=path.join(directory,'good.zip');
  await fs.writeFile(file,zipBytes([...base,
    {name:'Violet.app/Contents/Frameworks/F.framework/Versions/A/F',data:'native'},
    {name:'Violet.app/Contents/Frameworks/F.framework/Versions/Current',symlink:true,data:'A'},
    {name:'Violet.app/Contents/Frameworks/F.framework/F',symlink:true,data:'Versions/Current/F'}]));
  const result=await inspectZip(file);
  assert.equal(result.bundle,'Violet.app');
  assert.equal(result.entries.length,4);
});

test('ZIP rejects traversal, symlink escapes/chains, writes through links and mismatched local paths',async t=>{
  const directory=await temporary(t), file=path.join(directory,'bad.zip');
  const cases=[
    [...base,{name:'../stolen'}],
    [...base,{name:'/tmp/stolen'}],
    [...base,{name:'Violet.app/Contents\\evil'}],
    [...base,{name:'Violet.app/Contents/link',symlink:true,data:'../../../outside'}],
    [...base,{name:'Violet.app/link',symlink:true,data:'Contents'},{name:'Violet.app/link/file',data:'write'}],
    [...base,{name:'Violet.app/loop',symlink:true,data:'loop'}],
    [...base,{name:'Violet.app/a/b',symlink:true,data:'..'},
      {name:'Violet.app/escape',symlink:true,data:'a/b/../../outside'}],
    [...base,{name:'Violet.app/Contents/info.plist',data:'collision'}]
  ];
  for(const members of cases) {
    await fs.writeFile(file,zipBytes(members));
    await assert.rejects(inspectZip(file),/Небезопасный ZIP/);
  }
  const mismatched=zipBytes(base);
  mismatched[30]='X'.charCodeAt(0);
  await fs.writeFile(file,mismatched);
  await assert.rejects(inspectZip(file),/подмена локального пути/);
});

test('readiness requires the current transaction nonce, installed executable, version and armed state',async t=>{
  const directory=await temporary(t), userData=path.join(directory,'User Data'), target=path.join(directory,'Violet.app');
  const execPath=path.join(target,'Contents/MacOS/Violet');
  await fs.mkdir(path.dirname(execPath),{recursive:true}); await fs.writeFile(execPath,'binary');
  const id=crypto.randomUUID(), transaction=path.join(userData,'updates/installations',id);
  await fs.mkdir(transaction,{recursive:true});
  const token='a'.repeat(64);
  const plan={schema:1,id,token,transaction,userData,target,execPath,platform:process.platform,
    receipt:path.join(transaction,'ready.json'),info:{version:'0.5.0'}};
  const save=(name,value)=>fs.writeFile(path.join(transaction,name),JSON.stringify(value));
  await fs.writeFile(path.join(userData,'updates/installations/pending.json'),JSON.stringify({schema:1,id}));
  await save('plan.json',plan); await save('armed.json',{id,token});
  await fs.writeFile(path.join(transaction,'status'),'starting-new-version');
  assert.equal(await acknowledgeUpdateReady({userData,version:'0.4.0',execPath}),false);
  assert.equal(await acknowledgeUpdateReady({userData,version:'0.5.0',execPath}),true);
  const receipt=JSON.parse(await fs.readFile(plan.receipt,'utf8'));
  assert.equal(receipt.token,token); assert.equal(receipt.id,id); assert.equal(receipt.execPath,execPath);
  await save('armed.json',{id,token:'b'.repeat(64)});
  assert.equal(await acknowledgeUpdateReady({userData,version:'0.5.0',execPath}),false);
  await save('armed.json',{id,token}); await fs.writeFile(path.join(transaction,'status'),'rolled-back');
  assert.equal(await acknowledgeUpdateReady({userData,version:'0.5.0',execPath}),false);
  await fs.writeFile(path.join(transaction,'status'),'starting-new-version');
  await fs.writeFile(path.join(transaction,'cancel'),'');
  assert.equal(await acknowledgeUpdateReady({userData,version:'0.5.0',execPath}),false);
});

async function fakeBundle(bundle,version) {
  await fs.mkdir(path.join(bundle,'Contents/MacOS'),{recursive:true});
  await fs.writeFile(path.join(bundle,'Contents/Info.plist'),JSON.stringify({
    CFBundleIdentifier:'com.violet.interior-rd',CFBundleShortVersionString:version,CFBundleExecutable:'Violet'
  }));
  await fs.writeFile(path.join(bundle,'Contents/MacOS/Violet'),version);
}
async function helperFixture(t,{validReceipt=true,aliveParent=false,failOpen=false}={}) {
  const directory=await temporary(t), id=crypto.randomUUID(), token='b'.repeat(64);
  const transaction=path.join(directory,id), target=path.join(directory,'Installed App.app'),
    staged=path.join(directory,'Staged App.app'), backup=path.join(directory,'Old Backup.app'),
    failed=path.join(directory,'Failed App.app'), archive=path.join(directory,'download.zip');
  await fs.mkdir(transaction); await fakeBundle(target,'0.4.0'); await fakeBundle(staged,'0.5.0');
  await fs.writeFile(archive,'authenticated');
  const fakeOpen=path.join(directory,'fake open.sh'), receipt=path.join(transaction,'ready.json');
  const contents={id,token:validReceipt?token:'stale-other-transaction',version:'0.5.0',
    execPath:path.join(target,'Contents/MacOS/Violet')};
  await fs.writeFile(fakeOpen,`#!/bin/sh\n${failOpen?'exit 1':
    `printf '%s' ${quote(JSON.stringify(contents))} > ${quote(receipt)}\nexit 0`}\n`,{mode:0o700});
  // The production helper stays untouched. Substitute only LaunchServices in a
  // disposable copy, so these tests never launch or replace a real application.
  let source=await fs.readFile(path.join(__dirname,'../update-helpers/mac-install.sh'),'utf8');
  source=source.replaceAll('/usr/bin/open',quote(fakeOpen));
  const helper=path.join(transaction,'helper-test.sh');
  await fs.writeFile(helper,source);
  const parentStart=aliveParent?(await run('/bin/ps',['-p',String(process.pid),'-o','lstart='])).stdout.trim():'not-this-process';
  const args=[helper,transaction,target,staged,backup,failed,String(process.pid),parentStart,
    '0.5.0',token,'1','1',archive,sha(Buffer.from('authenticated'))];
  return {directory,transaction,target,staged,backup,failed,args};
}

test('mac helper installs atomically, acknowledges correct nonce and retains old app backup',{skip:!mac},async t=>{
  const f=await helperFixture(t);
  await run('/bin/sh',f.args,{timeout:10000});
  assert.equal((await fs.readFile(path.join(f.transaction,'status'),'utf8')).trim(),'ready');
  assert.equal(await fs.readFile(path.join(f.target,'Contents/MacOS/Violet'),'utf8'),'0.5.0');
  assert.equal(await fs.readFile(path.join(f.backup,'Contents/MacOS/Violet'),'utf8'),'0.4.0');
});

test('mac helper rolls back on stale receipt, preserving failed version separately',{skip:!mac},async t=>{
  const f=await helperFixture(t,{validReceipt:false});
  await assert.rejects(run('/bin/sh',f.args,{timeout:10000}));
  assert.equal((await fs.readFile(path.join(f.transaction,'status'),'utf8')).trim(),'rolled-back');
  assert.equal(await fs.readFile(path.join(f.target,'Contents/MacOS/Violet'),'utf8'),'0.4.0');
  assert.equal(await fs.readFile(path.join(f.failed,'Contents/MacOS/Violet'),'utf8'),'0.5.0');
});

test('mac helper bounded parent wait leaves installed app completely unchanged',{skip:!mac},async t=>{
  const f=await helperFixture(t,{aliveParent:true});
  await assert.rejects(run('/bin/sh',f.args,{timeout:10000}));
  assert.equal((await fs.readFile(path.join(f.transaction,'status'),'utf8')).trim(),'previous-process-still-running');
  assert.equal(await fs.readFile(path.join(f.target,'Contents/MacOS/Violet'),'utf8'),'0.4.0');
  await assert.rejects(fs.access(f.backup));
});

test('mac helper rejects cancelled launch and changed bytes before replacing the app',{skip:!mac},async t=>{
  const f=await helperFixture(t);
  await fs.writeFile(path.join(f.transaction,'cancel'),'');
  await assert.rejects(run('/bin/sh',f.args,{timeout:10000}));
  assert.equal((await fs.readFile(path.join(f.transaction,'status'),'utf8')).trim(),'cancelled');
  assert.equal(await fs.readFile(path.join(f.target,'Contents/MacOS/Violet'),'utf8'),'0.4.0');
});

test('Windows helper uses documented NSIS flags, no elevation or execution-policy bypass',async()=>{
  const script=await fs.readFile(path.join(__dirname,'../update-helpers/windows-install.ps1'),'utf8');
  assert.match(script,/\$arguments = '--updated \/S --force-run \/D='/);
  assert.match(script,/WaitForExit\(\[int\]\$plan\.parentTimeout \* 1000\)/);
  assert.equal((script.match(/Get-ArchiveHash \$plan\.archive/g)||[]).length,2);
  assert.match(script,/\[Security\.Cryptography\.SHA256\]::Create\(\)/);
  assert.match(script,/Get-Content -LiteralPath \$receiptPath -Raw -Encoding UTF8/);
  assert.doesNotMatch(script,/Start-Process[^;\n]+-Verb\s+RunAs/);
  const launcher=await fs.readFile(path.join(__dirname,'../update-install.cjs'),'utf8');
  assert.doesNotMatch(launcher,/'-ExecutionPolicy'/);
});

test('every post-spawn failure disarms helper before returning to the live app',{skip:!mac},async t=>{
  const directory=await temporary(t), userData=path.join(directory,'User Data');
  await fs.mkdir(userData);
  const target=path.join(directory,'Installed.app'), file=path.join(directory,'verified.zip');
  await fakeBundle(target,'0.4.0'); await fs.writeFile(file,'testzip');
  let killed=false, transaction;
  const source=await fs.readFile(path.join(__dirname,'../update-install.cjs'),'utf8');
  const context=vm.createContext({
    require(name) {
      if(name==='node:child_process') return {
        // Native OS execution is not under test here. Real filesystem and real
        // prepare/launch functions are used; only ditto/plutil/lipo/ps are faked.
        execFile(binary,args,options,callback) {
          (async()=>{
            let stdout='';
            if(binary.endsWith('/ditto')) {
              await fakeBundle(path.join(args[3],'Violet.app'),'0.5.0');
            } else if(binary.endsWith('/plutil')) {
              stdout=String(JSON.parse(await fs.readFile(args.at(-1),'utf8'))[args[1]]);
            } else if(binary.endsWith('/lipo')) stdout='arm64';
            else if(binary.endsWith('/ps')) stdout='fixture-start';
            return {stdout,stderr:''};
          })().then(result=>callback(null,result),callback);
        },
        spawn(binary,args) {
          transaction=args[1];
          const child=new EventEmitter();
          child.kill=()=>{killed=true; return true;}; child.unref=()=>{};
          // A malformed armed marker must not leave a helper waiting for a
          // later, unrelated exit of the editor.
          fs.writeFile(path.join(transaction,'armed.json'),'not json').catch(()=>{});
          return child;
        }
      };
      if(name==='./update-zip.cjs') return {inspectZip:async()=>({bundle:'Violet.app',uncompressedBytes:32})};
      return require(name);
    },
    module:{exports:{}},__dirname:path.resolve(__dirname,'..'),process,Buffer,setTimeout,console
  });
  vm.runInContext(source,context);
  const installer=context.module.exports;
  const plan=await installer.prepareUpdate({
    file,info:{version:'0.5.0',platform:'darwin',arch:'arm64',filename:'Violet-0.5.0-mac-arm64.zip',
      size:7,sha256:sha(Buffer.from('testzip'))},
    userData,appPath:path.join(target,'Contents/Resources/app.asar'),
    execPath:path.join(target,'Contents/MacOS/Violet'),arch:'arm64'
  });
  await assert.rejects(installer.launchUpdate(plan));
  assert.equal(killed,true);
  await fs.access(path.join(transaction,'cancel'));
});

test('Windows PowerShell helper parses paths with spaces and confirms matching new-app receipt',
  {skip:process.platform!=='win32'},async t=>{
  const directory=await temporary(t), transaction=path.join(directory,crypto.randomUUID());
  await fs.mkdir(transaction);
  const archive=path.join(transaction,'Violet installer.exe'), data=Buffer.from('test-only-not-an-executable');
  await fs.writeFile(archive,data);
  const plan={id:path.basename(transaction),token:'d'.repeat(64),transaction,archive,
    info:{sha256:sha(data),version:'0.5.0'},pid:2147483647,parentTimeout:1,readyTimeout:2,
    target:path.join(directory,'Приложение With Spaces'),execPath:path.join(directory,'Приложение With Spaces','Violet.exe')};
  const encoded=Buffer.from(JSON.stringify(plan),'utf8').toString('base64');
  // Override installer execution only; actual Windows PowerShell parses the
  // production helper, hashes the fixture and evaluates its receipt checks.
  const fakeInstaller=`
function Start-Process {
  param([string] $FilePath, [string] $ArgumentList, [switch] $PassThru)
  if ($FilePath -ne $plan.archive -or $ArgumentList -ne ('--updated /S --force-run /D=' + $plan.target)) { throw 'bad-arguments' }
  $receipt = @{id=$plan.id; token=$plan.token; version=$plan.info.version; execPath=$plan.execPath} | ConvertTo-Json -Compress
  [IO.File]::WriteAllText((Join-Path $plan.transaction 'ready.json'), $receipt, $utf8)
  $result = [pscustomobject]@{ExitCode=0}
  Add-Member -InputObject $result -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) return $true }
  return $result
}
`;
  const source=`$violetPlanJson=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}'))\r\n`+
    fakeInstaller+await fs.readFile(path.join(__dirname,'../update-helpers/windows-install.ps1'),'utf8');
  const powershell=path.join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0/powershell.exe');
  try {
    await run(powershell,['-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(source,'utf16le').toString('base64')],{timeout:30000});
  } catch (error) {
    const status=await fs.readFile(path.join(transaction,'status'),'utf8').catch(()=>'(no status written)');
    // Surface the helper's actual failure, not the enormous encoded command.
    assert.fail(`Windows helper exit ${error.code}: ${status}\n${error.stderr||''}`);
  }
  assert.equal((await fs.readFile(path.join(transaction,'status'),'utf8')).trim(),'ready');
});
