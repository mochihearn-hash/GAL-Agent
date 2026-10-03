// 发布打包入口：复制 Electron 运行时、压入前端/桌面代码、附带冻结 Python 后端。
// 此脚本只组装已经构建的产物；npm run pack 会先执行前端和后端构建。
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');

async function packageApp() {
  // 从 package.json 读取发布版本，分别输出到版本目录，旧版本目录不会统一清除。
  const root = path.resolve(__dirname, '..');
  const metadata=JSON.parse(await fs.readFile(path.join(root,'package.json'),'utf8'));
  if(!/^\d+\.\d+\.\d+$/.test(metadata.version))throw new Error('Invalid package version');
  const release = path.resolve(root, 'release');
  const output = path.resolve(release, `GAL-Agent-${metadata.version}`);
  const staging = path.resolve(release, 'app-staging');
  // 删除的仅是当前版本发布产物与临时暂存目录。先验证解析后的父目录关系，防止路径越界。
  // 重新打包同一版本会替换该版本目录，因此应在该版本应用关闭后执行。
  // These are build artifact directories only; verify before recursive cleanup.
  if (path.dirname(output) !== release || path.dirname(staging) !== release || path.dirname(release) !== root) throw new Error('Invalid packaging destination');
  for (const required of ['node_modules/electron/dist/electron.exe','frontend/dist/index.html','backend/dist/gal-backend/gal-backend.exe']) await fs.access(path.join(root,required));
  await fs.mkdir(release,{recursive:true});
  await fs.rm(output,{recursive:true,force:true});
  await fs.rm(staging,{recursive:true,force:true});
  await fs.cp(path.join(root,'node_modules/electron/dist'),output,{recursive:true});
  await fs.mkdir(staging,{recursive:true});
  await fs.cp(path.join(root,'desktop'),path.join(staging,'desktop'),{recursive:true,filter:source=>!source.endsWith('.test.cjs')});
  // 不把测试代码装进应用；asar 仅打包页面/桌面代码，后端可执行文件在外部资源目录运行。
  await fs.cp(path.join(root,'frontend/dist'),path.join(staging,'frontend/dist'),{recursive:true});
  const packaged={name:'gal-agent',productName:'GAL Agent',version:metadata.version,description:metadata.description,main:'desktop/main.cjs'};
  await fs.writeFile(path.join(staging,'package.json'),JSON.stringify(packaged,null,2));
  const asar=require('@electron/asar');
  await asar.createPackage(staging,path.join(output,'resources','app.asar'));
  await fs.cp(path.join(root,'backend/dist/gal-backend'),path.join(output,'resources','backend'),{recursive:true});
  await fs.rename(path.join(output,'electron.exe'),path.join(output,'GAL Agent.exe'));
  await fs.copyFile(path.join(root,'README.md'),path.join(output,'使用说明.md'));
  await fs.cp(path.join(root,'docs'),path.join(output,'docs'),{recursive:true});
  await fs.rm(staging,{recursive:true,force:true});
  console.log('Windows app:',path.join(output,'GAL Agent.exe'));
  if(process.argv.includes('--zip')){
    // ZIP 通过 PowerShell 辅助脚本生成，路径作为独立参数传递，保留空格而不拼接命令字符串。
    const destination=path.join(release,`GAL-Agent-${metadata.version}-Windows.zip`);
    await new Promise((resolve,reject)=>{
      const helper=spawn('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',path.join(root,'scripts','Zip.ps1'),'-SourcePath',output,'-DestinationPath',destination],{windowsHide:true,stdio:'inherit'});
      helper.on('error',reject);helper.on('exit',code=>code===0?resolve():reject(new Error('Archive creation failed')));
    });
    console.log('Archive:',destination);
  }
}
packageApp().catch(error=>{console.error(error.message);process.exitCode=1;});
