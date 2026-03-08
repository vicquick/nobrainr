export interface Memory {
  id: string
  content: string
  summary: string | null
  category: string | null
  tags: string[]
  source_type: string | null
  source_machine: string | null
  importance: number
  stability: number
  access_count: number
  extraction_status: string | null
  created_at: string
  updated_at: string
  similarity?: number
  relevance_score?: number
  quality_score?: number | null
  quality_specificity?: number | null
  quality_actionability?: number | null
  quality_self_containment?: number | null
}

export interface Entity {
  id: string
  canonical_name: string
  entity_type: string
  description: string | null
  mention_count: number
  created_at: string
  confidence?: number
}

export interface EntityConnection {
  direction: string
  relationship_type: string
  connected_name: string
  connected_type: string
  connected_id: string
  confidence: number
}

export interface NodeDetail {
  entity: Entity
  connections: EntityConnection[]
  memories: Memory[]
}

export interface GraphData {
  nodes: Array<{ data: { id: string; label: string; type: string; mention_count: number; description?: string; x: number; y: number; community: number } }>
  edges: Array<{ data: { id: string; source: string; target: string; label: string; confidence: number } }>
}

export interface Stats {
  total_memories: number
  total_entities: number
  total_relations: number
  raw_conversations: number
  extraction_done: number
  extraction_pending: number
  by_source: Array<{ source_type: string; cnt: number }>
  by_category: Array<{ category: string; cnt: number }>
  by_machine: Array<{ source_machine: string; cnt: number }>
  top_tags: Array<{ tag: string; cnt: number }>
  feedback_total: number
  feedback_positive: number
  archived_memories: number
  events_24h: number
}

export interface SchedulerStatus {
  running: boolean
  tasks: Array<{
    name: string
    interval_hours: number
    last_run: string | null
    next_run: string | null
    run_count: number
    type?: string
  }>
}

export interface AgentEvent {
  id: string
  event_type: string
  event_data: Record<string, unknown>
  source: string | null
  created_at: string
}

export interface FeedbackStats {
  total: number
  positive: number
  negative: number
  positive_rate: number
}

export interface ChatSources {
  memories: Array<{ id: string; summary: string | null; content: string }>
  entities: Array<{ id: string; name: string; entity_type: string }>
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  sources?: ChatSources
  timestamp: number
}
