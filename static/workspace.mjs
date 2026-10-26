import * as pdfjs from "/static/vendor/pdfjs/pdf.mjs";
import {ContinuousReader} from "/static/reader.mjs";
import {clampMenuPosition,markdownDirectoryFromResult,markdownLocationUrl} from "/static/file-context-menu.mjs";
import {importFilesSequential,normalizeImportResult,summarizeImportResults,validateImportFile} from "/static/import-batch.mjs";
import {createAgentPanel} from "/static/agent-panel.mjs";
import {selectionAfterClick,chunks,pendingTranslationIds} from "/static/library-selection.mjs";
pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.mjs";
const $=id=>document.getElementById(id);
const state={jobs:[],job:null,file:null,files:[],importInFlight:false,page:1,zoom:100,view:"compare",timer:null,docs:{},annotations:{},cacheJobOrder:[],selection:null,selectedAnnotation:null,undo:null,readerPreserve:true,selectRequest:0,jobsRequest:0,deletedJobIds:new Set(),library:{folders:[],documents:{}},expanded:new Set(),folderEdit:null,movingJob:null,movingJobs:null,deletingJob:null,deleteInFlight:null,deletingFolder:null,folderDeleteInFlight:null,folderDeleteRequest:0,draggingJob:null,draggingJobs:[],dragMoveInFlight:null,dragRenderPending:false,dropFolderId:undefined,selectedJobIds:new Set(),selectionAnchor:null,visibleJobIds:[],batch:null,batchFinished:new Set(),memoJobId:null,memoOriginal:null,memoSaving:false};
const key=(side,jobId=state.job?.id)=>String(jobId)+":"+side;
const path=id=>encodeURIComponent(String(id));
let reader;
let renderFrame=0;
let resizeTimer=0;
let fileMenuJob=null;
let fileMenuTrigger=null;
let fileToastTimer=0;
const sidebarPrefs={width:250,collapsed:false,dragging:false,frame:0,pendingWidth:250};
const sidebarKeys={width:"paper2zh.sidebar.width",collapsed:"paper2zh.sidebar.collapsed"};
function sidebarLimits(){const min=180;const agentWidth=agentPrefs.collapsed?0:Number(agentPrefs.width)||0;const max=Math.max(min,Math.min(1000,window.innerWidth-agentWidth-320));return {min,max};}
function readPreference(name,fallback){try{return localStorage.getItem(name)??fallback;}catch(_error){return fallback;}}
function writePreference(name,value){try{localStorage.setItem(name,String(value));}catch(_error){}}
function applySidebarWidth(width,persist=false){const {min,max}=sidebarLimits();sidebarPrefs.width=Math.max(min,Math.min(max,Number(width)||250));const root=document.querySelector(".app");root.style.setProperty("--sidebar-width",sidebarPrefs.collapsed?"0px":sidebarPrefs.width+"px");const handle=$("sidebar-resizer");handle?.setAttribute("aria-valuemin",String(min));handle?.setAttribute("aria-valuemax",String(max));handle?.setAttribute("aria-valuenow",String(Math.round(sidebarPrefs.width)));if(persist){writePreference(sidebarKeys.width,sidebarPrefs.width);saveSidebarBridge();}}
function setSidebarCollapsed(collapsed,persist=true){sidebarPrefs.collapsed=Boolean(collapsed);document.querySelector(".app").classList.toggle("sidebar-collapsed",sidebarPrefs.collapsed);const button=$("sidebar-toggle");button.textContent=sidebarPrefs.collapsed?"›":"‹";button.title=sidebarPrefs.collapsed?"展开侧栏":"折叠侧栏";button.setAttribute("aria-label",sidebarPrefs.collapsed?"展开侧栏":"折叠侧栏");button.setAttribute("aria-expanded",String(!sidebarPrefs.collapsed));if(persist)writePreference(sidebarKeys.collapsed,sidebarPrefs.collapsed);applySidebarWidth(sidebarPrefs.width,persist);}
function toggleSidebar(){setSidebarCollapsed(!sidebarPrefs.collapsed);requestRenderPage();}
function finishSidebarResize(){if(!sidebarPrefs.dragging)return;sidebarPrefs.dragging=false;document.body.classList.remove("sidebar-resizing");if(sidebarPrefs.frame){cancelAnimationFrame(sidebarPrefs.frame);sidebarPrefs.frame=0;}applySidebarWidth(sidebarPrefs.pendingWidth,true);requestRenderPage();}
function startSidebarResize(event){if(sidebarPrefs.collapsed||event.button!==0||event.target.closest?.("#sidebar-toggle"))return;event.preventDefault();sidebarPrefs.dragging=true;sidebarPrefs.pendingWidth=sidebarPrefs.width;document.body.classList.add("sidebar-resizing");const startX=event.clientX,startWidth=sidebarPrefs.width;const move=moveEvent=>{sidebarPrefs.pendingWidth=startWidth+moveEvent.clientX-startX;if(sidebarPrefs.frame)return;sidebarPrefs.frame=requestAnimationFrame(()=>{sidebarPrefs.frame=0;applySidebarWidth(sidebarPrefs.pendingWidth);});};const end=()=>{window.removeEventListener("pointermove",move);window.removeEventListener("pointerup",end);window.removeEventListener("pointercancel",end);window.removeEventListener("blur",end);finishSidebarResize();};window.addEventListener("pointermove",move);window.addEventListener("pointerup",end);window.addEventListener("pointercancel",end);window.addEventListener("blur",end);}
function adjustSidebarByKeyboard(event){const {min,max}=sidebarLimits();if(event.key==="Enter"||event.key===" "){event.preventDefault();toggleSidebar();return;}if(event.key==="Home"){event.preventDefault();setSidebarCollapsed(false);applySidebarWidth(min,true);requestRenderPage();return;}if(event.key==="End"){event.preventDefault();setSidebarCollapsed(false);applySidebarWidth(max,true);requestRenderPage();return;}if(!["ArrowLeft","ArrowRight"].includes(event.key))return;event.preventDefault();if(sidebarPrefs.collapsed)setSidebarCollapsed(false,true);const delta=event.key==="ArrowLeft"?-16:16;applySidebarWidth(sidebarPrefs.width+delta,true);requestRenderPage();}
function saveSidebarBridge(){const bridge=window.pywebview?.api;if(typeof bridge?.save_workspace_preferences!=="function")return;Promise.resolve(bridge.save_workspace_preferences({sidebar_width:Math.round(sidebarPrefs.width),sidebar_collapsed:sidebarPrefs.collapsed})).catch(_error=>{});}
async function syncSidebarPreferences(){const bridge=window.pywebview?.api;if(typeof bridge?.get_workspace_preferences!=="function")return;try{const prefs=await bridge.get_workspace_preferences();if(Number.isFinite(Number(prefs?.sidebar_width)))sidebarPrefs.width=Number(prefs.sidebar_width);if(typeof prefs?.sidebar_collapsed==="boolean")sidebarPrefs.collapsed=prefs.sidebar_collapsed;setSidebarCollapsed(sidebarPrefs.collapsed,false);applySidebarWidth(sidebarPrefs.width);writePreference(sidebarKeys.width,sidebarPrefs.width);writePreference(sidebarKeys.collapsed,sidebarPrefs.collapsed);requestRenderPage();}catch(_error){}}
function initSidebar(){const savedWidth=Number(readPreference(sidebarKeys.width,250));sidebarPrefs.collapsed=readPreference(sidebarKeys.collapsed,"false")==="true";sidebarPrefs.width=Number.isFinite(savedWidth)?savedWidth:250;applySidebarWidth(sidebarPrefs.width);setSidebarCollapsed(sidebarPrefs.collapsed,false);const handle=$("sidebar-resizer");handle.addEventListener("pointerdown",startSidebarResize);handle.addEventListener("keydown",adjustSidebarByKeyboard);$("sidebar-toggle").addEventListener("click",event=>{event.stopPropagation();toggleSidebar();});window.addEventListener("pywebviewready",syncSidebarPreferences);syncSidebarPreferences();}
const agentPrefs={width:360,collapsed:false,dragging:false,frame:0,pendingWidth:360};
const agentKeys={width:"paper2zh.agent.width",collapsed:"paper2zh.agent.collapsed"};
function agentLimits(){const min=300;const max=Math.max(min,Math.min(680,window.innerWidth-560));return {min,max};}
function applyAgentWidth(width,persist=false){const {min,max}=agentLimits();agentPrefs.width=Math.max(min,Math.min(max,Number(width)||360));document.querySelector(".app").style.setProperty("--agent-width",agentPrefs.width+"px");const handle=$("agent-resizer");handle?.setAttribute("aria-valuemin",String(min));handle?.setAttribute("aria-valuemax",String(max));handle?.setAttribute("aria-valuenow",String(Math.round(agentPrefs.width)));if(typeof applySidebarWidth==="function")applySidebarWidth(sidebarPrefs.width);if(persist)writePreference(agentKeys.width,agentPrefs.width);}
function setAgentCollapsed(collapsed,persist=true){agentPrefs.collapsed=Boolean(collapsed);document.querySelector(".app").classList.toggle("agent-collapsed",agentPrefs.collapsed);const toggle=$("toggle-agent");if(toggle){toggle.title=agentPrefs.collapsed?"显示助手":"隐藏助手";toggle.setAttribute("aria-pressed",String(!agentPrefs.collapsed));}if(persist)writePreference(agentKeys.collapsed,agentPrefs.collapsed);applyAgentWidth(agentPrefs.width,persist);}
function startAgentResize(event){if(agentPrefs.collapsed||event.button!==0)return;event.preventDefault();agentPrefs.dragging=true;agentPrefs.pendingWidth=agentPrefs.width;document.body.classList.add("agent-resizing");const startX=event.clientX,startWidth=agentPrefs.width;const move=moveEvent=>{agentPrefs.pendingWidth=startWidth-(moveEvent.clientX-startX);if(agentPrefs.frame)return;agentPrefs.frame=requestAnimationFrame(()=>{agentPrefs.frame=0;applyAgentWidth(agentPrefs.pendingWidth);});};const end=()=>{window.removeEventListener("pointermove",move);window.removeEventListener("pointerup",end);window.removeEventListener("pointercancel",end);window.removeEventListener("blur",end);if(agentPrefs.frame){cancelAnimationFrame(agentPrefs.frame);agentPrefs.frame=0;}agentPrefs.dragging=false;document.body.classList.remove("agent-resizing");applyAgentWidth(agentPrefs.pendingWidth,true);requestRenderPage();};window.addEventListener("pointermove",move);window.addEventListener("pointerup",end);window.addEventListener("pointercancel",end);window.addEventListener("blur",end);}
function initAgent(){const savedWidth=Number(readPreference(agentKeys.width,360));agentPrefs.width=Number.isFinite(savedWidth)?savedWidth:360;const stored=readPreference(agentKeys.collapsed,null);agentPrefs.collapsed=stored===null?window.innerWidth<980:stored==="true";applyAgentWidth(agentPrefs.width);setAgentCollapsed(agentPrefs.collapsed,false);$("agent-resizer").addEventListener("pointerdown",startAgentResize);$("toggle-agent").addEventListener("click",()=>{setAgentCollapsed(!agentPrefs.collapsed);requestRenderPage();});$("agent-collapse").addEventListener("click",()=>{setAgentCollapsed(true);requestRenderPage();});}
function msg(id,text,kind=""){if(id==="status"){$("status-text").textContent=text;$("status").className="status"+(kind?" "+kind:"");return;}const el=$(id);el.textContent=text;el.className="msg"+(kind?" "+kind:"");}
function reportClientError(context={}){const payload={event:context.event||"pdf_preview_failed"};for(const key of ["pdf","page","job_id"]){if(context[key]!==undefined)payload[key]=context[key];}try{fetch("/api/diagnostics/client-error",{method:"POST",headers:{"Content-Type":"application/json","X-Paper-Translator":"1"},body:JSON.stringify(payload),keepalive:true}).catch(()=>{});}catch(_error){}}
function open(id){if(id==="import-modal"&&state.importInFlight)return;const modal=$(id);modal.classList.add("open");if(id==="delete-modal")$("delete-cancel")?.focus?.();if(id==="folder-delete-modal")$("folder-delete-cancel")?.focus?.();if(id==="settings-modal")loadSettings();}
function close(id,internal=false){if(id==="import-modal"&&state.importInFlight&&!internal)return;if(id==="delete-modal"&&state.deleteInFlight)return;if(id==="folder-delete-modal"&&state.folderDeleteInFlight)return;if(id==="memo-modal"&&!internal&&state.memoOriginal!==null&&$("memo-content").value!==state.memoOriginal){if(!window.confirm("备忘录还没有保存，确定关闭吗？"))return;}$(id).classList.remove("open");if(id==="delete-modal")state.deletingJob=null;if(id==="folder-delete-modal")state.deletingFolder=null;if(id==="memo-modal"){state.memoJobId=null;state.memoOriginal=null;}}
async function api(url,options={}){const headers=new Headers(options.headers||{});if(options.method&&options.method!=="GET")headers.set("X-Paper-Translator","1");const response=await fetch(url,{...options,headers});const data=await response.json();if(!response.ok)throw Error(data.error||"请求失败");return data;}
function post(url,data){return api(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});}
function label(job){if(job.status==="completed")return job.demo_mode?"演示完成":"翻译完成";if(job.status==="failed")return "失败";if(job.status==="running")return "翻译中";if(job.status==="queued")return "等待处理";return "已导入";}
function hasTranslation(job){return Boolean(job&&(job.translated_file||job.status==="completed"&&!job.import_only));}
function jobTitle(job){return job?.translated_title||job?.display_title||job?.filename||"";}
async function loadLibrary(){try{state.library=await api("/api/library");for(const folder of state.library.folders||[])state.expanded.add(folder.id);renderList();}catch(error){msg("status","文件夹读取失败："+error.message,"error");}}
function folderOptions(select,selected=null,exclude=null){select.replaceChildren();const root=document.createElement("option");root.value="";root.textContent="根目录";select.append(root);const walk=(parent,depth)=>{for(const folder of state.library.folders.filter(f=>f.parent_id===parent)){if(folder.id===exclude)continue;const option=document.createElement("option");option.value=folder.id;option.textContent="　".repeat(depth)+folder.name;select.append(option);walk(folder.id,depth+1);}};walk(null,0);select.value=selected||"";}
function folderModal(action,id=null,parent=null){state.folderEdit={action,id};const folder=state.library.folders.find(f=>f.id===id);$("folder-title").textContent=action==="create_folder"?"新建文件夹":action==="rename_folder"?"重命名文件夹":"移动文件夹";$("folder-name-wrap").hidden=action==="move_folder";$("folder-name").value=action==="create_folder"?"":folder?.name||"";$("folder-parent").parentElement.hidden=action==="rename_folder";folderOptions($("folder-parent"),parent??folder?.parent_id??null,action==="move_folder"?id:null);msg("folder-msg","");open("folder-modal");}
async function submitFolder(){const edit=state.folderEdit;if(!edit)return;const name=$("folder-name").value.trim(),parent_id=$("folder-parent").value||null;if(edit.action!=="move_folder"&&!name){msg("folder-msg","请输入文件夹名称。","error");return;}const payload={action:edit.action};if(edit.action==="create_folder")Object.assign(payload,{name,parent_id});else if(edit.action==="rename_folder")Object.assign(payload,{id:edit.id,name});else Object.assign(payload,{id:edit.id,parent_id});try{state.library=await post("/api/library",payload);if(edit.action==="create_folder")for(const folder of state.library.folders)if(folder.name===name&&folder.parent_id===parent_id)state.expanded.add(folder.id);close("folder-modal");renderList();}catch(error){msg("folder-msg",error.message,"error");}}
function moveDocument(job){moveDocuments([job]);}
function moveDocuments(jobs){
  const list=(jobs||[]).filter(job=>job?.id);
  if(!list.length)return;
  state.movingJobs=list;state.movingJob=list[0];
  $("move-name").textContent=list.length===1?(list[0].filename||"未命名论文"):`已选择 ${list.length} 篇论文`;
  folderOptions($("move-destination"),state.library.documents?.[list[0].id]??null);
  msg("move-msg","");
  open("move-modal");
}
async function submitMove(){
  const jobs=state.movingJobs?.length?state.movingJobs:(state.movingJob?[state.movingJob]:[]);
  if(!jobs.length)return;
  const button=$("submit-move");button.disabled=true;
  try{
    if(jobs.length===1){
      state.library=await post("/api/library",{action:"move_document",job_id:jobs[0].id,folder_id:$("move-destination").value||null});
      close("move-modal");renderList();return;
    }
    let library=state.library,failed=0;
    for(const job of jobs){
      try{const result=await post("/api/library",{action:"move_document",job_id:job.id,folder_id:$("move-destination").value||null});library=result?.library||result||library;}
      catch(_error){failed++;}
    }
    state.library=library;close("move-modal");renderList();
    if(failed)showFileToast(`已移动 ${jobs.length-failed} 篇，${failed} 篇失败`,"error");
  }catch(error){msg("move-msg",error.message,"error");}finally{button.disabled=false;}
}
function folderForId(id){return state.library.folders?.find(folder=>String(folder.id)===String(id))||null;}
function deleteFolder(folder){if(!folder?.id||state.folderDeleteInFlight||$("folder-delete-modal").classList.contains("open"))return;state.deletingFolder={id:folder.id,name:folder.name||"未命名文件夹",parent_id:folder.parent_id??null};$("folder-delete-name").textContent=state.deletingFolder.name;msg("folder-delete-msg","");open("folder-delete-modal");}
async function submitFolderDelete(){const folder=state.deletingFolder;if(!folder?.id||state.folderDeleteInFlight)return;const button=$("submit-folder-delete"),request=++state.folderDeleteRequest;state.folderDeleteInFlight=String(folder.id);button.disabled=true;msg("folder-delete-msg","正在删除分类…");try{const result=await post("/api/library",{action:"delete_folder",id:folder.id});if(request!==state.folderDeleteRequest||state.deletingFolder?.id!==folder.id)return;state.library=result?.library||result;const activeIds=new Set((state.library.folders||[]).map(item=>String(item.id)));for(const id of [...state.expanded])if(!activeIds.has(String(id)))state.expanded.delete(id);const parent=folder.parent_id??null;if(parent!==null)state.expanded.add(parent);state.deletingFolder=null;state.folderDeleteInFlight=null;close("folder-delete-modal");renderList();}catch(error){if(request===state.folderDeleteRequest)msg("folder-delete-msg",error.message||"删除分类失败","error");}finally{if(request===state.folderDeleteRequest){state.folderDeleteInFlight=null;button.disabled=false;}}}
function knownDraggedJobs(){
  const ids=(state.draggingJobs||[]).map(item=>String(item.id));
  if(!ids.length&&state.draggingJob?.id!=null)ids.push(String(state.draggingJob.id));
  return ids.map(id=>state.jobs.find(job=>String(job.id)===id)).filter(job=>job&&!isDeletedJob(job.id));
}
function knownDraggedJob(){return knownDraggedJobs()[0]||null;}
function clearDropTarget(){document.querySelectorAll(".drop-target").forEach(node=>node.classList.remove("drop-target"));state.dropFolderId=undefined;}
function clearDocumentDrag({render=true}={}){state.draggingJob=null;state.draggingJobs=[];clearDropTarget();document.querySelectorAll(".dragging").forEach(node=>node.classList.remove("dragging"));if(render&&state.dragRenderPending){state.dragRenderPending=false;renderList();}}
function startDocumentDrag(event,job){
  if(!job?.id||state.dragMoveInFlight||!state.jobs.some(item=>String(item.id)===String(job.id))||isDeletedJob(job.id))return;
  const selected=state.selectedJobIds&&typeof state.selectedJobIds.size==="number"?[...state.selectedJobIds].map(String):[];
  const ids=selected.includes(String(job.id))?selected:[String(job.id)];
  const jobs=ids.map(id=>state.jobs.find(item=>String(item.id)===id)).filter(item=>item&&!isDeletedJob(item.id));
  if(!jobs.length)return;
  const payload=jobs.map(item=>String(item.id));
  state.draggingJobs=jobs.map(item=>({id:item.id}));
  state.draggingJob={id:jobs[0].id};
  state.dragRenderPending=false;
  event.dataTransfer?.setData?.("application/x-paper2zh-jobs",JSON.stringify(payload));
  event.dataTransfer?.setData?.("application/x-paper2zh-job",String(job.id));
  event.dataTransfer?.setData?.("text/plain",payload.join("\n"));
  if(event.dataTransfer)event.dataTransfer.effectAllowed="move";
  event.currentTarget?.classList?.add("dragging");
  document.querySelectorAll?.("[data-job-id]")?.forEach?.(node=>{if(payload.includes(String(node.dataset?.jobId)))node.classList.add("dragging");});
}
function canHandleDocumentDrop(event){if(!knownDraggedJobs().length)return false;const types=event?.dataTransfer?.types;if(types?.length&&!Array.from(types).some(type=>type==="application/x-paper2zh-job"||type==="application/x-paper2zh-jobs"))return false;return true;}
function documentDragOver(event,folderId,target){if(!canHandleDocumentDrop(event))return;event.preventDefault();event.stopPropagation();if(event.dataTransfer)event.dataTransfer.dropEffect="move";if(state.dropFolderId!==folderId){clearDropTarget();state.dropFolderId=folderId;target.classList.add("drop-target");}else target.classList.add("drop-target");}
function documentDragLeave(event,target){if(!target.contains(event.relatedTarget)){target.classList.remove("drop-target");if(String(state.dropFolderId??"")===String(target.dataset.folderId??""))state.dropFolderId=undefined;}}
async function moveDocumentByDrag(folderId,target){
  const jobs=knownDraggedJobs();
  if(!jobs.length||state.dragMoveInFlight)return;
  const moving=jobs.filter(job=>{const current=state.library.documents?.[job.id]??null;const same=current===null&&folderId===null||current!==null&&folderId!==null&&String(current)===String(folderId);return !same;});
  if(!moving.length){clearDocumentDrag();return;}
  state.dragMoveInFlight=true;clearDropTarget();
  let library=state.library,failed=0;
  try{
    for(const job of moving){
      try{const result=await post("/api/library",{action:"move_document",job_id:job.id,folder_id:folderId??null});library=result?.library||result||library;}
      catch(_error){failed++;}
    }
    state.library=library;
    if(folderId!==null)state.expanded.add(folderId);
    if(!failed&&state.selectedJobIds?.clear){state.selectedJobIds.clear();state.selectionAnchor=null;}
    const pending=state.dragRenderPending;
    clearDocumentDrag();
    if(!pending)renderList();
    if(failed)showFileToast(`已移动 ${moving.length-failed} 篇，${failed} 篇失败`,"error");
    else if(moving.length>1)showFileToast(`已移动 ${moving.length} 篇论文`);
  }catch(error){clearDocumentDrag();showFileToast(error.message||"移动论文失败","error");}
  finally{state.dragMoveInFlight=null;if(typeof updateActions==="function")updateActions();}
}
function documentDrop(event,folderId,target){if(!canHandleDocumentDrop(event))return;event.preventDefault();event.stopPropagation();target.classList.remove("drop-target");moveDocumentByDrag(folderId,target);}
function bindDocumentDropTarget(target,folderId){target.dataset.folderId=folderId===null?"":String(folderId);target.addEventListener("dragover",event=>documentDragOver(event,folderId,target));target.addEventListener("dragenter",event=>documentDragOver(event,folderId,target));target.addEventListener("dragleave",event=>documentDragLeave(event,target));target.addEventListener("drop",event=>documentDrop(event,folderId,target));}
const uiIcons={
  file:'<svg viewBox="0 0 16 16" focusable="false"><path d="M3.5 1.75h5.25L12.5 5.5v8.75h-9z"/><path d="M8.75 1.75V5.5h3.75"/><path d="M5.5 8h5M5.5 10.5h5"/></svg>',
  paper:'<svg viewBox="0 0 16 16" focusable="false"><path d="M3.5 1.75h5.25L12.5 5.5v8.75h-9z"/><path d="M8.75 1.75V5.5h3.75"/><path d="M5.5 8h5M5.5 10.5h5"/></svg>',
  library:'<svg viewBox="0 0 16 16" focusable="false"><path d="M2 13.5V2.5M4.6 13.5V2.5M8.4 13.5V4.6l2.6-.9v9.8M12.4 13.5V4.4l1.4-.4"/></svg>',
  folder:'<svg viewBox="0 0 16 16" focusable="false"><path d="M1.75 4.5h4l1.25 1.5h7.25v7.75h-12.5z"/><path d="M1.75 4.5V3.25h4l1.25 1.25"/></svg>',
  "folder-open":'<svg viewBox="0 0 16 16" focusable="false"><path d="M1.75 4.5h4l1.25 1.5h7.25v1.25H3.5l-1.75 6V4.5z"/><path d="M1.75 13.25 3.5 7.25h11.25l-1.75 6z"/></svg>',
  "chevron-right":'<svg viewBox="0 0 16 16" focusable="false"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>',
  "chevron-down":'<svg viewBox="0 0 16 16" focusable="false"><path d="m3.5 6 4.5 4.5L12.5 6"/></svg>',
  more:'<svg viewBox="0 0 16 16" focusable="false"><circle cx="3.25" cy="8" r="1"/><circle cx="8" cy="8" r="1"/><circle cx="12.75" cy="8" r="1"/></svg>',
  copy:'<svg viewBox="0 0 16 16" focusable="false"><rect x="5" y="4.5" width="7.25" height="9" rx="1"/><path d="M3.75 11.5h-.5a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1h6.5a1 1 0 0 1 1 1V3"/></svg>',
  plus:'<svg viewBox="0 0 16 16" focusable="false"><path d="M8 3v10M3 8h10"/></svg>',
  edit:'<svg viewBox="0 0 16 16" focusable="false"><path d="m3 11.75-.45 1.7 1.7-.45 7.8-7.8-1.25-1.25z"/><path d="m10.8 3.95 1.25 1.25 1-1a.88.88 0 0 0-1.25-1.25z"/></svg>',
  trash:'<svg viewBox="0 0 16 16" focusable="false"><path d="M3.5 4.5h9M6 4.5V3h4v1.5M5 6.25v6.5h6v-6.5M7 7.75v3.5M9 7.75v3.5"/></svg>',
};
function iconSvg(name){const icon=document.createElement("span");icon.className="ui-icon";icon.setAttribute("aria-hidden","true");icon.innerHTML=uiIcons[name]||"";return icon;}
function openFileMenu(event,job,trigger=event.currentTarget){event?.preventDefault?.();event?.stopPropagation?.();const menu=$("file-context-menu");fileMenuJob=job;fileMenuTrigger=trigger;trigger?.setAttribute?.("aria-haspopup","menu");trigger?.setAttribute?.("aria-expanded","true");menu.hidden=false;menu.classList.add("open");const multi=state.selectedJobIds instanceof Set&&state.selectedJobIds.size>1&&state.selectedJobIds.has(String(job.id));const moveItem=menu.querySelector('[data-file-action="move"]');const moveLabel=moveItem?.lastChild;if(moveLabel&&moveLabel.nodeType===3)moveLabel.textContent=multi?` 移动所选 ${state.selectedJobIds.size} 篇到文件夹`:" 移动到文件夹";menu.querySelector('[data-file-action="copy-markdown"]').hidden=multi;menu.querySelector('[data-file-action="delete"]').hidden=multi;const rect=trigger?.getBoundingClientRect?.()||{left:event?.clientX||8,top:event?.clientY||8,right:event?.clientX||8,bottom:event?.clientY||8};const menuRect=menu.getBoundingClientRect();const point=clampMenuPosition({x:event?.clientX??rect.left,y:event?.clientY??rect.bottom,width:menuRect.width,height:menuRect.height,viewportWidth:window.innerWidth,viewportHeight:window.innerHeight});menu.style.left=point.left+"px";menu.style.top=point.top+"px";menu.querySelector("[role=menuitem]")?.focus();}
function closeFileMenu(restoreFocus=true){const menu=$("file-context-menu");menu.classList.remove("open");menu.hidden=true;const trigger=fileMenuTrigger;trigger?.setAttribute?.("aria-expanded","false");fileMenuJob=null;fileMenuTrigger=null;if(restoreFocus)trigger?.focus?.();}
function showFileToast(text,kind=""){const toast=$("file-toast");if(fileToastTimer)clearTimeout(fileToastTimer);toast.textContent=text;toast.className="file-toast"+(kind?" "+kind:"");toast.hidden=false;fileToastTimer=setTimeout(()=>{toast.hidden=true;fileToastTimer=0;},2600);}
async function copyText(text){if(navigator.clipboard?.writeText){try{await navigator.clipboard.writeText(text);return;}catch(_error){}}
  const area=document.createElement("textarea");area.value=text;area.setAttribute("readonly","");area.style.position="fixed";area.style.opacity="0";document.body.append(area);area.select();let copied=false;try{copied=document.execCommand("copy");}catch(_error){}area.remove();if(!copied)throw Error("系统未能访问剪贴板，目录地址为："+text);}
async function copyMarkdownDirectory(job){if(!job?.id)throw Error("未找到要复制的论文。");const bridge=window.pywebview?.api;if(typeof bridge?.copy_markdown_directory==="function"){const result=await bridge.copy_markdown_directory(job.id);if(result?.error)throw Error(result.error);if(!result?.copied)throw Error("Markdown 目录尚未生成，暂时无法复制。");markdownDirectoryFromResult(result);return result.directory;}
  const result=await api(markdownLocationUrl(job.id));const directory=markdownDirectoryFromResult(result);await copyText(directory);return directory;}
function isDeletedJob(jobId){return state.deletedJobIds.has(String(jobId));}
function deleteDocument(job){if(!job?.id||isDeletedJob(job.id)||state.deleteInFlight||$("delete-modal").classList.contains("open"))return;state.deletingJob={id:job.id,title:jobTitle(job)||job.filename||"未命名论文"};$("delete-name").textContent=state.deletingJob.title;msg("delete-msg","");open("delete-modal");}
function clearDeletedJob(jobId){const id=String(jobId),oldJobs=state.jobs.slice(),deletedIndex=oldJobs.findIndex(job=>String(job.id)===id),deletingCurrent=state.job?.id!=null&&String(state.job.id)===id;state.deletedJobIds.add(id);state.selectedJobIds?.delete?.(id);state.jobsRequest++;state.selectRequest++;if(deletingCurrent&&state.timer){clearTimeout(state.timer);state.timer=null;}state.jobs=oldJobs.filter(job=>String(job.id)!==id);for(const cacheKey of Object.keys(state.docs)){if(cacheKey.startsWith(id+":"))disposeDocument(cacheKey);}for(const cacheKey of Object.keys(state.annotations)){if(cacheKey.startsWith(id+":"))delete state.annotations[cacheKey];}state.cacheJobOrder=state.cacheJobOrder.filter(cacheId=>String(cacheId)!==id);const deletedSelection=state.selection?.jobId!=null&&String(state.selection.jobId)===id;if(deletedSelection){state.selection=null;$("annotation-menu").classList.remove("open");}if(state.selectedAnnotation?.jobId!=null&&String(state.selectedAnnotation.jobId)===id)state.selectedAnnotation=null;if(state.undo?.jobId!=null&&String(state.undo.jobId)===id)state.undo=null;if(deletingCurrent){reader?.destroy();state.job=null;state.page=1;state.zoom=100;$("empty").hidden=false;$("pages").hidden=true;$("pagebar").hidden=true;$("title").textContent="选择一篇论文";$("notice").textContent="";$("notice").classList.remove("show");$("progress-wrap").hidden=true;$("progress-stage").textContent="正在准备";$("progress-value").textContent="处理中";$("progress-fill").style.width="";$("token-usage").textContent="Token 未统计";$("status-text").textContent="就绪";$("page-number").value=1;$("total").textContent="—";$("zoom-label").textContent="100%";$("open-translate").disabled=true;$("open-download").disabled=true;agentPanel.setPaper(null);const next=state.jobs[deletedIndex]||state.jobs[deletedIndex-1];return next?.id??null;}renderList();return null;}
async function submitDelete(){const job=state.deletingJob;if(!job?.id||isDeletedJob(job.id)||state.deleteInFlight)return;const button=$("submit-delete");state.deleteInFlight=String(job.id);button.disabled=true;msg("delete-msg","正在移除文献库副本…");try{const result=await post("/api/jobs/"+path(job.id)+"/delete",{});const nextId=clearDeletedJob(job.id);state.deletingJob=null;state.deleteInFlight=null;close("delete-modal");state.library=result.library||await api("/api/library");await loadJobs(false);if(!state.job&&state.jobs.length)await select(nextId&&state.jobs.some(item=>String(item.id)===String(nextId))?nextId:state.jobs[0].id);renderList();msg("status","已从文献库移除，软件托管文件和记录已清理，外部原始 PDF 未删除。");}catch(error){msg("delete-msg",error.message||"删除失败","error");}finally{state.deleteInFlight=null;button.disabled=false;}}
async function runFileMenuAction(action){const job=fileMenuJob;closeFileMenu();if(!job)return;if(action==="move"){const ids=state.selectedJobIds instanceof Set&&state.selectedJobIds.size>1&&state.selectedJobIds.has(String(job.id))?[...state.selectedJobIds]:[job.id];const jobs=ids.map(id=>state.jobs.find(item=>String(item.id)===String(id))).filter(Boolean);moveDocuments(jobs);return;}if(action==="delete"){deleteDocument(job);return;}if(action!=="copy-markdown")return;try{await copyMarkdownDirectory(job);showFileToast("Markdown 目录地址已复制");}catch(error){showFileToast(error.message||"目录地址复制失败","error");}}
function renderList(){
  if(state.draggingJob){state.dragRenderPending=true;return;}
  if(fileMenuJob)closeFileMenu(false);
  const box=$("files");box.replaceChildren();
  const folders=state.library.folders||[],mapping=state.library.documents||{},jobs=state.jobs.filter(job=>!isDeletedJob(job.id)),visible=[];
  const rootTarget=document.createElement("div");
  rootTarget.className="library-root-drop";
  rootTarget.setAttribute("role","button");
  rootTarget.setAttribute("aria-label","论文库根目录，拖到这里放回论文");
  const rootIcon=iconSvg("library");rootIcon.classList.add("row-icon");
  const rootLabel=document.createElement("span");rootLabel.className="row-label";rootLabel.textContent="论文库";
  const rootCount=document.createElement("span");rootCount.className="root-count";rootCount.textContent=String(jobs.length);
  rootTarget.append(rootIcon,rootLabel,rootCount);
  bindDocumentDropTarget(rootTarget,null);
  box.append(rootTarget);
  const addJob=(job,depth)=>{
    const row=document.createElement("div");
    row.className="file-row";
    row.dataset.jobId=String(job.id);
    visible.push(String(job.id));
    row.style.paddingLeft=(6+depth*14)+"px";
    row.draggable=true;
    const button=document.createElement("button");
    button.type="button";
    button.className="file"+(state.job?.id===job.id?" active":"");
    const icon=iconSvg("paper");icon.classList.add("row-icon");
    const name=document.createElement("span");name.className="row-label";
    name.textContent=jobTitle(job)||job.filename||"未命名论文";
    button.append(icon,name);
    const status=job.status;
    if(status==="running"||status==="queued"||status==="failed"){
      const dot=document.createElement("span");
      dot.className="status-dot "+(status==="failed"?"failed":"running");
      dot.title=label(job);
      button.append(dot);
    }
    button.title=(job.filename||"")+" · "+label(job)+" · "+(job.page_count||"?")+" 页";
    if(state.job?.id===job.id)row.classList.add("active");
    if(state.selectedJobIds.has(String(job.id))&&state.job?.id!==job.id)row.classList.add("selected");
    button.addEventListener("click",event=>{
      closeFileMenu(false);
      if(event.ctrlKey||event.metaKey||event.shiftKey){
        const result=selectionAfterClick(state.selectedJobIds,state.visibleJobIds,state.selectionAnchor,job.id,{ctrl:event.ctrlKey||event.metaKey,meta:false,shift:event.shiftKey});
        state.selectedJobIds=new Set(result.selected);
        state.selectionAnchor=result.anchor;
        renderList();updateActions();
        return;
      }
      state.selectedJobIds.clear();state.selectionAnchor=job.id;
      select(job.id);
    });
    button.addEventListener("contextmenu",event=>openFileMenu(event,job,button));
    button.addEventListener("keydown",event=>{if((event.key==="ContextMenu")||(event.shiftKey&&event.key==="F10")){event.preventDefault();openFileMenu(event,job,button);}});
    const menuButton=document.createElement("button");
    menuButton.className="file-menu-trigger";
    menuButton.type="button";
    menuButton.title="更多论文操作";
    menuButton.setAttribute("aria-label","打开论文操作菜单");
    menuButton.append(iconSvg("more"));
    menuButton.addEventListener("click",event=>openFileMenu(event,job,menuButton));
    menuButton.addEventListener("keydown",event=>{if(event.key==="ContextMenu"||(event.shiftKey&&event.key==="F10")){event.preventDefault();openFileMenu(event,job,menuButton);}});
    row.addEventListener("dragstart",event=>startDocumentDrag(event,job));
    row.addEventListener("dragend",()=>clearDocumentDrag());
    row.append(button,menuButton);
    box.append(row);
  };
  const walk=(parent,depth)=>{
    for(const folder of folders.filter(f=>f.parent_id===parent)){
      const expanded=state.expanded.has(folder.id);
      const row=document.createElement("div");
      row.className="folder-row"+(expanded?" is-open":"");
      row.style.paddingLeft=(6+depth*14)+"px";
      const toggle=document.createElement("button");
      toggle.type="button";
      toggle.className="folder-toggle";
      const disclosure=iconSvg(expanded?"chevron-down":"chevron-right");disclosure.classList.add("disclosure");
      const folderIcon=iconSvg(expanded?"folder-open":"folder");folderIcon.classList.add("folder-glyph");
      const folderName=document.createElement("span");folderName.className="row-label";folderName.textContent=folder.name;
      toggle.append(disclosure,folderIcon,folderName);
      toggle.title=folder.name;
      toggle.setAttribute("aria-expanded",String(expanded));
      toggle.addEventListener("click",()=>{expanded?state.expanded.delete(folder.id):state.expanded.add(folder.id);renderList();});
      row.append(toggle);
      bindDocumentDropTarget(row,folder.id);
      for(const [title,icon,action] of [["新建子文件夹","plus","create_folder"],["重命名","edit","rename_folder"],["移动文件夹","folder","move_folder"],["删除文件夹","trash","delete_folder"]]){
        const actionButton=document.createElement("button");
        actionButton.type="button";
        actionButton.className="folder-action";
        actionButton.title=title;
        actionButton.setAttribute("aria-label",title);
        actionButton.append(iconSvg(icon));
        actionButton.addEventListener("click",event=>{event.stopPropagation();if(action==="delete_folder")deleteFolder(folder);else folderModal(action,folder.id,action==="create_folder"?folder.id:null);});
        row.append(actionButton);
      }
      box.append(row);
      if(expanded){walk(folder.id,depth+1);for(const job of jobs.filter(j=>String(mapping[j.id])===String(folder.id)))addJob(job,depth+1);}
    }
  };
  walk(null,0);
  for(const job of jobs.filter(j=>!mapping[j.id]))addJob(job,0);
  if(!folders.length&&!jobs.length){const empty=document.createElement("div");empty.className="files-empty";empty.textContent="还没有论文。";box.append(empty);}
  state.visibleJobIds=visible;
  updateActions();
}

async function openMemo(){
  const job=state.job;
  if(!job)return;
  const jobId=String(job.id);
  state.memoJobId=jobId;state.memoOriginal=null;
  $("memo-paper").textContent=jobTitle(job)||job.filename||"当前论文";
  $("memo-content").value="";
  $("memo-status").textContent="正在读取…";$("memo-status").className="msg";
  $("save-memo").disabled=true;
  open("memo-modal");
  try{
    const data=await api("/api/jobs/"+path(jobId)+"/memo");
    if(state.memoJobId!==jobId)return;
    const content=String(data.content||"");
    $("memo-content").value=content;state.memoOriginal=content;
    $("memo-status").textContent=data.updated_at?("上次保存："+new Date(data.updated_at).toLocaleString()):"还没有备忘录";
    $("save-memo").disabled=false;
    $("memo-content").focus();
  }catch(error){
    if(state.memoJobId!==jobId)return;
    state.memoOriginal="";
    $("memo-status").textContent=error.message||"备忘录读取失败";$("memo-status").className="msg error";
    $("save-memo").disabled=false;
  }
}
async function saveMemo(){
  const jobId=state.memoJobId;
  if(!jobId||state.memoSaving)return;
  const content=$("memo-content").value;
  if(content.length>200000){$("memo-status").textContent="备忘录过长，请控制在 20 万字以内。";$("memo-status").className="msg error";return;}
  const button=$("save-memo");button.disabled=true;state.memoSaving=true;
  $("memo-status").textContent="正在保存…";$("memo-status").className="msg";
  try{
    const data=await post("/api/jobs/"+path(jobId)+"/memo",{content});
    if(state.memoJobId!==jobId)return;
    state.memoOriginal=content;
    $("memo-status").textContent="已保存 "+new Date(data.updated_at||Date.now()).toLocaleString();
    $("memo-status").className="msg ok";
    showFileToast("备忘录已保存");
    close("memo-modal");
  }catch(error){
    $("memo-status").textContent=error.message||"备忘录保存失败";$("memo-status").className="msg error";
  }finally{state.memoSaving=false;button.disabled=false;}
}
function updateActions(){
  const count=state.selectedJobIds?.size||0;
  const multi=count>1;
  const chip=$("selection-chip");
  if(chip){chip.hidden=!multi;if(multi)$("selection-count").textContent=`已选 ${count} 篇`;}
  const label=$("translate-label");
  if(label)label.textContent=multi?`翻译所选 (${count})`:"翻译";
  const busy=Boolean(state.batch?.active);
  const translate=$("open-translate");
  if(translate)translate.disabled=busy||(multi?false:(!state.job||["queued","running"].includes(state.job.status)));
  const download=$("open-download");
  if(download)download.disabled=multi||!state.job||!hasTranslation(state.job);
  const memo=$("open-memo");
  if(memo)memo.disabled=!state.job;
}
function clearSelection(){if(!state.selectedJobIds?.size)return;state.selectedJobIds.clear();state.selectionAnchor=null;renderList();updateActions();}
function sleep(ms){return new Promise(resolve=>setTimeout(resolve,ms));}
function openBatchTranslate(ids){
  const eligible=pendingTranslationIds(state.jobs,ids);
  if(!eligible.length){showFileToast("所选论文都已翻译或正在翻译","error");return;}
  const pages=eligible.reduce((sum,id)=>{const job=state.jobs.find(item=>String(item.id)===id);return sum+(Number(job?.page_count)||0);},0);
  state.batchRequest={ids:eligible};
  $("batch-translate-summary").textContent=`已选 ${ids.length} 篇，其中 ${eligible.length} 篇需要翻译，共约 ${pages} 页。`;
  msg("batch-translate-msg","");
  open("batch-translate-modal");
}
function submitBatchTranslate(){const ids=state.batchRequest?.ids||[];close("batch-translate-modal");if(ids.length)void runBatchTranslation(ids);}
function updateBatchBar(){
  const batch=state.batch,wrap=$("batch-progress-wrap");
  if(!wrap)return;
  if(!batch||!batch.active){wrap.hidden=true;return;}
  wrap.hidden=false;
  const total=batch.ids.length,finished=batch.finished.size,failed=batch.failed.size;
  $("batch-progress-stage").textContent=`批量翻译 · ${finished}/${total} 完成`+(failed?` · ${failed} 失败`:"");
  $("batch-progress-value").textContent=`${Math.round(finished/total*100)}%`;
  $("batch-progress-fill").style.width=`${Math.max(0,Math.min(100,finished/total*100))}%`;
}
async function runBatchTranslation(ids){
  const batch={ids:[...ids],finished:new Set(),failed:new Set(),active:true};
  state.batch=batch;updateActions();updateBatchBar();
  for(const group of chunks(batch.ids,3)){
    const started=[];
    for(const id of group){
      try{await post("/api/jobs/"+path(id)+"/translate",{mode:"full",pages:"",demo_mode:false});started.push(String(id));}
      catch(_error){
        const job=state.jobs.find(item=>String(item.id)===String(id));
        if(!job||job.status!=="completed")batch.failed.add(String(id));
        batch.finished.add(String(id));
      }
    }
    updateBatchBar();
    while(true){
      await sleep(2500);
      try{await loadJobs(false);}catch(_error){}
      for(const id of started){
        const job=state.jobs.find(item=>String(item.id)===String(id));
        if(!job){batch.failed.add(id);batch.finished.add(id);continue;}
        if(job.status==="completed")batch.finished.add(id);
        else if(job.status==="failed"){batch.failed.add(id);batch.finished.add(id);}
      }
      updateBatchBar();
      const active=started.some(id=>{const job=state.jobs.find(item=>String(item.id)===String(id));return job&&["queued","running"].includes(job.status);});
      if(!active)break;
    }
  }
  batch.active=false;state.batch=null;
  if(!batch.failed.size){state.selectedJobIds.clear();state.selectionAnchor=null;}
  updateActions();
  await loadJobs(false);
  if(state.job){try{state.job=await api("/api/jobs/"+path(state.job.id));showJob();}catch(_error){}}
  const success=batch.ids.length-batch.failed.size;
  if(batch.failed.size)msg("status",`批量翻译完成：${success} 篇成功，${batch.failed.size} 篇失败。`,"error");
  else msg("status",`批量翻译完成：${success} 篇全部成功。`,"ok");
  $("batch-progress-stage").textContent=`批量翻译完成：${success} 成功${batch.failed.size?`，${batch.failed.size} 失败`:""}`;
  $("batch-progress-value").textContent="100%";
  $("batch-progress-fill").style.width="100%";
  setTimeout(()=>{if(!state.batch)$("batch-progress-wrap").hidden=true;},6000);
}
async function nativeFolderImport(){
  if(state.importInFlight)return;
  setImportBusy(true);msg("import-msg","请在系统窗口中选择包含 PDF 的文件夹…");
  try{
    const result=await window.pywebview.api.import_folder({action:"import"});
    if(result?.cancelled){msg("import-msg","");return;}
    if(result?.error)throw Error(result.error);
    if(!result?.total){msg("import-msg","该文件夹中没有 PDF 文件。","error");return;}
    const results=(result.results||[]).map(item=>item?.error?{filename:item.filename||"未命名论文",error:item.error}:normalizeImportResult(item,item?.filename));
    await finishBatchImport(results);
    if(result.truncated)msg("import-msg",`文件较多，仅导入前 ${result.total} 个 PDF。`,"error");
  }catch(error){msg("import-msg",error.message||"文件夹导入失败","error");}
  finally{setImportBusy(false);}
}
function pickFolder(){if(state.importInFlight)return;if(window.pywebview?.api?.import_folder)return nativeFolderImport();$("folder-input").click();}
function chooseFolderFiles(files){
  const pdfs=Array.from(files||[]).filter(file=>/\.pdf$/i.test(String(file.name||"")));
  if(!pdfs.length){msg("import-msg","该文件夹中没有 PDF 文件。","error");return;}
  $("import-action").value="import";
  chooseFiles(pdfs);
  void submitImport({files:pdfs,action:"import"});
}
function initFileDrop(){
  let depth=0;
  const hasFiles=event=>Array.from(event.dataTransfer?.types||[]).includes("Files");
  document.addEventListener("dragenter",event=>{if(!hasFiles(event)||state.draggingJob)return;event.preventDefault();depth++;document.body.classList.add("file-drop-active");});
  document.addEventListener("dragover",event=>{if(!hasFiles(event)||state.draggingJob)return;event.preventDefault();if(event.dataTransfer)event.dataTransfer.dropEffect="copy";document.body.classList.add("file-drop-active");});
  document.addEventListener("dragleave",event=>{if(!hasFiles(event))return;depth=Math.max(0,depth-1);if(!depth)document.body.classList.remove("file-drop-active");});
  document.addEventListener("drop",event=>{
    if(!hasFiles(event)||state.draggingJob)return;
    event.preventDefault();depth=0;document.body.classList.remove("file-drop-active");
    const files=Array.from(event.dataTransfer?.files||[]).filter(file=>/\.pdf$/i.test(String(file.name||"")));
    if(!files.length){showFileToast("请拖入 PDF 文件","error");return;}
    openImportModal();$("import-action").value="import";chooseFiles(files);void submitImport({files,action:"import"});
  });
}
async function loadJobs(selectFirst=true){const request=++state.jobsRequest;try{const data=await api("/api/jobs");if(request!==state.jobsRequest)return;state.jobs=(data.jobs||[]).filter(job=>!isDeletedJob(job.id));const existing=new Set(state.jobs.map(job=>String(job.id)));for(const id of [...state.selectedJobIds])if(!existing.has(String(id)))state.selectedJobIds.delete(id);renderList();if(selectFirst&&!state.job&&state.jobs.length)await select(state.jobs[0].id);}catch(error){if(request===state.jobsRequest)msg("status",error.message,"error");}}
async function select(id){if(isDeletedJob(id))return;const request=++state.selectRequest;try{const job=await api("/api/jobs/"+path(id));if(request!==state.selectRequest||isDeletedJob(id)||isDeletedJob(job.id))return;const previousJobId=state.job?.id;if(previousJobId&&previousJobId!==job.id)reader?.destroy();state.job=job;agentPanel.setPaper(job);state.selectedJobIds.clear();state.selectionAnchor=job.id;touchCacheJob(job.id);trimJobCaches();state.page=1;state.zoom=100;state.view="compare";state.readerPreserve=false;state.selection=null;state.selectedAnnotation=null;state.undo=null;$("sidebar").classList.remove("open");renderList();showJob();poll();}catch(error){if(request===state.selectRequest&&!isDeletedJob(id))msg("status",error.message,"error");}}
function updateProgress(job){const active=job.status==="queued"||job.status==="running",wrap=$("progress-wrap"),value=$("progress-value"),fill=$("progress-fill");$("open-download").disabled=active||!hasTranslation(job);wrap.hidden=!active;if(!active)return;const number=Number(job.progress),known=Number.isFinite(number)&&job.progress!==null&&!job.progress_indeterminate;wrap.classList.toggle("indeterminate",!known);fill.style.width=known?Math.max(0,Math.min(100,number))+"%":"";$("progress-stage").textContent=job.message||({queued:"等待处理",running:"正在翻译"}[job.status]||"处理中");value.textContent=known?Math.round(number)+"%":"处理中";}
function showJob(){const job=state.job;if(!job)return;$("title").textContent=job.translated_title||job.display_title||job.filename||"未命名论文";$("title").title=job.filename||"";const usage=job.token_usage;$("token-usage").textContent=usage?"Token 累计 "+Number(usage.total_tokens).toLocaleString()+" · 本次 "+Number(usage.last_run?.total_tokens||0).toLocaleString():"Token 未统计";$("total").textContent=job.page_count||"—";$("page-number").value=state.page;$("zoom-label").textContent=state.zoom+"%";$("open-translate").disabled=job.status==="queued"||job.status==="running";$("open-download").disabled=!hasTranslation(job);$("empty").hidden=true;$("pages").hidden=false;$("pagebar").hidden=false;document.querySelectorAll("[data-view]").forEach(b=>b.classList.toggle("active",b.dataset.view===state.view));let note="";if(job.demo_mode&&hasTranslation(job))note="演示模式：译文是原文副本，未调用 API。";else if(job.status==="failed")note=job.error||"翻译失败，仍可阅读原文。";else if(!hasTranslation(job)&&job.status!=="queued"&&job.status!=="running")note="尚无译文。点击右上角“翻译”开始。";$("notice").textContent=note;$("notice").classList.toggle("show",Boolean(note));msg("status",job.status==="failed"?(job.error||"失败"):label(job),job.status==="failed"?"error":"");updateProgress(job);renderPage();updateActions();}
function touchCacheJob(jobId){state.cacheJobOrder=state.cacheJobOrder.filter(id=>id!==jobId);state.cacheJobOrder.unshift(jobId);}
function disposeDocument(cacheKey){const promise=state.docs[cacheKey];if(!promise)return;delete state.docs[cacheKey];Promise.resolve(promise).then(doc=>doc?.destroy?.()).catch(()=>{});}
function trimJobCaches(){const keep=new Set(state.cacheJobOrder.slice(0,3));if(state.job?.id)keep.add(state.job.id);for(const cacheKey of Object.keys(state.docs)){const jobId=cacheKey.slice(0,cacheKey.lastIndexOf(":"));if(!keep.has(jobId)&&jobId!==state.job?.id)disposeDocument(cacheKey);}for(const cacheKey of Object.keys(state.annotations)){const jobId=cacheKey.slice(0,cacheKey.lastIndexOf(":"));if(!keep.has(jobId)&&jobId!==state.job?.id)delete state.annotations[cacheKey];}state.cacheJobOrder=state.cacheJobOrder.filter(id=>keep.has(id));}
async function documentFor(jobId,side){if(isDeletedJob(jobId))throw Error("文献已删除。");touchCacheJob(jobId);const cacheKey=key(side,jobId);if(!state.docs[cacheKey]){const url="/api/jobs/"+path(jobId)+"/"+side;const task=pdfjs.getDocument({url,cMapUrl:"/static/vendor/pdfjs/cmaps/",cMapPacked:true,standardFontDataUrl:"/static/vendor/pdfjs/standard_fonts/",wasmUrl:"/static/vendor/pdfjs/wasm/"});let promise=task.promise.then(doc=>{if(isDeletedJob(jobId)){doc?.destroy?.();throw Error("文献已删除。");}return doc;});promise=promise.catch(error=>{if(state.docs[cacheKey]===promise)delete state.docs[cacheKey];throw error;});state.docs[cacheKey]=promise;trimJobCaches();}return state.docs[cacheKey];}
async function annotationsFor(jobId,side,force=false){if(isDeletedJob(jobId))throw Error("文献已删除。");touchCacheJob(jobId);const cacheKey=key(side,jobId);if(force||!state.annotations[cacheKey]){let promise=api("/api/jobs/"+path(jobId)+"/"+side+"/annotations").then(result=>{if(isDeletedJob(jobId))throw Error("文献已删除。");return result;});promise=promise.catch(error=>{if(state.annotations[cacheKey]===promise)delete state.annotations[cacheKey];throw error;});state.annotations[cacheKey]=promise;}trimJobCaches();return state.annotations[cacheKey];}
function cancelScheduledRender(){if(renderFrame){cancelAnimationFrame(renderFrame);renderFrame=0;}if(resizeTimer){clearTimeout(resizeTimer);resizeTimer=0;}}
function requestRenderPage(){if(renderFrame)return;renderFrame=requestAnimationFrame(()=>{renderFrame=0;renderPage();});}
function renderPage(){cancelScheduledRender();const job=state.job;if(!job||!reader)return;const preserve=state.readerPreserve;state.readerPreserve=true;reader.open({jobId:job.id,pageCount:job.page_count,translated:hasTranslation(job),view:state.view,zoom:state.zoom,page:state.page,preserve});}
function page(delta){if(!state.job)return;const target=Math.min(Math.max(1,state.page+delta),Number(state.job.page_count)||1);state.selection=null;state.selectedAnnotation=null;$("annotation-menu").classList.remove("open");if(reader)reader.goToPage(target);else{state.page=target;renderPage();}}
function zoom(delta){state.zoom=Math.max(50,Math.min(200,state.zoom+delta));$("zoom-label").textContent=state.zoom+"%";requestRenderPage();}
function poll(){if(state.timer)clearTimeout(state.timer);const job=state.job;if(!job||isDeletedJob(job.id)||!["queued","running"].includes(job.status))return;state.timer=setTimeout(async()=>{try{const next=await api("/api/jobs/"+path(job.id));if(isDeletedJob(job.id)||state.job?.id!==job.id)return;const previous=state.job;const statusChanged=previous.status!==next.status;const translationChanged=hasTranslation(previous)!==hasTranslation(next);const titleChanged=jobTitle(previous)!==jobTitle(next);state.job=next;updateProgress(next);if(translationChanged||statusChanged&&next.status==="completed"){disposeDocument(key("translated",job.id));delete state.annotations[key("translated",job.id)];}const index=state.jobs.findIndex(j=>j.id===job.id);if(index>=0)state.jobs[index]={...state.jobs[index],...next};if(statusChanged||translationChanged||titleChanged)renderList();if(statusChanged||translationChanged||titleChanged)showJob();poll();}catch(error){if(!isDeletedJob(job.id)){msg("status",error.message,"error");state.timer=setTimeout(poll,3000);}}},1500);}
function importOptions(){const action=$("import-action").value;$("import-options").hidden=action==="import";$("import-pages-wrap").hidden=$("import-mode").value==="full";$("submit-import").textContent=action==="import"?"导入":"导入并开始";const multiple=state.files.length>1;$("import-batch-hint").hidden=!multiple;if(multiple&&action!=="import")msg("import-msg","多篇导入仅支持“仅导入，稍后翻译”，请改回该选项后再提交。","error");}
function translateOptions(){$("translate-pages-wrap").hidden=$("translate-mode").value==="full";}
function clearImportSelection(){state.file=null;state.files=[];$("file-input").value="";$("folder-input").value="";$("pick-file").textContent="点击选择 PDF（最大 100 MB）";$("import-selection").textContent="未选择文件";$("import-batch-hint").hidden=true;$("import-summary").hidden=true;$("import-summary").replaceChildren();}
function openImportModal(){if(state.importInFlight)return;clearImportSelection();msg("import-msg","");open("import-modal");}
function setImportProgress(done=null,total=null){const wrap=$("import-progress"),known=Number.isFinite(Number(done))&&Number(total)>0;wrap.classList.toggle("indeterminate",!known);$("import-progress-stage").textContent=known?"正在导入":"正在处理所选文件";$("import-progress-value").textContent=known?`${done}/${total}`:"处理中";$("import-progress-fill").style.width=known?Math.max(0,Math.min(100,Number(done)/Number(total)*100))+"%":"";}
function setImportBusy(busy){state.importInFlight=Boolean(busy);for(const id of ["pick-file","pick-folder","file-input","folder-input","import-action","import-mode","import-pages","submit-import"]){const el=$(id);if(el)el.disabled=state.importInFlight;}$("import-progress").hidden=!state.importInFlight;if(state.importInFlight)setImportProgress();else setImportProgress(null,null);if(!state.importInFlight)importOptions();}
function releaseImportFiles(){state.file=null;state.files=[];$("file-input").value="";$("pick-file").textContent="点击选择 PDF（最大 100 MB）";$("import-selection").textContent="批次已完成，可重新选择文件";$("import-batch-hint").hidden=true;}
function chooseFiles(files){const selected=Array.from(files||[]);if(!selected.length)return;$("import-summary").hidden=true;$("import-summary").replaceChildren();const invalid=selected.map(file=>({file,error:validateImportFile(file)})).filter(item=>item.error);state.files=selected;state.file=selected.length===1?selected[0]:null;$("import-selection").textContent=`已选择 ${selected.length} 个文件`;$("pick-file").textContent=selected.length===1?selected[0].name:`已选择 ${selected.length} 个 PDF`;importOptions();if(invalid.length){const first=invalid[0];msg("import-msg",`已标记 ${invalid.length} 个失败项：${first.file?.name||"未命名论文"}：${first.error}`,"error");}else msg("import-msg","");}
function choose(file){chooseFiles(file?[file]:[]);}
function renderBatchSummary(results){const summary=summarizeImportResults(results),box=$("import-summary");box.replaceChildren();const headline=document.createElement("div");headline.textContent=`批量导入完成：${summary.added.length} 个新增，${summary.duplicates.length} 个重复，${summary.failed.length} 个失败。`;box.append(headline);const appendGroup=(label,items,format)=>{if(!items.length)return;const group=document.createElement("div");group.textContent=label+"：";for(const item of items){const row=document.createElement("div");row.textContent=format(item);group.append(row);}box.append(group);};appendGroup("新增",summary.added,item=>item.filename);appendGroup("重复",summary.duplicates,item=>item.filename);appendGroup("失败",summary.failed,item=>`${item.filename}：${item.error}`);box.hidden=false;return summary;}
async function finishSingleImport(result){const normalized=normalizeImportResult(result);if(normalized.error)throw Error(normalized.error);const job=normalized.job;close("import-modal",true);clearImportSelection();await loadJobs(false);await select(job.id);if(normalized.duplicate){$("duplicate-name").textContent=`已打开已有论文：${job.translated_title||job.display_title||job.filename||"未命名论文"}`;open("duplicate-modal");}}
async function openImportResult(result){return finishSingleImport(result);}
async function finishBatchImport(results){const summary=renderBatchSummary(results);let refreshError=null;try{await loadJobs(false);const first=summary.added[0]||summary.duplicates[0];if(first?.job?.id!=null)await select(first.job.id);}catch(error){refreshError=error;}finally{releaseImportFiles();}if(refreshError)msg("status",refreshError.message||"列表刷新失败","error");else if(summary.failed.length)msg("status",`批量导入完成：${summary.added.length} 个新增，${summary.duplicates.length} 个重复，${summary.failed.length} 个失败。`,summary.added.length||summary.duplicates.length?"":"error");else msg("status",`批量导入完成：${summary.added.length} 个新增，${summary.duplicates.length} 个重复。`);msg("import-msg",refreshError?.message||"批次处理完成，请查看下方汇总。",refreshError?"error":"");return summary;}
async function nativeImport(){const action=$("import-action").value,mode=$("import-mode").value,pages=$("import-pages").value.trim();if(action!=="import"&&mode==="trial"&&!pages){msg("import-msg","请填写页码范围。","error");return;}setImportBusy(true);msg("import-msg","请选择 PDF，确认后将逐篇导入，请稍候…");try{const result=await window.pywebview.api.import_pdf({action,mode,pages});if(result?.cancelled){msg("import-msg","");return;}if(result?.error)throw Error(result.error);if(Array.isArray(result?.results)){await finishBatchImport(result.results.map(item=>item?.error?{filename:item.filename||"未命名论文",error:item.error}:normalizeImportResult(item,item?.filename)));}else await finishSingleImport(result);}catch(error){msg("import-msg",error.message||"导入失败","error");}finally{setImportBusy(false);}}
async function submitImport(options={}){
  if(state.importInFlight)return;
  const forced=options&&Array.isArray(options.files)?options.files:null;
  const forcedAction=options&&typeof options.action==="string"?options.action:null;
  if(!forced&&window.pywebview?.api?.import_pdf)return nativeImport();
  const files=forced&&forced.length?forced:(state.files.length?state.files:(state.file?[state.file]:[]));
  if(!files.length){msg("import-msg","请先选择 PDF。","error");return;}
  const action=forcedAction||$("import-action").value,mode=$("import-mode").value,pages=$("import-pages").value.trim();
  if(files.length>1&&action!=="import"){msg("import-msg","多篇导入仅支持“仅导入，稍后翻译”，请改回该选项后再提交。","error");return;}
  if(action!=="import"&&mode==="trial"&&!pages){msg("import-msg","请填写页码范围。","error");return;}
  setImportBusy(true);
  msg("import-msg",files.length>1?`正在导入 ${files.length} 个文件…`:"正在导入…");
  try{
    if(files.length===1&&!forced){
      const file=files[0],error=validateImportFile(file);
      if(error)throw Error(error);
      const form=new FormData();
      form.append("file",file);
      form.append("mode",action==="import"?"import":mode);
      form.append("pages",action==="import"?"":mode==="trial"?pages:"");
      form.append("demo_mode",action==="demo"?"true":"false");
      await finishSingleImport(await api("/api/jobs",{method:"POST",body:form}));
    }else{
      const results=await importFilesSequential(files,async file=>{
        const error=validateImportFile(file);
        if(error)throw Error(error);
        const form=new FormData();
        form.append("file",file);
        form.append("mode","import");
        form.append("pages","");
        form.append("demo_mode","false");
        return api("/api/jobs",{method:"POST",body:form});
      },(done,total)=>{setImportProgress(done,total);msg("import-msg",`正在导入 ${done}/${total} 个文件…`);});
      await finishBatchImport(results);
    }
  }catch(error){msg("import-msg",error.message||"导入失败","error");}
  finally{setImportBusy(false);}
}

async function submitTranslate(){const job=state.job;if(!job)return;const mode=$("translate-mode").value,pages=$("translate-pages").value.trim();if(mode==="trial"&&!pages){msg("translate-msg","请填写页码范围。","error");return;}$("submit-translate").disabled=true;msg("translate-msg","正在启动…");try{const result=await post("/api/jobs/"+path(job.id)+"/translate",{mode,pages:mode==="trial"?pages:"",demo_mode:$("translate-demo").checked});close("translate-modal");state.job=result.job||await api("/api/jobs/"+path(job.id));disposeDocument(key("translated",job.id));delete state.annotations[key("translated",job.id)];await loadJobs();showJob();poll();}catch(error){msg("translate-msg",error.message,"error");}finally{$("submit-translate").disabled=false;}}
async function loadSettings(){try{const data=await api("/api/settings");$("provider").value=data.provider||"";$("base-url").value=data.base_url||"";$("model").value=data.model||"";$("api-key").value="";$("reuse-key").checked=false;$("key-state").textContent=data.has_api_key?"已保存 "+(data.api_key_masked||"掩码 Key"):"当前未保存 Key";$("provider-summary").textContent=(data.provider||"服务")+" · "+(data.has_api_key?"已设置 Key":"未设置 Key");$("storage").textContent=data.storage||"";msg("settings-msg","");}catch(error){msg("settings-msg",error.message,"error");}}
async function saveSettings(){const button=$("save-settings");button.disabled=true;try{const result=await post("/api/settings",{provider:$("provider").value.trim(),base_url:$("base-url").value.trim(),model:$("model").value.trim(),api_key:$("api-key").value,reuse_existing_key:$("reuse-key").checked});$("api-key").value="";$("reuse-key").checked=false;$("key-state").textContent=result.settings.has_api_key?"已保存 "+result.settings.api_key_masked:"当前未保存 Key";$("provider-summary").textContent=result.settings.provider+" · "+(result.settings.has_api_key?"已设置 Key":"未设置 Key");$("storage").textContent=result.settings.storage||"";msg("settings-msg","设置已保存。","ok");}catch(error){msg("settings-msg",error.message,"error");}finally{button.disabled=false;}}
async function testConnection(){const button=$("test-connection");button.disabled=true;msg("settings-msg","正在发送最小测试请求…");try{const result=await post("/api/test-connection",{});msg("settings-msg",result.message||"测试完成。",result.ok?"ok":"error");}catch(error){msg("settings-msg",error.message,"error");}finally{button.disabled=false;}}
async function downloadDiagnostics(){const bridge=window.pywebview?.api;if(typeof bridge?.save_diagnostics==="function"){try{const result=await bridge.save_diagnostics();if(result?.error)throw Error(result.error);if(!result?.cancelled)msg("status","诊断日志已保存。","ok");}catch(error){msg("status",error.message||"诊断日志保存失败。","error");}return;}const link=document.createElement("a");link.href="/api/diagnostics/download";link.download="paper2zh-diagnostics.zip";document.body.append(link);link.click();link.remove();}
async function downloads(){const job=state.job;if(!job||!hasTranslation(job))return;try{if(window.pywebview?.api?.save_translation){const result=await window.pywebview.api.save_translation(job.id);if(result?.error)throw Error(result.error);if(!result?.cancelled)msg("status",result?.message||"译文已保存。");return;}const link=document.createElement("a");link.href="/api/jobs/"+path(job.id)+"/translated";const base=(job.translated_title||job.display_title||job.filename||"translated").replace(/\.pdf$/i,"");link.download=base+".zh.pdf";document.body.append(link);link.click();link.remove();}catch(error){msg("status",error.message,"error");}}
function handleReaderSelection(selection){
  if(!state.job||selection.jobId!==state.job.id)return;
  state.selection=selection;state.selectedAnnotation=null;
  const menu=$("annotation-menu"),box=selection.bounds;
  menu.classList.add("open");
  /* Measure after opening so the toolbar can be placed above the selection.
     WebView2/Edge draw their own selection mini menu below the text; keeping
     the annotation palette above it avoids overlapping hit targets. */
  const rect=menu.getBoundingClientRect();
  const gap=8;
  const left=Math.max(gap,Math.min(window.innerWidth-rect.width-gap,box.left));
  let top=box.top-rect.height-gap;
  if(top<gap)top=box.bottom+gap;
  top=Math.max(gap,Math.min(window.innerHeight-rect.height-gap,top));
  menu.style.left=left+"px";
  menu.style.top=top+"px";
  msg("status","已选择文字，选择颜色或下划线。");
}
function handleReaderSelectionError(message){state.selection=null;$("annotation-menu").classList.remove("open");msg("status",message,"error");}
function handleReaderError(message,context){reportClientError(context);msg("status",message,"error");}
function handleReaderAnnotationClick(target){if(!state.job||target.jobId!==state.job.id)return;state.selectedAnnotation=target;msg("status","已选择批注，点击“删除”可移除。");}
const panState={space:false,dragging:false,pointerId:null,startX:0,startY:0,startLeft:0,startTop:0};
function isTypingTarget(target){return Boolean(target?.closest?.("input,textarea,select,button,[contenteditable=true],.backdrop,.modal"));}
function releasePanPointer(){const scroll=$("scroll"),pointerId=panState.pointerId;panState.pointerId=null;if(pointerId===null)return;try{scroll?.releasePointerCapture?.(pointerId);}catch(_error){}}
function clearPan(){panState.space=false;panState.dragging=false;releasePanPointer();$("scroll")?.classList.remove("space-pan","is-panning");}
function startPan(event){if(!panState.space||event.button!==0)return;event.preventDefault();panState.dragging=true;panState.pointerId=event.pointerId;panState.startX=event.clientX;panState.startY=event.clientY;panState.startLeft=$("scroll").scrollLeft;panState.startTop=$("scroll").scrollTop;window.getSelection()?.removeAllRanges();$("scroll").classList.add("is-panning");$("scroll").setPointerCapture?.(event.pointerId);}
function movePan(event){if(!panState.dragging||event.pointerId!==panState.pointerId)return;event.preventDefault();$("scroll").scrollLeft=panState.startLeft-(event.clientX-panState.startX);$("scroll").scrollTop=panState.startTop-(event.clientY-panState.startY);}
function endPan(){if(!panState.dragging){releasePanPointer();return;}panState.dragging=false;releasePanPointer();$("scroll").classList.remove("is-panning");window.getSelection()?.removeAllRanges();}
async function addAnnotation(type,color){const selected=state.selection;if(!selected||!state.job||selected.jobId!==state.job.id)return;try{const current=await annotationsFor(selected.jobId,selected.side);if(state.job?.id!==selected.jobId)return;const result=await post("/api/jobs/"+path(selected.jobId)+"/"+selected.side+"/annotations",{action:"add",revision:current.revision,page:selected.page,type,color,rects:selected.rects});state.annotations[key(selected.side,selected.jobId)]=Promise.resolve(result);if(state.job?.id!==selected.jobId)return;const before=new Set((current.annotations||[]).map(x=>x.id));const added=(result.annotations||[]).find(x=>!before.has(x.id));if(added)state.undo={jobId:selected.jobId,side:selected.side,page:selected.page,id:added.id};state.selectedAnnotation=null;reader?.redrawAnnotations(selected.jobId,selected.side,selected.page,result.annotations||[]);$("annotation-menu").classList.remove("open");window.getSelection()?.removeAllRanges();state.selection=null;msg("status","批注已保存到本机。");}catch(error){if(state.job?.id===selected.jobId)msg("status",error.message,"error");}}
async function deleteAnnotation(target){if(!target||!state.job||target.jobId!==state.job.id)return;try{const current=await annotationsFor(target.jobId,target.side);if(state.job?.id!==target.jobId)return;const result=await post("/api/jobs/"+path(target.jobId)+"/"+target.side+"/annotations",{action:"delete",revision:current.revision,id:target.id});state.annotations[key(target.side,target.jobId)]=Promise.resolve(result);if(state.job?.id!==target.jobId)return;reader?.redrawAnnotations(target.jobId,target.side,target.page,result.annotations||[]);state.selectedAnnotation=null;state.undo=null;msg("status","批注已删除。");}catch(error){if(state.job?.id===target.jobId)msg("status",error.message,"error");}}
$("new-folder").addEventListener("click",()=>folderModal("create_folder"));$("submit-folder").addEventListener("click",submitFolder);$("submit-move").addEventListener("click",submitMove);$("open-import").addEventListener("click",()=>open("import-modal"));$("open-settings").addEventListener("click",()=>open("settings-modal"));$("open-translate").addEventListener("click",async()=>{if(state.batch?.active)return;const selected=[...state.selectedJobIds];if(selected.length>1){openBatchTranslate(selected);return;}if(selected.length===1&&String(selected[0])!==String(state.job?.id))await select(selected[0]);if(!state.job)return;$("translate-name").textContent=state.job.filename;open("translate-modal");});$("open-memo").addEventListener("click",()=>void openMemo());$("save-memo").addEventListener("click",()=>void saveMemo());$("memo-content").addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key==="Enter"){event.preventDefault();void saveMemo();}});$("selection-clear").addEventListener("click",clearSelection);$("submit-batch-translate").addEventListener("click",submitBatchTranslate);$("pick-folder").addEventListener("click",pickFolder);$("folder-input").addEventListener("change",event=>{chooseFolderFiles(event.target.files);event.target.value="";});$("open-download").addEventListener("click",downloads);$("download-diagnostics").addEventListener("click",downloadDiagnostics);$("menu").addEventListener("click",()=>$("sidebar").classList.toggle("open"));
$("pick-file").addEventListener("click",()=>window.pywebview?.api?.import_pdf?submitImport():$("file-input").click());$("file-input").addEventListener("change",event=>choose(event.target.files[0]));$("import-action").addEventListener("change",importOptions);$("import-mode").addEventListener("change",importOptions);$("submit-import").addEventListener("click",submitImport);$("translate-mode").addEventListener("change",translateOptions);$("submit-translate").addEventListener("click",submitTranslate);$("save-settings").addEventListener("click",saveSettings);$("test-connection").addEventListener("click",testConnection);
document.querySelectorAll("[data-close]").forEach(b=>b.addEventListener("click",()=>close(b.dataset.close)));$("submit-delete").addEventListener("click",submitDelete);document.querySelectorAll(".backdrop").forEach(b=>b.addEventListener("click",e=>{if(e.target===b)close(b.id);}));document.querySelectorAll("[data-file-action]").forEach(button=>button.addEventListener("click",()=>runFileMenuAction(button.dataset.fileAction)));document.querySelectorAll("[data-menu-icon]").forEach(icon=>{icon.innerHTML=uiIcons[icon.dataset.menuIcon]||"";});document.addEventListener("keydown",e=>{if($("file-context-menu").classList.contains("open")){const items=[...$("file-context-menu").querySelectorAll("[role=menuitem]")],index=items.indexOf(document.activeElement);if(e.key==="Escape"){e.preventDefault();closeFileMenu();return;}if(e.key==="ArrowDown"||e.key==="ArrowUp"){e.preventDefault();items[(index+(e.key==="ArrowDown"?1:-1)+items.length)%items.length]?.focus();return;}if(e.key==="Home"||e.key==="End"){e.preventDefault();items[e.key==="Home"?0:items.length-1]?.focus();return;}if(e.key==="Enter"||e.key===" "){if(index>=0){e.preventDefault();items[index].click();return;}}}if(e.key==="Escape"){$("annotation-menu").classList.remove("open");document.querySelectorAll(".backdrop.open").forEach(b=>close(b.id));if(state.selectedJobIds.size)clearSelection();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="a"&&!isTypingTarget(e.target)&&!document.querySelector(".backdrop.open")){e.preventDefault();state.selectedJobIds=new Set(state.visibleJobIds);state.selectionAnchor=state.visibleJobIds[0]||null;renderList();updateActions();return;}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="o"){e.preventDefault();open("import-modal");}});document.addEventListener("pointerdown",event=>{if($("file-context-menu").classList.contains("open")&&!$("file-context-menu").contains(event.target)&&!event.target.closest?.(".file-menu-trigger,.file"))closeFileMenu(false);if(!$("annotation-menu").contains(event.target))$("annotation-menu").classList.remove("open");});
document.addEventListener("keydown",event=>{if(event.code!=="Space"||event.isComposing||isTypingTarget(event.target))return;if(event.repeat){if(panState.space)event.preventDefault();return;}event.preventDefault();panState.space=true;$("scroll").classList.add("space-pan");});document.addEventListener("keyup",event=>{if(event.code==="Space")clearPan();});window.addEventListener("blur",clearPan);$("scroll").addEventListener("pointerdown",startPan);$("scroll").addEventListener("pointermove",movePan);$("scroll").addEventListener("pointerup",endPan);$("scroll").addEventListener("pointercancel",endPan);$("scroll").addEventListener("lostpointercapture",endPan);
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>{state.view=b.dataset.view;document.querySelectorAll("[data-view]").forEach(x=>x.classList.toggle("active",x===b));renderPage();}));$("prev").addEventListener("click",()=>page(-1));$("next").addEventListener("click",()=>page(1));$("page-number").addEventListener("change",()=>{const target=Math.min(Math.max(1,Number($("page-number").value)||1),Number(state.job?.page_count)||1);if(reader)reader.goToPage(target);else{state.page=target;renderPage();}});$("zoom-out").addEventListener("click",()=>zoom(-10));$("zoom-in").addEventListener("click",()=>zoom(10));
document.querySelectorAll("[data-color]").forEach(button=>button.addEventListener("click",()=>addAnnotation("highlight",button.dataset.color)));$("annotation-underline").addEventListener("click",()=>addAnnotation("underline","#FFE066"));$("annotation-menu").addEventListener("mousedown",event=>event.preventDefault());$("annotation-delete").addEventListener("click",()=>deleteAnnotation(state.selectedAnnotation));$("annotation-undo").addEventListener("click",()=>deleteAnnotation(state.undo));
$("scroll").addEventListener("wheel",event=>{if(!event.ctrlKey||!state.job)return;event.preventDefault();zoom(event.deltaY<0?10:-10);},{passive:false});window.addEventListener("resize",()=>{applyAgentWidth(agentPrefs.width);applySidebarWidth(sidebarPrefs.width);if(!state.job)return;if(resizeTimer)clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{resizeTimer=0;requestRenderPage();},120);});
$("submit-folder-delete").addEventListener("click",submitFolderDelete);
$("file-input").addEventListener("change",event=>chooseFiles(event.target.files));
$("open-import").addEventListener("click",openImportModal);
initSidebar();initAgent();initFileDrop();const agentPanel=createAgentPanel({getJob:()=>state.job,getPage:()=>state.page});reader=new ContinuousReader({scroller:$("scroll"),pages:$("pages"),getDocument:documentFor,getAnnotations:annotationsFor,onPageChange:pageNumber=>{state.page=pageNumber;$("page-number").value=pageNumber;agentPanel.setPage(pageNumber);},onSelection:handleReaderSelection,onSelectionError:handleReaderSelectionError,onAnnotationClick:handleReaderAnnotationClick,onError:handleReaderError});
importOptions();translateOptions();loadSettings();loadLibrary().then(loadJobs);
