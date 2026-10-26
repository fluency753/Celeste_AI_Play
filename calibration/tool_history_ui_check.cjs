const fs = require('fs');
const {chromium} = require('playwright');
const assert = require('assert');
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 fs.mkdirSync('local',{recursive:true});
 const html=fs.readFileSync('overlay/hud.html','utf8');
 const config=JSON.parse(fs.readFileSync('configs/hud.json','utf8'));
 let items=[{id:'first',tool:'celeste_observe',text:'区域碰撞精查',wall:1793023000,status:'returned',duration_s:.14,
  parameters:{detail:'geometry',region:'[120, 40, 160, 80]'},result:{'本页对象数':'8','文字字符数':1840},attached_images:null},
 {id:'second',tool:'celeste_read',text:'读取文件 · /workspace/game/latest_obs.json',wall:1793023002,status:'running',parameters:null,result:null},
 {id:'third',tool:'celeste_bash',text:'执行程序',wall:1793023004,status:'failed',duration_s:1.2,
  parameters:{command:'<img src=x onerror="window.injected=1">',description:'agent填写的说明'},result:{'文字字符数':128},attached_images:null}];
 for(const obs of [false,true]){
  const page=await browser.newPage({viewport:{width:480,height:940}});
  let toolRequests=0, toolRun='fixture-run';const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());let data={};
   if(u.hostname==='fixture' && u.pathname==='/hud')return route.fulfill({contentType:'text/html',body:html});
   if(u.pathname==='/hud/state') data={config,run:'fixture-run',now:Date.now()/1000,metrics:{agent:'5.6 Sol',model:'gpt-5.6-sol',effort:'high',status:'thinking',chapter_time_ticks:300000000,context_tokens:25000,context_window:475000}};
   if(u.pathname==='/hud/history')data={run:'fixture-run',items:[{id:'chat',text:'先核对通道的碰撞箱，再尝试跨越。',kind:'解说',wall:1793023000}]};
   if(u.pathname==='/hud/tools'){toolRequests++;data={run:toolRun,items,scope:'仅本次运行；参数为请求，不证明模型已理解。'};}
   if(u.pathname==='/api/control')data={run:'fixture-run',control:{connected:false,messages:[]}};
   if(u.pathname.includes('models'))data={models:[]};
   if(u.pathname.includes('saves'))data={saves:[]};
   if(u.hostname!=='fixture')return route.fulfill({status:200,body:''});
   return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto('http://fixture/hud'+(obs?'?surface=obs':''));
  await page.waitForTimeout(1300);
  if(obs){
   assert.equal(toolRequests,0);
   assert.equal(await page.locator('.history-tabs').isVisible(),false);
   await page.evaluate(()=>selectHistory(true));
   await page.waitForTimeout(1200);
   assert.equal(toolRequests,0);
   assert.equal(await page.locator('#toolActivity').isVisible(),false);
  }else{
   assert.equal(toolRequests,0);
   assert.equal(await page.locator('#toolActivity').isVisible(),false);
   await page.getByRole('tab',{name:'工具记录'}).click();
   await page.locator('.tool-entry').first().waitFor();
   assert.equal(await page.locator('#thought').isVisible(),false);
   assert.equal(await page.locator('#toolActivity').isVisible(),true);
   await page.locator('.tool-entry summary').first().click();
   assert.equal(await page.locator('.tool-entry').first().evaluate(e=>e.open),true);
   items[0]={...items[0],duration_s:.16};
   await page.waitForTimeout(1200);
   assert.equal(await page.locator('.tool-entry').first().evaluate(e=>e.open),true);
   await page.locator('.tool-entry summary').last().click();
   assert.equal(await page.evaluate(()=>Boolean(window.injected)),false);
   await page.locator('#toolActivity').evaluate(e=>e.scrollTop=0);
   await page.screenshot({path:'local/tool-history-preview.png'});
   toolRun='other-run';
   await page.waitForTimeout(1300);
   assert.equal(await page.locator('.tool-entry').count(),0);
   assert.ok((await page.locator('#toolNotice').innerText()).includes('另一运行'));
   toolRun='fixture-run';
   await page.getByRole('tab',{name:'对话',exact:true}).click();
   await page.waitForTimeout(200);
   const before=toolRequests;
   await page.waitForTimeout(1400);
   assert.equal(toolRequests,before);
   assert.ok((await page.locator('#thought').innerText()).includes('先核对通道的碰撞箱，再尝试跨越。'));
  }
  console.log(JSON.stringify({surface:obs?'obs':'desktop',toolRequests,errors}));
  assert.deepEqual(errors,[]);
  await page.close();
 }
 await browser.close();browser=null;
})().catch(async e=>{if(browser) await browser.close();console.error(e);process.exit(1)});
