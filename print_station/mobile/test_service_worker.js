'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {}, shown = [], opened = [];
const origin = 'https://print.example.com';
const context = {URL, self:{location:{origin}, addEventListener:(name,fn)=>handlers[name]=fn,
  registration:{showNotification:async(title,options)=>shown.push({title,options})},
  clients:{matchAll:async()=>[],openWindow:async url=>opened.push(url)}}};
vm.runInNewContext(fs.readFileSync(__dirname+'/static/sw.js','utf8'),context);
(async()=>{
  let pending;
  handlers.push({data:{json:()=>({title:'Print completed',body:'mask',tag:'a'.repeat(32)})},waitUntil:p=>pending=p});
  await pending;
  assert.equal(shown[0].title,'Print completed');
  assert.equal(shown[0].options.data.url,'/?event='+'a'.repeat(32));
  handlers.push({data:{json:()=>{throw Error('bad JSON');}},waitUntil:p=>pending=p});
  await pending;
  assert.equal(shown[1].title,'Print Station');
  handlers.notificationclick({notification:{close(){},data:{url:'https://evil.example/'}},waitUntil:p=>pending=p});
  await pending;
  assert.equal(opened[0],origin+'/');
  handlers.notificationclick({notification:{close(){},data:{url:'/?event='+'b'.repeat(32)}},waitUntil:p=>pending=p});
  await pending;
  assert.equal(opened[1],origin+'/?event='+'b'.repeat(32));
  assert.equal(handlers.fetch,undefined); // No private/offline caching interception.
  console.log('Service worker push/fallback/click isolation checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
