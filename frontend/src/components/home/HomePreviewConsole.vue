<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  COMBINED_PREVIEW_SUMMARY,
  HOME_DESIGN_RULES,
  PREVIEW_INK_CHOICES,
  SCENARIO_OPTIONS,
} from '../../data/homePreviewFixture'
import type { HomePreviewScenario } from '../../data/homePreviewFixture'
import type { InkKey } from '../../theme/inks'
import '../../styles/home-preview-console.css'

const props = defineProps<{
  scenario: HomePreviewScenario
  inkKey: InkKey
}>()

const emit = defineEmits<{
  selectScenario: [scenario: HomePreviewScenario]
  replaySlow: []
  selectInk: [key: InkKey]
}>()

const barCollapsed = ref(false)

const currentScenarioMeta = computed(
  () => SCENARIO_OPTIONS.find((item) => item.id === props.scenario) ?? SCENARIO_OPTIONS[0],
)
</script>

<template>
  <!-- 轻量预览台：可一键收起，方便无干扰核对组合定版与窄屏排布。 -->
  <aside
    class="preview-console"
    aria-label="首页组合定版与场景预览控制台"
  >
    <div class="preview-console-bar">
      <div class="preview-console-heading">
        <span class="preview-badge">首页组合定版预览</span>
        <strong>窗景首屏 × 家刊近况</strong>
        <span class="preview-summary">{{ currentScenarioMeta?.label }}</span>
      </div>
      <div class="preview-console-toggles">
        <RouterLink
          class="text-button preview-cross-link"
          to="/preview/work-detail"
        >
          看交办详情样板 →
        </RouterLink>
        <button
          type="button"
          class="text-button preview-collapse-btn"
          :aria-expanded="!barCollapsed"
          @click="barCollapsed = !barCollapsed"
        >
          {{ barCollapsed ? '展开场景面板' : '收起面板' }}
        </button>
      </div>
    </div>

    <div
      v-if="!barCollapsed"
      class="preview-console-body"
    >
      <div class="preview-control-row">
        <span class="preview-control-label">合成场景</span>
        <div
          class="preview-chip-group"
          role="group"
          aria-label="切换账号与网络状态场景"
        >
          <button
            v-for="item in SCENARIO_OPTIONS"
            :key="item.id"
            type="button"
            class="preview-chip is-sm"
            :aria-pressed="scenario === item.id"
            @click="emit('selectScenario', item.id)"
          >
            {{ item.label }}
          </button>
          <button
            v-if="scenario === 'schedule-slow'"
            type="button"
            class="preview-chip is-sm is-replay"
            @click="emit('replaySlow')"
          >
            重播 900ms 慢响应
          </button>
        </div>
      </div>

      <div class="preview-control-row">
        <span class="preview-control-label">代表色档</span>
        <div
          class="preview-chip-group"
          role="group"
          aria-label="对比代表色档正文与焦点"
        >
          <button
            v-for="choice in PREVIEW_INK_CHOICES"
            :key="choice.key"
            type="button"
            class="preview-chip is-sm"
            :aria-pressed="inkKey === choice.key"
            @click="emit('selectInk', choice.key)"
          >
            {{ choice.label }}
          </button>
        </div>
        <span class="preview-variant-note">{{ COMBINED_PREVIEW_SUMMARY }}</span>
      </div>

      <details class="preview-rules-drawer">
        <summary>查看组合定版规则与视觉约束（6 条）</summary>
        <dl class="preview-rules-list">
          <div
            v-for="item in HOME_DESIGN_RULES"
            :key="item.title"
            class="preview-rule-item"
          >
            <dt>{{ item.title }}</dt>
            <dd>{{ item.rule }}</dd>
          </div>
        </dl>
      </details>
    </div>
  </aside>
</template>
