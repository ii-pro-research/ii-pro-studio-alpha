// Test-only entry point: fetch never reaches a network, regardless of credentials.
import { runMain } from '../bridge.mjs';
const frame=p=>`data: ${JSON.stringify(p)}\n\n`;
globalThis.fetch=async(_url,{signal,body})=>{
  const mode=process.env.II_PRO_TEST_CASE || 'success';
  if(mode==='http') return Response.json({error:{code:'server_error',message:'PRIVATE'}},{status:500});
  if(mode==='hang') return new Response(new ReadableStream({start(body){
    signal.addEventListener('abort',()=>body.error(new DOMException('cancel','AbortError')),{once:true});
  }}));
  let data=frame({type:'response.created',response:{id:'fixture'}})+frame({type:'response.future_metadata',text:'NOT PUBLIC'});
  if (mode === 'many_chunks' || mode === 'many_chunks_eof') {
    for (let n = 0; n < 180; n++) data += frame({type:'response.output_text.delta', delta:'abcd'});
    if (mode === 'many_chunks') data += frame({type:'response.completed',response:{status:'completed'}});
    return new Response(new TextEncoder().encode(data));
  }
  const {instructions} = JSON.parse(body);
  if(mode==='limit') return new Response(new TextEncoder().encode(
    data+frame({type:'error',code:'subscription_sharing_usage_limit_exceeded'})
  ));
  const answer=mode==='secret'?'fixture-access-credential':
    instructions.startsWith('You are GPT Architect mentoring') ? JSON.stringify({
      summary:'The Local attempt was closed without a conclusion.',facts:['No Local conclusion was produced'],
      assumptions:[],coaching:['Compare the original snapshot with the separate candidate'],
    }) :
    instructions.startsWith('You are GPT Architect reviewing') ? JSON.stringify({
      verdict:'accepted',summary:'Syntax and observations support the proposed change.',checks:['Run the numeric acceptance check separately'],
    }) :
    instructions.startsWith('You are GPT Engineer.') ? JSON.stringify({
      path:'pricing.py',before:'    return price + price * percent / 100',after:'    return price - price * percent / 100',
      explanation:'A discount subtracts the percentage.',checks:['10% of 100 gives 90'],
    }) : instructions.includes('Plan from the supplied public fixture') ? JSON.stringify({
      plan:['Inspect the discount formula'], checks:['Preserve the function signature'],
    }) : 'Developer Mock answer';
  data+=frame({type:'response.output_text.delta',delta:answer});
  if(mode!=='eof' && !(mode==='mentor_eof' && instructions.startsWith('You are GPT Architect mentoring')))data+=frame({type:'response.completed',response:{status:'completed'}});
  return new Response(new TextEncoder().encode(data));
};
await runMain();
