import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";
import {importFilesSequential,normalizeImportResult,summarizeImportResults,validateImportFile} from "../static/import-batch.mjs";

const workspaceSource=fs.readFileSync(new URL("../static/workspace.mjs",import.meta.url),"utf8");
const importStart=workspaceSource.indexOf("function importOptions");
const importEnd=workspaceSource.indexOf("async function submitTranslate",importStart);
assert.ok(importStart>=0&&importEnd>importStart);

function importHarness({files=[],action="import",apiResults=[],apiReject=false,apiRejectAt=null,apiDeferred=false}={}){
  const names=["import-options","import-pages-wrap","submit-import","import-batch-hint","import-progress","import-progress-stage","import-progress-value","import-progress-fill","file-input","pick-file","import-selection","import-summary","import-msg","import-modal","duplicate-modal","duplicate-name","status","status-text","import-action","import-mode","import-pages"];
  const nodes=new Map(names.map(id=>[id,{hidden:false,disabled:false,value:id==="import-action"?action:id==="import-mode"?"trial":id==="import-pages"?"1":"",textContent:"",children:[],style:{},classList:{add(){},remove(){},toggle(){},contains(){return false;}},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);}}]));
  nodes.get("import-action").value=action;nodes.get("import-mode").value="trial";nodes.get("import-pages").value="1";
  const state={file:files.length===1?files[0]:null,files:files.slice(),importInFlight:false};
  const calls=[];let nextResult=0;let resolvePending;
  const context={state,$:id=>nodes.get(id),validateImportFile,importFilesSequential,normalizeImportResult,summarizeImportResults,
    document:{createElement:()=>({textContent:"",children:[],append(...items){this.children.push(...items);}})},window:{},
    FormData:class{constructor(){this.fields=[];}append(key,value){this.fields.push([key,value]);}},
    msg:(id,text)=>{const node=nodes.get(id);if(node)node.textContent=text;},open:id=>nodes.get(id).classList.add("open"),close:(id)=>nodes.get(id).classList.remove("open"),
    importOptions:undefined,loadJobs:async selectFirst=>{calls.push(["loadJobs",selectFirst]);},select:async id=>calls.push(["select",id]),
    api:async(_url,options)=>{calls.push(["api",options]);const index=nextResult++;if(apiDeferred)return new Promise(resolve=>{resolvePending=()=>resolve(apiResults[index]||{job:{id:`job-${index+1}`}});});if(apiReject||index===apiRejectAt)throw Error(index===apiRejectAt?"失败原因":"请求失败");return apiResults[index]||{job:{id:`job-${index+1}`}};},
  };
  vm.runInNewContext(workspaceSource.slice(importStart,importEnd),context);
  return {state,nodes,calls,resolve:()=>resolvePending?.(),...context};
}

test("batch importer preserves order and continues after failures",async()=>{
  const files=[{name:"a.pdf",size:1},{name:"b.pdf",size:1},{name:"c.pdf",size:1}];
  const calls=[];
  const results=await importFilesSequential(files,async file=>{calls.push(file.name);if(file.name==="b.pdf")throw Error("损坏");return {job:{id:file.name},duplicate:file.name==="a.pdf"};});
  assert.deepEqual(calls,["a.pdf","b.pdf","c.pdf"]);
  assert.equal(results[1].error,"损坏");
  assert.equal(summarizeImportResults(results).duplicates.length,1);
  assert.equal(summarizeImportResults(results).failed.length,1);
});

test("per-file validation reports size without affecting other files",async()=>{
  const files=[{name:"large.pdf",size:100*1024*1024+1},{name:"ok.pdf",size:1}];
  const calls=[];
  const results=await importFilesSequential(files,async file=>{const error=validateImportFile(file);if(error)throw Error(error);calls.push(file.name);return {job:{id:"ok"}};});
  assert.deepEqual(calls,["ok.pdf"]);
  assert.equal(results[0].filename,"large.pdf");
  assert.equal(results[0].error,"文件超过 100 MB。");
  assert.equal(results[1].job.id,"ok");
});

test("summary keeps duplicate and failure names",()=>{
  const summary=summarizeImportResults([{filename:"new.pdf",job:{id:"1"}},{filename:"same.pdf",job:{id:"2"},duplicate:true},{filename:"bad.pdf",error:"网络错误"}]);
  assert.deepEqual(summary.added.map(item=>item.filename),["new.pdf"]);
  assert.deepEqual(summary.duplicates.map(item=>item.filename),["same.pdf"]);
  assert.deepEqual(summary.failed,[{filename:"bad.pdf",error:"网络错误"}]);
});

test("real submitImport posts in order, refreshes once, selects first success and releases files",async()=>{
  const h=importHarness({files:[{name:"one.pdf",size:1},{name:"two.pdf",size:1},{name:"three.pdf",size:1}],apiResults:[{job:{id:"one"}},{job:{id:"two"},duplicate:true}],apiRejectAt:2});
  await h.submitImport();
  assert.equal(h.calls.filter(item=>item[0]==="api").length,3);
  assert.deepEqual(h.calls.filter(item=>item[0]==="loadJobs"),[["loadJobs",false]]);
  assert.deepEqual(h.calls.filter(item=>item[0]==="select"),[["select","one"]]);
  assert.equal(h.state.files.length,0);
  assert.match(h.nodes.get("import-summary").children[0].textContent,/1 个新增，1 个重复，1 个失败/);
  assert.match(h.nodes.get("import-summary").children.at(-1).children[0].textContent,/three\.pdf/);
});

test("real submitImport lock ignores a double click while request is pending",async()=>{
  const h=importHarness({files:[{name:"one.pdf",size:1}],apiDeferred:true});
  const first=h.submitImport();
  const second=h.submitImport();
  assert.equal(h.calls.filter(item=>item[0]==="api").length,1);
  h.resolve();
  await Promise.all([first,second]);
});

test("real submitImport keeps all-failed batches from selecting a job",async()=>{
  const h=importHarness({files:[{name:"one.pdf",size:1},{name:"two.pdf",size:1}],apiReject:true});
  await h.submitImport();
  assert.equal(h.calls.some(item=>item[0]==="select"),false);
  assert.equal(h.calls.filter(item=>item[0]==="loadJobs").length,1);
});

test("real submitImport blocks non-import actions only for multi-file selections",async()=>{
  const h=importHarness({files:[{name:"one.pdf",size:1},{name:"two.pdf",size:1}],action:"translate"});
  await h.submitImport();
  assert.equal(h.calls.some(item=>item[0]==="api"),false);
  assert.match(h.nodes.get("import-msg").textContent,/多篇导入仅支持/);
  const single=importHarness({files:[{name:"one.pdf",size:1}],action:"translate",apiResults:[{job:{id:"one"}}]});
  await single.submitImport();
  assert.equal(single.calls.filter(item=>item[0]==="api").length,1);
});
