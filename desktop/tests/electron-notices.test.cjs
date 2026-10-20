"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {preserve,verify} = require("../build/electron_notices.cjs");

for (const platform of ["darwin","win32"]) {
  test(`${platform} package retains exact Electron/Chromium notices after branding`, async()=>{
    const directory = fs.mkdtempSync(path.join(os.tmpdir(),"violet-electron-notices-"));
    try {
      const extracted=path.join(directory,"extracted"), resources=path.join(directory,platform,"resources");
      fs.mkdirSync(extracted);
      fs.writeFileSync(path.join(extracted,"LICENSE"),"Electron license fixture");
      fs.writeFileSync(path.join(extracted,"LICENSES.chromium.html"),"<html>Chromium notice fixture</html>");
      const context={appOutDir:extracted,packager:{info:{framework:{version:"44.9.0"}},getResourcesDir:()=>resources}};
      const destination=path.join(resources,"notices/electron");
      await preserve(context,destination);
      fs.unlinkSync(path.join(extracted,"LICENSE"));
      fs.unlinkSync(path.join(extracted,"LICENSES.chromium.html"));
      await verify(context);
      assert.equal(JSON.parse(fs.readFileSync(path.join(destination,"inventory.json"))).electronVersion,"44.9.0");
      fs.appendFileSync(path.join(destination,"LICENSE"),"changed");
      await assert.rejects(verify(context),/changed/);
      await assert.rejects(preserve(context,destination),/ENOENT/);
    } finally { fs.rmSync(directory,{recursive:true,force:true}); }
  });
}
