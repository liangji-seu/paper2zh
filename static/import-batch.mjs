export const MAX_IMPORT_FILE_BYTES=100*1024*1024;

export function validateImportFile(file){
  if(!file)return "请选择 PDF 文件。";
  if(!/\.pdf$/i.test(String(file.name||"")))return "请选择 PDF 文件。";
  if(Number(file.size)>MAX_IMPORT_FILE_BYTES)return "文件超过 100 MB。";
  return "";
}

export function normalizeImportResult(result,filename=""){
  const job=result?.job;
  if(job?.id!=null)return {filename:filename||job.filename||"未命名论文",job,duplicate:Boolean(result?.duplicate||job.duplicate)};
  return {filename:filename||"未命名论文",error:result?.error||"导入未返回论文任务。"};
}

export async function importFilesSequential(files,importOne,onProgress=()=>{}){
  const list=Array.from(files||[]),results=[];
  for(let index=0;index<list.length;index++){
    const file=list[index];
    try{results.push(normalizeImportResult(await importOne(file,index),file?.name));}
    catch(error){results.push({filename:file?.name||"未命名论文",error:error?.message||String(error||"导入失败")});}
    onProgress(results.length,list.length,results[results.length-1]);
  }
  return results;
}

export function summarizeImportResults(results){
  const list=Array.from(results||[]),added=[],duplicates=[],failed=[];
  for(const result of list){
    if(result?.error){failed.push({filename:String(result.filename||"未命名论文"),error:String(result.error)});continue;}
    (result?.duplicate?duplicates:added).push(result);
  }
  return {total:list.length,added,duplicates,failed};
}
