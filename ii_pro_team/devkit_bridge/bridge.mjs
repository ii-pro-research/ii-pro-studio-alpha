// Trusted single-operation IPC wrapper. Responses parsing belongs to OpenAI.
import { pathToFileURL } from 'node:url';
import projectOutput from './project-output.json' with { type: 'json' };
import { streamResponse } from './vendor/local/dist/responses.js';
import { ChatGPTError } from './vendor/local/dist/errors.js';

export const MAX_TEXT = 16000;
// Bound the number of stdout packets independently of the provider's SSE granularity.
const DELTA_BATCH_CHARS = 256;
const fields = ['access_token', 'deadline_ms', 'model', 'operation', 'prompt', 'request_id', 'role', 'version'];
const instructions = Object.freeze({
  diagnostic: 'You are GPT Architect. Return only the single line requested by the user. No tools or file access.',
  architect: 'You are GPT Architect. Plan from the supplied public fixture only. No tools or file access. Return only JSON: {"plan":["short step"],"checks":["observable check"]}.',
  mentor: 'You are GPT Architect mentoring after an incomplete Local attempt. No tools or files. Use observed facts only as evidence, explain ineffective tool choices and give coaching. Do not invent a Local conclusion or claim functional tests. Return only JSON: {"summary":"review summary","facts":["confirmed fact"],"assumptions":[],"coaching":["practice recommendation"]}.',
  mentor_tested: 'You are GPT Architect mentoring an incomplete Local attempt. No tools or files. Explain ineffective tool choices and distinguish sealed functional test evidence from Local observations. Do not invent a Local conclusion or generalize beyond the listed tests. Return only JSON: {"summary":"review summary","facts":["confirmed fact"],"assumptions":[],"coaching":["practice recommendation"]}.',
  reviewer: 'You are GPT Architect reviewing the supplied engineering evidence. No tools or file access. Distinguish syntax checks and observations from functional execution. Return only JSON: {"verdict":"accepted or changes_requested","summary":"public assessment","checks":["remaining check"]}.',
  engineer: 'You are GPT Engineer. Use only the supplied public fixture and evidence. No tools or file access. Do not claim any changes or tests were run. Return only JSON: {"path":"pricing.py","before":"exact source fragment","after":"replacement","explanation":"public engineering explanation","checks":["proposed verification"]}.',
  ...Object.fromEntries(Object.entries(projectOutput.templates).map(([role, template]) =>
    [role, template.replace(/@(\w+)@/g, (_, key) => String(projectOutput.limits[key]))])),
});
const codes = new Set(['invalid_stream', 'stream_interrupted', 'network_error', 'cancelled',
  'response_incomplete', 'response_too_large', 'output_quota', 'invalid_request', 'bridge_error',
  'subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable',
  'subscription_sharing_user_not_eligible', 'invalid_token', 'invalid_api_key', 'api_error',
  'model_not_found', 'server_error', 'invalid_request_error', 'permission_denied']);

export async function generate(request, emit, signal) {
  if (!request || typeof request !== 'object' || Array.isArray(request) ||
      JSON.stringify(Object.keys(request).sort()) !== JSON.stringify(fields) ||
      request.version !== 1 || request.operation !== 'text.generate' ||
      typeof request.role !== 'string' || !Object.hasOwn(instructions, request.role) ||
      request.model !== 'gpt-6.2-sol' || !/^[a-f0-9]{32}$/.test(request.request_id) ||
      typeof request.prompt !== 'string' || !request.prompt.trim() || request.prompt.length > (request.role === 'diagnostic' ? 1800 : 8000) ||
      typeof request.access_token !== 'string' || !/^[\x21-\x7e]{1,16000}$/.test(request.access_token) ||
      !Number.isInteger(request.deadline_ms) || request.deadline_ms < 1 ||
      (request.deadline_ms > 60000 && !(request.role === 'project_engineer_fragments' &&
        [120000, 180000].includes(request.deadline_ms)))) {
    throw new Error('Invalid bounded bridge envelope');
  }
  let length = 0, quota = false, pending = '';
  const flushDelta = () => {
    if (!pending) return;
    emit({type: 'delta', text: pending});
    pending = '';
  };
  const deadline = AbortSignal.timeout(request.deadline_ms);
  // The pinned transport exposes text deltas, but no raw SSE or connect callback.
  const idle = request.deadline_ms === 180000 ? new AbortController() : null;
  let idleTimer;
  const resetIdle = () => {
    if (!idle) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => idle.abort(), 90000);
  };
  const timed = AbortSignal.any([signal, deadline, ...(idle ? [idle.signal] : [])]);
  resetIdle();
  emit({type: 'started'}); // Invocation began; HTTP transmission is not observed.
  try {
    const result = await streamResponse(request.access_token, {
      model: request.model,
      input: request.prompt,
      instructions: instructions[request.role],
      onDelta(delta) {
        if (delta.length) resetIdle();
        length += delta.length;
        if (length > MAX_TEXT) { quota = true; throw new Error('Output quota'); }
        pending += delta;
        if (pending.length >= DELTA_BATCH_CHARS) flushDelta();
      },
    }, timed);
    timed.throwIfAborted();
    if (!result.text.trim() || result.text.length > MAX_TEXT) throw new Error('Empty or oversized result');
    // The unmodified official method resolves only after response.completed.
    flushDelta();
    emit({type: 'completed', text: result.text});
  } catch (error) {
    flushDelta();
    emit({type: 'error',
      code: signal.aborted ? 'cancelled' : deadline.aborted ? 'deadline'
        : idle?.signal.aborted ? 'text_idle' : quota ? 'output_quota'
        : error instanceof ChatGPTError && codes.has(error.code) ? error.code : 'bridge_error',
      http_status: error instanceof ChatGPTError && Number.isInteger(error.status) ? error.status : null,
      // Success usage, response identity, request ID and headers are not exposed by this module.
      request_id: error instanceof ChatGPTError && typeof error.requestId === 'string' &&
        /^[A-Za-z0-9_.:\[\]-]{1,200}$/.test(error.requestId) && !error.requestId.includes(request.access_token)
        ? error.requestId : null,
    });
  } finally {
    clearTimeout(idleTimer);
  }
}

export async function runMain() {
  // No SDK/exception logs are forwarded to Core; stdout is this protocol only.
  for (const key of ['debug', 'info', 'log', 'warn', 'error']) console[key] = () => {};
  const controller = new AbortController();
  let buffer = Buffer.alloc(0), started = false, settled = false;
  const emit = (packet) => process.stdout.write(JSON.stringify({version: 1, ...packet}) + '\n');
  const finish = () => { settled = true; process.stdin.pause(); process.stdin.destroy(); };
  process.stdin.on('data', (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length > 32768) { controller.abort(); process.exitCode = 2; finish(); return; }
    let end;
    while ((end = buffer.indexOf(10)) >= 0) {
      const line = buffer.subarray(0, end); buffer = buffer.subarray(end + 1);
      let value;
      try { value = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(line)); }
      catch { controller.abort(); process.exitCode = 2; finish(); return; }
      if (!started) {
        started = true;
        void generate(value, emit, controller.signal)
          .catch(() => { emit({type: 'error', code: 'invalid_request', http_status: null, request_id: null}); process.exitCode = 2; })
          .finally(finish);
      } else if (JSON.stringify(value) === '{"version":1,"operation":"cancel"}') {
        controller.abort();
      } else { controller.abort(); process.exitCode = 2; finish(); }
    }
  });
  process.stdin.on('end', () => { if (!settled) controller.abort(); });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await runMain();
