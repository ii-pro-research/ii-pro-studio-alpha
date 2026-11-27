export type Status = string;
export interface Run {
  text_generation?: boolean;
  diagnostic?: boolean;
  backend?: string;
  real_inference?: boolean;
  id: string;
  task_id: string;
  attempt: number;
  executor: string;
  status: Status;
  effect_status: string;
  error_code: string | null;
  created_at: string;
  finished_at: string | null;
  source: string;
}
export interface Exam {
  id: string;
  source: string;
  suite_id: string;
  created_at: string;
  run_id: string | null;
  status: Status;
  origin: string;
  code: string;
  code_redacted: boolean;
  code_sha256: string;
  static_awarded: number;
  static_available: number;
  total_awarded: number | null;
  functional: null | {
    passed: number;
    total: number;
    functional_awarded: number;
    cases: { id: string; status: string; diagnostic: string }[];
    resources: {
      memory_peak: number;
      cpu_usage_usec: number;
      pids_current: number;
    };
  };
  evidence: null | {
    verified: boolean;
    manifest_sha256: string;
    package_sha256: string;
    suite_sha256: string;
    artifacts: { id: string; name: string; sha256: string; bytes: number }[];
  };
}
export interface Attempt {
  backend?: string;
  real_inference?: boolean;
  run_id: string;
  lesson_id: string;
  sequence: number;
  status: Status;
  source: string;
  created_at: string;
  raw_answer: string;
  answer_sha256: string | null;
  elapsed_seconds: number | null;
  tokens: number | null;
  tokens_per_second: number | null;
  static_legacy: Record<string, unknown> | null;
}
export interface State {
  api_version: number;
  observed_at: string;
  read_only: boolean;
  sources: {
    key: string;
    title: string;
    status: Status;
    owner_state: string | null;
    schema: number | null;
  }[];
  core: { data_available: boolean; owner_state: string };
  ollama: {
    status: Status;
    version: string | null;
    model_available: boolean;
    model_digest: string | null;
    loaded: {
      name: string;
      size_mib: number | null;
      vram_mib: number | null;
    }[];
  };
  worker: {
    daemon: Status;
    linux: boolean | null;
    cgroup: string | null;
    jobs: {
      run_id: string;
      phase: string;
      stop_confirmed: number;
      source: string;
      exit_code: number | null;
      reason: string;
    }[];
  };
  resources: {
    cpu_percent: number | null;
    ram_total_mib: number | null;
    ram_available_mib: number | null;
    gpus: {
      name: string;
      utilization: number | null;
      used_mib: number | null;
      total_mib: number | null;
      temperature: number | null;
      power_w: number | null;
    }[];
  };
  tasks: {
    id: string;
    title: string;
    executor: string;
    status: string;
    source: string;
    created_at: string;
    updated_at: string;
  }[];
  runs: Run[];
  students: { id: string; name: string; model_id: string; source: string }[];
  models: {
    id: string;
    name: string;
    digest: string;
    options: Record<string, number>;
    source: string;
  }[];
  lessons: {
    id: string;
    kind: string;
    title: string;
    requirements: string;
    source: string;
  }[];
  attempts: Attempt[];
  feedback: {
    run_id: string;
    role: string;
    origin: string;
    author: string;
    text: string;
  }[];
  exams: Exam[];
  latest_exam: Exam | null;
  warnings: { kind: string; message: string }[];
  project: {
    name: string;
    branch: string | null;
    commit: string | null;
    language: string;
    components: Record<string, boolean>;
  };
  team: { role: string; description: string; status: Status }[];
}
