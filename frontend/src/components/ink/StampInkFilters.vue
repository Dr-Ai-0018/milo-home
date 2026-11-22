<script setup lang="ts">
// 墨怎么落到纸上，两个滤镜放在印面的 <defs> 里：
// f：边缘发毛（位移）、漏白（细噪声挖洞）、按压不匀（大斑块）；
// p：轮廓积墨（边缘一圈）和局部墨团。噪声种子都来自同一枚章，刷新不变。
defineProps<{ id: string; noise: number; rough: number; dry: number }>()
</script>

<template>
  <filter
    :id="`f${id}`"
    x="-15%"
    y="-15%"
    width="130%"
    height="130%"
  >
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.08"
      numOctaves="2"
      :seed="noise"
      result="wobble"
    />
    <feDisplacementMap
      in="SourceGraphic"
      in2="wobble"
      :scale="rough"
      xChannelSelector="R"
      yChannelSelector="G"
      result="rough"
    />
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.75"
      numOctaves="2"
      :seed="noise + 3"
      result="grain"
    />
    <feColorMatrix
      in="grain"
      type="matrix"
      :values="`0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 -4.2 0 0 0 ${dry}`"
      result="speck"
    />
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.028"
      numOctaves="2"
      :seed="noise + 7"
      result="blotch"
    />
    <feColorMatrix
      in="blotch"
      type="matrix"
      values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 3 0 0 0 -0.55"
      result="press"
    />
    <feComposite
      in="speck"
      in2="press"
      operator="arithmetic"
      k1="1"
      k2="0.25"
      result="cover"
    />
    <feComposite
      in="rough"
      in2="cover"
      operator="in"
    />
  </filter>
  <filter
    :id="`p${id}`"
    x="-15%"
    y="-15%"
    width="130%"
    height="130%"
  >
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.08"
      numOctaves="2"
      :seed="noise"
      result="wobble"
    />
    <feDisplacementMap
      in="SourceGraphic"
      in2="wobble"
      scale="1.8"
      xChannelSelector="R"
      yChannelSelector="G"
      result="rough"
    />
    <feMorphology
      in="rough"
      operator="erode"
      radius="0.9"
      result="inner"
    />
    <feComposite
      in="rough"
      in2="inner"
      operator="out"
      result="rim"
    />
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.045"
      numOctaves="2"
      :seed="noise + 11"
      result="pools"
    />
    <feColorMatrix
      in="pools"
      type="matrix"
      values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 6.5 0 0 0 -3.6"
      result="pool"
    />
    <feComposite
      in="rough"
      in2="pool"
      operator="in"
      result="blob"
    />
    <feMerge>
      <feMergeNode in="rim" />
      <feMergeNode in="blob" />
    </feMerge>
  </filter>
</template>
