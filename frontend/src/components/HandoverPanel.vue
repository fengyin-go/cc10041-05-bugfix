<template>
  <section class="handover" data-testid="handover-panel">
    <header class="handover-head">
      <h3>交接期转换</h3>
      <p class="page-desc">
        兼容报告两个方向分别核对必需列；映射必须关联与本次候选字段相符的报告；受理时冻结映射版本。
      </p>
    </header>

    <div class="handover-grid">
      <article class="card">
        <h4>1. 字段兼容检查</h4>
        <label class="field">
          <span>Schema / 主题</span>
          <input v-model="topic" data-testid="handover-topic" placeholder="orders" />
        </label>
        <label class="field">
          <span>候选字段（逗号分隔）</span>
          <input v-model="candidate" data-testid="handover-candidate" placeholder="id,total" />
        </label>
        <button class="btn primary" type="button" data-testid="handover-check" @click="runCheck">
          生成兼容报告
        </button>

        <div v-if="report" class="report" :data-compatible="String(report.compatible)">
          <p>
            <strong>{{ report.compatible ? '兼容' : '不兼容' }}</strong>
            <span class="muted"> · {{ report.report_id }} · {{ report.normalized_compatibility_type }}</span>
          </p>
          <p class="muted">{{ report.reason }}</p>
          <table class="mini-table">
            <thead>
              <tr><th>方向</th><th>必需列</th><th>命中</th><th>缺失</th><th>结论</th></tr>
            </thead>
            <tbody>
              <tr v-for="dir in directionRows" :key="dir.direction">
                <td>{{ dir.direction }}</td>
                <td>{{ dir.required_columns.join(', ') }}</td>
                <td>{{ formatMatched(dir.matched_columns) }}</td>
                <td>{{ dir.missing_required.join(', ') || '—' }}</td>
                <td :class="dir.compatible ? 'ok' : 'bad'">{{ dir.compatible ? '通过' : '失败' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>

      <article class="card">
        <h4>2. 发布映射</h4>
        <label class="field">
          <span>映射名称</span>
          <input v-model="mappingName" data-testid="handover-mapping-name" />
        </label>
        <label class="field">
          <span>字段映射（JSON）</span>
          <input v-model="fieldMapText" data-testid="handover-field-map" placeholder='{"id":"id","amount":"amount"}' />
        </label>
        <label class="field">
          <span>金额优先字段（逗号分隔）</span>
          <input v-model="priorityText" data-testid="handover-priority" placeholder="gross_total" />
        </label>
        <label class="check">
          <input v-model="confirmOnPublish" type="checkbox" data-testid="handover-confirm-now" />
          <span>发布确认已到达</span>
        </label>
        <button class="btn primary" type="button" data-testid="handover-publish" @click="runPublish">
          发布映射
        </button>
        <p v-if="publishMessage" class="muted">{{ publishMessage }}</p>
      </article>

      <article class="card">
        <h4>3. 受理消息</h4>
        <label class="field">
          <span>交易号</span>
          <input v-model="transactionId" data-testid="handover-txn" placeholder="TX-001" />
        </label>
        <label class="field">
          <span>消息负载（JSON）</span>
          <input v-model="payloadText" data-testid="handover-payload" placeholder='{"id":1,"amount":13}' />
        </label>
        <label class="check">
          <input v-model="deferConversion" type="checkbox" data-testid="handover-defer" />
          <span>先受理，稍后放行转换</span>
        </label>
        <button class="btn primary" type="button" data-testid="handover-accept" @click="runAccept">
          提交消息
        </button>
        <p v-if="acceptMessageText" class="muted" data-testid="handover-accept-status">{{ acceptMessageText }}</p>
      </article>

      <article class="card">
        <h4>4. 放行与收入汇总</h4>
        <div class="row-actions">
          <button class="btn" type="button" data-testid="handover-release" @click="runRelease">放行待转换</button>
          <button class="btn" type="button" data-testid="handover-summary" @click="refreshSummary">刷新汇总</button>
        </div>
        <p v-if="releaseMessage" class="muted">{{ releaseMessage }}</p>
        <p v-if="summary" class="muted" data-testid="handover-summary-total">
          有效交易 {{ summary.count }} 笔，合计 {{ summary.total_amount }} 元
        </p>
        <table v-if="summary && summary.entries.length" class="mini-table">
          <thead>
            <tr><th>交易号</th><th>金额</th><th>来源字段</th><th>映射版本</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in summary.entries" :key="row['交易号']">
              <td>{{ row['交易号'] }}</td>
              <td>{{ row['金额'] }}</td>
              <td>{{ row['金额来源'] ?? '—' }}</td>
              <td>{{ row.mapping_version }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="summary && summary.excluded.length" class="muted">
          未计入：{{ summary.excluded.map((item) => `${item.message_id}(${item.reason})`).join('；') }}
        </p>
        <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  acceptMessage as submitMessage,
  checkCompatibility,
  confirmMapping,
  getIncomeSummary,
  publishMapping,
  releasePending,
} from '@/api/transform-service'
import type { CompatReport, IncomeSummary } from '@/api/transform-service'

const topic = ref('orders')
const candidate = ref('id,total')
const mappingName = ref('交接映射')
const fieldMapText = ref('{"id":"id","amount":"total"}')
const priorityText = ref('')
const confirmOnPublish = ref(false)
const transactionId = ref('TX-001')
const payloadText = ref('{"id":1,"amount":13}')
const deferConversion = ref(false)

const report = ref<CompatReport | null>(null)
const summary = ref<IncomeSummary | null>(null)
const publishMessage = ref('')
const acceptMessageText = ref('')
const releaseMessage = ref('')
const errorMessage = ref('')

const directionRows = computed(() =>
  report.value ? Object.values(report.value.directions) : [],
)

function formatMatched(matched: Record<string, string>) {
  return Object.entries(matched)
    .map(([name, hit]) => (name === hit ? name : `${name}←${hit}`))
    .join(', ') || '—'
}

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '操作失败'
}

async function runCheck() {
  errorMessage.value = ''
  try {
    report.value = await checkCompatibility(topic.value, candidate.value)
  } catch (error) {
    fail(error)
  }
}

async function runPublish() {
  errorMessage.value = ''
  publishMessage.value = ''
  try {
    if (!report.value) throw new Error('请先生成兼容报告')
    let fieldMap: Record<string, string>
    try {
      fieldMap = JSON.parse(fieldMapText.value)
    } catch {
      throw new Error('字段映射不是合法 JSON')
    }
    const result = await publishMapping({
      name: mappingName.value,
      topic: topic.value,
      field_map: fieldMap,
      compat_report_id: report.value.report_id,
      candidate_fields: candidate.value,
      amount_priority: priorityText.value,
      confirm: confirmOnPublish.value,
    })
    if (!result.ok) {
      publishMessage.value = `发布被拒绝：${String(result.reason ?? '')}`
      return
    }
    const mapping = result.mapping as { mapping_id: number; mapping_version: string }
    if (confirmOnPublish.value) {
      publishMessage.value = `已发布并确认 ${mapping.mapping_version}`
    } else {
      const confirmed = await confirmMapping(mapping.mapping_id)
      publishMessage.value = String(confirmed.message ?? `已登记 ${mapping.mapping_version}，等待确认`)
    }
    await refreshSummary()
  } catch (error) {
    fail(error)
  }
}

async function runAccept() {
  errorMessage.value = ''
  acceptMessageText.value = ''
  try {
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(payloadText.value)
    } catch {
      throw new Error('消息负载不是合法 JSON')
    }
    const result = await submitMessage({
      topic: topic.value,
      message_id: transactionId.value,
      payload,
      defer_conversion: deferConversion.value,
    })
    const suffix = result.result
      ? `，金额 ${result.result.amount}，来源 ${result.result['金额来源'] ?? '缺失'}，版本 ${result.result.mapping_version}`
      : ''
    acceptMessageText.value = `${result.status}：${result.message}${suffix}`
    await refreshSummary()
  } catch (error) {
    fail(error)
  }
}

async function runRelease() {
  errorMessage.value = ''
  try {
    const result = await releasePending(topic.value)
    releaseMessage.value = `已放行 ${result.released_count} 笔`
    await refreshSummary()
  } catch (error) {
    fail(error)
  }
}

async function refreshSummary() {
  try {
    summary.value = await getIncomeSummary(topic.value)
  } catch (error) {
    fail(error)
  }
}
</script>

<style scoped>
.handover { margin: 16px 0; }
.handover-head h3 { margin: 0 0 4px; }
.handover-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.card h4 { margin: 0 0 10px; font-size: 14px; }
.field { display: block; margin-bottom: 8px; }
.field span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.field input { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.check { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-bottom: 8px; }
.report { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 8px; }
.mini-table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 12px; }
.mini-table th, .mini-table td { border: 1px solid var(--border); padding: 4px 6px; text-align: left; }
.muted { color: var(--muted); font-size: 12px; }
.ok { color: #15803d; }
.bad { color: #b42318; }
.row-actions { display: flex; gap: 8px; margin-bottom: 8px; }
</style>
