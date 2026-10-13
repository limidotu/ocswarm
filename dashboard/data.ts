export type TokensSource = "org" | "local" | "none";

export interface DashboardKpis {
  
  tokensSaved: number | null;
  
  tokensSource: TokensSource;
  
  skillsCreated: number;
  
  memorySearches: number;
  
  sessionsCount: number | null;
  
  userTokensSaved: number | null;
}

export interface DashboardGraphSummary {
  
  commitSha: string | null;
  
  snapshotPath: string;
  nodeCount: number;
  edgeCount: number;
  
  snapshot: unknown;
}

export interface SwarmSlot {
  name: string;
  role: string;
  state: string;
  item: string;
  activity: string;
  model?: string;
  elapsed?: number;
  log?: string;
}

export interface SwarmTaskRecord {
  id: string;
  title: string;
  state: string;
  role?: string;
  brief?: string;
  summary?: string;
  last_error?: string;
  owns?: string[];
  files?: string[];
  evidence?: string[];
  model?: string;
  time?: string;
  tests_passed?: boolean;
}

export interface SwarmView {
  phase: string;
  message: string;
  task: string;
  done: number;
  total: number;
  running: number;
  waiting: number;
  slots: SwarmSlot[];
  tasks?: SwarmTaskRecord[];
  history?: SwarmTaskRecord[];
}

export interface DashboardData {
  
  repoKey: string;
  
  repoProject: string;
  
  generatedAt: string;
  kpis: DashboardKpis;
  
  graph: DashboardGraphSummary | null;
  
  swarm?: SwarmView;
}

