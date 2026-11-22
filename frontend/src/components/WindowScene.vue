<script setup lang="ts">
import { handCopy } from '../theme/handCopy'
import HandNote from './ink/HandNote.vue'
import '../styles/window-scene.css'
</script>

<template>
  <!-- 双色 Risograph 套印静物：有机曲线、窗外远山流云与微光热气，全部颜色绑定油墨变量。 -->
  <figure class="window-scene">
    <svg
      viewBox="0 0 560 400"
      role="img"
      aria-labelledby="scene-title scene-desc"
    >
      <title id="scene-title">窗边的一张书桌</title>
      <desc id="scene-desc">
        双色手绘静物：拱顶木窗外有远山、流云与暖阳，窗台放着摊开的手账本、钢笔、冒热气的茶杯、绿植和弯颈台灯。
      </desc>
      <defs>
        <pattern
          id="ws-dots"
          width="6.5"
          height="6.5"
          patternUnits="userSpaceOnUse"
        >
          <circle
            cx="2"
            cy="2"
            r="1.15"
          />
        </pattern>
        <linearGradient
          id="ws-sky-grad"
          x1="0%"
          y1="0%"
          x2="0%"
          y2="100%"
        >
          <stop
            offset="0%"
            class="ws-sky-top"
          />
          <stop
            offset="100%"
            class="ws-sky-bottom"
          />
        </linearGradient>
        <radialGradient
          id="ws-sun-halo"
          cx="50%"
          cy="50%"
          r="50%"
        >
          <stop
            offset="0%"
            class="ws-halo-core"
          />
          <stop
            offset="55%"
            class="ws-halo-mid"
          />
          <stop
            offset="100%"
            class="ws-halo-edge"
          />
        </radialGradient>
        <radialGradient
          id="ws-lamp-glow"
          cx="46%"
          cy="14%"
          r="82%"
        >
          <stop
            offset="0%"
            class="ws-lamp-core"
          />
          <stop
            offset="55%"
            class="ws-lamp-mid"
          />
          <stop
            offset="100%"
            class="ws-lamp-edge"
          />
        </radialGradient>
        <clipPath id="ws-pane-clip">
          <path d="M126 272 V102 Q126 56 176 54 H322 Q372 56 372 102 V272 Z" />
        </clipPath>
      </defs>

      <!-- 柔和手绘墙面底晕：无硬角矩形边界，让画面稳稳融在纸面上 -->
      <path
        class="scene-wall-wash"
        d="M72 44 C156 26 404 28 482 48 C500 132 500 258 488 332 H64 C54 252 56 132 72 44 Z"
      />

      <!-- 窗内远景层：渐变晴空、缓慢流云、暖阳晕散与三层远山小屋 -->
      <g clip-path="url(#ws-pane-clip)">
        <rect
          x="120"
          y="48"
          width="260"
          height="230"
          fill="url(#ws-sky-grad)"
        />
        <circle
          class="scene-sun-halo mul"
          cx="298"
          cy="112"
          r="48"
          fill="url(#ws-sun-halo)"
        />
        <circle
          class="scene-sun-ring mul"
          cx="298"
          cy="112"
          r="28"
        />
        <circle
          class="scene-sun mul"
          cx="298"
          cy="112"
          r="20"
        />

        <!-- 飘过的流云 -->
        <g class="scene-clouds">
          <path
            class="scene-cloud"
            d="M142 132 Q158 117 178 126 Q196 112 218 124 Q234 119 246 132 Z"
          />
          <path
            class="scene-cloud is-soft"
            d="M276 156 Q292 146 310 152 Q324 143 342 152 Q354 149 362 158 Z"
          />
        </g>

        <!-- 三层起伏远山与网点叠印 -->
        <path
          class="scene-hill-far mul"
          d="M120 220 Q176 176 238 206 Q298 228 378 192 V276 H120 Z"
        />
        <path
          class="scene-hill-mid mul"
          d="M120 236 Q196 188 274 224 Q326 202 378 226 V276 H120 Z"
        />
        <path
          class="scene-dots mul"
          d="M120 236 Q196 188 274 224 Q326 202 378 226 V276 H120 Z"
        />
        <!-- 远坡上的小小房子剪影 -->
        <g class="scene-distant-home">
          <path d="M188 214 l12-9 12 9 v12 h-24 z" />
          <path d="M204 203 h4 v7" />
        </g>
        <path
          class="scene-hill-near mul"
          d="M120 250 Q188 226 266 244 Q328 258 378 242 V276 H120 Z"
        />

        <!-- 窗玻璃淡淡斜反光 -->
        <path
          class="scene-glass-sheen"
          d="M148 56 L186 56 L132 162 L132 118 Z M212 56 L232 56 L132 232 L132 204 Z"
        />

        <!-- 左侧轻薄纱帘，随微风极轻摆动 -->
        <g class="scene-curtain">
          <path
            class="scene-curtain-fill"
            d="M124 52 Q168 58 176 112 Q162 170 174 224 Q158 248 168 274 H124 Z"
          />
          <path
            class="scene-curtain-fold"
            d="M140 56 Q150 120 142 192 Q148 232 140 272 M156 58 Q166 116 156 184"
          />
        </g>
      </g>

      <!-- 拱顶木窗框与分格窗棂 -->
      <path
        class="scene-window-trim-back"
        d="M126 272 V102 Q126 56 176 54 H322 Q372 56 372 102 V272 M249 54 V272 M126 154 H372"
      />
      <path
        class="scene-window-trim-ink"
        d="M126 272 V102 Q126 56 176 54 H322 Q372 56 372 102 V272 M249 55 V272 M126 154 H372"
      />
      <!-- 窗台板 -->
      <path
        class="scene-sill-shadow mul"
        d="M114 278 H386 L378 286 H122 Z"
      />
      <path
        class="scene-sill"
        d="M110 272 H388 Q392 272 392 276 Q392 280 388 280 H110 Q106 280 106 276 Q106 272 110 272 Z"
      />

      <!-- 台灯漫射暖光晕（软径向渐变，先于桌面器物落层） -->
      <path
        class="scene-lamp-glow mul"
        d="M362 198 C306 236 248 288 224 334 H438 C428 278 408 228 386 200 Z"
        fill="url(#ws-lamp-glow)"
      />

      <!-- 桌面与投下的器物柔影 -->
      <path
        class="scene-table"
        d="M52 332 H496 Q518 332 528 352 Q530 356 522 356 H32 Q24 356 27 351 Z"
      />
      <path
        class="scene-table-edge"
        d="M34 354 H522 Q524 363 516 364 H40 Q32 363 34 354 Z"
      />
      <g class="scene-desk-shadows mul">
        <ellipse
          cx="96"
          cy="335"
          rx="26"
          ry="4.5"
        />
        <ellipse
          cx="216"
          cy="335"
          rx="74"
          ry="5"
        />
        <ellipse
          cx="320"
          cy="335"
          rx="22"
          ry="4.2"
        />
        <ellipse
          cx="422"
          cy="335"
          rx="36"
          ry="5"
        />
      </g>

      <!-- 左：手感陶盆与枝叶舒展的小绿植 -->
      <g class="scene-plant">
        <g class="scene-foliage">
          <path
            class="scene-stem"
            d="M96 306 Q93 280 80 264 M97 306 Q100 272 116 258 M97 306 Q96 284 98 250"
          />
          <path
            class="scene-leaf-tint mul"
            d="M92 288 C74 286 64 274 68 266 C80 266 90 276 92 288 Z M101 282 C118 280 128 266 123 258 C110 260 102 270 101 282 Z M98 262 C88 246 92 232 100 232 C106 242 104 254 98 262 Z"
          />
          <path
            class="scene-leaf-line"
            d="M92 288 C74 286 64 274 68 266 C80 266 90 276 92 288 Z M91 287 Q78 277 71 269 M101 282 C118 280 128 266 123 258 C110 260 102 270 101 282 Z M102 281 Q113 271 120 262 M98 262 C88 246 92 232 100 232 C106 242 104 254 98 262 Z M98 260 Q98 246 100 236"
          />
        </g>
        <path
          class="scene-pot mul"
          d="M72 304 H120 L114 333 Q113 335 110 335 H82 Q79 335 78 333 Z"
        />
        <path
          class="scene-pot-rim"
          d="M68 302 Q96 299 124 302 Q125 308 122 309 Q96 307 70 309 Q67 308 68 302 Z"
        />
        <path
          class="scene-pot-stripe"
          d="M77 316 Q96 319 115 316"
        />
      </g>

      <!-- 中左：弧页摊开的手账本、丝带书签与斜搁的钢笔 -->
      <g class="scene-notebook">
        <path
          class="scene-book-stack"
          d="M148 314 Q184 307 216 318 Q248 307 284 314 V332 Q248 326 216 334 Q184 326 148 332 Z"
        />
        <path
          class="scene-book"
          d="M150 302 Q184 294 216 308 Q248 294 282 302 V325 Q248 318 216 327 Q184 318 150 325 Z"
        />
        <path
          class="scene-ink-line"
          d="M150 302 Q184 294 216 308 Q248 294 282 302 V325 Q248 318 216 327 Q184 318 150 325 Z M216 308 V327"
        />
        <path
          class="scene-book-lines"
          d="M161 308 Q184 303 205 311 M161 314 Q184 309 205 317 M228 311 Q249 304 271 308 M228 317 Q249 310 271 314"
        />
        <path
          class="scene-ribbon mul"
          d="M222 325 C224 334 220 342 224 348 L229 343 L234 348 C231 340 231 333 229 324 Z"
        />
        <!-- 钢笔静静搁在书页旁 -->
        <g class="scene-pen">
          <path d="M273 331 L297 315" />
          <path
            class="scene-pen-nib"
            d="M273 331 L268 334 L271 329 Z"
          />
        </g>
      </g>

      <!-- 中右：暖手冲杯与缓缓升起的热气 -->
      <g class="scene-mug-group">
        <path
          class="scene-steam is-a"
          d="M312 296 Q307 286 314 277 Q320 268 313 259"
        />
        <path
          class="scene-steam is-b"
          d="M324 295 Q329 286 323 277 Q318 269 325 262"
        />
        <path
          class="scene-mug-handle"
          d="M335 311 Q346 311 346 320 Q346 329 334 329"
        />
        <path
          class="scene-mug-body"
          d="M304 304 Q320 301 336 304 L334 330 Q334 334 320 334 Q306 334 306 330 Z"
        />
        <path
          class="scene-mug-band mul"
          d="M305 315 Q320 317 335 315 L334 324 Q320 326 306 324 Z"
        />
      </g>

      <!-- 右：优雅弯颈读信台灯 -->
      <g class="scene-lamp-group">
        <path
          class="scene-lamp"
          d="M426 329 C428 260 444 198 386 196"
        />
        <path
          class="scene-lamp-base"
          d="M394 334 Q396 324 426 324 Q456 324 458 334 Z"
        />
        <path
          class="scene-shade mul"
          d="M350 202 Q370 176 398 188 L392 214 Q368 220 350 202 Z"
        />
        <path
          class="scene-shade-rim"
          d="M350 202 Q370 176 398 188 L392 214 Q368 220 350 202 Z"
        />
        <circle
          class="scene-bulb"
          cx="368"
          cy="211"
          r="5"
        />
      </g>
    </svg>
    <figcaption>
      <HandNote
        size="sm"
        :tilt="-2"
        >{{ handCopy.sceneCaption }}</HandNote
      >
      <span>画出来的小家 · 不是实时画面</span>
    </figcaption>
  </figure>
</template>
