<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
type Lesson = { text: string; evidence_ids: string[]; kind?: string }
type Step = { id: string; role: string; purpose: string; depends_on: string[]; evidence_ids: string[] }
type Blueprint = {
  id: string; source_run: string; source_hash: string; goal: string; summary: string;
  verified_success: boolean; semantic_status: string; concepts: string[];
  lessons: Lesson[]; steps: Step[]; gates: Record<string, unknown>[];
  measurements: { agents: number; events: number; tokens: number | null; cost_usd: number | null };
}
const blueprints = ref<Blueprint[]>([])
const query = ref(''), nextGoal = ref(''), error = ref(''), selected = ref<Blueprint | null>(null)
const briefing = ref('')
const matches = computed(() => blueprints.value.filter(b =>
  [b.goal, b.summary, ...b.concepts].join(' ').toLowerCase().includes(query.value.toLowerCase())))
let timer: ReturnType<typeof setInterval> | undefined
async function load() {
  try {
    const res = await fetch('/api/blueprints')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    blueprints.value = await res.json()
    error.value = ''
  } catch (e) { error.value = String(e) }
}
onMounted(() => { void load(); timer = setInterval(load, 5000) })
onUnmounted(() => clearInterval(timer))
function choose(b: Blueprint) { selected.value = b; briefing.value = '' }
function prepare() {
  const b = selected.value
  if (!b || !nextGoal.value.trim()) return
  briefing.value = JSON.stringify({new_goal:nextGoal.value, source_blueprint:b.id,
    source_run:b.source_run, source_hash:b.source_hash, advisory_only:true,
    verified_source:b.verified_success, summary:b.summary,
    suggested_steps:b.verified_success ? b.steps : [], lessons:b.lessons,
    permissions:[], budget:null,
    revalidate:['Current repository and dependencies','File hashes via the blueprint warm CLI',
      'New acceptance criteria','Every imported lesson against its source evidence']}, null, 2)
}
function download() {
  const url = URL.createObjectURL(new Blob([briefing.value], {type:'application/json'}))
  const a = document.createElement('a'); a.href=url; a.download='warm-start.json'; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <section class="blueprints">
    <div class="heading"><div><p class="eyebrow">EXECUTION MEMORY</p><h1>Blueprints</h1>
      <p>Roles, decisions and acceptance evidence from completed factory runs.</p></div>
      <span class="count">{{ blueprints.length }} recorded</span></div>
    <p v-if="error" class="error">{{ error }}</p>
    <input v-model="query" placeholder="Filter by task, decision or concept" aria-label="Filter blueprints" />
    <div class="layout">
      <div class="list">
        <p v-if="!matches.length" class="empty">No matching blueprints. An AGY workflow creates one when its run ends.</p>
        <button v-for="b in matches" :key="b.id" class="entry" :class="{selected:selected?.id===b.id}" @click="choose(b)">
          <span class="badge" :class="{pass:b.verified_success}">{{ b.verified_success ? 'VERIFIED RUN' : 'PARTIAL / FAILED' }}</span>
          <strong>{{ b.goal }}</strong>
          <span>{{ b.measurements.agents }} agents / {{ b.measurements.events }} events</span>
          <span class="tags">{{ b.concepts.join(' / ') || 'Semantic annotation pending' }}</span>
        </button>
      </div>
      <article v-if="selected" class="detail">
        <p class="eyebrow">{{ selected.semantic_status }}</p>
        <h2>{{ selected.summary || selected.goal }}</h2>
        <p><a :href="`#/${selected.source_run}`">Open original execution trace</a></p>
        <p class="dim">{{ selected.measurements.tokens?.toLocaleString() ?? 'Unknown' }} tokens /
          {{ selected.measurements.cost_usd == null ? 'Cost unavailable from provider' : `$${selected.measurements.cost_usd.toFixed(4)}` }}</p>
        <h3>Observed workflow</h3>
        <ol><li v-for="s in selected.steps" :key="s.id"><strong>{{ s.role }}</strong> {{ s.purpose }}
          <small>{{ s.evidence_ids.join(', ') }}</small></li></ol>
        <h3>Decisions and remaining risks</h3>
        <p v-if="!selected.lessons.length" class="dim">No semantic lessons have been recorded.</p>
        <div v-for="(l,i) in selected.lessons" :key="i" class="lesson"><p>{{ l.text }}</p>
          <small>{{ l.kind }} / evidence {{ l.evidence_ids.join(', ') }}</small></div>
        <details><summary>Acceptance evidence ({{ selected.gates.length }})</summary><pre>{{ JSON.stringify(selected.gates,null,2) }}</pre></details>
        <h3>Prepare the next run</h3>
        <p class="dim">Reuse context after checking relevance. New runs keep their own permissions, limits and tests.</p>
        <textarea v-model="nextGoal" placeholder="State the new task and definition of done" aria-label="New task" />
        <button :disabled="!nextGoal.trim()" @click="prepare">Prepare warm start</button>
        <template v-if="briefing"><textarea readonly :value="briefing" class="briefing" aria-label="Warm start JSON" />
          <button @click="download">Download warm-start.json</button></template>
      </article>
      <article v-else class="detail empty">Select a run to inspect the evidence and prepare a warm start.</article>
    </div>
  </section>
</template>

<style scoped>
.blueprints{max-width:1500px;margin:auto;padding:32px}.heading{display:flex;justify-content:space-between;align-items:center}
.eyebrow{color:var(--cyan);letter-spacing:.16em;font-size:13px}h1{font-size:38px;margin:0}h2{font-size:23px}h3{margin-top:28px}
.heading p,.dim,.count,.empty{color:var(--dim)}input,textarea{width:100%;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:12px;font:inherit}input{margin:18px 0}textarea{min-height:100px}
.layout{display:grid;grid-template-columns:minmax(280px,1fr) 2fr;gap:24px}.entry{display:flex;flex-direction:column;text-align:left;gap:10px;width:100%;margin-bottom:14px;background:var(--surface);color:var(--text);border:1px solid var(--border);padding:20px;border-radius:12px;cursor:pointer}.entry.selected{border-color:var(--cyan)}.entry span{font-size:14px;color:var(--dim)}.entry .badge{font-size:12px;color:var(--amber)}.entry .pass{color:var(--green)}.entry strong{font-size:17px}.entry .tags{color:var(--purple)}
.detail{background:var(--surface);padding:24px;border:1px solid var(--border);border-radius:12px;min-width:0}.detail>button{padding:10px 16px;margin:12px 0;border:1px solid var(--cyan);border-radius:6px;background:var(--panel-2);color:var(--cyan);cursor:pointer}.detail>button:disabled{opacity:.4;cursor:default}.lesson{border-left:2px solid var(--purple);padding-left:14px}small{display:block;color:var(--faint);font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}li{margin-bottom:16px}.briefing{height:260px;font-family:var(--mono);font-size:12px}.error{color:var(--red)}pre{max-height:350px;overflow:auto} @media(max-width:850px){.layout{grid-template-columns:1fr}.blueprints{padding:18px}}
</style>
