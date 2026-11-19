import { Database } from "bun:sqlite";

const project = "PAPER_OLD_FORMULA";
const API = "https://paper-api.alpaca.markets";
const SHEET_ID = "14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ";
const DB_PATH = "/data/old-formula-paper.sqlite";
const MIN_SCORE = 95;
const BUY_FRACTION = 0.02;
const MAX_DAILY = 10;
const MAX_OPEN = 30;
const TERMINAL = new Set(["filled","canceled","expired","rejected"]);
const sleep = (ms:number) => new Promise(r => setTimeout(r,ms));
const log = (event:string, payload:any={}) => console.log(JSON.stringify({project,event,at:new Date().toISOString(),...payload}));
function requireEnv(name:string) {
  const v = Bun.env[name]?.trim();
  if (!v || v.includes("${{")) throw new Error("Missing or unresolved environment variable: "+name);
  return v;
}
function timeParts(tz:string) {
  const parts = new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23",weekday:"short"}).formatToParts(new Date());
  const x:any={}; for (const p of parts) x[p.type]=p.value;
  return {date:x.year+"-"+x.month+"-"+x.day, hour:Number(x.hour), minute:Number(x.minute),weekday:x.weekday};
}
function isoDayDiff(today:string, past:string) {
  return (Date.parse(today+"T12:00:00Z")-Date.parse(past+"T12:00:00Z"))/86400000;
}
function roundMoney(n:number) {return Math.floor(n*100)/100;}
function safeNum(n:any){const v=Number(n);return Number.isFinite(v)?v:NaN;}
function getKeys() {return {id:requireEnv("APCA_API_KEY_ID"),secret:requireEnv("APCA_API_SECRET_KEY")};}
async function req(path:string, method="GET", body:any=undefined, missingOK=false) {
 const k=getKeys();
 const resp=await fetch(API+path,{method,headers:{"APCA-API-KEY-ID":k.id,"APCA-API-SECRET-KEY":k.secret,...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(25000)});
 if(resp.status===404 && missingOK)return null;
 if(!resp.ok)throw new Error("PAPER_ALPACA_HTTP_"+resp.status+" "+path.split("?")[0]);
 return await resp.json();
}
async function googleToken() {
 const info=JSON.parse(requireEnv("GOOGLE_SERVICE_ACCOUNT_JSON"));
 if(!info.private_key || !info.client_email)throw new Error("Google service account fields missing");
 const base64url=(bytes:Uint8Array)=>btoa(String.fromCharCode(...bytes)).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
 const enc=(str:string)=>base64url(new TextEncoder().encode(str));
 const sec=Math.floor(Date.now()/1000);
 const msg=enc(JSON.stringify({alg:"RS256",typ:"JWT"}))+"."+enc(JSON.stringify({iss:info.client_email,scope:"https://www.googleapis.com/auth/spreadsheets.readonly",aud:"https://oauth2.googleapis.com/token",iat:sec,exp:sec+3500}));
 const pem=info.private_key.replace(/-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g,"");
 const binary=Uint8Array.from(atob(pem),c=>c.charCodeAt(0));
 const key=await crypto.subtle.importKey("pkcs8",binary,{name:"RSASSA-PKCS1-v1_5",hash:"SHA-256"},false,["sign"]);
 const sig=new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5",key,new TextEncoder().encode(msg)));
 const tokenResp=await fetch("https://oauth2.googleapis.com/token",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({grant_type:"urn:ietf:params:oauth:grant-type:jwt-bearer",assertion:msg+"."+base64url(sig)}),signal:AbortSignal.timeout(25000)});
 if(!tokenResp.ok)throw new Error("GOOGLE_AUTH_HTTP_"+tokenResp.status);
 const r:any=await tokenResp.json();if(!r.access_token)throw new Error("Google token missing");return r.access_token;
}
async function loadMarketwide(){
 const token=await googleToken();
 const url="https://sheets.googleapis.com/v4/spreadsheets/"+SHEET_ID+"/values/"+encodeURIComponent("Marketwide!A1:I8058")+"?valueRenderOption=UNFORMATTED_VALUE";
 const resp=await fetch(url,{headers:{Authorization:"Bearer "+token},signal:AbortSignal.timeout(35000)});
 if(!resp.ok)throw new Error("SHEET_READ_HTTP_"+resp.status);
 return (await resp.json()).values as any[][];
}
function openDb() {
 const db=new Database(DB_PATH,{create:true});
 db.exec("PRAGMA journal_mode=WAL");
 db.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)");
 db.exec("CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, session TEXT NOT NULL, scan_utc TEXT NOT NULL, total INTEGER NOT NULL, valid INTEGER NOT NULL, candidates TEXT NOT NULL)");
 db.exec("CREATE TABLE IF NOT EXISTS orders (cid TEXT PRIMARY KEY, day TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, filled_qty REAL NOT NULL DEFAULT 0, notional REAL NOT NULL DEFAULT 0)");
 db.exec("CREATE TABLE IF NOT EXISTS highs (symbol TEXT PRIMARY KEY, high_gain REAL NOT NULL, basis REAL NOT NULL, observed_at TEXT NOT NULL, paused TEXT)");
 return db;
}
function dbVal(db:Database,q:string,...params:any[]) {return db.query(q).get(...params) as any;}
function dbRows(db:Database,q:string,...params:any[]) {return db.query(q).all(...params) as any[];}
async function checkAccount(db:Database){
 const account=await req("/v2/account");
 if(!account?.id || account.status!=="ACTIVE")throw new Error("Paper account is not active/identifiable");
 const existing=dbVal(db,"SELECT v FROM meta WHERE k='paper_account_id'")?.v;
 if(existing && existing!==account.id)throw new Error("PAPER_ACCOUNT_ID_CHANGED_FAIL_CLOSED");
 if(!existing)db.query("INSERT INTO meta(k,v) VALUES('paper_account_id',?)").run(account.id);
 return account;
}
function scoreRow(row:any[]){
 const [sym,p,s200,s50,pos,vol]=row;
 const v=[p,s200,s50,pos,vol].map(safeNum);
 if(typeof sym!=="string" || !/^[A-Z][A-Z0-9.\-]{0,12}$/.test(sym) || !(v[0]>0 && v[1]>0 && v[2]>0 && v[3]>=0 && v[3]<=1 && v[4]>=0))return null;
 const points=40*Number(v[0]>v[1])+20*Number(v[2]>v[1])+30*v[3]+10*Math.min(1,v[4]/10);
 return {symbol:sym,score:Math.round(points*10000)/10000,close:v[0],sma200:v[1],sma50:v[2],pos:v[3],volM:v[4]};
}
async function screen(db:Database, den:any, ny:any) {
 await checkAccount(db);
 const values=await loadMarketwide();
 const header=["symbol","close","sma_200","sma_50","pos_52w","dollar_vol_m","score","completed_session_date","scan_utc"];
 if(!values?.[0] || header.some((v,i)=>values[0][i]!==v))throw new Error("MARKETWIDE_HEADERS_UNEXPECTED");
 const rows=values.slice(1).filter(x=>x && x[0]);
 const first=rows.find(r=>r[7] && r[8]);
 if(!first)throw new Error("MARKETWIDE_NO_SESSION_MARKERS");
 const completed=String(first[7]);const scanUTC=String(first[8]);
 const scanDate=timeDate(scanUTC,"America/Denver");
 const delta=isoDayDiff(ny.date,completed);
 if(rows.length<6500 || scanDate!==den.date || !Number.isFinite(delta) || delta<1 || delta>4)throw new Error("MARKETWIDE_STALE_OR_INCOMPLETE_"+scanDate+"_"+completed+"_"+rows.length);
 let good=0,wrongDates=0;
 const candidates:any[]=[];const seen=new Set<string>();
 for(const row of rows){
   const v=scoreRow(row);
   if(!v)continue;
   if(seen.has(v.symbol))throw new Error("MARKETWIDE_DUPLICATE_SYMBOL_"+v.symbol);
   seen.add(v.symbol);good++;
   if(String(row[7])!==completed || String(row[8])!==scanUTC){wrongDates++;continue;}
   if(v.score>=MIN_SCORE)candidates.push(v);
 }
 if(good<5000 || wrongDates>Math.max(100,good*.05))throw new Error("MARKETWIDE_UNRELIABLE_"+good+"_"+wrongDates);
 candidates.sort((a,b)=>b.score-a.score || a.symbol.localeCompare(b.symbol));
 db.query("INSERT OR REPLACE INTO snapshots(day,session,scan_utc,total,valid,candidates) VALUES(?,?,?,?,?,?)").run(den.date,completed,scanUTC,rows.length,good,JSON.stringify(candidates));
 log("SCREEN_SNAPSHOT_OK",{date:den.date,completed,universe:rows.length,valid:good,qualified:candidates.length,top:candidates.slice(0,5).map(x=>[x.symbol,x.score])});
}
function timeDate(iso:string,tz:string){
 const d=new Date(iso);if(!Number.isFinite(d.valueOf()))throw new Error("Invalid marketwide timestamp");
 const a:any={};for(const p of new Intl.DateTimeFormat("en-US",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(d))a[p.type]=p.value;
 return a.year+"-"+a.month+"-"+a.day;
}
function orderId(side:string,date:string,symbol:string){return "pold"+(side==="buy"?"b":"s")+"-"+date.replace(/-/g,"")+"-"+symbol;}
async function reconcile(db:Database){
 const pending=dbRows(db,"SELECT * FROM orders WHERE status NOT IN ('filled','canceled','expired','rejected')");
 for(const o of pending) {
   const order=await req("/v2/orders:by_client_order_id?client_order_id="+encodeURIComponent(o.cid),"GET",undefined,true);
   if(!order){log("ORDER_REVIEW_REQUIRED",{cid:o.cid,status:o.status,reason:"Broker order not found, never blind retry"});continue;}
   const fq=safeNum(order.filled_qty);
   if(!(fq>=0))throw new Error("FILLED_QTY_UNAVAILABLE_"+o.cid);
   db.query("UPDATE orders SET status=?,filled_qty=? WHERE cid=?").run(String(order.status||"unknown"),fq,o.cid);
 }
}
function owned(db:Database){
 const rows=dbRows(db,"SELECT symbol,SUM(CASE WHEN side='buy' THEN filled_qty ELSE -filled_qty END) as qty FROM orders GROUP BY symbol");
 return rows.filter(x=>x.qty>0.0000001);
}
async function buy(db:Database,den:any,ny:any) {
 const snap=dbVal(db,"SELECT * FROM snapshots WHERE day=?",den.date);
 if(!snap){log("BUY_SKIPPED_NO_SAME_DAY_SCREEN",{day:den.date});return;}
 const delta=isoDayDiff(ny.date,snap.session);
 if(delta<1 || delta>4)throw new Error("BUY_SNAPSHOT_SESSION_STALE");
 const clock=await req("/v2/clock");if(!clock?.is_open){log("BUY_SKIP_MARKET_CLOSED");return;}
 const account=await checkAccount(db);
 await reconcile(db);
 const uncertainties=dbRows(db,"SELECT cid FROM orders WHERE status='RESERVED' OR status='unknown'");
 if(uncertainties.length){log("BUY_BLOCKED_UNCERTAIN_ORDER",{count:uncertainties.length});return;}
 const originalPower=safeNum(account.buying_power);
 if(!(originalPower>0))throw new Error("BUY_NO_BUYING_POWER");
 const amount=roundMoney(originalPower*BUY_FRACTION);
 if(amount<1){log("BUY_SKIP_AMOUNT_TOO_SMALL",{amount});return;}
 const existing=await req("/v2/positions");
 const held=new Set((existing||[]).map((x:any)=>x.symbol));
 const pending=await req("/v2/orders?status=open&limit=500&direction=desc");
 const open=new Set((pending||[]).map((x:any)=>x.symbol));
 let sent=Number(dbVal(db,"SELECT COUNT(*) n FROM orders WHERE day=? AND side='buy'",den.date)?.n||0);
 let botOpen=owned(db).length;let skipped=0;
 const candidates=JSON.parse(snap.candidates);
 for(const c of candidates){
   if(sent>=MAX_DAILY || botOpen>=MAX_OPEN)break;
   if(c.symbol==="VT" || c.symbol==="SGOV" || held.has(c.symbol) || open.has(c.symbol)){skipped++;continue;}
   const cid=orderId("buy",ny.date,c.symbol);
   if(dbVal(db,"SELECT 1 FROM orders WHERE cid=?",cid)){skipped++;continue;}
   const asset=await req("/v2/assets/"+encodeURIComponent(c.symbol),"GET",undefined,true);
   if(!asset || asset.status!=="active" || asset.tradable!==true || asset.fractionable!==true || asset.class!=="us_equity"){skipped++;continue;}
   const freshAccount=await checkAccount(db);
   if(safeNum(freshAccount.buying_power)<amount)break;
   const freshClock=await req("/v2/clock");
   if(!freshClock?.is_open)break;
   const checkOrders=await req("/v2/orders?status=open&limit=500");
   if((checkOrders||[]).some((o:any)=>o.symbol===c.symbol)){skipped++;continue;}
   const checkPos=await req("/v2/positions/"+encodeURIComponent(c.symbol),"GET",undefined,true);
   if(checkPos){skipped++;continue;}
   db.query("INSERT INTO orders(cid,day,symbol,side,status,filled_qty,notional) VALUES(?,?,?,?,?,?,?)").run(cid,ny.date,c.symbol,"buy","RESERVED",0,amount);
   try{
     const placed=await req("/v2/orders","POST",{symbol:c.symbol,side:"buy",type:"market",time_in_force:"day",notional:amount,client_order_id:cid});
     db.query("UPDATE orders SET status=?,filled_qty=? WHERE cid=?").run(String(placed.status||"new"),safeNum(placed.filled_qty)||0,cid);
     log("BUY_ORDER_PAPER_SUBMITTED",{symbol:c.symbol,score:c.score,notional:amount,cid,brokerId:placed.id});
     sent++;botOpen++;held.add(c.symbol);open.add(c.symbol);
   }catch(err){log("BUY_SUBMISSION_UNCERTAIN_NO_RETRY",{cid,error:String(err)});throw err;}
   await sleep(100);
 }
 log("BUY_CYCLE_DONE",{day:ny.date,ordersToday:sent,heldSkipped:skipped,qualified:candidates.length,perOrderUSD:amount,buyingPowerUsed:"margin-aware-paper"});
}
async function seller(db:Database, ny:any) {
 const clock=await req("/v2/clock");if(!clock?.is_open)return;
 await checkAccount(db);
 await reconcile(db);
 const positions=await req("/v2/positions");
 const posMap=new Map((positions||[]).map((x:any)=>[x.symbol,x]));
 const owner=owned(db);
 const now=new Date().toISOString();
 let observed=0, flagged=0, sold=0;
 for(const x of owner){
   const p:any=posMap.get(x.symbol);
   if(!p){flagged++;log("SELL_PAUSE_MISSING_POSITION",{symbol:x.symbol});continue;}
   const q=safeNum(p.qty),basis=safeNum(p.avg_entry_price),price=safeNum(p.current_price);
   if(!(q>0 && basis>0 && price>0) || Math.abs(q-x.qty)>0.000001){flagged++;log("SELL_PAUSE_EXTERNAL_QTY_CHANGE",{symbol:x.symbol,brokerQty:q,ownedQty:x.qty});continue;}
   const old=dbVal(db,"SELECT * FROM highs WHERE symbol=?",x.symbol);
   if(old && (Math.abs(old.basis-basis)/basis)>0.0005){flagged++;log("SELL_PAUSE_COST_BASIS_CHANGE",{symbol:x.symbol});continue;}
   const gain=(price/basis-1)*100;
   const maxGain=Math.max(gain,old?old.high_gain:gain);
   db.query("INSERT INTO highs(symbol,high_gain,basis,observed_at,paused) VALUES(?,?,?,?,NULL) ON CONFLICT(symbol) DO UPDATE SET high_gain=excluded.high_gain,basis=excluded.basis,observed_at=excluded.observed_at").run(x.symbol,maxGain,basis,now);
   observed++;
   if(ny.minute!==35 || ny.hour<9 || ny.hour>15 || maxGain<13 || gain>maxGain-3 || gain<10)continue;
   const cid=orderId("sell",ny.date,x.symbol);
   if(dbVal(db,"SELECT 1 FROM orders WHERE cid=?",cid))continue;
   const open=await req("/v2/orders?status=open&limit=500");
   if((open||[]).some((o:any)=>o.symbol===x.symbol))continue;
   const fresh:any=await req("/v2/positions/"+encodeURIComponent(x.symbol),"GET",undefined,true);
   if(!fresh || Math.abs(safeNum(fresh.qty)-x.qty)>0.000001)continue;
   const freshGain=(safeNum(fresh.current_price)/safeNum(fresh.avg_entry_price)-1)*100;
   if(!Number.isFinite(freshGain) || freshGain<10 || freshGain>maxGain-3)continue;
   const limit=Math.ceil(basis*1.10*100-1e-9)/100;
   const limitStr=limit.toFixed(2);
   db.query("INSERT INTO orders(cid,day,symbol,side,status,filled_qty,notional) VALUES(?,?,?,?,?,?,?)").run(cid,ny.date,x.symbol,"sell","RESERVED",0,0);
   try{
     const placed=await req("/v2/orders","POST",{symbol:x.symbol,side:"sell",type:"limit",time_in_force:"day",qty:String(fresh.qty),limit_price:limitStr,client_order_id:cid});
     db.query("UPDATE orders SET status=?,filled_qty=? WHERE cid=?").run(String(placed.status||"new"),safeNum(placed.filled_qty)||0,cid);
     sold++;
     log("SELL_PROFIT_LIMIT_PAPER_SUBMITTED",{symbol:x.symbol,gain:+freshGain.toFixed(2),high:+maxGain.toFixed(2),limit:limitStr,cid,brokerId:placed.id});
   }catch(err){log("SELL_SUBMISSION_UNCERTAIN_NO_RETRY",{cid,error:String(err)});throw err;}
 }
 log("SELL_OBSERVER_DONE",{observed,flagged,orders:sold});
}
async function main(){
 log("BOOT_CHECK",{diagnosticFlag:Bun.env.PAPER_DIAGNOSTIC==="true",paperFlag:Bun.env.PAPER_ONLY==="true",hasKeys:Boolean(Bun.env.APCA_API_KEY_ID&&Bun.env.APCA_API_SECRET_KEY),hasGoogle:Boolean(Bun.env.GOOGLE_SERVICE_ACCOUNT_JSON)});
 if(Bun.env.PAPER_DIAGNOSTIC==="true"){
   if(requireEnv("PAPER_ONLY")!=="true")throw new Error("Diagnostic paper guard failed");
   const db=openDb();
   try{
      const account=await checkAccount(db);
      const data=await loadMarketwide();
      const valid=(data||[]).slice(1).map(scoreRow).filter(Boolean);
      log("PAPER_DIAGNOSTIC_OK",{accountStatus:account.status,paperAccountVerified:true,sheetRows:data.length-1,validRows:valid.length,oldScore95:valid.filter((x:any)=>x.score>=95).length,storage:"persistent"});
   }finally{db.close();}
   return;
 }
 const den=timeParts("America/Denver");const ny=timeParts("America/New_York");
 if(den.weekday==="Sat" || den.weekday==="Sun")return;
 const isScreen=den.hour===9 && den.minute===0;
 const isBuy=den.hour===12 && den.minute===0;
 const isObserver=ny.hour>=9 && ny.hour<=15 && ny.minute%10===5 && !(ny.hour===9 && ny.minute<35);
 if(!isScreen && !isBuy && !isObserver){return;}
 if(requireEnv("PAPER_ONLY")!=="true")throw new Error("PAPER_ONLY_GUARD_BLOCKED");
 if(requireEnv("PAPER_TRADING_ENABLED")!=="true")throw new Error("Paper trading disabled");
 const db=openDb();
 try{
   if(isScreen)await screen(db,den,ny);
   if(isBuy)await buy(db,den,ny);
   if(isObserver)await seller(db,ny);
 }finally{db.close();}
}
main().catch(e=>{log("FAIL_CLOSED",{error:String(e)});process.exitCode=1;});
