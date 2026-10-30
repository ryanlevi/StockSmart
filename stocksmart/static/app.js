function formatTimes(root=document){
  root.querySelectorAll('time[datetime]').forEach(el=>{
    const date=new Date(el.dateTime);
    if(!isNaN(date)){
      el.textContent=date.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
      el.title=date.toLocaleString();
    }
  });
}
function updatePreview(){
  const store=document.querySelector('#store_id'),box=document.querySelector('#open_box');
  if(!store||!box)return;
  document.querySelector('#preview-store').textContent=store.selectedOptions[0].textContent;
  document.querySelector('#preview-type').textContent=box.checked?'Open-box watch':'New stock watch';
  document.querySelector('#preview-condition').textContent=box.checked?'Open-box inventory only':'New, unopened inventory';
}
formatTimes();updatePreview();
document.querySelector('#store_id')?.addEventListener('change',updatePreview);
document.querySelector('#open_box')?.addEventListener('change',updatePreview);
document.addEventListener('submit',event=>{
  const form=event.target;
  if(form.dataset.confirm&&!window.confirm(form.dataset.confirm)){event.preventDefault();return;}
  // Defer disabling until the clicked button's action value is captured by the browser.
  setTimeout(()=>form.querySelectorAll('button').forEach(button=>button.disabled=true),0);
});
document.addEventListener('click',event=>{
  document.querySelectorAll('details.actions[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});
});
let refreshing=false;
setInterval(async()=>{
  if(refreshing||document.hidden||!document.querySelector('[data-refresh]')||document.querySelector('details[open]')||
      ['INPUT','SELECT','BUTTON'].includes(document.activeElement?.tagName))return;
  refreshing=true;
  try{
    const response=await fetch(location.href,{headers:{'X-Requested-With':'StockSmart'}});
    if(!response.ok)return;
    const next=new DOMParser().parseFromString(await response.text(),'text/html');
    for(const id of ['alert-list','stats']){
      const incoming=next.getElementById(id),current=document.getElementById(id);
      if(incoming&&current)current.replaceWith(incoming);
    }
    const incomingStatus=next.querySelector('.connection'),currentStatus=document.querySelector('.connection');
    if(incomingStatus&&currentStatus)currentStatus.replaceWith(incomingStatus);
    formatTimes();
  }catch(_){/* Keep the last displayed results if the network is interrupted. */}
  finally{refreshing=false;}
},15000);
