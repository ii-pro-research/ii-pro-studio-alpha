import assert from 'node:assert/strict';
import test from 'node:test';
import { generate } from '../bridge.mjs';

const frame = (event) => `data: ${JSON.stringify(event)}\r\n\r\n`;
const delta = (text) => ({type:'response.output_text.delta', delta:text});
const completed = {type:'response.completed',response:{status:'completed'}};
const request = {version:1,operation:'text.generate',request_id:'a'.repeat(32),role:'diagnostic',model:'gpt-6.2-sol',prompt:'Developer fixture',access_token:'SYNTHETIC-SECRET',deadline_ms:1000};
const run = async (value=request, signal=new AbortController().signal) => {
  const packets=[]; await generate(value,p=>packets.push(p),signal); return packets;
};
function response(wire, headers={}) { return new Response(new TextEncoder().encode(wire),{headers}); }

test('one fragments Engineer request can use 120 seconds without widening other roles', async(t)=>{
  let calls=0;
  t.mock.method(globalThis,'fetch',async()=>{ calls++; return response(frame(delta('fixture'))+frame(completed)); });
  await assert.rejects(run({...request,role:'project_reviewer',deadline_ms:120000}), /bounded bridge envelope/);
  await assert.rejects(run({...request,role:'project_engineer_fragments',deadline_ms:120001}), /bounded bridge envelope/);
  await assert.rejects(run({...request,role:'project_reviewer',deadline_ms:180000}), /bounded bridge envelope/);
  const packets=await run({...request,role:'project_engineer_fragments',deadline_ms:120000});
  assert.equal(packets.at(-1).type,'completed');
  assert.equal(calls,1);
});

const experimental = {...request, role:'project_engineer_fragments', deadline_ms:180000};

for (const scenario of ['slow', 'idle', 'deadline', 'late']) test('experimental Engineer: '+scenario, async(t)=>{
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  const schedule = setTimeout;
  let outer = true;
  t.mock.method(AbortSignal, 'timeout', milliseconds=>{
    if (outer && milliseconds === 180000) {
      outer = false;
      return timeout(scenario === 'idle' ? 500 : 150);
    }
    return timeout(milliseconds);
  });
  t.mock.method(globalThis, 'setTimeout', (cb, delay, ...args)=>
    schedule(cb, delay === 90000 ? 65 : delay, ...args));
  let calls=0, ticks=0;
  const late = Promise.withResolvers();
  t.mock.method(globalThis,'fetch',async(_url,{signal})=>{
    calls++;
    return new Response(new ReadableStream({start(body){
      const push = event=>body.enqueue(new TextEncoder().encode(frame(event)));
      if (scenario === 'late') {
        schedule(()=>{
          try { push(delta('late')); push(completed); body.close(); }
          catch (error) { if (error.code !== 'ERR_INVALID_STATE') return late.reject(error); }
          late.resolve();
        }, 85);
        return; // Deliberately ignore abort, proving late output cannot complete.
      }
      const timer=setInterval(()=>{
        ticks++;
        if (scenario === 'idle') {
          push({type:'response.in_progress'});
        } else {
          push(delta('part'));
          if (scenario === 'slow' && ticks === 4) {
            clearInterval(timer); push(completed); body.close();
          }
        }
      }, 24);
      signal.addEventListener('abort',()=>{
        clearInterval(timer); body.error(new DOMException('cancel','AbortError'));
      },{once:true});
    }}));
  });
  const packets=await run(experimental);
  if (scenario === 'late') await late.promise;
  assert.equal(calls,1);
  if (scenario === 'slow') {
    assert.equal(packets.at(-1).type,'completed');
    assert.equal(packets.at(-1).text,'part'.repeat(4));
  } else {
    assert.equal(packets.some(packet=>packet.type==='completed'),false);
    assert.equal(packets.at(-1).code,scenario === 'deadline' ? 'deadline' : 'text_idle');
  }
});

test('official transport tolerates additional valid events and missing MIME, exact payload no effort/tools',async(t)=>{
  let calls=0;
  t.mock.method(globalThis,'fetch',async(url,init)=>{
    calls++; assert.equal(String(url),'https://api.openai.com/v1/responses');
    const body=JSON.parse(init.body);
    assert.deepEqual(Object.keys(body).sort(),['input','instructions','model','store','stream']);
    assert.equal(body.store,false);assert.equal(body.stream,true);assert.equal(init.redirect,'error');
    assert.equal(new Headers(init.headers).get('accept'),'text/event-stream');
    return response(frame({type:'response.created',response:{id:'fixture'}})+
      frame({type:'response.reasoning_summary_text.done',text:'DO NOT RETAIN'})+
      frame({type:'response.future_metadata',private:'DO NOT RETAIN'})+
      frame(delta('Готов'))+frame({type:'response.output_text.annotation.added',annotation:{}})+frame(completed));
  });
  const packets=await run();
  assert.equal(calls,1);assert.equal(packets.at(-1).type,'completed');assert.equal(packets.at(-1).text,'Готов');
  assert.equal(JSON.stringify(packets).includes('DO NOT RETAIN'),false);
});

for(const kind of ['response.failed','response.incomplete','error']) test(kind+' is not success',async(t)=>{
  let calls=0;
  t.mock.method(globalThis,'fetch',async()=>{calls++;return response(frame({type:kind,response:{error:{code:'server_error'}},code:'server_error'}));});
  const packets=await run();assert.equal(calls,1);assert.equal(packets.at(-1).type,'error');
});
for(const status of [400,401,403,429,500,302]) test('HTTP '+status+' never retries',async(t)=>{
  let calls=0;
  t.mock.method(globalThis,'fetch',async()=>{calls++;return Response.json({error:{code:'api_error',message:'SYNTHETIC-SECRET'}},{status});});
  const packets=await run();assert.equal(calls,1);assert.equal(packets.at(-1).type,'error');
  assert.equal(JSON.stringify(packets).includes('SYNTHETIC-SECRET'),false);
});
for(const wire of ['',frame(delta('partial')),'data: {broken}\n\n']) test('EOF/invalid SSE never completes '+wire.length,async(t)=>{
  t.mock.method(globalThis,'fetch',async()=>response(wire));
  assert.equal((await run()).at(-1).type,'error');
});
test('abort interrupts active official stream without retry',async(t)=>{
  const controller=new AbortController();let calls=0;
  t.mock.method(globalThis,'fetch',async(_url,{signal})=>{calls++;return new Response(new ReadableStream({start(body){
    body.enqueue(new TextEncoder().encode(frame(delta('partial'))));
    signal.addEventListener('abort',()=>body.error(new DOMException('cancel','AbortError')),{once:true});
    setTimeout(()=>controller.abort(),10);
  }}));});
  const packets=await run(request,controller.signal);assert.equal(calls,1);assert.equal(packets.at(-1).code,'cancelled');
});
test('local quota remains stricter than SDK and never succeeds',async(t)=>{
  t.mock.method(globalThis,'fetch',async()=>response(frame(delta('x'.repeat(16001)))+frame(completed)));
  assert.equal((await run()).at(-1).code,'output_quota');
});
test('no filesystem/command/URL capabilities accepted',async()=>{
  for(const extra of [{file:'private.txt'},{command:'whoami'},{url:'https://foreign.invalid'},{tools:[]}])
    await assert.rejects(run({...request,...extra}));
});
test("Engineer gets only the frozen role-specific text and no computer capabilities", async(t)=>{
  let calls=0;
  t.mock.method(globalThis, 'fetch', async(_url, init)=>{
    calls++;
    const body=JSON.parse(init.body);
    assert.deepEqual(Object.keys(body).sort(), ['input','instructions','model','store','stream']);
    assert.match(body.instructions, /^You are GPT Engineer\./);
    assert.equal(body.input[0].content,'Public pricing fixture');
    assert.equal(body.store,false);
    assert.equal(body.stream,true);
    return response(frame(delta('proposal'))+frame(completed));
  });
  assert.equal((await run({...request,role:'engineer',prompt:'Public pricing fixture'})).at(-1).text,'proposal');
  await assert.rejects(run({...request,role:'shell_operator'}));
  assert.equal(calls,1);
});

test('reviewer has a separate text instruction and no tool arguments', async(t)=>{
  let calls=0;
  t.mock.method(globalThis,'fetch',async(_url,init)=>{
    calls++;
    const body=JSON.parse(init.body);
    assert.match(body.instructions,/GPT Architect reviewing/);
    assert.deepEqual(Object.keys(body).sort(),['input','instructions','model','store','stream']);
    return response(frame(delta('review'))+frame(completed));
  });
  assert.equal((await run({...request,role:'reviewer'})).at(-1).text,'review');
  assert.equal(calls,1);
});

test('fragmented Unicode text remains exact with a bounded number of IPC packets', async(t)=>{
  const fragment = 'ёж🙂';
  let calls=0;
  t.mock.method(globalThis,'fetch',async()=>{
    calls++;
    return response(Array.from({length:1000},()=>frame(delta(fragment))).join('')+frame(completed));
  });
  const packets=await run();
  assert.equal(calls,1);
  assert.ok(packets.length < 128);
  assert.equal(packets.at(-1).type,'completed');
  assert.equal(packets.at(-1).text,fragment.repeat(1000));
  assert.equal(packets.slice(1,-1).map(packet=>packet.text).join(''),fragment.repeat(1000));
});

test('mentor receives sealed functional evidence in text only', async(t)=>{
  t.mock.method(globalThis,'fetch',async(_url,init)=>{
    const body=JSON.parse(init.body);
    assert.match(body.instructions,/distinguish sealed functional test evidence/);
    assert.deepEqual(Object.keys(body).sort(),['input','instructions','model','store','stream']);
    assert.equal(body.input[0].content,'Sealed pricing test summary');
    return response(frame(delta('review'))+frame(completed));
  });
  assert.equal((await run({...request,role:'mentor_tested',prompt:'Sealed pricing test summary'})).at(-1).text,'review');
});

test('project roles send only bounded text to the official transport', async(t)=>{
  const roles=['project_architect','project_engineer','project_reviewer','project_engineer_fragments'];
  const instructions=[];
  t.mock.method(globalThis,'fetch',async(_url,init)=>{
    const body=JSON.parse(init.body);
    instructions.push(body.instructions);
    assert.deepEqual(Object.keys(body).sort(),['input','instructions','model','store','stream']);
    assert.equal(body.input[0].content,'Approved Python snapshot');
    assert.equal(body.store,false);
    return response(frame(delta('fixture'))+frame(completed));
  });
  for(const role of roles) assert.equal((await run({...request,role,prompt:'Approved Python snapshot'})).at(-1).text,'fixture');
  assert.equal(new Set(instructions).size,4);
  for(const instruction of instructions) assert.match(instruction,/No tools.*file access/);
  assert.match(instructions[0], /plan and checks, both arrays of 1 to 5 nonblank strings, each at most 180 characters/);
  assert.match(instructions[1], /4096 characters.*4400 UTF-8 bytes.*400 characters/);
  assert.match(instructions[2], /700 characters.*1 to 5 nonblank strings, each at most 180 characters/);
  assert.match(instructions[3], /1 to 8 objects with exactly path, before_sha256, before and after.*4096 characters.*4400 UTF-8 bytes/);
});
