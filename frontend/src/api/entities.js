import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const diary={
 wiring:()=>api('/wiring'),
 addWiring:data=>api('/wiring',{method:'POST',body:JSON.stringify(data)}),
 batches:()=>api('/diary/batches'),
 addBatch:data=>api('/diary/batches',{method:'POST',body:JSON.stringify(data)}),
 builds:base=>api('/diary/builds'+(base?'?base_batch_id='+base:'')),
 build:id=>api('/diary/builds/'+id),
 createBuild:data=>api('/diary/builds',{method:'POST',body:JSON.stringify(data)}),
 retry:id=>api('/diary/builds/'+id+'/retry',{method:'POST'}),
 publish:id=>api('/diary/builds/'+id+'/publish',{method:'POST'}),
 history:base=>api('/diary/history?base_batch_id='+base),
};
