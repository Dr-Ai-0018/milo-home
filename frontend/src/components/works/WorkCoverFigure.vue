<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { PublicWorkCover } from '../../api/client'
import { formatWorkCoverRatio, resolvePublicAssetUrl } from '../../utils/publicWorks'

const props = withDefaults(
  defineProps<{
    cover: PublicWorkCover
    variant?: 'home' | 'catalog' | 'detail'
  }>(),
  {
    variant: 'detail',
  },
)

const failed = ref(false)

const src = computed(() => resolvePublicAssetUrl(props.cover.url))
const ratio = computed(() => formatWorkCoverRatio(props.cover))

watch([src, () => props.cover], () => {
  failed.value = false
})

function onError() {
  failed.value = true
}
</script>

<template>
  <figure
    class="work-cover-figure"
    :class="`is-${variant}`"
  >
    <div
      class="work-cover-frame"
      :class="{ 'is-failed': failed }"
      :style="{ aspectRatio: ratio }"
    >
      <img
        v-if="!failed"
        class="work-cover-img"
        :src="src"
        :alt="cover.alt"
        :width="cover.width"
        :height="cover.height"
        decoding="async"
        @error="onError"
      />
      <div
        v-else
        class="work-cover-fallback"
        role="img"
        :aria-label="cover.alt"
      >
        <p class="work-cover-fallback-state">封面暂时无法读取</p>
        <p class="work-cover-fallback-alt">{{ cover.alt }}</p>
      </div>
    </div>
  </figure>
</template>
