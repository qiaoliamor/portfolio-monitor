function mountChart(){
 const element=document.getElementById('history-chart');
 const source=document.getElementById('chart-data');
 if(!element||!source||!window.Chart)return;
 if(Chart.getChart(element))return;
 const data=JSON.parse(source.textContent);
 new Chart(element,{type:'line',data:{labels:data.labels,datasets:[{data:data.values,borderColor:'#23836b',backgroundColor:'rgba(35,131,107,.07)',borderWidth:2,pointRadius:0,fill:true,tension:.2}]},options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:7,color:'#7d8783'}},y:{grid:{color:'#edf0eb'},ticks:{color:'#7d8783'}}}}});
}
document.addEventListener('DOMContentLoaded',mountChart);
document.addEventListener('htmx:afterSwap',mountChart);
document.addEventListener('submit',event=>{
 const button=event.target.querySelector('[data-loading]');
 if(button){button.disabled=true;button.textContent=button.dataset.loading;}
});
