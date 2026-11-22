/** 交接期转换相关的接口封装：兼容报告、映射发布、消息受理、收入汇总。 */

export type CompatDirection = {
  direction: string
  label: string
  required_columns: string[]
  matched_columns: Record<string, string>
  missing_required: string[]
  removed_fields: string[]
  added_fields: string[]
  compatible: boolean
}

export type CompatReport = {
  report_id: string
  schema_name: string
  compatible: boolean
  reason: string
  existing_fields: string[]
  candidate_fields: string[]
  required_columns: string[]
  amount_aliases: string[]
  compatibility_type: string
  normalized_compatibility_type: string
  removed_fields: string[]
  added_fields: string[]
  fields_fingerprint: string
  directions: Record<string, CompatDirection>
}

export type MappingSnapshot = {
  mapping_id: number
  mapping_name: string
  mapping_version: string
  topic: string
  field_map: Record<string, string>
  amount_priority: string[]
  candidate_fields: string[]
  required_fields: string[]
  compat_report_id: string
  confirmed: boolean
}

export type ConvertResult = {
  message_id: string
  id: number | string | null
  amount: number | null
  '金额来源': string | null
  '取值方式': string
  '可读金额字段': Record<string, number>
  mapping_version: string
  mapping_confirmed: boolean
  counted: boolean
}

export type MessageRecord = {
  message_id: string
  topic: string
  payload: Record<string, unknown>
  status: string
  accepted: boolean
  missing_required: string[]
  reason?: string
  mapping_version: string | null
}

export type IncomeSummary = {
  count: number
  total_amount: number
  entries: {
    '交易号': string
    '金额': number
    '金额来源': string | null
    '取值方式': string
    mapping_version: string
  }[]
  excluded: { message_id: string; reason: string }[]
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export function checkCompatibility(schemaName: string, candidateFields: string) {
  return postJson<CompatReport>('/api/transform/compatibility/check', {
    schema_name: schemaName,
    candidate_fields: candidateFields,
  })
}

export function publishMapping(payload: {
  name: string
  topic: string
  field_map: Record<string, string>
  compat_report_id: string
  candidate_fields: string
  amount_priority?: string
  confirm: boolean
}) {
  return postJson<Record<string, unknown>>('/api/transform/mappings', {
    ...payload,
    candidate_fields: payload.candidate_fields,
    amount_priority: payload.amount_priority ?? '',
  })
}

export function confirmMapping(mappingId: number) {
  return postJson<Record<string, unknown>>(`/api/transform/mappings/${mappingId}/confirm`, {})
}

export function acceptMessage(payload: {
  topic: string
  message_id: string
  payload: Record<string, unknown>
  defer_conversion: boolean
}) {
  return postJson<{
    ok: boolean
    accepted: boolean
    deduplicated: boolean
    status: string
    message: string
    message_record: MessageRecord
    original: Record<string, unknown>
    result: ConvertResult | null
  }>('/api/transform/messages', payload)
}

export function releasePending(topic: string) {
  return postJson<{ released_count: number; results: ConvertResult[] }>(
    `/api/transform/messages/release?topic=${encodeURIComponent(topic)}`,
    {},
  )
}

export function getIncomeSummary(topic: string) {
  return getJson<IncomeSummary>(`/api/transform/income/summary?topic=${encodeURIComponent(topic)}`)
}
